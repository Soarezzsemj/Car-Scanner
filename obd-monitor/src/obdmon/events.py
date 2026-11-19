"""Eventos, alertas e buffer circular."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, Optional

from . import dtc as dtcmod
from .config import Config
from .pids import BY_PID

# Tipos de evento (coluna "evento" do CSV)
LUZ_OLEO_MANUAL = "LUZ_OLEO_MANUAL"
DTC_NOVO = "DTC_NOVO"
DTC_APAGADO = "DTC_APAGADO"
ALERTA_ARREFECIMENTO_ALTO = "ALERTA_ARREFECIMENTO_ALTO"
ALERTA_ARREFECIMENTO_CRITICO = "ALERTA_ARREFECIMENTO_CRITICO"
ALERTA_OLEO_QUENTE = "ALERTA_OLEO_QUENTE"
ALERTA_TENSAO_BAIXA = "ALERTA_TENSAO_BAIXA"
CONEXAO_PERDIDA = "CONEXAO_PERDIDA"
CONEXAO_RESTABELECIDA = "CONEXAO_RESTABELECIDA"
PID_NAO_SUPORTADO = "PID_NAO_SUPORTADO"
INICIO_VIAGEM = "INICIO_VIAGEM"
FIM_VIAGEM = "FIM_VIAGEM"


@dataclass
class Event:
    code: str                      # ex.: "DTC_NOVO:P0524"
    t_wall: datetime
    t_s: float
    detail: str = ""
    critical: bool = False
    values: dict = field(default_factory=dict)

    @property
    def kind(self) -> str:
        return self.code.split(":", 1)[0]

    @property
    def arg(self) -> str:
        return self.code.split(":", 1)[1] if ":" in self.code else ""

    @property
    def snapshot(self) -> bool:
        """Dispara o instantâneo de 60 s antes + 30 s depois?"""
        return self.kind in (LUZ_OLEO_MANUAL, DTC_NOVO) or (self.kind.startswith("ALERTA_") and self.critical)

    @property
    def level(self) -> str:
        if self.critical or self.kind in (LUZ_OLEO_MANUAL, CONEXAO_PERDIDA):
            return "critico"
        if self.kind.startswith("ALERTA_") or self.kind == DTC_NOVO:
            return "aviso"
        return "info"

    @property
    def label(self) -> str:
        k, a = self.kind, self.arg
        if k == LUZ_OLEO_MANUAL:
            return "LUZ DO ÓLEO ACENDEU"
        if k == DTC_NOVO:
            return f"DTC NOVO {a} – {dtcmod.describe(a)}"
        if k == DTC_APAGADO:
            return f"DTC apagado {a}"
        if k == ALERTA_ARREFECIMENTO_ALTO:
            return "ARREFECIMENTO ALTO"
        if k == ALERTA_ARREFECIMENTO_CRITICO:
            return "ARREFECIMENTO CRÍTICO"
        if k == ALERTA_OLEO_QUENTE:
            return "ÓLEO QUENTE" + (" (CRÍTICO)" if self.critical else "")
        if k == ALERTA_TENSAO_BAIXA:
            return "TENSÃO BAIXA"
        if k == CONEXAO_PERDIDA:
            return "CONEXÃO PERDIDA"
        if k == CONEXAO_RESTABELECIDA:
            return "Conexão restabelecida"
        if k == PID_NAO_SUPORTADO:
            try:
                name = BY_PID[int(a, 16)].name
            except (KeyError, ValueError):
                name = "?"
            return f"PID {a} ({name}) não suportado pelo carro"
        if k == INICIO_VIAGEM:
            return "Início da viagem"
        if k == FIM_VIAGEM:
            return "Fim da viagem"
        return self.code


def _fmt_num(v: Optional[float], decimals: int = 0) -> str:
    if v is None:
        return "--"
    s = f"{v:.{decimals}f}"
    return s.replace(".", ",")


def values_summary(v: dict) -> str:
    parts = [
        f"arrefecimento {_fmt_num(v.get('arrefecimento_c'))} °C",
    ]
    if v.get("oleo_c") is not None:
        parts.append(f"óleo {_fmt_num(v.get('oleo_c'))} °C")
    parts += [
        f"RPM {_fmt_num(v.get('rpm'))}",
        f"{_fmt_num(v.get('velocidade_kmh'))} km/h",
        f"carga {_fmt_num(v.get('carga_pct'))}%",
        f"acelerador {_fmt_num(v.get('acelerador_pct'))}%",
    ]
    if v.get("tensao_v") is not None:
        parts.append(f"tensão {_fmt_num(v.get('tensao_v'), 1)} V")
    return " | ".join(parts)


def format_event_line(ev: Event) -> str:
    """Linha do arquivo eventos_<viagem>.log."""
    s = f"{ev.t_wall:%H:%M:%S}  {ev.label}"
    if ev.detail:
        s += f" ({ev.detail})"
    return s + " | " + values_summary(ev.values)


class AlertDetector:
    """Detecta alertas por limite, com confirmação (n leituras) e histerese."""

    HYST_TEMP = 2.0
    HYST_VOLT = 0.3

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.active: dict[str, str] = {}   # "arrefecimento"/"oleo"/"tensao" -> "aviso"/"critico"
        self._pending: dict[str, tuple[str, int]] = {}

    def _level(self, name: str, value: Optional[float], warn: float, crit: float) -> Optional[str]:
        cur = self.active.get(name)
        if value is None:
            return cur  # sem dado: mantém o estado
        h = self.HYST_TEMP
        if value >= crit or (cur == "critico" and value >= crit - h):
            return "critico"
        if value >= warn or (cur is not None and value >= warn - h):
            return "aviso"
        return None

    def _confirm(self, name: str, new: Optional[str], need: int) -> Optional[str]:
        """Só muda de nível depois de ``need`` leituras seguidas (evita ruído).
        Subir de nível exige confirmação; baixar é imediato."""
        cur = self.active.get(name)
        if new == cur:
            self._pending.pop(name, None)
            return cur
        rank = {None: 0, "aviso": 1, "critico": 2}
        if rank[new] < rank[cur]:
            self._pending.pop(name, None)
            return new
        p = self._pending.get(name)
        n = p[1] + 1 if p and p[0] == new else 1
        self._pending[name] = (new, n)
        if n >= need:
            self._pending.pop(name, None)
            return new
        return cur

    def check(self, v: dict) -> list[tuple[str, str, bool]]:
        """Devolve [(código, detalhe, crítico)] dos alertas que COMEÇARAM agora."""
        out: list[tuple[str, str, bool]] = []
        c = self.cfg

        cool = v.get("arrefecimento_c")
        new = self._confirm("arrefecimento", self._level("arrefecimento", cool, c.coolant_warn, c.coolant_crit), 2)
        old = self.active.get("arrefecimento")
        if new != old:
            if new == "critico":
                out.append((ALERTA_ARREFECIMENTO_CRITICO, f"≥ {c.coolant_crit:g} °C", True))
            elif new == "aviso" and old is None:
                out.append((ALERTA_ARREFECIMENTO_ALTO, f"≥ {c.coolant_warn:g} °C", False))
            self._set("arrefecimento", new)

        oil = v.get("oleo_c")
        new = self._confirm("oleo", self._level("oleo", oil, c.oil_warn, c.oil_crit), 2)
        old = self.active.get("oleo")
        if new != old:
            if new == "critico":
                out.append((ALERTA_OLEO_QUENTE, f"crítico ≥ {c.oil_crit:g} °C", True))
            elif new == "aviso" and old is None:
                out.append((ALERTA_OLEO_QUENTE, f"aviso ≥ {c.oil_warn:g} °C", False))
            self._set("oleo", new)

        volt, rpm = v.get("tensao_v"), v.get("rpm")
        cur = self.active.get("tensao")
        if volt is None or rpm is None:
            lvl = cur
        elif rpm > 400 and volt < c.voltage_low:
            lvl = "aviso"
        elif cur and volt < c.voltage_low + self.HYST_VOLT and rpm > 400:
            lvl = cur
        else:
            lvl = None
        new = self._confirm("tensao", lvl, 8)  # 4 s abaixo do limite
        if new != cur:
            if new and not cur:
                out.append((ALERTA_TENSAO_BAIXA, f"< {c.voltage_low:g} V com motor ligado", False))
            self._set("tensao", new)
        return out

    def _set(self, name: str, lvl: Optional[str]) -> None:
        if lvl is None:
            self.active.pop(name, None)
        else:
            self.active[name] = lvl


class RingBuffer:
    """Guarda os itens dos últimos ``seconds`` segundos (pela chave t_s)."""

    def __init__(self, seconds: float) -> None:
        self.seconds = seconds
        self._d: deque = deque()

    def append(self, t: float, item) -> None:
        self._d.append((t, item))
        while self._d and self._d[0][0] < t - self.seconds:
            self._d.popleft()

    def items(self) -> list:
        return [it for _, it in self._d]

    def __len__(self) -> int:
        return len(self._d)


def join_codes(codes: Iterable[str]) -> str:
    return "|".join(codes)
