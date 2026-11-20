"""Log automático: CSV por viagem, instantâneos de evento, log de texto e resumo.

Dois CSVs por viagem:
- ``obd_<viagem>.csv``: uma linha a cada 0,5 s com o ÚLTIMO valor válido de
  cada parâmetro (valores repetidos = o mesmo dado ainda não foi renovado) e
  ``idade_ms_<param>`` = quanto tempo aquele valor tinha quando a linha foi escrita;
- ``leituras_<viagem>.csv``: formato longo, UMA linha por leitura real que
  chegou do carro, com o instante da chegada da resposta (ms) e a latência.

Toda a escrita acontece numa thread própria (fila), para nunca atrasar a
leitura do carro. Cada linha recebe flush + fsync: se o notebook desligar ou o
programa travar, o que já foi gravado fica no disco.
"""
from __future__ import annotations

import logging
import os
import queue
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from .events import Event, RingBuffer, format_event_line
from .pids import BY_KEY, FAST_CORE_KEYS, VALUE_KEYS

log = logging.getLogger(__name__)

AGE_KEYS: tuple[str, ...] = FAST_CORE_KEYS
AGE_COLUMNS: tuple[str, ...] = tuple(f"idade_ms_{k}" for k in AGE_KEYS)
COLUMNS = ("data_hora", "t_s") + VALUE_KEYS + ("dtcs_ativos", "evento", "nota") + AGE_COLUMNS
READING_COLUMNS = ("data_hora", "pid", "nome", "valor", "latencia_ms")
READINGS_SYNC_S = 0.5
SEP = ";"
ENCODING = "utf-8-sig"


def fmt_value(key: str, v) -> str:
    """Número no formato do Excel brasileiro (vírgula decimal)."""
    if v is None:
        return ""
    d = BY_KEY[key].decimals if key in BY_KEY else 1
    if d == 0:
        return str(int(round(v)))
    return f"{v:.{d}f}".replace(".", ",")


def _clean(text: str) -> str:
    return str(text).replace(SEP, ",").replace("\r", " ").replace("\n", " ")


def format_row(row: dict) -> str:
    cells = [
        row["t_wall"].isoformat(timespec="milliseconds"),
        f"{row['t_s']:.1f}".replace(".", ","),
    ]
    cells += [fmt_value(k, row["values"].get(k)) for k in VALUE_KEYS]
    cells.append(_clean(" ".join(row.get("dtcs", []))))
    cells.append(_clean("|".join(row.get("events", []))))
    cells.append(_clean(row.get("note", "")))
    ages = row.get("ages") or {}
    for k in AGE_KEYS:
        a = ages.get(k)
        cells.append("" if a is None or row["values"].get(k) is None else str(int(round(a))))
    return SEP.join(cells) + "\n"


HEADER = SEP.join(COLUMNS) + "\n"
READING_HEADER = SEP.join(READING_COLUMNS) + "\n"


def format_reading(t_wall: datetime, pid: str, name: str, value: str, latency_ms=None) -> str:
    lat = "" if latency_ms is None else str(int(round(latency_ms)))
    return SEP.join((t_wall.isoformat(timespec="milliseconds"), pid, _clean(name), value, lat)) + "\n"


@dataclass
class _Snapshot:
    path: Path
    end_t: float
    fh: object


@dataclass
class TripStats:
    started: datetime
    rows: int = 0
    rows_without_data: int = 0
    last_t: float = 0.0
    mins: dict = field(default_factory=dict)
    maxs: dict = field(default_factory=dict)
    max_at: dict = field(default_factory=dict)
    sums: dict = field(default_factory=dict)
    counts: dict = field(default_factory=dict)
    events: dict = field(default_factory=dict)
    event_lines: list = field(default_factory=list)

    def add(self, row: dict, events: list[Event]) -> None:
        self.rows += 1
        self.last_t = row["t_s"]
        vals = row["values"]
        if all(vals.get(k) is None for k in VALUE_KEYS):
            self.rows_without_data += 1
        for k in VALUE_KEYS:
            v = vals.get(k)
            if v is None:
                continue
            if k not in self.mins or v < self.mins[k]:
                self.mins[k] = v
            if k not in self.maxs or v > self.maxs[k]:
                self.maxs[k] = v
                self.max_at[k] = row["t_wall"]
            self.sums[k] = self.sums.get(k, 0.0) + v
            self.counts[k] = self.counts.get(k, 0) + 1
        for ev in events:
            self.events[ev.kind] = self.events.get(ev.kind, 0) + 1


class TripLogger:
    def __init__(self, logs_dir: Path, simulated: bool = False, start: Optional[datetime] = None,
                 interval: float = 0.5, pre_s: float = 60.0, post_s: float = 30.0,
                 fsync: bool = True, on_error: Optional[Callable[[str], None]] = None,
                 summary_every_s: float = 30.0) -> None:
        self.start = start or datetime.now()
        self.simulated = simulated
        self.prefix = "sim_" if simulated else ""
        self.trip_id = f"{self.start:%Y-%m-%d_%H-%M-%S}"
        self.dir = Path(logs_dir)
        self.events_dir = self.dir / "eventos"
        self.csv_path = self._unique(self.dir / f"{self.prefix}obd_{self.trip_id}.csv")
        stem_id = self.csv_path.stem.split("obd_", 1)[1]
        self.events_path = self.dir / f"{self.prefix}eventos_{stem_id}.log"
        self.summary_path = self.dir / f"{self.prefix}resumo_{stem_id}.txt"
        self.readings_path = self.dir / f"{self.prefix}leituras_{stem_id}.csv"
        self.readings_count = 0
        self.interval = interval
        self.pre_s = pre_s
        self.post_s = post_s
        self.fsync = fsync
        self.on_error = on_error
        self.summary_every_s = summary_every_s
        self.ring = RingBuffer(pre_s)
        self.stats = TripStats(self.start)
        self.snapshots_open: list[_Snapshot] = []
        self.snapshot_paths: list[Path] = []
        self.errors: list[str] = []
        self._q: "queue.Queue[tuple]" = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._csv = None
        self._evlog = None
        self._readings = None
        self._readings_dirty = False
        self._readings_synced = 0.0
        self._last_summary_t = 0.0
        self.summary_extra: dict = {}

    @staticmethod
    def _unique(p: Path) -> Path:
        if not p.exists():
            return p
        i = 2
        while True:
            q = p.with_name(f"{p.stem}_{i}{p.suffix}")
            if not q.exists():
                return q
            i += 1

    # -- API (qualquer thread) ------------------------------------------------
    def open(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        self.events_dir.mkdir(parents=True, exist_ok=True)
        self._csv = open(self.csv_path, "w", encoding=ENCODING, newline="")
        self._csv.write(HEADER)
        self._sync(self._csv)
        self._evlog = open(self.events_path, "a", encoding=ENCODING, newline="\r\n")
        kind = "SIMULADOR" if self.simulated else "carro real"
        self._evlog.write(f"Viagem {self.trip_id} ({kind})\n")
        self._sync(self._evlog)
        self._readings = open(self.readings_path, "w", encoding=ENCODING, newline="")
        self._readings.write(READING_HEADER)
        self._sync(self._readings)
        self._thread = threading.Thread(target=self._run, name="obd-logger", daemon=True)
        self._thread.start()

    def log_tick(self, row: dict, events: list[Event]) -> None:
        self._q.put(("tick", row, list(events)))

    def log_reading(self, t_wall: datetime, pid: str, name: str, value: str, latency_ms=None) -> None:
        """Uma leitura real (formato longo). Gravada em lote: flush/fsync a cada 0,5 s."""
        self._q.put(("reading", format_reading(t_wall, pid, name, value, latency_ms)))

    def close(self, summary_extra: Optional[dict] = None, timeout: float = 10.0) -> None:
        if summary_extra:
            self.summary_extra.update(summary_extra)
        self._q.put(("close",))
        if self._thread:
            self._thread.join(timeout)

    def flush_wait(self, timeout: float = 5.0) -> None:
        """Espera a fila esvaziar (usado nos testes)."""
        done = threading.Event()
        self._q.put(("barrier", done))
        done.wait(timeout)

    # -- thread de escrita ----------------------------------------------------
    def _err(self, msg: str) -> None:
        log.error(msg)
        self.errors.append(msg)
        if self.on_error:
            try:
                self.on_error(msg)
            except Exception:
                pass

    def _sync(self, fh) -> None:
        fh.flush()
        if self.fsync:
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass

    def _run(self) -> None:
        while True:
            item = self._q.get()
            try:
                if item[0] == "tick":
                    self._tick(item[1], item[2])
                elif item[0] == "reading":
                    self._reading(item[1])
                elif item[0] == "barrier":
                    item[1].set()
                elif item[0] == "close":
                    self._close()
                    return
            except Exception as e:  # disco cheio, arquivo bloqueado...
                self._err(f"Erro ao gravar log: {e}")

    def _reading(self, line: str) -> None:
        self._readings.write(line)
        self.readings_count += 1
        self._readings_dirty = True
        now = time.monotonic()
        if now - self._readings_synced >= READINGS_SYNC_S:
            self._sync_readings(now)

    def _sync_readings(self, now: Optional[float] = None) -> None:
        if self._readings is not None and self._readings_dirty:
            self._sync(self._readings)
            self._readings_dirty = False
        self._readings_synced = time.monotonic() if now is None else now

    def _tick(self, row: dict, events: list[Event]) -> None:
        self._sync_readings()
        line = format_row(row)
        self._csv.write(line)
        self._sync(self._csv)
        self.stats.add(row, events)
        self.ring.append(row["t_s"], line)

        # instantâneos abertos recebem as linhas "depois" do evento
        for snap in list(self.snapshots_open):
            snap.fh.write(line)
            self._sync(snap.fh)
            if row["t_s"] >= snap.end_t:
                snap.fh.close()
                self.snapshots_open.remove(snap)

        for ev in events:
            text = format_event_line(ev)
            self.stats.event_lines.append(text)
            self._evlog.write(text + "\n")
            self._sync(self._evlog)
            if ev.snapshot:
                self._open_snapshot(ev, row)

        if row["t_s"] - self._last_summary_t >= self.summary_every_s:
            self._last_summary_t = row["t_s"]
            self._write_summary(final=False)

    def _open_snapshot(self, ev: Event, row: dict) -> None:
        safe = ev.code.replace(":", "_").replace("/", "_")
        p = self._unique(self.events_dir / f"{self.prefix}evento_{ev.t_wall:%Y-%m-%d_%H-%M-%S}_{safe}.csv")
        fh = open(p, "w", encoding=ENCODING, newline="")
        fh.write(HEADER)
        for line in self.ring.items():  # 60 s antes (inclui a linha do evento)
            fh.write(line)
        self._sync(fh)
        self.snapshot_paths.append(p)
        self.snapshots_open.append(_Snapshot(p, row["t_s"] + self.post_s, fh))

    def _close(self) -> None:
        for snap in self.snapshots_open:
            try:
                snap.fh.close()
            except Exception:
                pass
        self.snapshots_open.clear()
        for fh in (self._csv, self._evlog, self._readings):
            try:
                if fh:
                    self._sync(fh)
                    fh.close()
            except Exception:
                pass
        self._write_summary(final=True)

    # -- resumo ---------------------------------------------------------------
    def summary_text(self, final: bool = True) -> str:
        s = self.stats
        dur = s.last_t
        h, rem = divmod(int(dur), 3600)
        m, sec = divmod(rem, 60)
        lines = [
            f"RESUMO DA VIAGEM {self.trip_id}" + ("  [SIMULADOR – dados FALSOS]" if self.simulated else ""),
            "" if final else "(resumo parcial – a viagem ainda não tinha terminado)",
            f"Início: {s.started:%d/%m/%Y %H:%M:%S}",
            f"Duração: {h:02d}:{m:02d}:{sec:02d}",
            f"Linhas gravadas: {s.rows}  (sem dados: {s.rows_without_data}, ≈ {s.rows_without_data * self.interval:.0f} s)",
            f"Arquivo: {self.csv_path.name}",
            f"Leituras individuais: {self.readings_path.name} ({self.readings_count} leituras)",
            "",
            "Parâmetro                    mínimo     máximo     média   (hora do máximo)",
        ]
        for k in VALUE_KEYS:
            d = BY_KEY[k]
            if k not in s.counts:
                lines.append(f"  {d.name:<26} {'sem dados':>10}")
                continue
            avg = s.sums[k] / s.counts[k]
            lines.append(f"  {d.name + ' (' + d.unit + ')':<26} {fmt_value(k, s.mins[k]):>10} {fmt_value(k, s.maxs[k]):>10}"
                         f" {fmt_value(k, avg):>9}   ({s.max_at[k]:%H:%M:%S})")
        lines += ["", f"Eventos: {sum(s.events.values())}"]
        for kind, n in sorted(s.events.items()):
            lines.append(f"  {kind}: {n}")
        if self.snapshot_paths:
            lines += ["", "Instantâneos de evento (60 s antes + 30 s depois):"]
            lines += [f"  eventos/{p.name}" for p in self.snapshot_paths]
        if s.event_lines:
            lines += ["", "Linha do tempo:"] + [f"  {t}" for t in s.event_lines]
        for k, v in self.summary_extra.items():
            lines.append(f"{k}: {v}")
        return "\n".join(lines) + "\n"

    def _write_summary(self, final: bool) -> None:
        try:
            tmp = self.summary_path.with_suffix(".tmp")
            with open(tmp, "w", encoding=ENCODING, newline="\r\n") as fh:
                fh.write(self.summary_text(final))
                self._sync(fh)
            os.replace(tmp, self.summary_path)
        except Exception as e:
            self._err(f"Erro ao gravar resumo: {e}")
