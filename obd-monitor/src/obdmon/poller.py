"""Leitura contínua: conexão, inicialização, otimizações de velocidade com
detecção automática, ciclos rápido/médio/lento, DTCs, watchdog e reconexão
automática com backoff.

Escalonador (documentado no README):
- Ciclo rápido = RPM → velocidade → arrefecimento → carga → acelerador
  (SEMPRE), mais o óleo (5C) se for suportado e couber no orçamento de 0,5 s.
  Com multi-PID, vários PIDs vão numa só requisição ("010C0D0504111").
- Depois de cada rodada rápida COMPLETA roda no máximo 1 tarefa extra (PID
  médio/lento ou leitura de DTC 03/07/0A), ou 2 se houver fila. Uma leitura
  de DTC nunca entra no meio de uma rodada rápida.
- O poller não espera o tique de 0,5 s do log: emenda uma rodada na outra.
  O log só tira uma "foto" dos últimos valores a cada 0,5 s.

Otimizações (cada uma com detecção e fallback para o modo seguro):
- protocolo fixo (ATSP<n>) depois da detecção;
- sufixo de respostas esperadas ("010C1");
- multi-PID (até 6 por requisição);
- ATAT/ATST (só com valores escolhidos pelo benchmark, nunca no escuro);
- escrita BLE sem resposta (se o adaptador não responder, volta para com resposta).
Se uma otimização gerar resposta inválida/incompleta/trocada ou valor fora da
faixa física, ela é desligada até o fim da sessão (registrado no programa.log,
na coluna "evento" do CSV e avisado na interface).
"""
from __future__ import annotations

import asyncio
import copy
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Optional

from . import dtc as dtcmod
from . import events as ev
from .elm import ELM327, EcuNotResponding, ElmInfo, ElmTimeout, GroupResult, PidResult, Tuning
from .pids import BY_PID, FAST, FAST_CORE, MEDIUM, PIDS, SLOW, plausible
from .safety import MAX_PIDS_PER_REQUEST, ForbiddenCommandError
from .transport.base import Transport, TransportError

log = logging.getLogger(__name__)

BACKOFF = (1.0, 2.0, 5.0, 10.0)
WATCHDOG_WARN_S = 3.0
WATCHDOG_LOST_S = 8.0
MEDIUM_PERIOD = 2.0
DEMOTED_PERIOD = 1.0
SLOW_PERIOD = 30.0
DTC_PERIOD = 5.0
DTC_PERMANENT_PERIOD = 60.0
RETRY_UNSUPPORTED = 60.0
NODATA_LIMIT = 5
DEFAULT_LAT = 0.12
HZ_WINDOW = 5.0
LOG_STATS_EVERY = 30.0

# Erros de uma requisição otimizada que indicam que o ADAPTADOR não lida bem
# com ela (≠ TIMEOUT/NO DATA, que também acontecem no modo seguro).
BAD_RESPONSE = ("?", "INCOMPLETA", "TRUNCADA", "INCONSISTENTE", "INVALIDA")

# Tolerância para comparar uma leitura otimizada com a leitura individual
# (os valores mudam entre uma consulta e outra, principalmente RPM e carga).
TOLERANCE = {0x0C: 400.0, 0x0D: 5.0, 0x05: 3.0, 0x04: 25.0, 0x11: 15.0, 0x5C: 3.0, 0x0F: 3.0,
             0x42: 1.0, 0x46: 3.0}

# Estados
ST_STOPPED = "parado"
ST_CONNECTING = "conectando"
ST_INIT = "inicializando"
ST_WAIT_ECU = "aguardando_ecu"
ST_CONNECTED = "conectado"
ST_RECONNECT_WAIT = "reconectando"
ST_SELFTEST = "autoteste"


class ConnectionLost(Exception):
    pass


def rate(times: list[float], now: float) -> float:
    """Eventos por segundo a partir dos instantes (janela já filtrada).
    Usa o intervalo real coberto (não a janela cheia): logo depois de conectar
    a taxa não aparece artificialmente baixa. Sem evento há mais de 2
    intervalos médios, a taxa cai (dado parou de chegar)."""
    if len(times) < 2:
        return 0.0
    span = times[-1] - times[0]
    if span <= 0:
        return 0.0
    r = (len(times) - 1) / span
    gap = now - times[-1]
    if gap > 2.0 / r:                  # parou de chegar: conta o silêncio também
        r = (len(times) - 1) / (now - times[0])
    return r


def is_bad_response(err: Optional[str]) -> bool:
    return bool(err) and any(err.startswith(b) for b in BAD_RESPONSE)


def values_agree(pid: int, v: float, refs: list[float]) -> bool:
    """O valor otimizado bate com as leituras individuais (antes/depois)?"""
    refs = [r for r in refs if r is not None]
    if not refs:
        return False
    tol = TOLERANCE.get(pid, 5.0)
    return min(refs) - tol <= v <= max(refs) + tol


@dataclass
class ReqStats:
    """Tempos médios (EWMA, ms) das requisições: escrita, até o 1º byte, espera do '>'."""
    n: int = 0
    write: float = 0.0
    first: float = 0.0
    wait: float = 0.0
    total: float = 0.0
    max_total: float = 0.0

    def add(self, tm) -> None:
        if tm is None:
            return
        a = 0.1 if self.n > 10 else 1.0 / (self.n + 1)
        self.n += 1
        self.write += a * (tm.write - self.write)
        self.first += a * (tm.first - self.first)
        self.wait += a * (tm.wait - self.wait)
        self.total += a * (tm.total - self.total)
        self.max_total = max(self.max_total, tm.total)


@dataclass
class PidStats:
    pid: int
    supported: Optional[bool] = None   # pela máscara (None = não sabe)
    runtime_unsupported: bool = False  # NO DATA repetido
    ok: int = 0
    fails: int = 0
    nodata_streak: int = 0
    lat_ewma: Optional[float] = None
    lat_min: Optional[float] = None
    lat_max: Optional[float] = None
    lat_sum: float = 0.0
    last_error: str = ""
    cycle: str = ""
    last_t: Optional[float] = None     # monotonic da última leitura válida (chegada da resposta)
    times: deque = field(default_factory=lambda: deque(maxlen=200))

    def record(self, lat: float, ok: bool, t: Optional[float] = None) -> None:
        if ok:
            self.ok += 1
            self.lat_sum += lat
            self.lat_ewma = lat if self.lat_ewma is None else 0.8 * self.lat_ewma + 0.2 * lat
            self.lat_min = lat if self.lat_min is None else min(self.lat_min, lat)
            self.lat_max = lat if self.lat_max is None else max(self.lat_max, lat)
            if t is not None:
                self.last_t = t
                self.times.append(t)
        else:
            self.fails += 1

    @property
    def lat_avg(self) -> Optional[float]:
        return self.lat_sum / self.ok if self.ok else None

    def hz(self, now: Optional[float] = None, window: float = HZ_WINDOW) -> float:
        """Leituras válidas por segundo nos últimos ``window`` segundos."""
        now = time.monotonic() if now is None else now
        return rate([t for t in self.times if t >= now - window], now)

    def age(self, now: Optional[float] = None) -> Optional[float]:
        if self.last_t is None:
            return None
        return (time.monotonic() if now is None else now) - self.last_t

    @property
    def usable(self) -> bool:
        return self.supported is not False and not self.runtime_unsupported


@dataclass
class DtcEntry:
    code: str
    modes: set = field(default_factory=set)
    first_seen: datetime = field(default_factory=datetime.now)
    missing: int = 0

    @property
    def description(self) -> str:
        return dtcmod.describe(self.code)


class Poller:
    def __init__(self, transport_factory: Callable[[], Transport], *,
                 budget: float = 0.5,
                 on_event: Callable[[str, str], None] = lambda c, d: None,
                 on_status: Callable[[str, str], None] = lambda s, d: None,
                 on_dtc: Callable[[dict], None] = lambda d: None,
                 on_values: Callable[[], None] = lambda: None,
                 on_reading: Callable[[int, float, float, float], None] = lambda p, v, t, lat: None,
                 on_notice: Callable[[str], None] = lambda m: None,
                 search_timeout: float = 12.0,
                 backoff: tuple[float, ...] = BACKOFF,
                 lost_after: float = WATCHDOG_LOST_S,
                 tuning: Optional[Tuning] = None,
                 optimize: bool = True) -> None:
        self.factory = transport_factory
        self.budget = budget
        self.on_event = on_event
        self.on_status = on_status
        self.on_dtc = on_dtc
        self.on_values = on_values
        self.on_reading = on_reading
        self.on_notice = on_notice
        self.search_timeout = search_timeout
        self.backoff = backoff
        self.lost_after = lost_after
        self.optimize = optimize
        self.tuning: Tuning = copy.copy(tuning) if tuning is not None else Tuning(source="padrao")
        self.active: Tuning = Tuning.safe()     # o que está em uso nesta conexão
        self.disabled: dict[str, str] = {}      # otimização -> motivo (vale até o fim da sessão)
        self._retune = False
        self._session_tuning: Optional[Tuning] = None   # resultado da detecção (reconexões)

        self.status = ST_STOPPED
        self.status_detail = ""
        self.values: dict[str, tuple[float, float]] = {}   # key -> (valor, monotonic da resposta)
        self.last_valid = 0.0
        self.ever_connected = False
        self.lost = False
        self.lost_since: Optional[float] = None
        self.reconnect_attempt = 0
        self.connections = 0
        self.stats: dict[int, PidStats] = {p.pid: PidStats(p.pid, cycle=p.cycle) for p in PIDS}
        self.req_single = ReqStats()
        self.req_multi = ReqStats()
        self.supported: Optional[set[int]] = None
        self.fast: list[int] = []
        self.groups: list[list[int]] = []
        self.medium: list[int] = []
        self.slow: list[int] = []
        self.demoted: list[int] = []
        self.round_ewma: Optional[float] = None
        self.default_lat = DEFAULT_LAT
        self.hz = 0.0
        self.rounds_total = 0              # rodadas rápidas completas (todos os PIDs rápidos com valor)
        self.poll_time_total = 0.0         # tempo total dentro do laço de leitura
        self._round_times: list[float] = []
        self.dtcs: dict[str, DtcEntry] = {}
        self._dtc_last: dict[int, set] = {}
        self.dtc_reads = 0
        self.dtc_read_ms: Optional[float] = None
        self.elm: Optional[ELM327] = None
        self.elm_info: ElmInfo = ElmInfo()
        self.transport: Optional[Transport] = None
        self.transport_info: dict = {}
        self.last_error = ""
        self._stop = asyncio.Event()
        self._link_down = asyncio.Event()
        self._pause_req = False
        self._paused = asyncio.Event()
        self._resume = asyncio.Event()
        self._reported_unsupported: set[int] = set()
        self._due: dict[str, float] = {}
        self._last_stats_log = 0.0

    # -- utilidades -------------------------------------------------------------
    def _set_status(self, st: str, detail: str = "") -> None:
        self.status, self.status_detail = st, detail
        self.on_status(st, detail)

    def stop(self) -> None:
        self._stop.set()

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    @property
    def data_age(self) -> Optional[float]:
        return None if not self.last_valid else time.monotonic() - self.last_valid

    @property
    def avg_hz(self) -> float:
        """Média da viagem: rodadas rápidas completas / tempo lendo."""
        return self.rounds_total / self.poll_time_total if self.poll_time_total > 0 else 0.0

    def current(self, key: str, max_age: float) -> Optional[float]:
        v = self.values.get(key)
        if v is None or time.monotonic() - v[1] > max_age:
            return None
        return v[0]

    def value_age(self, key: str) -> Optional[float]:
        v = self.values.get(key)
        return None if v is None else time.monotonic() - v[1]

    async def _sleep_or_stop(self, s: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), s)
        except asyncio.TimeoutError:
            pass

    def _disable(self, what: str, reason: str) -> None:
        """Desliga uma otimização até o fim da sessão e avisa."""
        if what in self.disabled:
            return
        self.disabled[what] = reason
        names = {"multi": "multi-PID", "sufixo": "sufixo de respostas esperadas",
                 "protocolo": "protocolo fixo", "timing": "ATAT/ATST do benchmark",
                 "escrita_sem_resposta": "escrita BLE sem resposta"}
        msg = f"Otimização desativada: {names.get(what, what)} – {reason}. Voltando ao modo seguro."
        log.warning(msg)
        self.on_notice(msg)
        self.on_event(f"{ev.OTIMIZACAO_DESATIVADA}:{what}", reason[:100])
        if what == "multi":
            self.active.multi = 1
            self._make_groups()
        elif what == "sufixo":
            self.active.suffix = False
        elif what == "escrita_sem_resposta":
            self.active.write_with_response = True
            self.tuning.write_with_response = True

    # -- laço principal ---------------------------------------------------------
    async def run(self) -> None:
        while not self.stopped:
            transport = None
            try:
                self._set_status(ST_CONNECTING, "Conectando ao adaptador...")
                transport = self.factory()
                self._link_down.clear()
                transport.set_disconnect_callback(self._link_down.set)
                await transport.connect()
                self.transport = transport
                # modo seguro = como era antes: escrita COM resposta (se existir)
                wwr = self.tuning.write_with_response if self.optimize else True
                if "escrita_sem_resposta" in self.disabled:
                    wwr = True
                if wwr is not None:
                    transport.set_write_with_response(wwr)
                self.transport_info = transport.describe()
                self.elm = ELM327(transport)
                self._set_status(ST_INIT, "Inicializando o ELM327 (ATZ...)")
                await self._init_elm()
                if self.supported is None:
                    await self._discover()
                self.connections += 1
                self.last_valid = time.monotonic()
                self.ever_connected = True
                self.reconnect_attempt = 0
                if self.lost:
                    self.lost = False
                    self.lost_since = None
                    self.on_event(ev.CONEXAO_RESTABELECIDA, "")
                # já "conectado": as leituras de referência da detecção vão para o log
                self._set_status(ST_CONNECTED, "Recebendo dados (ajustando a velocidade de leitura...)")
                await self._setup_tuning()
                self._compute_cycles()
                self.transport_info = transport.describe()
                self._set_status(ST_CONNECTED, "Recebendo dados")
                await self._poll_loop()
            except asyncio.CancelledError:
                raise
            except ForbiddenCommandError:
                raise  # bug grave: nunca deveria acontecer
            except (TransportError, ElmTimeout, ConnectionLost, EcuNotResponding, OSError) as e:
                self.last_error = str(e)
                log.warning("Conexão: %s", e)
            except Exception as e:  # nunca morrer em silêncio
                self.last_error = f"Erro inesperado: {e!r}"
                log.exception("Erro inesperado no poller")
            finally:
                if transport is not None:
                    try:
                        await transport.disconnect()
                    except Exception:
                        pass
                self.elm = None
            if self.stopped:
                break
            if self.ever_connected and not self.lost:
                self.lost = True
                self.lost_since = time.monotonic()
                self.on_event(ev.CONEXAO_PERDIDA, self.last_error[:120])
            delay = self.backoff[min(self.reconnect_attempt, len(self.backoff) - 1)]
            self.reconnect_attempt += 1
            self._set_status(ST_RECONNECT_WAIT,
                             f"Tentando reconectar em {delay:g} s (tentativa {self.reconnect_attempt}) – {self.last_error[:90]}")
            await self._sleep_or_stop(delay)
        self._set_status(ST_STOPPED, "Desconectado")

    def _fixed_protocol_wanted(self) -> str:
        t = self._session_tuning or self.tuning
        if self.optimize and t.fix_protocol and t.protocol and "protocolo" not in self.disabled:
            return t.protocol
        return ""

    async def _init_elm(self) -> None:
        elm = self.elm
        assert elm is not None and self.transport is not None
        proto = self._fixed_protocol_wanted()
        try:
            await elm.configure(protocol=proto)
        except ElmTimeout:
            # escrita sem resposta pode não funcionar em alguns adaptadores BLE
            tr = self.transport
            if tr.write_with_response is False and True in tr.write_modes:
                tr.set_write_with_response(True)
                self._disable("escrita_sem_resposta", "o adaptador não respondeu aos comandos AT")
                await elm.configure(protocol=proto)
            else:
                raise
        while True:
            try:
                await elm.probe_ecu(self.search_timeout)
                break
            except EcuNotResponding as e:
                if proto:
                    # protocolo fixo pode não servir: volta para a busca automática
                    log.info("Protocolo fixo ATSP%s não respondeu (%s); voltando ao automático", proto, e)
                    await elm.set_protocol("0")
                    proto = ""
                    continue
                # adaptador ok, carro não responde: insiste sem derrubar o Bluetooth
                self.last_error = str(e)
                self._set_status(ST_WAIT_ECU, "Adaptador conectado, mas o carro não responde "
                                              "(ignição desligada?). Tentando de novo...")
                await self._sleep_or_stop(3.0)
                if self.stopped:
                    raise ConnectionLost("parado") from None
                self._check_link()
        self.elm_info = await elm.read_protocol_info()

    async def _discover(self) -> None:
        assert self.elm is not None
        t0 = time.monotonic()
        n0 = len(self.elm.sent_log)
        sup = await self.elm.supported_pids()
        n = len(self.elm.sent_log) - n0
        if n:
            self.default_lat = (time.monotonic() - t0) / n
        self.supported = sup
        for p in PIDS:
            st = self.stats[p.pid]
            st.supported = None if sup is None else (p.pid in sup)
            if st.supported is False and p.pid not in self._reported_unsupported:
                self._reported_unsupported.add(p.pid)
                self.on_event(f"{ev.PID_NAO_SUPORTADO}:{p.hex}", "pela máscara 0100/0120/0140")

    # -- otimizações: detecção e aplicação ------------------------------------
    def _fast_candidates(self) -> list[int]:
        """Os 5 do núcleo + óleo (5C), se o carro tiver: candidatos ao grupo multi-PID."""
        core = [p for p in FAST_CORE if self.stats[p].usable]
        extra = [p.pid for p in sorted(PIDS, key=lambda d: d.priority)
                 if p.cycle == FAST and p.pid not in FAST_CORE and self.stats[p.pid].usable]
        return core + extra

    async def _individual(self, pid: int, suffix: bool = False) -> Optional[float]:
        """Leitura de referência (detecção). O valor válido também vai para o log."""
        assert self.elm is not None
        r = await self.elm.query_pid(pid, suffix=suffix)
        if r.ok and r.value is not None and plausible(pid, r.value):
            self._accept(r)
            return r.value
        self._check_alive()     # watchdog também durante a detecção (adaptador travado)
        return None

    async def _setup_tuning(self) -> None:
        """Decide as otimizações desta conexão. Sem benchmark salvo: detecta
        sufixo e multi-PID (comparando com leituras individuais) e fixa o
        protocolo. ATAT/ATST só com valores medidos pelo benchmark."""
        elm = self.elm
        assert elm is not None
        # reconexão na mesma sessão: reaproveita o que já foi detectado (só confere)
        req = self._session_tuning or self.tuning
        act = Tuning.safe()
        act.write_with_response = self.transport.write_with_response if self.transport else None
        if not self.optimize:
            self.active = act
            self._make_groups()
            log.info("Otimizações desligadas nas Configurações: modo seguro")
            return
        act.source = req.source if req.source in ("benchmark", "auto") else "auto"
        info = self.elm_info
        # 1) protocolo fixo
        num = info.protocol_num
        act.protocol = num or req.protocol
        if req.fix_protocol and "protocolo" not in self.disabled and num and num != "0":
            if info.extra.get("protocolo_auto", True):
                if await elm.set_protocol(num) and await self._individual(self._first_fast()) is not None:
                    act.fix_protocol = True
                    log.info("Protocolo fixado: ATSP%s", num)
                else:
                    await elm.set_protocol("0")
                    self._disable("protocolo", f"ATSP{num} não funcionou")
            else:
                act.fix_protocol = True          # já veio fixo do configure()
        # 2) ATAT/ATST (só do benchmark)
        if (req.adaptive is not None or req.st is not None) and "timing" not in self.disabled:
            await elm.apply_timing(req.adaptive, req.st)
            if await self._individual(self._first_fast()) is not None:
                act.adaptive, act.st = req.adaptive, req.st
            else:
                await elm.apply_timing(None, None)
                self._disable("timing", f"ATAT{req.adaptive}/ATST{req.st} deram erro")
        fast = self._fast_candidates()
        if not fast:
            self.active = act
            self._make_groups()
            return
        trusted = req.source in ("benchmark", "sessao")
        self.active = act        # a detecção do multi usa o sufixo já decidido
        # 3) sufixo
        if "sufixo" not in self.disabled and (req.suffix or not trusted):
            act.suffix = await self._detect_suffix(fast[0])
            if not act.suffix and trusted:
                self._disable("sufixo", "o adaptador recusou o sufixo nesta conexão")
        # 4) multi-PID
        if "multi" not in self.disabled and (req.multi > 1 or not trusted):
            want = req.multi if trusted else MAX_PIDS_PER_REQUEST
            n, msuf, why = await self._detect_multi(fast, act.suffix and req.multi_suffix, want)
            act.multi, act.multi_suffix = n, msuf
            if n == 1:
                log.info("Multi-PID não será usado: %s", why)
                if trusted:
                    self._disable("multi", why)
        self.active = act
        self._make_groups()
        if req.source != "benchmark":
            self._session_tuning = copy.copy(act)
            self._session_tuning.source = "sessao"
        log.info("Modo de leitura (%s): %s", act.source, act.describe())

    def _first_fast(self) -> int:
        f = self._fast_candidates()
        return f[0] if f else 0x0C

    async def _detect_suffix(self, pid: int) -> bool:
        """2 leituras com "01xx1" precisam vir válidas e bater com a leitura normal."""
        for _ in range(2):
            ref = await self._individual(pid)
            v = await self._individual(pid, suffix=True)
            if v is None or ref is None or not values_agree(pid, v, [ref]):
                log.info("Sufixo de respostas esperadas NÃO funcionou neste adaptador")
                return False
        log.info("Sufixo de respostas esperadas funciona (01%02X1)", pid)
        return True

    async def _detect_multi(self, fast: list[int], suffix: bool, want: int) -> tuple[int, bool, str]:
        """Maior grupo (≤ want) cuja resposta combinada vem completa e bate com
        as leituras individuais (2 vezes seguidas). Devolve (n, sufixo, motivo)."""
        assert self.elm is not None
        top = min(want, len(fast), MAX_PIDS_PER_REQUEST)
        if top < 2:
            return 1, False, "um PID só"

        async def try_n(n: int, reps: int = 2) -> tuple[bool, bool, str]:
            why = ""
            for use_suffix in ((True, False) if suffix else (False,)):
                ok = await self._check_group(fast[:n], use_suffix, reps)
                if ok == "":
                    log.info("Multi-PID funciona com %d PIDs por requisição%s", n,
                             " (com sufixo)" if use_suffix else "")
                    return True, use_suffix, ""
                why = f"grupo de {n}: {ok}"
                if ok.startswith("?"):
                    break       # '?' = não entende multi-PID; sem sufixo não muda nada
            return False, False, why

        # grupo de 2 primeiro (1 frame CAN, o caso mais simples): se nem ele
        # funciona, o adaptador não aceita multi-PID e a detecção termina rápido
        ok2, suf2, why = await try_n(2, reps=1 if top > 2 else 2)   # só o "portão"
        if not ok2:
            return 1, False, why
        for n in range(top, 2, -1):
            ok, suf, _ = await try_n(n)
            if ok:
                return n, suf, ""
        return 2, suf2, ""

    async def _check_group(self, group: list[int], suffix: bool, reps: int = 2) -> str:
        """'' se o grupo passou; senão o motivo."""
        assert self.elm is not None
        before = {p: await self._individual(p, suffix=self.active.suffix) for p in group}
        for _ in range(reps):   # vezes seguidas, cada uma entre leituras individuais
            g = await self.elm.query_group(group, suffix=suffix)
            if not g.ok:
                self._check_alive()
                return g.error or "erro"
            after = {p: await self._individual(p, suffix=self.active.suffix) for p in group}
            for p in group:
                v = g.results[p].value
                if v is None or not plausible(p, v):
                    return f"valor fora da faixa no PID {p:02X}"
                if not values_agree(p, v, [before[p], after[p]]):
                    return f"PID {p:02X} não bate com a leitura individual"
            before = after
        return ""

    def _make_groups(self) -> None:
        n = max(1, self.active.multi)
        self.groups = [self.fast[i:i + n] for i in range(0, len(self.fast), n)]

    def set_tuning(self, tuning: Tuning) -> None:
        """Nova configuração (ex.: benchmark). Aplicada na volta da pausa."""
        self.tuning = copy.copy(tuning)
        self._session_tuning = None
        self.disabled.clear()
        self._retune = True

    def _compute_cycles(self) -> None:
        usable = [p for p in PIDS if self.stats[p.pid].usable]
        core = [p for p in FAST_CORE if self.stats[p].usable]
        extra_fast = sorted((p for p in usable if p.cycle == FAST and p.pid not in FAST_CORE),
                            key=lambda p: p.priority)
        limit = self.budget * 0.85
        n = max(1, self.active.multi)
        # tempo estimado da rodada: uma requisição por grupo
        per_req = [self.stats[p].lat_ewma or self.default_lat for p in core]
        est = sum(per_req[i] for i in range(0, len(per_req), n))
        fast, demoted = list(core), []
        for p in extra_fast:
            lat = self.stats[p.pid].lat_ewma or self.default_lat
            if (len(fast) % n) != 0:      # cabe no último grupo sem requisição a mais
                lat = 0.0
            if est + lat <= limit:
                fast.append(p.pid)
                est += lat
            else:
                demoted.append(p.pid)
        self.fast = fast
        self.demoted = demoted
        self._make_groups()
        self.medium = [p.pid for p in usable if p.cycle == MEDIUM]
        self.slow = [p.pid for p in usable if p.cycle == SLOW]
        for p in PIDS:
            st = self.stats[p.pid]
            if not st.usable:
                st.cycle = "não suportado"
            elif p.pid in fast:
                st.cycle = FAST
            elif p.pid in demoted:
                st.cycle = "rebaixado (1 s)"
            else:
                st.cycle = p.cycle

    def _check_link(self) -> None:
        if self._link_down.is_set() or (self.transport is not None and not self.transport.is_connected):
            raise ConnectionLost("Bluetooth desconectado")

    def _check_alive(self) -> None:
        self._check_link()
        if self.last_valid and time.monotonic() - self.last_valid > self.lost_after:
            raise ConnectionLost(f"Nenhum dado válido há {self.lost_after:g} s")

    # -- ciclo de leitura ---------------------------------------------------
    async def _poll_loop(self) -> None:
        now = time.monotonic()
        due = self._due
        for pid in self.medium:
            due.setdefault(f"pid:{pid:02X}", now)
        for pid in self.slow:
            due.setdefault(f"pid:{pid:02X}", now + 1.0)
        due.setdefault("dtc:03", now + 0.5)
        due.setdefault("dtc:07", now + 1.5)
        due.setdefault("dtc:0A", now + 15.0)
        last_rebalance = now - 7.0  # primeiro rebalanceamento após ~3 s
        self._last_stats_log = now
        while not self.stopped:
            if self._pause_req:
                self._paused.set()
                await self._resume.wait()
                self._resume.clear()
                self._paused.clear()
                if self._retune:
                    self._retune = False
                    await self._setup_tuning()
                    self._compute_cycles()
                self.last_valid = time.monotonic()
            t0 = time.monotonic()
            got = 0
            for group in list(self.groups):
                got += await self._query_group(group)
                self._check_alive()
            if not self.fast:
                await asyncio.sleep(0.2)
            complete = bool(self.fast) and got == len(self.fast)
            # tarefas extras vencidas: sempre DEPOIS da rodada rápida
            now = time.monotonic()
            for pid in self.demoted:
                due.setdefault(f"pid:{pid:02X}", now)
            for pid, st in self.stats.items():
                if st.runtime_unsupported:
                    due.setdefault(f"retry:{pid:02X}", now + RETRY_UNSUPPORTED)
            ready = sorted((t, k) for k, t in due.items() if t <= now)
            for _, key in ready[: (2 if len(ready) > 3 else 1)]:
                await self._run_task(key)
                self._check_alive()
            end = time.monotonic()
            rt = end - t0
            self.poll_time_total += rt
            self.round_ewma = rt if self.round_ewma is None else 0.85 * self.round_ewma + 0.15 * rt
            if complete:
                self.rounds_total += 1
                self._round_times.append(end)
            while self._round_times and self._round_times[0] < end - HZ_WINDOW:
                self._round_times.pop(0)
            self.hz = rate(self._round_times, end)
            self.on_values()
            if end - last_rebalance > 10.0:
                last_rebalance = end
                self._compute_cycles()
            if end - self._last_stats_log >= LOG_STATS_EVERY:
                self._last_stats_log = end
                log.info(self.stats_line())
            # sem pausas artificiais: só cede a vez ao loop (log/GUI). Se nada
            # respondeu e foi instantâneo (ex.: "?"), espera um pouco para não martelar.
            await asyncio.sleep(0.02 if (got == 0 and rt < 0.02) else 0)

    def stats_line(self) -> str:
        def part(name, r: ReqStats):
            if not r.n:
                return f"{name}: --"
            return (f"{name}: {r.total:.0f} ms (escrita {r.write:.0f} + até 1º byte {r.first:.0f} + "
                    f"espera do '>' {r.wait:.0f}; máx {r.max_total:.0f})")
        per = ", ".join(f"{BY_PID[p].key} {self.stats[p].hz():.1f} Hz".replace(".", ",") for p in self.fast)
        return (f"Leitura: {self.hz:.1f} rodadas/s agora, {self.avg_hz:.2f} média | "
                f"{part('requisição 1 PID', self.req_single)} | {part('multi-PID', self.req_multi)} | "
                f"{per} | modo: {self.active.describe()}")

    async def _run_task(self, key: str) -> None:
        kind, arg = key.split(":")
        now = time.monotonic()
        if kind == "pid":
            pid = int(arg, 16)
            if pid not in self.medium and pid not in self.slow and pid not in self.demoted:
                self._due.pop(key, None)
                return
            period = DEMOTED_PERIOD if pid in self.demoted else (SLOW_PERIOD if pid in self.slow else MEDIUM_PERIOD)
            self._due[key] = now + period
            await self._query(pid)
        elif kind == "retry":
            pid = int(arg, 16)
            self._due.pop(key, None)
            st = self.stats[pid]
            if st.runtime_unsupported:
                r = await self._query(pid, count_nodata=False)
                if r:
                    st.runtime_unsupported = False
                    st.nodata_streak = 0
                    self._compute_cycles()
        elif kind == "dtc":
            mode = int(arg, 16)
            self._due[key] = now + (DTC_PERMANENT_PERIOD if mode == 0x0A else DTC_PERIOD)
            await self._read_dtc(mode)

    async def _query_group(self, group: list[int]) -> int:
        """Lê um grupo do ciclo rápido. Devolve quantos PIDs vieram válidos.
        Se a requisição multi-PID falhar de um jeito "do adaptador", desliga o
        multi-PID e lê os mesmos PIDs um por um NESTA rodada (nada se perde)."""
        assert self.elm is not None
        if len(group) == 1 or self.active.multi <= 1:
            got = 0
            for p in group:
                got += int(await self._query(p))
            return got
        suffix = self.active.suffix and self.active.multi_suffix
        g: GroupResult = await self.elm.query_group(group, suffix=suffix)
        self.req_multi.add(g.timing)
        if g.ok:
            bad = [p for p in group if not plausible(p, g.results[p].value)]
            if not bad:
                for p in group:
                    self._accept(g.results[p])
                return len(group)
            self._disable("multi", f"valor fora da faixa física no PID {bad[0]:02X} ({g.results[bad[0]].value:g})")
        elif is_bad_response(g.error):
            self._disable("multi", f"resposta {g.error} para {g.cmd}")
        else:
            for p in group:   # TIMEOUT / NO DATA: igual ao modo seguro
                self.stats[p].record(g.latency, False)
                self.stats[p].last_error = g.error or ""
            return 0
        got = 0
        for p in group:
            got += int(await self._query(p))
        return got

    def _accept(self, r: PidResult) -> None:
        st = self.stats[r.pid]
        t = r.t_resp or time.monotonic()
        st.record(r.latency, True, t)
        self.values[BY_PID[r.pid].key] = (r.value, t)
        self.last_valid = time.monotonic()
        st.nodata_streak = 0
        try:
            self.on_reading(r.pid, r.value, t, r.latency)
        except Exception:
            log.exception("on_reading falhou")

    async def _query(self, pid: int, count_nodata: bool = True) -> bool:
        assert self.elm is not None
        suffix = self.active.suffix
        r = await self.elm.query_pid(pid, suffix=suffix)
        self.req_single.add(r.timing)
        st = self.stats[pid]
        if r.ok and r.value is not None:
            if plausible(pid, r.value):
                self._accept(r)
                return True
            r.ok, r.error = False, "FORA_DA_FAIXA"
            if suffix:
                self._disable("sufixo", f"valor fora da faixa física no PID {pid:02X} ({r.value:g})")
        elif suffix and r.error == "?":
            self._disable("sufixo", f"o adaptador respondeu '?' a 01{pid:02X}1")
        st.record(r.latency, False)
        st.last_error = r.error or ""
        if r.error == "NO DATA" and count_nodata:
            # só conta se outros PIDs estão respondendo (senão é o carro desligado)
            if self.last_valid and time.monotonic() - self.last_valid < 2.0:
                st.nodata_streak += 1
                if st.nodata_streak >= NODATA_LIMIT and not st.runtime_unsupported:
                    st.runtime_unsupported = True
                    self._compute_cycles()
                    if pid not in self._reported_unsupported:
                        self._reported_unsupported.add(pid)
                        self.on_event(f"{ev.PID_NAO_SUPORTADO}:{pid:02X}", f"NO DATA {NODATA_LIMIT}x seguidas")
        return False

    async def _read_dtc(self, mode: int) -> None:
        assert self.elm is not None
        t0 = time.monotonic()
        codes = await self.elm.read_dtcs(mode)
        ms = (time.monotonic() - t0) * 1000
        self.dtc_read_ms = ms if self.dtc_read_ms is None else 0.8 * self.dtc_read_ms + 0.2 * ms
        if codes is None:
            return
        self.dtc_reads += 1
        self.last_valid = time.monotonic()
        self.update_dtcs(mode, codes)

    def update_dtcs(self, mode: int, codes: list[str]) -> None:
        """Atualiza a tabela de DTCs; gera DTC_NOVO / DTC_APAGADO."""
        self._dtc_last[mode] = set(codes)
        label = {0x03: "confirmado", 0x07: "pendente", 0x0A: "permanente"}[mode]
        changed = False
        for c in codes:
            e = self.dtcs.get(c)
            if e is None:
                e = self.dtcs[c] = DtcEntry(c)
                self.on_event(f"{ev.DTC_NOVO}:{c}", label)
                changed = True
            if label not in e.modes:
                e.modes.add(label)
                changed = True
            e.missing = 0
        for c, e in list(self.dtcs.items()):
            if label in e.modes and c not in codes:
                e.modes.discard(label)
                changed = True
            if not e.modes:
                e.missing += 1
                if e.missing >= 2:  # some em 2 leituras seguidas: apagado
                    del self.dtcs[c]
                    self.on_event(f"{ev.DTC_APAGADO}:{c}", "")
                    changed = True
        if changed:
            self.on_dtc(self.dtc_table())

    def dtc_table(self) -> dict:
        return {c: {"modos": sorted(e.modes), "primeira_vez": e.first_seen, "descricao": e.description}
                for c, e in self.dtcs.items() if e.modes}

    def active_codes(self) -> list[str]:
        return sorted(c for c, e in self.dtcs.items() if e.modes)

    # -- pausa para o autoteste / benchmark -----------------------------------
    async def pause(self, timeout: float = 10.0, status: str = "Autoteste em andamento") -> bool:
        if self.status != ST_CONNECTED:
            return False
        self._pause_req = True
        try:
            await asyncio.wait_for(self._paused.wait(), timeout)
        except asyncio.TimeoutError:
            self._pause_req = False
            return False
        self._set_status(ST_SELFTEST, status)
        return True

    def resume(self) -> None:
        self._pause_req = False
        self.last_valid = time.monotonic()
        self._resume.set()
        if self.status == ST_SELFTEST:
            self._set_status(ST_CONNECTED, "Recebendo dados")

    def diagnostics(self) -> dict:
        now = time.monotonic()
        return {
            "elm": self.elm_info,
            "transporte": dict(self.transport_info),
            "suportados": sorted(self.supported) if self.supported is not None else None,
            "stats": {pid: st for pid, st in self.stats.items()},
            "hz_pid": {pid: st.hz(now) for pid, st in self.stats.items()},
            "idade_pid": {pid: st.age(now) for pid, st in self.stats.items()},
            "rapido": list(self.fast), "medio": list(self.medium), "lento": list(self.slow),
            "grupos": [list(g) for g in self.groups],
            "rebaixados": list(self.demoted), "hz": self.hz, "hz_medio": self.avg_hz,
            "ciclo_ms": None if self.round_ewma is None else self.round_ewma * 1000,
            "dtc_ms": self.dtc_read_ms, "conexoes": self.connections, "erro": self.last_error,
            "otimizacoes": self.active.describe() if self.status in (ST_CONNECTED, ST_SELFTEST) else "--",
            "origem_otimizacoes": self.active.source,
            "desativadas": dict(self.disabled),
            "req_1": self.req_single, "req_multi": self.req_multi,
        }
