"""Painel de controle do simulador (só aparece no modo simulador)."""
from __future__ import annotations

from typing import Callable

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFrame, QGridLayout, QHBoxLayout, QInputDialog, QLabel,
                               QPushButton, QScrollArea, QSlider, QVBoxLayout, QWidget)

from ..dtc import DESCRIPTIONS
from ..sim.scenarios import SCENARIOS
from . import theme

FACTORS = [1, 2, 5, 10, 20, 60]


class SimPanel(QWidget):
    restart_requested = Signal(str, float)   # cenário, fator

    def __init__(self, call: Callable, parent=None) -> None:
        """``call(fn)`` executa ``fn`` na thread do asyncio (onde vive o carro)."""
        super().__init__(parent)
        self.call = call
        self.car = None
        self.adapter = None
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        outer.addWidget(scroll)
        body = QWidget()
        scroll.setWidget(body)
        lay = QVBoxLayout(body)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(8)

        warn = QLabel("SIMULADOR – dados FALSOS")
        warn.setStyleSheet(f"color:{theme.SIM}; font-weight:800; letter-spacing:1px;")
        lay.addWidget(warn)

        self.scenario = QComboBox()
        for name, cls in SCENARIOS.items():
            self.scenario.addItem(cls.title, name)
        self.scenario.currentIndexChanged.connect(self._scenario_changed)
        lay.addWidget(self.scenario)
        self.desc = QLabel()
        self.desc.setWordWrap(True)
        self.desc.setObjectName("muted")
        lay.addWidget(self.desc)
        row = QHBoxLayout()
        row.addWidget(QLabel("Tempo"))
        self.factor = QComboBox()
        for f in FACTORS:
            self.factor.addItem(f"{f}x", f)
        self.factor.currentIndexChanged.connect(self._factor_changed)
        row.addWidget(self.factor)
        rb = QPushButton("↻ Reiniciar viagem")
        rb.setToolTip("Começa uma nova viagem simulada com o cenário escolhido")
        rb.clicked.connect(self._restart)
        row.addWidget(rb, 1)
        lay.addLayout(row)

        # painel "do carro" simulado
        card = QFrame()
        card.setObjectName("card")
        g = QGridLayout(card)
        g.setContentsMargins(10, 8, 10, 8)
        self.ind: dict[str, QLabel] = {}
        for i, (k, label) in enumerate([("luz", "Luz do óleo (painel)"), ("apito", "Apito do painel"),
                                        ("vent", "Ventoinha"), ("trecho", "Trecho / marcha"),
                                        ("tsim", "Tempo simulado"), ("ecu", "ECU")]):
            lbl = QLabel(label)
            lbl.setObjectName("muted")
            v = QLabel("--")
            g.addWidget(lbl, i, 0)
            g.addWidget(v, i, 1)
            self.ind[k] = v
        lay.addWidget(card)

        # sliders para forçar valores
        self.sliders = {}
        for key, label, lo, hi in [("arrefecimento_c", "Arrefecimento °C", 20, 130), ("rpm", "RPM", 0, 6500),
                                   ("velocidade_kmh", "Velocidade km/h", 0, 180)]:
            row = QHBoxLayout()
            cb = QCheckBox(f"Forçar {label}")
            val = QLabel("")
            val.setMinimumWidth(44)
            val.setAlignment(Qt.AlignmentFlag.AlignRight)
            row.addWidget(cb, 1)
            row.addWidget(val)
            lay.addLayout(row)
            s = QSlider(Qt.Orientation.Horizontal)
            s.setRange(lo, hi)
            s.setValue({"arrefecimento_c": 90, "rpm": 2000, "velocidade_kmh": 60}[key])
            s.setEnabled(False)
            lay.addWidget(s)
            val.setText(str(s.value()))
            cb.toggled.connect(lambda on, k=key, sl=self.sliders: self._override(k))
            s.valueChanged.connect(lambda v, k=key, lb=val: (lb.setText(str(v)), self._override(k)))
            self.sliders[key] = (cb, s)

        grid = QVBoxLayout()
        grid.setSpacing(6)
        b1 = QPushButton("🔥 Superaquecer agora")
        b1.clicked.connect(self._overheat)
        b2 = QPushButton("⚠ Injetar DTC…")
        b2.clicked.connect(self._inject)
        b3 = QPushButton("📡 Derrubar conexão 10 s")
        b3.clicked.connect(lambda: self.call(lambda: self.adapter and self.adapter.drop_for(10.0)))
        b4 = QPushButton("💡 Acender luz do óleo")
        b4.setToolTip("Acende a luz no painel simulado por 6 s (sem DTC) — para treinar a tecla S")
        b4.clicked.connect(self._oil_light)
        b5 = QPushButton("↺ Restaurar")
        b5.clicked.connect(self._restore)
        for b in (b1, b2, b3, b4, b5):
            grid.addWidget(b)
        lay.addLayout(grid)
        lay.addStretch(1)
        self._scenario_changed()

    # -- ligação com a simulação atual ----------------------------------------
    def attach(self, car, adapter) -> None:
        self.car, self.adapter = car, adapter
        idx = self.scenario.findData(car.scenario.name)
        self.scenario.blockSignals(True)
        self.scenario.setCurrentIndex(max(0, idx))
        self.scenario.blockSignals(False)
        self.desc.setText(car.scenario.description)
        fidx = min(range(len(FACTORS)), key=lambda i: abs(FACTORS[i] - car.time_factor))
        self.factor.blockSignals(True)
        self.factor.setCurrentIndex(fidx)
        self.factor.blockSignals(False)
        for cb, s in self.sliders.values():
            cb.blockSignals(True)
            cb.setChecked(False)
            cb.blockSignals(False)
            s.setEnabled(False)

    def refresh(self) -> None:
        if self.car is None:
            return
        s = self.car.model.snapshot()

        def dot(on, text_on, text_off, color):
            return f"<span style='color:{color if on else theme.DIM}'>●</span> {text_on if on else text_off}"
        self.ind["luz"].setText(dot(s["luz_oleo"], "<b style='color:%s'>ACESA</b>" % theme.RED, "apagada", theme.RED))
        self.ind["apito"].setText(dot(s["apito"], "apitando", "quieto", theme.YELLOW))
        if self.car.model.fan_failed:
            self.ind["vent"].setText(f"<span style='color:{theme.RED}'>✖ FALHOU</span>")
        else:
            self.ind["vent"].setText(dot(s["ventoinha"], "ligada", "desligada", theme.BLUE))
        self.ind["trecho"].setText(f"{s['trecho']} / {s['marcha'] or 'N'}")
        t = int(s["t_sim"])
        self.ind["tsim"].setText(f"{t // 60:02d}:{t % 60:02d}  ({self.car.time_factor:g}x)")
        self.ind["ecu"].setText("respondendo" if s["ecu"] else "dormindo (ignição desligada)")

    # -- ações ----------------------------------------------------------------
    def _scenario_changed(self) -> None:
        name = self.scenario.currentData()
        self.desc.setText(SCENARIOS[name].description)

    def _restart(self) -> None:
        self.restart_requested.emit(self.scenario.currentData(), float(self.factor.currentData()))

    def _factor_changed(self) -> None:
        f = float(self.factor.currentData())
        self.call(lambda: self.car and self.car.set_time_factor(f))

    def _override(self, key: str) -> None:
        cb, s = self.sliders[key]
        s.setEnabled(cb.isChecked())
        on, v = cb.isChecked(), float(s.value())

        def apply():
            if self.car is None:
                return
            if on:
                self.car.model.overrides[key] = v
            else:
                self.car.model.overrides.pop(key, None)
        self.call(apply)

    def _overheat(self) -> None:
        def apply():
            m = self.car.model
            m.fan_failed = True
            m.radiator_eff = 0.12
            m.extra_heat = 0.35
            m.coolant = max(m.coolant, 96.0)
        self.call(apply)

    def _oil_light(self) -> None:
        def apply():
            m = self.car.model
            m.oil_light = m.dash_beep = True
            off_at = m.t + 6.0 * max(1.0, self.car.time_factor)

            def check():  # apaga depois de ~6 s reais
                if m.t >= off_at:
                    m.oil_light = m.dash_beep = False
                    return True
                return False
            self.car.extra_hooks.append(check)
        self.call(apply)

    def _inject(self) -> None:
        items = [f"{c} – {d}" for c, d in sorted(DESCRIPTIONS.items())]
        start = next((i for i, s in enumerate(items) if s.startswith("P0524")), 0)
        item, ok = QInputDialog.getItem(self, "Injetar DTC", "Código (ou digite outro, ex.: P1234):",
                                        items, start, True)
        if not ok or not item:
            return
        code = item.split(" ", 1)[0].strip().upper()
        if len(code) != 5 or code[0] not in "PCBU":
            return
        mode, ok = QInputDialog.getItem(self, "Injetar DTC", "Como o código aparece:",
                                        ["confirmado (03)", "pendente (07)", "permanente (0A)"], 0, False)
        if not ok:
            return

        def apply():
            d = self.car.model.dtcs
            {"c": d.confirmed, "p": d.pending}.get(mode[1], d.permanent).add(code)
        self.call(apply)

    def _restore(self) -> None:
        for cb, _ in self.sliders.values():
            cb.setChecked(False)

        def apply():
            m = self.car.model
            self.car.scenario_enabled = False
            m.overrides.clear()
            m.fan_failed = False
            m.radiator_eff = 1.0
            m.extra_heat = 0.0
            m.oil_extra = 0.0
            m.oil_light = m.dash_beep = False
            m.dtcs.confirmed.clear()
            m.dtcs.pending.clear()
            m.dtcs.permanent.clear()
            m.ecu_awake = m.ignition_on = m.engine_on = True
            if self.adapter:
                self.adapter.link_down_until = 0
                self.adapter.stall_until = 0
                self.adapter.stop()
        self.call(apply)
