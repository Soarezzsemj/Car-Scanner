"""Janela principal."""
from __future__ import annotations

import os
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QDesktopServices, QKeySequence, QShortcut
from PySide6.QtWidgets import (QFrame, QGridLayout, QHBoxLayout, QLabel, QMainWindow, QMenu, QMessageBox,
                               QPushButton, QSizePolicy, QStackedWidget, QStatusBar, QTabWidget, QVBoxLayout,
                               QWidget)

from .. import events as ev
from ..config import Config
from ..engine import (LINK_CONNECTING, LINK_LOST, LINK_OK, LINK_STOPPED, LINK_UNSTABLE, AsyncRunner, Engine,
                      build_ble_engine, build_sim_engine)
from ..events import Event, values_summary
from ..selftest import latest_report
from . import sound, theme
from .charts import LiveChart
from .diag_panel import DiagnosticsPage
from .dtc_panel import DtcPanel
from .events_panel import EventsPanel
from .gauges import ArcGauge, BarGauge
from .settings import SettingsPage
from .sim_panel import SimPanel

LINK_BADGES = {
    LINK_OK: ("● Conectado", theme.GREEN),
    LINK_UNSTABLE: ("● Instável", theme.YELLOW),
    LINK_LOST: ("● Sem conexão", theme.RED),
    LINK_CONNECTING: ("● Conectando...", theme.BLUE),
    LINK_STOPPED: ("● Desconectado", theme.GRAY),
}


MIN_W, MIN_H = 1024, 560


class TripGaugeGrid(QWidget):
    """Os 4 medidores do modo viagem: 1x4 ou 2x2, o que der o mostrador maior."""

    def __init__(self, gauges: list, parent=None) -> None:
        super().__init__(parent)
        self.gauges = gauges
        self.grid = QGridLayout(self)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(14)
        self.cols = 0
        self._arrange(4)

    def _arrange(self, cols: int) -> None:
        if cols == self.cols:
            return
        self.cols = cols
        for g in self.gauges:
            self.grid.removeWidget(g)
        for i, g in enumerate(self.gauges):
            self.grid.addWidget(g, i // cols, i % cols)
        for c in range(4):
            self.grid.setColumnStretch(c, 1 if c < cols else 0)
        for r in range(4):
            self.grid.setRowStretch(r, 1 if r < 4 // cols else 0)

    @staticmethod
    def best_cols(w: float, h: float) -> int:
        def side(cols):
            rows = 4 // cols
            cw, ch = (w - 14 * (cols - 1)) / cols, (h - 14 * (rows - 1)) / rows
            return min(cw, ch * 0.9)   # o mostrador é um pouco mais largo que alto
        return 4 if side(4) >= side(2) else 2

    def resizeEvent(self, e) -> None:
        self._arrange(self.best_cols(self.width(), self.height()))
        super().resizeEvent(e)


class Bridge(QObject):
    message = Signal(str, object)


def _num(v, d=0, unit=""):
    if v is None:
        return "--"
    return f"{v:.{d}f}".replace(".", ",") + unit


class MainWindow(QMainWindow):
    def __init__(self, cfg: Config, runner: AsyncRunner, simulated: bool, scenario: str = "normal",
                 factor: Optional[float] = None, address: str = "", autostart: bool = True,
                 profile: str = "bom") -> None:
        super().__init__()
        self.cfg = cfg
        self.runner = runner
        self.simulated = simulated
        self.profile = profile
        self.last_ages: dict = {}
        self._unsupported: set = set()
        self.scenario = scenario
        self.factor = factor
        self.cli_address = address
        self.engine: Optional[Engine] = None
        self.engine_future = None
        self.trip_start: Optional[float] = None
        self.last_values: dict = {}
        self.link = LINK_STOPPED
        self.alerts: dict = {}
        self.crit_unacked: Optional[Event] = None
        self.last_crit_beep = 0.0
        self.last_mark_ui = 0.0
        self._pending_restart: Optional[tuple] = None
        self._closing = False
        self.marks = 0
        sound.enabled = cfg.sound

        self.bridge = Bridge()
        self.bridge.message.connect(self.on_message)
        self.setWindowIcon(theme.make_icon())
        self.setWindowTitle("Monitor OBD2 – Citroën C3" + ("  —  SIMULADOR (dados FALSOS)" if simulated else ""))
        self.resize(1440, 900)
        # cabe em 1920x1080 a 150% (1280x720 lógicos) e 1366x768 a 125% (1093x614), descontando a barra de tarefas
        self.setMinimumSize(MIN_W, MIN_H)
        self._topbar_level = -1
        self._build()
        self.apply_settings()

        self._anim_last = time.monotonic()
        self.anim = QTimer(self)
        self.anim.setInterval(16)
        self.anim.timeout.connect(self._animate)
        self.anim.start()
        self.slow_timer = QTimer(self)
        self.slow_timer.setInterval(500)
        self.slow_timer.timeout.connect(self._slow_update)
        self.slow_timer.start()

        rep = latest_report(cfg.logs_path) if cfg.logs_path.exists() else None
        if rep:
            try:
                self.diag.set_report(rep.read_text(encoding="utf-8-sig"))
            except OSError:
                pass
        if autostart:
            QTimer.singleShot(200, self._autostart)

    # ------------------------------------------------------------------ UI
    def _build(self) -> None:
        root = QWidget()
        root.setObjectName("root")
        if self.simulated:
            root.setStyleSheet(f"QWidget#root {{ border: 3px solid {theme.SIM}; }}")
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(3 if self.simulated else 0, 3 if self.simulated else 0,
                                 3 if self.simulated else 0, 0)
        outer.setSpacing(0)
        self.topbar = self._build_topbar()
        outer.addWidget(self.topbar)

        self.conn_banner = QLabel()
        self.conn_banner.setWordWrap(True)
        self.conn_banner.hide()
        outer.addWidget(self.conn_banner)
        self.alert_banner = QFrame()
        ab = QHBoxLayout(self.alert_banner)
        ab.setContentsMargins(16, 8, 12, 8)
        self.alert_text = QLabel()
        self.alert_text.setWordWrap(True)
        self.alert_ok = QPushButton("OK, ciente")
        self.alert_ok.setStyleSheet("background: rgba(0,0,0,0.35); border: 1px solid rgba(255,255,255,0.5);"
                                    "color: white; font-size: 15px; padding: 8px 18px;")
        self.alert_ok.clicked.connect(self.ack_alert)
        ab.addWidget(self.alert_text, 1)
        ab.addWidget(self.alert_ok)
        self.alert_banner.hide()
        outer.addWidget(self.alert_banner)

        self.stack = QStackedWidget()
        outer.addWidget(self.stack, 1)
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.stack.addWidget(self.tabs)
        self.tabs.addTab(self._build_dashboard(), "  Painel  ")
        self.diag = DiagnosticsPage()
        self.diag.logs_dir = self.cfg.logs_path
        self.diag.selftest_requested.connect(self.run_selftest)
        self.tabs.addTab(self.diag, "  Diagnóstico do adaptador  ")
        self.settings = SettingsPage(self.cfg, adapter_key=self._adapter_key)
        self.settings.saved.connect(self.apply_settings)
        self.settings.scan_requested.connect(self._scan_from_settings)
        self.settings.benchmark_requested.connect(self.run_benchmark)
        self.tabs.addTab(self.settings, "  Configurações  ")
        self.stack.addWidget(self._build_trip_mode())
        self._set_link_badge(self.link)   # o badge do modo viagem nasce vazio

        self.status = QStatusBar()
        self.setStatusBar(self.status)
        self.file_label = QLabel("")
        self.file_label.setObjectName("muted")
        self.status.addPermanentWidget(self.file_label)

        for seq in ("S", "Shift+S"):
            sc = QShortcut(QKeySequence(seq), self)
            sc.setContext(Qt.ShortcutContext.ApplicationShortcut)
            sc.activated.connect(self.mark_oil_light)
        QShortcut(QKeySequence("F11"), self, activated=self.toggle_trip_mode,
                  context=Qt.ShortcutContext.ApplicationShortcut)
        QShortcut(QKeySequence("Escape"), self, activated=lambda: self.stack.currentIndex() == 1 and self.toggle_trip_mode())

    def _build_topbar(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("topbar")
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(16, 10, 16, 10)
        lay.setSpacing(12)
        icon = QLabel()
        icon.setPixmap(theme.make_icon().pixmap(36, 36))
        lay.addWidget(icon)
        tl = QVBoxLayout()
        tl.setSpacing(0)
        t = QLabel("Monitor OBD2")
        t.setObjectName("appTitle")
        s = QLabel("Citroën C3 · somente leitura")
        s.setObjectName("appSub")
        self.app_title, self.app_sub = t, s
        tl.addWidget(t)
        tl.addWidget(s)
        lay.addLayout(tl)
        lay.addSpacing(10)
        if self.simulated:
            sb = QLabel("SIMULADOR")
            sb.setStyleSheet(theme.badge_style(theme.SIM))
            sb.setToolTip("Os dados mostrados são FALSOS (gerados pelo simulador)")
            lay.addWidget(sb)
        self.link_badge = QLabel()
        lay.addWidget(self.link_badge)
        self._set_link_badge(LINK_STOPPED)
        lay.addStretch(1)
        self.clock = QLabel("⏱ 00:00:00")
        self.clock.setObjectName("bigClock")
        self.clock.setToolTip("Tempo desta viagem (gravação)")
        lay.addWidget(self.clock)
        self.rate = QLabel("-- Hz")
        self.rate.setObjectName("muted")
        self.rate.setToolTip("Taxa real do ciclo rápido e latência média por consulta")
        lay.addWidget(self.rate)
        self.connect_btn = QPushButton("Conectar")
        self.connect_btn.setObjectName("primary")
        self.connect_btn.setMinimumWidth(130)
        self.connect_btn.clicked.connect(self.toggle_connection)
        lay.addWidget(self.connect_btn)
        if not self.simulated:
            more = QPushButton("▾")
            more.setFixedWidth(34)
            m = QMenu(more)
            a = QAction("Procurar outro adaptador...", m)
            a.triggered.connect(self._scan_and_connect)
            m.addAction(a)
            more.setMenu(m)
            lay.addWidget(more)
        lb = QPushButton("📁 Pasta de logs")
        lb.setToolTip("Abrir a pasta dos logs")
        lb.clicked.connect(self.open_logs)
        lay.addWidget(lb)
        tb = QPushButton("⛶ Modo viagem (F11)")
        tb.setToolTip("Modo viagem em tela cheia (F11)")
        tb.clicked.connect(self.toggle_trip_mode)
        lay.addWidget(tb)
        self.logs_btn, self.trip_btn = lb, tb
        self._rate_full = "-- Hz"
        return bar

    def _make_gauges(self, big: bool = False) -> dict:
        tf = lambda v: f"{self.cfg.display_temp(v):.0f}"  # noqa: E731
        return {
            "velocidade_kmh": ArcGauge("Velocidade", "km/h", 0, 200, [(9999, theme.ACCENT)], big=big),
            "rpm": ArcGauge("RPM", "rpm", 0, 7000, [(4500, theme.GREEN), (6000, theme.YELLOW), (99999, theme.RED)],
                            big=big, fmt=lambda v: f"{v:.0f}"),
            "arrefecimento_c": ArcGauge("Arrefecimento", "°C", 40, 130, [], big=big, fmt=tf),
            "oleo_c": ArcGauge("Óleo", "°C", 40, 150, [], big=big, fmt=tf),
        }

    def _build_dashboard(self) -> QWidget:
        w = QWidget()
        lay = QHBoxLayout(w)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(14)
        left = QVBoxLayout()
        left.setSpacing(10)
        lay.addLayout(left, 1)

        self.gauges = self._make_gauges()
        row = QHBoxLayout()
        row.setSpacing(10)
        for g in self.gauges.values():
            row.addWidget(g)
        left.addLayout(row, 5)

        self.bars = {
            "carga_pct": BarGauge("Carga do motor", "%", 0, 100, [(80, theme.GREEN), (95, theme.YELLOW), (999, theme.RED)]),
            "acelerador_pct": BarGauge("Acelerador", "%", 0, 100, [(999, theme.ACCENT)]),
            "tensao_v": BarGauge("Tensão", "V", 10, 16, [], decimals=1),
            "admissao_c": BarGauge("Ar de admissão", "°C", -10, 80, [(50, theme.GREEN), (65, theme.YELLOW), (999, theme.RED)],
                                   fmt=lambda v: f"{self.cfg.display_temp(v):.0f}"),
            "ambiente_c": BarGauge("Ambiente", "°C", -10, 50, [(999, theme.ACCENT)],
                                   fmt=lambda v: f"{self.cfg.display_temp(v):.0f}"),
        }
        row = QHBoxLayout()
        row.setSpacing(10)
        for b in self.bars.values():
            row.addWidget(b)
        left.addLayout(row)

        # taxa real (Hz) e idade do dado de cada parâmetro rápido
        self.freshness = QLabel("")
        self.freshness.setObjectName("muted")
        self.freshness.setTextFormat(Qt.TextFormat.RichText)
        self.freshness.setToolTip("Leituras por segundo de cada parâmetro (últimos 5 s) e idade do último "
                                  "valor válido. Em amarelo: valor com mais de 1,5 s.")
        left.addWidget(self.freshness)

        card = QFrame()
        card.setObjectName("card")
        cl = QVBoxLayout(card)
        cl.setContentsMargins(10, 8, 10, 6)
        self.chart = LiveChart(self.cfg.chart_window_min)
        cl.addWidget(self.chart)
        left.addWidget(card, 6)

        right = QVBoxLayout()
        right.setSpacing(8)
        rw = QWidget()
        rw.setLayout(right)
        rw.setFixedWidth(390)
        self.right_col = rw
        lay.addWidget(rw)
        self.oil_btn = self._oil_button()
        right.addWidget(self.oil_btn)
        self.mark_info = QLabel("Aperte quando a luz do óleo acender (tecla S).")
        self.mark_info.setWordWrap(True)
        self.mark_info.setObjectName("muted")
        self.mark_info.setMinimumHeight(40)
        right.addWidget(self.mark_info)
        nb = QPushButton("✎ Adicionar nota ao log")
        nb.clicked.connect(self.add_note)
        right.addWidget(nb)
        self.side_tabs = QTabWidget()
        self.events_panel = EventsPanel()
        self.events_panel.event_clicked.connect(self._event_clicked)
        self.side_tabs.addTab(self.events_panel, "Eventos")
        self.dtc_panel = DtcPanel()
        self.side_tabs.addTab(self.dtc_panel, "Códigos (DTC)")
        self.sim_panel = None
        if self.simulated:
            self.sim_panel = SimPanel(lambda fn: self.runner.call(fn))
            self.sim_panel.restart_requested.connect(self.restart_sim)
            self.side_tabs.addTab(self.sim_panel, "Simulador")
        right.addWidget(self.side_tabs, 1)
        return w

    def _oil_button(self, big: bool = False) -> QPushButton:
        b = QPushButton("🛢  LUZ DO ÓLEO ACENDEU\n(tecla S)")
        b.setObjectName("oilButton")
        b.setMinimumHeight(170 if big else 110)
        b.setProperty("max_px", 34 if big else 22)   # a fonte real é ajustada à largura (_fit_oil_button)
        b.setStyleSheet(f"font-size: {b.property('max_px')}px;")
        b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        b.clicked.connect(self.mark_oil_light)
        return b

    def _build_trip_mode(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(18, 14, 18, 14)
        top = QHBoxLayout()
        self.trip_badge = QLabel()
        top.addWidget(self.trip_badge)
        if self.simulated:
            sb = QLabel("SIMULADOR")
            sb.setStyleSheet(theme.badge_style(theme.SIM))
            top.addWidget(sb)
        self.trip_alert = QLabel("")
        self.trip_alert.setStyleSheet(f"color:{theme.RED}; font-size: 20px; font-weight: 800;")
        top.addWidget(self.trip_alert)
        top.addStretch(1)   # o espaço livre fica aqui, não esticando os badges
        self.trip_alert.hide()  # a faixa de alerta global (com o botão OK) já aparece no modo viagem
        self.trip_clock = QLabel("00:00:00")
        self.trip_clock.setObjectName("bigClock")
        top.addWidget(self.trip_clock)
        hint = QLabel("   F11 / Esc para sair")
        hint.setObjectName("muted")
        top.addWidget(hint)
        lay.addLayout(top)
        self.trip_gauges = self._make_gauges(big=True)
        self.trip_grid = TripGaugeGrid(list(self.trip_gauges.values()))
        lay.addWidget(self.trip_grid, 1)
        self.trip_oil = self._oil_button(big=True)
        lay.addWidget(self.trip_oil)
        self.trip_mark = QLabel("")
        self.trip_mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.trip_mark.setStyleSheet(f"color:{theme.GREEN}; font-size: 18px; font-weight: 700;")
        lay.addWidget(self.trip_mark)
        return w

    # ------------------------------------------------------------- settings
    def apply_settings(self) -> None:
        c = self.cfg
        sym = c.temp_symbol
        for gs in (self.gauges, self.trip_gauges):
            gs["arrefecimento_c"].bands = [(60, theme.BLUE), (c.coolant_warn, theme.GREEN),
                                           (c.coolant_crit, theme.YELLOW), (999, theme.RED)]
            gs["oleo_c"].bands = [(70, theme.BLUE), (c.oil_warn, theme.GREEN), (c.oil_crit, theme.YELLOW),
                                  (999, theme.RED)]
            for k in ("arrefecimento_c", "oleo_c"):
                gs[k].unit = sym
                gs[k].update()
        self.bars["tensao_v"].bands = [(c.voltage_low, theme.RED), (12.9, theme.YELLOW), (15.0, theme.GREEN),
                                       (99, theme.RED)]
        for k in ("admissao_c", "ambiente_c"):
            self.bars[k].unit = sym
            self.bars[k].update()
        self.chart.set_thresholds(c.coolant_warn, c.coolant_crit)
        self.chart.set_window(c.chart_window_min)
        sound.enabled = c.sound
        self.diag.logs_dir = c.logs_path

    # -------------------------------------------------------------- engine
    def _listener(self, kind, payload) -> None:
        self.bridge.message.emit(kind, payload)

    def _autostart(self) -> None:
        if self.simulated:
            self.start_engine()
        elif self.cli_address or self.cfg.preferred_address or self.cfg.last_address:
            self.start_engine()
        else:
            self._scan_and_connect()

    def _address(self) -> str:
        return self.cli_address or self.cfg.preferred_address or self.cfg.last_address

    def _adapter_key(self) -> str:
        """Chave das otimizações salvas no config.json (endereço do adaptador)."""
        return f"simulador:{self.profile}" if self.simulated else self._address()

    def start_engine(self, scenario: Optional[str] = None, factor: Optional[float] = None) -> None:
        if self.engine is not None and self.engine.running:
            return
        if self.simulated:
            scenario = scenario or self.scenario
            factor = factor if factor is not None else self.factor
            self.scenario, self.factor = scenario, factor
            self.engine = build_sim_engine(self.cfg, scenario, time_factor=factor, listener=self._listener,
                                           profile=self.profile)
            self.sim_panel.attach(self.engine.sim_car, self.engine.sim_adapter)
        else:
            addr = self._address()
            if not addr:
                self._scan_and_connect()
                return
            self.engine = build_ble_engine(self.cfg, addr, listener=self._listener)
        self.engine.running = True
        self.engine_future = self.runner.submit(self.engine.run())
        self.connect_btn.setText("Desconectar")
        self.connect_btn.setObjectName("danger")
        self.connect_btn.setStyle(self.connect_btn.style())

    def stop_engine(self) -> None:
        if self.engine is not None:
            self.connect_btn.setEnabled(False)
            self.connect_btn.setText("Encerrando...")
            self.runner.call(self.engine.stop)

    def toggle_connection(self) -> None:
        if self.engine is not None and self.engine.running:
            self.stop_engine()
        else:
            self.start_engine()

    def restart_sim(self, scenario: str, factor: float) -> None:
        if self.engine is not None and self.engine.running:
            self._pending_restart = (scenario, factor)
            self.stop_engine()
        else:
            self.start_engine(scenario, factor)

    def _scan_and_connect(self) -> None:
        from .scan_dialog import ScanDialog
        dlg = ScanDialog(self.runner, self)
        if dlg.exec() and dlg.selected is not None:
            self.cfg.last_address = dlg.selected.address
            self.cfg.last_name = dlg.selected.name
            self.cfg.save()
            self.cli_address = dlg.selected.address
            self.settings.load()
            if self.engine is not None and self.engine.running:
                self._pending_restart = ("ble", 0)
                self.stop_engine()
            else:
                self.start_engine()

    def _scan_from_settings(self) -> None:
        from .scan_dialog import ScanDialog
        dlg = ScanDialog(self.runner, self)
        if dlg.exec() and dlg.selected is not None:
            self.settings.addr.setText(dlg.selected.address)
            self.cfg.last_name = dlg.selected.name

    # ------------------------------------------------------------ mensagens
    def on_message(self, kind: str, p) -> None:
        h = getattr(self, f"_on_{kind}", None)
        if h is not None:
            h(p)

    def _on_trip(self, p) -> None:
        self.trip_start = time.monotonic()
        for g in list(self.gauges.values()) + list(self.trip_gauges.values()) + list(self.bars.values()):
            g.reset_trip()
            g.set_supported(True)
        self.chart.clear()
        self.events_panel.clear()
        self.dtc_panel.set_table({})
        self.crit_unacked = None
        self.alerts = {}
        self.marks = 0
        self._update_alert_banner()
        self.file_label.setText(f"Gravando: {Path(p['csv']).name}")
        self.status.showMessage(f"Nova viagem – log em {p['csv']}", 8000)

    def _on_live(self, p: dict) -> None:
        self._set_values(p["values"], p.get("ages"))

    def _set_values(self, values: dict, ages: Optional[dict] = None) -> None:
        from ..engine import STALE_S
        self.last_values = values
        ages = ages or {}
        self.last_ages = ages

        def stale(k):
            a = ages.get(k)
            return a is not None and a > STALE_S
        for k, g in self.gauges.items():
            g.set_value(values.get(k))
            g.set_stale(stale(k))
            self.trip_gauges[k].set_value(values.get(k))
            self.trip_gauges[k].set_stale(stale(k))
        for k, b in self.bars.items():
            b.set_value(values.get(k))
            b.set_stale(stale(k))

    def _update_freshness(self, hz: dict, ages: dict) -> None:
        from ..engine import STALE_S
        from ..pids import BY_KEY, FAST_CORE_KEYS
        parts = []
        for k in FAST_CORE_KEYS + ("admissao_c",):
            if k not in BY_KEY or k in self._unsupported:
                continue
            a = ages.get(k)
            col = theme.YELLOW if (a is None or a > STALE_S) else theme.MUTED
            age_txt = "--" if a is None else f"{a:.1f} s".replace(".", ",")
            parts.append(f"<span style='color:{col}'>{BY_KEY[k].name.replace('Temp. ', '')} "
                         f"<b>{_num(hz.get(k), 1)} Hz</b> · {age_txt}</span>")
        self.freshness.setText("  |  ".join(parts))

    def _on_tick(self, p) -> None:
        row = p["row"]
        self._unsupported = set(p["unsupported"])
        self._set_values(p.get("display", row["values"]), p.get("ages"))
        self._update_freshness(p.get("hz_by_key", {}), p.get("ages", {}))
        self.chart.add_row(row["t_s"], row["values"])
        self.chart.tick()
        unsupported = set(p["unsupported"])
        for k in self.gauges:
            self.gauges[k].set_supported(k not in unsupported)
            self.trip_gauges[k].set_supported(k not in unsupported)
        for k in self.bars:
            self.bars[k].set_supported(k not in unsupported)
        self.alerts = p["alerts"]
        for e in p["events"]:
            self._handle_event(e)
        self._update_link(p)
        self._update_alert_banner()
        hz = p["hz"] or 0
        lat = p["latency_ms"]
        self._rate_full = f"{_num(hz, 1)} rodadas/s · {_num(lat, 0)} ms/consulta"
        self.rate.setText(self._rate_full if self._topbar_level < 1 else f"{_num(hz, 1)} Hz")
        if self.trip_start is not None:
            el = int(row["t_s"])
            txt = f"{el // 3600:02d}:{el % 3600 // 60:02d}:{el % 60:02d}"
            self.clock.setText("⏱ " + txt)
            self.trip_clock.setText(txt)

    def _handle_event(self, e: Event) -> None:
        tip = f"{e.t_wall:%H:%M:%S} – {e.label}" + (f" ({e.detail})" if e.detail else "") + "\n" + \
            values_summary(e.values).replace(" | ", "\n")
        if e.kind != ev.INICIO_VIAGEM:
            self.events_panel.add(e)
        if e.kind in (ev.INICIO_VIAGEM, ev.FIM_VIAGEM, ev.PID_NAO_SUPORTADO, ev.DTC_APAGADO,
                      ev.OTIMIZACAO_DESATIVADA):
            return  # só na lista, sem marcador no gráfico
        self.chart.add_event(e, tip)
        if e.kind == ev.CONEXAO_PERDIDA:
            sound.beep("lost")
        elif e.kind == ev.CONEXAO_RESTABELECIDA:
            sound.beep("restored")
        elif e.kind == ev.DTC_NOVO:
            sound.beep("alert")
            self.status.showMessage(f"Novo código de falha: {e.label}", 15000)
            self.side_tabs.setCurrentWidget(self.dtc_panel)
        elif e.kind.startswith("ALERTA_"):
            if e.critical:
                self.crit_unacked = e
                self.last_crit_beep = time.monotonic()
                sound.beep("critical")
            else:
                sound.beep("alert")

    def _set_link_badge(self, link: str) -> None:
        text, color = LINK_BADGES[link]
        self.link_badge.setText(text)
        self.link_badge.setStyleSheet(theme.badge_style(color))
        if hasattr(self, "trip_badge"):
            self.trip_badge.setText(text)
            self.trip_badge.setStyleSheet(theme.badge_style(color))

    def _update_link(self, p) -> None:
        link = p["link"]
        if link != self.link:
            self.link = link
            self._set_link_badge(link)
        style = "padding: 10px 18px; font-size: 17px; font-weight: 800; color: white;"
        if link == LINK_LOST:
            lost = p.get("lost_for")
            secs = f"há {lost:.0f} s" if lost is not None else ""
            self.conn_banner.setText(f"⚠  SEM CONEXÃO COM O CARRO {secs} — tentando reconectar sozinho "
                                     f"(tentativa {p.get('attempt', 0)}). O log continua sendo gravado.")
            self.conn_banner.setStyleSheet(f"background: #b3261e; {style}")
            self.conn_banner.show()
        elif link == LINK_UNSTABLE:
            if p["status"] == "aguardando_ecu":
                msg = "Adaptador conectado, mas o carro não responde (ignição desligada?). Tentando de novo..."
            else:
                msg = f"Conexão instável: sem dados válidos há {_num(p.get('age'), 0)} s"
            self.conn_banner.setText("⚠  " + msg)
            self.conn_banner.setStyleSheet(f"background: #8a6a00; {style}")
            self.conn_banner.show()
        elif link == LINK_CONNECTING:
            self.conn_banner.setText("…  " + (p.get("status_detail") or "Conectando..."))
            self.conn_banner.setStyleSheet(f"background: #1d4f8a; {style}")
            self.conn_banner.show()
        else:
            self.conn_banner.hide()

    def _update_alert_banner(self) -> None:
        crit = self.crit_unacked
        active = self.alerts
        v = self.last_values
        if crit is not None:
            what = {"ALERTA_ARREFECIMENTO_CRITICO": f"ARREFECIMENTO CRÍTICO: {_num(v.get('arrefecimento_c'))} °C",
                    "ALERTA_OLEO_QUENTE": f"ÓLEO MUITO QUENTE: {_num(v.get('oleo_c'))} °C"}.get(crit.kind, crit.label)
            self.alert_text.setText(f"🚨  {what} (desde {crit.t_wall:%H:%M:%S}) — pare em local seguro e "
                                    f"desligue o motor.")
            self.alert_banner.setStyleSheet("QFrame { background: #c62828; } QLabel { color: white; font-size: 19px;"
                                            " font-weight: 800; }")
            self.alert_ok.show()
            self.alert_banner.show()
            self.trip_alert.setText("🚨 " + what)
            return
        warn = []
        crit_txt = " CRÍTICO (alerta reconhecido)"
        if "arrefecimento" in active:
            lvl = crit_txt if active["arrefecimento"] == "critico" else " alto"
            warn.append(f"Arrefecimento{lvl}: {_num(v.get('arrefecimento_c'))} °C")
        if "oleo" in active:
            lvl = crit_txt if active["oleo"] == "critico" else " quente"
            warn.append(f"Óleo{lvl}: {_num(v.get('oleo_c'))} °C")
        if "tensao" in active:
            warn.append(f"Tensão baixa: {_num(v.get('tensao_v'), 1)} V")
        if warn:
            self.alert_text.setText("⚠  " + "   •   ".join(warn))
            self.alert_banner.setStyleSheet("QFrame { background: #a77b00; } QLabel { color: white; font-size: 17px;"
                                            " font-weight: 700; }")
            self.alert_ok.hide()
            self.alert_banner.show()
            self.trip_alert.setText("⚠ " + warn[0].replace(" (alerta reconhecido)", ""))
        else:
            self.alert_banner.hide()
            self.trip_alert.setText("")

    def ack_alert(self) -> None:
        self.crit_unacked = None
        self._update_alert_banner()

    def _on_dtc(self, table) -> None:
        self.dtc_panel.set_table(table)

    def _on_diag(self, d) -> None:
        self.diag.update_diag(d)

    def _on_status(self, p) -> None:
        st, detail = p
        if detail:
            self.status.showMessage(detail, 10000)

    def _on_notice(self, msg) -> None:
        """Aviso do poller (ex.: otimização desativada por dado errado)."""
        self.status.showMessage("⚠ " + msg, 30000)
        self.diag.add_notice(msg)

    def _on_log_error(self, msg) -> None:
        self.status.showMessage("ERRO NO LOG: " + msg, 30000)
        self.conn_banner.setText("⚠  ERRO AO GRAVAR O LOG: " + msg + "  (disco cheio?)")
        self.conn_banner.setStyleSheet("background: #b3261e; padding: 10px 18px; font-size: 16px; font-weight: 800;")
        self.conn_banner.show()

    def _on_stopped(self, p) -> None:
        self.connect_btn.setEnabled(True)
        self.connect_btn.setText("Conectar")
        self.connect_btn.setObjectName("primary")
        self.connect_btn.setStyle(self.connect_btn.style())
        self._set_link_badge(LINK_STOPPED)
        self.link = LINK_STOPPED
        self.conn_banner.hide()
        self.file_label.setText(f"Viagem encerrada – resumo: {Path(p['summary']).name}")
        if self._pending_restart and not self._closing:
            scen, factor = self._pending_restart
            self._pending_restart = None
            if scen == "ble":
                self.start_engine()
            else:
                self.start_engine(scen, factor)

    def _on_selftest_progress(self, line) -> None:
        self.diag.report.appendPlainText(line)

    def _on_selftest_done(self, text) -> None:
        self.diag.set_running(False)
        self.diag.set_report(text or "Autoteste não pôde ser executado.")

    def _on_benchmark_done(self, text) -> None:
        self.diag.set_running(False)
        self.settings.set_benchmark_running(False)
        self.diag.set_report(text or "Benchmark não pôde ser executado.")
        self.settings.load()

    # --------------------------------------------------------------- ações
    def mark_oil_light(self) -> None:
        # instante EXATO do aperto (monotonic + hora), antes de qualquer outra coisa
        at = time.monotonic()
        at_wall = datetime.now()
        if at - self.last_mark_ui < 1.0:
            return
        self.last_mark_ui = at
        v = self.last_values
        if self.engine is None or not self.engine.running:
            self.mark_info.setText("⚠ Não está gravando: conecte ao carro primeiro.")
            self.mark_info.setStyleSheet(f"color:{theme.YELLOW}; font-weight: 700;")
            return
        self.runner.call(self.engine.mark_oil_light, "", at)
        self.marks += 1
        sound.beep("mark")
        msg = (f"✔ Marcado às {at_wall:%H:%M:%S}.{at_wall.microsecond // 1000:03d} — "
               f"arrefecimento {_num(v.get('arrefecimento_c'))} °C · "
               f"RPM {_num(v.get('rpm'))} · {_num(v.get('velocidade_kmh'))} km/h  (marcação nº {self.marks})")
        for lbl in (self.mark_info, self.trip_mark):
            lbl.setText(msg)
            lbl.setStyleSheet(f"color:{theme.GREEN}; font-weight: 700; font-size: {18 if lbl is self.trip_mark else 14}px;")
        for b in (self.oil_btn, self.trip_oil):
            b.setText("✔  MARCADO!\n60 s antes + 30 s depois")
            self._fit_oil_button(b)
        QTimer.singleShot(2500, self._reset_oil_buttons)

    def _reset_oil_buttons(self) -> None:
        for b in (self.oil_btn, self.trip_oil):
            b.setText("🛢  LUZ DO ÓLEO ACENDEU\n(tecla S)")
            self._fit_oil_button(b)

    def add_note(self) -> None:
        from PySide6.QtWidgets import QInputDialog
        if self.engine is None or not self.engine.running:
            return
        text, ok = QInputDialog.getText(self, "Nota no log", "Texto (vai na coluna \"nota\" do CSV):")
        if ok and text.strip():
            self.runner.call(self.engine.add_note, text.strip())
            self.status.showMessage(f"Nota gravada: {text.strip()}", 6000)

    def open_logs(self) -> None:
        p = self.cfg.logs_path
        p.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            os.startfile(str(p))  # noqa: S606
        else:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(p)))

    def toggle_trip_mode(self) -> None:
        if self.stack.currentIndex() == 0:
            self.stack.setCurrentIndex(1)
            self.statusBar().hide()
            self.topbar.hide()
            self.showFullScreen()
        else:
            self.stack.setCurrentIndex(0)
            self.statusBar().show()
            self.topbar.show()
            self.showMaximized()

    def _event_clicked(self, t_s: float) -> None:
        self.chart.center_on(t_s)

    def run_selftest(self) -> None:
        if self.engine is None or not self.engine.running or self.link not in (LINK_OK, LINK_UNSTABLE):
            QMessageBox.information(self, "Autoteste", "Conecte ao carro primeiro (motor ligado, carro parado).")
            return
        self.diag.set_running(True)
        self.diag.set_report("")
        bridge = self.bridge
        fut = self.runner.submit(self.engine.run_selftest(lambda s: bridge.message.emit("selftest_progress", s)))

        def done(f):
            try:
                bridge.message.emit("selftest_done", f.result())
            except Exception as e:
                bridge.message.emit("selftest_done", f"Erro no autoteste: {e!r}")
        fut.add_done_callback(done)

    def run_benchmark(self, seconds: Optional[float] = None) -> None:
        """Refaz o benchmark de velocidade (Configurações → Refazer benchmark)."""
        if self.engine is None or not self.engine.running or self.link not in (LINK_OK, LINK_UNSTABLE):
            QMessageBox.information(self, "Benchmark", "Conecte ao carro primeiro (carro PARADO, motor ligado).")
            return
        self.diag.set_running(True)
        self.settings.set_benchmark_running(True)
        self.diag.set_report("BENCHMARK DE VELOCIDADE (carro parado, motor ligado) – leva ~2 a 3 minutos\n")
        self.tabs.setCurrentWidget(self.diag)
        bridge = self.bridge
        fut = self.runner.submit(self.engine.run_benchmark(
            lambda s: bridge.message.emit("selftest_progress", s), seconds))

        def done(f):
            try:
                bridge.message.emit("benchmark_done", f.result())
            except Exception as e:
                bridge.message.emit("benchmark_done", f"Erro no benchmark: {e!r}")
        fut.add_done_callback(done)

    # ------------------------------------------------------------- timers
    def _animate(self) -> None:
        now = time.monotonic()
        dt = now - self._anim_last
        self._anim_last = now
        visible = self.trip_gauges if self.stack.currentIndex() == 1 else {**self.gauges, **self.bars}
        for g in visible.values():
            g.step(dt)

    def _slow_update(self) -> None:
        if self.sim_panel is not None and self.side_tabs.currentWidget() is self.sim_panel:
            self.sim_panel.refresh()
        if self.crit_unacked is not None:
            still = any(lvl == "critico" for lvl in self.alerts.values())
            if still and time.monotonic() - self.last_crit_beep >= 5.0:
                self.last_crit_beep = time.monotonic()
                sound.beep("critical")

    # ---------------------------------------------------------------- saída
    def shutdown(self, timeout: float = 10.0) -> None:
        self._closing = True
        if self.engine is not None and self.engine.running:
            self.runner.call(self.engine.stop)
            if self.engine_future is not None:
                try:
                    self.engine_future.result(timeout)
                except Exception:
                    pass
        self.cfg.save()

    # ------------------------------------------------------------ tamanho
    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        # coluna da direita: 27% da largura, entre 300 e 390 px
        self.right_col.setFixedWidth(int(max(300, min(390, self.width() * 0.27))))
        self._fit_topbar()
        for b in (self.oil_btn, self.trip_oil):
            self._fit_oil_button(b)

    def _fit_oil_button(self, b: QPushButton) -> None:
        """Maior fonte (até o máximo do botão) em que cada linha do texto cabe sem cortar."""
        from PySide6.QtGui import QFontMetricsF
        avail = b.width() - 2 * 16 - 8   # padding do estilo (14) + borda (2), com folga
        if avail <= 0:
            return
        lines = b.text().split("\n")
        px = int(b.property("max_px") or 22)
        while px > 12:
            if max(QFontMetricsF(theme.font(px, 800)).horizontalAdvance(t) for t in lines) <= avail:
                break
            px -= 1
        b.setStyleSheet(f"font-size: {px}px;")

    def _apply_topbar_level(self, level: int) -> None:
        self._topbar_level = level
        self.app_sub.setVisible(level < 1)
        self.logs_btn.setText("📁 Pasta de logs" if level < 1 else ("📁 Logs" if level < 3 else "📁"))
        self.trip_btn.setText("⛶ Modo viagem (F11)" if level < 1 else ("⛶ Viagem" if level < 3 else "⛶"))
        self.rate.setText(self._rate_full if level < 1 else self._rate_full.split(" · ")[0])
        self.rate.setVisible(level < 2)
        self.app_title.setVisible(level < 3)

    def _fit_topbar(self) -> None:
        """Escolhe a versão mais completa da barra do topo que cabe sem cortar nada."""
        # largura da JANELA (no resizeEvent a barra ainda tem a largura antiga); desconta a borda roxa
        m = self.centralWidget().layout().contentsMargins()
        avail = self.width() - m.left() - m.right()
        for level in range(4):
            self._apply_topbar_level(level)
            if self.topbar_width_needed() <= avail:
                return

    def topbar_width_needed(self) -> int:
        """Largura que a barra do topo precisa. invalidate(): ao mostrar/ocultar itens o layout
        só recalcula num evento posterior, e o sizeHint() viria do cache (valor antigo)."""
        from PySide6.QtWidgets import QLayout
        lay = self.topbar.layout()
        # invalida também os sublayouts (ex.: título + subtítulo): o invalidate() do layout
        # principal não limpa o cache deles, e a medida dependeria da ordem das mudanças
        for sub in lay.findChildren(QLayout):
            sub.invalidate()
        lay.invalidate()
        return lay.sizeHint().width()

    def closeEvent(self, e) -> None:
        self.shutdown()
        super().closeEvent(e)


__all__ = ["MainWindow", "QSizePolicy"]
