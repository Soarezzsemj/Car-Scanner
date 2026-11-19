"""Autoteste com o carro parado (motor ligado), antes de sair na rua.

Uso:  iniciar.bat --selftest            (adaptador real)
      iniciar.bat --selftest --sim      (com o simulador)
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from .elm import ELM327, EcuNotResponding, ElmInfo
from .pids import FAST, PIDS

N_QUERIES = 10
BUDGET = 0.5


@dataclass
class PidTest:
    pid: int
    name: str
    supported: Optional[bool]
    ok: int = 0
    fails: int = 0
    lats: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    last_value: Optional[float] = None

    @property
    def avg(self): return sum(self.lats) / len(self.lats) if self.lats else None


@dataclass
class SelftestResult:
    when: datetime
    info: ElmInfo
    transport: dict
    supported: Optional[set]
    pids: list
    dtc: dict
    fast_set: list
    hz_fast: float
    hz_all: float
    dtc_ok: bool
    round_plain_ms: float
    round_with_dtc_ms: float
    verdict: list
    simulated: bool
    text: str = ""


def _ms(x: Optional[float]) -> str:
    return "--" if x is None else f"{x * 1000:.0f} ms"


async def selftest_core(elm: ELM327, info: ElmInfo, supported: Optional[set],
                        progress: Callable[[str], None] = print, n: int = N_QUERIES,
                        transport_info: Optional[dict] = None, simulated: bool = False) -> SelftestResult:
    progress("1/5  Adaptador inicializado: " + (info.version or "versão desconhecida"))
    if supported is None:
        progress("2/5  Descobrindo PIDs suportados (0100/0120/0140/0160)...")
        supported = await elm.supported_pids()
    else:
        progress("2/5  Usando a lista de PIDs suportados já descoberta")

    tests: list[PidTest] = []
    for p in PIDS:
        sup = None if supported is None else (p.pid in supported)
        t = PidTest(p.pid, p.name, sup)
        tests.append(t)
        if sup is False:
            progress(f"     PID {p.hex} {p.name}: não suportado pelo carro")
            continue
        for _ in range(n):
            r = await elm.query_pid(p.pid)
            if r.ok:
                t.ok += 1
                t.lats.append(r.latency)
                t.last_value = r.value
            else:
                t.fails += 1
                t.errors.append(r.error)
        if t.ok == 0 and sup is None:
            t.supported = False
        progress(f"     PID {p.hex} {p.name}: média {_ms(t.avg)}, falhas {t.fails}/{n}")

    progress("3/5  Lendo DTCs (03, 07, 0A) e medindo impacto no ciclo...")
    fast_cand = [t for t in sorted(tests, key=lambda t: next(p.priority for p in PIDS if p.pid == t.pid))
                 if t.ok and next(p.cycle for p in PIDS if p.pid == t.pid) == FAST]

    async def fast_round():
        t0 = time.monotonic()
        for t in fast_cand:
            await elm.query_pid(t.pid)
        return time.monotonic() - t0

    plain = [await fast_round() for _ in range(3)]
    dtc: dict = {}
    dtc_ok = True
    with_dtc = []
    for mode in (0x03, 0x07, 0x0A):
        t0 = time.monotonic()
        try:
            codes = await asyncio.wait_for(elm.read_dtcs(mode), 6.0)
        except asyncio.TimeoutError:
            codes = None
        dt = time.monotonic() - t0
        dtc[mode] = (codes, dt)
        if codes is None:
            dtc_ok = False
        with_dtc.append(await fast_round() + dt)
    plain_ms = sum(plain) / len(plain) * 1000
    dtc_ms = sum(with_dtc) / len(with_dtc) * 1000

    progress("4/5  Calculando a taxa possível...")
    total_all = sum(t.avg for t in tests if t.avg)
    hz_all = 1 / total_all if total_all else 0.0
    fast_set, total = [], 0.0
    for i, t in enumerate(fast_cand):
        if i < 3 or total + t.avg <= BUDGET * 0.85:
            fast_set.append(t)
            total += t.avg
    hz_fast = 1 / total if total else 0.0

    progress("5/5  Veredito")
    verdict = []
    oil = next(t for t in tests if t.pid == 0x5C)
    if oil.supported is False or oil.ok == 0:
        verdict.append("PID 5C (temperatura do óleo) NÃO suportado – o medidor do óleo ficará cinza.")
    else:
        verdict.append(f"PID 5C (óleo) suportado: {oil.last_value:.0f} °C agora.")
    for t in tests:
        if t.pid != 0x5C and (t.supported is False or t.ok == 0):
            verdict.append(f"PID {t.pid:02X} ({t.name}) não suportado.")
        elif t.fails:
            verdict.append(f"PID {t.pid:02X} ({t.name}) falhou {t.fails}/{n} vezes.")
    verdict.append(f"Ciclo rápido: {hz_fast:.1f} Hz com {', '.join(t.name for t in fast_set) or 'nenhum PID'}."
                   .replace(".", ",", 1))
    left = [t.name for t in fast_cand if t not in fast_set]
    if left:
        verdict.append("Não couberam em 0,5 s (vão para o ciclo de 1 s): " + ", ".join(left) + ".")
    verdict.append(f"Todos os PIDs em sequência: {hz_all:.1f} Hz.".replace(".", ",", 1))
    if hz_fast < 2.0:
        verdict.append("ATENÇÃO: o ciclo rápido não cabe em 0,5 s; o log continua a cada 0,5 s, "
                       "mas alguns valores serão repetidos.")
    if dtc_ok:
        dtcs = sorted({c for codes, _ in dtc.values() for c in (codes or [])})
        verdict.append("Leitura de DTC OK, sem travar" + (f" – códigos: {', '.join(dtcs)}" if dtcs else " – nenhum código."))
        verdict.append(f"Impacto da leitura de DTC numa rodada: {plain_ms:.0f} ms → {dtc_ms:.0f} ms "
                       "(no monitor, só 1 leitura de DTC por rodada).")
    else:
        verdict.append("Algum modo de DTC não respondeu (o carro pode não suportar o modo 0A).")
    res = SelftestResult(datetime.now(), info, transport_info or {}, supported, tests, dtc, fast_set,
                         hz_fast, hz_all, dtc_ok, plain_ms, dtc_ms, verdict, simulated)
    res.text = format_report(res, n)
    return res


def format_report(r: SelftestResult, n: int = N_QUERIES) -> str:
    L = [f"AUTOTESTE DO ADAPTADOR – {r.when:%d/%m/%Y %H:%M:%S}"]
    if r.simulated:
        L.append("*** SIMULADOR: estes resultados NÃO são do carro real ***")
    L += ["", f"ELM: {r.info.version}  {r.info.device}".rstrip(),
          f"Protocolo: {r.info.protocol or '?'} (número {r.info.protocol_num or '?'})",
          f"Tensão (ATRV): {r.info.voltage or '?'}"]
    for k, v in r.transport.items():
        L.append(f"{k}: {v}")
    sup = "não foi possível ler" if r.supported is None else " ".join(f"{p:02X}" for p in sorted(r.supported))
    L += ["", f"PIDs anunciados pelo carro: {sup}", "",
          f"PID  Nome                      Suporte   média     mín      máx   falhas/{n}  último valor"]
    for t in r.pids:
        s = "não" if t.supported is False else ("sim" if t.ok else "?")
        mn = _ms(min(t.lats)) if t.lats else "--"
        mx = _ms(max(t.lats)) if t.lats else "--"
        last = "" if t.last_value is None else f"{t.last_value:.1f}".replace(".", ",")
        L.append(f"{t.pid:02X}   {t.name:<25} {s:<8} {_ms(t.avg):>7} {mn:>8} {mx:>8}   {t.fails:>4}      {last}")
    L += ["", "DTCs:"]
    names = {0x03: "03 confirmados", 0x07: "07 pendentes", 0x0A: "0A permanentes"}
    for mode, (codes, dt) in r.dtc.items():
        txt = "FALHOU" if codes is None else (", ".join(codes) or "nenhum")
        L.append(f"  {names[mode]:<16} {txt}   ({dt * 1000:.0f} ms)")
    L += ["", "VEREDITO:"] + [f"  • {v}" for v in r.verdict]
    return "\n".join(L) + "\n"


def save_report(r: SelftestResult, logs_dir: Path, simulated: bool) -> Path:
    logs_dir = Path(logs_dir)
    logs_dir.mkdir(parents=True, exist_ok=True)
    p = logs_dir / f"{'sim_' if simulated else ''}selftest_{r.when:%Y-%m-%d_%H-%M-%S}.txt"
    p.write_text(r.text, encoding="utf-8-sig")
    return p


def latest_report(logs_dir: Path) -> Optional[Path]:
    files = sorted(Path(logs_dir).glob("*selftest_*.txt"), key=lambda p: p.stat().st_mtime)
    return files[-1] if files else None


async def run_cli(transport, logs_dir: Path, simulated: bool, wait_ecu_s: float = 60.0) -> int:
    """Autoteste em modo console (sem GUI)."""
    print("Conectando ao adaptador...")
    await transport.connect()
    try:
        elm = ELM327(transport)
        print("Inicializando (ATZ, ATE0, ATL0, ATS0, ATH0, ATSP0)...")
        await elm.configure()
        print("Falando com o carro (busca de protocolo, até 12 s)...")
        t_end = time.monotonic() + wait_ecu_s
        while True:
            try:
                await elm.probe_ecu(12.0)
                break
            except EcuNotResponding as e:
                if time.monotonic() > t_end:
                    print(f"\nO carro não respondeu: {e}\nLigue a ignição (ou o motor) e tente de novo.")
                    return 2
                print("  O carro ainda não responde (ignição desligada?). Tentando de novo em 3 s...")
                await asyncio.sleep(3)
        info = await elm.read_protocol_info()
        res = await selftest_core(elm, info, None, transport_info=transport.describe(), simulated=simulated)
        path = save_report(res, logs_dir, simulated)
        print("\n" + res.text)
        print(f"Relatório salvo em: {path}")
        return 0
    finally:
        await transport.disconnect()
