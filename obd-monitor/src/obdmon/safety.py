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

Otimizações de velocidade permitidas (todas continuam SOMENTE LEITURA):
- modo 01 com 1 a 6 PIDs na mesma requisição (``010C0D05``), sem PID repetido;
- sufixo de "respostas esperadas" no modo 01: UM dígito 1-9 no fim
  (``010C1``, ``010C0D051``) — faz o ELM327 responder sem esperar o timeout;
- AT de temporização/protocolo: ATAT0/1/2, ATST<hh>, ATSP<n>, ATDP/ATDPN, ATCAF0/1.
"""
from __future__ import annotations

import re

MAX_PIDS_PER_REQUEST = 6

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
        r"^ATSPA?[0-9A-C]$",  # seleção de protocolo (fixa ou "automático começando por")
        r"^ATST[0-9A-F]{2}$",  # timeout de resposta (x 4 ms)
        r"^ATAT[0-2]$",      # timeout adaptativo
        r"^ATCAF[01]$",      # formatação automática CAN
        # --- OBD2 somente leitura ---
        # modo 01 (dado atual): o formato exato (1-6 PIDs + sufixo opcional) é
        # conferido em _mode01_ok(); este padrão só deixa passar hex do modo 01.
        r"^01(?:[0-9A-F]{2}){1,6}[1-9]?$",
        r"^03$",             # DTCs confirmados
        r"^07$",             # DTCs pendentes
        r"^0A$",             # DTCs permanentes
        r"^09(00|02|04|0A)$",  # informações do veículo (VIN, calibração, nome da ECU)
    )
)

_MODE01_RE = re.compile(r"^01((?:[0-9A-F]{2}){1,6})([1-9]?)$")


def split_mode01(norm: str) -> tuple[list[int], int] | None:
    """``"010C0D1"`` -> ([0x0C, 0x0D], 1). Sufixo 0 = sem sufixo. None se não for modo 01 válido."""
    m = _MODE01_RE.match(norm)
    if not m:
        return None
    hexs = m.group(1)
    pids = [int(hexs[i:i + 2], 16) for i in range(0, len(hexs), 2)]
    return pids, int(m.group(2) or 0)


def _mode01_ok(norm: str) -> bool:
    sp = split_mode01(norm)
    if sp is None:
        return False
    pids, _ = sp
    return 1 <= len(pids) <= MAX_PIDS_PER_REQUEST and len(set(pids)) == len(pids)


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
    if not any(p.match(norm) for p in ALLOWED_PATTERNS):
        return False
    if norm.startswith("01"):
        return _mode01_ok(norm)
    return True


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
