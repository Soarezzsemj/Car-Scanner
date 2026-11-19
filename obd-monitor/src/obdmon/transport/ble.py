"""Transporte Bluetooth LE real (bleak) para adaptadores ELM327 BLE."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Optional

from .. import safety
from .base import Transport, TransportError

log = logging.getLogger(__name__)

NAME_HINTS = ("obd", "elm", "link", "vgate", "icar", "veepeak", "scan")
STANDARD_SERVICES = ("00001800", "00001801", "0000180a")
PREFERRED = (("fff0", "fff1", "fff2"), ("ffe0", "ffe1", "ffe1"), ("18f0", "2af0", "2af1"))


@dataclass
class FoundDevice:
    name: str
    address: str
    rssi: Optional[int]
    candidate: bool


async def scan_adapters(timeout: float = 8.0) -> list[FoundDevice]:
    """Procura dispositivos BLE. Candidatos (nome parecido com OBD) primeiro."""
    from bleak import BleakScanner

    try:
        found = await BleakScanner.discover(timeout=timeout, return_adv=True)
    except Exception as e:  # Bluetooth desligado, sem adaptador, etc.
        raise TransportError(f"Não foi possível procurar dispositivos Bluetooth: {e}") from e
    out: list[FoundDevice] = []
    for dev, adv in found.values():
        name = dev.name or adv.local_name or ""
        cand = bool(name) and any(h in name.lower() for h in NAME_HINTS)
        out.append(FoundDevice(name or "(sem nome)", dev.address, getattr(adv, "rssi", None), cand))
    out.sort(key=lambda d: (not d.candidate, -(d.rssi or -999)))
    return out


def _short(uuid: str) -> str:
    u = uuid.lower()
    return u[4:8] if u.startswith("0000") and u.endswith("-0000-1000-8000-00805f9b34fb") else u


def pick_characteristics(services) -> tuple[object, object]:
    """Escolhe (rx, tx). Prefere os pares conhecidos, depois o mesmo serviço,
    depois características de serviços diferentes."""
    usable = [s for s in services if not s.uuid.lower().startswith(STANDARD_SERVICES)]

    def can_rx(ch): return "notify" in ch.properties or "indicate" in ch.properties
    def can_tx(ch): return "write" in ch.properties or "write-without-response" in ch.properties

    by_short = {_short(s.uuid): s for s in usable}
    for svc, rxu, txu in PREFERRED:
        s = by_short.get(svc)
        if not s:
            continue
        chars = {_short(c.uuid): c for c in s.characteristics}
        rx, tx = chars.get(rxu), chars.get(txu)
        if rx is not None and tx is not None and can_rx(rx) and can_tx(tx):
            return rx, tx
    for s in usable:
        rx = next((c for c in s.characteristics if can_rx(c)), None)
        tx = next((c for c in s.characteristics if can_tx(c)), None)
        if rx is not None and tx is not None:
            return rx, tx
    rx = tx = None
    for s in usable:
        for c in s.characteristics:
            if rx is None and can_rx(c):
                rx = c
            if tx is None and can_tx(c):
                tx = c
    return rx, tx


def describe_services(services) -> str:
    lines = []
    for s in services:
        lines.append(f"Serviço {s.uuid}")
        for c in s.characteristics:
            lines.append(f"    {c.uuid}  {', '.join(c.properties)}")
    return "\n".join(lines)


class BleTransport(Transport):
    name = "Bluetooth LE"

    def __init__(self, address: str, connect_timeout: float = 15.0) -> None:
        super().__init__()
        self.address = address
        self.connect_timeout = connect_timeout
        self._client = None
        self._rx = None
        self._tx = None
        self._with_response = False
        self._connected = False

    async def connect(self) -> None:
        from bleak import BleakClient

        def _disc(_client):
            was = self._connected
            self._connected = False
            if was:
                self._emit_disconnect()

        client = BleakClient(self.address, timeout=self.connect_timeout, disconnected_callback=_disc)
        try:
            await client.connect()
        except Exception as e:
            raise TransportError(f"Falha ao conectar em {self.address}: {e}") from e
        rx, tx = pick_characteristics(client.services)
        if rx is None or tx is None:
            desc = describe_services(client.services)
            try:
                await client.disconnect()
            except Exception:
                pass
            raise TransportError("Não achei as características de comunicação do adaptador.\n"
                                 "Serviços encontrados:\n" + desc)
        self._client, self._rx, self._tx = client, rx, tx
        self._with_response = "write" in tx.properties
        try:
            await client.start_notify(rx, lambda _h, data: self._emit_data(bytes(data)))
        except Exception as e:
            try:
                await client.disconnect()
            except Exception:
                pass
            raise TransportError(f"Falha ao ativar notificações: {e}") from e
        self._connected = True
        log.info("BLE conectado: rx=%s tx=%s response=%s", rx.uuid, tx.uuid, self._with_response)

    async def write(self, data: bytes) -> None:
        safety.check_raw_bytes(data)  # última barreira: somente leitura
        if not self._connected or self._client is None:
            raise TransportError("Não conectado")
        try:
            await self._client.write_gatt_char(self._tx, data, response=self._with_response)
        except Exception as e:
            raise TransportError(f"Erro de escrita BLE: {e}") from e

    async def disconnect(self) -> None:
        c, self._client = self._client, None
        self._connected = False
        if c is not None:
            try:
                await asyncio.wait_for(c.disconnect(), 5.0)
            except Exception:
                pass

    @property
    def is_connected(self) -> bool:
        return self._connected and self._client is not None and bool(getattr(self._client, "is_connected", True))

    def describe(self) -> dict:
        d = {"transporte": self.name, "endereco": self.address}
        if self._rx is not None:
            d["caracteristica_rx"] = self._rx.uuid
            d["caracteristica_tx"] = self._tx.uuid
            d["escrita_com_resposta"] = "sim" if self._with_response else "não"
        return d
