"""Linha do tempo de eventos (clicar centraliza o gráfico)."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QAbstractItemView, QHeaderView, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget

from ..events import Event, values_summary
from . import theme

HIDDEN = {"INICIO_VIAGEM"}


class EventsPanel(QWidget):
    event_clicked = Signal(float)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Hora", "Evento"])
        self.tree.setRootIsDecorated(False)
        self.tree.setAlternatingRowColors(True)
        self.tree.setWordWrap(True)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.tree.itemClicked.connect(self._clicked)
        lay.addWidget(self.tree)
        self.count = 0

    def clear(self) -> None:
        self.tree.clear()
        self.count = 0

    def add(self, ev: Event) -> None:
        if ev.kind in HIDDEN:
            return
        label = ev.label + (f" ({ev.detail})" if ev.detail else "")
        it = QTreeWidgetItem([f"{ev.t_wall:%H:%M:%S}", label])
        it.setData(0, Qt.ItemDataRole.UserRole, ev.t_s)
        it.setToolTip(1, values_summary(ev.values).replace(" | ", "\n"))
        col = QColor(theme.LEVEL_COLORS.get(ev.level, theme.MUTED))
        if ev.kind == "LUZ_OLEO_MANUAL":
            col = QColor(theme.RED)
            f = it.font(1)
            f.setBold(True)
            it.setFont(1, f)
        elif ev.kind in ("CONEXAO_RESTABELECIDA", "FIM_VIAGEM", "DTC_APAGADO"):
            col = QColor(theme.GREEN if ev.kind == "CONEXAO_RESTABELECIDA" else theme.MUTED)
        it.setForeground(1, col)
        it.setForeground(0, QColor(theme.MUTED))
        sub = QTreeWidgetItem(["", values_summary(ev.values)])
        sub.setForeground(1, QColor(theme.MUTED))
        sub.setData(0, Qt.ItemDataRole.UserRole, ev.t_s)
        self.tree.insertTopLevelItem(0, sub)
        self.tree.insertTopLevelItem(0, it)
        self.count += 1

    def _clicked(self, item: QTreeWidgetItem, _col: int) -> None:
        t = item.data(0, Qt.ItemDataRole.UserRole)
        if t is not None:
            self.event_clicked.emit(float(t))
