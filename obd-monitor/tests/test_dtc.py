from obdmon import dtc
from obdmon.poller import Poller


def test_decode_pairs():
    assert dtc.decode_pair(0x01, 0x33) == "P0133"
    assert dtc.decode_pair(0x05, 0x24) == "P0524"
    assert dtc.decode_pair(0x42, 0x00) == "C0200"
    assert dtc.decode_pair(0x40, 0x01) == "C0001"
    assert dtc.decode_pair(0x91, 0x23) == "B1123"
    assert dtc.decode_pair(0xC1, 0x00) == "U0100"
    assert dtc.decode_pair(0x30, 0x00) == "P3000"
    assert dtc.decode_pair(0x00, 0x00) is None


def test_decode_sequence_dedup_and_padding():
    assert dtc.decode_dtc_bytes([0x01, 0x33, 0x00, 0x00, 0x01, 0x33, 0xC1, 0x00]) == ["P0133", "U0100"]


def test_descriptions_pt():
    for code in ["P0520", "P0521", "P0522", "P0523", "P0524", "P0115", "P0116", "P0117", "P0118",
                 "P0119", "P0125", "P0128", "P0217", "P0480", "P0481", "P0300", "P0301", "P0302",
                 "P0303", "P0304", "P0171", "P0172", "P0562", "P0563"]:
        assert dtc.describe(code) != dtc.UNKNOWN, code
    assert dtc.describe("P1ABC") == "descrição não disponível"
    assert "óleo" in dtc.describe("P0524").lower()


def test_new_codes():
    assert dtc.new_codes(["P0133"], ["P0133", "P0524"]) == ["P0524"]
    assert dtc.new_codes([], []) == []


def test_poller_dtc_events():
    events = []
    p = Poller(lambda: None, on_event=lambda c, d: events.append(c))
    p.update_dtcs(0x07, ["P0521"])
    assert events == ["DTC_NOVO:P0521"]
    p.update_dtcs(0x07, ["P0521", "P0524"])
    assert events[-1] == "DTC_NOVO:P0524"
    # pendente -> confirmado não é "novo"
    p.update_dtcs(0x03, ["P0524"])
    assert events.count("DTC_NOVO:P0524") == 1
    assert p.dtcs["P0524"].modes == {"pendente", "confirmado"}
    # some de todos os modos: precisa de 2 leituras para ser "apagado"
    p.update_dtcs(0x07, [])
    p.update_dtcs(0x03, [])
    assert "DTC_APAGADO:P0521" in events      # sumiu em 2 leituras
    assert "DTC_APAGADO:P0524" not in events  # só 1 leitura sem ele
    p.update_dtcs(0x07, [])
    assert "DTC_APAGADO:P0524" in events
    assert p.active_codes() == []
