"""Tabela de PIDs do modo 01, fórmulas e máscaras de suporte."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

FAST, MEDIUM, SLOW = "rapido", "medio", "lento"


@dataclass(frozen=True)
class PidDef:
    pid: int
    key: str            # nome da coluna no CSV
    name: str           # nome em português
    unit: str
    nbytes: int
    decode: Callable[[list[int]], float]
    cycle: str
    priority: int       # menor = mais importante (ordem de prioridade no ciclo rápido)
    decimals: int = 0

    @property
    def hex(self) -> str:
        return f"{self.pid:02X}"


def _a(b): return b[0]
def _temp(b): return b[0] - 40
def _pct(b): return b[0] * 100.0 / 255.0
def _rpm(b): return (256 * b[0] + b[1]) / 4.0
def _volt(b): return (256 * b[0] + b[1]) / 1000.0


PIDS: tuple[PidDef, ...] = (
    PidDef(0x0C, "rpm", "RPM", "rpm", 2, _rpm, FAST, 0),
    PidDef(0x0D, "velocidade_kmh", "Velocidade", "km/h", 1, _a, FAST, 1),
    PidDef(0x05, "arrefecimento_c", "Temp. arrefecimento", "°C", 1, _temp, FAST, 2),
    PidDef(0x04, "carga_pct", "Carga do motor", "%", 1, _pct, FAST, 3, 1),
    PidDef(0x11, "acelerador_pct", "Acelerador", "%", 1, _pct, FAST, 4, 1),
    PidDef(0x5C, "oleo_c", "Temp. do óleo", "°C", 1, _temp, FAST, 5),
    PidDef(0x0F, "admissao_c", "Temp. ar de admissão", "°C", 1, _temp, MEDIUM, 6),
    PidDef(0x42, "tensao_v", "Tensão do módulo", "V", 2, _volt, MEDIUM, 7, 2),
    PidDef(0x46, "ambiente_c", "Temp. ambiente", "°C", 1, _temp, SLOW, 8),
)

BY_PID: dict[int, PidDef] = {p.pid: p for p in PIDS}
BY_KEY: dict[str, PidDef] = {p.key: p for p in PIDS}
VALUE_KEYS: tuple[str, ...] = (
    "velocidade_kmh", "rpm", "arrefecimento_c", "oleo_c", "carga_pct",
    "acelerador_pct", "admissao_c", "tensao_v", "ambiente_c",
)

# Os 5 parâmetros que ficam SEMPRE no ciclo rápido (ordem de prioridade).
FAST_CORE: tuple[int, ...] = (0x0C, 0x0D, 0x05, 0x04, 0x11)
FAST_CORE_KEYS: tuple[str, ...] = ("rpm", "velocidade_kmh", "arrefecimento_c", "carga_pct", "acelerador_pct")

# Faixa física plausível de cada valor decodificado. Fora dela = dado errado
# (resposta trocada/desalinhada): a otimização que gerou o valor é desligada.
PHYS_RANGE: dict[int, tuple[float, float]] = {
    0x0C: (0.0, 9000.0),     # RPM (o C3 corta bem antes)
    0x0D: (0.0, 250.0),      # km/h
    0x05: (-40.0, 160.0),    # °C
    0x04: (0.0, 100.0),
    0x11: (0.0, 100.0),
    0x5C: (-40.0, 180.0),
    0x0F: (-40.0, 120.0),
    0x42: (0.0, 20.0),
    0x46: (-40.0, 70.0),
}


def plausible(pid: int, value: float) -> bool:
    lo, hi = PHYS_RANGE.get(pid, (float("-inf"), float("inf")))
    return lo <= value <= hi


# PIDs de máscara de suporte: 00 -> 01..20, 20 -> 21..40, 40 -> 41..60, 60 -> 61..80
SUPPORT_PIDS = (0x00, 0x20, 0x40, 0x60)


def decode_support_mask(base: int, data: list[int]) -> set[int]:
    """Decodifica os 4 bytes de uma resposta 0100/0120/0140/0160.

    Bit mais significativo do primeiro byte = PID base+1.
    """
    out: set[int] = set()
    if len(data) < 4:
        return out
    bits = (data[0] << 24) | (data[1] << 16) | (data[2] << 8) | data[3]
    for i in range(32):
        if bits & (1 << (31 - i)):
            out.add(base + i + 1)
    return out


def decode_value(pid: int, data: list[int]) -> Optional[float]:
    d = BY_PID.get(pid)
    if d is None or len(data) < d.nbytes:
        return None
    return d.decode(data[: d.nbytes])
