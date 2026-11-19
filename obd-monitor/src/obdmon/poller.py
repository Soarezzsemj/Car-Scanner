"""Leitura contínua: conexão, inicialização, ciclos rápido/médio/lento, DTCs,
watchdog e reconexão automática com backoff.

Decisão de prioridade (documentada no README):
- O ciclo rápido deve caber no intervalo do log (0,5 s). A cada 10 s o tempo
  médio de resposta de cada PID é medido; os PIDs rápidos entram na ordem de
  prioridade RPM → velocidade → arrefecimento → carga → acelerador → óleo
  enquanto a soma dos tempos couber em 85% do orçamento (o resto fica para
  DTC/médios). RPM, velocidade e arrefecimento ficam SEMPRE no ciclo rápido.
  Os que não couberem são "rebaixados" para um ciclo de 1 s.
- Por rodada do ciclo rápido roda no máximo 1 tarefa extra (PID médio/lento ou
  leitura de DTC), ou 2 se houver fila; assim uma leitura de DTC nunca atrasa
  o ciclo rápido em mais que ~1 consulta.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Optional

from . import dtc as dtcmod
from . import events as ev
from .elm import ELM327, EcuNotResponding, ElmTimeout, ElmInfo
from .pids import BY_PID, FAST, MEDIUM, PIDS, SLOW
from .safety import ForbiddenCommandError
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

    def record(self, lat: float, ok: bool) -> None:
        if ok:
            self.ok += 1
            self.lat_sum += lat
            self.lat_ewma = lat if self.lat_ewma is None else 0.8 * self.lat_ewma + 0.2 * lat
            self.lat_min = lat if self.lat_min is None else min(self.lat_min, lat)
            self.lat_max = lat if self.lat_max is None else max(self.lat_max, lat)
        else:
            self.fails += 1

    @property
    def lat_avg(self) -> Optional[float]:
        return self.lat_sum / self.ok if self.ok else None

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
                 search_timeout: float = 12.0,
                 backoff: tuple[float, ...] = BACKOFF,
                 lost_after: float = WATCHDOG_LOST_S) -> None:
        self.factory = transport_factory
        self.budget = budget
        self.on_event = on_event
        self.on_status = on_status
        self.on_dtc = on_dtc
        self.on_values = on_values
        self.search_timeout = search_timeout
        self.backoff = backoff
        self.lost_after = lost_after

        self.status = ST_STOPPED
        self.status_detail = ""
        self.values: dict[str, tuple[float, float]] = {}   # key -> (valor, monotonic)
        self.last_valid = 0.0
        self.ever_connected = False
        self.lost = False
        self.lost_since: Optional[float] = None
        self.reconnect_attempt = 0
        self.connections = 0
        self.stats: dict[int, PidStats] = {p.pid: PidStats(p.pid, cycle=p.cycle) for p in PIDS}
        self.supported: Optional[set[int]] = None
        self.fast: list[int] = []
        self.medium: list[int] = []
        self.slow: list[int] = []
        self.demoted: list[int] = []
        self.round_ewma: Optional[float] = None
        self.default_lat = DEFAULT_LAT
        self.hz = 0.0
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

    def current(self, key: str, max_age: float) -> Optional[float]:
        v = self.values.get(key)
        if v is None or time.monotonic() - v[1] > max_age:
            return None
        return v[0]

    async def _sleep_or_stop(self, s: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), s)
        except asyncio.TimeoutError:
            pass

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
                self.transport_info = transport.describe()
                self.elm = ELM327(transport)
                self._set_status(ST_INIT, "Inicializando o ELM327 (ATZ...)")
                await self._init_elm()
                if self.supported is None:
                    await self._discover()
                self._compute_cycles()
                self.connections += 1
                self.last_valid = time.monotonic()
                self.ever_connected = True
                self.reconnect_attempt = 0
                if self.lost:
                    self.lost = False
                    self.lost_since = None
                    self.on_event(ev.CONEXAO_RESTABELECIDA, "")
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

    async def _init_elm(self) -> None:
        elm = self.elm
        assert elm is not None
        await elm.configure()
        while True:
            try:
                await elm.probe_ecu(self.search_timeout)
                break
            except EcuNotResponding as e:
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

    def _compute_cycles(self) -> None:
        usable = [p for p in PIDS if self.stats[p.pid].usable]
        fast_cand = sorted((p for p in usable if p.cycle == FAST), key=lambda p: p.priority)
        limit = self.budget * 0.85
        fast, demoted, total = [], [], 0.0
        for i, p in enumerate(fast_cand):
            lat = self.stats[p.pid].lat_ewma or self.default_lat
            if i < 3 or total + lat <= limit:
                fast.append(p.pid)
                total += lat
            else:
                demoted.append(p.pid)
        self.fast = fast
        self.demoted = demoted
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
        due.setdefault("dtc:03", now + 1.5)
        due.setdefault("dtc:07", now + 3.0)
        due.setdefault("dtc:0A", now + 15.0)
        last_rebalance = now - 7.0  # primeiro rebalanceamento após ~3 s
        while not self.stopped:
            if self._pause_req:
                self._paused.set()
                await self._resume.wait()
                self._resume.clear()
                self._paused.clear()
                self.last_valid = time.monotonic()
            t0 = time.monotonic()
            for pid in list(self.fast):
                await self._query(pid)
                self._check_alive()
            if not self.fast:
                await asyncio.sleep(0.2)
            # tarefas extras vencidas
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
            rt = time.monotonic() - t0
            self.round_ewma = rt if self.round_ewma is None else 0.85 * self.round_ewma + 0.15 * rt
            self._round_times.append(time.monotonic())
            while len(self._round_times) > 2 and self._round_times[0] < time.monotonic() - 5.0:
                self._round_times.pop(0)
            if len(self._round_times) > 1:
                span = self._round_times[-1] - self._round_times[0]
                self.hz = (len(self._round_times) - 1) / span if span > 0 else 0.0
            self.on_values()
            if time.monotonic() - last_rebalance > 10.0:
                last_rebalance = time.monotonic()
                self._compute_cycles()
            if rt < 0.05:
                await asyncio.sleep(0.05 - rt)

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

    async def _query(self, pid: int, count_nodata: bool = True) -> bool:
        assert self.elm is not None
        r = await self.elm.query_pid(pid)
        st = self.stats[pid]
        st.record(r.latency, r.ok)
        if r.ok and r.value is not None:
            self.values[BY_PID[pid].key] = (r.value, time.monotonic())
            self.last_valid = time.monotonic()
            st.nodata_streak = 0
            return True
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

    # -- pausa para o autoteste -----------------------------------------------
    async def pause(self, timeout: float = 10.0) -> bool:
        if self.status != ST_CONNECTED:
            return False
        self._pause_req = True
        try:
            await asyncio.wait_for(self._paused.wait(), timeout)
        except asyncio.TimeoutError:
            self._pause_req = False
            return False
        self._set_status(ST_SELFTEST, "Autoteste em andamento")
        return True

    def resume(self) -> None:
        self._pause_req = False
        self.last_valid = time.monotonic()
        self._resume.set()
        if self.status == ST_SELFTEST:
            self._set_status(ST_CONNECTED, "Recebendo dados")

    def diagnostics(self) -> dict:
        return {
            "elm": self.elm_info,
            "transporte": dict(self.transport_info),
            "suportados": sorted(self.supported) if self.supported is not None else None,
            "stats": {pid: st for pid, st in self.stats.items()},
            "rapido": list(self.fast), "medio": list(self.medium), "lento": list(self.slow),
            "rebaixados": list(self.demoted), "hz": self.hz,
            "ciclo_ms": None if self.round_ewma is None else self.round_ewma * 1000,
            "dtc_ms": self.dtc_read_ms, "conexoes": self.connections, "erro": self.last_error,
        }
