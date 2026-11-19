"""Cenários do simulador. Tempos em segundos SIMULADOS (exceto falhas de Bluetooth,
que são em segundos reais, porque afetam o transporte)."""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Optional

from .car_model import CarModel, Segment, smoothstep


@dataclass
class LinkFaults:
    """Falhas do "Bluetooth" simulado (segundos reais)."""
    drop_every: Optional[tuple[float, float]] = None   # intervalo entre quedas
    drop_duration: tuple[float, float] = (2.0, 15.0)
    stall_prob: float = 0.0       # fração das falhas que são "travadas" silenciosas
    packet_loss: float = 0.0      # chance de perder um pedaço (chunk)
    truncate: float = 0.0         # chance de cortar a resposta antes do fim
    first_drop_after: float = 8.0


class Scenario:
    name = "normal"
    title = "Viagem normal"
    description = "Viagem tranquila de ~15 min: lenta, cidade, estrada, cidade."
    default_factor = 5.0
    expected_events: tuple[str, ...] = ()

    def __init__(self) -> None:
        self.faults = LinkFaults()

    def setup(self, m: CarModel) -> None:
        pass

    def update(self, m: CarModel) -> None:
        pass


class Normal(Scenario):
    pass


class Superaquecimento(Scenario):
    name = "superaquecimento"
    title = "Superaquecimento"
    description = ("Depois de aquecer, a ventoinha falha e o radiador perde eficiência: "
                   "o arrefecimento sobe devagar até ~118 °C e o painel apita.")
    default_factor = 10.0
    expected_events = ("ALERTA_ARREFECIMENTO_ALTO", "ALERTA_ARREFECIMENTO_CRITICO")

    def setup(self, m: CarModel) -> None:
        m.plan = [Segment("lenta", 40), Segment("cidade", 2000)]
        m.loop_plan = False
        m.coolant = m.oil = 70.0

    def update(self, m: CarModel) -> None:
        if m.t > 90:
            m.fan_failed = True
            k = smoothstep((m.t - 90) / 240)
            m.radiator_eff = 1.0 - 0.85 * k
            m.extra_heat = 0.12 * k
        if m.coolant >= 118:
            m.coolant = min(m.coolant, 118.5)
            m.dash_beep = True
            m.extra_heat = 0.0
        if m.coolant >= 115 and "P0217" not in m.dtcs.confirmed:
            m.dtcs.pending.add("P0217")
            m.dtcs.confirmed.add("P0217")


class QuedaPressaoOleo(Scenario):
    name = "queda_pressao_oleo"
    title = "Queda de pressão do óleo"
    description = ("Em marcha lenta com o motor quente, o óleo esquenta, aparece P0521 (pendente) "
                   "e depois P0524 (confirmado); a luz do óleo acende. Depois os códigos somem.")
    default_factor = 10.0
    expected_events = ("DTC_NOVO:P0524", "ALERTA_OLEO_QUENTE")

    def setup(self, m: CarModel) -> None:
        m.plan = [Segment("cidade", 200), Segment("lenta", 3000)]
        m.loop_plan = False
        m.coolant = m.oil = 75.0

    def update(self, m: CarModel) -> None:
        t = m.t
        if 200 <= t < 520:
            m.oil_extra = 48.0 * smoothstep((t - 200) / 150)
        elif t >= 520:
            m.oil_extra = max(0.0, 48.0 * (1 - (t - 520) / 120))
        if 250 <= t < 520:
            m.dtcs.pending.add("P0521")
        if 300 <= t < 520:
            m.dtcs.pending.add("P0524")
            m.dtcs.confirmed.add("P0524")
            m.oil_light = True
            m.dash_beep = True
        if t >= 520:
            m.dtcs.pending.discard("P0521")
            m.dtcs.pending.discard("P0524")
            m.dtcs.confirmed.discard("P0524")
            m.oil_light = False
            m.dash_beep = False


class Intermitente(Scenario):
    name = "intermitente"
    title = "Luz intermitente (sem DTC)"
    description = ("A luz do óleo acende e apaga de vez em quando, sem nenhum código e sem mudar "
                   "nada no OBD — só o botão S registra. Treine o fluxo manual!")
    default_factor = 1.0

    def __init__(self) -> None:
        super().__init__()
        self._next = 40.0
        self._off_at = 0.0

    def update(self, m: CarModel) -> None:
        if m.oil_light and m.t >= self._off_at:
            m.oil_light = False
            m.dash_beep = False
        if not m.oil_light and m.t >= self._next:
            m.oil_light = True
            m.dash_beep = True
            self._off_at = m.t + m.rng.uniform(3, 9)
            self._next = m.t + m.rng.uniform(50, 120)


class BluetoothInstavel(Scenario):
    name = "bluetooth_instavel"
    title = "Bluetooth instável"
    description = ("Quedas de conexão aleatórias de 2 a 15 s, travadas silenciosas, perda de "
                   "pacotes e respostas truncadas. O log não pode parar.")
    default_factor = 1.0
    expected_events = ("CONEXAO_PERDIDA", "CONEXAO_RESTABELECIDA")

    def __init__(self) -> None:
        super().__init__()
        self.faults = LinkFaults(drop_every=(15.0, 40.0), drop_duration=(2.0, 15.0), stall_prob=0.3,
                                 packet_loss=0.03, truncate=0.03)


class SemOleoPid(Scenario):
    name = "sem_oleo_pid"
    title = "Sem PID de óleo (como o C3 real deve ser)"
    description = "O carro não suporta o PID 5C (temperatura do óleo) nem o 46 (ambiente)."
    default_factor = 5.0
    expected_events = ("PID_NAO_SUPORTADO:5C",)

    def setup(self, m: CarModel) -> None:
        m.supported.discard(0x5C)
        m.supported.discard(0x46)


class EcuDormindo(Scenario):
    name = "ecu_dormindo"
    title = "ECU dormindo (ignição desligada)"
    description = "Nos primeiros 20 s o carro não responde (UNABLE TO CONNECT); depois a ignição liga."
    default_factor = 1.0

    def setup(self, m: CarModel) -> None:
        m.ecu_awake = False
        m.engine_on = False
        m.ignition_on = False

    def update(self, m: CarModel) -> None:
        if not m.ecu_awake and m.t >= 20.0:
            m.ecu_awake = m.ignition_on = m.engine_on = True


class DtcVariados(Scenario):
    name = "dtc_variados"
    title = "DTCs variados"
    description = "Vários códigos aparecendo e sumindo (pendente → confirmado → apagado)."
    default_factor = 5.0
    expected_events = ("DTC_NOVO:P0133", "DTC_NOVO:P0480")

    # (tempo, ação, modo, código)
    TIMELINE = [
        (40, "+", "07", "P0133"), (90, "+", "07", "P0301"), (110, "+", "03", "P0133"),
        (110, "+", "0A", "P0133"), (150, "-", "07", "P0301"), (170, "+", "03", "P0480"),
        (200, "+", "07", "P0562"), (250, "+", "03", "P0562"), (250, "+", "03", "P0128"),
        (330, "-", "03", "P0480"), (330, "-", "03", "P0562"), (330, "-", "07", "P0562"),
        (380, "-", "03", "P0128"), (420, "-", "03", "P0133"), (420, "-", "07", "P0133"),
        (520, "-", "0A", "P0133"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._i = 0

    def update(self, m: CarModel) -> None:
        sets = {"03": m.dtcs.confirmed, "07": m.dtcs.pending, "0A": m.dtcs.permanent}
        while self._i < len(self.TIMELINE) and m.t >= self.TIMELINE[self._i][0]:
            _, act, mode, code = self.TIMELINE[self._i]
            (sets[mode].add if act == "+" else sets[mode].discard)(code)
            self._i += 1


SCENARIOS: dict[str, type[Scenario]] = {
    c.name: c for c in (Normal, Superaquecimento, QuedaPressaoOleo, Intermitente,
                        BluetoothInstavel, SemOleoPid, EcuDormindo, DtcVariados)
}


def make_scenario(name: str) -> Scenario:
    try:
        return SCENARIOS[name]()
    except KeyError:
        raise ValueError(f"Cenário desconhecido: {name}. Opções: {', '.join(SCENARIOS)}") from None


__all__ = ["SCENARIOS", "make_scenario", "Scenario", "LinkFaults", "random", "field"]
