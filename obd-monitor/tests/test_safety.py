"""Whitelist: prova que nenhum comando fora da lista (principalmente o modo 04)
consegue chegar ao adaptador."""
import asyncio

import pytest

from obdmon import safety
from obdmon.elm import ELM327
from obdmon.safety import ForbiddenCommandError
from obdmon.sim.fake_elm import SimAdapter, SimCar
from obdmon.transport.ble import BleTransport
from obdmon.transport.simulated import SimulatedTransport

from .conftest import FAST_LAT, FAST_SEARCH

FORBIDDEN = [
    "04", "0400", " 04 ", "04\r", "14", "14FF00", "11", "1101", "27", "2701", "2E", "2EF190",
    "2F", "2F000103", "31", "3101FF00", "10", "1003", "3E", "22F190", "08", "0501", "06",
    "ATPP0CSV00", "ATPP0CON", "ATSH7E0", "ATMA", "ATBRD23", "ATCRA7E8", "ATWM", "ATSI",
    "0100\r04", "0100\n04", "010C04", "0104 04", "", "0", "01", "01ZZ", "AT Z\r04", "ÁTZ",
    "0D", "ATZ;04", "0300", "0702",
]
ALLOWED = ["ATZ", "ATE0", "ATE1", "ATL0", "ATS0", "ATH0", "ATSP0", "ATSPA6", "ATDP", "ATDPN", "ATRV",
           "ATI", "AT@1", "0100", "010C", "01 0C", "015C", "03", "07", "0A", "0902", "atz", "ATST32"]


@pytest.mark.parametrize("cmd", FORBIDDEN)
def test_forbidden(cmd):
    assert not safety.is_command_allowed(cmd)
    with pytest.raises(ForbiddenCommandError):
        safety.check_command(cmd)


@pytest.mark.parametrize("cmd", ALLOWED)
def test_allowed(cmd):
    assert safety.is_command_allowed(cmd)


def test_every_mode_except_whitelisted_is_blocked():
    """Varre todos os 256 modos com 0, 1 e 2 bytes de parâmetro."""
    allowed_modes = {0x01, 0x03, 0x07, 0x0A, 0x09}
    for mode in range(256):
        for suffix in ("", "00", "0C", "0000", "FF00"):
            cmd = f"{mode:02X}{suffix}"
            if safety.is_command_allowed(cmd):
                assert mode in allowed_modes, cmd


def test_raw_bytes_layer():
    safety.check_raw_bytes(b"010C\r")
    for raw in (b"04\r", b"010C\r04\r", b"010C", b"010C\r\n", b"\xc3\x81TZ\r"):
        with pytest.raises(ForbiddenCommandError):
            safety.check_raw_bytes(raw)


def _sim():
    car = SimCar("normal", time_factor=1, seed=1)
    return SimAdapter(car, latency=FAST_LAT, search_delay=FAST_SEARCH)


def test_elm_send_blocks_mode04_and_nothing_reaches_adapter():
    async def go():
        ad = _sim()
        t = SimulatedTransport(ad)
        await t.connect()
        elm = ELM327(t)
        await elm.initialize()
        before = list(ad.elm.received)
        for cmd in FORBIDDEN:
            with pytest.raises(ForbiddenCommandError):
                await elm.send(cmd)
        assert ad.elm.received == before  # nada chegou ao "adaptador"
        # e o ELM continua funcionando normalmente
        r = await elm.query_pid(0x0C)
        assert r.ok
        await t.disconnect()
        return ad.elm.received
    received = asyncio.run(go())
    assert not any(c.strip().upper().startswith("04") for c in received)
    assert all(safety.is_command_allowed(c) for c in received)


def test_transport_layer_blocks_even_if_elm_is_bypassed():
    async def go():
        ad = _sim()
        t = SimulatedTransport(ad)
        await t.connect()
        for raw in (b"04\r", b"0400\r", b"14FF00\r", b"ATPP0CSV00\r"):
            with pytest.raises(ForbiddenCommandError):
                await t.write(raw)
        await t.disconnect()
        return ad.elm.received
    assert asyncio.run(go()) == []


def test_ble_transport_blocks_before_touching_bluetooth():
    class Boom:
        is_connected = True

        async def write_gatt_char(self, *a, **k):
            raise AssertionError("não deveria chegar ao Bluetooth")

    async def go():
        t = BleTransport("00:11:22:33:44:55")
        t._client = Boom()
        t._connected = True
        with pytest.raises(ForbiddenCommandError):
            await t.write(b"04\r")
        with pytest.raises(ForbiddenCommandError):
            await t.write(b"010C\r04\r")
    asyncio.run(go())


def test_no_forbidden_command_in_source_code():
    """Varredura do código-fonte: nenhuma string de comando proibido sendo enviada."""
    import re
    from pathlib import Path
    src = Path(__file__).resolve().parents[1] / "src" / "obdmon"
    pat = re.compile(r"""send\(\s*["'](04|14|2E|2F|31|11|27)""", re.I)
    for f in src.rglob("*.py"):
        assert not pat.search(f.read_text(encoding="utf-8")), f
