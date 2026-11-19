"""Diálogo para escolher o adaptador Bluetooth LE."""
from __future__ import annotations

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (QCheckBox, QDialog, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
                               QPushButton, QVBoxLayout)

from ..transport.ble import scan_adapters


class _Relay(QObject):
    done = Signal(object, str)


class ScanDialog(QDialog):
    def __init__(self, runner, parent=None) -> None:
        super().__init__(parent)
        self.runner = runner
        self.setWindowTitle("Escolher adaptador OBD2 (Bluetooth LE)")
        self.resize(520, 420)
        self.devices = []
        self.selected = None
        lay = QVBoxLayout(self)
        self.info = QLabel("Procurando adaptadores... (8 s)\nConfira: adaptador no carro, ignição ligada, "
                           "app do celular FECHADO (o adaptador só aceita uma conexão).")
        self.info.setWordWrap(True)
        lay.addWidget(self.info)
        self.list = QListWidget()
        self.list.itemDoubleClicked.connect(lambda *_: self.accept_sel())
        lay.addWidget(self.list, 1)
        self.show_all = QCheckBox("Mostrar todos os dispositivos BLE")
        self.show_all.toggled.connect(self._fill)
        lay.addWidget(self.show_all)
        row = QHBoxLayout()
        self.again = QPushButton("Procurar de novo")
        self.again.clicked.connect(self.scan)
        ok = QPushButton("Conectar")
        ok.setObjectName("primary")
        ok.clicked.connect(self.accept_sel)
        cancel = QPushButton("Cancelar")
        cancel.clicked.connect(self.reject)
        row.addWidget(self.again)
        row.addStretch(1)
        row.addWidget(cancel)
        row.addWidget(ok)
        lay.addLayout(row)
        self.relay = _Relay()
        self.relay.done.connect(self._done)
        self.scan()

    def scan(self) -> None:
        self.again.setEnabled(False)
        self.list.clear()
        self.info.setText("Procurando adaptadores... (8 s)")
        fut = self.runner.submit(scan_adapters(8.0))

        def cb(f):
            try:
                self.relay.done.emit(f.result(), "")
            except Exception as e:
                self.relay.done.emit([], str(e))
        fut.add_done_callback(cb)

    def _done(self, devices, err: str) -> None:
        self.again.setEnabled(True)
        self.devices = devices
        cands = [d for d in devices if d.candidate]
        if err:
            self.info.setText(f"Erro: {err}\nO Bluetooth do notebook está ligado?")
        elif not devices:
            self.info.setText("Nenhum dispositivo encontrado. Confira se o Bluetooth está ligado e a ignição também.")
        elif not cands:
            self.info.setText("Nenhum adaptador com nome de OBD encontrado. Marque \"Mostrar todos\" e tente "
                              "reconhecer o seu (ex.: \"OBDII\").")
            self.show_all.setChecked(True)
        else:
            self.info.setText(f"{len(cands)} adaptador(es) encontrado(s). Escolha o seu e clique em Conectar.")
        self._fill()
        if len(cands) == 1 and not err:
            self.list.setCurrentRow(0)

    def _fill(self) -> None:
        self.list.clear()
        for d in self.devices:
            if not d.candidate and not self.show_all.isChecked():
                continue
            rssi = f"   sinal {d.rssi} dBm" if d.rssi is not None else ""
            it = QListWidgetItem(f"{d.name}   [{d.address}]{rssi}")
            it.setData(Qt.ItemDataRole.UserRole, d)
            self.list.addItem(it)

    def accept_sel(self) -> None:
        it = self.list.currentItem()
        if it is None:
            return
        self.selected = it.data(Qt.ItemDataRole.UserRole)
        self.accept()
