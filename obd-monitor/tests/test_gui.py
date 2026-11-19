"""Smoke test da GUI em modo simulador (QT_QPA_PLATFORM=offscreen).

Abre a janela, roda o cenário "superaquecimento" acelerado, confere que o
alerta aparece, aciona a tecla S e o botão da luz do óleo, e salva capturas
de tela em docs/img/ (usadas no README)."""
import os
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from obdmon.config import Config  # noqa: E402
from obdmon.engine import AsyncRunner  # noqa: E402
from obdmon.ui import theme  # noqa: E402
from obdmon.ui.main_window import MainWindow  # noqa: E402

IMG = Path(__file__).resolve().parents[1] / "docs" / "img"


@pytest.fixture(scope="module")
def app():
    a = QApplication.instance() or QApplication([])
    a.setStyle("Fusion")
    a.setStyleSheet(theme.STYLESHEET)
    return a


def wait_until(app, cond, timeout):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        app.processEvents()
        if cond():
            return True
        QTest.qWait(50)
    return cond()


@pytest.fixture
def window(app, tmp_path, monkeypatch):
    monkeypatch.setattr(Config, "save", lambda self, *a, **k: None)  # não mexe no config.json real
    cfg = Config(logs_dir=str(tmp_path), sound=False)
    runner = AsyncRunner()
    w = MainWindow(cfg, runner, simulated=True, scenario="superaquecimento", factor=80, autostart=False)
    w.resize(1440, 900)
    w.show()
    w.activateWindow()
    yield w
    w.shutdown()
    w.close()
    runner.shutdown()


def test_gui_superaquecimento_alert_and_oil_mark(app, window):
    w = window
    w.start_engine()
    assert wait_until(app, lambda: w.link == "ok", 20), "não conectou ao simulador"
    # alerta crítico aparece (e persiste até o OK)
    assert wait_until(app, lambda: w.crit_unacked is not None, 60), "alerta crítico não apareceu"
    assert w.alert_banner.isVisible()
    assert "CRÍTICO" in w.alert_text.text()
    IMG.mkdir(parents=True, exist_ok=True)
    QTest.qWait(1500)
    w.grab().save(str(IMG / "painel_alerta.png"))

    # tecla S (com o foco em outro widget)
    w.events_panel.tree.setFocus()
    QTest.keyClick(w.events_panel.tree, Qt.Key.Key_S)
    got_key = wait_until(app, lambda: any(e.kind == "LUZ_OLEO_MANUAL" for e in w.engine.events), 3)
    # botão (sempre)
    QTest.qWait(1100)  # passa o debounce de 1 s
    n_before = sum(e.kind == "LUZ_OLEO_MANUAL" for e in w.engine.events)
    QTest.mouseClick(w.oil_btn, Qt.MouseButton.LeftButton)
    assert wait_until(app, lambda: sum(e.kind == "LUZ_OLEO_MANUAL" for e in w.engine.events) > n_before, 3)
    assert "Marcado às" in w.mark_info.text()
    marks = sum(e.kind == "LUZ_OLEO_MANUAL" for e in w.engine.events)
    assert marks == (2 if got_key else 1)
    print(f"tecla S acionou o evento: {got_key}")
    assert got_key, "a tecla S não acionou a marcação"

    # o alerta crítico continua até o OK
    assert w.alert_ok.isVisible()
    QTest.mouseClick(w.alert_ok, Qt.MouseButton.LeftButton)
    assert w.crit_unacked is None

    # clicar num evento centraliza o gráfico
    it = w.events_panel.tree.topLevelItem(0)
    w.events_panel._clicked(it, 0)
    assert w.chart.follow is False

    # capturas das outras telas
    w.side_tabs.setCurrentWidget(w.sim_panel)
    w.sim_panel.refresh()
    QTest.qWait(300)
    w.grab().save(str(IMG / "painel_simulador.png"))
    w.chart.set_follow(True)
    w.side_tabs.setCurrentIndex(0)
    w.tabs.setCurrentWidget(w.diag)
    QTest.qWait(2500)
    w.grab().save(str(IMG / "diagnostico.png"))
    w.tabs.setCurrentWidget(w.settings)
    QTest.qWait(200)
    w.grab().save(str(IMG / "configuracoes.png"))
    w.tabs.setCurrentIndex(0)
    w.toggle_trip_mode()          # modo viagem (F11)
    assert w.stack.currentIndex() == 1 and not w.topbar.isVisible()
    QTest.qWait(800)             # tela cheia de verdade (tela emulada 1920x1080, ver conftest)
    w.grab().save(str(IMG / "modo_viagem.png"))
    w.toggle_trip_mode()
    assert w.stack.currentIndex() == 0 and w.topbar.isVisible()

    # logs gravados com prefixo sim_ e instantâneo do evento
    logs = Path(w.cfg.logs_path)
    csvs = list(logs.glob("sim_obd_*.csv"))
    assert csvs
    snaps = list((logs / "eventos").glob("sim_evento_*LUZ_OLEO_MANUAL*.csv"))
    assert snaps
    assert (IMG / "painel_alerta.png").stat().st_size > 20000


def test_gui_normal_screenshot_and_selftest(app, window):
    w = window
    w.restart_sim("normal", 20)
    assert wait_until(app, lambda: w.link == "ok", 20)
    QTest.qWait(9000)  # deixa o gráfico encher um pouco
    w.chart.refresh()
    w.grab().save(str(IMG / "painel.png"))
    w.run_selftest()
    assert wait_until(app, lambda: w.diag.run_btn.isEnabled() and "VEREDITO" in w.diag.report.toPlainText(), 60)
    w.tabs.setCurrentWidget(w.diag)
    QTest.qWait(2500)
    w.grab().save(str(IMG / "diagnostico.png"))
    assert "Ciclo rápido" in w.diag.report.toPlainText()
