"""Tabela de códigos de falha ativos/pendentes/permanentes."""
from __future__ import annotations

from PySide6.QtGui import QColor
from PySide6.QtWidgets import QHeaderView, QLabel, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget

from .. import dtc as dtcmod
from . import theme

MODE_COLORS = {"confirmado": theme.RED, "pendente": theme.YELLOW, "permanente": theme.ORANGE}


class DtcPanel(QWidget):
    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        self.table.resizeRowsToContents()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Código", "Situação", "Descrição", "Desde"])
        self.table.verticalHeader().hide()
        self.table.setWordWrap(True)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        h = self.table.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        h.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        h.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        h.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        h.sectionResized.connect(lambda *_: self.table.resizeRowsToContents())
        lay.addWidget(self.table, 1)
        self.empty = QLabel("Nenhum código de falha.\n\nLembrete: no C3 a luz de pressão do óleo pode ser "
                            "tratada pelo painel/BSI, fora do OBD2 genérico — é possível que nenhum código "
                            "apareça aqui. Use o botão da luz do óleo para marcar o momento.")
        self.empty.setWordWrap(True)
        self.empty.setObjectName("muted")
        self.empty.setMargin(12)
        lay.addWidget(self.empty)
        self.set_table({})

    def set_table(self, table: dict) -> None:
        self.table.setRowCount(0)
        for code, info in sorted(table.items()):
            r = self.table.rowCount()
            self.table.insertRow(r)
            c = QTableWidgetItem(code)
            f = c.font()
            f.setBold(True)
            c.setFont(f)
            if code in dtcmod.OIL_COOLING:
                c.setForeground(QColor(theme.RED))
                c.setToolTip("Código relacionado a óleo/arrefecimento")
            modes = info["modos"]
            m = QTableWidgetItem(", ".join(modes))
            worst = next((k for k in ("confirmado", "permanente", "pendente") if k in modes), None)
            if worst:
                m.setForeground(QColor(MODE_COLORS[worst]))
            d = QTableWidgetItem(info["descricao"])
            d.setToolTip(info["descricao"])
            t = QTableWidgetItem(f"{info['primeira_vez']:%H:%M:%S}")
            for col, it in enumerate((c, m, d, t)):
                self.table.setItem(r, col, it)
        self.table.resizeRowsToContents()
        has = bool(table)
        self.table.setVisible(has)
        self.empty.setVisible(not has)
