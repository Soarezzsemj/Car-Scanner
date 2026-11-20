"""O texto "mín X   máx Y" dos medidores nunca pode sair cortado: em janela
pequena, grande, tela cheia (F11) e com escala de tela de 125% e 150%."""
import os
import subprocess
import sys
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication

from obdmon.ui import theme

from . import gauge_check

HERE = Path(__file__).resolve().parent


@pytest.fixture(scope="module")
def app():
    a = QApplication.instance() or QApplication([])
    a.setStyle("Fusion")
    a.setStyleSheet(theme.STYLESHEET)
    return a


# A renderização pesada (centenas de medidores + janelas) roda SÓ nos subprocessos de
# test_real_screens_and_windows_scaling: feita aqui, no mesmo processo dos outros testes,
# ela esgota o fallback de fontes de emoji do plugin offscreen e as capturas do README
# (test_gui) sairiam com quadradinhos no lugar dos emojis.


def test_small_gauge_shrinks_instead_of_clipping(app):
    """Medidor no tamanho mínimo: fontes e arco diminuem, nada sai do cartão."""
    from obdmon.ui.gauges import FOOT_MIN_PX, ArcGauge
    big = ArcGauge("RPM", "rpm", 0, 7000, [], fmt=lambda v: f"{v:.0f}")
    small = ArcGauge("RPM", "rpm", 0, 7000, [], fmt=lambda v: f"{v:.0f}")
    for g in (big, small):
        g.set_value(800.0)
        g.set_value(6543.0)
    Lb = big.compute_layout(460, 700)
    Ls = small.compute_layout(190, 190)
    assert Ls.r < Lb.r
    assert Ls.foot_font.pixelSize() <= Lb.foot_font.pixelSize()
    assert Ls.foot_font.pixelSize() >= FOOT_MIN_PX
    assert Ls.foot_rect.bottom() <= 190 - 2


@pytest.mark.parametrize("screen,scale", gauge_check.SCREENS,
                         ids=[f"{s}@{int(k * 100)}%" for s, k in gauge_check.SCREENS])
def test_real_screens_and_windows_scaling(screen, scale, tmp_path):
    """Tela física emulada + escala de tela do Windows (processo separado: o
    QT_SCALE_FACTOR e a tela precisam existir antes de criar o QApplication)."""
    env = dict(os.environ, PYTHONPATH=str(HERE.parent / "src"), PYTHONIOENCODING="utf-8")
    r = subprocess.run([sys.executable, str(HERE / "gauge_check.py"), "--screen", screen, "--scale", str(scale),
                        "--out", str(tmp_path)],
                       capture_output=True, text=True, encoding="utf-8", env=env, timeout=300)
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"devicePixelRatio={float(scale)}" in r.stdout, r.stdout
    assert "0 problema(s)" in r.stdout


def test_stale_value_is_yellow(app):
    """Valor com idade > 1,5 s: número em amarelo (sem renderizar)."""
    from obdmon.ui.gauges import ArcGauge, BarGauge
    for g in (ArcGauge("RPM", "rpm", 0, 7000, []), BarGauge("Carga", "%", 0, 100, [])):
        g.set_value(800)
        assert g.value_color().name() == theme.TEXT.lower()
        g.set_stale(True)
        assert g.value_color().name() == theme.YELLOW.lower()
        g.set_stale(False)
        assert g.value_color().name() == theme.TEXT.lower()
