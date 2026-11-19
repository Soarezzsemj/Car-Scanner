"""Interface comum dos transportes (BLE real e simulado)."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Callable, Optional

DataCallback = Callable[[bytes], None]
DisconnectCallback = Callable[[], None]


class TransportError(Exception):
    """Falha de conexão/escrita no transporte."""


class Transport(ABC):
    """Um canal de bytes até o ELM327.

    - ``connect()``  abre o canal (levanta TransportError se falhar)
    - ``write(b)``   envia bytes (um comando terminado em ``\\r``)
    - os dados recebidos chegam em pedaços pelo callback de dados
    - ``disconnect()`` fecha
    Toda implementação DEVE chamar ``safety.check_raw_bytes`` em ``write``.
    """

    name: str = "transporte"
    is_simulated: bool = False

    def __init__(self) -> None:
        self._on_data: Optional[DataCallback] = None
        self._on_disconnect: Optional[DisconnectCallback] = None

    def set_data_callback(self, cb: Optional[DataCallback]) -> None:
        self._on_data = cb

    def set_disconnect_callback(self, cb: Optional[DisconnectCallback]) -> None:
        self._on_disconnect = cb

    def _emit_data(self, data: bytes) -> None:
        if self._on_data is not None:
            self._on_data(data)

    def _emit_disconnect(self) -> None:
        if self._on_disconnect is not None:
            self._on_disconnect()

    @abstractmethod
    async def connect(self) -> None: ...

    @abstractmethod
    async def write(self, data: bytes) -> None: ...

    @abstractmethod
    async def disconnect(self) -> None: ...

    @property
    @abstractmethod
    def is_connected(self) -> bool: ...

    def describe(self) -> dict:
        """Informações para a aba Diagnóstico."""
        return {"transporte": self.name}
