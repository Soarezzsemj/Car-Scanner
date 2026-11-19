"""Tema escuro, cores por faixa e ícone do app (desenhado em código)."""
from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QFont, QIcon, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap

BG = "#0d1117"
PANEL = "#151b23"
PANEL2 = "#1b232d"
BORDER = "#283241"
TEXT = "#e8eef5"
MUTED = "#8a97a6"
DIM = "#4c5866"
BLUE = "#3ea6ff"
GREEN = "#2fd27f"
YELLOW = "#ffc53d"
ORANGE = "#ff8a3d"
RED = "#ff4d4f"
GRAY = "#5b6672"
ACCENT = "#5ad1e6"
SIM = "#b47cff"

FONT_FAMILY = "Segoe UI"

STYLESHEET = f"""
* {{ font-family: "{FONT_FAMILY}"; color: {TEXT}; }}
QMainWindow, QWidget#root {{ background: {BG}; }}
QWidget {{ background: transparent; font-size: 14px; }}
QFrame#card {{ background: {PANEL}; border: 1px solid {BORDER}; border-radius: 12px; }}
QFrame#topbar {{ background: {PANEL}; border-bottom: 1px solid {BORDER}; }}
QLabel#appTitle {{ font-size: 20px; font-weight: 700; }}
QLabel#appSub {{ color: {MUTED}; font-size: 12px; }}
QLabel#muted {{ color: {MUTED}; }}
QLabel#bigClock {{ font-size: 20px; font-weight: 600; font-family: "Consolas"; }}
QLabel#sectionTitle {{ color: {MUTED}; font-size: 12px; font-weight: 700; letter-spacing: 1px; }}
QPushButton {{
    background: {PANEL2}; border: 1px solid {BORDER}; border-radius: 8px;
    padding: 7px 14px; font-weight: 600;
}}
QPushButton:hover {{ background: #232d39; border-color: #3a4757; }}
QPushButton:pressed {{ background: #2b3746; }}
QPushButton:disabled {{ color: {DIM}; }}
QPushButton#primary {{ background: #1f6feb; border-color: #388bfd; }}
QPushButton#primary:hover {{ background: #2f7cf6; }}
QPushButton#danger {{ background: #3a1517; border-color: #6e2a2d; }}
QPushButton#oilButton {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #e5383b, stop:1 #a4161a);
    border: 2px solid #ff6b6e; border-radius: 16px; font-size: 22px; font-weight: 800;
    padding: 14px; color: white;
}}
QPushButton#oilButton:hover {{ background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #f04b4e, stop:1 #b81d21); }}
QPushButton#oilButton:pressed {{ background: #7d1013; }}
QTabWidget::pane {{ border: 1px solid {BORDER}; border-radius: 10px; background: {PANEL}; top: -1px; }}
QTabBar::tab {{
    background: transparent; color: {MUTED}; padding: 8px 16px; border: none;
    border-bottom: 2px solid transparent; font-weight: 600;
}}
QTabBar::tab:selected {{ color: {TEXT}; border-bottom: 2px solid {BLUE}; }}
QTabBar::tab:hover {{ color: {TEXT}; }}
QTableWidget, QTreeWidget, QListWidget, QPlainTextEdit, QTextEdit {{
    background: {PANEL}; border: none; gridline-color: {BORDER};
    selection-background-color: #22344a; selection-color: {TEXT}; alternate-background-color: #18202a;
}}
QHeaderView::section {{
    background: {PANEL2}; color: {MUTED}; border: none; border-bottom: 1px solid {BORDER};
    padding: 6px; font-weight: 700; font-size: 12px;
}}
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
    background: {PANEL2}; border: 1px solid {BORDER}; border-radius: 6px; padding: 5px 8px; min-height: 22px;
}}
QComboBox QAbstractItemView {{ background: {PANEL2}; border: 1px solid {BORDER}; selection-background-color: #22344a; }}
QCheckBox {{ spacing: 8px; }}
QCheckBox::indicator {{
    width: 18px; height: 18px; border: 1px solid #4a5868; border-radius: 4px; background: {PANEL2};
}}
QCheckBox::indicator:checked {{ background: {BLUE}; border-color: {BLUE}; }}
QCheckBox::indicator:hover {{ border-color: {BLUE}; }}
QSlider::groove:horizontal {{ height: 6px; background: {PANEL2}; border-radius: 3px; }}
QSlider::handle:horizontal {{ background: {BLUE}; width: 16px; margin: -6px 0; border-radius: 8px; }}
QSlider::sub-page:horizontal {{ background: #2a5d93; border-radius: 3px; }}
QScrollBar:vertical {{ background: transparent; width: 10px; }}
QScrollBar::handle:vertical {{ background: #2d3846; border-radius: 5px; min-height: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
QToolTip {{ background: {PANEL2}; color: {TEXT}; border: 1px solid {BORDER}; padding: 6px; font-size: 13px; }}
QStatusBar {{ background: {PANEL}; color: {MUTED}; border-top: 1px solid {BORDER}; }}
QMenu {{ background: {PANEL2}; border: 1px solid {BORDER}; }}
QMenu::item {{ padding: 6px 18px; }}
QMenu::item:selected {{ background: #22344a; }}
"""

LEVEL_COLORS = {"info": MUTED, "aviso": YELLOW, "critico": RED}


def font(size: int, weight: int = QFont.Weight.Normal, family: str = FONT_FAMILY) -> QFont:
    f = QFont(family)
    f.setPixelSize(size)
    f.setWeight(QFont.Weight(weight))
    return f


def badge_style(color: str, filled: bool = True) -> str:
    c = QColor(color)
    bg = f"rgba({c.red()},{c.green()},{c.blue()},0.18)" if filled else "transparent"
    return (f"background:{bg}; color:{color}; border:1px solid {color}; border-radius: 13px;"
            f"padding: 3px 12px; font-weight: 700; font-size: 13px;")


def make_icon() -> QIcon:
    """Ícone: mostrador com ponteiro e uma gota de óleo."""
    icon = QIcon()
    for size in (16, 24, 32, 48, 64, 128, 256):
        pm = QPixmap(size, size)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        s = size / 256.0
        g = QLinearGradient(0, 0, 0, size)
        g.setColorAt(0, QColor("#1d2836"))
        g.setColorAt(1, QColor("#0b0f14"))
        p.setBrush(QBrush(g))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(QRectF(0, 0, size, size), 56 * s, 56 * s)
        r = QRectF(36 * s, 40 * s, 184 * s, 184 * s)
        pen = QPen(QColor("#2b3a4c"), 22 * s, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        p.drawArc(r, 225 * 16, -270 * 16)
        for start, span, col in ((225, -110, GREEN), (115, -90, YELLOW), (25, -70, RED)):
            pen.setColor(QColor(col))
            p.setPen(pen)
            p.drawArc(r, start * 16, span * 16)
        p.setPen(QPen(QColor("white"), 14 * s, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        c = r.center()
        p.drawLine(c, QPointF(c.x() + 62 * s, c.y() - 50 * s))
        p.setBrush(QColor("white"))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(c, 14 * s, 14 * s)
        # gota
        path = QPainterPath()
        cx, cy = 128 * s, 196 * s
        path.moveTo(cx, cy - 34 * s)
        path.cubicTo(cx + 24 * s, cy - 6 * s, cx + 26 * s, cy + 22 * s, cx, cy + 24 * s)
        path.cubicTo(cx - 26 * s, cy + 22 * s, cx - 24 * s, cy - 6 * s, cx, cy - 34 * s)
        p.setBrush(QColor(ORANGE))
        p.drawPath(path)
        p.end()
        icon.addPixmap(pm)
    return icon
