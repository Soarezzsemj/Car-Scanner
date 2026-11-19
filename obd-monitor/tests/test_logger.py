"""Logger: CSV por viagem, flush a cada linha, instantâneos 60 s antes + 30 s depois."""
import csv
import time
from datetime import datetime, timedelta

from obdmon.events import Event
from obdmon.logger import COLUMNS, TripLogger, format_row
from obdmon.pids import VALUE_KEYS


def make_row(t0: datetime, t_s: float, coolant=90, events=()):
    vals = {k: None for k in VALUE_KEYS}
    vals.update(rpm=800 + t_s, velocidade_kmh=0, arrefecimento_c=coolant, carga_pct=20.0, tensao_v=14.1)
    return {"t_wall": t0 + timedelta(seconds=t_s), "t_s": t_s, "values": vals, "dtcs": [],
            "events": list(events), "note": ""}


def read_csv(p):
    with open(p, encoding="utf-8-sig", newline="") as f:
        return list(csv.reader(f, delimiter=";"))


def test_format_row_excel_br():
    t0 = datetime(2026, 1, 2, 3, 4, 5, 123000)
    r = make_row(t0, 1.5)
    line = format_row(r)
    cells = line.rstrip("\n").split(";")
    assert len(cells) == len(COLUMNS)
    assert cells[0] == "2026-01-02T03:04:06.623"
    assert cells[1] == "1,5"
    assert cells[COLUMNS.index("tensao_v")] == "14,10"
    assert cells[COLUMNS.index("oleo_c")] == ""


def test_flush_each_line_and_new_file_per_trip(tmp_path):
    t0 = datetime(2026, 1, 1, 10, 0, 0)
    lg = TripLogger(tmp_path, start=t0)
    lg.open()
    for i in range(5):
        lg.log_tick(make_row(t0, i * 0.5), [])
    lg.flush_wait()
    # arquivo ainda aberto: as 5 linhas já estão no disco
    rows = read_csv(lg.csv_path)
    assert rows[0] == list(COLUMNS)
    assert len(rows) == 6
    lg.close()
    lg2 = TripLogger(tmp_path, start=t0)  # mesma hora -> nome diferente, não sobrescreve
    lg2.open()
    lg2.close()
    assert lg2.csv_path != lg.csv_path
    assert lg.csv_path.exists() and lg2.csv_path.exists()
    assert lg.summary_path.exists()


def test_sim_prefix(tmp_path):
    lg = TripLogger(tmp_path, simulated=True)
    lg.open()
    lg.close()
    assert lg.csv_path.name.startswith("sim_obd_")
    assert lg.events_path.name.startswith("sim_eventos_")
    assert lg.summary_path.name.startswith("sim_resumo_")
    assert "SIMULADOR" in lg.summary_path.read_text(encoding="utf-8-sig")


def test_snapshot_60s_before_30s_after(tmp_path):
    t0 = datetime(2026, 1, 1, 10, 0, 0)
    lg = TripLogger(tmp_path, start=t0)
    lg.open()
    event_t = 100.0
    for i in range(int(200 / 0.5) + 1):
        t = i * 0.5
        evs = []
        if t == event_t:
            evs = [Event("LUZ_OLEO_MANUAL", t0 + timedelta(seconds=t), t, values={"arrefecimento_c": 104})]
        lg.log_tick(make_row(t0, t, events=[e.code for e in evs]), evs)
    lg.close()
    assert len(lg.snapshot_paths) == 1
    snap = lg.snapshot_paths[0]
    assert snap.parent.name == "eventos"
    assert snap.name.startswith("evento_2026-01-01_10-01-40_LUZ_OLEO_MANUAL")
    rows = read_csv(snap)[1:]
    ts = [float(r[1].replace(",", ".")) for r in rows]
    assert min(ts) == 40.0          # 60 s antes
    assert max(ts) == 130.0         # 30 s depois
    assert len(ts) == len(set(ts)) == 181
    assert any("LUZ_OLEO_MANUAL" in r[COLUMNS.index("evento")] for r in rows)
    # log de texto legível
    txt = lg.events_path.read_text(encoding="utf-8-sig")
    assert "10:01:40  LUZ DO ÓLEO ACENDEU | arrefecimento 104 °C" in txt


def test_non_critical_alert_has_no_snapshot(tmp_path):
    t0 = datetime.now()
    lg = TripLogger(tmp_path, start=t0)
    lg.open()
    lg.log_tick(make_row(t0, 0), [Event("ALERTA_ARREFECIMENTO_ALTO", t0, 0)])
    lg.log_tick(make_row(t0, 0.5), [Event("ALERTA_ARREFECIMENTO_CRITICO", t0, 0.5, critical=True)])
    lg.log_tick(make_row(t0, 1.0), [Event("DTC_NOVO:P0524", t0, 1.0)])
    lg.close()
    names = [p.name for p in lg.snapshot_paths]
    assert len(names) == 2
    assert any("ARREFECIMENTO_CRITICO" in n for n in names)
    assert any("DTC_NOVO_P0524" in n for n in names)


def test_summary_min_max(tmp_path):
    t0 = datetime.now()
    lg = TripLogger(tmp_path, start=t0)
    lg.open()
    for i, c in enumerate([80, 95, 117, 101]):
        lg.log_tick(make_row(t0, i * 0.5, coolant=c), [])
    lg.close()
    s = lg.summary_path.read_text(encoding="utf-8-sig")
    line = next(ln for ln in s.splitlines() if "arrefecimento" in ln)
    assert "80" in line and "117" in line


def test_writer_does_not_block_caller(tmp_path):
    lg = TripLogger(tmp_path)
    lg.open()
    t0 = time.perf_counter()
    for i in range(2000):
        lg.log_tick(make_row(datetime.now(), i * 0.5), [])
    assert time.perf_counter() - t0 < 0.5   # só enfileira
    lg.close(timeout=60)
    assert len(read_csv(lg.csv_path)) == 2001
