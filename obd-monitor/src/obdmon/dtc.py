"""Decodificação de códigos de falha (DTC) e descrições em português."""
from __future__ import annotations

from typing import Iterable

_TYPE = "PCBU"

DESCRIPTIONS: dict[str, str] = {
    # --- Óleo ---
    "P0520": "Sensor/interruptor de pressão do óleo – falha no circuito",
    "P0521": "Sensor/interruptor de pressão do óleo – faixa/desempenho",
    "P0522": "Sensor/interruptor de pressão do óleo – sinal baixo (pressão baixa)",
    "P0523": "Sensor/interruptor de pressão do óleo – sinal alto",
    "P0524": "Pressão do óleo do motor muito baixa",
    "P0195": "Sensor de temperatura do óleo – falha no circuito",
    "P0196": "Sensor de temperatura do óleo – faixa/desempenho",
    "P0197": "Sensor de temperatura do óleo – sinal baixo",
    "P0198": "Sensor de temperatura do óleo – sinal alto",
    # --- Arrefecimento ---
    "P0115": "Sensor de temperatura do arrefecimento – falha no circuito",
    "P0116": "Sensor de temperatura do arrefecimento – faixa/desempenho",
    "P0117": "Sensor de temperatura do arrefecimento – sinal baixo",
    "P0118": "Sensor de temperatura do arrefecimento – sinal alto",
    "P0119": "Sensor de temperatura do arrefecimento – sinal intermitente",
    "P0125": "Temperatura do arrefecimento insuficiente para controle em malha fechada",
    "P0126": "Temperatura do arrefecimento insuficiente para operação estável",
    "P0128": "Termostato – temperatura abaixo da regulagem (termostato aberto/travado)",
    "P0217": "Superaquecimento do motor (temperatura excessiva)",
    "P0480": "Ventoinha de arrefecimento 1 – falha no circuito de comando",
    "P0481": "Ventoinha de arrefecimento 2 – falha no circuito de comando",
    "P0482": "Ventoinha de arrefecimento 3 – falha no circuito de comando",
    "P0483": "Ventoinha de arrefecimento – falha na verificação de racionalidade",
    # --- Ignição ---
    "P0300": "Falha de ignição aleatória/múltiplos cilindros",
    "P0301": "Falha de ignição no cilindro 1",
    "P0302": "Falha de ignição no cilindro 2",
    "P0303": "Falha de ignição no cilindro 3",
    "P0304": "Falha de ignição no cilindro 4",
    # --- Mistura ---
    "P0171": "Mistura muito pobre (banco 1)",
    "P0172": "Mistura muito rica (banco 1)",
    # --- Tensão ---
    "P0560": "Tensão do sistema – falha",
    "P0562": "Tensão do sistema baixa",
    "P0563": "Tensão do sistema alta",
    # --- Outros comuns ---
    "P0130": "Sonda lambda (B1S1) – falha no circuito",
    "P0133": "Sonda lambda (B1S1) – resposta lenta",
    "P0420": "Eficiência do catalisador abaixo do limite (banco 1)",
    "P0500": "Sensor de velocidade do veículo – falha",
    "P0505": "Sistema de controle da marcha lenta – falha",
    "P0335": "Sensor de rotação (virabrequim) – falha no circuito",
    "P0340": "Sensor de fase (comando) – falha no circuito",
    "U0100": "Comunicação perdida com a ECU do motor",
    "U0155": "Comunicação perdida com o painel de instrumentos",
}

UNKNOWN = "descrição não disponível"

# Códigos ligados ao problema investigado (óleo/arrefecimento), destacados na UI.
OIL_COOLING = {c for c in DESCRIPTIONS if c[:4] in {"P052", "P019", "P011", "P012", "P021", "P048"}}


def describe(code: str) -> str:
    return DESCRIPTIONS.get(code.upper(), UNKNOWN)


def decode_pair(a: int, b: int) -> str | None:
    """Dois bytes -> código. 0000 significa "sem código" (enchimento)."""
    if a == 0 and b == 0:
        return None
    t = _TYPE[(a >> 6) & 0x03]
    d1 = (a >> 4) & 0x03
    return f"{t}{d1}{a & 0x0F:X}{b >> 4:X}{b & 0x0F:X}"


def decode_dtc_bytes(data: Iterable[int]) -> list[str]:
    """Decodifica uma sequência de bytes (pares) em códigos, sem duplicatas."""
    data = list(data)
    out: list[str] = []
    for i in range(0, len(data) - 1, 2):
        c = decode_pair(data[i], data[i + 1])
        if c and c not in out:
            out.append(c)
    return out


def new_codes(previous: Iterable[str], current: Iterable[str]) -> list[str]:
    prev = set(previous)
    return [c for c in current if c not in prev]
