"""Medidores desenhados com QPainter: mostrador em arco (com ponteiro animado)
e barra horizontal. A animação é feita por um timer único (MainWindow) que
chama ``step(dt)``; o widget só redesenha quando o valor mostrado muda."""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable, Optional

from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QPainter, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from . import theme

Bands = list[tuple[float, str]]   # [(até, cor), ...] em ordem crescente

PAD = 8.0            # margem interna do cartão (px lógicos)
TITLE_MIN_PX = 9.0
FOOT_MIN_PX = 9.0
UNIT_MIN_PX = 8
UNIT_TOP = 0.54      # topo do texto da unidade, em frações do raio abaixo do centro
MIN_DIAL_R = 55.0    # abaixo disso as fontes de título/rodapé também encolhem


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _thick(r: float) -> float:
    """Espessura do arco: 13% do raio, mínimo 8 px (ou 20% do raio em mostradores minúsculos)."""
    return max(min(8.0, r * 0.2), r * 0.13)


@dataclass
class GaugeLayout:
    title_rect: QRectF
    title_font: QFont
    cx: float
    cy: float
    r: float
    thick: float
    outer: float
    below: float
    foot_rect: QRectF
    foot_font: QFont
    dial_bottom: float


def band_color(bands: Bands, v: float) -> str:
    for upto, col in bands:
        if v < upto:
            return col
    return bands[-1][1] if bands else theme.ACCENT


class _Base(QWidget):
    def __init__(self, title: str, unit: str, vmin: float, vmax: float, bands: Bands,
                 decimals: int = 0, fmt: Optional[Callable[[float], str]] = None, parent=None) -> None:
        super().__init__(parent)
        self.title = title
        self.unit = unit
        self.vmin, self.vmax = vmin, vmax
        self.bands = bands
        self.decimals = decimals
        self.fmt = fmt
        self.target: Optional[float] = None
        self.shown: Optional[float] = None
        self.supported = True
        self.stale = False          # dado com mais de 1,5 s: valor em amarelo
        self.trip_min: Optional[float] = None
        self.trip_max: Optional[float] = None
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, False)

    def text(self, v: Optional[float]) -> str:
        if v is None:
            return "--"
        if self.fmt:
            return self.fmt(v)
        return f"{v:.{self.decimals}f}".replace(".", ",")

    def set_value(self, v: Optional[float]) -> None:
        self.target = v
        if v is not None:
            self.trip_min = v if self.trip_min is None else min(self.trip_min, v)
            self.trip_max = v if self.trip_max is None else max(self.trip_max, v)
            if self.shown is None:
                self.shown = v
        self.update()

    def set_supported(self, ok: bool) -> None:
        if ok != self.supported:
            self.supported = ok
            self.update()

    def set_stale(self, stale: bool) -> None:
        if stale != self.stale:
            self.stale = stale
            self.update()

    def value_color(self) -> QColor:
        if self.target is None:
            return QColor(theme.DIM)
        return QColor(theme.YELLOW if self.stale else theme.TEXT)

    def reset_trip(self) -> None:
        self.trip_min = self.trip_max = None
        self.target = self.shown = None
        self.update()

    def step(self, dt: float) -> None:
        """Aproxima o valor mostrado do alvo (animação suave, sem 'pular')."""
        if self.target is None:
            if self.shown is not None:
                self.shown = None
                self.update()
            return
        if self.shown is None:
            self.shown = self.target
            self.update()
            return
        diff = self.target - self.shown
        span = (self.vmax - self.vmin) or 1
        if abs(diff) < span * 0.0008:
            if diff:
                self.shown = self.target
                self.update()
            return
        self.shown += diff * min(1.0, dt * 9.0)
        self.update()

    def frac(self, v: float) -> float:
        return max(0.0, min(1.0, (v - self.vmin) / ((self.vmax - self.vmin) or 1)))

    def current_color(self) -> QColor:
        if not self.supported:
            return QColor(theme.GRAY)
        if self.target is None:
            return QColor(theme.DIM)
        return QColor(band_color(self.bands, self.target))


class ArcGauge(_Base):
    """Mostrador de 270° com faixas coloridas, ponteiro e máx/mín da viagem."""

    def __init__(self, *a, big: bool = False, **k) -> None:
        super().__init__(*a, **k)
        self.big = big
        self._draw_footer = True   # só os testes desligam (para medir a "tinta" do rodapé)
        self.setMinimumSize(QSize(140, 150))
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def sizeHint(self) -> QSize:
        return QSize(240, 240)

    def footer_text(self) -> str:
        return f"mín {self.text(self.trip_min)}   máx {self.text(self.trip_max)}"

    def compute_layout(self, w: float, h: float, foot_text: Optional[str] = None) -> GaugeLayout:
        """Calcula a geometria do medidor a partir das métricas REAIS das fontes.

        De cima para baixo: título | mostrador | rodapé (mín/máx). O rodapé tem
        uma área própria (ascent + descent + margem) que o arco nunca invade.
        Se faltar espaço, o arco encolhe; se ficar pequeno demais, as fontes do
        título e do rodapé também encolhem, proporcionalmente, em vez de cortar."""
        foot_text = foot_text if foot_text is not None else self.footer_text()
        base = min(w, h)
        title_px = _clamp(base * 0.06, TITLE_MIN_PX, 22)
        foot_px = _clamp(base * 0.05, FOOT_MIN_PX, 18)
        avail_w = max(1.0, w - 2 * PAD)
        while True:
            title_font = theme.font(int(round(title_px)), QFont.Weight.Bold)
            foot_font = theme.font(int(round(foot_px)))
            tfm, ffm = QFontMetricsF(title_font), QFontMetricsF(foot_font)
            # o texto do rodapé tem que caber na largura
            adv = ffm.horizontalAdvance(foot_text)
            if adv > avail_w and foot_px > FOOT_MIN_PX:
                foot_px = max(FOOT_MIN_PX, foot_px * avail_w / adv - 0.5)
                continue
            title_h = tfm.ascent() + tfm.descent()
            foot_h = ffm.ascent() + ffm.descent()
            dial_top = PAD + title_h + PAD / 2
            dial_bottom = h - PAD - foot_h - PAD / 2   # limite do arco: acima do rodapé
            avail_h = dial_bottom - dial_top
            r = self._fit_radius(avail_w, avail_h)
            # mostrador pequeno demais? encolhe as fontes de título/rodapé e tenta de novo
            if r < MIN_DIAL_R and (title_px > TITLE_MIN_PX or foot_px > FOOT_MIN_PX):
                title_px = max(TITLE_MIN_PX, title_px * 0.9)
                foot_px = max(FOOT_MIN_PX, foot_px * 0.9)
                continue
            break
        outer, below = self._extents(r)
        block = outer + below
        cy = dial_top + max(0.0, avail_h - block) / 2 + outer
        foot_top = cy + below + PAD / 2
        return GaugeLayout(
            title_rect=QRectF(PAD, PAD, avail_w, title_h), title_font=title_font,
            cx=w / 2, cy=cy, r=r, thick=_thick(r), outer=outer, below=below,
            foot_rect=QRectF(PAD, foot_top, avail_w, foot_h), foot_font=foot_font,
            dial_bottom=dial_bottom)

    def _unit_font(self, r: float) -> QFont:
        return theme.font(max(UNIT_MIN_PX, int(r * 0.12)))

    def _extents(self, r: float) -> tuple[float, float]:
        """(quanto o desenho sobe acima do centro, quanto desce abaixo dele)."""
        thick = _thick(r)
        band = max(3.0, thick * 0.22)
        outer = r + thick * 0.25 + band / 2 + 1.0          # faixas finas por fora + antialias
        unit_h = QFontMetricsF(self._unit_font(r)).height()
        below = max(outer * math.sin(math.radians(45)) + 1.0,  # pontas do arco de 270°
                    r * UNIT_TOP + unit_h,                     # texto da unidade
                    r * 0.60)                                  # "não suportado pelo carro"
        return outer, below

    def _fit_radius(self, avail_w: float, avail_h: float) -> float:
        """Maior raio cujo desenho cabe em avail_w × avail_h (busca binária)."""
        lo, hi = 1.0, max(2.0, max(avail_w, avail_h))
        for _ in range(40):
            mid = (lo + hi) / 2
            outer, below = self._extents(mid)
            if 2 * outer <= avail_w and outer + below <= avail_h:
                lo = mid
            else:
                hi = mid
        return lo

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        # cartão
        p.setPen(QPen(QColor(theme.BORDER), 1))
        p.setBrush(QColor(theme.PANEL))
        p.drawRoundedRect(QRectF(0.5, 0.5, w - 1, h - 1), 14, 14)

        mm = self.footer_text()
        L = self.compute_layout(w, h, mm)
        cx, cy, r, thick = L.cx, L.cy, L.r, L.thick
        ring = QRectF(cx - r, cy - r, 2 * r, 2 * r)
        start, span = 225.0, -270.0

        p.setPen(QColor(theme.MUTED))
        p.setFont(L.title_font)
        title = QFontMetricsF(L.title_font).elidedText(self.title.upper(), Qt.TextElideMode.ElideRight,
                                                       L.title_rect.width())
        p.drawText(L.title_rect, Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter, title)

        # trilho
        inner = ring.adjusted(thick / 2, thick / 2, -thick / 2, -thick / 2)
        p.setPen(QPen(QColor("#222c38"), thick, Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap))
        p.drawArc(inner, int(start * 16), int(span * 16))

        # faixas finas por fora
        band_rect = ring.adjusted(-thick * 0.25, -thick * 0.25, thick * 0.25, thick * 0.25)
        lo = self.vmin
        for upto, col in self.bands:
            hi = min(upto, self.vmax)
            if hi <= lo:
                continue
            a0 = start + span * self.frac(lo)
            a1 = start + span * self.frac(hi)
            c = QColor(col if self.supported else theme.DIM)
            c.setAlpha(150)
            p.setPen(QPen(c, max(3.0, thick * 0.22), Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap))
            p.drawArc(band_rect, int(a0 * 16), int((a1 - a0) * 16))
            lo = hi

        # marcas
        p.setPen(QPen(QColor("#3a4757"), 2))
        tick_l, tick_s = max(4.0, r * 0.10), max(2.5, r * 0.06)
        for i in range(11):
            a = math.radians(start + span * i / 10)
            r1, r2 = r - thick - 4, r - thick - 4 - (tick_l if i % 5 == 0 else tick_s)
            p.drawLine(QPointF(cx + r1 * math.cos(a), cy - r1 * math.sin(a)),
                       QPointF(cx + r2 * math.cos(a), cy - r2 * math.sin(a)))

        col = self.current_color()
        v = self.shown
        if self.supported and v is not None:
            f = self.frac(v)
            glow = QColor(col)
            glow.setAlpha(60)
            p.setPen(QPen(glow, thick * 1.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap))
            p.drawArc(inner, int(start * 16), int(span * f * 16))
            p.setPen(QPen(col, thick, Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap))
            p.drawArc(inner, int(start * 16), int(span * f * 16))
            # ponteiro
            a = math.radians(start + span * f)
            rn = max(r * 0.3, r - thick - max(6.0, r * 0.12))
            tip = QPointF(cx + rn * math.cos(a), cy - rn * math.sin(a))
            base = QPointF(cx - rn * 0.12 * math.cos(a), cy + rn * 0.12 * math.sin(a))
            p.setPen(QPen(QColor("white"), max(2.5, r * 0.025), Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawLine(base, tip)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor("#0d1117"))
            p.drawEllipse(QPointF(cx, cy), r * 0.07, r * 0.07)
            p.setBrush(col)
            p.drawEllipse(QPointF(cx, cy), r * 0.04, r * 0.04)

        # valor
        if not self.supported:
            p.setPen(QColor(theme.MUTED))
            p.setFont(theme.font(max(UNIT_MIN_PX, int(r * 0.15)), QFont.Weight.Bold))
            p.drawText(QRectF(cx - r, cy + r * 0.05, 2 * r, r * 0.55), Qt.AlignmentFlag.AlignCenter,
                       "não suportado\npelo carro")
        else:
            p.setPen(self.value_color() if v is not None else QColor(theme.DIM))
            p.setFont(theme.font(max(UNIT_MIN_PX, int(r * (0.36 if self.big else 0.33))), QFont.Weight.Bold))
            p.drawText(QRectF(cx - r, cy + r * 0.08, 2 * r, r * (UNIT_TOP - 0.08)), Qt.AlignmentFlag.AlignCenter,
                       self.text(self.target if self.target is not None else None))
            p.setPen(QColor(theme.MUTED))
            uf = self._unit_font(r)
            p.setFont(uf)
            p.drawText(QRectF(cx - r, cy + r * UNIT_TOP, 2 * r, QFontMetricsF(uf).height()),
                       Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop, self.unit)

        # máx / mín da viagem: área própria no rodapé, nunca cortada
        if self._draw_footer:
            p.setFont(L.foot_font)
            p.setPen(QColor(theme.MUTED))
            p.drawText(L.foot_rect, Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop, mm)
        p.end()


class BarGauge(_Base):
    """Barra horizontal compacta: título, valor e barra colorida por faixa."""

    def __init__(self, *a, **k) -> None:
        super().__init__(*a, **k)
        self.setMinimumHeight(76)
        self.setMinimumWidth(100)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def sizeHint(self) -> QSize:
        return QSize(200, 80)

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        p.setPen(QPen(QColor(theme.BORDER), 1))
        p.setBrush(QColor(theme.PANEL))
        p.drawRoundedRect(QRectF(0.5, 0.5, w - 1, h - 1), 12, 12)
        pad = 12
        p.setPen(QColor(theme.MUTED))
        tf = theme.font(12, QFont.Weight.Bold)
        p.setFont(tf)
        rng = f"{self.text(self.trip_min)}–{self.text(self.trip_max)}"
        rng_w = QFontMetricsF(theme.font(11)).horizontalAdvance(rng) + 8
        title_w = QFontMetricsF(tf).horizontalAdvance(self.title.upper())
        show_rng = self.supported and self.trip_min is not None and title_w + rng_w <= w - 2 * pad
        title = QFontMetricsF(tf).elidedText(self.title.upper(), Qt.TextElideMode.ElideRight, w - 2 * pad)
        p.drawText(QRectF(pad, 8, w - 2 * pad, 18), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, title)
        if self.supported:
            txt = self.text(self.target) + (f" {self.unit}" if self.target is not None else "")
            p.setPen(self.value_color())
            p.setFont(theme.font(22, QFont.Weight.Bold))
        else:
            txt = "não suportado"
            p.setPen(QColor(theme.MUTED))
            p.setFont(theme.font(14, QFont.Weight.Bold))
        p.drawText(QRectF(pad, 24, w - 2 * pad, 30), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, txt)
        if show_rng:
            p.setFont(theme.font(11))
            p.setPen(QColor(theme.DIM))
            p.drawText(QRectF(pad, 8, w - 2 * pad, 18), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, rng)
        bar = QRectF(pad, h - 18, w - 2 * pad, 7)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#222c38"))
        p.drawRoundedRect(bar, 3.5, 3.5)
        if self.supported and self.shown is not None:
            fill = QRectF(bar.x(), bar.y(), bar.width() * self.frac(self.shown), bar.height())
            p.setBrush(self.current_color())
            p.drawRoundedRect(fill, 3.5, 3.5)
        p.end()
