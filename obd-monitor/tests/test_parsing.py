"""Parsing do ELM327 e fórmulas dos PIDs, com respostas reais e malformadas."""
import pytest

from obdmon.elm import clean_lines, find_error, parse_dtc_response, parse_mode01
from obdmon.pids import BY_PID, decode_support_mask, decode_value


def p01(raw, pid, cmd=None):
    return parse_mode01(clean_lines(raw, cmd or f"01{pid:02X}"), pid)


# --- fórmulas -------------------------------------------------------------

@pytest.mark.parametrize("pid,data,expected", [
    (0x0C, [0x1A, 0xF8], 1726.0),        # (256*26+248)/4
    (0x0C, [0x00, 0x00], 0.0),
    (0x0C, [0xFF, 0xFF], 16383.75),
    (0x0D, [0x3C], 60),
    (0x05, [0x82], 90),                  # 130-40
    (0x05, [0x00], -40),
    (0x5C, [0xAF], 135),
    (0x04, [0xFF], 100.0),
    (0x04, [0x80], 128 * 100 / 255),
    (0x11, [0x26], 38 * 100 / 255),
    (0x0F, [0x46], 30),
    (0x42, [0x37, 0x6E], 14.19),         # 14190 mV
    (0x46, [0x41], 25),
])
def test_formulas(pid, data, expected):
    assert decode_value(pid, data) == pytest.approx(expected)


def test_formula_truncated_returns_none():
    assert decode_value(0x0C, [0x1A]) is None
    assert decode_value(0x42, []) is None


def test_all_pids_have_formula():
    for pid, d in BY_PID.items():
        assert decode_value(pid, [0x40] * d.nbytes) is not None


# --- modo 01 ---------------------------------------------------------------

def test_basic_with_spaces():
    assert p01("41 0C 1A F8 \r\r", 0x0C) == [0x1A, 0xF8]


def test_without_spaces():
    assert p01("410C1AF8\r\r", 0x0C) == [0x1A, 0xF8]


def test_echo_on_separate_line():
    assert p01("010C\r41 0C 1A F8 \r\r", 0x0C) == [0x1A, 0xF8]


def test_echo_glued_to_response():
    assert p01("010C410C1AF8\r", 0x0C) == [0x1A, 0xF8]


def test_searching_prefix():
    assert p01("SEARCHING...\r41 00 BE 3F A8 13 \r\r", 0x00) == [0xBE, 0x3F, 0xA8, 0x13]


def test_searching_glued():
    assert p01("SEARCHING...410D3C\r", 0x0D) == [0x3C]


def test_no_data():
    lines = clean_lines("NO DATA\r\r", "015C")
    assert parse_mode01(lines, 0x5C) is None
    assert find_error(lines) == "NO DATA"


@pytest.mark.parametrize("raw,err", [
    ("SEARCHING...\rUNABLE TO CONNECT\r", "UNABLE TO CONNECT"),
    ("CAN ERROR\r", "CAN ERROR"),
    ("BUS INIT: ...ERROR\r", "BUS INIT"),
    ("?\r", "?"),
    ("STOPPED\r", "STOPPED"),
])
def test_errors(raw, err):
    lines = clean_lines(raw, "0100")
    assert parse_mode01(lines, 0) is None
    assert find_error(lines) == err


def test_bus_init_ok_is_not_error():
    lines = clean_lines("BUS INIT: ...OK\r41 0D 3C\r", "010D")
    assert find_error(lines) is None
    assert parse_mode01(lines, 0x0D) == [0x3C]


def test_duplicate_lines_two_ecus():
    assert p01("41 0C 1A F8 \r41 0C 1A F8 \r\r", 0x0C) == [0x1A, 0xF8]


def test_wrong_pid_rejected():
    # resposta atrasada de outro PID não pode virar valor deste
    assert p01("41 0D 3C \r", 0x0C) is None


def test_garbage_and_mid_byte_match():
    # "410C" aparece no meio de outros bytes desalinhados: não deve casar
    assert p01("1410C1\r", 0x0C) is None
    assert p01("XYZ 41 0C\r", 0x0C) is None   # sem dados


def test_lowercase_and_linefeeds():
    assert p01("41 0c 1a f8\r\n\r\n", 0x0C) == [0x1A, 0xF8]


def test_support_mask_two_ecus_union():
    from obdmon.elm import parse_mode01_all
    lines = clean_lines("41 00 BE 1F A8 13 \r41 00 80 00 00 01 \r\r", "0100")
    answers = parse_mode01_all(lines, 0x00)
    assert answers == [[0xBE, 0x1F, 0xA8, 0x13], [0x80, 0x00, 0x00, 0x01]]
    union = set()
    for a in answers:
        union |= decode_support_mask(0x00, a)
    assert 0x01 in union and 0x20 in union and 0x0C in union


def test_support_mask():
    # BE 1F A8 13 = exemplo clássico
    sup = decode_support_mask(0x00, [0xBE, 0x1F, 0xA8, 0x13])
    assert {0x01, 0x03, 0x04, 0x05, 0x06, 0x07, 0x0C, 0x0D, 0x0E, 0x0F, 0x10, 0x11, 0x13, 0x15, 0x1C, 0x1F, 0x20} == sup
    sup40 = decode_support_mask(0x40, [0x40, 0x00, 0x00, 0x10])
    assert sup40 == {0x42, 0x5C}
    assert decode_support_mask(0x00, [0xBE]) == set()


# --- DTC (modos 03/07/0A) -------------------------------------------------

def dtcs(raw, mode=3, can=True):
    return parse_dtc_response(clean_lines(raw, f"{mode:02X}"), mode, can)


def test_dtc_can_single():
    assert dtcs("43 02 01 33 05 24 \r\r") == ["P0133", "P0524"]


def test_dtc_can_none():
    assert dtcs("43 00 \r") == []


def test_dtc_no_data_is_empty():
    assert dtcs("NO DATA\r") == []


def test_dtc_can_multiframe():
    raw = "00A\r0: 43 04 01 33 05 24 \r1: 02 17 04 80 00 00 00 \r\r"
    assert dtcs(raw) == ["P0133", "P0524", "P0217", "P0480"]


def test_dtc_can_multiframe_out_of_order_and_no_spaces():
    raw = "00A\r1:0217048000000\r0:430401330524\r"
    # frame 1 com número ímpar de dígitos (truncado) -> ainda decodifica o que dá
    assert dtcs(raw)[:2] == ["P0133", "P0524"]


def test_dtc_can_multiframe_out_of_order():
    raw = "00A\r1: 02 17 04 80 00 00 00\r0: 43 04 01 33 05 24\r"
    assert dtcs(raw) == ["P0133", "P0524", "P0217", "P0480"]


def test_dtc_truncated_returns_none():
    assert dtcs("43 03 01 33 05 24\r") is None


def test_dtc_legacy_kline():
    raw = "43 01 33 05 24 00 00\r43 C0 01 00 00 00 00\r"
    assert dtcs(raw, can=False) == ["P0133", "P0524", "U0001"]


def test_dtc_autodetect():
    assert dtcs("43 01 33 05 24 00 00\r", can=None) == ["P0133", "P0524"]
    assert dtcs("43 01 01 33\r", can=None) == ["P0133"]


def test_dtc_pending_and_permanent_ids():
    assert dtcs("47 01 05 21\r", mode=7) == ["P0521"]
    assert dtcs("4A 01 01 33\r", mode=0x0A) == ["P0133"]
    assert dtcs("43 01 01 33\r", mode=7) is None  # resposta de outro modo


def test_dtc_with_echo():
    assert dtcs("03\r43 01 01 33\r") == ["P0133"]
