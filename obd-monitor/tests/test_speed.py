"""Velocidade e precisão: parsing multi-PID, sufixo de respostas esperadas,
detecção automática e fallback, benchmark, log longo com timestamps reais,
coluna de idade e tecla S com milissegundos."""
import asyncio
import csv
import time
from datetime import datetime, timedelta

import pytest

from obdmon.benchmark import Combo, ComboResult, benchmark_core, choose_best, tuning_record
from obdmon.config import Config
from obdmon.elm import ELM327, Tuning, clean_lines, mode01_cmd, parse_mode01_multi
from obdmon.engine import build_sim_engine
from obdmon.logger import AGE_COLUMNS, COLUMNS, READING_COLUMNS, format_row
from obdmon.pids import FAST_CORE, VALUE_KEYS
from obdmon.sim.car_model import Segment
from obdmon.sim.fake_elm import FakeELM, SimAdapter, SimCar
from obdmon.transport.simulated import SimulatedTransport

from .conftest import FAST_LAT, FAST_SEARCH


def read_rows(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f, delimiter=";"))


def num(s):
    return float(s.replace(",", "."))


# --- parsing multi-PID ------------------------------------------------------

def multi(raw, pids):
    return parse_mode01_multi(clean_lines(raw, mode01_cmd(pids)), pids)


def test_multi_single_line():
    d, why = multi("41 0C 1A F8 0D 3C \r\r", [0x0C, 0x0D])
    assert why == "" and d == {0x0C: [0x1A, 0xF8], 0x0D: [0x3C]}


def test_multi_single_line_no_spaces_any_order():
    d, why = multi("410D3C0C1AF8\r\r", [0x0C, 0x0D])
    assert why == "" and d[0x0C] == [0x1A, 0xF8] and d[0x0D] == [0x3C]


def test_multi_multiframe_can():
    # 41 0C 1A F8 0D 3C 05 7B 04 80 11 26 = 12 bytes (0x00C), 2 frames, padding 00
    raw = "00C\r0:410C1AF80D3C\r1:057B0480112600\r\r"
    d, why = multi(raw, [0x0C, 0x0D, 0x05, 0x04, 0x11])
    assert why == ""
    assert d == {0x0C: [0x1A, 0xF8], 0x0D: [0x3C], 0x05: [0x7B], 0x04: [0x80], 0x11: [0x26]}


def test_multi_multiframe_out_of_order_with_spaces():
    raw = "00C \r1: 05 7B 04 80 11 26 00 \r0: 41 0C 1A F8 0D 3C \r\r"
    d, why = multi(raw, [0x0C, 0x0D, 0x05, 0x04, 0x11])
    assert why == "" and d[0x11] == [0x26]


def test_multi_incomplete_truncated_inconsistent():
    assert multi("410C1AF8\r\r", [0x0C, 0x0D])[1].startswith("INCOMPLETA")      # só o 1º PID (clone)
    assert multi("410C1AF80D\r\r", [0x0C, 0x0D])[1] == "TRUNCADA"
    assert multi("410C1AF8053C\r\r", [0x0C, 0x0D])[1].startswith("INCONSISTENTE")  # PID trocado
    assert multi("?\r\r", [0x0C, 0x0D])[1] == "?"
    assert multi("NO DATA\r\r", [0x0C, 0x0D])[1] == "NO DATA"
    # multi-frame cortado (faltou o frame 1)
    assert multi("00C\r0:410C1AF80D3C\r\r", [0x0C, 0x0D, 0x05, 0x04, 0x11])[0] is None


def test_mode01_cmd():
    assert mode01_cmd([0x0C]) == "010C"
    assert mode01_cmd([0x0C], 1) == "010C1"
    assert mode01_cmd([0x0C, 0x0D, 0x05, 0x04, 0x11], 1) == "010C0D0504111"


# --- ELM falso ----------------------------------------------------------------

def _fake(profile="bom"):
    e = FakeELM(SimCar("normal", time_factor=1, seed=3), profile=profile)
    e.connected_protocol = True
    e.echo, e.spaces = False, False
    return e


def test_fake_suffix_returns_without_timeout_wait():
    e = _fake()
    parts = e.handle_parts("010C")
    assert len(parts) == 2 and parts[1][0].endswith(">") and parts[1][1] > 0.03   # espera do '>'
    parts = e.handle_parts("010C1")
    assert len(parts) == 1 and parts[0][0].startswith("410C")                       # sem espera
    assert e.handle_parts("030")[0][0].startswith("?")                              # sufixo só no 01


def test_fake_multi_single_and_multiframe():
    e = _fake()
    t, _ = e.handle("010C0D1")
    d, why = parse_mode01_multi(clean_lines(t), [0x0C, 0x0D])
    assert why == ""
    t, _ = e.handle("010C0D0504111")
    lines = clean_lines(t)
    assert lines[0] == "00C" and lines[1].startswith("0:")
    d, why = parse_mode01_multi(lines, list(FAST_CORE))
    assert why == "" and set(d) == set(FAST_CORE)


def test_fake_bad_clones():
    t, _ = _fake("clone_ruim").handle("010C0D")
    assert parse_mode01_multi(clean_lines(t), [0x0C, 0x0D])[1].startswith("INCOMPLETA")
    t, _ = _fake("clone_interrog").handle("010C0D")
    assert clean_lines(t) == ["?"]
    t, _ = _fake("sem_sufixo").handle("010C1")
    assert clean_lines(t) == ["?"]


def test_fake_timing_commands():
    e = _fake()
    assert e.handle("ATAT2")[0].strip().endswith(">") and e.adaptive == 2
    e.handle("ATST19")
    assert e.st == 0x19
    e.handle("ATSP6")
    assert not e.protocol_auto and "6" in e.handle("ATDPN")[0]
    e.handle("ATST01")                 # 4 ms: menor que a resposta da ECU
    assert "NODATA" in e.handle("010C")[0].replace(" ", "")


def test_elm_timing_split():
    """A latência medida separa escrita / até o 1º byte / espera do '>'."""
    async def go():
        ad = SimAdapter(SimCar("normal", time_factor=1, seed=2), search_delay=FAST_SEARCH)
        t = SimulatedTransport(ad)
        await t.connect()
        elm = ELM327(t)
        await elm.initialize()
        plain = await elm.query_pid(0x0C)
        fast = await elm.query_pid(0x0C, suffix=True)
        await t.disconnect()
        return plain, fast
    plain, fast = asyncio.run(go())
    assert plain.ok and fast.ok
    assert plain.timing.wait > 40          # ELM esperando outras ECUs
    assert fast.timing.wait < 10           # com sufixo: '>' junto com a resposta
    assert plain.t_resp > 0 and plain.timing.t_first <= plain.timing.t_prompt


# --- engine / poller com o simulador -----------------------------------------

async def run_engine(tmp_path, seconds, cfg=None, during=None, **kw):
    cfg = cfg or Config()
    kw.setdefault("search_delay", FAST_SEARCH)
    eng = build_sim_engine(cfg, "normal", time_factor=1, logs_dir=tmp_path, seed=5, fsync=False, **kw)
    eng.sim_car.model.plan = [Segment("lenta", 1e9)]       # parado, motor ligado (valores estáveis)
    task = asyncio.create_task(eng.run())
    t0 = time.monotonic()
    while time.monotonic() - t0 < seconds:
        await asyncio.sleep(0.1)
        if during:
            during(eng, time.monotonic() - t0)
    eng.stop()
    await asyncio.wait_for(task, 15)
    return eng


def _fast_fails(eng):
    return sum(eng.poller.stats[p].fails for p in FAST_CORE)


def test_acceptance_optimized_at_least_3x_faster(tmp_path):
    """Critério de aceite: no simulador (modelo de latência BLE + timeout do
    ELM), a rodada rápida fica ≥ 3x mais rápida com as otimizações do que no
    modo seguro, sem aumento de erros."""
    safe_cfg = Config(optimize=False)
    safe = asyncio.run(run_engine(tmp_path / "seguro", 8.0, cfg=safe_cfg))
    opt = asyncio.run(run_engine(tmp_path / "otim", 8.0))
    print(f"modo seguro {safe.poller.avg_hz:.2f} rodadas/s, otimizado {opt.poller.avg_hz:.2f} rodadas/s "
          f"({opt.poller.active.describe()})")
    assert safe.poller.active.multi == 1 and not safe.poller.active.suffix
    assert opt.poller.active.suffix and opt.poller.active.multi >= 2
    assert safe.poller.avg_hz > 0.3
    assert opt.poller.avg_hz >= 3 * safe.poller.avg_hz
    assert _fast_fails(opt) <= _fast_fails(safe)
    assert not opt.poller.disabled


def test_bad_clone_falls_back_to_one_by_one_without_losing_log(tmp_path):
    eng = asyncio.run(run_engine(tmp_path, 7.0, profile="clone_ruim"))
    p = eng.poller
    assert p.active.multi == 1          # detectou na inicialização que multi-PID não funciona
    assert p.active.suffix              # o sufixo esse clone aceita
    rows = read_rows(eng.logger.csv_path)
    ts = [num(r["t_s"]) for r in rows]
    gaps = [b - a for a, b in zip(ts, ts[1:])]
    assert max(gaps[:-1]) < 0.75        # o log de 0,5 s não falhou
    with_rpm = [r for r in rows if r["rpm"]]
    assert len(with_rpm) >= len(rows) - 8
    assert all(p.stats[pid].ok > 10 for pid in FAST_CORE)


def test_interrogation_clone_and_old_elm(tmp_path):
    eng = asyncio.run(run_engine(tmp_path / "a", 5.0, profile="clone_interrog"))
    assert eng.poller.active.multi == 1 and eng.poller.stats[0x0C].ok > 10
    eng = asyncio.run(run_engine(tmp_path / "b", 5.0, profile="sem_sufixo"))
    assert not eng.poller.active.suffix and eng.poller.stats[0x0C].ok > 5


def test_runtime_failure_disables_multi_and_keeps_reading(tmp_path):
    """Clone que passa na detecção e depois começa a responder incompleto: o
    agrupamento é desligado em tempo de execução (com motivo) e a leitura
    continua um por um, sem buraco no log."""
    eng = asyncio.run(run_engine(tmp_path, 9.0, profile="clone_instavel"))
    p = eng.poller
    assert "multi" in p.disabled and "INCOMPLETA" in p.disabled["multi"]
    assert p.active.multi == 1
    codes = [e.code for e in eng.events]
    assert "OTIMIZACAO_DESATIVADA:multi" in codes
    rows = read_rows(eng.logger.csv_path)
    assert any("OTIMIZACAO_DESATIVADA:multi" in r["evento"] for r in rows)
    ts = [num(r["t_s"]) for r in rows]
    assert max(b - a for a, b in zip(ts[:-1], ts[1:-1])) < 0.75
    # depois de desligar, os valores continuam chegando
    t_dis = next(e.t_s for e in eng.events if e.code == "OTIMIZACAO_DESATIVADA:multi")
    after = [r for r in rows if num(r["t_s"]) > t_dis + 1.0 and "FIM_VIAGEM" not in r["evento"]]
    assert after and all(r["rpm"] and r["carga_pct"] for r in after)
    summary = eng.logger.summary_path.read_text(encoding="utf-8-sig")
    assert "Otimizações desativadas" in summary


def test_long_log_timestamps_and_age_columns(tmp_path):
    eng = asyncio.run(run_engine(tmp_path, 7.0))
    lg = eng.logger
    assert lg.readings_path.name.startswith("sim_leituras_")
    readings = read_rows(lg.readings_path)
    assert list(readings[0].keys()) == list(READING_COLUMNS)
    start, end = eng.start_wall, eng.start_wall + timedelta(seconds=10)
    rpm = [r for r in readings if r["pid"] == "0C"]
    # uma linha por leitura real (sem repetir valores do tique de 0,5 s)
    assert len(rpm) == eng.poller.stats[0x0C].ok
    rows_with_rpm = [r for r in read_rows(lg.csv_path) if r["rpm"]]
    assert len(rpm) > 2 * len(rows_with_rpm)     # bem mais leituras que linhas do CSV de 0,5 s
    times = [datetime.fromisoformat(r["data_hora"]) for r in rpm]
    end = start + timedelta(seconds=12)
    assert all(start <= t <= end for t in times)
    assert times == sorted(times)
    assert all(len(r["data_hora"].split(".")[-1]) == 3 for r in rpm)       # milissegundos
    assert all(r["valor"] and int(r["latencia_ms"]) >= 0 for r in rpm)
    # CSV de 0,5 s: coluna de idade coerente com o instante real da leitura
    rows = read_rows(lg.csv_path)
    assert list(rows[0].keys()) == list(COLUMNS) and set(AGE_COLUMNS) <= set(COLUMNS)
    checked = 0
    by_time = [(datetime.fromisoformat(r["data_hora"]), r["valor"]) for r in rpm]
    for r in rows:
        if not r["rpm"]:
            assert r["idade_ms_rpm"] == ""
            continue
        age = int(r["idade_ms_rpm"])
        assert 0 <= age <= 1500
        t_read = datetime.fromisoformat(r["data_hora"]) - timedelta(milliseconds=age)
        # existe uma leitura real com esse valor nesse instante (±3 ms de arredondamento)
        assert any(abs((t - t_read).total_seconds()) <= 0.003 and v == r["rpm"] for t, v in by_time), r
        checked += 1
    assert checked >= 5


def test_format_row_age_columns():
    vals = {k: None for k in VALUE_KEYS}
    vals["rpm"] = 800.0
    row = {"t_wall": datetime(2026, 1, 1), "t_s": 0.0, "values": vals, "dtcs": [], "events": [], "note": "",
           "ages": {"rpm": 123.4, "velocidade_kmh": 50.0}}
    cells = format_row(row).rstrip("\n").split(";")
    assert cells[COLUMNS.index("idade_ms_rpm")] == "123"
    assert cells[COLUMNS.index("idade_ms_velocidade_kmh")] == ""   # sem valor = sem idade


def test_oil_key_timestamp_has_milliseconds(tmp_path):
    pressed = {}

    def press(eng, t):
        if t > 2.0 and "at" not in pressed:
            at = time.monotonic() - 0.137      # aperto 137 ms antes de o engine receber
            pressed["at"] = at
            eng.mark_oil_light("teste", at=at)
    eng = asyncio.run(run_engine(tmp_path, 3.5, during=press))
    ev = next(e for e in eng.events if e.kind == "LUZ_OLEO_MANUAL")
    expected = eng.wall_of(pressed["at"])
    assert abs((ev.t_wall - expected).total_seconds()) < 0.001
    stamp = f"{expected:%H:%M:%S}.{expected.microsecond // 1000:03d}"
    assert stamp in eng.logger.events_path.read_text(encoding="utf-8-sig")
    rows = read_rows(eng.logger.csv_path)
    marked = next(r for r in rows if "LUZ_OLEO_MANUAL" in r["evento"])
    assert f"tecla S às {stamp}" in marked["nota"]
    # a linha do CSV é a do tique seguinte; o evento guarda a hora do aperto
    assert datetime.fromisoformat(marked["data_hora"]) >= ev.t_wall
    s_rows = [r for r in read_rows(eng.logger.readings_path) if r["pid"] == "S"]
    assert len(s_rows) == 1 and s_rows[0]["data_hora"].endswith(stamp.split(":")[-1])


def test_poller_does_not_wait_for_log_tick(tmp_path):
    """Várias rodadas completas entre dois tiques de 0,5 s do log."""
    seen = {}

    def grab(eng, t):
        if t > 6.5 and "hz" not in seen:
            seen["hz"] = eng.poller.stats[0x0C].hz()
            seen["rounds"] = eng.poller.hz
    eng = asyncio.run(run_engine(tmp_path, 7.0, during=grab))
    assert eng.poller.avg_hz > 4.0
    assert seen["hz"] >= 4.0 and seen["rounds"] >= 4.0      # ≥ 2 leituras por tique de 0,5 s


# --- benchmark ----------------------------------------------------------------

def _res(key, rps, errors=0, mismatches=0, rounds=10, multi=1):
    r = ComboResult(Combo(key, key, Tuning(multi=multi)), seconds=rounds / rps if rps else 1, rounds=rounds,
                    readings=100)
    if errors:
        r.errors["NO DATA"] = errors
    r.mismatches = mismatches
    return r


def test_choose_best_fastest_with_zero_errors():
    rs = [_res("A", 1.3), _res("B", 4.0), _res("D5", 12.0, errors=3), _res("D3", 8.0),
          _res("D2", 9.0, mismatches=1)]
    assert choose_best(rs).combo.key == "D3"


def test_choose_best_when_everything_errs_a_little():
    rs = [_res("A", 1.3, errors=1), _res("B", 4.0, errors=1), _res("D5", 12.0, errors=5)]
    assert choose_best(rs).combo.key == "B"      # não erra mais que o modo seguro


def _bench(profile, seconds=1.0):
    async def go():
        car = SimCar("normal", time_factor=1, seed=4)
        car.model.plan = [Segment("lenta", 1e9)]
        ad = SimAdapter(car, profile=profile, search_delay=FAST_SEARCH)
        t = SimulatedTransport(ad)
        await t.connect()
        elm = ELM327(t)
        await elm.configure()
        await elm.probe_ecu()
        info = await elm.read_protocol_info()
        sup = await elm.supported_pids()
        lines = []
        res = await benchmark_core(elm, t, info, sup, lines.append, seconds=seconds, simulated=True,
                                   adapter_key=f"simulador:{profile}")
        await t.disconnect()
        return res
    return asyncio.run(go())


def test_benchmark_picks_fastest_clean_combo_and_reports_gain(tmp_path):
    res = _bench("bom")
    keys = [r.combo.key for r in res.results]
    assert keys[0] == "A" and "B" in keys and any(k.startswith("C") for k in keys)
    assert {"D2", "D3", "D5"} <= set(keys)
    assert res.best.clean and res.best.combo.key.startswith("D")
    assert res.gain >= 3
    assert res.tuning.source == "benchmark" and res.tuning.suffix and res.tuning.multi >= 2
    assert "Ganho: de " in res.text and " rodadas/s" in res.text
    rec = tuning_record(res)
    cfg = Config(logs_dir=str(tmp_path))
    cfg.set_tuning("simulador:bom", rec)
    path = tmp_path / "config.json"
    cfg.save(path)
    again = Config.load(path)
    t = Tuning.from_dict(again.tuning_for("simulador:bom"))
    assert t.multi == res.tuning.multi and t.suffix and t.source == "benchmark"


def test_benchmark_bad_clone_avoids_multi():
    res = _bench("clone_ruim")
    assert res.tuning is not None and res.tuning.multi == 1
    d = [r for r in res.results if r.combo.key.startswith("D")]
    assert d and all(r.n_errors > 0 for r in d)    # multi-PID detectado como incompleto


def test_saved_benchmark_is_used_on_next_connection(tmp_path):
    cfg = Config()
    cfg.set_tuning("simulador:bom", Tuning(suffix=True, multi=3, source="benchmark", protocol="6").to_dict())
    eng = asyncio.run(run_engine(tmp_path, 4.0, cfg=cfg))
    a = eng.poller.active
    assert a.source == "benchmark" and a.multi == 3 and a.suffix and a.fix_protocol
    assert len(eng.poller.groups[0]) == 3 and all(len(g) <= 3 for g in eng.poller.groups)


@pytest.mark.parametrize("profile", ["bom", "clone_ruim"])
def test_reconnect_still_works_with_optimizations(tmp_path, profile):
    """Robustez: queda do Bluetooth no meio, reconecta, volta a ler (com o
    protocolo já fixado no configure)."""
    def kill(eng, t):
        # só depois de estar lendo de verdade (o ciclo rápido já rodando)
        if eng.poller.rounds_total > 5 and eng.sim_adapter.drops == 0:
            eng.sim_adapter.drop_for(0.5)
    eng = asyncio.run(run_engine(tmp_path, 10.0, during=kill, profile=profile, backoff=(0.2, 0.5)))
    codes = [e.code for e in eng.events]
    assert "CONEXAO_PERDIDA" in codes and "CONEXAO_RESTABELECIDA" in codes
    assert eng.poller.connections >= 2
    rows = read_rows(eng.logger.csv_path)
    assert rows[-2]["rpm"]       # lendo de novo no fim


def test_ble_write_mode_choice():
    from obdmon.transport.ble import BleTransport, choose_write_mode
    assert choose_write_mode((True, False), None) is False      # padrão: sem resposta
    assert choose_write_mode((True, False), True) is True       # benchmark/fallback pediu com resposta
    assert choose_write_mode((True,), None) is True             # só existe com resposta
    assert choose_write_mode((False,), True) is False

    class Ch:
        properties = ["write", "write-without-response", "notify"]
        uuid = "0000fff2-0000-1000-8000-00805f9b34fb"
    t = BleTransport("00:11:22:33:44:55")
    t._tx = Ch()
    assert t.write_modes == (True, False)
    assert t.set_write_with_response(True) and t.write_with_response is True
    Ch.properties = ["write"]
    assert not t.set_write_with_response(False)
