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
from .pids import SUPPORT_PIDS, decode_support_mask, decode_value
from .transport.base import Transport, TransportError

log = logging.getLogger(__name__)

PROMPT = ">"

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
class PidResult:
    pid: int
    ok: bool
    value: Optional[float] = None
    data: Optional[list[int]] = None
    error: Optional[str] = None      # NO DATA, TIMEOUT, ...
    latency: float = 0.0


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
        self._segments: asyncio.Queue[str] = asyncio.Queue()
        self._lock = asyncio.Lock()
        self.write_timeout = write_timeout
        self.info = ElmInfo()
        self.sent_log: list[str] = []  # últimos comandos enviados (diagnóstico/testes)
        transport.set_data_callback(self._on_data)

    # -- recepção -----------------------------------------------------------
    def _on_data(self, data: bytes) -> None:
        self._buf += data.decode("ascii", "ignore")
        while PROMPT in self._buf:
            seg, self._buf = self._buf.split(PROMPT, 1)
            self._segments.put_nowait(seg)

    def _drain(self) -> None:
        self._buf = ""
        while not self._segments.empty():
            self._segments.get_nowait()

    # -- envio ----------------------------------------------------------------
    async def send(self, cmd: str, timeout: float = 2.0,
                   accept: Optional[Callable[[list[str]], bool]] = None) -> list[str]:
        """Envia um comando permitido e devolve as linhas limpas da resposta.

        ``accept``: se dado, segmentos atrasados de comandos anteriores (que não
        passam no teste) são descartados enquanto houver tempo."""
        norm = safety.check_command(cmd)  # levanta ForbiddenCommandError
        async with self._lock:
            self._drain()
            self.sent_log.append(norm)
            if len(self.sent_log) > 200:
                del self.sent_log[:100]
            try:
                await asyncio.wait_for(self.t.write((norm + "\r").encode("ascii")), self.write_timeout)
            except asyncio.TimeoutError as e:
                raise TransportError(f"Escrita demorou demais ({norm})") from e
            deadline = time.monotonic() + timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ElmTimeout(norm, self._buf)
                try:
                    seg = await asyncio.wait_for(self._segments.get(), remaining)
                except asyncio.TimeoutError:
                    raise ElmTimeout(norm, self._buf) from None
                lines = clean_lines(seg, norm)
                if accept is None or accept(lines) or find_error(lines):
                    return lines
                log.debug("descartado segmento atrasado: %r", seg)

    # -- inicialização --------------------------------------------------------
    async def configure(self) -> ElmInfo:
        """ATZ, ATE0, ATL0, ATS0, ATH0, ATSP0 (+ versão). Não fala com o carro."""
        info = ElmInfo()
        try:
            lines = await self.send("ATZ", timeout=4.0)
            info.version = next((s for s in lines if "ELM" in s.upper()), " ".join(lines))
        except ElmTimeout as e:
            # Alguns clones não mandam '>' depois do ATZ; segue em frente.
            info.version = " ".join(clean_lines(e.partial))
        await asyncio.sleep(1.0)
        self._drain()
        for c in ("ATE0", "ATL0", "ATS0", "ATH0", "ATSP0"):
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
            info.protocol_num = " ".join(await self.send("ATDPN", timeout=1.5)).lstrip("A")
            info.is_can = info.protocol_num in {"6", "7", "8", "9"} or "15765" in info.protocol
        except ElmTimeout:
            pass
        try:
            info.voltage = " ".join(await self.send("ATRV", timeout=1.5))
        except ElmTimeout:
            pass
        return info

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
    async def query_pid(self, pid: int, timeout: float = 1.5, decode: bool = True) -> PidResult:
        cmd = f"01{pid:02X}"
        t0 = time.monotonic()
        try:
            lines = await self.send(cmd, timeout=timeout, accept=lambda ls: parse_mode01(ls, pid) is not None)
        except ElmTimeout:
            return PidResult(pid, False, error="TIMEOUT", latency=time.monotonic() - t0)
        lat = time.monotonic() - t0
        data = parse_mode01(lines, pid)
        if data is None:
            return PidResult(pid, False, error=find_error(lines) or "INVALIDA", latency=lat)
        if not decode:
            return PidResult(pid, True, data=data, latency=lat)
        val = decode_value(pid, data)
        if val is None:
            return PidResult(pid, False, data=data, error="TRUNCADA", latency=lat)
        return PidResult(pid, True, value=val, data=data, latency=lat)

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
