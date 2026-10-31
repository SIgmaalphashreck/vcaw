"""RISEN TWEAKS - UI-Effekte (Hover-Fades, Gradient-Leiste, Puls).

Rein visuell; enthaelt keine Systemzugriffe. Alle Funktionen sind gegen
TclError gesichert (Widget kann waehrend einer Animation zerstoert werden).
"""
from __future__ import annotations

from typing import Callable


def _clean(color: str) -> str:
    return color.lstrip("#")


def hex_to_rgb(color: str) -> tuple[int, int, int]:
    c = _clean(color)
    return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)


def rgb_to_hex(rgb: tuple[float, float, float]) -> str:
    r, g, b = (max(0, min(255, int(round(v)))) for v in rgb)
    return f"#{r:02x}{g:02x}{b:02x}"


def lerp_color(a: str, b: str, t: float) -> str:
    t = max(0.0, min(1.0, t))
    ra, rb = hex_to_rgb(a), hex_to_rgb(b)
    return rgb_to_hex(tuple(x + (y - x) * t for x, y in zip(ra, rb)))


def gradient_stops(colors: list[str], n: int) -> list[str]:
    """n Farbwerte entlang einer Farbliste (z. B. violett -> cyan)."""
    if n <= 1 or len(colors) == 1:
        return [colors[0]] * max(1, n)
    out = []
    seg = len(colors) - 1
    for i in range(n):
        pos = i / (n - 1) * seg
        k = min(int(pos), seg - 1)
        out.append(lerp_color(colors[k], colors[k + 1], pos - k))
    return out


def _pointer_inside(widget) -> bool:
    try:
        x, y = widget.winfo_pointerxy()
        wx, wy = widget.winfo_rootx(), widget.winfo_rooty()
        return wx <= x < wx + widget.winfo_width() and wy <= y < wy + widget.winfo_height()
    except Exception:  # noqa: BLE001
        return False


def _walk(widget):
    yield widget
    try:
        for child in widget.winfo_children():
            yield from _walk(child)
    except Exception:  # noqa: BLE001
        return


def hover_fade(widget, props: dict[str, tuple[str, str]], steps: int = 7, delay: int = 16,
               on_change: Callable[[float], None] | None = None) -> None:
    """Blendet Widget-Eigenschaften beim Hover weich zwischen (normal, hover) ein/aus.

    props: {"fg_color": (normal, hover), "border_color": (normal, hover)}
    Bindet auch alle Kind-Widgets, damit ein Wechsel auf ein Kind kein Flackern ausloest.
    Rufe die Funktion NACH dem Aufbau der Kinder auf.
    """
    st = {"t": 0.0, "dir": 0, "job": None}

    def render():
        cfg = {k: lerp_color(a, b, st["t"]) for k, (a, b) in props.items()}
        try:
            widget.configure(**cfg)
        except Exception:  # noqa: BLE001
            st["dir"] = 0
            return False
        if on_change:
            on_change(st["t"])
        return True

    def tick():
        st["job"] = None
        st["t"] = max(0.0, min(1.0, st["t"] + st["dir"] / steps))
        if not render():
            return
        if (st["dir"] > 0 and st["t"] < 1.0) or (st["dir"] < 0 and st["t"] > 0.0):
            try:
                st["job"] = widget.after(delay, tick)
            except Exception:  # noqa: BLE001
                st["job"] = None

    def go(direction: int):
        st["dir"] = direction
        if st["job"] is None:
            tick()

    def enter(_e=None):
        go(1)

    def leave(_e=None):
        if _pointer_inside(widget):
            return
        go(-1)

    for w in _walk(widget):
        try:
            w.bind("<Enter>", enter, add="+")
            w.bind("<Leave>", leave, add="+")
        except Exception:  # noqa: BLE001
            pass


def pulse(widget, prop: str, a: str, b: str, period_ms: int = 1800, fps: int = 10,
          alive: Callable[[], bool] = lambda: True) -> None:
    """Dauerhaft sanft zwischen zwei Farben pendeln (z. B. Statuspunkt)."""
    import math
    frame_ms = max(16, 1000 // fps)
    st = {"i": 0}

    def tick():
        if not alive():
            return
        st["i"] += frame_ms
        t = (math.sin(st["i"] / period_ms * 2 * math.pi) + 1) / 2
        try:
            widget.configure(**{prop: lerp_color(a, b, t)})
            widget.after(frame_ms, tick)
        except Exception:  # noqa: BLE001
            return
    tick()


def draw_gradient(canvas, colors: list[str], width: int, height: int) -> None:
    canvas.delete("grad")
    width = max(2, width)
    for x, col in enumerate(gradient_stops(colors, width)):
        canvas.create_line(x, 0, x, height, fill=col, tags="grad")


def shimmer(canvas, base_colors: list[str], width_fn: Callable[[], int], height: int, color: str = "#ffffff",
            alive: Callable[[], bool] = lambda: True, speed: int = 14, fps: int = 12) -> None:
    """Heller Lichtstreifen, der ueber die Gradient-Leiste wandert (mischt mit der Gradientenfarbe)."""
    frame_ms = max(16, 1000 // fps)
    st = {"x": -80, "w": 0, "stops": []}

    def tick():
        if not alive():
            return
        try:
            w = max(2, int(width_fn()))
        except Exception:  # noqa: BLE001
            w = 2
        if w != st["w"]:
            st["w"], st["stops"] = w, gradient_stops(base_colors, w)
        st["x"] += speed
        if st["x"] > w + 80:
            st["x"] = -120
        try:
            canvas.delete("shim")
            for i in range(-40, 41, 4):
                px = st["x"] + i
                if 0 <= px < w:
                    strength = (1 - abs(i) / 40) * 0.7
                    canvas.create_line(px, 0, px, height, tags="shim",
                                       fill=lerp_color(st["stops"][int(px)], color, strength))
            canvas.after(frame_ms, tick)
        except Exception:  # noqa: BLE001
            return
    tick()
