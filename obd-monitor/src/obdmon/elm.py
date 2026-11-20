"""Protocolo ELM327: fila de comandos, parsing e inicialização.

Somente leitura: todo comando passa por ``safety.check_command``.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from . import dtc as dtcmod
from . import safety
from .pids import BY_PID, SUPPORT_PIDS, decode_support_mask, decode_value
from .transport.base import Transport, TransportError

log = logging.getLogger(__name__)

PROMPT = ">"
ELM_DEFAULT_ST = 0x32   # timeout padrão do ELM327 (0x32 x 4 ms ≈ 200 ms)

# Mensagens de erro do ELM327 (comparação em maiúsculas, sem espaços extras)
ERROR_MARKERS = (
    "NO DATA", "UNABLE TO CONNECT", "CAN ERROR", "BUS INIT", "BUS ERROR", "BUS BUSY",
    "STOPPED", "BUFFER FULL", "FB ERROR", "DATA ERROR", "RX ERROR", "ACT ALERT", "LV RESET",
    "ERR",
)
NOISE_LINES = ("SEARCHING...", "SEARCHING", "OK")


class ElmTimeout(Exception):
    """O prompt '>' não chegou a tempo."""

    def __init__(self, cmd: str, partial: str) -> None:
        super().__init__(f"Sem resposta para {cmd!r} (parcial: {partial!r})")
        self.cmd = cmd
        self.partial = partial


class EcuNotResponding(Exception):
    """O adaptador responde, mas o carro não (ignição desligada?)."""


# ----------------------------------------------------------------------------
# Parsing (funções puras, testadas isoladamente)
# ----------------------------------------------------------------------------

_HEX_RE = re.compile(r"^[0-9A-F]+$")
_FRAME_RE = re.compile(r"^([0-9A-F]):([0-9A-F]*)$")
_LEN_RE = re.compile(r"^[0-9A-F]{3}$")


def clean_lines(raw: str, cmd: str = "") -> list[str]:
    """Quebra a resposta em linhas limpas (maiúsculas, sem espaços),
    removendo prompt, eco do comando e linhas de ruído (SEARCHING...)."""
    cmd_n = safety.normalize(cmd) if cmd else ""
    out: list[str] = []
    for line in re.split(r"[\r\n]+", raw.replace(PROMPT, "\r")):
        s = line.strip().upper()
        if not s:
            continue
        compact = s.replace(" ", "")
        if cmd_n and compact == cmd_n:
            continue  # eco (ATE1)
        if compact.startswith("SEARCHING"):
            # "SEARCHING..." pode vir colado à resposta em alguns clones
            rest = compact[len("SEARCHING"):].lstrip(".")
            if not rest:
                continue
            compact = rest
        if compact in NOISE_LINES:
            continue
        if cmd_n and compact.startswith(cmd_n) and len(compact) > len(cmd_n) and not compact.startswith("4"):
            compact = compact[len(cmd_n):]  # eco colado na resposta
        out.append(compact if _HEX_RE.match(compact) or _FRAME_RE.match(compact) else s)
    return out


def find_error(lines: list[str]) -> Optional[str]:
    for s in lines:
        if s == "?":
            return "?"
        up = s.upper()
        for m in ERROR_MARKERS:
            if m.replace(" ", "") in up.replace(" ", "").replace(".", ""):
                if m == "BUS INIT" and "ERROR" not in up:
                    continue  # "BUS INIT: ...OK" não é erro
                return m
    return None


def _hex_bytes(s: str) -> Optional[list[int]]:
    if not s or not _HEX_RE.match(s) or len(s) % 2:
        return None
    return [int(s[i:i + 2], 16) for i in range(0, len(s), 2)]


def _assemble(lines: list[str]) -> list[list[int]]:
    """Converte linhas em "mensagens" (listas de bytes).

    Trata respostas multi-frame do CAN ("00A", "0:...", "1:...") — inclusive
    frames fora de ordem — e respostas de várias ECUs (uma linha cada)."""
    msgs: list[list[int]] = []
    frames: dict[int, str] = {}
    total_len: Optional[int] = None
    for s in lines:
        if _LEN_RE.match(s) and total_len is None and not frames:
            total_len = int(s, 16)
            continue
        m = _FRAME_RE.match(s)
        if m:
            idx = int(m.group(1), 16)
            frames.setdefault(idx, m.group(2))  # duplicata: mantém a primeira
            continue
        b = _hex_bytes(s)
        if b:
            msgs.append(b)
    if frames:
        hexs = "".join(frames[k] for k in sorted(frames))
        b = _hex_bytes(hexs[: len(hexs) - (len(hexs) % 2)]) or []
        if total_len is not None:
            b = b[:total_len]
        msgs.insert(0, b)
    return msgs


def parse_mode01(lines: list[str], pid: int) -> Optional[list[int]]:
    """Procura 41 <PID> e devolve os bytes seguintes (primeira ECU que responder)."""
    for msg in _assemble(lines):
        for i in range(len(msg) - 1):
            if msg[i] == 0x41 and msg[i + 1] == pid:
                data = msg[i + 2:]
                return data if data else None
    return None


def parse_mode01_multi(lines: list[str], pids: list[int]) -> tuple[Optional[dict[int, list[int]]], str]:
    """Decodifica a resposta de uma consulta com vários PIDs (``010C0D05``).

    Formato (CAN, uma ECU): ``41 0C A B 0D A 05 A`` — numa linha (até 7 bytes)
    ou em vários frames ("00A", "0:...", "1:..."). Em K-line pode vir uma linha
    por PID. Os PIDs podem vir em qualquer ordem.

    Devolve ({pid: bytes}, "") ou (None, motivo). Motivos: INCOMPLETA (faltou
    algum PID), TRUNCADA (faltaram bytes), INCONSISTENTE (PID não pedido no
    meio da resposta = desalinhada/trocada) ou o erro do ELM ("?", NO DATA...)."""
    want = list(dict.fromkeys(pids))
    wset = set(want)
    got: dict[int, list[int]] = {}
    saw = False
    for msg in _assemble(lines):
        if len(msg) < 2 or msg[0] != 0x41:
            continue
        saw = True
        i = 1
        while i < len(msg) and len(got) < len(wset):
            pid = msg[i]
            if pid not in wset or pid not in BY_PID:
                return None, f"INCONSISTENTE (PID {pid:02X} não pedido)"
            n = BY_PID[pid].nbytes
            data = msg[i + 1:i + 1 + n]
            if len(data) < n:
                return None, "TRUNCADA"
            got.setdefault(pid, data)
            i += 1 + n
    if not saw:
        return None, find_error(lines) or "INVALIDA"
    missing = [p for p in want if p not in got]
    if missing:
        return None, "INCOMPLETA (faltou " + " ".join(f"{p:02X}" for p in missing) + ")"
    return got, ""


def mode01_cmd(pids: list[int], suffix: int = 0) -> str:
    """Monta ``01<pids>[n]``; ``suffix`` = número de respostas esperadas (0 = sem)."""
    return "01" + "".join(f"{p:02X}" for p in pids) + (str(suffix) if suffix else "")


def parse_mode01_all(lines: list[str], pid: int) -> list[list[int]]:
    """Como parse_mode01, mas devolve a resposta de TODAS as ECUs (máscaras de suporte)."""
    out = []
    for msg in _assemble(lines):
        for i in range(len(msg) - 1):
            if msg[i] == 0x41 and msg[i + 1] == pid and msg[i + 2:]:
                out.append(msg[i + 2:])
                break
    return out


def parse_dtc_response(lines: list[str], mode: int, can: Optional[bool] = True) -> Optional[list[str]]:
    """Decodifica a resposta dos modos 03/07/0A.

    CAN (ISO 15765):  <43> <qtde> <pares...>   (pode vir em vários frames)
    Não-CAN (K-line): <43> <3 pares>  por linha, sem contador.
    Devolve None se a resposta estiver incompleta/inválida (para não gerar
    falsos "DTC novo/apagado")."""
    resp_id = 0x40 + mode
    if find_error(lines) == "NO DATA":
        return []
    codes: list[str] = []
    found = False
    for msg in _assemble(lines):
        if not msg or msg[0] != resp_id:
            continue
        body = msg[1:]
        is_can = can
        if is_can is None:
            is_can = not (len(msg) == 7)
        if is_can:
            if not body:
                continue
            count = body[0]
            pairs = body[1:]
            if len(pairs) < 2 * count:
                return None  # truncada
            new = dtcmod.decode_dtc_bytes(pairs[: 2 * count])
        else:
            new = dtcmod.decode_dtc_bytes(body)
        found = True
        for c in new:
            if c not in codes:
                codes.append(c)
    return codes if found else None


# ----------------------------------------------------------------------------
# Cliente ELM327
# ----------------------------------------------------------------------------

@dataclass
class Timing:
    """Tempo de uma consulta, separado em partes (ms).

    - ``write``: duração da escrita no transporte. Com "escrita com resposta"
      no BLE inclui a ida e volta da confirmação (1+ intervalo de conexão).
    - ``first``: do fim da escrita até o 1º byte da resposta (rádio BLE nos
      dois sentidos + tempo da ECU responder).
    - ``wait``: do 1º byte até o '>' — o ELM327 já recebeu a resposta e está
      esperando o timeout para ver se outra ECU responde. O sufixo "1"
      (respostas esperadas) zera esta parte.
    """
    write: float = 0.0
    first: float = 0.0
    wait: float = 0.0
    t_first: float = 0.0      # time.monotonic() do 1º byte (instante da resposta)
    t_prompt: float = 0.0     # time.monotonic() do '>'

    @property
    def total(self) -> float:
        return self.write + self.first + self.wait


@dataclass
class PidResult:
    pid: int
    ok: bool
    value: Optional[float] = None
    data: Optional[list[int]] = None
    error: Optional[str] = None      # NO DATA, TIMEOUT, ...
    latency: float = 0.0
    t_resp: float = 0.0              # monotonic do instante em que a resposta chegou
    timing: Optional[Timing] = None


@dataclass
class GroupResult:
    """Resultado de uma requisição (1 ou mais PIDs)."""
    pids: list
    results: dict                    # pid -> PidResult
    error: Optional[str] = None      # erro do grupo inteiro (None = todos ok)
    latency: float = 0.0
    timing: Optional[Timing] = None
    cmd: str = ""

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass
class Tuning:
    """Otimizações de velocidade em uso (salvas por adaptador no config.json)."""
    suffix: bool = False             # "010C1": responde sem esperar o timeout
    multi: int = 1                   # PIDs por requisição (1 = um por vez)
    multi_suffix: bool = True        # usa o sufixo também nas multi-PID
    adaptive: Optional[int] = None   # ATAT0/1/2 (None = padrão do ELM)
    st: Optional[int] = None         # ATST hh (None = padrão do ELM, 0x32)
    fix_protocol: bool = True        # ATSP<n> depois da detecção
    protocol: str = ""               # último protocolo detectado (ex.: "6")
    write_with_response: Optional[bool] = None  # None = automático (sem resposta, se existir)
    source: str = "padrao"           # padrao | auto | benchmark | seguro

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> "Tuning":
        t = cls()
        for k, v in (d or {}).items():
            if k in cls.__dataclass_fields__:
                setattr(t, k, v)
        try:
            t.multi = max(1, min(safety.MAX_PIDS_PER_REQUEST, int(t.multi)))
        except (TypeError, ValueError):
            t.multi = 1
        return t

    @classmethod
    def safe(cls) -> "Tuning":
        """Modo seguro: exatamente como o programa fazia antes das otimizações."""
        return cls(suffix=False, multi=1, adaptive=None, st=None, fix_protocol=False,
                   write_with_response=None, source="seguro")

    def describe(self) -> str:
        parts = ["sufixo 1 " + ("ligado" if self.suffix else "desligado"),
                 f"{self.multi} PID(s) por requisição"]
        if self.adaptive is not None:
            parts.append(f"ATAT{self.adaptive}")
        if self.st is not None:
            parts.append(f"ATST{self.st:02X} (~{self.st * 4} ms)")
        if self.fix_protocol and self.protocol:
            parts.append(f"protocolo fixo ATSP{self.protocol}")
        if self.write_with_response is not None:
            parts.append("escrita BLE " + ("com" if self.write_with_response else "sem") + " resposta")
        return ", ".join(parts)


@dataclass
class ElmInfo:
    version: str = ""
    device: str = ""
    protocol: str = ""
    protocol_num: str = ""
    is_can: Optional[bool] = None
    voltage: str = ""
    extra: dict = field(default_factory=dict)


class ELM327:
    def __init__(self, transport: Transport, write_timeout: float = 3.0) -> None:
        self.t = transport
        self._buf = ""
        self._buf_t0 = 0.0
        self._segments: asyncio.Queue[tuple[str, float, float]] = asyncio.Queue()
        self._lock = asyncio.Lock()
        self.write_timeout = write_timeout
        self.info = ElmInfo()
        self.sent_log: list[str] = []  # últimos comandos enviados (diagnóstico/testes)
        self.last_timing = Timing()
        transport.set_data_callback(self._on_data)

    # -- recepção -----------------------------------------------------------
    def _on_data(self, data: bytes) -> None:
        now = time.monotonic()
        if not self._buf:
            self._buf_t0 = now          # 1º byte desta resposta
        self._buf += data.decode("ascii", "ignore")
        while PROMPT in self._buf:
            seg, self._buf = self._buf.split(PROMPT, 1)
            self._segments.put_nowait((seg, self._buf_t0, now))
            self._buf_t0 = now

    def _drain(self) -> None:
        self._buf = ""
        while not self._segments.empty():
            self._segments.get_nowait()

    # -- envio ----------------------------------------------------------------
    async def send(self, cmd: str, timeout: float = 2.0,
                   accept: Optional[Callable[[list[str]], bool]] = None) -> list[str]:
        """Envia um comando permitido e devolve as linhas limpas da resposta.

        ``accept``: se dado, segmentos atrasados de comandos anteriores (que não
        passam no teste) são descartados enquanto houver tempo.
        O tempo de cada parte fica em ``self.last_timing``."""
        norm = safety.check_command(cmd)  # levanta ForbiddenCommandError
        async with self._lock:
            self._drain()
            self.sent_log.append(norm)
            if len(self.sent_log) > 200:
                del self.sent_log[:100]
            t0 = time.monotonic()
            try:
                await asyncio.wait_for(self.t.write((norm + "\r").encode("ascii")), self.write_timeout)
            except asyncio.TimeoutError as e:
                raise TransportError(f"Escrita demorou demais ({norm})") from e
            t1 = time.monotonic()
            deadline = t0 + timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ElmTimeout(norm, self._buf)
                try:
                    seg, tf, tp = await asyncio.wait_for(self._segments.get(), remaining)
                except asyncio.TimeoutError:
                    raise ElmTimeout(norm, self._buf) from None
                lines = clean_lines(seg, norm)
                if accept is None or accept(lines) or find_error(lines):
                    tf = max(tf, t1)
                    self.last_timing = Timing(write=(t1 - t0) * 1000, first=(tf - t1) * 1000,
                                              wait=(tp - tf) * 1000, t_first=tf, t_prompt=tp)
                    return lines
                log.debug("descartado segmento atrasado: %r", seg)

    # -- inicialização --------------------------------------------------------
    async def configure(self, protocol: str = "") -> ElmInfo:
        """ATZ, ATE0, ATL0, ATS0, ATH0, ATSP0 (ou ATSP<protocol>) + versão.
        Não fala com o carro."""
        info = ElmInfo()
        try:
            lines = await self.send("ATZ", timeout=4.0)
            info.version = next((s for s in lines if "ELM" in s.upper()), " ".join(lines))
            settle = 0.3            # o '>' chegou: só um respiro para o reset terminar
        except ElmTimeout as e:
            # Alguns clones não mandam '>' depois do ATZ; segue em frente.
            info.version = " ".join(clean_lines(e.partial))
            settle = 1.0
        await asyncio.sleep(settle)
        self._drain()
        sp = f"ATSP{protocol}" if protocol and safety.is_command_allowed(f"ATSP{protocol}") else "ATSP0"
        for c in ("ATE0", "ATL0", "ATS0", "ATH0", sp):
            try:
                await self.send(c, timeout=2.0)
            except ElmTimeout:
                await self.send(c, timeout=3.0)  # uma segunda chance
        try:
            info.device = " ".join(await self.send("AT@1", timeout=1.5))
        except ElmTimeout:
            pass
        if "ELM" not in info.version.upper():
            try:
                info.version = " ".join(await self.send("ATI", timeout=1.5))
            except ElmTimeout:
                pass
        self.info = info
        return info

    async def read_protocol_info(self) -> ElmInfo:
        info = self.info
        try:
            info.protocol = " ".join(await self.send("ATDP", timeout=1.5))
            raw = " ".join(await self.send("ATDPN", timeout=1.5)).strip().upper()
            info.extra["protocolo_auto"] = raw.startswith("A")
            info.protocol_num = raw.lstrip("A")
            info.is_can = info.protocol_num in {"6", "7", "8", "9"} or "15765" in info.protocol
        except ElmTimeout:
            pass
        try:
            info.voltage = " ".join(await self.send("ATRV", timeout=1.5))
        except ElmTimeout:
            pass
        return info

    async def set_protocol(self, num: str) -> bool:
        """ATSP<n>: para a busca automática (protocolo já detectado)."""
        cmd = f"ATSP{num}"
        if not num or not safety.is_command_allowed(cmd):
            return False
        try:
            lines = await self.send(cmd, timeout=1.5)
        except ElmTimeout:
            return False
        return find_error(lines) is None

    async def apply_timing(self, adaptive: Optional[int], st: Optional[int]) -> bool:
        """ATAT<n> e ATST<hh>. None = volta ao padrão do ELM (ATAT1, ATST32)."""
        ok = True
        for cmd in (f"ATAT{1 if adaptive is None else adaptive}",
                    f"ATST{(ELM_DEFAULT_ST if st is None else st):02X}"):
            try:
                lines = await self.send(cmd, timeout=1.5)
                ok = ok and find_error(lines) is None
            except ElmTimeout:
                ok = False
        return ok

    async def initialize(self, search_timeout: float = 12.0) -> ElmInfo:
        """Sequência completa: configure + 0100 (busca de protocolo) + info."""
        await self.configure()
        await self.probe_ecu(search_timeout)
        return await self.read_protocol_info()

    async def probe_ecu(self, timeout: float = 12.0) -> list[int]:
        """Envia 0100 (faz a busca de protocolo). Levanta EcuNotResponding."""
        lines = await self.send("0100", timeout=timeout, accept=lambda ls: parse_mode01(ls, 0) is not None)
        data = parse_mode01(lines, 0x00)
        if data is None:
            err = find_error(lines) or "resposta inválida"
            raise EcuNotResponding(f"O carro não respondeu ao 0100: {err} ({' '.join(lines)})")
        return data

    async def supported_pids(self) -> Optional[set[int]]:
        """Consulta 0100/0120/0140/0160 enquanto o bit de "próxima faixa" estiver ligado."""
        supported: set[int] = set()
        any_ok = False
        for base in SUPPORT_PIDS:
            if base and base not in supported:
                break
            try:
                lines = await self.send(f"01{base:02X}", timeout=3.0,
                                        accept=lambda ls, b=base: bool(parse_mode01_all(ls, b)))
            except ElmTimeout:
                break
            answers = parse_mode01_all(lines, base)
            if not answers:
                break
            any_ok = True
            for data in answers:  # várias ECUs: une as máscaras
                supported |= decode_support_mask(base, data)
        return supported if any_ok else None

    # -- consultas ----------------------------------------------------------
    async def query_pid(self, pid: int, timeout: float = 1.5, decode: bool = True,
                        suffix: bool = False) -> PidResult:
        """Um PID por vez. ``suffix``: envia "010C1" (1 resposta esperada)."""
        cmd = mode01_cmd([pid], 1 if suffix else 0)
        t0 = time.monotonic()
        try:
            lines = await self.send(cmd, timeout=timeout, accept=lambda ls: parse_mode01(ls, pid) is not None)
        except ElmTimeout:
            return PidResult(pid, False, error="TIMEOUT", latency=time.monotonic() - t0)
        tm = self.last_timing
        lat = time.monotonic() - t0
        data = parse_mode01(lines, pid)
        if data is None:
            return PidResult(pid, False, error=find_error(lines) or "INVALIDA", latency=lat, timing=tm)
        if not decode:
            return PidResult(pid, True, data=data, latency=lat, t_resp=tm.t_first, timing=tm)
        val = decode_value(pid, data)
        if val is None:
            return PidResult(pid, False, data=data, error="TRUNCADA", latency=lat, timing=tm)
        return PidResult(pid, True, value=val, data=data, latency=lat, t_resp=tm.t_first, timing=tm)

    async def query_group(self, pids: list[int], suffix: bool = False, timeout: float = 1.5) -> GroupResult:
        """Vários PIDs (até 6) numa só requisição: "010C0D05" (+ "1" com sufixo).

        Se a resposta vier incompleta, truncada ou inconsistente, NENHUM valor
        do grupo é aproveitado (``error`` diz o motivo)."""
        pids = list(dict.fromkeys(pids))
        if len(pids) == 1:
            r = await self.query_pid(pids[0], timeout=timeout, suffix=suffix)
            return GroupResult(pids, {r.pid: r}, None if r.ok else r.error, r.latency, r.timing,
                               mode01_cmd(pids, 1 if suffix else 0))
        cmd = mode01_cmd(pids, 1 if suffix else 0)
        t0 = time.monotonic()
        wset = set(pids)

        def accept(ls: list[str]) -> bool:
            return any(len(m) > 1 and m[0] == 0x41 and m[1] in wset for m in _assemble(ls))
        try:
            lines = await self.send(cmd, timeout=timeout, accept=accept)
        except ElmTimeout:
            return GroupResult(pids, {}, "TIMEOUT", time.monotonic() - t0, None, cmd)
        tm = self.last_timing
        lat = time.monotonic() - t0
        datas, why = parse_mode01_multi(lines, pids)
        if datas is None:
            return GroupResult(pids, {}, why, lat, tm, cmd)
        res: dict[int, PidResult] = {}
        for p in pids:
            val = decode_value(p, datas[p])
            if val is None:
                return GroupResult(pids, {}, "TRUNCADA", lat, tm, cmd)
            res[p] = PidResult(p, True, value=val, data=datas[p], latency=lat, t_resp=tm.t_first, timing=tm)
        return GroupResult(pids, res, None, lat, tm, cmd)

    async def read_dtcs(self, mode: int, timeout: float = 3.0) -> Optional[list[str]]:
        """Lê DTCs (modo 3, 7 ou 0x0A). None = leitura falhou."""
        cmd = f"{mode:02X}"
        can = self.info.is_can
        try:
            lines = await self.send(cmd, timeout=timeout,
                                    accept=lambda ls: parse_dtc_response(ls, mode, can) is not None)
        except ElmTimeout:
            return None
        return parse_dtc_response(lines, mode, can=can)
