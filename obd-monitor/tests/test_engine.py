"""Testes de ponta a ponta com o simulador: cenários, temporização do log e
reconexão. Rodam em tempo real (com o carro acelerado pelo fator de tempo)."""
import asyncio
import csv
import time

import pytest

from obdmon.config import Config
from obdmon.engine import build_sim_engine
from obdmon.sim.scenarios import SCENARIOS, LinkFaults

from .conftest import FAST_LAT, FAST_SEARCH


def read_rows(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f, delimiter=";"))


async def run_engine(tmp_path, scenario, factor, max_s, until=None, during=None, **kw):
    """Roda o engine até ``until(engine)`` ficar verdadeiro ou ``max_s`` segundos."""
    cfg = Config()
    kw.setdefault("latency", FAST_LAT)
    kw.setdefault("search_delay", FAST_SEARCH)
    eng = build_sim_engine(cfg, scenario, time_factor=factor, logs_dir=tmp_path, seed=7,
                           fsync=False, **kw)
    task = asyncio.create_task(eng.run())
    t0 = time.monotonic()
    while time.monotonic() - t0 < max_s:
        await asyncio.sleep(0.2)
        if during:
            during(eng, time.monotonic() - t0)
        if until and until(eng):
            await asyncio.sleep(0.6)
            break
    eng.stop()
    await asyncio.wait_for(task, 15)
    return eng


def codes(eng):
    return [e.code for e in eng.events]


def has(*wanted):
    return lambda eng: all(any(c == w or c.startswith(w) for c in codes(eng)) for w in wanted)


def test_all_scenarios_registered():
    assert set(SCENARIOS) == {"normal", "superaquecimento", "queda_pressao_oleo", "intermitente",
                              "bluetooth_instavel", "sem_oleo_pid", "ecu_dormindo", "dtc_variados"}


def test_normal_runs_and_logs_every_half_second(tmp_path):
    eng = asyncio.run(run_engine(tmp_path, "normal", 10, 6.0))
    rows = read_rows(eng.logger.csv_path)
    assert 11 <= len(rows) <= 14
    ts = [float(r["t_s"].replace(",", ".")) for r in rows]
    gaps = [b - a for a, b in zip(ts, ts[1:])]
    assert all(0.35 <= g <= 0.65 for g in gaps[:-1]), gaps   # última linha = FIM_VIAGEM
    assert rows[0]["evento"] == "INICIO_VIAGEM"
    assert "FIM_VIAGEM" in rows[-1]["evento"]
    assert any(r["rpm"] for r in rows) and any(r["arrefecimento_c"] for r in rows)
    assert eng.poller.hz > 2.0
    assert eng.logger.csv_path.name.startswith("sim_obd_")
    assert eng.logger.summary_path.exists()


def test_superaquecimento(tmp_path):
    eng = asyncio.run(run_engine(tmp_path, "superaquecimento", 80, 40.0,
                                 until=has("ALERTA_ARREFECIMENTO_CRITICO")))
    c = codes(eng)
    assert "ALERTA_ARREFECIMENTO_ALTO" in c
    assert "ALERTA_ARREFECIMENTO_CRITICO" in c
    assert c.index("ALERTA_ARREFECIMENTO_ALTO") < c.index("ALERTA_ARREFECIMENTO_CRITICO")
    snaps = [p.name for p in eng.logger.snapshot_paths]
    assert any("ARREFECIMENTO_CRITICO" in n for n in snaps)


def test_queda_pressao_oleo(tmp_path):
    eng = asyncio.run(run_engine(tmp_path, "queda_pressao_oleo", 60, 40.0,
                                 until=has("DTC_NOVO:P0524", "ALERTA_OLEO_QUENTE")))
    c = codes(eng)
    assert "DTC_NOVO:P0524" in c
    assert "DTC_NOVO:P0521" in c
    assert "ALERTA_OLEO_QUENTE" in c
    assert any("DTC_NOVO_P0524" in p.name for p in eng.logger.snapshot_paths)
    rows = read_rows(eng.logger.csv_path)
    assert any("P0524" in r["dtcs_ativos"] for r in rows)


def test_queda_pressao_oleo_codes_disappear(tmp_path):
    eng = asyncio.run(run_engine(tmp_path, "queda_pressao_oleo", 100, 40.0,
                                 until=has("DTC_APAGADO:P0524")))
    assert "DTC_APAGADO:P0524" in codes(eng)


def test_intermitente_manual_mark(tmp_path):
    def press(eng, t):
        if 2.0 < t < 2.3:
            eng.mark_oil_light("teste")
            eng.mark_oil_light("repetido")  # debounce: ignorado
    eng = asyncio.run(run_engine(tmp_path, "intermitente", 5, 4.0, during=press))
    assert codes(eng).count("LUZ_OLEO_MANUAL") == 1
    assert not any(c.startswith("DTC_NOVO") for c in codes(eng))
    rows = read_rows(eng.logger.csv_path)
    marked = [r for r in rows if "LUZ_OLEO_MANUAL" in r["evento"]]
    assert len(marked) == 1 and marked[0]["nota"].startswith("teste / tecla S às ")
    assert any("LUZ_OLEO_MANUAL" in p.name for p in eng.logger.snapshot_paths)
    log = eng.logger.events_path.read_text(encoding="utf-8-sig")
    assert "LUZ DO ÓLEO ACENDEU" in log


def test_sem_oleo_pid(tmp_path):
    eng = asyncio.run(run_engine(tmp_path, "sem_oleo_pid", 5, 5.0, until=lambda e: e.rows_written > 6))
    assert "PID_NAO_SUPORTADO:5C" in codes(eng)
    assert "oleo_c" in eng.unsupported_keys()
    assert 0x5C not in eng.poller.fast
    rows = read_rows(eng.logger.csv_path)
    assert all(r["oleo_c"] == "" for r in rows)
    # o 5C nunca é consultado quando a máscara diz que não existe
    assert eng.poller.stats[0x5C].ok == 0 and eng.poller.stats[0x5C].fails == 0


def test_ecu_dormindo(tmp_path):
    statuses = []
    eng_holder = {}

    def watch(eng, t):
        statuses.append(eng.poller.status)
        eng_holder["e"] = eng
    # 20 s simulados com fator 10 = 2 s reais
    eng = asyncio.run(run_engine(tmp_path, "ecu_dormindo", 10, 20.0, during=watch,
                                 until=lambda e: e.poller.status == "conectado" and e.rows_written > 4))
    assert "aguardando_ecu" in statuses
    assert eng.poller.status in ("conectado", "parado")
    rows = read_rows(eng.logger.csv_path)
    assert any(r["rpm"] for r in rows)
    # nada foi "perdido": a conexão nunca existiu antes da ECU acordar
    assert "CONEXAO_PERDIDA" not in codes(eng)


def test_dtc_variados(tmp_path, monkeypatch):
    import obdmon.poller
    monkeypatch.setattr(obdmon.poller, "DTC_PERIOD", 1.0)  # lê DTC a cada 1 s (padrão: 5 s)
    eng = asyncio.run(run_engine(tmp_path, "dtc_variados", 20, 40.0,
                                 until=has("DTC_NOVO:P0128", "DTC_APAGADO:P0480")))
    c = codes(eng)
    for code in ("DTC_NOVO:P0133", "DTC_NOVO:P0301", "DTC_NOVO:P0480", "DTC_NOVO:P0562", "DTC_NOVO:P0128"):
        assert code in c, code
    assert "DTC_APAGADO:P0301" in c
    rows = read_rows(eng.logger.csv_path)
    # chegou a ter 3+ códigos ao mesmo tempo (resposta multi-frame)
    assert max(len(r["dtcs_ativos"].split()) for r in rows) >= 3


def test_bluetooth_instavel_log_never_stops(tmp_path):
    # entre uma falha e outra precisa caber uma reinicializacao do ELM (~2 s: ATZ + 1 s + comandos AT);
    # com falhas a cada 2-3 s o teste as vezes nao reconectava a tempo (instabilidade do teste, nao do app)
    faults = LinkFaults(drop_every=(3.0, 5.0), drop_duration=(1.0, 2.5), stall_prob=0.25,
                        packet_loss=0.03, truncate=0.03, first_drop_after=2.0)
    eng = asyncio.run(run_engine(tmp_path, "bluetooth_instavel", 1, 60.0, faults=faults,
                                 backoff=(0.2, 0.5, 1.0),
                                 until=lambda e: codes(e).count("CONEXAO_RESTABELECIDA") >= 2))
    c = codes(eng)
    assert "CONEXAO_PERDIDA" in c
    assert "CONEXAO_RESTABELECIDA" in c
    assert c.index("CONEXAO_PERDIDA") < c.index("CONEXAO_RESTABELECIDA")
    rows = read_rows(eng.logger.csv_path)
    ts = [float(r["t_s"].replace(",", ".")) for r in rows]
    gaps = [b - a for a, b in zip(ts, ts[1:])]
    assert max(gaps[:-1]) < 0.75, max(gaps)              # o log NÃO parou
    lost = [r for r in rows if "CONEXAO_PERDIDA" in r["evento"]]
    assert lost and lost[0]["rpm"] == ""                  # linha da perda já sem valores
    assert eng.poller.connections >= 2


def test_reconnect_after_killing_transport(tmp_path):
    """Mata o transporte simulado no meio da viagem: o programa tem que voltar sozinho."""
    killed = {}

    def kill(eng, t):
        if t > 2.0 and not killed:
            killed["t"] = t
            eng.sim_adapter.drop_for(1.5)
    eng = asyncio.run(run_engine(tmp_path, "normal", 5, 20.0, during=kill, backoff=(0.3, 0.5, 1.0),
                                 until=lambda e: "CONEXAO_RESTABELECIDA" in codes(e) and e.rows_written > 14))
    c = codes(eng)
    assert c.count("CONEXAO_PERDIDA") == 1
    assert c.count("CONEXAO_RESTABELECIDA") == 1
    assert eng.poller.connections == 2
    rows = read_rows(eng.logger.csv_path)
    idx = next(i for i, r in enumerate(rows) if "CONEXAO_RESTABELECIDA" in r["evento"])
    assert any(r["rpm"] for r in rows[idx:])   # voltou a ter dados
    # redescoberta de PIDs não é refeita (já sabia), mas o ELM é reinicializado (ATZ)
    assert eng.sim_adapter.elm.received.count("ATZ") == 2
    assert eng.sim_adapter.elm.received.count("0100") == 3   # 2 probes + 1 máscara


def test_silent_stall_triggers_watchdog(tmp_path):
    """Adaptador para de responder sem desconectar: watchdog 3 s (instável) / 8 s (perdida)."""
    links = []

    start = {}

    def stall(eng, t):
        links.append((round(t, 1), eng.link_state()))
        if eng.poller.status == "conectado" and eng.rows_written > 6 and not eng.sim_adapter.stalls:
            start["t"] = t
            eng.sim_adapter.stall_for(10.0)
    eng = asyncio.run(run_engine(tmp_path, "normal", 5, 25.0, during=stall, backoff=(0.3, 0.5),
                                 until=lambda e: "CONEXAO_RESTABELECIDA" in codes(e)))
    states = [s for _, s in links]
    assert "instavel" in states
    assert "CONEXAO_PERDIDA" in codes(eng)
    assert "CONEXAO_RESTABELECIDA" in codes(eng)
    t_unstable = next(t for t, s in links if s == "instavel")
    assert t_unstable >= start["t"] + 2.5            # só fica amarelo depois de ~3 s sem dados


def test_selftest_with_simulator(tmp_path):
    async def go():
        eng = build_sim_engine(Config(), "sem_oleo_pid", time_factor=5, logs_dir=tmp_path,
                               latency=FAST_LAT, search_delay=FAST_SEARCH, seed=3, fsync=False)
        task = asyncio.create_task(eng.run())
        for _ in range(50):
            await asyncio.sleep(0.1)
            if eng.poller.status == "conectado":
                break
        text = await eng.run_selftest()
        await asyncio.sleep(1.0)
        status_after = eng.poller.status
        eng.stop()
        await task
        return text, status_after, eng
    text, status_after, eng = asyncio.run(go())
    assert "PID 5C (temperatura do óleo) NÃO suportado" in text
    assert "Ciclo rápido:" in text and "Hz" in text
    assert "SIMULADOR" in text
    assert status_after == "conectado"                  # o monitor voltou a ler
    assert "CONEXAO_PERDIDA" not in codes(eng)           # o autoteste não "derruba" a conexão
    assert list(tmp_path.glob("sim_selftest_*.txt"))


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_every_scenario_smoke(tmp_path, name):
    eng = asyncio.run(run_engine(tmp_path, name, 20, 3.0))
    assert eng.rows_written >= 5
    assert eng.logger.summary_path.exists()
