"""ELM327 FALSO em nível de bytes + "adaptador" virtual plugado no carro simulado.

Recebe ``ATZ\\r``, ``010C\\r``, ``03\\r``... e responde exatamente no formato do
adaptador real (``41 0C 1A F8 \\r\\r>``), incluindo SEARCHING..., NO DATA,
UNABLE TO CONNECT, eco (ATE1), espaços (ATS0/1), multi-frame CAN e '?'.
"""
from __future__ import annotations

import asyncio
import random
import time
from typing import Callable, Optional

from .car_model import CarModel, dtc_to_bytes
from .scenarios import Scenario, make_scenario


class SimCar:
    """Carro simulado: modelo + cenário + relógio (tempo real × fator)."""

    def __init__(self, scenario: Scenario | str = "normal", time_factor: Optional[float] = None,
                 seed: Optional[int] = None) -> None:
        self.scenario = make_scenario(scenario) if isinstance(scenario, str) else scenario
        self.rng = random.Random(seed)
        self.model = CarModel(self.rng)
        self.scenario.setup(self.model)
        self.time_factor = float(time_factor if time_factor is not None else self.scenario.default_factor)
        self._last_real = time.monotonic()
        self.scenario_enabled = True          # "Restaurar" no painel desliga o roteiro do cenário
        self.extra_hooks: list[Callable[[], None]] = []

    def sync(self) -> None:
        now = time.monotonic()
        dt_real = now - self._last_real
        self._last_real = now
        dt = max(0.0, min(dt_real, 5.0)) * self.time_factor
        while dt > 1e-9:
            step = min(dt, 0.5)
            self.model.advance(step)
            if self.scenario_enabled:
                self.scenario.update(self.model)
            for hook in list(self.extra_hooks):
                if hook():  # True = terminou, remove
                    self.extra_hooks.remove(hook)
            dt -= step

    def set_time_factor(self, f: float) -> None:
        self.sync()
        self.time_factor = max(0.1, float(f))


class FakeELM:
    def __init__(self, car: SimCar, search_delay: tuple[float, float] = (1.2, 2.5)) -> None:
        self.car = car
        self.search_delay = search_delay
        self.received: list[str] = []
        self.reset()

    def reset(self) -> None:
        self.echo = True
        self.linefeed = False
        self.spaces = True
        self.headers = False
        self.protocol_auto = True
        self.connected_protocol = False

    # -- formatação -----------------------------------------------------------
    @property
    def eol(self) -> str:
        return "\r\n" if self.linefeed else "\r"

    def _fmt(self, data: list[int]) -> str:
        if self.spaces:
            return "".join(f"{b:02X} " for b in data)
        return "".join(f"{b:02X}" for b in data)

    def _pack(self, lines: list[str], cmd: str) -> str:
        out = ""
        if self.echo:
            out += cmd + self.eol
        out += "".join(s + self.eol for s in lines)
        out += self.eol + ">"
        return out

    # -- processamento --------------------------------------------------------
    def handle(self, raw_cmd: str) -> tuple[str, float]:
        """Devolve (texto da resposta, atraso extra em segundos reais)."""
        self.received.append(raw_cmd)
        cmd = raw_cmd.strip().upper().replace(" ", "")
        if not cmd:
            return self._pack([], raw_cmd), 0.0
        if cmd.startswith("AT"):
            return self._at(cmd, raw_cmd)
        if all(c in "0123456789ABCDEF" for c in cmd) and len(cmd) % 2 == 0:
            return self._obd(cmd, raw_cmd)
        return self._pack(["?"], raw_cmd), 0.0

    def _at(self, cmd: str, raw: str) -> tuple[str, float]:
        a = cmd[2:]
        ok = ["OK"]
        if a == "Z" or a == "WS":
            echoed = (raw + "\r") if self.echo else ""  # o eco sai antes do reset
            self.reset()
            return echoed + "\r\rELM327 v1.5\r\r>", 0.6
        if a == "D":
            self.reset()
            return self._pack(ok, raw), 0.0
        if a == "I":
            return self._pack(["ELM327 v1.5"], raw), 0.0
        if a == "@1":
            return self._pack(["OBDII to RS232 Interpreter"], raw), 0.0
        if a == "RV":
            self.car.sync()
            v = self.car.model.voltage if self.car.model.ignition_on else 12.3
            return self._pack([f"{v:.1f}V"], raw), 0.0
        if a in ("E0", "E1"):
            self.echo = a == "E1"
            return self._pack(ok, raw), 0.0
        if a in ("L0", "L1"):
            self.linefeed = a == "L1"
            return self._pack(ok, raw), 0.0
        if a in ("S0", "S1"):
            self.spaces = a == "S1"
            return self._pack(ok, raw), 0.0
        if a in ("H0", "H1"):
            self.headers = a == "H1"
            return self._pack(ok, raw), 0.0
        if a.startswith("SP"):
            self.protocol_auto = True
            self.connected_protocol = False
            return self._pack(ok, raw), 0.0
        if a.startswith("ST") or a.startswith("AT"):
            return self._pack(ok, raw), 0.0
        if a == "DP":
            p = "ISO 15765-4 (CAN 11/500)" if self.connected_protocol else "AUTO"
            return self._pack([("AUTO, " + p) if self.connected_protocol else p], raw), 0.0
        if a == "DPN":
            return self._pack(["A6" if self.connected_protocol else "A0"], raw), 0.0
        return self._pack(["?"], raw), 0.0

    def _obd(self, cmd: str, raw: str) -> tuple[str, float]:
        car = self.car
        car.sync()
        m = car.model
        pre: list[str] = []
        delay = 0.0
        if not self.connected_protocol:
            pre.append("SEARCHING...")
            delay = car.rng.uniform(*self.search_delay)
            if not m.ecu_awake:
                return self._pack(pre + ["UNABLE TO CONNECT"], raw), delay
            self.connected_protocol = True
        if not m.ecu_awake:
            return self._pack(["NO DATA"], raw), 0.15
        mode = int(cmd[:2], 16)
        if mode == 0x01 and len(cmd) == 4:
            pid = int(cmd[2:4], 16)
            data = m.read_pid(pid)
            if data is None:
                return self._pack(pre + ["NO DATA"], raw), delay + 0.1
            return self._pack(pre + [self._fmt([0x41, pid] + data)], raw), delay
        if mode in (0x03, 0x07, 0x0A) and len(cmd) == 2:
            codes = m.read_dtcs(mode)
            payload = [0x40 + mode, len(codes)]
            for c in codes:
                payload += list(dtc_to_bytes(c))
            return self._pack(pre + self._can_frames(payload), raw), delay
        if mode == 0x09:
            return self._pack(pre + ["NO DATA"], raw), delay
        # Qualquer outro modo: o fake responde como uma ECU que não aceita.
        return self._pack(pre + ["NO DATA"], raw), delay

    def _can_frames(self, payload: list[int]) -> list[str]:
        if len(payload) <= 7:
            return [self._fmt(payload)]
        lines = [f"{len(payload):03X}"]
        first, rest = payload[:6], payload[6:]
        lines.append("0:" + (" " if self.spaces else "") + self._fmt(first))
        n = 1
        while rest:
            chunk, rest = rest[:7], rest[7:]
            chunk = chunk + [0x00] * (7 - len(chunk))
            lines.append(f"{n % 16:X}:" + (" " if self.spaces else "") + self._fmt(chunk))
            n += 1
        return lines


class SimAdapter:
    """O "adaptador" virtual plugado no carro. Sobrevive às reconexões do
    transporte (assim como o adaptador real continua no carro)."""

    def __init__(self, car: SimCar, latency: tuple[float, float] = (0.03, 0.09),
                 chunk_gap: tuple[float, float] = (0.001, 0.006), chunk_size: int = 20,
                 search_delay: tuple[float, float] = (1.2, 2.5)) -> None:
        self.car = car
        self.elm = FakeELM(car, search_delay=search_delay)
        self.latency = latency
        self.chunk_gap = chunk_gap
        self.chunk_size = chunk_size
        self.link_down_until = 0.0
        self.stall_until = 0.0
        self.faults = car.scenario.faults
        self._listeners: list[Callable[[], None]] = []
        self._fault_task: Optional[asyncio.Task] = None
        self.drops = 0
        self.stalls = 0

    @property
    def rng(self) -> random.Random:
        return self.car.rng

    def link_up(self) -> bool:
        return time.monotonic() >= self.link_down_until

    def stalled(self) -> bool:
        return time.monotonic() < self.stall_until

    def add_drop_listener(self, cb: Callable[[], None]) -> None:
        self._listeners.append(cb)

    def remove_drop_listener(self, cb: Callable[[], None]) -> None:
        if cb in self._listeners:
            self._listeners.remove(cb)

    def drop_for(self, seconds: float) -> None:
        """Derruba o "Bluetooth" por N segundos reais."""
        self.drops += 1
        self.link_down_until = time.monotonic() + seconds
        for cb in list(self._listeners):
            cb()

    def stall_for(self, seconds: float) -> None:
        self.stalls += 1
        self.stall_until = time.monotonic() + seconds

    def ensure_fault_task(self) -> None:
        if self.faults.drop_every and (self._fault_task is None or self._fault_task.done()):
            self._fault_task = asyncio.get_running_loop().create_task(self._fault_loop())

    async def _fault_loop(self) -> None:
        f = self.faults
        await asyncio.sleep(f.first_drop_after)
        while True:
            dur = self.rng.uniform(*f.drop_duration)
            if self.rng.random() < f.stall_prob:
                self.stall_for(dur)
            else:
                self.drop_for(dur)
            await asyncio.sleep(dur + self.rng.uniform(*f.drop_every))

    def stop(self) -> None:
        if self._fault_task:
            self._fault_task.cancel()
