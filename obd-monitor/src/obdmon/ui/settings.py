"""Aba Configurações."""
from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout, QFrame,
                               QHBoxLayout, QLabel, QLineEdit, QPushButton, QScrollArea, QVBoxLayout, QWidget)

from ..config import Config


def _spin(lo, hi, step, dec, suffix):
    s = QDoubleSpinBox()
    s.setRange(lo, hi)
    s.setSingleStep(step)
    s.setDecimals(dec)
    s.setSuffix(suffix)
    s.setMinimumWidth(130)
    s.setMaximumWidth(200)
    return s


class SettingsPage(QWidget):
    saved = Signal()
    scan_requested = Signal()

    def __init__(self, cfg: Config, parent=None) -> None:
        super().__init__(parent)
        self.cfg = cfg
        outer = QVBoxLayout(self)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        outer.addWidget(scroll)
        body = QWidget()
        scroll.setWidget(body)
        lay = QVBoxLayout(body)
        lay.setContentsMargins(24, 16, 24, 16)

        def section(title):
            lbl = QLabel(title)
            lbl.setObjectName("sectionTitle")
            lay.addSpacing(10)
            lay.addWidget(lbl)
            card = QFrame()
            card.setObjectName("card")
            form = QFormLayout(card)
            form.setContentsMargins(18, 14, 18, 14)
            form.setHorizontalSpacing(24)
            form.setVerticalSpacing(10)
            lay.addWidget(card)
            return form

        f = section("LIMITES DE ALERTA")
        self.cw = _spin(60, 140, 1, 0, " °C")
        self.cc = _spin(60, 150, 1, 0, " °C")
        self.ow = _spin(60, 170, 1, 0, " °C")
        self.oc = _spin(60, 180, 1, 0, " °C")
        self.vl = _spin(9, 14, 0.1, 1, " V")
        f.addRow("Arrefecimento – aviso a partir de", self.cw)
        f.addRow("Arrefecimento – crítico a partir de", self.cc)
        f.addRow("Óleo – aviso a partir de", self.ow)
        f.addRow("Óleo – crítico a partir de", self.oc)
        f.addRow("Tensão baixa (motor ligado) abaixo de", self.vl)

        f = section("LOG E GRÁFICO")
        self.li = _spin(0.2, 5, 0.1, 1, " s")
        self.li.setToolTip("Vale a partir da próxima viagem")
        self.cwin = _spin(1, 60, 1, 0, " min")
        f.addRow("Intervalo do log (próxima viagem)", self.li)
        f.addRow("Janela do gráfico", self.cwin)
        row = QHBoxLayout()
        self.logs = QLineEdit()
        self.logs.setPlaceholderText(str(cfg.logs_path))
        b = QPushButton("Escolher...")
        b.clicked.connect(self._pick_logs)
        row.addWidget(self.logs, 1)
        row.addWidget(b)
        f.addRow("Pasta dos logs", row)

        f = section("INTERFACE")
        self.sound = QCheckBox("Tocar avisos sonoros")
        self.unit = QComboBox()
        self.unit.addItems(["°C (Celsius)", "°F (Fahrenheit)"])
        self.unit.setMaximumWidth(200)
        f.addRow("Som", self.sound)
        f.addRow("Unidade de temperatura (só na tela)", self.unit)

        f = section("ADAPTADOR")
        row = QHBoxLayout()
        self.addr = QLineEdit()
        self.addr.setPlaceholderText("vazio = usar o último adaptador conectado")
        sb = QPushButton("Procurar...")
        sb.clicked.connect(self.scan_requested.emit)
        row.addWidget(self.addr, 1)
        row.addWidget(sb)
        f.addRow("Adaptador preferido (endereço)", row)
        self.last = QLabel()
        self.last.setObjectName("muted")
        f.addRow("Último usado", self.last)

        lay.addSpacing(14)
        btns = QHBoxLayout()
        btns.addStretch(1)
        self.status = QLabel("")
        self.status.setObjectName("muted")
        btns.addWidget(self.status)
        rb = QPushButton("Restaurar padrões")
        rb.clicked.connect(self._defaults)
        sv = QPushButton("Salvar")
        sv.setObjectName("primary")
        sv.clicked.connect(self._save)
        btns.addWidget(rb)
        btns.addWidget(sv)
        lay.addLayout(btns)
        lay.addStretch(1)
        self.load()

    def load(self) -> None:
        c = self.cfg
        self.cw.setValue(c.coolant_warn)
        self.cc.setValue(c.coolant_crit)
        self.ow.setValue(c.oil_warn)
        self.oc.setValue(c.oil_crit)
        self.vl.setValue(c.voltage_low)
        self.li.setValue(c.log_interval)
        self.cwin.setValue(c.chart_window_min)
        self.logs.setText(c.logs_dir)
        self.sound.setChecked(c.sound)
        self.unit.setCurrentIndex(1 if c.temp_unit == "F" else 0)
        self.addr.setText(c.preferred_address)
        self.last.setText(f"{c.last_name or '-'}  {c.last_address}")

    def _defaults(self) -> None:
        d = Config()
        for k in ("coolant_warn", "coolant_crit", "oil_warn", "oil_crit", "voltage_low", "log_interval",
                  "chart_window_min", "sound", "temp_unit"):
            setattr(self.cfg, k, getattr(d, k))
        self.load()

    def _pick_logs(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Pasta dos logs", str(self.cfg.logs_path))
        if d:
            self.logs.setText(d)

    def _save(self) -> None:
        c = self.cfg
        if self.cc.value() <= self.cw.value() or self.oc.value() <= self.ow.value():
            self.status.setText("O limite crítico precisa ser maior que o de aviso.")
            return
        c.coolant_warn, c.coolant_crit = self.cw.value(), self.cc.value()
        c.oil_warn, c.oil_crit = self.ow.value(), self.oc.value()
        c.voltage_low = self.vl.value()
        c.log_interval = round(self.li.value(), 1)
        c.chart_window_min = self.cwin.value()
        c.logs_dir = self.logs.text().strip()
        c.sound = self.sound.isChecked()
        c.temp_unit = "F" if self.unit.currentIndex() == 1 else "C"
        c.preferred_address = self.addr.text().strip()
        c.save()
        self.status.setText("Configurações salvas ✔")
        self.saved.emit()
