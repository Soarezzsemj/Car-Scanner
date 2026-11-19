"""Whitelist de comandos: o programa é SOMENTE LEITURA.

Qualquer comando que não case com um dos padrões abaixo é recusado antes
de chegar ao adaptador. A checagem é feita em duas camadas:

1. ``ELM327.send()`` (camada de protocolo);
2. ``Transport.write()`` de cada transporte (última barreira antes do rádio).

Modos proibidos explicitamente (exemplos): 04 (apagar DTC), 2E, 2F, 31, 14,
11, 27, 10, 3E, 22* — e qualquer outro que não esteja listado.
(*22 é leitura UDS e um dia poderá ser liberado para a extensão PSA, mas
não agora.)

Comandos AT que gravam na EEPROM do adaptador (ATPP...), mudam o cabeçalho
(ATSH) ou entram em modo de monitoramento (ATMA) também não são permitidos.
"""
from __future__ import annotations

import re

ALLOWED_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p)
    for p in (
        # --- AT (configuração do próprio ELM327, só o necessário) ---
        r"^ATZ$",            # reset
        r"^ATWS$",           # reset "morno"
        r"^ATD$",            # padrões de fábrica (não grava EEPROM)
        r"^ATI$",            # versão
        r"^AT@1$",           # descrição do dispositivo
        r"^ATRV$",           # tensão na porta OBD
        r"^ATDPN?$",         # protocolo atual (descrição / número)
        r"^ATE[01]$",        # eco
        r"^ATL[01]$",        # linefeed
        r"^ATS[01]$",        # espaços
        r"^ATH[01]$",        # cabeçalhos
        r"^ATSPA?[0-9A-C]$",  # seleção de protocolo
        r"^ATST[0-9A-F]{2}$",  # timeout
        r"^ATAT[0-2]$",      # timeout adaptativo
        # --- OBD2 somente leitura ---
        r"^01[0-9A-F]{2}$",  # modo 01: dado atual (um PID por vez)
        r"^03$",             # DTCs confirmados
        r"^07$",             # DTCs pendentes
        r"^0A$",             # DTCs permanentes
        r"^09(00|02|04|0A)$",  # informações do veículo (VIN, calibração, nome da ECU)
    )
)


class ForbiddenCommandError(Exception):
    """Tentativa de enviar comando fora da whitelist (bloqueado)."""


def normalize(cmd: str) -> str:
    return cmd.replace(" ", "").upper()


def is_command_allowed(cmd: str) -> bool:
    """True somente se o comando inteiro casa com um padrão permitido.

    Caracteres de controle (\\r, \\n...) dentro do comando são recusados, para
    impedir "injeção" de um segundo comando (ex.: ``"0100\\r04"``).
    """
    if not isinstance(cmd, str) or not cmd:
        return False
    if any(ord(c) < 32 or ord(c) > 126 for c in cmd):
        return False
    norm = normalize(cmd)
    return any(p.match(norm) for p in ALLOWED_PATTERNS)


def check_command(cmd: str) -> str:
    """Valida e devolve o comando normalizado, ou levanta ForbiddenCommandError."""
    if not is_command_allowed(cmd):
        raise ForbiddenCommandError(f"Comando bloqueado pela whitelist (somente leitura): {cmd!r}")
    return normalize(cmd)


def check_raw_bytes(data: bytes) -> None:
    """Checagem na camada de transporte: os bytes devem ser exatamente
    ``<comando permitido>\\r``."""
    try:
        text = data.decode("ascii")
    except UnicodeDecodeError as e:
        raise ForbiddenCommandError(f"Bytes não-ASCII bloqueados: {data!r}") from e
    if not text.endswith("\r") or text.count("\r") != 1 or "\n" in text:
        raise ForbiddenCommandError(f"Formato de envio inválido (um comando por vez): {data!r}")
    check_command(text[:-1])
