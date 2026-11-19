"""Configurações persistentes (config.json ao lado do programa)."""
from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, fields
from pathlib import Path

log = logging.getLogger(__name__)

APP_DIR = Path(os.environ.get("OBDMON_HOME") or Path(__file__).resolve().parents[2])
CONFIG_PATH = APP_DIR / "config.json"


@dataclass
class Config:
    coolant_warn: float = 105.0
    coolant_crit: float = 112.0
    oil_warn: float = 125.0
    oil_crit: float = 135.0
    voltage_low: float = 11.8
    log_interval: float = 0.5
    chart_window_min: float = 5.0
    sound: bool = True
    temp_unit: str = "C"            # "C" ou "F" (somente exibição; o log é sempre em °C)
    preferred_address: str = ""     # adaptador preferido (se vazio, usa o último)
    last_address: str = ""
    last_name: str = ""
    logs_dir: str = ""              # vazio = <pasta do programa>/logs
    sim_scenario: str = "normal"
    sim_factor: float = 0.0         # 0 = padrão do cenário

    @property
    def logs_path(self) -> Path:
        return Path(self.logs_dir) if self.logs_dir else APP_DIR / "logs"

    @classmethod
    def load(cls, path: Path = CONFIG_PATH) -> "Config":
        cfg = cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            names = {f.name: f.type for f in fields(cls)}
            for k, v in data.items():
                if k in names:
                    setattr(cfg, k, type(getattr(cfg, k))(v))
        except FileNotFoundError:
            pass
        except Exception as e:  # arquivo corrompido: usa padrões
            log.warning("config.json inválido (%s); usando padrões", e)
        return cfg

    def save(self, path: Path = CONFIG_PATH) -> None:
        try:
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(asdict(self), indent=2, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, path)
        except Exception as e:
            log.warning("Não foi possível salvar config.json: %s", e)

    def display_temp(self, c: float) -> float:
        return c * 9 / 5 + 32 if self.temp_unit == "F" else c

    @property
    def temp_symbol(self) -> str:
        return "°F" if self.temp_unit == "F" else "°C"
