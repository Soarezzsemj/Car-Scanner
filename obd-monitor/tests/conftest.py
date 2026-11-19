import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

def _offscreen_platform() -> str:
    """Plugin offscreen com uma tela 1920x1080 (o padrão do Qt é 800x800, irreal).
    Caminho RELATIVO: o Qt separa as opções por ":", e o "C:" de um caminho absoluto quebra."""
    import json
    import tempfile
    cfg = Path(tempfile.gettempdir()) / "obdmon_tela_testes_1920x1080.json"
    cfg.write_text(json.dumps({"screens": [{"name": "tela", "x": 0, "y": 0, "width": 1920, "height": 1080}]}))
    try:
        return "offscreen:configfile=" + os.path.relpath(cfg).replace("\\", "/")
    except ValueError:  # outra unidade de disco: fica com a tela padrão
        return "offscreen"


os.environ.setdefault("QT_QPA_PLATFORM", _offscreen_platform())
if os.name == "nt":  # o plugin offscreen não acha as fontes do Windows sozinho
    os.environ.setdefault("QT_QPA_FONTDIR", os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"))

import asyncio  # noqa: E402

import pytest  # noqa: E402

from obdmon.config import Config  # noqa: E402

FAST_LAT = (0.002, 0.006)
FAST_SEARCH = (0.02, 0.05)


@pytest.fixture
def cfg():
    return Config()


def run(coro):
    return asyncio.run(coro)
