"""Ponto de extensão para dados específicos do fabricante (NÃO implementado).

Ideia para o futuro: ler, via UDS (ISO 14229), parâmetros da PSA que não
existem no OBD2 genérico — por exemplo o estado do pressostato de óleo ou a
temperatura do óleo calculada pelo motor/BSI.

Como seria:
1. Uma classe que implemente ``VehicleExtension`` (abaixo);
2. Ampliar a whitelist em ``safety.py`` SOMENTE com serviços de leitura
   (ex.: ``22 xx xx`` – ReadDataByIdentifier) e o cabeçalho da ECU de destino
   (``ATSH``), com testes provando que nenhum serviço de escrita/atuação passa;
3. O poller chamaria ``poll()`` no ciclo médio e juntaria os valores ao log.

Nada disso está ativo: hoje o programa só usa OBD2 genérico (modos 01/03/07/0A).
"""
from __future__ import annotations

from typing import Protocol

from ..elm import ELM327


class VehicleExtension(Protocol):
    name: str
    columns: tuple[str, ...]   # colunas extras no CSV

    async def setup(self, elm: ELM327) -> bool:
        """Prepara a comunicação (ex.: ATSH). Devolve False se não suportado."""
        ...

    async def poll(self, elm: ELM327) -> dict[str, float | None]:
        """Lê os valores extras. Deve usar apenas comandos de LEITURA."""
        ...


EXTENSIONS: list[VehicleExtension] = []   # vazio de propósito
