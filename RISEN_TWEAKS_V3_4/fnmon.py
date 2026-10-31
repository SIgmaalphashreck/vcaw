"""RISEN TWEAKS - Fortnite Messung.

FPS, Frametime und 1%-Low lassen sich unter Windows nicht ehrlich ohne ETW-
Daten messen. Dieses Modul wertet deshalb die CSV von Intel PresentMon aus
(MIT-Lizenz, https://github.com/GameTechDev/PresentMon). PresentMon.exe wird
NICHT mitgeliefert: Lege die Datei neben RISEN_TWEAKS.exe, dann startet die App
die Messung. Ohne PresentMon zeigt die App 'N/A' und erfindet nichts.

Hinweis: Die PresentMon-Aufrufe sind fuer Version 1.x und 2.x vorbereitet, aber
nicht auf einem Windows-System mit Fortnite getestet.
"""
from __future__ import annotations

import csv
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import psutil

FORTNITE_EXE = "FortniteClient-Win64-Shipping.exe"
IS_WINDOWS = os.name == "nt"

FRAME_COLUMNS = ("msbetweenpresents", "frametime", "msbetweendisplaychange")


@dataclass
class FrameStats:
    frames: int
    duration_s: float
    avg_fps: float
    low1_fps: float          # Durchschnitt der langsamsten 1 % der Frames
    low01_fps: float         # Durchschnitt der langsamsten 0,1 % der Frames
    avg_ms: float
    p99_ms: float
    max_ms: float
    stutters: int            # Frames > 2x Median-Frametime

    def lines(self) -> list[str]:
        return [
            f"Frames: {self.frames} in {self.duration_s:.1f} s",
            f"Ø FPS: {self.avg_fps:.1f}",
            f"1% Low: {self.low1_fps:.1f} FPS · 0,1% Low: {self.low01_fps:.1f} FPS",
            f"Frametime Ø {self.avg_ms:.2f} ms · 99. Perzentil {self.p99_ms:.2f} ms · max {self.max_ms:.1f} ms",
            f"Ruckler (> 2x Median): {self.stutters}",
        ]


def _low_fps(sorted_desc: list[float], fraction: float) -> float:
    n = max(1, int(round(len(sorted_desc) * fraction)))
    worst = sorted_desc[:n]
    mean_ms = sum(worst) / len(worst)
    return 1000.0 / mean_ms if mean_ms > 0 else 0.0


def compute_frame_stats(frametimes_ms: list[float]) -> Optional[FrameStats]:
    ft = [x for x in frametimes_ms if x and x > 0]
    if len(ft) < 30:
        return None  # zu wenige Frames fuer belastbare Werte
    total_ms = sum(ft)
    ordered = sorted(ft)
    desc = sorted(ft, reverse=True)
    median = ordered[len(ordered) // 2]
    p99 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.99))]
    return FrameStats(
        frames=len(ft),
        duration_s=total_ms / 1000.0,
        avg_fps=1000.0 * len(ft) / total_ms,
        low1_fps=_low_fps(desc, 0.01),
        low01_fps=_low_fps(desc, 0.001),
        avg_ms=total_ms / len(ft),
        p99_ms=p99,
        max_ms=desc[0],
        stutters=sum(1 for x in ft if x > 2 * median),
    )


def parse_presentmon_csv(path: Path) -> list[float]:
    """Liest Frametimes (ms) aus einer PresentMon-CSV (1.x und 2.x)."""
    values: list[float] = []
    with path.open("r", encoding="utf-8", errors="replace", newline="") as f:
        reader = csv.reader(f)
        header = next(reader, None)
        if not header:
            return values
        lowered = [h.strip().lower() for h in header]
        idx = next((lowered.index(c) for c in FRAME_COLUMNS if c in lowered), None)
        if idx is None:
            return values
        for row in reader:
            if idx < len(row):
                try:
                    values.append(float(row[idx]))
                except ValueError:
                    continue
    return values


def find_presentmon() -> Optional[Path]:
    names = ("PresentMon.exe", "PresentMon-2.exe", "PresentMon-1.exe")
    dirs: list[Path] = []
    if getattr(sys, "frozen", False):
        dirs.append(Path(sys.executable).resolve().parent)
    dirs.append(Path(__file__).resolve().parent)
    dirs.append(Path.cwd())
    for d in dirs:
        for n in names:
            p = d / n
            if p.is_file():
                return p
    found = shutil.which("PresentMon.exe") or shutil.which("PresentMon")
    if found:
        return Path(found)
    for p in sorted(dirs[0].glob("PresentMon*.exe")) if dirs else []:
        return p
    return None


def run_capture(exe: Path, out_csv: Path, seconds: int = 30,
                process_name: str = FORTNITE_EXE) -> tuple[bool, str]:
    """Startet PresentMon fuer `seconds` Sekunden. Probiert zuerst die 2.x-,
    danach die 1.x-Syntax."""
    attempts = [
        [str(exe), "--process_name", process_name, "--output_file", str(out_csv),
         "--timed", str(seconds), "--terminate_after_timed", "--no_console_stats"],
        [str(exe), "-process_name", process_name, "-output_file", str(out_csv),
         "-timed", str(seconds), "-terminate_after_timed", "-no_top"],
    ]
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if IS_WINDOWS else 0
    last = ""
    for args in attempts:
        try:
            if out_csv.exists():
                out_csv.unlink()
            p = subprocess.run(args, capture_output=True, text=True, errors="replace",
                               timeout=seconds + 25, creationflags=flags)
            last = ((p.stdout or "") + (p.stderr or "")).strip()[:400]
            if out_csv.exists() and out_csv.stat().st_size > 0:
                return True, ""
        except Exception as exc:  # noqa: BLE001
            last = str(exc)
    return False, last or "PresentMon hat keine Daten geschrieben (laeuft Fortnite? Adminrechte noetig?)"


def process_snapshot(process_name: str = FORTNITE_EXE) -> Optional[dict]:
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            if (proc.info.get("name") or "").lower() == process_name.lower():
                with proc.oneshot():
                    return {
                        "pid": proc.pid,
                        "cpu_pct": proc.cpu_percent(interval=0.3) / (psutil.cpu_count() or 1),
                        "ram_mb": proc.memory_info().rss / 1024 / 1024,
                        "threads": proc.num_threads(),
                    }
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return None


def top_background(limit: int = 8, skip_names: tuple[str, ...] = (FORTNITE_EXE,)) -> list[tuple[str, float, float]]:
    """Prozesse mit der hoechsten CPU-Last (ohne Fortnite). Rein informativ."""
    rows = []
    procs = list(psutil.process_iter(["pid", "name"]))
    for p in procs:
        try:
            p.cpu_percent(None)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    time.sleep(0.6)
    ncpu = psutil.cpu_count() or 1
    for p in procs:
        try:
            name = p.info.get("name") or "?"
            if name.lower() in {s.lower() for s in skip_names} or name in ("System Idle Process", "Idle"):
                continue
            cpu = p.cpu_percent(None) / ncpu
            ram = p.memory_info().rss / 1024 / 1024
            rows.append((name, cpu, ram))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    rows.sort(key=lambda r: (r[1], r[2]), reverse=True)
    return rows[:limit]
