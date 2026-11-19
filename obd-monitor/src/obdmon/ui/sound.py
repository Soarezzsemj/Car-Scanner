"""Avisos sonoros (winsound no Windows). Tocam numa thread para não travar a GUI."""
from __future__ import annotations

import threading

try:
    import winsound
except ImportError:  # fora do Windows: silencioso
    winsound = None

PATTERNS = {
    "mark": [(1500, 90), (0, 40), (1900, 140)],
    "alert": [(880, 180), (0, 80), (880, 180)],
    "critical": [(660, 220), (0, 60), (990, 220), (0, 60), (660, 220), (0, 60), (990, 300)],
    "lost": [(900, 160), (700, 160), (500, 260)],
    "restored": [(600, 100), (900, 140)],
}

_lock = threading.Lock()
enabled = True


def _play(pattern) -> None:
    if not _lock.acquire(blocking=False):
        return  # já tocando: não empilha sons
    try:
        for freq, dur in pattern:
            if freq == 0:
                threading.Event().wait(dur / 1000)
            else:
                winsound.Beep(freq, dur)
    except Exception:
        pass
    finally:
        _lock.release()


def beep(kind: str) -> None:
    if not enabled or winsound is None:
        return
    threading.Thread(target=_play, args=(PATTERNS.get(kind, PATTERNS["alert"]),), daemon=True).start()
