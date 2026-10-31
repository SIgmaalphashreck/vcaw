"""RISEN TWEAKS - leichte Canvas-Widgets (Ring-Anzeige, Verlaufsgrafik).

Nur tkinter, keine Systemzugriffe. Gezeichnet wird nur, wenn sich etwas aendert,
damit die Oberflaeche fluessig bleibt.
"""
from __future__ import annotations

import tkinter as tk
from collections import deque
from typing import Optional

from fx import lerp_color


def clamp01(v: float) -> float:
    return max(0.0, min(1.0, v))


class RingGauge(tk.Canvas):
    """Ring mit Prozentwert, weicher Animation und Farbwechsel bei hoher Last."""

    def __init__(self, master, size: int = 150, thickness: int = 13, color: str = "#8B5CF6", color2: str = "#22D3EE",
                 warn: str = "#F43F5E", bg: str = "#0D0D18", track: str = "#1C1C30", text_color: str = "#F2F3FF",
                 sub_color: str = "#7C7F99", font: str = "Segoe UI"):
        super().__init__(master, width=size, height=size, bg=bg, highlightthickness=0, bd=0)
        self.size, self.th = size, thickness
        self.c1, self.c2, self.warn = color, color2, warn
        self._target = 0.0
        self._shown = 0.0
        self._job: Optional[str] = None
        pad = thickness // 2 + 3
        self._box = (pad, pad, size - pad, size - pad)
        self.create_oval(*self._box, outline=track, width=thickness)
        self._arc = self.create_arc(*self._box, start=90, extent=0, style="arc", width=thickness, outline=color2)
        self._text = self.create_text(size // 2, size // 2 - 6, text="–", fill=text_color, font=(font, max(14, size // 6), "bold"))
        self._sub = self.create_text(size // 2, size // 2 + size // 7, text="", fill=sub_color, font=(font, 8))

    def set(self, frac: Optional[float], text: str, sub: str = "") -> None:
        self.itemconfigure(self._text, text=text)
        self.itemconfigure(self._sub, text=sub)
        self._target = clamp01(frac) if frac is not None else 0.0
        if self._job is None and abs(self._target - self._shown) > 0.002:
            self._step()

    def _step(self) -> None:
        self._job = None
        diff = self._target - self._shown
        self._shown = self._target if abs(diff) < 0.004 else self._shown + diff * 0.35
        col = lerp_color(self.c1, self.c2, self._shown)
        if self._shown > 0.75:
            col = lerp_color(col, self.warn, clamp01((self._shown - 0.75) / 0.25))
        try:
            self.itemconfigure(self._arc, extent=-359.9 * self._shown, outline=col)
            if abs(self._target - self._shown) > 0.002:
                self._job = self.after(33, self._step)
        except tk.TclError:
            self._job = None


class Sparkline(tk.Canvas):
    """Verlaufsgrafik der letzten Werte (0..vmax) mit Flaeche und Gitternetz."""

    def __init__(self, master, height: int = 74, color: str = "#22D3EE", bg: str = "#0D0D18", grid: str = "#1A1A2E",
                 maxlen: int = 60, vmax: Optional[float] = 100.0, label: str = "", unit: str = "%",
                 text_color: str = "#B6B8D0", font: str = "Segoe UI"):
        super().__init__(master, height=height, bg=bg, highlightthickness=0, bd=0)
        self.color, self.bgc, self.grid_c = color, bg, grid
        self.vmax = vmax
        self.label, self.unit, self.text_color, self.font = label, unit, text_color, font
        self.data: deque[float] = deque([0.0] * maxlen, maxlen=maxlen)
        self.w = 300
        self.h = height
        self.bind("<Configure>", self._on_resize)

    def _on_resize(self, event) -> None:
        self.w, self.h = max(40, event.width), max(20, event.height)
        self.redraw()

    def push(self, value: float) -> None:
        self.data.append(float(value))

    def redraw(self) -> None:
        self.delete("all")
        w, h = self.w, self.h
        for i in range(1, 4):
            y = h * i / 4
            self.create_line(0, y, w, y, fill=self.grid_c)
        vals = list(self.data)
        top = self.vmax if self.vmax else max(max(vals), 1.0) * 1.15
        n = len(vals)
        pts = []
        for i, v in enumerate(vals):
            x = w * i / max(1, n - 1)
            y = h - 3 - (h - 8) * clamp01(v / top)
            pts.append((x, y))
        flat = [c for p in pts for c in p]
        fill = lerp_color(self.bgc, self.color, 0.16)
        self.create_polygon([0, h, *flat, w, h], fill=fill, outline="")
        self.create_line(*flat, fill=self.color, width=2, smooth=True)
        cur = vals[-1] if vals else 0.0
        txt = f"{self.label}  {cur:.0f}{self.unit}" if self.unit == "%" else f"{self.label}  {cur:.1f}{self.unit}"
        self.create_text(8, 7, text=txt, anchor="nw", fill=self.text_color, font=(self.font, 9, "bold"))
