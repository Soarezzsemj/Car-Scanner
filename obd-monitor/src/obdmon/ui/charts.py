"""Gráfico em tempo real (pyqtgraph): temperaturas em °C e motor normalizado,
com marcadores verticais nos eventos e cursor que mostra os valores."""
from __future__ import annotations

import math
from bisect import bisect_left
from typing import Optional

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from ..events import Event
from . import theme

pg.setConfigOptions(antialias=True, background=theme.PANEL, foreground=theme.MUTED)

MAX_POINTS = 4 * 3600 * 2   # 4 h a 2 Hz

SERIES_TEMP = [("arrefecimento_c", "Arrefecimento", theme.ORANGE), ("oleo_c", "Óleo", theme.YELLOW),
               ("admissao_c", "Admissão", "#7aa2c4")]
SERIES_ENGINE = [("rpm", "RPM (0–7000)", theme.BLUE, 7000.0), ("velocidade_kmh", "Velocidade (0–200)", theme.GREEN, 200.0),
                 ("carga_pct", "Carga (%)", "#d07cff", 100.0)]


def _mmss(t: float) -> str:
    t = max(0, int(t))
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


class TimeAxis(pg.AxisItem):
    def tickStrings(self, values, scale, spacing):
        return [_mmss(v) if v >= 0 else "" for v in values]


class EventMarker(pg.InfiniteLine):
    def __init__(self, ev: Event, color: str, tooltip: str) -> None:
        super().__init__(pos=ev.t_s, angle=90, movable=False,
                         pen=pg.mkPen(color, width=2, style=Qt.PenStyle.DashLine),
                         hoverPen=pg.mkPen(color, width=4))
        self.setToolTip(tooltip)
        self.setAcceptHoverEvents(True)
        self.ev = ev


class LiveChart(QWidget):
    follow_changed = Signal(bool)

    def __init__(self, window_min: float = 5.0, parent=None) -> None:
        super().__init__(parent)
        self.window_s = window_min * 60
        self.follow = True
        self.t: list[float] = []
        self.data: dict[str, list[float]] = {k: [] for k, *_ in SERIES_TEMP + SERIES_ENGINE}
        self.data.update(tensao_v=[], acelerador_pct=[])
        self.markers: list[EventMarker] = []
        self._markers2: list[EventMarker] = []
        self._dirty = False

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        head = QHBoxLayout()
        ttl = QLabel("GRÁFICO EM TEMPO REAL")
        ttl.setObjectName("sectionTitle")
        head.addWidget(ttl)
        head.addStretch(1)
        self.cursor_label = QLabel("")
        self.cursor_label.setObjectName("muted")
        head.addWidget(self.cursor_label)
        self.live_btn = QPushButton("● Ao vivo")
        self.live_btn.setToolTip("Voltar a acompanhar o momento atual")
        self.live_btn.clicked.connect(lambda: self.set_follow(True))
        head.addWidget(self.live_btn)
        lay.addLayout(head)

        self.glw = pg.GraphicsLayoutWidget()
        self.glw.ci.setSpacing(2)
        lay.addWidget(self.glw, 1)
        self.p_temp = self.glw.addPlot(row=0, col=0, axisItems={"bottom": TimeAxis("bottom")})
        self.p_eng = self.glw.addPlot(row=1, col=0, axisItems={"bottom": TimeAxis("bottom")})
        self.p_eng.setXLink(self.p_temp)
        for pl, lbl in ((self.p_temp, "°C"), (self.p_eng, "% da escala")):
            pl.showGrid(x=True, y=True, alpha=0.12)
            pl.setLabel("left", lbl)
            pl.setMenuEnabled(False)
            pl.setClipToView(True)
            pl.setDownsampling(auto=True, mode="peak")
            pl.getAxis("left").setWidth(48)
            for ax in ("left", "bottom"):
                pl.getAxis(ax).setTickFont(theme.font(11))
                pl.getAxis(ax).setTextPen(theme.MUTED)
            pl.getAxis("left").label.setFont(theme.font(11))
            leg = pl.addLegend(offset=(8, 4), labelTextColor=theme.TEXT, labelTextSize="9pt",
                               brush=pg.mkBrush(21, 27, 35, 200))
            leg.setColumnCount(3)
        self.p_temp.getAxis("bottom").setStyle(showValues=False)
        self.p_temp.setYRange(20, 130)
        self.p_eng.setYRange(0, 105)
        self.curves = {}
        for k, name, col in SERIES_TEMP:
            self.curves[k] = self.p_temp.plot([], [], pen=pg.mkPen(col, width=2.2), name=name, connect="finite")
        for k, name, col, _ in SERIES_ENGINE:
            self.curves[k] = self.p_eng.plot([], [], pen=pg.mkPen(col, width=1.8), name=name, connect="finite")
        self.warn_line = pg.InfiniteLine(pos=105, angle=0, pen=pg.mkPen(theme.YELLOW, width=1, style=Qt.PenStyle.DotLine))
        self.crit_line = pg.InfiniteLine(pos=112, angle=0, pen=pg.mkPen(theme.RED, width=1, style=Qt.PenStyle.DotLine))
        self.p_temp.addItem(self.warn_line)
        self.p_temp.addItem(self.crit_line)
        # cursor
        self.vline = pg.InfiniteLine(angle=90, pen=pg.mkPen("#6b7a8c", width=1))
        self.vline2 = pg.InfiniteLine(angle=90, pen=pg.mkPen("#6b7a8c", width=1))
        self.p_temp.addItem(self.vline, ignoreBounds=True)
        self.p_eng.addItem(self.vline2, ignoreBounds=True)
        self.vline.hide()
        self.vline2.hide()
        self.glw.scene().sigMouseMoved.connect(self._mouse_moved)
        self.p_temp.vb.sigRangeChangedManually.connect(lambda *_: self.set_follow(False))
        self.p_eng.vb.sigRangeChangedManually.connect(lambda *_: self.set_follow(False))
        self._update_live_btn()

    # -- dados ----------------------------------------------------------------
    def set_thresholds(self, warn: float, crit: float) -> None:
        self.warn_line.setPos(warn)
        self.crit_line.setPos(crit)

    def set_window(self, minutes: float) -> None:
        self.window_s = max(30.0, minutes * 60)
        self.refresh()

    def clear(self) -> None:
        self.t.clear()
        for v in self.data.values():
            v.clear()
        for m in self.markers:
            self.p_temp.removeItem(m)
        for m in self._markers2:
            self.p_eng.removeItem(m)
        self.markers.clear()
        self._markers2.clear()
        self.set_follow(True)
        self.refresh()

    def add_row(self, t_s: float, values: dict) -> None:
        self.t.append(t_s)
        for k, arr in self.data.items():
            v = values.get(k)
            arr.append(math.nan if v is None else float(v))
        if len(self.t) > MAX_POINTS:
            cut = len(self.t) - MAX_POINTS
            del self.t[:cut]
            for arr in self.data.values():
                del arr[:cut]
        self._dirty = True

    def add_event(self, ev: Event, tooltip: str) -> None:
        color = theme.LEVEL_COLORS.get(ev.level, theme.MUTED)
        if ev.kind == "LUZ_OLEO_MANUAL":
            color = theme.RED
        elif ev.kind.startswith("CONEXAO"):
            color = "#8a6cff"
        m1 = EventMarker(ev, color, tooltip)
        m2 = EventMarker(ev, color, tooltip)
        short = {"LUZ_OLEO_MANUAL": "ÓLEO", "DTC_NOVO": ev.arg, "ALERTA_ARREFECIMENTO_ALTO": "TEMP",
                 "ALERTA_ARREFECIMENTO_CRITICO": "TEMP!", "ALERTA_OLEO_QUENTE": "ÓLEO°",
                 "ALERTA_TENSAO_BAIXA": "V", "CONEXAO_PERDIDA": "BT✕", "CONEXAO_RESTABELECIDA": "BT✓"}.get(ev.kind)
        if short:
            lbl = pg.InfLineLabel(m1, text=short, position=0.93, color=color, fill=pg.mkBrush(13, 17, 23, 220))
            lbl.setFont(theme.font(11, 700))
        self.p_temp.addItem(m1)
        self.p_eng.addItem(m2)
        self.markers.append(m1)
        self._markers2.append(m2)

    def refresh(self) -> None:
        if not self.t:
            for c in self.curves.values():
                c.setData([], [])
            self.p_temp.setXRange(0, self.window_s, padding=0)
            return
        t = np.asarray(self.t)
        for k, *_ in SERIES_TEMP:
            self.curves[k].setData(t, np.asarray(self.data[k]))
        for k, _, _, scale in SERIES_ENGINE:
            self.curves[k].setData(t, np.asarray(self.data[k]) * (100.0 / scale))
        if self.follow:
            end = self.t[-1]
            start = max(0.0, end - self.window_s)
            # no começo da viagem a janela cresce junto (mínimo 60 s), sem espremer os dados
            self.p_temp.setXRange(start, max(end, start + min(self.window_s, 60.0)), padding=0.01)
        self._dirty = False

    def tick(self) -> None:
        if self._dirty:
            self.refresh()

    # -- navegação --------------------------------------------------------------
    def set_follow(self, on: bool) -> None:
        if on == self.follow:
            return
        self.follow = on
        self._update_live_btn()
        self.follow_changed.emit(on)
        if on:
            self.refresh()

    def _update_live_btn(self) -> None:
        self.live_btn.setEnabled(not self.follow)
        self.live_btn.setText("● Ao vivo" if self.follow else "▶ Voltar ao vivo")

    def center_on(self, t_s: float) -> None:
        self.follow = False
        self._update_live_btn()
        half = self.window_s / 2
        self.p_temp.setXRange(t_s - half, t_s + half, padding=0)
        self._show_cursor(t_s)

    def values_at(self, t_s: float) -> Optional[dict]:
        if not self.t:
            return None
        i = bisect_left(self.t, t_s)
        i = min(max(i, 0), len(self.t) - 1)
        if i > 0 and abs(self.t[i - 1] - t_s) < abs(self.t[i] - t_s):
            i -= 1
        return {"t": self.t[i], **{k: v[i] for k, v in self.data.items()}}

    def _mouse_moved(self, pos) -> None:
        for pl in (self.p_temp, self.p_eng):
            if pl.sceneBoundingRect().contains(pos):
                x = pl.vb.mapSceneToView(pos).x()
                self._show_cursor(x)
                return
        self.vline.hide()
        self.vline2.hide()
        self.cursor_label.setText("")

    def _show_cursor(self, x: float) -> None:
        v = self.values_at(x)
        if v is None:
            return
        self.vline.setPos(v["t"])
        self.vline2.setPos(v["t"])
        self.vline.show()
        self.vline2.show()

        def f(k, unit="", d=0):
            val = v.get(k)
            return "--" if val is None or math.isnan(val) else f"{val:.{d}f}".replace(".", ",") + unit
        self.cursor_label.setText(
            f"{_mmss(v['t'])}  •  arref. {f('arrefecimento_c', ' °C')}  •  óleo {f('oleo_c', ' °C')}  •  "
            f"RPM {f('rpm')}  •  {f('velocidade_kmh', ' km/h')}  •  carga {f('carga_pct', '%')}")
