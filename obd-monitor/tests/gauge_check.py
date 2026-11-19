"""Verificador do layout dos medidores (texto "mín/máx" nunca cortado).

Usado pelos testes em processo (escala 100%) e também como script, para
testar escalas de tela do Windows (o QT_SCALE_FACTOR precisa ser definido
antes de criar o QApplication, por isso roda num processo separado):

    python tests/gauge_check.py --scale 1.5 --out pasta_capturas
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if os.name == "nt":
    os.environ.setdefault("QT_QPA_FONTDIR", os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"))

import numpy as np  # noqa: E402
from PySide6.QtCore import QPointF, Qt  # noqa: E402
from PySide6.QtGui import QColor, QFontMetricsF, QImage, QPainter  # noqa: E402

# tamanhos de medidor (w, h) – do mínimo permitido até tela grande
GAUGE_SIZES = [(190, 190), (200, 420), (240, 240), (260, 330), (330, 260), (350, 600),
               (460, 760), (700, 245), (900, 400), (1000, 1000)]
# telas reais (px físicos) x escala do Windows; a janela é testada maximizada
# (descontando a barra de tarefas) e no tamanho mínimo; o modo viagem em tela cheia
SCREENS = [("1366x768", 1.0), ("1366x768", 1.25), ("1920x1080", 1.0), ("1920x1080", 1.25),
           ("1920x1080", 1.5), ("2560x1440", 1.5), ("3440x1440", 1.0)]
TASKBAR_PX = 48

def _img_array(img: QImage) -> np.ndarray:
    img = img.convertToFormat(QImage.Format.Format_RGBA8888)
    ptr = img.constBits()
    arr = np.frombuffer(ptr, dtype=np.uint8, count=img.sizeInBytes())
    # .copy(): o QImage convertido é temporário; sem copiar, o numpy apontaria para memória liberada
    return arr.reshape(img.height(), img.bytesPerLine())[:, : img.width() * 4].reshape(
        img.height(), img.width(), 4).copy()


def _ink_bbox(a: np.ndarray, b: np.ndarray):
    diff = np.abs(a.astype(int) - b.astype(int)).max(axis=2) > 24
    ys, xs = np.nonzero(diff)
    if len(ys) == 0:
        return None
    return xs.min(), ys.min(), xs.max(), ys.max()


def _reference_ink(text: str, font, dpr: float):
    """Tinta do mesmo texto, mesma fonte, desenhado sem nenhum limite."""
    fm = QFontMetricsF(font)
    w, h = int(fm.horizontalAdvance(text) + 60), int(fm.height() + 60)
    imgs = []
    for draw in (False, True):
        img = QImage(int(w * dpr), int(h * dpr), QImage.Format.Format_RGBA8888)
        img.setDevicePixelRatio(dpr)
        img.fill(QColor("#151b23"))
        if draw:
            p = QPainter(img)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            p.setFont(font)
            p.setPen(QColor("#8a97a6"))
            p.drawText(QPointF(30, 30 + fm.ascent()), text)
            p.end()
        imgs.append(_img_array(img))
    x0, y0, x1, y1 = _ink_bbox(imgs[0], imgs[1])
    return x1 - x0 + 1, y1 - y0 + 1


def check_gauge(g, label: str = "") -> list[str]:
    """Confere um medidor já dimensionado. Devolve a lista de problemas."""
    errs: list[str] = []
    w, h = g.width(), g.height()
    name = f"{label}{g.title} {w}x{h}"
    text = g.footer_text()
    L = g.compute_layout(w, h, text)
    fr = L.foot_rect
    fm = QFontMetricsF(L.foot_font)
    # 1) geometria
    if fr.top() < 0 or fr.bottom() > h - 2:
        errs.append(f"{name}: área do rodapé fora do cartão ({fr.top():.1f}–{fr.bottom():.1f}, h={h})")
    if fr.height() < fm.ascent() + fm.descent() - 0.01:
        errs.append(f"{name}: área do rodapé menor que ascent+descent")
    if L.cy + L.below > fr.top() + 0.01:
        errs.append(f"{name}: arco invade o rodapé ({L.cy + L.below:.1f} > {fr.top():.1f})")
    if L.cy - L.outer < L.title_rect.bottom() - 0.01:
        errs.append(f"{name}: arco invade o título")
    if fm.horizontalAdvance(text) > fr.width() + 0.5:
        errs.append(f"{name}: texto do rodapé mais largo que o medidor")
    if 2 * L.outer > w - 2 * 8 + 0.5:
        errs.append(f"{name}: arco mais largo que o medidor")
    # 2) tinta real: com e sem rodapé
    g._draw_footer = False
    without = _img_array(g.grab().toImage())
    g._draw_footer = True
    pm = g.grab()
    dpr = pm.devicePixelRatio()
    with_ = _img_array(pm.toImage())
    bb = _ink_bbox(without, with_)
    if bb is None:
        errs.append(f"{name}: rodapé não desenhou nada")
        return errs
    x0, y0, x1, y1 = bb
    ref_w, ref_h = _reference_ink(text, L.foot_font, dpr)
    ink_w, ink_h = x1 - x0 + 1, y1 - y0 + 1
    H = with_.shape[0]
    if ink_h < ref_h - 1:
        errs.append(f"{name}: texto mín/máx CORTADO na vertical (tinta {ink_h}px, esperado {ref_h}px)")
    if ink_w < ref_w - 2:
        errs.append(f"{name}: texto mín/máx CORTADO na horizontal (tinta {ink_w}px, esperado {ref_w}px)")
    if y1 > H - 1 - 2 * dpr:   # tem que sobrar margem acima da borda do cartão
        errs.append(f"{name}: texto encosta na borda de baixo (y={y1}, altura={H})")
    if not ("mín" in text and "máx" in text):
        errs.append(f"{name}: texto inesperado {text!r}")
    return errs


def standalone_gauges() -> list[str]:
    from obdmon.ui import theme
    from obdmon.ui.gauges import ArcGauge
    errs = []
    for big in (False, True):
        for supported in (True, False):
            for w, h in GAUGE_SIZES:
                g = ArcGauge("Arrefecimento", "°C", 40, 130,
                             [(60, theme.BLUE), (105, theme.GREEN), (112, theme.YELLOW), (999, theme.RED)], big=big)
                g.resize(w, h)
                g.set_value(72.0)
                g.set_value(118.0)
                g.shown = g.target
                g.set_supported(supported)
                errs += check_gauge(g, "avulso: ")
                rpm = ArcGauge("RPM", "rpm", 0, 7000, [(4500, theme.GREEN)], big=big, fmt=lambda v: f"{v:.0f}")
                rpm.resize(w, h)
                rpm.set_value(787.0)
                rpm.set_value(16383.75)   # texto mais longo possível
                rpm.shown = rpm.target
                errs += check_gauge(rpm, "avulso: ")
    return errs


def _overlaps(widgets, root) -> list[str]:
    from PySide6.QtCore import QRect
    errs = []
    rects = []
    for g in widgets:
        tl = g.mapTo(root, g.rect().topLeft())
        rects.append((g, QRect(tl, g.size())))
    for i, (g1, r1) in enumerate(rects):
        par = g1.parentWidget()
        if g1.width() < g1.minimumWidth() or g1.height() < g1.minimumHeight():
            errs.append(f"{g1.title}: espremido abaixo do mínimo ({g1.width()}x{g1.height()})")
        if not par.rect().contains(g1.geometry()):
            errs.append(f"{g1.title}: sai da área do pai ({g1.geometry()} fora de {par.rect()})")
        for g2, r2 in rects[i + 1:]:
            if r1.intersects(r2):
                errs.append(f"{g1.title} e {g2.title} se SOBREPÕEM")
    return errs


def _topbar_checks(w) -> list[str]:
    from PySide6.QtWidgets import QLabel, QPushButton
    errs = []
    if w.topbar_width_needed() > w.topbar.width():
        errs.append(f"barra do topo não cabe ({w.topbar_width_needed()} > {w.topbar.width()})")
    for c in w.topbar.findChildren(QPushButton) + w.topbar.findChildren(QLabel):
        if c.isVisible() and c.width() < c.sizeHint().width() - 1:
            errs.append(f"topo: '{c.text()[:25]}' espremido ({c.width()} < {c.sizeHint().width()})")
    # não pode estar mais compacta que o necessário: o nível anterior NÃO pode caber
    level = w._topbar_level
    if level > 0:
        w._apply_topbar_level(level - 1)
        if w.topbar_width_needed() <= w.topbar.width():
            errs.append(f"barra do topo compacta à toa (nível {level}, o nível {level - 1} cabe)")
        w._apply_topbar_level(level)
    return errs


def _button_checks(b) -> list[str]:
    """Cada linha do texto do botão cabe na largura útil (fonte efetiva, já com o estilo)."""
    b.ensurePolished()
    fm = QFontMetricsF(b.font())
    widest = max(fm.horizontalAdvance(t) for t in b.text().split("\n"))
    avail = b.width() - 2 * 16
    if widest > avail:
        return [f"botão '{b.text().splitlines()[0]}' cortado ({widest:.0f} > {avail} px)"]
    return []


def window_checks(app, out: Path | None = None, tag: str = "") -> list[str]:
    from PySide6.QtTest import QTest

    from obdmon.config import Config
    from obdmon.engine import AsyncRunner
    from obdmon.ui.main_window import MIN_H, MIN_W, MainWindow

    cfg = Config(logs_dir=str(Path(os.environ.get("TEMP", ".")) / "obdmon_gauge_check"), sound=False)
    cfg.save = lambda *a, **k: None
    runner = AsyncRunner()
    w = MainWindow(cfg, runner, simulated=True, autostart=False)
    errs: list[str] = []

    def settle():
        for _ in range(4):
            QTest.qWait(60)
            app.processEvents()

    try:
        for vals in ({"velocidade_kmh": 3, "rpm": 787, "arrefecimento_c": 72, "oleo_c": 63},
                     {"velocidade_kmh": 187, "rpm": 6543, "arrefecimento_c": 118, "oleo_c": 141}):
            w._set_values(vals)
        for g in list(w.gauges.values()) + list(w.trip_gauges.values()) + list(w.bars.values()):
            g.shown = g.target
        scr = app.primaryScreen().geometry()
        dpr = app.primaryScreen().devicePixelRatio()
        work = (scr.width(), scr.height() - round(TASKBAR_PX / dpr))
        w.show()
        sizes = [("max", work)]
        if work[0] > MIN_W + 50 or work[1] > MIN_H + 50:
            sizes.append(("min", (MIN_W, MIN_H)))
        for name, (ww, hh) in sizes:
            w.showNormal()
            w.resize(ww, hh)
            settle()
            lbl = f"painel {name} {w.width()}x{w.height()}: "
            for g in w.gauges.values():
                errs += check_gauge(g, lbl)
            errs += [lbl + e for e in _overlaps(list(w.gauges.values()) + list(w.bars.values()), w)]
            errs += [lbl + e for e in _topbar_checks(w)]
            errs += [lbl + e for e in _button_checks(w.oil_btn)]
            if w.width() > work[0] or w.height() > work[1]:
                errs.append(f"{lbl}janela maior que a tela útil {work}")
            if out:
                w.grab().save(str(out / f"painel_{tag}{name}.png"))
        # modo viagem em tela cheia (F11) na tela emulada
        w.showNormal()
        w.resize(*work)
        settle()
        w.toggle_trip_mode()
        settle()
        lbl = f"viagem tela cheia {w.width()}x{w.height()} ({w.trip_grid.cols} col): "
        for g in w.trip_gauges.values():
            errs += check_gauge(g, lbl)
        errs += [lbl + e for e in _overlaps(list(w.trip_gauges.values()), w)]
        errs += [lbl + e for e in _button_checks(w.trip_oil)]
        if not w.trip_badge.text():
            errs.append(lbl + "badge de conexão vazio no modo viagem")
        if out:
            w.grab().save(str(out / f"viagem_{tag}telacheia.png"))
        w.toggle_trip_mode()
        settle()
    finally:
        w.shutdown(timeout=2)
        w.close()
        runner.shutdown()
    return errs


def main() -> int:
    """Emula uma tela física (configfile do plugin offscreen) com a escala do Windows."""
    ap = argparse.ArgumentParser()
    ap.add_argument("--scale", default="1")
    ap.add_argument("--screen", default="1920x1080", help="resolução física, ex.: 1366x768")
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    import json
    import tempfile
    sw, sh = (int(x) for x in a.screen.lower().split("x"))
    cfgfile = Path(tempfile.gettempdir()) / f"obdmon_tela_{sw}x{sh}.json"
    cfgfile.write_text(json.dumps({"screens": [{"name": "tela", "x": 0, "y": 0, "width": sw, "height": sh}]}))
    # caminho RELATIVO: o Qt separa as opções da plataforma por ":", e o "C:" de um caminho absoluto quebra
    rel = os.path.relpath(cfgfile, os.getcwd()).replace("\\", "/")
    os.environ["QT_QPA_PLATFORM"] = f"offscreen:configfile={rel}"
    os.environ["QT_SCALE_FACTOR"] = a.scale
    from PySide6.QtWidgets import QApplication

    from obdmon.ui import theme
    app = QApplication([])
    app.setStyle("Fusion")
    app.setStyleSheet(theme.STYLESHEET)
    out = Path(a.out) if a.out else None
    if out:
        out.mkdir(parents=True, exist_ok=True)
    s = app.primaryScreen()
    print(f"tela {a.screen} escala {a.scale}: lógica {s.geometry().width()}x{s.geometry().height()}, "
          f"devicePixelRatio={s.devicePixelRatio()}")
    tag = f"{a.screen}_{int(round(float(a.scale) * 100))}_"
    errs = standalone_gauges() + window_checks(app, out, tag=tag)
    for e in errs:
        print("FALHA:", e)
    print(f"{len(errs)} problema(s)")
    return 1 if errs else 0


if __name__ == "__main__":
    sys.exit(main())


__all__ = ["check_gauge", "standalone_gauges", "window_checks", "Qt"]
