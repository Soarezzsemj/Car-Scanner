"""Modelo físico simples de um carro 1.6 a gasolina, para o simulador.

Tudo aqui é FALSO e serve só para testar o programa sem o veículo.
O tempo do modelo ("tempo simulado") avança = tempo real × fator.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Optional

IDLE_RPM = 800.0
GEAR_RATIO = (0.0, 118.0, 66.0, 45.0, 34.0, 28.0)   # rpm por km/h em cada marcha
GEAR_UP = (0, 18, 32, 50, 68, 999)                   # troca para cima (km/h)
ALL_PIDS = {0x04, 0x05, 0x0C, 0x0D, 0x0F, 0x11, 0x42, 0x46, 0x5C}


@dataclass
class Segment:
    kind: str        # "parado", "lenta", "cidade", "estrada"
    duration: float  # segundos simulados


@dataclass
class DtcState:
    confirmed: set = field(default_factory=set)   # modo 03
    pending: set = field(default_factory=set)     # modo 07
    permanent: set = field(default_factory=set)   # modo 0A


class CarModel:
    def __init__(self, rng: Optional[random.Random] = None, ambient: float = 27.0) -> None:
        self.rng = rng or random.Random()
        self.t = 0.0
        self.ambient = ambient
        self.coolant = ambient + 3.0
        self.oil = ambient + 3.0
        self.intake = ambient + 2.0
        self.speed = 0.0
        self.rpm = 0.0
        self.throttle = 0.0       # 0..1 (pedal)
        self.load = 0.0           # %
        self.gear = 0
        self.voltage = 12.4
        self.fan_on = False
        self.engine_on = True
        self.ignition_on = True
        self.ecu_awake = True
        self.supported: set[int] = set(ALL_PIDS)
        self.dtcs = DtcState()

        # Falhas / parâmetros de cenário
        self.radiator_eff = 1.0
        self.fan_failed = False
        self.extra_heat = 0.0      # °C/s adicionais
        self.oil_extra = 0.0       # °C acima do normal no óleo
        self.oil_light = False     # luz do painel (NÃO é visível pelo OBD!)
        self.dash_beep = False

        # Overrides do painel do simulador (valores forçados)
        self.overrides: dict[str, float] = {}

        # Motorista
        self.plan: list[Segment] = [Segment("lenta", 60), Segment("cidade", 300),
                                    Segment("estrada", 360), Segment("cidade", 180), Segment("lenta", 60)]
        self.loop_plan = True
        self._seg_idx = 0
        self._seg_t = 0.0
        self._target = 0.0
        self._city_phase = "parado"
        self._city_timer = 0.0
        self._hw_timer = 0.0

    # -- motorista ------------------------------------------------------------
    @property
    def segment(self) -> Segment:
        return self.plan[min(self._seg_idx, len(self.plan) - 1)]

    def _advance_plan(self, dt: float) -> None:
        self._seg_t += dt
        if self._seg_t >= self.segment.duration and (self._seg_idx < len(self.plan) - 1 or self.loop_plan):
            self._seg_t = 0.0
            self._seg_idx = (self._seg_idx + 1) % len(self.plan) if self.loop_plan else self._seg_idx + 1
            self._city_phase, self._city_timer = "parado", 0.0

    def _driver_target(self, dt: float) -> float:
        kind = self.segment.kind
        r = self.rng
        if kind in ("parado", "lenta"):
            return 0.0
        if kind == "cidade":
            self._city_timer -= dt
            if self._city_timer <= 0:
                if self._city_phase == "parado":
                    self._city_phase, self._target, self._city_timer = "andando", r.uniform(28, 55), r.uniform(15, 40)
                else:
                    self._city_phase, self._target, self._city_timer = "parado", 0.0, r.uniform(6, 25)
            return self._target
        # estrada
        self._hw_timer -= dt
        if self._hw_timer <= 0:
            self._target, self._hw_timer = r.uniform(85, 112), r.uniform(20, 60)
        return self._target

    # -- passo de simulação -------------------------------------------------
    def advance(self, dt_total: float) -> None:
        while dt_total > 1e-9:
            dt = min(0.25, dt_total)
            self._step(dt)
            dt_total -= dt

    def _step(self, dt: float) -> None:
        r = self.rng
        self.t += dt
        self._advance_plan(dt)

        if not self.engine_on:
            self.rpm = max(0.0, self.rpm - 2000 * dt)
            self.speed = max(0.0, self.speed - 10 * dt)
            self.throttle = 0.0
            self.load = 0.0
            self.voltage += (12.4 - self.voltage) * min(1.0, dt)
            self._thermal(dt)
            return

        target = self._driver_target(dt)
        if "velocidade_kmh" in self.overrides:
            target = self.overrides["velocidade_kmh"]
        err = target - self.speed
        # aceleração desejada em km/h/s
        acc = max(-9.0, min(6.0, 0.6 * err))
        if acc >= 0:
            self.throttle = max(0.0, min(1.0, 0.04 + 0.0035 * self.speed + 0.09 * acc + r.gauss(0, 0.01)))
            if err < 1.0 and self.speed < 1.0:
                self.throttle = 0.0
        else:
            self.throttle = 0.0  # tira o pé: freio motor / freio
        drag = 0.002 * self.speed + 0.00003 * self.speed ** 2
        self.speed = max(0.0, self.speed + (acc - (drag if acc >= 0 else 0.0)) * dt)
        if target <= 0.5 and self.speed < 2.0:
            self.speed = max(0.0, self.speed - 4 * dt)

        # marcha
        if self.speed < 6:
            self.gear = 0 if self.speed < 3 else 1
        else:
            g = max(1, self.gear)
            while g < 5 and self.speed > GEAR_UP[g]:
                g += 1
            while g > 1 and self.speed < GEAR_UP[g - 1] - 8:
                g -= 1
            self.gear = g

        if self.gear == 0:
            rpm_target = IDLE_RPM + (60 if self.coolant < 50 else 0) + 2500 * self.throttle
        else:
            rpm_target = max(IDLE_RPM, self.speed * GEAR_RATIO[self.gear])
        if "rpm" in self.overrides:
            rpm_target = self.overrides["rpm"]
        self.rpm += (rpm_target - self.rpm) * min(1.0, 4.0 * dt)
        self.rpm = max(0.0, self.rpm + r.gauss(0, 6))

        # carga calculada (%): freio motor -> quase 0 (corte de injeção)
        if self.throttle <= 0.001 and self.speed > 12 and self.gear > 0:
            load_t = 2.0
        else:
            load_t = 17.0 + 75.0 * self.throttle + 6.0 * (self.rpm / 6000.0)
        self.load += (load_t - self.load) * min(1.0, 5.0 * dt)

        # elétrica
        v_t = 14.15 - (0.25 if self.fan_on else 0.0) - (0.15 if self.rpm < 900 else 0.0)
        self.voltage += (v_t - self.voltage) * min(1.0, 2.0 * dt) + r.gauss(0, 0.01)
        self._thermal(dt)

    def _thermal(self, dt: float) -> None:
        heat = 0.0
        if self.engine_on and self.rpm > 300:
            heat = 0.30 * (self.rpm / 1500.0) * (0.35 + self.load / 100.0)
        heat += self.extra_heat
        opening = max(0.0, min(1.0, (self.coolant - 88.0) / 7.0))
        airflow = 0.004 + 0.00022 * self.speed
        if self.fan_failed:
            self.fan_on = False
        elif self.coolant >= 97.0:
            self.fan_on = True
        elif self.coolant <= 92.0:
            self.fan_on = False
        if self.fan_on:
            airflow += 0.012
        loss = 0.0015 * (self.coolant - self.ambient)
        loss += opening * (self.coolant - self.ambient) * airflow * self.radiator_eff * 1.2
        self.coolant += (heat - loss) * dt
        self.coolant = min(self.coolant, 125.0)
        if "arrefecimento_c" in self.overrides:
            self.coolant = self.overrides["arrefecimento_c"]

        oil_t = self.coolant + 2.0 + 22.0 * (self.load / 100.0) * (self.rpm / 4000.0) + self.oil_extra
        self.oil += (oil_t - self.oil) * min(1.0, dt / 80.0)

        soak = 22.0 if self.speed < 10 else 6.0
        self.intake += (self.ambient + soak - self.intake) * min(1.0, dt / 60.0)

    # -- leitura "OBD" --------------------------------------------------------
    def read_pid(self, pid: int) -> Optional[list[int]]:
        """Bytes de dados que a ECU devolveria (sem 41 PID), ou None (NO DATA)."""
        r = self.rng
        if pid in (0x00, 0x20, 0x40, 0x60):
            return self._mask(pid)
        if pid not in self.supported:
            return None
        if not self.ignition_on:
            return None

        def tbyte(v): return max(0, min(255, int(round(v + 40))))
        def pct(v): return max(0, min(255, int(round(v * 255 / 100))))

        if pid == 0x0C:
            v = int(max(0, self.rpm) * 4)
            return [(v >> 8) & 0xFF, v & 0xFF]
        if pid == 0x0D:
            return [max(0, min(255, int(round(self.speed))))]
        if pid == 0x05:
            return [tbyte(self.coolant + r.gauss(0, 0.2))]
        if pid == 0x5C:
            return [tbyte(self.oil + r.gauss(0, 0.2))]
        if pid == 0x04:
            return [pct(self.load)]
        if pid == 0x11:
            return [pct(12.0 + 80.0 * self.throttle)]
        if pid == 0x0F:
            return [tbyte(self.intake)]
        if pid == 0x42:
            v = int(max(0, self.voltage) * 1000)
            return [(v >> 8) & 0xFF, v & 0xFF]
        if pid == 0x46:
            return [tbyte(self.ambient)]
        return None

    def _mask(self, base: int) -> Optional[list[int]]:
        # O carro anuncia exatamente os PIDs suportados + "próxima faixa"
        sup = set(self.supported)
        sup |= {0x20, 0x40}  # o C3 (CAN) costuma ter as faixas 01-40 e 41-60
        if base != 0 and base not in sup:
            return None
        bits = 0
        for i in range(32):
            if (base + i + 1) in sup:
                bits |= 1 << (31 - i)
        return [(bits >> 24) & 0xFF, (bits >> 16) & 0xFF, (bits >> 8) & 0xFF, bits & 0xFF]

    def read_dtcs(self, mode: int) -> list[str]:
        if mode == 0x03:
            return sorted(self.dtcs.confirmed)
        if mode == 0x07:
            return sorted(self.dtcs.pending)
        if mode == 0x0A:
            return sorted(self.dtcs.permanent)
        return []

    def snapshot(self) -> dict:
        return {
            "t_sim": self.t, "rpm": self.rpm, "velocidade_kmh": self.speed,
            "arrefecimento_c": self.coolant, "oleo_c": self.oil, "carga_pct": self.load,
            "acelerador_pct": 12.0 + 80.0 * self.throttle, "admissao_c": self.intake,
            "tensao_v": self.voltage, "ambiente_c": self.ambient, "ventoinha": self.fan_on,
            "luz_oleo": self.oil_light, "apito": self.dash_beep, "marcha": self.gear,
            "trecho": self.segment.kind, "ecu": self.ecu_awake,
        }


def dtc_to_bytes(code: str) -> tuple[int, int]:
    t = "PCBU".index(code[0].upper())
    d1 = int(code[1], 16)
    a = (t << 6) | (d1 << 4) | int(code[2], 16)
    b = int(code[3:5], 16)
    return a, b


def smoothstep(x: float) -> float:
    x = max(0.0, min(1.0, x))
    return x * x * (3 - 2 * x)


__all__ = ["CarModel", "Segment", "dtc_to_bytes", "smoothstep", "ALL_PIDS", "math"]
