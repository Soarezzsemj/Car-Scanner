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

    def __init__(self, adapter: SimAdapter, write_with_response: Optional[bool] = None) -> None:
        """Como o BLE real: tem escrita com e sem resposta; o padrão
        (None) é SEM resposta, igual ao BleTransport."""
        super().__init__()
        self.adapter = adapter
        self._connected = False
        self._rxbuf = ""
        self._tasks: set[asyncio.Task] = set()
        self._gen = 0  # invalida respostas pendentes de uma conexão anterior
        self._with_response = bool(write_with_response)

    @property
    def write_modes(self) -> tuple[bool, ...]:
        return (True, False)

    @property
    def write_with_response(self) -> Optional[bool]:
        return self._with_response

    def set_write_with_response(self, with_response: bool) -> bool:
        self._with_response = bool(with_response)
        return True

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
        if self._with_response:
            # escrita com confirmação: espera o "ack" do próximo evento de conexão
            await asyncio.sleep(self.adapter.rng.uniform(*self.adapter.write_ack))
        else:
            await asyncio.sleep(0)
        self._rxbuf += data.decode("ascii")
        while "\r" in self._rxbuf:
            cmd, self._rxbuf = self._rxbuf.split("\r", 1)
            t = asyncio.get_running_loop().create_task(self._respond(cmd, self._gen))
            self._tasks.add(t)
            t.add_done_callback(self._tasks.discard)

    async def _respond(self, cmd: str, gen: int) -> None:
        ad = self.adapter
        parts = ad.elm.handle_parts(cmd)
        if ad.stalled():
            return  # travada silenciosa: o comando "some"
        if ad.faults.truncate and ad.rng.random() < ad.faults.truncate:
            text = "".join(t for t, _ in parts)
            if len(text) > 6:
                cut = ad.rng.randint(2, len(text) - 3)
                parts = [(text[:cut] + "\r>", sum(d for _, d in parts))]
        await asyncio.sleep(ad.rng.uniform(*ad.latency))   # rádio BLE
        n = ad.chunk_size
        for text, delay in parts:
            if delay > 0:
                await asyncio.sleep(delay)
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
        return {"transporte": "Simulador (ELM327 falso)", "cenario": self.adapter.car.scenario.name,
                "perfil_adaptador": self.adapter.profile,
                "escrita_com_resposta": "sim" if self._with_response else "não",
                "modos_de_escrita": "com resposta, sem resposta"}


def make_sim_factory(adapter: SimAdapter, write_with_response_ref=None):
    """``write_with_response_ref``: função que devolve o modo de escrita
    desejado (lido a cada reconexão, como o BleTransport)."""
    ref = write_with_response_ref or (lambda: None)
    return lambda: SimulatedTransport(adapter, ref())


__all__ = ["SimulatedTransport", "make_sim_factory", "Optional"]
