"""Orquestra tudo: poller + alertas + log, num tique fixo de 0,5 s.

Não depende de Qt. A GUI recebe mensagens pelo ``listener(kind, payload)``,
chamado na thread do asyncio (a ponte Qt converte em sinais).
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

from . import events as ev
from .config import Config
from .events import AlertDetector, Event
from .logger import TripLogger
from .pids import BY_KEY, FAST, MEDIUM, SLOW, VALUE_KEYS
from .poller import (ST_CONNECTED, ST_CONNECTING, ST_INIT, ST_RECONNECT_WAIT, ST_SELFTEST,
                     ST_STOPPED, ST_WAIT_ECU, WATCHDOG_WARN_S, Poller)
from .transport.base import Transport

log = logging.getLogger(__name__)

MAX_AGE = {FAST: 1.5, "rebaixado": 3.0, MEDIUM: 6.0, SLOW: 90.0}

Listener = Callable[[str, Any], None]

# Estados do "badge" de conexão
LINK_OK = "ok"
LINK_UNSTABLE = "instavel"
LINK_LOST = "perdida"
LINK_CONNECTING = "conectando"
LINK_STOPPED = "parado"


class Engine:
    def __init__(self, cfg: Config, transport_factory: Callable[[], Transport], *,
                 simulated: bool = False, sim_car=None, sim_adapter=None,
                 logs_dir: Optional[Path] = None, listener: Optional[Listener] = None,
                 search_timeout: float = 12.0, backoff: Optional[tuple] = None,
                 fsync: bool = True) -> None:
        self.cfg = cfg
        self.simulated = simulated
        self.sim_car = sim_car
        self.sim_adapter = sim_adapter
        self.listener = listener or (lambda k, p: None)
        self.logs_dir = Path(logs_dir) if logs_dir else cfg.logs_path
        self.fsync = fsync
        kw = {}
        if backoff:
            kw["backoff"] = backoff
        self.poller = Poller(transport_factory, budget=cfg.log_interval,
                             on_event=self._on_poller_event, on_status=self._on_status,
                             on_dtc=lambda t: self._emit("dtc", t), on_values=self._on_values,
                             search_timeout=search_timeout, **kw)
        self.alerts = AlertDetector(cfg)
        self.logger: Optional[TripLogger] = None
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self._stop = asyncio.Event()
        self._pending: list[tuple[str, str, bool]] = []
        self._notes: list[str] = []
        self.t0 = 0.0
        self.start_wall: Optional[datetime] = None
        self.rows_written = 0
        self.events: list[Event] = []
        self.last_row: Optional[dict] = None
        self._last_live = 0.0
        self._last_diag = 0.0
        self._last_mark = 0.0
        self.running = False
        self.selftest_running = False
        self.finished = asyncio.Event()

    # -- mensagens ------------------------------------------------------------
    def _emit(self, kind: str, payload: Any = None) -> None:
        try:
            self.listener(kind, payload)
        except Exception:
            log.exception("listener falhou (%s)", kind)

    def _on_poller_event(self, code: str, detail: str) -> None:
        self._pending.append((code, detail, False))

    def _on_status(self, st: str, detail: str) -> None:
        self._emit("status", (st, detail))

    def _on_values(self) -> None:
        now = time.monotonic()
        if now - self._last_live >= 0.1:
            self._last_live = now
            self._emit("live", self.current_values())

    # -- API pública (thread do asyncio) ---------------------------------------
    def mark_oil_light(self, note: str = "") -> bool:
        now = time.monotonic()
        if now - self._last_mark < 1.0:
            return False  # tecla repetida / duplo clique
        self._last_mark = now
        self._pending.append((ev.LUZ_OLEO_MANUAL, note, True))
        return True

    def add_note(self, note: str) -> None:
        self._notes.append(note)

    def stop(self) -> None:
        self._stop.set()

    # -- valores ----------------------------------------------------------------
    def _max_age(self, key: str) -> float:
        d = BY_KEY[key]
        if d.pid in self.poller.demoted:
            return MAX_AGE["rebaixado"]
        return MAX_AGE[d.cycle]

    def current_values(self) -> dict:
        if self.poller.status not in (ST_CONNECTED, ST_SELFTEST):
            return {k: None for k in VALUE_KEYS}
        return {k: self.poller.current(k, self._max_age(k)) for k in VALUE_KEYS}

    def link_state(self) -> str:
        st = self.poller.status
        if st == ST_STOPPED:
            return LINK_STOPPED
        if st in (ST_CONNECTED, ST_SELFTEST):
            age = self.poller.data_age
            return LINK_UNSTABLE if age is not None and age > WATCHDOG_WARN_S else LINK_OK
        if st == ST_WAIT_ECU:
            return LINK_UNSTABLE
        if self.poller.lost or st == ST_RECONNECT_WAIT:
            return LINK_LOST
        return LINK_CONNECTING

    def avg_latency_ms(self) -> Optional[float]:
        lats = [self.poller.stats[p].lat_ewma for p in self.poller.fast if self.poller.stats[p].lat_ewma]
        return sum(lats) / len(lats) * 1000 if lats else None

    def unsupported_keys(self) -> list[str]:
        out = []
        for st in self.poller.stats.values():
            if not st.usable:
                out.append(next(d.key for d in BY_KEY.values() if d.pid == st.pid))
        return out

    # -- laço principal ---------------------------------------------------------
    async def run(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.running = True
        self.start_wall = datetime.now()
        self.t0 = time.monotonic()
        self.logger = TripLogger(self.logs_dir, simulated=self.simulated, start=self.start_wall,
                                 interval=self.cfg.log_interval, fsync=self.fsync,
                                 on_error=lambda m: self._emit("log_error", m))
        try:
            self.logger.open()
        except Exception as e:
            self._emit("log_error", f"Não foi possível criar o log: {e}")
            raise
        self._emit("trip", {"csv": str(self.logger.csv_path), "dir": str(self.logs_dir),
                            "start": self.start_wall, "simulated": self.simulated})
        self._pending.insert(0, (ev.INICIO_VIAGEM, "simulador" if self.simulated else "", False))
        poll_task = asyncio.create_task(self.poller.run(), name="poller")
        try:
            await self._tick_loop(poll_task)
        finally:
            self._pending.append((ev.FIM_VIAGEM, "", False))
            self._tick(final=True)
            self.poller.stop()
            try:
                await asyncio.wait_for(poll_task, 6.0)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                poll_task.cancel()
            if self.sim_adapter is not None:
                self.sim_adapter.stop()
            extra = {"Conexões": self.poller.connections,
                     "Taxa média do ciclo rápido": f"{self.poller.hz:.1f} Hz".replace(".", ",")}
            await asyncio.get_running_loop().run_in_executor(None, self.logger.close, extra)
            self.running = False
            self._emit("stopped", {"summary": str(self.logger.summary_path), "csv": str(self.logger.csv_path)})
            self.finished.set()

    async def _tick_loop(self, poll_task: asyncio.Task) -> None:
        interval = self.cfg.log_interval
        next_t = time.monotonic()
        while not self._stop.is_set():
            if poll_task.done() and poll_task.exception() is not None:
                # nunca deveria acontecer (ex.: comando proibido); registra e para
                self._emit("log_error", f"Leitura parou: {poll_task.exception()!r}")
                raise poll_task.exception()
            self._tick()
            next_t += interval
            delay = next_t - time.monotonic()
            if delay < 0:            # atrasou (PC ocupado): realinha sem acumular
                next_t = time.monotonic()
                delay = 0
            try:
                await asyncio.wait_for(self._stop.wait(), delay)
            except asyncio.TimeoutError:
                pass

    def _tick(self, final: bool = False) -> None:
        mono = time.monotonic()
        wall = datetime.now()
        t_s = mono - self.t0
        values = self.current_values()
        events: list[Event] = []
        pending, self._pending = self._pending, []
        for code, detail, crit in pending:
            events.append(Event(code, wall, t_s, detail, crit, dict(values)))
        if not final:
            for code, detail, crit in self.alerts.check(values):
                events.append(Event(code, wall, t_s, detail, crit, dict(values)))
        notes = list(self._notes)
        self._notes.clear()
        for e in events:
            if e.kind == ev.LUZ_OLEO_MANUAL and e.detail:
                notes.append(e.detail)
        if self.poller.status == ST_SELFTEST:
            notes.append("AUTOTESTE")
        row = {
            "t_wall": wall, "t_s": t_s, "values": values,
            "dtcs": self.poller.active_codes(),
            "events": [e.code for e in events], "note": " / ".join(notes),
        }
        assert self.logger is not None
        self.logger.log_tick(row, events)
        self.rows_written += 1
        self.last_row = row
        self.events.extend(events)
        lost_for = None
        if self.poller.lost and self.poller.lost_since:
            lost_for = mono - self.poller.lost_since
        elif self.poller.data_age is not None and self.link_state() != LINK_OK:
            lost_for = self.poller.data_age
        self._emit("tick", {
            "row": row, "events": events, "status": self.poller.status,
            "status_detail": self.poller.status_detail, "link": self.link_state(),
            "age": self.poller.data_age, "lost_for": lost_for, "hz": self.poller.hz,
            "latency_ms": self.avg_latency_ms(), "alerts": dict(self.alerts.active),
            "unsupported": self.unsupported_keys(), "attempt": self.poller.reconnect_attempt,
        })
        if mono - self._last_diag > 2.0 or final:
            self._last_diag = mono
            self._emit("diag", self.poller.diagnostics())

    # -- autoteste com o monitor rodando --------------------------------------
    async def run_selftest(self, progress: Callable[[str], None] = lambda s: None):
        from .selftest import selftest_core, save_report
        if self.selftest_running:
            return None
        self.selftest_running = True
        try:
            if not await self.poller.pause():
                return "Autoteste indisponível: é preciso estar conectado ao carro."
            try:
                res = await selftest_core(self.poller.elm, self.poller.elm_info, self.poller.supported,
                                          progress, transport_info=self.poller.transport_info,
                                          simulated=self.simulated)
            finally:
                self.poller.resume()
            path = save_report(res, self.logs_dir, self.simulated)
            res.text += f"\nRelatório salvo em: {path}\n"
            return res.text
        finally:
            self.selftest_running = False


# -----------------------------------------------------------------------------
# Construção (real / simulado)
# -----------------------------------------------------------------------------

def build_sim_engine(cfg: Config, scenario: str = "normal", time_factor: Optional[float] = None,
                     listener: Optional[Listener] = None, logs_dir: Optional[Path] = None,
                     latency: Optional[tuple] = None, search_delay: Optional[tuple] = None,
                     seed: Optional[int] = None, faults=None, **kw) -> Engine:
    from .sim.fake_elm import SimAdapter, SimCar
    from .transport.simulated import make_sim_factory
    car = SimCar(scenario, time_factor=time_factor, seed=seed)
    if faults is not None:
        car.scenario.faults = faults
    akw = {}
    if latency:
        akw["latency"] = latency
    if search_delay:
        akw["search_delay"] = search_delay
    adapter = SimAdapter(car, **akw)
    return Engine(cfg, make_sim_factory(adapter), simulated=True, sim_car=car, sim_adapter=adapter,
                  listener=listener, logs_dir=logs_dir, **kw)


def build_ble_engine(cfg: Config, address: str, listener: Optional[Listener] = None,
                     logs_dir: Optional[Path] = None, **kw) -> Engine:
    from .transport.ble import BleTransport
    return Engine(cfg, lambda: BleTransport(address), simulated=False, listener=listener,
                  logs_dir=logs_dir, **kw)


class AsyncRunner:
    """Uma thread dedicada com o loop asyncio (BLE + leitura + log).

    No Windows, rodar o bleak fora da thread da GUI evita o conflito de
    apartamento COM (STA) do Qt."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, name="obd-asyncio", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def submit(self, coro) -> concurrent.futures.Future:
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def call(self, fn, *args) -> None:
        self.loop.call_soon_threadsafe(fn, *args)

    def shutdown(self, timeout: float = 3.0) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(timeout)


__all__ = ["Engine", "AsyncRunner", "build_sim_engine", "build_ble_engine",
           "LINK_OK", "LINK_UNSTABLE", "LINK_LOST", "LINK_CONNECTING", "LINK_STOPPED",
           "ST_CONNECTING", "ST_INIT"]
