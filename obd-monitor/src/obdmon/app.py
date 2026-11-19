"""Ponto de entrada.

    python -m obdmon                      carro real (Bluetooth LE)
    python -m obdmon --sim                simulador (cenário "normal")
    python -m obdmon --sim --cenario superaquecimento --fator 10
    python -m obdmon --selftest           autoteste no console (carro parado, motor ligado)
    python -m obdmon --endereco AA:BB:CC:DD:EE:FF
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from .config import APP_DIR, Config


def _setup_logging(logs: Path) -> None:
    logs.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    try:
        handlers.append(logging.FileHandler(logs / "programa.log", encoding="utf-8"))
    except OSError:
        pass
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=handlers)
    logging.getLogger("bleak").setLevel(logging.WARNING)


def parse_args(argv=None):
    from .sim.scenarios import SCENARIOS
    ap = argparse.ArgumentParser(prog="obdmon", description="Monitor OBD2 (ELM327 Bluetooth LE) – somente leitura")
    ap.add_argument("--sim", action="store_true", help="usar o simulador de carro (sem hardware)")
    ap.add_argument("--cenario", default=None, choices=sorted(SCENARIOS), help="cenário do simulador")
    ap.add_argument("--fator", type=float, default=None, help="fator de tempo do simulador (1, 5, 10...)")
    ap.add_argument("--selftest", action="store_true", help="autoteste no console e sai")
    ap.add_argument("--endereco", default="", help="endereço Bluetooth do adaptador")
    ap.add_argument("--listar", action="store_true", help="procura adaptadores BLE e sai")
    ap.add_argument("--auto-sair", type=float, default=0, help=argparse.SUPPRESS)  # testes: fecha após N s
    return ap.parse_args(argv)


async def _pick_address_cli(cfg: Config, given: str) -> str:
    from .transport.ble import scan_adapters
    addr = given or cfg.preferred_address or cfg.last_address
    if addr:
        return addr
    print("Procurando adaptador Bluetooth LE (8 s)...")
    devs = await scan_adapters(8.0)
    cands = [d for d in devs if d.candidate]
    if not cands:
        print("Nenhum adaptador encontrado. Dispositivos vistos:")
        for d in devs:
            print(f"  {d.name}  {d.address}")
        print("Use --endereco ENDERECO se o seu aparecer com outro nome.")
        return ""
    if len(cands) == 1:
        d = cands[0]
    else:
        for i, d in enumerate(cands, 1):
            print(f"  {i}) {d.name}  {d.address}")
        while True:
            r = input("Número do seu adaptador: ").strip()
            if r.isdigit() and 1 <= int(r) <= len(cands):
                d = cands[int(r) - 1]
                break
    print(f"Usando {d.name} ({d.address})")
    cfg.last_address, cfg.last_name = d.address, d.name
    cfg.save()
    return d.address


def run_selftest_cli(args, cfg: Config) -> int:
    from .selftest import run_cli

    async def go() -> int:
        if args.sim:
            from .sim.fake_elm import SimAdapter, SimCar
            from .transport.simulated import SimulatedTransport
            t = SimulatedTransport(SimAdapter(SimCar(args.cenario or "sem_oleo_pid", time_factor=args.fator)))
        else:
            from .transport.ble import BleTransport
            addr = await _pick_address_cli(cfg, args.endereco)
            if not addr:
                return 2
            t = BleTransport(addr)
        try:
            return await run_cli(t, cfg.logs_path, args.sim)
        except Exception as e:
            print(f"\nFalha no autoteste: {e}")
            return 1
    return asyncio.run(go())


def run_list_cli() -> int:
    from .transport.ble import scan_adapters
    devs = asyncio.run(scan_adapters(8.0))
    for d in devs:
        print(f"{'*' if d.candidate else ' '} {d.name:<30} {d.address}  {d.rssi if d.rssi is not None else ''}")
    return 0


def run_gui(args, cfg: Config) -> int:
    from PySide6.QtWidgets import QApplication

    from .engine import AsyncRunner
    from .ui import theme
    from .ui.main_window import MainWindow

    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("obdmon.monitor.c3")
        except Exception:
            pass
    app = QApplication(sys.argv[:1])
    app.setApplicationName("Monitor OBD2")
    app.setStyle("Fusion")
    app.setStyleSheet(theme.STYLESHEET)
    app.setWindowIcon(theme.make_icon())
    runner = AsyncRunner()
    scen = args.cenario or cfg.sim_scenario or "normal"
    w = MainWindow(cfg, runner, simulated=args.sim, scenario=scen, factor=args.fator, address=args.endereco)
    w.showMaximized()
    if args.auto_sair:
        from PySide6.QtCore import QTimer
        QTimer.singleShot(int(args.auto_sair * 1000), w.close)
    code = app.exec()
    runner.shutdown()
    return code


def main(argv=None) -> int:
    args = parse_args(argv)
    cfg = Config.load()
    _setup_logging(cfg.logs_path)
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    if args.listar:
        return run_list_cli()
    if args.selftest:
        return run_selftest_cli(args, cfg)
    return run_gui(args, cfg)


__all__ = ["main", "APP_DIR"]
