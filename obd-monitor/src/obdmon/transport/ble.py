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


def choose_write_mode(modes: tuple, wanted: Optional[bool]) -> bool:
    """True = escrita com resposta. Padrão: SEM resposta quando a característica
    permite (economiza uma ida e volta de rádio por comando)."""
    if wanted is not None and wanted in modes:
        return wanted
    return False if False in modes else True


def describe_services(services) -> str:
    lines = []
    for s in services:
        lines.append(f"Serviço {s.uuid}")
        for c in s.characteristics:
            lines.append(f"    {c.uuid}  {', '.join(c.properties)}")
    return "\n".join(lines)


class BleTransport(Transport):
    name = "Bluetooth LE"

    def __init__(self, address: str, connect_timeout: float = 15.0,
                 write_with_response: Optional[bool] = None) -> None:
        """``write_with_response``: None = automático (usa escrita SEM resposta
        quando a característica permite: economiza uma ida e volta de rádio por
        comando; os comandos são curtos e sempre esperamos o '>' antes do próximo)."""
        super().__init__()
        self.address = address
        self.connect_timeout = connect_timeout
        self._client = None
        self._rx = None
        self._tx = None
        self._with_response = False
        self._wanted_response = write_with_response
        self._connected = False
        self._link_info: dict = {}
        self._conn_req = None

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
        modes = self.write_modes
        self._with_response = choose_write_mode(modes, self._wanted_response)
        try:
            await client.start_notify(rx, lambda _h, data: self._emit_data(bytes(data)))
        except Exception as e:
            try:
                await client.disconnect()
            except Exception:
                pass
            raise TransportError(f"Falha ao ativar notificações: {e}") from e
        self._connected = True
        self._link_info = self._tune_link(client)
        log.info("BLE conectado: rx=%s tx=%s escrita_com_resposta=%s modos=%s %s", rx.uuid, tx.uuid,
                 self._with_response, ",".join("com" if m else "sem" for m in modes),
                 " ".join(f"{k}={v}" for k, v in self._link_info.items()))

    def _tune_link(self, client) -> dict:
        """Melhor esforço, só no Windows 11: pede ao Windows parâmetros de conexão
        "ThroughputOptimized" (intervalo de conexão curto) e lê o intervalo
        atual. O Windows/adaptador podem recusar; nada aqui é obrigatório.
        O pedido só vale enquanto o objeto existir: fica guardado em _conn_req."""
        info: dict = {}
        try:
            info["mtu"] = int(client.mtu_size)
        except Exception:
            pass
        dev = getattr(getattr(client, "_backend", None), "_requester", None)
        if dev is None:
            return info
        try:
            from winrt.windows.devices.bluetooth import BluetoothLEPreferredConnectionParameters as P
            req = dev.request_preferred_connection_parameters(P.throughput_optimized)
            self._conn_req = req
            info["pedido_intervalo_curto"] = str(getattr(req, "status", "enviado")).split(".")[-1]
        except Exception as e:  # Windows 10 / API ausente
            info["pedido_intervalo_curto"] = f"indisponível ({type(e).__name__})"
        try:
            cp = dev.get_connection_parameters()
            info["intervalo_conexao_ms"] = round(cp.connection_interval * 1.25, 2)
            info["latencia_escravo"] = cp.connection_latency
            info["supervisao_ms"] = cp.link_timeout * 10
        except Exception:
            pass
        return info

    @property
    def write_modes(self) -> tuple[bool, ...]:
        if self._tx is None:
            return ()
        props = self._tx.properties
        return tuple(m for m, p in ((True, "write"), (False, "write-without-response")) if p in props)

    @property
    def write_with_response(self) -> Optional[bool]:
        return self._with_response if self._tx is not None else None

    def set_write_with_response(self, with_response: bool) -> bool:
        if with_response not in self.write_modes:
            return False
        self._with_response = with_response
        self._wanted_response = with_response
        log.info("BLE: escrita %s resposta", "com" if with_response else "sem")
        return True

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
        req, self._conn_req = self._conn_req, None
        if req is not None:
            try:
                req.close()
            except Exception:
                pass
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
            d["modos_de_escrita"] = ", ".join("com resposta" if m else "sem resposta" for m in self.write_modes)
        for k, v in self._link_info.items():
            d[k] = v
        if self._client is not None and "intervalo_conexao_ms" in self._link_info:
            try:   # o Windows pode ter mudado o intervalo depois do pedido
                dev = self._client._backend._requester
                d["intervalo_conexao_ms"] = round(dev.get_connection_parameters().connection_interval * 1.25, 2)
            except Exception:
                pass
        return d
