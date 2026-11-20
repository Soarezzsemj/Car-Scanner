"""Autobenchmark de velocidade (carro PARADO, motor ligado).

    iniciar.bat --benchmark            adaptador real
    iniciar.bat --benchmark --sim      simulador

Testa, por ~15 s cada, combinações de otimizações na rodada rápida (RPM,
velocidade, arrefecimento, carga, acelerador):

    A   um PID por vez, sem sufixo (modo seguro = como o programa era antes)
    A2  idem, com escrita BLE sem resposta
    B   um PID por vez com sufixo "1" (respostas esperadas)
    C   B (ou A2, se o sufixo não funcionar) + ATAT1/ATAT2 e ATST menores
    D   multi-PID com 2, 3 e 5 PIDs por requisição (com e sem sufixo)

Para cada uma mede rodadas completas por segundo, latência média/máxima por
requisição, taxa de erros (respostas inválidas, truncadas, incompletas, "?",
NO DATA, TIMEOUT) e confere se os valores batem com a leitura individual.
Escolhe a mais rápida com 0% de erros (se nenhuma tiver 0%, a mais rápida
que não erra mais do que o modo seguro), salva em config.json por adaptador
e gera logs/benchmark_<data>.txt.

Somente leitura: só usa comandos da whitelist (safety.py).
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from .elm import ELM327, EcuNotResponding, ElmInfo, Tuning
from .pids import BY_PID, FAST_CORE, plausible
from .poller import TOLERANCE, values_agree

log = logging.getLogger(__name__)

SECONDS_PER_COMBO = 15.0
CHECK_EVERY_S = 1.0       # conferência com leitura individual
ST_CANDIDATES = (0x19,)   # ATST 19 = 25 x 4 ms ≈ 100 ms


@dataclass
class Combo:
    key: str
    label: str
    tuning: Tuning


@dataclass
class ComboResult:
    combo: Combo
    seconds: float = 0.0
    rounds: int = 0                  # rodadas completas (todos os PIDs com valor válido)
    requests: int = 0
    readings: int = 0                # leituras tentadas (PIDs)
    errors: dict = field(default_factory=dict)   # tipo -> quantidade
    lats: list = field(default_factory=list)     # ms por requisição
    checks: int = 0
    mismatches: int = 0
    skipped: str = ""

    @property
    def rps(self) -> float:
        return self.rounds / self.seconds if self.seconds > 0 else 0.0

    @property
    def n_errors(self) -> int:
        return sum(self.errors.values())

    @property
    def error_rate(self) -> float:
        return self.n_errors / self.readings if self.readings else (1.0 if self.skipped else 0.0)

    @property
    def lat_avg(self) -> Optional[float]:
        return sum(self.lats) / len(self.lats) if self.lats else None

    @property
    def lat_max(self) -> Optional[float]:
        return max(self.lats) if self.lats else None

    @property
    def clean(self) -> bool:
        return not self.skipped and self.rounds > 0 and self.n_errors == 0 and self.mismatches == 0

    def add_error(self, kind: str) -> None:
        kind = (kind or "ERRO").split(" ")[0]
        self.errors[kind] = self.errors.get(kind, 0) + 1


@dataclass
class BenchResult:
    when: datetime
    info: ElmInfo
    transport: dict
    fast: list
    results: list
    best: Optional[ComboResult]
    baseline: Optional[ComboResult]
    tuning: Optional[Tuning]
    simulated: bool
    adapter_key: str = ""
    text: str = ""

    @property
    def gain(self) -> Optional[float]:
        if not self.best or not self.baseline or self.baseline.rps <= 0:
            return None
        return self.best.rps / self.baseline.rps


def _br(x: Optional[float], d: int = 1) -> str:
    return "--" if x is None else f"{x:.{d}f}".replace(".", ",")


def choose_best(results: list[ComboResult]) -> Optional[ComboResult]:
    """A mais rápida com 0% de erros e valores conferidos. Se nenhuma estiver
    limpa (adaptador que erra sempre um pouco), a mais rápida que não erra mais
    do que o modo seguro (A) e cujos valores batem."""
    valid = [r for r in results if not r.skipped and r.rounds > 0]
    clean = [r for r in valid if r.clean]
    if clean:
        return max(clean, key=lambda r: r.rps)
    base = next((r for r in valid if r.combo.key == "A"), None)
    limit = base.error_rate if base else 0.0
    ok = [r for r in valid if r.mismatches == 0 and r.error_rate <= limit + 1e-9]
    if ok:
        return max(ok, key=lambda r: r.rps)
    return base


class Bench:
    def __init__(self, elm: ELM327, transport, fast: list[int], protocol: str,
                 progress: Callable[[str], None], seconds: float) -> None:
        self.elm = elm
        self.t = transport
        self.fast = fast
        self.protocol = protocol
        self.progress = progress
        self.seconds = seconds
        self.modes = tuple(getattr(transport, "write_modes", ()) or ())
        self._fixed: Optional[bool] = None

    async def apply(self, tu: Tuning) -> None:
        if tu.write_with_response is not None and tu.write_with_response in self.modes:
            self.t.set_write_with_response(tu.write_with_response)
        fixed = bool(tu.fix_protocol and self.protocol)
        if fixed != self._fixed:
            if fixed:
                await self.elm.set_protocol(self.protocol)
            else:
                # volta à busca automática e já faz a busca (fora da medição)
                await self.elm.set_protocol("0")
                try:
                    await self.elm.probe_ecu(12.0)
                except EcuNotResponding:
                    pass
            self._fixed = fixed
        await self.elm.apply_timing(tu.adaptive, tu.st)

    async def reference(self, pid: int) -> Optional[float]:
        r = await self.elm.query_pid(pid)          # leitura individual, sem sufixo
        return r.value if r.ok and r.value is not None and plausible(pid, r.value) else None

    async def one_round(self, tu: Tuning, res: Optional[ComboResult]) -> dict:
        """Uma rodada; devolve {pid: valor} das leituras válidas."""
        n = max(1, tu.multi)
        vals: dict[int, float] = {}
        for i in range(0, len(self.fast), n):
            group = self.fast[i:i + n]
            suffix = tu.suffix and (len(group) == 1 or tu.multi_suffix)
            g = await self.elm.query_group(group, suffix=suffix)
            if res is not None:
                res.requests += 1
                res.readings += len(group)
                if g.timing is not None:
                    res.lats.append(g.timing.total)
                elif g.latency:
                    res.lats.append(g.latency * 1000)
            if not g.ok:
                if res is not None:
                    for _ in group:
                        res.add_error(g.error or "ERRO")
                continue
            for p in group:
                v = g.results[p].value
                if v is None or not plausible(p, v):
                    if res is not None:
                        res.add_error("FORA_DA_FAIXA")
                    continue
                vals[p] = v
        return vals

    async def run_combo(self, combo: Combo) -> ComboResult:
        res = ComboResult(combo)
        tu = combo.tuning
        await self.apply(tu)
        await self.one_round(tu, None)               # aquecimento (busca de protocolo etc.)
        t0 = time.monotonic()
        next_check = t0 + CHECK_EVERY_S
        while time.monotonic() - t0 < self.seconds:
            vals = await self.one_round(tu, res)
            if len(vals) == len(self.fast):
                res.rounds += 1
            if time.monotonic() >= next_check:
                # conferência: a leitura otimizada bate com a individual?
                pause0 = time.monotonic()
                for p, v in vals.items():
                    ref = await self.reference(p)
                    if ref is None:
                        continue
                    res.checks += 1
                    if not values_agree(p, v, [ref]):
                        res.mismatches += 1
                        log.warning("Benchmark %s: PID %02X deu %s, leitura individual %s",
                                    combo.key, p, v, ref)
                spent = time.monotonic() - pause0
                t0 += spent                           # a conferência não conta no tempo
                next_check = time.monotonic() + CHECK_EVERY_S
        res.seconds = time.monotonic() - t0
        return res


def _label_timing(tu: Tuning) -> str:
    parts = []
    if tu.adaptive is not None:
        parts.append(f"ATAT{tu.adaptive}")
    if tu.st is not None:
        parts.append(f"ATST{tu.st:02X}")
    return " + ".join(parts)


async def benchmark_core(elm: ELM327, transport, info: ElmInfo, supported: Optional[set],
                         progress: Callable[[str], None] = print, seconds: float = SECONDS_PER_COMBO,
                         simulated: bool = False, adapter_key: str = "") -> BenchResult:
    fast = [p for p in FAST_CORE if supported is None or p in supported]
    protocol = info.protocol_num if info.protocol_num and info.protocol_num != "0" else ""
    b = Bench(elm, transport, fast, protocol, progress, seconds)
    modes = b.modes
    wwr_ok = False in modes
    both = True in modes and False in modes
    fast_wr = False if wwr_ok else (True if True in modes else None)
    results: list[ComboResult] = []
    names = ", ".join(BY_PID[p].name for p in fast)
    progress(f"PIDs do ciclo rápido: {names}")
    progress(f"Cada combinação roda por ~{seconds:g} s. Não mexa no carro (parado, motor ligado).")

    async def run(combo: Combo) -> ComboResult:
        progress(f"  {combo.key:<4} {combo.label} ...")
        try:
            r = await asyncio.wait_for(b.run_combo(combo), seconds * 3 + 20)
        except asyncio.TimeoutError:
            r = ComboResult(combo, skipped="travou (sem resposta)")
        results.append(r)
        errs = f"{r.error_rate * 100:.1f}%".replace(".", ",")
        progress(f"       {_br(r.rps)} rodadas/s, latência média {_br(r.lat_avg, 0)} ms, erros {errs}"
                 + (f", {r.mismatches} valor(es) divergente(s)" if r.mismatches else ""))
        return r

    if not fast:
        progress("Nenhum PID do ciclo rápido é suportado: nada para medir.")
        res = BenchResult(datetime.now(), info, transport.describe(), fast, [], None, None, None,
                          simulated, adapter_key)
        res.text = format_report(res)
        return res

    # A: modo seguro (exatamente como antes)
    safe = Tuning.safe()
    safe.write_with_response = True if True in modes else None
    base = await run(Combo("A", "um PID por vez, sem sufixo (modo seguro)", safe))
    a2 = None
    if both:
        a2 = await run(Combo("A2", "um PID por vez, sem sufixo, escrita BLE sem resposta",
                             Tuning(fix_protocol=True, write_with_response=False)))
    plain_wr = False if (a2 is not None and a2.clean) else (True if True in modes else None)
    # B: sufixo
    bres = await run(Combo("B", "um PID por vez com sufixo \"1\"",
                           Tuning(suffix=True, fix_protocol=True, write_with_response=fast_wr)))
    suffix_ok = bres.clean
    if not suffix_ok and both and fast_wr is False:
        bres2 = await run(Combo("B2", "sufixo \"1\", escrita BLE com resposta",
                                Tuning(suffix=True, fix_protocol=True, write_with_response=True)))
        if bres2.clean:
            suffix_ok, fast_wr = True, True
    wr = fast_wr if suffix_ok else plain_wr
    # C: temporização adaptativa / timeout menor
    timing_opts = [(2, None)] + [(a, st) for st in ST_CANDIDATES for a in (1, 2)]
    best_timing: tuple = (None, None)
    best_c = bres if suffix_ok else (a2 if (a2 is not None and a2.clean) else base)
    for i, (at, st) in enumerate(timing_opts, 1):
        tu = Tuning(suffix=suffix_ok, adaptive=at, st=st, fix_protocol=True, write_with_response=wr)
        r = await run(Combo(f"C{i}", ("sufixo + " if suffix_ok else "sem sufixo + ") + _label_timing(tu), tu))
        # só troca o timing padrão do ELM se o ganho for claro (≥ 8%), não por ruído
        if r.clean and (best_c is None or not best_c.clean or r.rps > best_c.rps * 1.08):
            best_c, best_timing = r, (at, st)
    at, st = best_timing
    # D: multi-PID
    for n in (2, 3, 5):
        if n > len(fast):
            continue
        variants = [True, False] if suffix_ok else [False]
        for msuf in variants:
            tu = Tuning(suffix=suffix_ok, multi=n, multi_suffix=msuf, adaptive=at, st=st,
                        fix_protocol=True, write_with_response=wr)
            lab = f"{n} PIDs por requisição" + (" com sufixo" if msuf else (" sem sufixo" if suffix_ok else ""))
            if _label_timing(tu):
                lab += " + " + _label_timing(tu)
            r = await run(Combo(f"D{n}" + ("" if msuf or not suffix_ok else "s"), lab, tu))
            if r.clean:
                break   # com sufixo funcionou: não precisa testar sem

    best = choose_best(results)
    tuning = None
    if best is not None:
        tuning = Tuning.from_dict(best.combo.tuning.to_dict())
        tuning.protocol = protocol
        tuning.fix_protocol = bool(protocol) and best.combo.key != "A"
        tuning.source = "benchmark"
        await b.apply(tuning)                    # deixa o ELM como vai ser usado
    res = BenchResult(datetime.now(), info, transport.describe(), fast, results, best, base, tuning,
                      simulated, adapter_key)
    res.text = format_report(res)
    return res


def tuning_record(res: BenchResult) -> Optional[dict]:
    """O que vai para o config.json (por endereço de adaptador)."""
    if res.tuning is None:
        return None
    d = res.tuning.to_dict()
    d["benchmark_em"] = res.when.isoformat(timespec="seconds")
    d["rodadas_s"] = round(res.best.rps, 2) if res.best else None
    d["rodadas_s_modo_seguro"] = round(res.baseline.rps, 2) if res.baseline else None
    return d


def format_report(r: BenchResult) -> str:
    L = [f"BENCHMARK DE VELOCIDADE – {r.when:%d/%m/%Y %H:%M:%S}"]
    if r.simulated:
        L.append("*** SIMULADOR: estes números NÃO são do carro real ***")
    L += ["", f"ELM: {r.info.version}  {r.info.device}".rstrip(),
          f"Protocolo: {r.info.protocol or '?'} (número {r.info.protocol_num or '?'})"]
    for k, v in r.transport.items():
        L.append(f"{k}: {v}")
    L += ["", "PIDs do ciclo rápido: " + (", ".join(BY_PID[p].name for p in r.fast) or "nenhum"), "",
          "Comb. Descrição                                              rodadas/s  lat.média  lat.máx   erros   conferência"]
    for x in r.results:
        if x.skipped:
            L.append(f"{x.combo.key:<5} {x.combo.label:<55} {'--':>9}  {x.skipped}")
            continue
        errs = f"{x.error_rate * 100:.1f}%".replace(".", ",")
        chk = "ok" if x.mismatches == 0 else f"{x.mismatches} divergente(s)"
        chk += f" ({x.checks})"
        L.append(f"{x.combo.key:<5} {x.combo.label[:55]:<55} {_br(x.rps):>9} {_br(x.lat_avg, 0):>7} ms"
                 f" {_br(x.lat_max, 0):>6} ms {errs:>7}   {chk}")
        if x.errors:
            L.append("        erros: " + ", ".join(f"{k} {v}" for k, v in sorted(x.errors.items())))
    L.append("")
    if r.best is None:
        L.append("Nenhuma combinação funcionou. O programa vai usar o modo seguro.")
    else:
        L.append(f"ESCOLHIDA: {r.best.combo.key} – {r.best.combo.label}")
        if r.tuning is not None:
            L.append(f"Configuração: {r.tuning.describe()}")
        if r.baseline is not None and r.gain is not None:
            L.append(f"Ganho: de {_br(r.baseline.rps)} para {_br(r.best.rps)} rodadas/s "
                     f"({_br(r.gain)}x) na rodada rápida.")
        if not r.best.clean:
            L.append("AVISO: nenhuma combinação ficou com 0% de erros; foi escolhida a mais rápida que "
                     "não erra mais do que o modo seguro.")
        L += ["", "Salvo no config.json para este adaptador e usado automaticamente nas próximas conexões.",
              "Durante a viagem, se alguma otimização gerar dado errado, ela é desligada sozinha "
              "(registrado no programa.log e no CSV).",
              "Para refazer: Configurações → \"Refazer benchmark\" ou iniciar.bat --benchmark."]
    return "\n".join(L) + "\n"


def save_report(r: BenchResult, logs_dir: Path) -> Path:
    logs_dir = Path(logs_dir)
    logs_dir.mkdir(parents=True, exist_ok=True)
    p = logs_dir / f"{'sim_' if r.simulated else ''}benchmark_{r.when:%Y-%m-%d_%H-%M-%S}.txt"
    p.write_text(r.text, encoding="utf-8-sig")
    return p


async def run_cli(transport, cfg, adapter_key: str, simulated: bool, seconds: float = SECONDS_PER_COMBO,
                  wait_ecu_s: float = 60.0, progress: Callable[[str], None] = print) -> int:
    """Benchmark em modo console: conecta, inicializa, mede, salva e imprime."""
    progress("Conectando ao adaptador...")
    await transport.connect()
    try:
        elm = ELM327(transport)
        progress("Inicializando (ATZ, ATE0, ATL0, ATS0, ATH0, ATSP0)...")
        await elm.configure()
        progress("Falando com o carro (busca de protocolo, até 12 s)...")
        t_end = time.monotonic() + wait_ecu_s
        while True:
            try:
                await elm.probe_ecu(12.0)
                break
            except EcuNotResponding as e:
                if time.monotonic() > t_end:
                    progress(f"\nO carro não respondeu: {e}\nLigue o motor (carro parado) e tente de novo.")
                    return 2
                progress("  O carro ainda não responde (ignição desligada?). Tentando de novo em 3 s...")
                await asyncio.sleep(3)
        info = await elm.read_protocol_info()
        supported = await elm.supported_pids()
        res = await benchmark_core(elm, transport, info, supported, progress, seconds=seconds,
                                   simulated=simulated, adapter_key=adapter_key)
        path = save_report(res, cfg.logs_path)
        rec = tuning_record(res)
        if rec is not None:
            cfg.set_tuning(adapter_key, rec)
            cfg.save()
        progress("\n" + res.text)
        progress(f"Relatório salvo em: {path}")
        return 0
    finally:
        await transport.disconnect()


__all__ = ["benchmark_core", "choose_best", "ComboResult", "Combo", "BenchResult", "run_cli",
           "save_report", "tuning_record", "format_report", "TOLERANCE"]
