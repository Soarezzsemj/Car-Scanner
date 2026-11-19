"""Aba "Diagnóstico do adaptador": PIDs suportados, tempos, versão, protocolo,
autoteste e exportação de relatório."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (QFileDialog, QFrame, QGridLayout, QHBoxLayout, QHeaderView, QLabel,
                               QPlainTextEdit, QPushButton, QSplitter, QTableWidget, QTableWidgetItem,
                               QVBoxLayout, QWidget)
from PySide6.QtCore import Qt

from ..pids import PIDS
from . import theme


def _ms(x: Optional[float]) -> str:
    return "--" if x is None else f"{x * 1000:.0f}"


class DiagnosticsPage(QWidget):
    selftest_requested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.diag: dict = {}
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 12, 16, 12)

        info = QFrame()
        info.setObjectName("card")
        g = QGridLayout(info)
        g.setContentsMargins(16, 12, 16, 12)
        g.setHorizontalSpacing(28)
        self.fields: dict[str, QLabel] = {}
        names = [("transporte", "Transporte"), ("endereco", "Endereço"), ("versao", "Versão do ELM"),
                 ("protocolo", "Protocolo"), ("tensao", "Tensão (ATRV)"), ("hz", "Ciclo rápido"),
                 ("ciclo", "Duração do ciclo"), ("dtc", "Leitura de DTC"), ("conexoes", "Conexões"),
                 ("erro", "Último erro")]
        for i, (k, label) in enumerate(names):
            r, c = divmod(i, 2)
            lbl = QLabel(label)
            lbl.setObjectName("muted")
            val = QLabel("--")
            val.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            val.setWordWrap(True)
            g.addWidget(lbl, r, c * 2)
            g.addWidget(val, r, c * 2 + 1)
            self.fields[k] = val
        g.setColumnStretch(1, 1)
        g.setColumnStretch(3, 1)
        lay.addWidget(info)

        split = QSplitter(Qt.Orientation.Vertical)
        lay.addWidget(split, 1)
        self.table = QTableWidget(len(PIDS), 9)
        self.table.setHorizontalHeaderLabels(["PID", "Nome", "Suporte", "Ciclo", "Média (ms)", "Mín", "Máx",
                                              "OK", "Falhas"])
        self.table.verticalHeader().hide()
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        split.addWidget(self.table)

        box = QWidget()
        bl = QVBoxLayout(box)
        bl.setContentsMargins(0, 8, 0, 0)
        row = QHBoxLayout()
        t = QLabel("AUTOTESTE (carro parado, motor ligado)")
        t.setObjectName("sectionTitle")
        row.addWidget(t)
        row.addStretch(1)
        self.run_btn = QPushButton("▶ Rodar autoteste")
        self.run_btn.setObjectName("primary")
        self.run_btn.clicked.connect(self.selftest_requested.emit)
        self.export_btn = QPushButton("Exportar relatório")
        self.export_btn.clicked.connect(self.export)
        row.addWidget(self.run_btn)
        row.addWidget(self.export_btn)
        bl.addLayout(row)
        self.report = QPlainTextEdit()
        self.report.setReadOnly(True)
        mono = QFont("Consolas")
        mono.setPixelSize(13)
        self.report.setFont(mono)
        self.report.setPlaceholderText("Nenhum autoteste rodado ainda. Conecte ao carro (motor ligado, parado) "
                                       "e clique em \"Rodar autoteste\". Também dá para rodar pelo atalho "
                                       "iniciar.bat --selftest.")
        bl.addWidget(self.report, 1)
        split.addWidget(box)
        split.setSizes([300, 300])
        self.logs_dir: Optional[Path] = None

    def set_report(self, text: str) -> None:
        self.report.setPlainText(text)

    def set_running(self, running: bool) -> None:
        self.run_btn.setEnabled(not running)
        self.run_btn.setText("Autoteste em andamento..." if running else "▶ Rodar autoteste")

    def update_diag(self, d: dict) -> None:
        self.diag = d
        elm = d.get("elm")
        tr = d.get("transporte", {})
        f = self.fields
        f["transporte"].setText(tr.get("transporte", "--") + (f"  ({tr['cenario']})" if "cenario" in tr else ""))
        chars = ""
        if "caracteristica_rx" in tr:
            chars = f"\nRX {tr['caracteristica_rx']}\nTX {tr['caracteristica_tx']}"
        f["endereco"].setText(tr.get("endereco", "--") + chars)
        if elm is not None:
            f["versao"].setText(" ".join(x for x in (elm.version, elm.device) if x) or "--")
            f["protocolo"].setText(f"{elm.protocol or '--'}  (nº {elm.protocol_num or '?'})")
            f["tensao"].setText(elm.voltage or "--")
        hz = d.get("hz") or 0
        f["hz"].setText(f"{hz:.1f} Hz".replace(".", ","))
        cm = d.get("ciclo_ms")
        f["ciclo"].setText("--" if cm is None else f"{cm:.0f} ms")
        dm = d.get("dtc_ms")
        f["dtc"].setText("--" if dm is None else f"{dm:.0f} ms (03/07 a cada 5 s, 0A a cada 60 s)")
        f["conexoes"].setText(str(d.get("conexoes", 0)))
        f["erro"].setText(d.get("erro") or "nenhum")
        stats = d.get("stats", {})
        sup = d.get("suportados")
        for row, p in enumerate(PIDS):
            st = stats.get(p.pid)
            if st is None:
                continue
            if st.supported is False:
                s, col = "✖ não suportado", theme.RED
            elif st.runtime_unsupported:
                s, col = "✖ NO DATA repetido", theme.YELLOW
            elif st.supported:
                s, col = "✔ suportado", theme.GREEN
            else:
                s, col = "? (máscara não lida)" if sup is None else "?", theme.MUTED
            cycle = {"rapido": "rápido", "medio": "médio (2 s)", "lento": "lento (30 s)"}.get(st.cycle, st.cycle)
            cells = [p.hex, p.name, s, cycle, _ms(st.lat_avg), _ms(st.lat_min), _ms(st.lat_max),
                     str(st.ok), str(st.fails)]
            for c, txt in enumerate(cells):
                it = self.table.item(row, c)
                if it is None:
                    it = QTableWidgetItem()
                    self.table.setItem(row, c, it)
                it.setText(txt)
                if c == 2:
                    it.setForeground(QColor(col))

    def report_text(self) -> str:
        lines = [f"RELATÓRIO DO ADAPTADOR – {datetime.now():%d/%m/%Y %H:%M:%S}", ""]
        for k, lbl in self.fields.items():
            lines.append(f"{k}: {lbl.text()}")
        lines += ["", "PID  Nome                       Suporte              Ciclo            média  mín  máx  ok  falhas"]
        for r in range(self.table.rowCount()):
            cells = [(self.table.item(r, c).text() if self.table.item(r, c) else "") for c in range(9)]
            lines.append(f"{cells[0]:<4} {cells[1]:<26} {cells[2]:<20} {cells[3]:<16} {cells[4]:>5} {cells[5]:>4} "
                         f"{cells[6]:>4} {cells[7]:>4} {cells[8]:>5}")
        if self.report.toPlainText().strip():
            lines += ["", self.report.toPlainText()]
        return "\n".join(lines) + "\n"

    def export(self) -> None:
        base = self.logs_dir or Path.cwd()
        default = str(Path(base) / f"relatorio_adaptador_{datetime.now():%Y-%m-%d_%H-%M-%S}.txt")
        path, _ = QFileDialog.getSaveFileName(self, "Exportar relatório", default, "Texto (*.txt)")
        if path:
            Path(path).write_text(self.report_text(), encoding="utf-8-sig")
