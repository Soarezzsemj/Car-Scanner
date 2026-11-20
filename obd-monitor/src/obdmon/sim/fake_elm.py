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


# Perfis de adaptador simulado (--perfil-adaptador)
#   bom            : ELM327 v1.5 "bem comportado": sufixo e multi-PID funcionam
#   clone_ruim     : não aceita multi-PID (responde só o 1º PID) e erra de vez em quando
#   clone_interrog : responde "?" a qualquer multi-PID
#   clone_instavel : multi-PID funciona no começo e depois passa a vir incompleto às vezes
#   sem_sufixo     : responde "?" ao sufixo de respostas esperadas (ELM antigo)
PROFILES = ("bom", "clone_ruim", "clone_interrog", "clone_instavel", "sem_sufixo")


class FakeELM:
    """Modelo de tempo (segundos reais):

    - a ECU leva ``ecu_time`` para responder (+ ~1,5 ms por PID extra e
      +2 ms por frame CAN adicional);
    - SEM o sufixo de respostas esperadas, o ELM327 continua esperando outras
      ECUs depois da resposta: ATAT0 = timeout cheio (ATST x 4 ms, padrão
      0x32 ≈ 200 ms); ATAT1 (padrão) ≈ 4 x tempo da ECU + 40 ms; ATAT2 ≈
      2 x tempo da ECU + 15 ms — sempre limitado pelo ATST;
    - COM o sufixo ("010C1") ele devolve o '>' logo depois da resposta;
    - um ATST menor que o tempo de resposta da ECU dá NO DATA (por isso o
      benchmark confere a taxa de erros de cada combinação).
    O tempo do rádio BLE fica no transporte simulado (SimAdapter.latency).
    """

    def __init__(self, car: SimCar, search_delay: tuple[float, float] = (1.2, 2.5),
                 profile: str = "bom", ecu_time: tuple[float, float] = (0.010, 0.020),
                 timing_model: bool = True) -> None:
        if profile not in PROFILES:
            raise ValueError(f"Perfil de adaptador desconhecido: {profile}. Opções: {', '.join(PROFILES)}")
        self.car = car
        self.search_delay = search_delay
        self.profile = profile
        self.ecu_time = ecu_time
        self.timing_model = timing_model
        self.received: list[str] = []
        self.multi_requests = 0
        self.suffix_requests = 0
        self.reset()

    def reset(self) -> None:
        self.echo = True
        self.linefeed = False
        self.spaces = True
        self.headers = False
        self.protocol_auto = True
        self.connected_protocol = False
        self.adaptive = 1
        self.st = 0x32
        self.caf = True

    @property
    def rng(self) -> random.Random:
        return self.car.rng

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
        parts = self.handle_parts(raw_cmd)
        return "".join(t for t, _ in parts), sum(d for _, d in parts)

    def handle_parts(self, raw_cmd: str) -> list[tuple[str, float]]:
        """Resposta em partes [(texto, atraso ANTES deste texto)]: os dados
        saem assim que a ECU responde e o '>' só depois da espera do timeout."""
        self.received.append(raw_cmd)
        cmd = raw_cmd.strip().upper().replace(" ", "")
        if not cmd:
            return [(self._pack([], raw_cmd), 0.0)]
        if cmd.startswith("AT"):
            text, d = self._at(cmd, raw_cmd)
            return [(text, d)]
        if all(c in "0123456789ABCDEF" for c in cmd) and len(cmd) >= 2:
            return self._obd(cmd, raw_cmd)
        return [(self._pack(["?"], raw_cmd), 0.0)]

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
        if a in ("CAF0", "CAF1"):
            self.caf = a == "CAF1"
            return self._pack(ok, raw), 0.0
        if a in ("AT0", "AT1", "AT2"):
            self.adaptive = int(a[2])
            return self._pack(ok, raw), 0.0
        if a.startswith("ST") and len(a) == 4:
            try:
                self.st = int(a[2:], 16)
            except ValueError:
                return self._pack(["?"], raw), 0.0
            return self._pack(ok, raw), 0.0
        if a.startswith("SP"):
            n = a[2:]
            if n == "0" or n.startswith("A"):
                self.protocol_auto = True
                self.connected_protocol = False
            elif n == "6":                      # o protocolo do carro simulado (CAN 11/500)
                self.protocol_auto = False
                self.connected_protocol = True
            else:                               # protocolo errado: o carro não responde
                self.protocol_auto = False
                self.connected_protocol = False
            return self._pack(ok, raw), 0.0
        if a == "DP":
            if not self.protocol_auto:
                return self._pack(["ISO 15765-4 (CAN 11/500)"], raw), 0.0
            p = "ISO 15765-4 (CAN 11/500)" if self.connected_protocol else "AUTO"
            return self._pack([("AUTO, " + p) if self.connected_protocol else p], raw), 0.0
        if a == "DPN":
            if not self.protocol_auto:
                return self._pack(["6"], raw), 0.0
            return self._pack(["A6" if self.connected_protocol else "A0"], raw), 0.0
        return self._pack(["?"], raw), 0.0

    # -- tempo ----------------------------------------------------------------
    def _ecu_delay(self, extra_pids: int = 0, frames: int = 1) -> float:
        if not self.timing_model:
            return 0.0
        return self.rng.uniform(*self.ecu_time) + 0.0015 * extra_pids + 0.002 * max(0, frames - 1)

    def _wait_after(self, ecu: float, suffix: int) -> float:
        """Quanto o ELM espera DEPOIS da resposta antes de mandar o '>'."""
        if not self.timing_model or suffix:
            return 0.0
        st = self.st * 0.004
        if self.adaptive == 0:
            return st
        if self.adaptive == 1:
            return min(st, 4 * ecu + 0.040)
        return min(st, 2 * ecu + 0.015)

    def _too_slow(self, ecu: float) -> bool:
        """ATST menor que o tempo da ECU: o ELM desiste antes da resposta."""
        return self.timing_model and self.st * 0.004 < ecu

    # -- OBD ------------------------------------------------------------------
    def _obd(self, cmd: str, raw: str) -> list[tuple[str, float]]:
        car = self.car
        car.sync()
        m = car.model
        suffix = 0
        if len(cmd) % 2:                       # dígito final = respostas esperadas
            if cmd[:2] != "01" or self.profile == "sem_sufixo" or cmd[-1] == "0":
                return [(self._pack(["?"], raw), 0.0)]
            suffix = int(cmd[-1], 16)
            cmd = cmd[:-1]
            self.suffix_requests += 1
        pre: list[str] = []
        delay = 0.0
        if not self.connected_protocol:
            if not self.protocol_auto:          # protocolo fixo errado
                return [(self._pack(["UNABLE TO CONNECT"], raw), 0.3)]
            pre.append("SEARCHING...")
            delay = car.rng.uniform(*self.search_delay)
            if not m.ecu_awake:
                return [(self._pack(pre + ["UNABLE TO CONNECT"], raw), delay)]
            self.connected_protocol = True
        if not m.ecu_awake:
            return [(self._pack(["NO DATA"], raw), 0.15)]
        mode = int(cmd[:2], 16)
        st_s = self.st * 0.004
        if mode == 0x01 and len(cmd) >= 4:
            pids = [int(cmd[i:i + 2], 16) for i in range(2, len(cmd), 2)]
            if len(pids) > 6:
                return [(self._pack(["?"], raw), 0.0)]
            if len(pids) > 1:
                return self._multi(pids, pre, delay, suffix, raw)
            pid = pids[0]
            ecu = self._ecu_delay()
            if self._too_slow(ecu):
                return [(self._pack(pre + ["NO DATA"], raw), delay + st_s)]
            if self.profile == "clone_ruim" and pid not in (0x00, 0x20, 0x40, 0x60) and self.rng.random() < 0.01:
                return [(self._pack(pre + ["NO DATA"], raw), delay + ecu + st_s)]   # erro ocasional
            data = m.read_pid(pid)
            if data is None:
                return [(self._pack(pre + ["NO DATA"], raw), delay + max(st_s, 0.1))]
            return self._split(pre + [self._fmt([0x41, pid] + data)], raw, delay + ecu, self._wait_after(ecu, suffix))
        if mode in (0x03, 0x07, 0x0A) and len(cmd) == 2:
            codes = m.read_dtcs(mode)
            payload = [0x40 + mode, len(codes)]
            for c in codes:
                payload += list(dtc_to_bytes(c))
            frames = self._can_frames(payload)
            ecu = self._ecu_delay(frames=len(frames))
            return self._split(pre + frames, raw, delay + ecu, self._wait_after(ecu, 0))
        if mode == 0x09:
            return [(self._pack(pre + ["NO DATA"], raw), delay)]
        # Qualquer outro modo: o fake responde como uma ECU que não aceita.
        return [(self._pack(pre + ["NO DATA"], raw), delay)]

    def _multi(self, pids: list[int], pre: list[str], delay: float, suffix: int, raw: str) -> list[tuple[str, float]]:
        m = self.car.model
        self.multi_requests += 1
        if self.profile == "clone_interrog":
            return [(self._pack(["?"], raw), 0.02)]
        answer = pids
        if self.profile == "clone_ruim":
            answer = pids[:1]                   # só o primeiro PID
        elif self.profile == "clone_instavel" and self.multi_requests > 8 and self.rng.random() < 0.15:
            answer = pids[:-1]                  # de vez em quando some o último PID
        payload = [0x41]
        for p in answer:
            d = m.read_pid(p)
            if d is not None:                   # a ECU omite os PIDs que não tem
                payload += [p] + d
        st_s = self.st * 0.004
        if len(payload) == 1:
            return [(self._pack(pre + ["NO DATA"], raw), delay + max(st_s, 0.1))]
        frames = self._can_frames(payload)
        ecu = self._ecu_delay(extra_pids=len(pids) - 1, frames=len(frames))
        if self._too_slow(ecu):
            return [(self._pack(pre + ["NO DATA"], raw), delay + st_s)]
        return self._split(pre + frames, raw, delay + ecu, self._wait_after(ecu, suffix))

    def _split(self, lines: list[str], raw: str, before: float, wait: float) -> list[tuple[str, float]]:
        """Dados primeiro; a linha em branco + '>' depois da espera."""
        text = self._pack(lines, raw)
        tail = self.eol + ">"
        body = text[: -len(tail)]
        if wait <= 0:
            return [(text, before)]
        return [(body, before), (tail, wait)]

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

    def __init__(self, car: SimCar, latency: tuple[float, float] = (0.012, 0.030),
                 chunk_gap: tuple[float, float] = (0.001, 0.006), chunk_size: int = 20,
                 search_delay: tuple[float, float] = (1.2, 2.5), profile: str = "bom",
                 write_ack: tuple[float, float] = (0.015, 0.040), timing_model: bool = True) -> None:
        """``latency``: rádio BLE (ida do comando + volta da resposta).
        ``write_ack``: custo extra da escrita COM resposta (confirmação no
        próximo evento de conexão do BLE)."""
        self.car = car
        self.elm = FakeELM(car, search_delay=search_delay, profile=profile, timing_model=timing_model)
        self.profile = profile
        self.latency = latency
        self.write_ack = write_ack if timing_model else (0.0, 0.0)
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
