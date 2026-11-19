"""Transporte simulado: mesma interface do BLE, falando com o ELM327 falso.

As respostas chegam em pedaços de ~20 bytes com pequenos atrasos, como no BLE.
"""
from __future__ import annotations

import asyncio
from typing import Optional

from .. import safety
from ..sim.fake_elm import SimAdapter
from .base import Transport, TransportError


class SimulatedTransport(Transport):
    name = "Simulador"
    is_simulated = True

    def __init__(self, adapter: SimAdapter) -> None:
        super().__init__()
        self.adapter = adapter
        self._connected = False
        self._rxbuf = ""
        self._tasks: set[asyncio.Task] = set()
        self._gen = 0  # invalida respostas pendentes de uma conexão anterior

    async def connect(self) -> None:
        await asyncio.sleep(0.05)
        if not self.adapter.link_up():
            await asyncio.sleep(0.3)
            raise TransportError("Adaptador não encontrado (simulado: Bluetooth fora do ar)")
        self._connected = True
        self._gen += 1
        self.adapter.add_drop_listener(self._on_drop)
        self.adapter.ensure_fault_task()

    def _on_drop(self) -> None:
        if self._connected:
            self._connected = False
            self._gen += 1
            self.adapter.remove_drop_listener(self._on_drop)
            self._emit_disconnect()

    async def write(self, data: bytes) -> None:
        safety.check_raw_bytes(data)  # mesma barreira do BLE real
        if not self._connected:
            raise TransportError("Não conectado")
        await asyncio.sleep(0)
        self._rxbuf += data.decode("ascii")
        while "\r" in self._rxbuf:
            cmd, self._rxbuf = self._rxbuf.split("\r", 1)
            t = asyncio.get_running_loop().create_task(self._respond(cmd, self._gen))
            self._tasks.add(t)
            t.add_done_callback(self._tasks.discard)

    async def _respond(self, cmd: str, gen: int) -> None:
        ad = self.adapter
        text, extra = ad.elm.handle(cmd)
        if ad.stalled():
            return  # travada silenciosa: o comando "some"
        await asyncio.sleep(ad.rng.uniform(*ad.latency) + extra)
        if ad.faults.truncate and ad.rng.random() < ad.faults.truncate and len(text) > 6:
            cut = ad.rng.randint(2, len(text) - 3)
            text = text[:cut] + "\r>"
        n = ad.chunk_size
        for i in range(0, len(text), n):
            if gen != self._gen or not self._connected:
                return
            if ad.faults.packet_loss and ad.rng.random() < ad.faults.packet_loss:
                continue
            self._emit_data(text[i:i + n].encode("ascii"))
            await asyncio.sleep(ad.rng.uniform(*ad.chunk_gap))

    async def disconnect(self) -> None:
        self._connected = False
        self._gen += 1
        self.adapter.remove_drop_listener(self._on_drop)
        for t in list(self._tasks):
            t.cancel()

    @property
    def is_connected(self) -> bool:
        return self._connected

    def describe(self) -> dict:
        return {"transporte": "Simulador (ELM327 falso)", "cenario": self.adapter.car.scenario.name}


def make_sim_factory(adapter: SimAdapter):
    return lambda: SimulatedTransport(adapter)


__all__ = ["SimulatedTransport", "make_sim_factory", "Optional"]
