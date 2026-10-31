from __future__ import annotations

import base64
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, ttk, filedialog
import tkinter as tk

try:
    import customtkinter as ctk
except ImportError:
    print("Missing dependency: customtkinter. Run start.bat first.")
    raise

try:
    import psutil
except ImportError:
    print("Missing dependency: psutil. Run start.bat first.")
    raise

import fnmon
import fx
import ui_widgets
import netlab
import tweaklib

APP_NAME = "RISEN TWEAKS"
APP_VERSION = "3.4.0"
IS_WINDOWS = os.name == "nt"
LOCAL = Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
APP_DIR = LOCAL / "RisenTweaks"
BACKUP_FILE = APP_DIR / "backup_v2.json"
TWEAK_STATE_FILE = APP_DIR / "tweak_state.json"
LOG_FILE = APP_DIR / "risen_tweaks_v2.log"

BG = "#07070D"
SIDEBAR = "#0B0B16"
PANEL = "#0D0D18"
CARD = "#12121F"
CARD_HOVER = "#1A1A2E"
BORDER = "#1E1E33"
TEXT = "#F2F3FF"
MUTED = "#7C7F99"
PURPLE = "#8B5CF6"
PURPLE_HOVER = "#A78BFA"
PURPLE_DARK = "#2A1B5E"
CYAN = "#22D3EE"
GREEN = "#34D399"
RED = "#F43F5E"

GAME_MODE_VALUES = [
    (r"Software\Microsoft\GameBar", "AutoGameModeEnabled", 1),
    (r"Software\Microsoft\GameBar", "AllowAutoGameMode", 1),
]
GAME_DVR_VALUES = [
    (r"System\GameConfigStore", "GameDVR_Enabled", 0),
    (r"Software\Microsoft\Windows\CurrentVersion\GameDVR", "AppCaptureEnabled", 0),
]
VISUAL_FX_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\VisualEffects"
MOUSE_KEY = r"Control Panel\Mouse"


def log(message: str) -> None:
    try:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {message}\n")
    except Exception:
        pass


def run_cmd(args: list[str], timeout: int = 12) -> tuple[int, str]:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if IS_WINDOWS else 0
    try:
        p = subprocess.run(
            args, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, shell=False, creationflags=flags
        )
        return p.returncode, ((p.stdout or "") + (p.stderr or "")).strip()
    except Exception as exc:
        return 1, str(exc)


def run_ps(script: str, timeout: int = 18) -> tuple[int, str]:
    return run_cmd(
        ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
        timeout,
    )


def is_admin() -> bool:
    if not IS_WINDOWS:
        return False
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def fmt_bytes(value: float | int) -> str:
    amount = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if amount < 1024 or unit == "TB":
            return f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{amount:.1f} TB"


def safe_json_from_powershell(text: str):
    text = text.strip().lstrip("\ufeff")
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # PowerShell can occasionally prepend a warning; keep only the JSON object/array.
        start_obj, start_arr = text.find("{"), text.find("[")
        starts = [x for x in (start_obj, start_arr) if x >= 0]
        if not starts:
            return None
        start = min(starts)
        end = max(text.rfind("}"), text.rfind("]"))
        if end <= start:
            return None
        try:
            return json.loads(text[start:end + 1])
        except Exception:
            return None


class BackupManager:
    """Backs up only user-scoped registry values and the active power plan."""
    def __init__(self):
        APP_DIR.mkdir(parents=True, exist_ok=True)
        self.data = {"registry": {}, "power_plan": None}
        self.load()

    def load(self):
        try:
            if BACKUP_FILE.exists():
                loaded = json.loads(BACKUP_FILE.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    self.data.update(loaded)
        except Exception as exc:
            log(f"Backup read error: {exc}")

    def save(self):
        APP_DIR.mkdir(parents=True, exist_ok=True)
        temp = BACKUP_FILE.with_suffix(".tmp")
        temp.write_text(json.dumps(self.data, indent=2, ensure_ascii=False), encoding="utf-8")
        temp.replace(BACKUP_FILE)

    @staticmethod
    def _encode_value(value):
        if isinstance(value, bytes):
            return {"__bytes__": base64.b64encode(value).decode("ascii")}
        return value

    @staticmethod
    def _decode_value(value):
        if isinstance(value, dict) and "__bytes__" in value:
            return base64.b64decode(value["__bytes__"])
        return value

    def set_user_dword(self, key_path: str, name: str, value: int):
        if not IS_WINDOWS:
            raise RuntimeError("Diese Änderung ist nur unter Windows verfügbar.")
        import winreg
        key_id = key_path + "|" + name
        if key_id not in self.data["registry"]:
            record = {"key": key_path, "name": name, "existed": False, "type": None, "value": None}
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_READ) as key:
                    old_value, old_type = winreg.QueryValueEx(key, name)
                    record.update({"existed": True, "type": int(old_type), "value": self._encode_value(old_value)})
            except FileNotFoundError:
                pass
            except OSError as exc:
                log(f"Registry read warning {key_id}: {exc}")
            self.data["registry"][key_id] = record
            self.save()
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, name, 0, winreg.REG_DWORD, int(value))
        log(f"Registry: HKCU\\{key_path}\\{name}={value}")

    def set_user_string(self, key_path: str, name: str, value: str):
        if not IS_WINDOWS:
            raise RuntimeError("Diese Änderung ist nur unter Windows verfügbar.")
        import winreg
        key_id = key_path + "|" + name
        if key_id not in self.data["registry"]:
            record = {"key": key_path, "name": name, "existed": False, "type": None, "value": None}
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_READ) as key:
                    old_value, old_type = winreg.QueryValueEx(key, name)
                    record.update({"existed": True, "type": int(old_type), "value": self._encode_value(old_value)})
            except FileNotFoundError:
                pass
            except OSError as exc:
                log(f"Registry read warning {key_id}: {exc}")
            self.data["registry"][key_id] = record
            self.save()
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, str(value))
        log(f"Registry: HKCU\\{key_path}\\{name}={value}")

    def restore_registry(self) -> tuple[int, int]:
        if not IS_WINDOWS:
            return 0, len(self.data.get("registry", {}))
        import winreg
        restored = failed = 0
        remaining = {}
        for key_id, record in list(self.data.get("registry", {}).items()):
            try:
                with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, record["key"], 0, winreg.KEY_SET_VALUE) as key:
                    if record.get("existed"):
                        old = self._decode_value(record.get("value"))
                        winreg.SetValueEx(key, record["name"], 0, int(record["type"]), old)
                    else:
                        try:
                            winreg.DeleteValue(key, record["name"])
                        except FileNotFoundError:
                            pass
                restored += 1
            except Exception as exc:
                failed += 1
                remaining[key_id] = record
                log(f"Registry restore failed {key_id}: {exc}")
        self.data["registry"] = remaining
        self.save()
        return restored, failed

    def remember_power_plan(self, guid: str):
        if not self.data.get("power_plan"):
            self.data["power_plan"] = guid
            self.save()

    def restore_power_plan(self) -> bool:
        guid = self.data.get("power_plan")
        if not guid:
            return False
        code, output = run_cmd(["powercfg", "/setactive", guid], 10)
        if code == 0:
            self.data["power_plan"] = None
            self.save()
            log(f"Restored power plan {guid}")
            return True
        log(f"Power plan restore failed: {output}")
        return False


BACKUPS = BackupManager()
CATALOG = tweaklib.build_catalog()
CATALOG_BY_ID = {t.id: t for t in CATALOG}
YELLOW = "#FBBF24"


class Tooltip:
    """Einfacher Tooltip fuer Sidebar-Icons."""
    def __init__(self, widget, text: str):
        self.widget, self.text, self.tip = widget, text, None
        widget.bind("<Enter>", self.show, add="+")
        widget.bind("<Leave>", self.hide, add="+")

    def show(self, _event=None):
        if self.tip or not self.text:
            return
        try:
            x = self.widget.winfo_rootx() + self.widget.winfo_width() + 8
            y = self.widget.winfo_rooty() + 10
            self.tip = tk.Toplevel(self.widget)
            self.tip.wm_overrideredirect(True)
            self.tip.wm_geometry(f"+{x}+{y}")
            tk.Label(self.tip, text=self.text, bg="#1A1A2B", fg="#f0f0f0", padx=9, pady=4,
                     font=("Segoe UI", 9), relief="solid", borderwidth=1).pack()
        except Exception:
            self.tip = None

    def hide(self, _event=None):
        if self.tip is not None:
            try:
                self.tip.destroy()
            except Exception:
                pass
            self.tip = None


class RisenTweaks(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title(f"{APP_NAME} V{APP_VERSION}  •  Tweaks")
        self.geometry("1380x900")
        self.minsize(1120, 720)
        self.configure(fg_color=BG)
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.alive = True
        self.active_page = "Home"
        self.sort_by = "cpu"
        self.filter_text = ""
        self.process_rows: dict[str, tuple] = {}
        self.hw_data = {}
        self.last_net = psutil.net_io_counters()
        self.last_net_time = time.time()
        self.net_rates = (0.0, 0.0)
        self.last_disk = psutil.disk_io_counters()
        self.last_disk_time = time.time()
        self.disk_rates = (0.0, 0.0)
        self.gpu_data = None
        self.engine = tweaklib.TweakEngine(
            tweaklib.WinRegBackend(), TWEAK_STATE_FILE, hooks=tweaklib.DEFAULT_HOOKS, logger=log,
            fortnite_running=lambda: bool(self.find_fortnite()))
        self.tl_category = tweaklib.CATEGORIES[0]
        self.cc_selected: dict[str, bool] = {}
        self.cc_active: dict[str, bool] = {}
        self.cc_vars: dict = {}
        self.cc_busy = False
        self.tl_status_labels: dict[str, ctk.CTkLabel] = {}
        self.ping_cancel = threading.Event()
        self.net_busy = False
        self.fn_busy = False
        self._live_busy = False
        self._proc_busy = False
        self._fn_cache = ([], 0.0)
        self.fn_auto_high = True
        self._set_theme()
        self._build_shell()
        self._build_pages()
        self.show_page("Home")
        self.refresh_hardware_async()
        self.after(1200, self.periodic)

    def _set_theme(self):
        ctk.set_appearance_mode("Dark")
        ctk.set_default_color_theme("blue")
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Treeview", background="#0D0D18", foreground="#eeeeee", fieldbackground="#0D0D18", rowheight=30, borderwidth=0, font=("Segoe UI", 9))
        style.configure("Treeview.Heading", background="#1A1A2B", foreground="#dddddd", relief="flat", font=("Segoe UI", 9, "bold"))
        style.map("Treeview", background=[("selected", "#3B2A7A")], foreground=[("selected", "#ffffff")])
        style.configure("Vertical.TScrollbar", background="#242438", troughcolor="#0D0D18", bordercolor="#0D0D18", arrowcolor="#eeeeee")

    def _build_shell(self):
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(1, weight=1)
        # Slim left navigation, matching the reference screenshot.
        self.sidebar = ctk.CTkFrame(self, width=64, corner_radius=0, fg_color=SIDEBAR)
        self.sidebar.grid(row=0, column=0, rowspan=3, sticky="nsew")
        self.sidebar.grid_propagate(False)
        self.nav_buttons = {}
        nav_items = [
            ("Home", "⌂"), ("Tweaks", "⚡"), ("Fortnite", "F"), ("Processes", "☷"),
            ("Network Lab", "◎"), ("Hardware", "◈"), ("Cleanup", "▤"), ("Tools", "⚙"),
        ]
        for name, glyph in nav_items:
            btn = ctk.CTkButton(
                self.sidebar, text=glyph, width=46, height=45, corner_radius=12,
                fg_color=SIDEBAR, hover_color="#1B1B30", text_color="#f4f4f4", border_width=0, border_color=CYAN,
                font=ctk.CTkFont("Segoe UI Symbol", 22, "bold"), command=lambda p=name: self.show_page(p)
            )
            btn.bind("<Enter>", lambda _e, n=name: self._nav_hover(n, True), add="+")
            btn.bind("<Leave>", lambda _e, n=name: self._nav_hover(n, False), add="+")
            btn.pack(padx=8, pady=3)
            Tooltip(btn, name)
            self.nav_buttons[name] = btn

        self.header = ctk.CTkFrame(self, height=38, corner_radius=0, fg_color="#090912")
        self.header.grid(row=0, column=1, sticky="ew")
        self.header.grid_columnconfigure(0, weight=1)
        self.header.grid_columnconfigure(2, weight=1)
        brand = ctk.CTkFrame(self.header, fg_color="transparent")
        brand.grid(row=0, column=0, sticky="w", padx=16)
        ctk.CTkLabel(brand, text="RISEN", text_color=PURPLE_HOVER, font=ctk.CTkFont("Segoe UI", 15, "bold")).pack(side="left")
        ctk.CTkLabel(brand, text=" TWEAKS", text_color=CYAN, font=ctk.CTkFont("Segoe UI", 15, "bold")).pack(side="left")
        ctk.CTkLabel(brand, text=f"  v{APP_VERSION}", text_color=MUTED, font=ctk.CTkFont("Segoe UI", 9)).pack(side="left", pady=(3, 0))
        self.page_title = ctk.CTkLabel(self.header, text="Control Center", text_color="#eeeeee", font=ctk.CTkFont("Segoe UI", 12, "bold"))
        self.page_title.grid(row=0, column=1, padx=10, pady=8)
        self.admin_text = "ADMINISTRATOR" if is_admin() else "NORMAL MODE"
        self.admin_label = ctk.CTkLabel(self.header, text=f"● {self.admin_text}", text_color=GREEN if is_admin() else MUTED, font=ctk.CTkFont("Segoe UI", 9, "bold"))
        self.admin_label.grid(row=0, column=2, sticky="e", padx=16)
        self.glow = tk.Canvas(self.header, height=3, bg=BG, highlightthickness=0, bd=0)
        self.glow.grid(row=1, column=0, columnspan=3, sticky="ew")
        self.glow.bind("<Configure>", lambda e: fx.draw_gradient(self.glow, [PURPLE, CYAN], e.width, 3))
        fx.shimmer(self.glow, [PURPLE, CYAN], lambda: self.glow.winfo_width(), 3, alive=lambda: self.alive)
        if is_admin():
            fx.pulse(self.admin_label, "text_color", GREEN, "#1B6B4B", alive=lambda: self.alive)

        self.page_host = ctk.CTkFrame(self, corner_radius=0, fg_color=BG)
        self.page_host.grid(row=1, column=1, sticky="nsew", padx=(8, 0), pady=(4, 0))
        self.page_host.grid_rowconfigure(0, weight=1)
        self.page_host.grid_columnconfigure(0, weight=1)

        self.status_bar = ctk.CTkLabel(self, text="Bereit · Lokale Systemdaten", anchor="w", height=25, fg_color="#0D0D18", text_color="#a0a0a0", font=ctk.CTkFont("Segoe UI", 9))
        self.status_bar.grid(row=2, column=1, sticky="ew", padx=(8, 0), pady=(4, 0))

    def _nav_hover(self, name: str, hovering: bool):
        btn = self.nav_buttons.get(name)
        if btn is None:
            return
        active = name == self.active_page
        try:
            if active:
                btn.configure(fg_color=PURPLE_DARK, border_width=1, border_color=PURPLE_HOVER if not hovering else CYAN)
            else:
                btn.configure(fg_color="#171728" if hovering else SIDEBAR, border_width=1 if hovering else 0, border_color="#3A3A66")
        except Exception:
            pass

    def _new_page(self):
        return ctk.CTkScrollableFrame(self.page_host, fg_color=BG, corner_radius=0, scrollbar_button_color="#34344F", scrollbar_button_hover_color="#4A4A6A")

    PAGE_BUILDERS = {
        "Home": "_build_home_page", "Tweaks": "_build_tweaks_page", "Windows Tweaks": "_build_windows_page",
        "Control Center": "_build_tweaklib_page", "Performance": "_build_performance_page", "Cleanup": "_build_cleanup_page",
        "GPU Optimization": "_build_gpu_page", "Fortnite": "_build_fortnite_page", "Network Lab": "_build_netlab_page",
        "Processes": "_build_process_page", "Hardware": "_build_hardware_page", "Tools": "_build_tools_page",
    }

    def _build_pages(self):
        """Seiten werden erst beim ersten Oeffnen gebaut (schneller Start, kein Lag)."""
        self.pages = {}
        self._built = set()

    def ensure_built(self, name: str):
        if name in self._built:
            return
        self.pages[name] = self._new_page()
        getattr(self, self.PAGE_BUILDERS[name])()
        self._built.add(name)

    def show_page(self, name: str):
        self.ensure_built(name)
        prev = self.active_page
        if prev in self.pages and prev != name:
            self.pages[prev].grid_forget()
        self.active_page = name
        self.pages[name].grid(row=0, column=0, sticky="nsew")
        self.page_title.configure(text=name)
        for nav in self.nav_buttons:
            self._nav_hover(nav, False)
        hook = {"Home": self.on_show_home, "Tweaks": self.on_show_tweaks, "Fortnite": self.on_show_fortnite,
                "Processes": self.refresh_processes, "Hardware": self._hw_on_show, "Network Lab": self.nl_refresh_config,
                "Control Center": self.tl_render}.get(name)
        if hook:
            hook()

    def _hw_on_show(self):
        if not self.hw_data:
            self.refresh_hardware_async()

    def group(self, parent, title: str, row: int, col: int, columnspan: int = 1):
        panel = ctk.CTkFrame(parent, fg_color=PANEL, corner_radius=16, border_width=1, border_color=BORDER)
        panel.grid(row=row, column=col, columnspan=columnspan, sticky="nsew", padx=7, pady=7)
        panel.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(panel, text=title, text_color="#929292", font=ctk.CTkFont("Segoe UI", 10), anchor="center").pack(fill="x", padx=12, pady=(10, 9))
        body = ctk.CTkFrame(panel, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        return body

    def action_card(self, parent, title: str, description: str, button_text: str, callback, *, accent: bool = False, note: str = ""):
        card = ctk.CTkFrame(parent, fg_color=CARD, corner_radius=14, border_width=1, border_color="#1E1E33")
        card.pack(fill="x", pady=5)
        ctk.CTkLabel(card, text=title, text_color=TEXT, font=ctk.CTkFont("Segoe UI", 12, "bold"), anchor="w").pack(fill="x", padx=13, pady=(11, 3))
        ctk.CTkLabel(card, text=description, text_color="#a5a5a5", font=ctk.CTkFont("Segoe UI", 9), anchor="w", justify="left", wraplength=510).pack(fill="x", padx=13, pady=(0, 7))
        btn = ctk.CTkButton(card, text=button_text, command=callback, height=39, corner_radius=10,
                            fg_color=PURPLE if accent else "#1A1A2B", hover_color=PURPLE_HOVER if accent else "#2A2A44",
                            text_color="#ffffff", font=ctk.CTkFont("Segoe UI", 10, "bold"))
        btn.pack(fill="x", padx=9, pady=(0, 8))
        if note:
            ctk.CTkLabel(card, text=note, text_color=MUTED, font=ctk.CTkFont("Segoe UI", 8), anchor="w", justify="left", wraplength=510).pack(fill="x", padx=13, pady=(0, 9))
        fx.hover_fade(card, {"fg_color": (CARD, CARD_HOVER), "border_color": (BORDER, PURPLE)})
        return card

    def add_page_heading(self, parent, title, subtitle):
        ctk.CTkLabel(parent, text=title, text_color=TEXT, font=ctk.CTkFont("Segoe UI", 22, "bold"), anchor="w").pack(fill="x", padx=8, pady=(9, 2))
        ctk.CTkLabel(parent, text=subtitle, text_color=MUTED, font=ctk.CTkFont("Segoe UI", 10), anchor="w").pack(fill="x", padx=8, pady=(0, 9))

    def _build_gpu_page(self):
        p = self.pages["GPU Optimization"]
        self.add_page_heading(p, "GPU Optimization", "GPU-Status, Treiber und Hersteller-Tools · Einstellungen werden nicht blind in der Registry verändert.")
        grid = ctk.CTkFrame(p, fg_color="transparent")
        grid.pack(fill="x", expand=True)
        grid.grid_columnconfigure((0, 1), weight=1, uniform="gpu")
        nvidia = self.group(grid, "NVIDIA GPU TWEAKS", 0, 0)
        self.action_card(nvidia, "GPU Live-Telemetrie", "Liest GPU-Auslastung, VRAM, Temperatur und Leistungsaufnahme über NVIDIA-SMI, sofern der Treiber es bereitstellt.", "GPU-Daten aktualisieren", self.show_gpu_telemetry, accent=True)
        self.action_card(nvidia, "NVIDIA Control Panel", "Öffnet das offizielle NVIDIA Control Panel. Stelle dort pro Spiel bevorzugte maximale Leistung und V-Sync nur bewusst ein.", "Control Panel öffnen", self.open_nvidia_panel)
        self.action_card(nvidia, "NVIDIA Treiber herunterladen", "Öffnet die offizielle NVIDIA-Treiberseite. Für eine saubere Neuinstallation im Installer 'Benutzerdefiniert' und bei Bedarf 'Neuinstallation vornehmen' auswählen.", "Offizielle Treiberseite", lambda: webbrowser.open("https://www.nvidia.com/Download/index.aspx"))
        self.action_card(nvidia, "NVIDIA System-Check", "Prüft, ob NVIDIA-SMI verfügbar ist und welcher Treiber die GPU meldet. Es wird nichts am Treiber geändert.", "System-Check starten", self.nvidia_check)
        amd = self.group(grid, "RADEON GPU TWEAKS", 0, 1)
        self.action_card(amd, "AMD Radeon Software", "Öffnet AMD Software: Adrenalin Edition, falls installiert. Bei deinem RTX-System ist dieser Bereich normalerweise nicht anwendbar.", "AMD Software öffnen", self.open_amd_software)
        self.action_card(amd, "AMD Treiber & Support", "Offizielle AMD-Seite für Radeon-Treiber. Nur relevant, wenn ein AMD-Grafikgerät verwendet wird.", "Offizielle AMD-Seite", lambda: webbrowser.open("https://www.amd.com/en/support"))
        self.action_card(amd, "Radeon Kompatibilität", "Erkennt die vom Windows-System gemeldeten Grafikkarten und zeigt, ob ein AMD-Gerät vorhanden ist.", "Grafikkarten erkennen", self.refresh_hardware_async)
        info = self.group(grid, "GPU STATUS & SCHEDULING", 1, 0, 2)
        self.gpu_status_label = ctk.CTkLabel(info, text="GPU-Informationen werden geladen …", text_color="#bdbdbd", justify="left", anchor="w", font=ctk.CTkFont("Segoe UI", 10))
        self.gpu_status_label.pack(fill="x", padx=7, pady=7)
        self.action_card(info, "Windows Grafik-Einstellungen", "Dort kannst du Fortnite manuell auf 'Hohe Leistung' (RTX 5060 Ti) setzen. Das ist eine Windows-Zuweisung pro App.", "Grafik-Einstellungen öffnen", lambda: self.open_uri("ms-settings:display-advancedgraphics"))

    def _build_windows_page(self):
        p = self.pages["Windows Tweaks"]
        self.add_page_heading(p, "Windows Tweaks", "Mehrere übersichtliche Tweaks – jede unterstützte Registry-Änderung wird vorab gesichert.")
        grid = ctk.CTkFrame(p, fg_color="transparent")
        grid.pack(fill="x")
        grid.grid_columnconfigure((0, 1), weight=1, uniform="windows")
        general = self.group(grid, "WINDOWS GAMING", 0, 0)
        self.action_card(general, "Windows Game Mode", "Aktiviert den Windows-Spielemodus für deinen Benutzer. Der Effekt hängt von Windows, Treibern und Hintergrundlast ab.", "Game Mode aktivieren", self.apply_game_mode, accent=True)
        self.action_card(general, "Game DVR / Hintergrundaufnahmen", "Deaktiviert Windows-Aufnahmefunktionen für deinen Benutzer. Xbox/Windows-Hintergrundaufnahmen sind danach nicht mehr verfügbar.", "Game DVR deaktivieren", self.apply_game_dvr)
        self.action_card(general, "Animationen & visuelle Effekte", "Setzt den Windows-Effektmodus auf 'Beste Leistung'. Fensteranimationen und einige optische Effekte können verschwinden.", "Auf beste Leistung setzen", self.apply_visual_performance)
        self.action_card(general, "Windows Grafik-Einstellungen", "Weist Fortnite in Windows manuell der dedizierten Hochleistungs-GPU zu.", "Grafik-Einstellungen öffnen", lambda: self.open_uri("ms-settings:display-advancedgraphics"))
        power = self.group(grid, "POWER & PERFORMANCE", 0, 1)
        self.action_card(power, "Höchstleistung aktivieren", "Aktiviert den eingebauten Windows-Energiesparplan 'Höchstleistung'. Mehr Stromverbrauch und Wärme möglich; FPS-Gewinn nicht garantiert.", "Höchstleistung aktivieren", self.apply_high_performance, accent=True)
        self.action_card(power, "Höchstleistung zurücksetzen", "Stellt den Energieplan wieder her, der vor 'Höchstleistung' aktiv war (verifiziert).", "Energieplan zurücksetzen", lambda: self.engine_undo_dialog("power_high"))
        self.action_card(power, "Aktiven Energieplan anzeigen", "Zeigt den gerade aktiven Windows-Energiesparplan.", "Energieplan prüfen", self.show_power_plan)
        advanced = self.group(grid, "WINDOWS & DIAGNOSE", 1, 0)
        self.action_card(advanced, "Task-Manager", "Zeigt Prozesse, Autostart, Leistung und App-Verbrauch in Windows.", "Task-Manager öffnen", lambda: self.launch_tool("taskmgr.exe"))
        self.action_card(advanced, "Geräte-Manager", "Prüfe Gerätetreiber, Warnsymbole und erkannte Hardware.", "Geräte-Manager öffnen", lambda: self.launch_tool("devmgmt.msc"))
        self.action_card(advanced, "Windows Sicherheit", "Öffnet die Windows-Sicherheitsübersicht. RISEN deaktiviert Defender oder Firewall nicht.", "Windows Sicherheit öffnen", lambda: self.open_uri("windowsdefender:"))
        maintenance = self.group(grid, "WIEDERHERSTELLUNG", 1, 1)
        self.action_card(maintenance, "Gesicherte Tweaks zurücksetzen", "Stellt die ursprünglichen gesicherten Registry-Werte und den zuvor aktiven Energieplan wieder her.", "Alle Änderungen zurücksetzen", self.restore_all, accent=True)
        self.action_card(maintenance, "Backup-Ordner öffnen", "Öffnet den lokalen Ordner, in dem RISEN deine Sicherung ablegt.", "Backup öffnen", lambda: self.open_path(APP_DIR))

    def _build_performance_page(self):
        p = self.pages["Performance"]
        self.add_page_heading(p, "Performance Monitor", "Live-Ressourcenverbrauch und I/O-Durchsatz. Hardware-Sensorwerte hängen vom Treiber ab.")
        grid = ctk.CTkFrame(p, fg_color="transparent")
        grid.pack(fill="x")
        grid.grid_columnconfigure((0, 1), weight=1, uniform="perf")
        left = self.group(grid, "LIVE SYSTEM LOAD", 0, 0)
        self.perf_labels = {}
        for key, title in [("cpu", "CPU"), ("ram", "RAM"), ("gpu", "GPU"), ("disk", "DISK I/O"), ("network", "NETWORK I/O")]:
            row = ctk.CTkFrame(left, fg_color="transparent")
            row.pack(fill="x", padx=7, pady=6)
            ctk.CTkLabel(row, text=title, text_color="#dddddd", width=105, anchor="w", font=ctk.CTkFont("Segoe UI", 10, "bold")).pack(side="left")
            bar = ctk.CTkProgressBar(row, height=10, progress_color=PURPLE)
            bar.set(0)
            bar.pack(side="left", fill="x", expand=True, padx=8)
            label = ctk.CTkLabel(row, text="—", text_color="#aaaaaa", width=135, anchor="e", font=ctk.CTkFont("Segoe UI", 9))
            label.pack(side="right")
            self.perf_labels[key] = (bar, label)
        right = self.group(grid, "PERFORMANCE NOTES", 0, 1)
        ctk.CTkLabel(right, text="• CPU-Auslastung kommt von psutil / Windows.\n\n• GPU-Auslastung, VRAM und Temperatur werden nur angezeigt, wenn NVIDIA-SMI oder ein kompatibler Sensor sie meldet.\n\n• Hohe Datenträger- oder Netzwerkraten zeigen Aktivität, nicht automatisch ein Problem.\n\n• Keine App kann allein durch einen Registry-Schalter einen garantierten FPS-Boost erzeugen.", text_color="#bdbdbd", justify="left", anchor="w", wraplength=470, font=ctk.CTkFont("Segoe UI", 10)).pack(fill="x", padx=10, pady=10)

    def _build_cleanup_page(self):
        p = self.pages["Cleanup"]
        self.add_page_heading(p, "Cleanup & Maintenance", "Nur temporäre Dateien und Diagnosefunktionen. Keine Registry-Cleaner oder automatisches Löschen von Systemdateien.")
        grid = ctk.CTkFrame(p, fg_color="transparent")
        grid.pack(fill="x")
        grid.grid_columnconfigure((0, 1), weight=1, uniform="clean")
        temp = self.group(grid, "TEMP FILES", 0, 0)
        self.action_card(temp, "Benutzer-Temp leeren", "Löscht Inhalte aus deinem Benutzer-Temp-Ordner. Dateien, die Windows oder Apps gerade verwenden, werden übersprungen.", "Temp-Dateien bereinigen", self.clean_temp, accent=True)
        self.action_card(temp, "Temp-Ordner öffnen", "Öffnet den Temp-Ordner, damit du Dateien selbst prüfen kannst.", "Temp-Ordner öffnen", lambda: self.open_path(Path(os.environ.get("TEMP", str(Path.home() / "AppData/Local/Temp")))))
        dns = self.group(grid, "NETWORK MAINTENANCE", 0, 1)
        self.action_card(dns, "DNS-Cache leeren", "Nur lokaler DNS-Cache. Kein normaler Fortnite-Ping-Booster.", "DNS-Cache leeren", self.flush_dns)
        self.action_card(dns, "Netzwerk-Einstellungen", "Öffnet die Windows-Netzwerk- und Interneteinstellungen.", "Einstellungen öffnen", lambda: self.open_uri("ms-settings:network-status"))
        self.action_card(dns, "Windows Update", "Öffnet Windows Update. RISEN deaktiviert Updates nicht.", "Windows Update öffnen", lambda: self.open_uri("ms-settings:windowsupdate"))

    def _build_hardware_page(self):
        p = self.pages["Hardware"]
        self.add_page_heading(p, "Hardware & Components", "Erkannte Komponenten, Live-Auslastung und Gerätestatus. Nicht jedes Gerät stellt einen eigenen Nutzungs-Sensor bereit.")
        top = ctk.CTkFrame(p, fg_color="transparent")
        top.pack(fill="x", padx=7, pady=(0, 8))
        ctk.CTkButton(top, text="Hardware neu erkennen", command=self.refresh_hardware_async, fg_color=PURPLE, hover_color=PURPLE_HOVER, width=175).pack(side="left")
        ctk.CTkButton(top, text="Geräte-Manager öffnen", command=lambda: self.launch_tool("devmgmt.msc"), fg_color="#1A1A2B", hover_color="#2A2A44", width=175).pack(side="left", padx=8)
        ctk.CTkButton(top, text="Alle Geräte auflisten", command=self.show_all_devices, fg_color="#1A1A2B", hover_color="#2A2A44", width=175).pack(side="left", padx=0)
        grid = ctk.CTkFrame(p, fg_color="transparent")
        grid.pack(fill="x")
        grid.grid_columnconfigure((0, 1), weight=1, uniform="hardware")
        self.hw_labels = {}
        for idx, (key, title) in enumerate([
            ("cpu", "PROCESSOR"), ("gpu", "GRAPHICS"), ("ram", "MEMORY"), ("board", "MOTHERBOARD & BIOS"),
            ("drives", "STORAGE DEVICES"), ("network", "NETWORK ADAPTERS"), ("os", "WINDOWS"), ("displays", "DISPLAY")
        ]):
            body = self.group(grid, title, idx // 2, idx % 2)
            label = ctk.CTkLabel(body, text="Daten werden erkannt …", text_color="#cccccc", justify="left", anchor="w", font=ctk.CTkFont("Segoe UI", 9))
            label.pack(fill="x", padx=7, pady=7)
            self.hw_labels[key] = label

    def _build_tools_page(self):
        p = self.pages["Tools"]
        self.add_page_heading(p, "Tools, Backup & Settings", "Wiederherstellung, Diagnose und Windows-Werkzeuge.")
        grid = ctk.CTkFrame(p, fg_color="transparent")
        grid.pack(fill="x")
        grid.grid_columnconfigure((0, 1), weight=1, uniform="tools")
        backup = self.group(grid, "BACKUP & RESTORE", 0, 0)
        self.backup_label = ctk.CTkLabel(backup, text=self.backup_summary(), text_color="#bdbdbd", justify="left", anchor="w", font=ctk.CTkFont("Segoe UI", 9))
        self.backup_label.pack(fill="x", padx=8, pady=7)
        self.action_card(backup, "Gesicherte Änderungen wiederherstellen", "Stellt gespeicherte Registry-Werte im Benutzerbereich und den vorherigen Energieplan wieder her.", "Jetzt wiederherstellen", self.restore_all, accent=True)
        self.action_card(backup, "Backup-Verzeichnis", "Öffnet den lokalen AppData-Ordner.", "Ordner öffnen", lambda: self.open_path(APP_DIR))
        tools = self.group(grid, "WINDOWS TOOLS", 0, 1)
        self.action_card(tools, "Systeminformationen", "Öffnet msinfo32 mit einer ausführlichen Windows- und Hardwareübersicht.", "Systeminformationen öffnen", lambda: self.launch_tool("msinfo32.exe"))
        self.action_card(tools, "Ressourcenmonitor", "Öffnet den Windows-Ressourcenmonitor für CPU, Speicher, Datenträger und Netzwerk.", "Ressourcenmonitor öffnen", lambda: self.launch_tool("resmon.exe"))
        self.action_card(tools, "DirectX-Diagnose", "Öffnet dxdiag zur Anzeige von DirectX- und Grafiktreiberinformationen.", "dxdiag öffnen", lambda: self.launch_tool("dxdiag.exe"))
        self.action_card(tools, "Autostart-Programme prüfen", "Liest die Windows-Run-Schlüssel und Autostart-Ordner aus. Die Übersicht ist zunächst nur lesend – es werden keine Programme automatisch deaktiviert.", "Autostart anzeigen", self.show_startup_apps)
        diag = self.group(grid, "EXPORT & LOGS", 1, 0)
        self.action_card(diag, "Systembericht exportieren", "Speichert einen Textbericht mit Hardware-, Windows-, RAM- und Netzwerkdaten.", "Bericht exportieren", self.export_report)
        self.action_card(diag, "RISEN Log öffnen", "Zeigt das lokale Diagnoseprotokoll.", "Log öffnen", lambda: self.open_path(LOG_FILE))
        about = self.group(grid, "ABOUT RISEN TWEAKS", 1, 1)
        ctk.CTkLabel(about, text=f"RISEN TWEAKS v{APP_VERSION}\nWindows 10/11 · Python / CustomTkinter\n\nBackup: {BACKUP_FILE}\nLog: {LOG_FILE}\n\nHinweis: Änderungen sind lokal. Es werden keine Prozesse automatisch beendet und keine Sicherheitsfunktionen deaktiviert.", text_color="#bdbdbd", anchor="w", justify="left", font=ctk.CTkFont("Segoe UI", 9)).pack(fill="x", padx=8, pady=7)

    # --------------------------- live data ---------------------------
    def refresh_gpu_async(self):
        def worker():
            result = self.read_gpu_data()
            if self.alive:
                self.after(0, lambda: self._accept_gpu_data(result))
        threading.Thread(target=worker, daemon=True).start()

    def read_gpu_data(self):
        nvsmi = shutil.which("nvidia-smi")
        if not nvsmi:
            return {"available": False, "name": "NVIDIA-SMI not found"}
        code, output = run_cmd([nvsmi, "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw,clocks.gr,clocks.mem", "--format=csv,noheader,nounits"], 5)
        if code != 0 or not output:
            return {"available": False, "name": "NVIDIA telemetry unavailable", "error": output[:250]}
        parts = [x.strip() for x in output.splitlines()[0].split(",")]
        if len(parts) < 5:
            return {"available": False, "name": output[:180]}
        def number(i):
            try:
                return float(parts[i])
            except Exception:
                return None
        return {
            "available": True, "name": parts[0], "usage": number(1),
            "mem_used": number(2), "mem_total": number(3), "temp": number(4),
            "power": number(5) if len(parts) > 5 else None,
            "clock": number(6) if len(parts) > 6 else None,
            "mem_clock": number(7) if len(parts) > 7 else None,
        }

    def _accept_gpu_data(self, data):
        self.gpu_data = data
        if hasattr(self, "gpu_status_label"):
            self.gpu_status_label.configure(text=safe_gpu_detail(data, verbose=True))
        if data.get("available"):
            self.status_bar.configure(text=f"GPU: {data['name']} · {data.get('usage') if data.get('usage') is not None else 'N/A'}% · Live monitoring")

    def refresh_hardware_async(self):
        self.set_status("Hardware-Erkennung läuft …")
        def worker():
            script = r'''
$ErrorActionPreference = 'SilentlyContinue'
$cpu = Get-CimInstance Win32_Processor | Select-Object -First 1 Name,NumberOfCores,NumberOfLogicalProcessors,MaxClockSpeed
$board = Get-CimInstance Win32_BaseBoard | Select-Object -First 1 Manufacturer,Product,Version
$bios = Get-CimInstance Win32_BIOS | Select-Object -First 1 SMBIOSBIOSVersion,ReleaseDate
$video = @(Get-CimInstance Win32_VideoController | Select-Object Name,DriverVersion,VideoModeDescription,CurrentRefreshRate)
$drives = @(Get-CimInstance Win32_DiskDrive | Select-Object Model,MediaType,Size,Status,InterfaceType)
$mem = @(Get-CimInstance Win32_PhysicalMemory | Select-Object Capacity,Speed,ConfiguredClockSpeed,Manufacturer)
$net = @(Get-NetAdapter -ErrorAction SilentlyContinue | Select-Object Name,InterfaceDescription,Status,LinkSpeed)
$os = Get-CimInstance Win32_OperatingSystem | Select-Object Caption,Version,BuildNumber,OSArchitecture
$display = @(Get-CimInstance Win32_DesktopMonitor | Select-Object Name,ScreenWidth,ScreenHeight,Status)
$o = [pscustomobject]@{Mem=$mem;CPU=$cpu;Board=$board;BIOS=$bios;Video=$video;Drives=$drives;Network=$net;OS=$os;Display=$display}
$o | ConvertTo-Json -Depth 5 -Compress
'''
            code, output = run_ps(script, 24)
            data = safe_json_from_powershell(output) if code == 0 else None
            if self.alive:
                self.after(0, lambda: self._accept_hardware(data, output))
        threading.Thread(target=worker, daemon=True).start()
        self.refresh_gpu_async()

    def _accept_hardware(self, data, raw=""):
        if not data:
            self.set_status("Hardware-Erkennung teilweise nicht verfügbar · Prüfe PowerShell/CIM")
            log(f"Hardware CIM parse error: {raw[:500]}")
            return
        self.hw_data = data
        def obj(key):
            val = data.get(key)
            if isinstance(val, list):
                return val
            return [val] if isinstance(val, dict) else []
        cpu = obj("CPU")
        board = obj("Board")
        bios = obj("BIOS")
        videos = obj("Video")
        drives = obj("Drives")
        nets = obj("Network")
        osinfo = obj("OS")
        displays = obj("Display")
        freq = psutil.cpu_freq()
        cpu_extra = f"\nTakt: {freq.current:.0f} MHz (Basis laut Windows)" if freq else "\nTakt: N/A"
        cpu_extra += "\nTemperatur: N/A (Windows stellt ohne Sensor-Treiber keinen CPU-Temperatursensor bereit)"
        self._set_hw("cpu", ("\n".join([f"{x.get('Name', 'CPU')} · {x.get('NumberOfCores', '?')}C/{x.get('NumberOfLogicalProcessors', '?')}T" for x in cpu]) or "Nicht erkannt") + cpu_extra)
        gpu_lines = []
        for x in videos:
            gpu_lines.append(f"{x.get('Name', 'GPU')}\nDriver: {x.get('DriverVersion', 'N/A')}\nMode: {x.get('VideoModeDescription', 'N/A')} · {x.get('CurrentRefreshRate', 'N/A')} Hz")
        self._set_hw("gpu", "\n\n".join(gpu_lines) or "Nicht erkannt")
        mem_lines = [f"{fmt_bytes(m.get('Capacity') or 0)} · Nenntakt {m.get('Speed', 'N/A')} MT/s · aktuell {m.get('ConfiguredClockSpeed', 'N/A')} MT/s" for m in obj("Mem")]
        self._set_hw("ram", f"{fmt_bytes(psutil.virtual_memory().total)} installed\nUsage: {psutil.virtual_memory().percent:.1f}% · {fmt_bytes(psutil.virtual_memory().used)} used" + ("\n" + "\n".join(mem_lines) if mem_lines else ""))
        b = board[0] if board else {}
        bi = bios[0] if bios else {}
        self._set_hw("board", f"{b.get('Manufacturer', 'N/A')} {b.get('Product', '')}\nBIOS: {bi.get('SMBIOSBIOSVersion', 'N/A')}\nVersion / Date: {b.get('Version', 'N/A')} / {bi.get('ReleaseDate', 'N/A')}")
        disk_lines = []
        for x in drives:
            disk_lines.append(f"{x.get('Model', 'Disk')} · {fmt_bytes(x.get('Size') or 0)}\n{x.get('MediaType', '')} · {x.get('InterfaceType', '')} · {x.get('Status', '')}")
        # Also show mounted volumes and their usage.
        for part in psutil.disk_partitions(all=False):
            try:
                usage = psutil.disk_usage(part.mountpoint)
                disk_lines.append(f"{part.device} · {fmt_bytes(usage.total)}\nUsed {usage.percent:.1f}% · Free {fmt_bytes(usage.free)}")
            except Exception:
                continue
        self._set_hw("drives", "\n\n".join(disk_lines) or "Keine Laufwerke gemeldet")
        net_lines = [f"{x.get('Name', 'Adapter')} · {x.get('Status', 'N/A')} · {x.get('LinkSpeed', 'N/A')}\n{x.get('InterfaceDescription', '')}" for x in nets]
        self._set_hw("network", "\n\n".join(net_lines) or "Adapterdaten nicht verfügbar")
        oi = osinfo[0] if osinfo else {}
        self._set_hw("os", f"{oi.get('Caption', platform.platform())}\nVersion {oi.get('Version', platform.version())} · Build {oi.get('BuildNumber', 'N/A')}\n{oi.get('OSArchitecture', platform.machine())}")
        disp_lines = [f"{x.get('Name', 'Display')} · {x.get('ScreenWidth', 'N/A')} × {x.get('ScreenHeight', 'N/A')} · {x.get('Status', '')}" for x in displays]
        if not disp_lines:
            disp_lines = ["Display-Informationen werden vom Monitor-Treiber bereitgestellt."]
        self._set_hw("displays", "\n".join(disp_lines))
        self.set_status("Hardware-Erkennung abgeschlossen")
        if hasattr(self, "home_system_label"):
            cpus = cpu[0].get("Name", "N/A") if cpu else platform.processor()
            self.home_system_label.configure(text=f"OS: {oi.get('Caption', platform.platform())}\nCPU: {cpus}\nRAM: {fmt_bytes(psutil.virtual_memory().total)}\nBackup: {BACKUP_FILE}")

    def _set_hw(self, key, text):
        if key in getattr(self, "hw_labels", {}):
            self.hw_labels[key].configure(text=text)

    # --------------------------- processes ---------------------------
    def end_selected_process(self):
        selected = self.process_tree.selection()
        if not selected:
            messagebox.showinfo(APP_NAME, "Wähle zuerst einen Prozess aus.")
            return
        pid = int(selected[0])
        try:
            proc = psutil.Process(pid)
            name = proc.name()
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Prozess nicht mehr verfügbar: {exc}")
            return
        protected = {"system", "system idle process", "registry", "smss.exe", "csrss.exe", "wininit.exe", "services.exe", "lsass.exe", "winlogon.exe", "svchost.exe", "fontdrvhost.exe", "dwm.exe"}
        if pid <= 4 or name.lower() in protected:
            messagebox.showwarning(APP_NAME, "Dieser Windows-Systemprozess ist geschützt und kann über RISEN nicht beendet werden.")
            return
        if not messagebox.askyesno("Prozess beenden?", f"Soll '{name}' (PID {pid}) wirklich beendet werden?\n\nUngespeicherte Daten können verloren gehen."):
            return
        try:
            proc.terminate()
            self.set_status(f"Beenden angefordert: {name} (PID {pid})")
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Prozess konnte nicht beendet werden: {exc}")

    # --------------------------- tweak actions ---------------------------
    def _apply_registry_values(self, entries):
        if not IS_WINDOWS:
            raise RuntimeError("Registry-Tweaks sind nur auf Windows verfügbar.")
        for key, name, value in entries:
            BACKUPS.set_user_dword(key, name, value)
        self.update_backup_label()

    def apply_game_mode(self):
        self.engine_apply_dialog("game_mode")

    def apply_game_dvr(self):
        self.engine_apply_dialog("game_dvr")

    def apply_visual_performance(self):
        self.engine_apply_dialog("visualfx")

    def _current_power_plan(self):
        code, out = run_cmd(["powercfg", "/getactivescheme"], 8)
        m = re.search(r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})", out)
        return m.group(1) if code == 0 and m else None, out

    def _set_power_plan(self, scheme: str):
        current, output = self._current_power_plan()
        if not current:
            messagebox.showerror(APP_NAME, f"Aktiver Energieplan konnte nicht gelesen werden.\n{output}")
            return
        BACKUPS.remember_power_plan(current)
        code, out = run_cmd(["powercfg", "/setactive", scheme], 10)
        if code == 0:
            self.update_backup_label()
            messagebox.showinfo(APP_NAME, "Energieplan geändert. Der zuvor aktive Plan wurde gesichert.")
        else:
            messagebox.showerror(APP_NAME, f"Energieplan konnte nicht geändert werden:\n{out}")

    def apply_high_performance(self):
        self.engine_apply_dialog("power_high")

    def show_power_plan(self):
        _, out = self._current_power_plan()
        messagebox.showinfo("Aktiver Energieplan", out or "Keine Information verfügbar.")

    def restore_all(self):
        reg_count = len(BACKUPS.data.get("registry", {}))
        has_power = bool(BACKUPS.data.get("power_plan"))
        engine_ids = [t for t in CATALOG if self.engine.has_backup(t.id)]
        if not reg_count and not has_power and not engine_ids:
            messagebox.showinfo(APP_NAME, "Es sind keine gesicherten Tweaks vorhanden.")
            return
        if not messagebox.askyesno("Änderungen wiederherstellen?", "RISEN stellt alle gesicherten Tweaks (inkl. Registry-Werte und Energieplan) auf den Originalzustand zurück. Fortfahren?"):
            return
        e_ok, e_bad = self.engine.undo_all(CATALOG)
        restored, failed = BACKUPS.restore_registry() if reg_count else (0, 0)
        plan_ok = BACKUPS.restore_power_plan() if has_power else None
        self.update_backup_label()
        self.tl_render()
        messagebox.showinfo(APP_NAME, f"Wiederherstellung beendet.\nTweaks zurückgesetzt: {e_ok} · Fehler: {e_bad}\nAlte V2-Registry-Werte: {restored} · Fehler: {failed}\nAlter V2-Energieplan: {'wiederhergestellt' if plan_ok else ('nicht gesichert' if plan_ok is None else 'fehlgeschlagen')}")

    def backup_summary(self):
        applied = sum(1 for t in CATALOG if self.engine.has_backup(t.id))
        return f"Angewendete Tweaks mit Backup: {applied}\nAlte V2-Registry-Werte: {len(BACKUPS.data.get('registry', {}))}\nDatei: {TWEAK_STATE_FILE}"

    def update_backup_label(self):
        if hasattr(self, "backup_label"):
            self.backup_label.configure(text=self.backup_summary())

    def set_fortnite_high(self):
        procs = self.find_fortnite()
        if not procs:
            messagebox.showwarning(APP_NAME, "Fortnite läuft gerade nicht. Starte erst Fortnite und versuche es erneut.")
            return
        changed = failed = 0
        for proc in procs:
            try:
                proc.nice(psutil.HIGH_PRIORITY_CLASS)
                changed += 1
            except Exception as exc:
                failed += 1
                log(f"Fortnite priority error: {exc}")
        messagebox.showinfo(APP_NAME, f"Priorität auf HOCH gesetzt: {changed}\nFehlgeschlagen: {failed}\n\nNicht 'Echtzeit' verwenden.")

    def set_fortnite_normal(self):
        procs = self.find_fortnite()
        if not procs:
            messagebox.showinfo(APP_NAME, "Fortnite läuft gerade nicht. Nach dem nächsten Start gilt wieder die Standardpriorität.")
            return
        changed = 0
        for proc in procs:
            try:
                proc.nice(psutil.NORMAL_PRIORITY_CLASS)
                changed += 1
            except Exception as exc:
                log(f"Fortnite normal priority error: {exc}")
        messagebox.showinfo(APP_NAME, f"Auf NORMAL zurückgesetzt: {changed} Prozess(e).")

    def run_ping_test(self, address: str, label: str):
        self.network_result.configure(text=f"Teste {label} ({address}) …")
        self.set_status(f"Ping-Test zu {address} läuft …")
        def worker():
            res = netlab.ping_series(address, count=10, interval=0.2)
            text = f"{label}: " + res.summary().split(": ", 1)[-1]
            if self.alive:
                self.after(0, lambda: self.network_result.configure(text=text + "\nHinweis: allgemeiner ICMP-Test, nicht der Fortnite-Server-Ping."))
                self.after(0, lambda: self.set_status("Netzwerktest abgeschlossen"))
        threading.Thread(target=worker, daemon=True).start()

    def apply_mouse_acceleration_off(self):
        self.engine_apply_dialog("mouse_accel")

    def show_all_devices(self):
        script = "Get-PnpDevice -PresentOnly -ErrorAction SilentlyContinue | Sort-Object Class,FriendlyName | Select-Object -First 100 Class,FriendlyName,Status | Format-Table -AutoSize | Out-String -Width 220"
        code, out = run_ps(script, 20)
        if code == 0 and out:
            messagebox.showinfo("Erkannte Geräte (erste 100)", out[:11500])
        else:
            messagebox.showwarning(APP_NAME, "Die Windows-Geräteliste konnte nicht gelesen werden. Öffne stattdessen den Geräte-Manager.")

    def show_startup_apps(self):
        if not IS_WINDOWS:
            return
        import winreg
        found = []
        registry_paths = [
            (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", "HKCU Run"),
            (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\RunOnce", "HKCU RunOnce"),
            (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\Run", "HKLM Run"),
            (winreg.HKEY_LOCAL_MACHINE, r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Run", "HKLM Run 32-bit"),
        ]
        for hive, key_path, label in registry_paths:
            try:
                with winreg.OpenKey(hive, key_path, 0, winreg.KEY_READ) as key:
                    count = winreg.QueryInfoKey(key)[1]
                    for i in range(count):
                        name, value, _ = winreg.EnumValue(key, i)
                        found.append(f"[{label}] {name}\n{value}")
            except OSError:
                continue
        startup_dirs = [
            Path(os.environ.get("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs/Startup",
            Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "Microsoft/Windows/Start Menu/Programs/Startup",
        ]
        for folder in startup_dirs:
            try:
                for item in folder.iterdir():
                    found.append(f"[Startup folder] {item.name}\n{item}")
            except Exception:
                pass
        text = "\n\n".join(found) if found else "Keine Autostart-Einträge gefunden oder auslesbar."
        messagebox.showinfo("Autostart-Übersicht", text[:12000] + ("\n\n… Ausgabe gekürzt." if len(text) > 12000 else ""))

    def flush_dns(self):
        if not messagebox.askyesno("DNS-Cache leeren?", "Der lokale DNS-Namenscache wird geleert. Das senkt normalerweise nicht den Ingame-Ping. Fortfahren?"):
            return
        code, out = run_cmd(["ipconfig", "/flushdns"], 10)
        if code == 0:
            messagebox.showinfo(APP_NAME, out[-900:] or "DNS-Cache geleert.")
        else:
            messagebox.showerror(APP_NAME, f"DNS-Cache konnte nicht geleert werden:\n{out[-900:]}")

    def show_network_adapters(self):
        script = "Get-NetAdapter | Format-Table -AutoSize Name,Status,LinkSpeed,InterfaceDescription | Out-String -Width 220"
        code, out = run_ps(script, 12)
        messagebox.showinfo("Netzwerkadapter", out[:3500] if code == 0 and out else "Keine Adapterdaten verfügbar.")

    def clean_temp(self):
        temp = Path(os.environ.get("TEMP", str(Path.home() / "AppData/Local/Temp")))
        if not temp.exists() or not temp.is_dir():
            messagebox.showinfo(APP_NAME, "Temp-Ordner nicht gefunden.")
            return
        if not messagebox.askyesno("Temporäre Dateien löschen?", f"Es werden nur Inhalte dieses Ordners gelöscht:\n{temp}\n\nGeöffnete/gesperrte Dateien werden übersprungen. Fortfahren?"):
            return
        deleted = skipped = 0
        for item in temp.iterdir():
            try:
                if item.is_symlink():
                    skipped += 1
                elif item.is_dir():
                    shutil.rmtree(item)
                    deleted += 1
                else:
                    item.unlink()
                    deleted += 1
            except Exception:
                skipped += 1
        messagebox.showinfo(APP_NAME, f"Temp-Bereinigung abgeschlossen.\nGelöschte Einträge: {deleted}\nÜbersprungen: {skipped}")

    # --------------------------- launchers / tools ---------------------------
    def open_fortnite_config(self):
        path = LOCAL / "FortniteGame" / "Saved" / "Config" / "WindowsClient"
        if path.exists():
            self.open_path(path)
        else:
            messagebox.showinfo(APP_NAME, f"Fortnite Config nicht gefunden:\n{path}\n\nStarte Fortnite mindestens einmal.")

    def open_epic(self):
        candidates = [
            Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Epic Games/Launcher/Portal/Binaries/Win64/EpicGamesLauncher.exe",
            Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Epic Games/Launcher/Portal/Binaries/Win64/EpicGamesLauncher.exe",
        ]
        for candidate in candidates:
            if candidate.exists():
                subprocess.Popen([str(candidate)], close_fds=True)
                return
        try:
            os.startfile("com.epicgames.launcher://apps/Fortnite?action=launch&silent=true")
        except Exception:
            webbrowser.open("https://store.epicgames.com/")

    def show_gpu_telemetry(self):
        data = self.gpu_data or self.read_gpu_data()
        self._accept_gpu_data(data)
        messagebox.showinfo("GPU Live-Telemetrie", safe_gpu_detail(data, verbose=True))

    def nvidia_check(self):
        data = self.read_gpu_data()
        self._accept_gpu_data(data)
        if data.get("available"):
            messagebox.showinfo("NVIDIA System-Check", safe_gpu_detail(data, verbose=True))
        else:
            messagebox.showwarning("NVIDIA System-Check", "NVIDIA-SMI konnte keine Daten liefern. Prüfe NVIDIA-Treiberinstallation.\n\n" + str(data.get("error", data.get("name", "Unbekannter Fehler"))))

    def open_nvidia_panel(self):
        candidates = [
            Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "NVIDIA Corporation/Control Panel Client/nvcplui.exe",
            Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32/nvcplui.exe",
        ]
        for path in candidates:
            if path.exists():
                subprocess.Popen([str(path)], close_fds=True)
                return
        messagebox.showinfo(APP_NAME, "NVIDIA Control Panel wurde nicht an den Standardpfaden gefunden. Öffne die Windows-Grafikeinstellungen oder installiere die NVIDIA-App / den Treiber neu.")

    def open_amd_software(self):
        candidates = [
            Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "AMD/CNext/CNext/RadeonSoftware.exe",
            Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "AMD/CNext/CNext/RadeonSoftware.exe",
        ]
        for path in candidates:
            if path.exists():
                subprocess.Popen([str(path)], close_fds=True)
                return
        messagebox.showinfo(APP_NAME, "AMD Software wurde nicht gefunden. Bei deinem NVIDIA-PC ist dieser Bereich nicht erforderlich.")

    def launch_tool(self, executable: str):
        try:
            if IS_WINDOWS:
                subprocess.Popen(["cmd.exe", "/c", "start", "", executable], close_fds=True)
            else:
                raise RuntimeError("Nur unter Windows verfügbar.")
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Tool konnte nicht geöffnet werden:\n{exc}")

    def open_uri(self, uri: str):
        try:
            if IS_WINDOWS:
                os.startfile(uri)
            else:
                webbrowser.open(uri)
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Einstellung konnte nicht geöffnet werden:\n{exc}")

    def open_path(self, path: Path):
        try:
            path = Path(path)
            if path.is_file() and not path.exists():
                messagebox.showinfo(APP_NAME, "Die Datei wurde noch nicht erstellt.")
                return
            if not path.exists() and path.suffix:
                messagebox.showinfo(APP_NAME, "Die Datei wurde noch nicht erstellt.")
                return
            if path.is_dir():
                path.mkdir(parents=True, exist_ok=True)
            if IS_WINDOWS:
                os.startfile(str(path))
            else:
                messagebox.showinfo(APP_NAME, str(path))
        except Exception as exc:
            messagebox.showerror(APP_NAME, str(exc))

    def export_report(self):
        default_name = f"RISEN_Systemreport_{datetime.now():%Y%m%d_%H%M%S}.txt"
        destination = filedialog.asksaveasfilename(defaultextension=".txt", initialfile=default_name, filetypes=[("Textdatei", "*.txt")])
        if not destination:
            return
        vm = psutil.virtual_memory()
        lines = [
            f"{APP_NAME} v{APP_VERSION}", f"Generated: {datetime.now().isoformat(sep=' ', timespec='seconds')}",
            f"Windows: {platform.platform()}", f"Machine: {platform.machine()}", f"Processor: {platform.processor()}",
            f"CPU logical cores: {psutil.cpu_count(logical=True)}", f"RAM: {fmt_bytes(vm.total)} · {vm.percent:.1f}% used",
            f"GPU: {safe_gpu_detail(self.gpu_data or {})}", f"Backup: {BACKUP_FILE}", "",
            "NOTE: ICMP internet ping is not the same as Fortnite server ping.",
        ]
        try:
            Path(destination).write_text("\n".join(lines), encoding="utf-8")
            messagebox.showinfo(APP_NAME, f"Systembericht gespeichert:\n{destination}")
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Export fehlgeschlagen:\n{exc}")

    # ======================= Tweak Library (V3) =======================
    def _run_bg(self, work, done):
        def runner():
            try:
                res = work()
            except Exception as exc:  # noqa: BLE001
                log(f"Hintergrundaufgabe: {exc}")
                res = tweaklib.Result(False, f"Unerwarteter Fehler: {exc}")
            if self.alive:
                self.after(0, lambda: done(res))
        threading.Thread(target=runner, daemon=True).start()

    def engine_apply_dialog(self, tweak_id: str):
        t = CATALOG_BY_ID[tweak_id]
        if not messagebox.askyesno(
                f"{t.name} anwenden?",
                f"{t.description}\n\nAuswirkungen: {t.impact}\n\nDer Originalzustand wird vorher gesichert und die Änderung "
                f"anschließend per Rücklesen geprüft. Du kannst sie per Undo zurücksetzen. Fortfahren?"):
            return
        self.set_status(f"Wende an: {t.name} …")

        def done(res):
            self.update_backup_label()
            self.tl_refresh_status(tweak_id, res.message)
            self.set_status(("✔ " if res.ok else "✖ ") + t.name + ": " + res.message[:120])
            (messagebox.showinfo if res.ok else messagebox.showerror)(APP_NAME, res.message)
        self._run_bg(lambda: self.engine.apply(t), done)

    def engine_undo_dialog(self, tweak_id: str):
        t = CATALOG_BY_ID[tweak_id]
        if not self.engine.has_backup(tweak_id):
            messagebox.showinfo(APP_NAME, "Für diesen Tweak gibt es kein Backup: RISEN hat ihn nicht angewendet, daher ist nichts zurückzusetzen.")
            return
        if not messagebox.askyesno(f"{t.name} zurücksetzen?", "Der gesicherte Originalzustand wird wiederhergestellt und geprüft. Fortfahren?"):
            return

        def done(res):
            self.update_backup_label()
            self.tl_refresh_status(tweak_id, res.message)
            self.set_status(("✔ " if res.ok else "✖ ") + t.name + ": " + res.message[:120])
            (messagebox.showinfo if res.ok else messagebox.showerror)(APP_NAME, res.message)
        self._run_bg(lambda: self.engine.undo(t), done)

    CHIP_NAMES = {
        "Gaming Optimization": "Gaming", "Fortnite Optimization": "Fortnite", "Windows Optimization": "Windows",
        "Mouse Optimization": "Maus", "Keyboard Optimization": "Tastatur", "Power Optimization": "Power",
        "Network Optimization": "Netzwerk", "NVIDIA Optimization": "NVIDIA", "AMD Optimization": "AMD",
        "CPU Tweaks": "CPU", "RAM Tweaks": "RAM", "Cleanup": "Cleanup", "Startup Optimization": "Autostart",
        "Advanced Tweaks": "Advanced",
    }
    FALLBACK = {"power_ultimate": "power_high"}

    def _build_tweaklib_page(self):
        p = self.pages["Control Center"]
        hero = ctk.CTkFrame(p, fg_color=PANEL, corner_radius=18, border_width=1, border_color=BORDER)
        hero.pack(fill="x", padx=7, pady=(4, 8))
        hero.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(hero, text="CONTROL CENTER", text_color=TEXT, anchor="w",
                     font=ctk.CTkFont("Segoe UI", 28, "bold")).grid(row=0, column=0, sticky="w", padx=22, pady=(16, 0))
        ctk.CTkLabel(hero, text="Schalter setzen, dann Apply. Jede Änderung wird vorher gesichert, danach verifiziert und ist jederzeit umkehrbar.",
                     text_color=MUTED, anchor="w", font=ctk.CTkFont("Segoe UI", 10)).grid(row=1, column=0, sticky="w", padx=22)
        self.cc_pending = ctk.CTkLabel(hero, text="Keine Änderungen ausgewählt", text_color=CYAN, anchor="w",
                                       font=ctk.CTkFont("Segoe UI", 11, "bold"))
        self.cc_pending.grid(row=2, column=0, sticky="w", padx=22, pady=(8, 0))
        self.cc_progress = ctk.CTkProgressBar(hero, height=6, progress_color=CYAN, fg_color="#1A1A2B", corner_radius=3)
        self.cc_progress.set(0)
        self.cc_progress.grid(row=3, column=0, sticky="ew", padx=22, pady=(8, 0))
        self.cc_progress_label = ctk.CTkLabel(hero, text="", text_color=MUTED, anchor="w", font=ctk.CTkFont("Segoe UI", 9))
        self.cc_progress_label.grid(row=4, column=0, sticky="w", padx=22)
        bar = ctk.CTkFrame(hero, fg_color="transparent")
        bar.grid(row=5, column=0, sticky="ew", padx=16, pady=(8, 16))
        bar.grid_columnconfigure((0, 1, 2), weight=1, uniform="cc")
        specs = [
            ("⚡  BEST TWEAKS FOR MY PC", self.cc_best, PURPLE, PURPLE_HOVER, "#ffffff", 0, 56),
            ("APPLY", lambda: self.cc_apply(False), "#1E1E33", "#2A2A44", "#ffffff", 1, 56),
            ("APPLY & RESTART", lambda: self.cc_apply(True), CYAN, "#67E8F9", "#04121A", 2, 56),
        ]
        for text, cmd, fg, hov, tc, col, h in specs:
            b = ctk.CTkButton(bar, text=text, command=cmd, height=h, corner_radius=14, fg_color=fg, hover_color=hov,
                              text_color=tc, border_width=1, border_color=BORDER, font=ctk.CTkFont("Segoe UI", 13, "bold"))
            b.grid(row=0, column=col, sticky="ew", padx=6)
            fx.hover_fade(b, {"border_color": (BORDER, "#ffffff")})
        ctk.CTkButton(hero, text="Alle Tweaks zurücksetzen", command=self.restore_all, width=190, height=30, corner_radius=10,
                      fg_color="transparent", hover_color="#1E1E33", text_color=MUTED, font=ctk.CTkFont("Segoe UI", 9)
                      ).grid(row=0, column=1, rowspan=2, sticky="ne", padx=16, pady=14)
        self.cc_chip_frame = ctk.CTkFrame(p, fg_color="transparent")
        self.cc_chip_frame.pack(fill="x", padx=4, pady=(0, 4))
        self.tl_grid = ctk.CTkFrame(p, fg_color="transparent")
        self.tl_grid.pack(fill="x")
        self.tl_grid.grid_columnconfigure((0, 1), weight=1, uniform="tl")
        self.tl_extra = ctk.CTkFrame(p, fg_color="transparent")
        self.tl_extra.pack(fill="x")

    def tl_select_category(self, category: str):
        self.tl_category = category
        if self.active_page != "Control Center":
            self.show_page("Control Center")
        else:
            self.tl_render()

    def _render_chips(self):
        for w in self.cc_chip_frame.winfo_children():
            w.destroy()
        for i, cat in enumerate(tweaklib.CATEGORIES):
            active = cat == self.tl_category
            chip = ctk.CTkButton(self.cc_chip_frame, text=self.CHIP_NAMES.get(cat, cat), height=32, corner_radius=16, width=96,
                                 fg_color=PURPLE_DARK if active else "#12121F", hover_color="#2A2A44",
                                 text_color="#ffffff" if active else "#B6B8D0", border_width=1,
                                 border_color=CYAN if active else BORDER, font=ctk.CTkFont("Segoe UI", 10, "bold"),
                                 command=lambda c=cat: self.tl_select_category(c))
            chip.grid(row=i // 7, column=i % 7, padx=4, pady=4, sticky="ew")
            if not active:
                fx.hover_fade(chip, {"border_color": (BORDER, PURPLE_HOVER)})
        for c in range(7):
            self.cc_chip_frame.grid_columnconfigure(c, weight=1, uniform="chip")

    def tl_render(self):
        if not hasattr(self, "tl_grid"):
            return
        for w in list(self.tl_grid.winfo_children()) + list(self.tl_extra.winfo_children()):
            w.destroy()
        self.tl_status_labels, self.cc_vars = {}, {}
        self._render_chips()
        items = tweaklib.by_category(CATALOG, self.tl_category)
        cols = [self.group(self.tl_grid, f"{self.tl_category.upper()} · {len(items)} EINTRÄGE" if i == 0 else " ", 0, i) for i in range(2)]
        for idx, t in enumerate(items):
            self.tl_card(cols[idx % 2], t)
        if self.tl_category == "Startup Optimization":
            self.tl_render_startup()
        for t in items:
            self.tl_refresh_status(t.id)
        self.cc_update_pending()

    def tl_card(self, parent, t):
        toggle = t.kind in tweaklib.TOGGLE_KINDS
        card = ctk.CTkFrame(parent, fg_color=CARD, corner_radius=14, border_width=1, border_color=BORDER)
        card.pack(fill="x", pady=6)
        head = ctk.CTkFrame(card, fg_color="transparent")
        head.pack(fill="x", padx=13, pady=(11, 2))
        head.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(head, text=t.name, text_color=TEXT, font=ctk.CTkFont("Segoe UI", 12, "bold"), anchor="w", justify="left",
                     wraplength=430).grid(row=0, column=0, sticky="w")
        if toggle:
            var = tk.BooleanVar(value=self.cc_selected.get(t.id, self.cc_active.get(t.id, False)))
            self.cc_vars[t.id] = var
            ctk.CTkSwitch(head, text="", variable=var, onvalue=True, offvalue=False, width=58, switch_width=48, switch_height=24,
                          progress_color=CYAN, button_color="#ffffff", button_hover_color="#E0F7FF", fg_color="#2A2A44",
                          command=lambda i=t.id, v=var: self.cc_toggle(i, bool(v.get()))).grid(row=0, column=1, padx=(8, 0))
        ctk.CTkLabel(card, text=t.description, text_color="#B6B8D0", font=ctk.CTkFont("Segoe UI", 9), anchor="w", justify="left",
                     wraplength=500).pack(fill="x", padx=13, pady=(0, 4))
        ctk.CTkLabel(card, text="Auswirkung: " + t.impact, text_color="#8689A6", font=ctk.CTkFont("Segoe UI", 9), anchor="w",
                     justify="left", wraplength=500).pack(fill="x", padx=13, pady=(0, 4))
        if toggle:
            ctk.CTkLabel(card, text=f"Wirksam: {t.restart} · Backup · Verifikation · Undo per Schalter", text_color=MUTED,
                         font=ctk.CTkFont("Segoe UI", 8), anchor="w", justify="left", wraplength=500).pack(fill="x", padx=13, pady=(0, 4))
        status = ctk.CTkLabel(card, text="● wird geprüft …", text_color=MUTED, font=ctk.CTkFont("Segoe UI", 9, "bold"),
                              anchor="w", justify="left", wraplength=500)
        status.pack(fill="x", padx=13, pady=(0, 10 if toggle else 6))
        self.tl_status_labels[t.id] = status
        if not toggle:
            row = ctk.CTkFrame(card, fg_color="transparent")
            row.pack(fill="x", padx=9, pady=(0, 10))
            row.grid_columnconfigure((0, 1), weight=1, uniform="b")
            def mk(text, cmd, accent=False, col=0):
                ctk.CTkButton(row, text=text, command=cmd, height=36, corner_radius=10, text_color="#ffffff",
                              fg_color=PURPLE if accent else "#1E1E33", hover_color=PURPLE_HOVER if accent else "#2A2A44",
                              font=ctk.CTkFont("Segoe UI", 10, "bold")).grid(row=0, column=col, sticky="ew", padx=3)
            if t.kind == "action":
                mk(t.action_label, lambda a=t.action_id: self.tl_action(a), True, 0)
                ctk.CTkLabel(row, text="Nicht umkehrbar" if t.action_id != "open_startup" else "Umschaltbar", text_color=MUTED,
                             font=ctk.CTkFont("Segoe UI", 9)).grid(row=0, column=1)
            else:
                mk("Status prüfen", lambda i=t.id: self.tl_refresh_status(i), False, 0)
        fx.hover_fade(card, {"fg_color": (CARD, CARD_HOVER), "border_color": (BORDER, PURPLE)})

    def tl_action(self, action_id: str):
        if action_id == "clean_temp":
            self.clean_temp()
        elif action_id == "flush_dns":
            self.flush_dns()
        elif action_id == "open_startup":
            self.tl_select_category("Startup Optimization")

    def tl_refresh_status(self, tweak_id: str, extra: str = ""):
        t = CATALOG_BY_ID[tweak_id]
        label = self.tl_status_labels.get(tweak_id)
        if t.kind == "action":
            if label is not None:
                try:
                    label.configure(text="● einmalige Aktion", text_color=MUTED)
                except Exception:  # noqa: BLE001
                    pass
            return

        def show(result):
            state, detail = result
            self.cc_active[tweak_id] = state == tweaklib.STATUS_ACTIVE
            if t.kind in tweaklib.TOGGLE_KINDS and tweak_id not in self.cc_selected:
                var = getattr(self, "cc_vars", {}).get(tweak_id)
                if var is not None:
                    try:
                        var.set(self.cc_active[tweak_id])
                    except Exception:  # noqa: BLE001
                        pass
            self.cc_update_pending()
            if label is None:
                return
            if t.kind == "info":
                text, color = detail, "#CFD2EA"
            else:
                color = {tweaklib.STATUS_ACTIVE: GREEN, tweaklib.STATUS_PARTIAL: YELLOW}.get(state, MUTED)
                dot = "●" if state == tweaklib.STATUS_ACTIVE else "○"
                text = f"{dot} {state.upper()} · {detail}" + ("  [Backup]" if self.engine.has_backup(tweak_id) else "")
                if extra:
                    text += f"\nLetzte Aktion: {extra}"
            try:
                label.configure(text=text, text_color=color)
            except Exception:  # noqa: BLE001
                pass

        def runner():
            try:
                res = self.engine.status(t)
            except Exception as exc:  # noqa: BLE001
                res = (tweaklib.STATUS_UNKNOWN, str(exc))
            if self.alive:
                self.after(0, lambda: show(res))
        threading.Thread(target=runner, daemon=True).start()

    def tl_render_startup(self):
        box = self.group(self.tl_extra, "AUTOSTART (BENUTZER) · UMSCHALTEN WIE IM TASK-MANAGER", 0, 0)
        entries = tweaklib.list_startup(self.engine.backend)
        if not entries:
            ctk.CTkLabel(box, text="Keine Benutzer-Autostart-Einträge unter HKCU\\Run gefunden. Systemweite Einträge und der Autostart-Ordner stehen unter Tools → Autostart anzeigen.",
                         text_color="#bdbdbd", wraplength=900, justify="left", anchor="w").pack(fill="x", padx=8, pady=8)
            return
        for e in entries:
            row = ctk.CTkFrame(box, fg_color=CARD, corner_radius=12, border_width=1, border_color=BORDER)
            row.pack(fill="x", pady=3)
            row.grid_columnconfigure(0, weight=1)
            ctk.CTkLabel(row, text=f"{e.name}  ·  {'AKTIV' if e.enabled else 'DEAKTIVIERT'}", text_color=GREEN if e.enabled else MUTED,
                         anchor="w", font=ctk.CTkFont("Segoe UI", 10, "bold")).grid(row=0, column=0, sticky="w", padx=12, pady=(7, 0))
            ctk.CTkLabel(row, text=e.command[:140], text_color="#8689A6", anchor="w", font=ctk.CTkFont("Segoe UI", 8)).grid(row=1, column=0, sticky="w", padx=12, pady=(0, 7))
            ctk.CTkButton(row, text="Deaktivieren" if e.enabled else "Aktivieren", width=130, height=32, corner_radius=10,
                          fg_color="#1E1E33" if e.enabled else PURPLE, hover_color="#2A2A44" if e.enabled else PURPLE_HOVER,
                          command=lambda n=e.name, en=e.enabled: self.tl_toggle_startup(n, not en)).grid(row=0, column=1, rowspan=2, padx=10)
            fx.hover_fade(row, {"border_color": (BORDER, CYAN)})

    def tl_toggle_startup(self, name: str, enable: bool):
        if not enable and not messagebox.askyesno("Autostart deaktivieren?", f"'{name}' startet danach nicht mehr automatisch mit Windows. Der Eintrag wird nicht gelöscht und kann hier wieder aktiviert werden. Fortfahren?"):
            return
        res = tweaklib.set_startup_enabled(self.engine.backend, name, enable)
        log(f"Autostart {name} -> {enable}: {res.message}")
        self.set_status(("✔ " if res.ok else "✖ ") + res.message[:120])
        if not res.ok:
            messagebox.showerror(APP_NAME, res.message)
        self.tl_render()

    # ---------------- Control Center: Auswahl, Apply, Best Tweaks ----------------
    def cc_toggle(self, tweak_id: str, value: bool):
        self.cc_selected[tweak_id] = value
        self.cc_update_pending()

    def cc_plan(self) -> list:
        """Aenderungen = Schalter, die vom aktuellen Zustand abweichen. Reihenfolge so,
        dass Energieplan-Wechsel nicht mit Einzeleinstellungen kollidieren."""
        plan = []
        for tid, want in self.cc_selected.items():
            t = CATALOG_BY_ID.get(tid)
            if t is None or t.kind not in tweaklib.TOGGLE_KINDS:
                continue
            if want != self.cc_active.get(tid, False):
                plan.append((t, want))

        def key(item):
            t, on = item
            if on:
                return (1, 0 if t.kind == "power" else 1)
            return (0, {"powersetting": 0, "power": 2}.get(t.kind, 1))
        return sorted(plan, key=key)

    def cc_update_pending(self):
        if not hasattr(self, "cc_pending"):
            return
        n = len(self.cc_plan())
        try:
            self.cc_pending.configure(text="Keine Änderungen ausgewählt" if n == 0 else f"{n} Änderung{'en' if n != 1 else ''} ausstehend – APPLY drücken",
                                      text_color=CYAN if n == 0 else YELLOW)
        except Exception:  # noqa: BLE001
            pass

    def _cc_progress(self, value: float, text: str = ""):
        if not self.alive:
            return
        def apply():
            try:
                self.cc_progress.set(max(0.0, min(1.0, value)))
                if text:
                    self.cc_progress_label.configure(text=text)
            except Exception:  # noqa: BLE001
                pass
        self.after(0, apply)

    def cc_apply(self, restart: bool = False):
        if self.cc_busy:
            return
        plan = self.cc_plan()
        if not plan:
            messagebox.showinfo(APP_NAME, "Keine Änderungen ausgewählt.\n\nSchalte bei den Tweaks Schalter um (oder nutze 'Best Tweaks for my PC') und drücke dann APPLY.")
            return
        lines = [("＋ " if on else "－ ") + t.name + ("" if on else "  (zurücksetzen)") for t, on in plan]
        msg = "Diese Änderungen werden ausgeführt:\n\n" + "\n".join(lines) + "\n\nJede Änderung wird vorher gesichert und danach geprüft."
        if restart:
            msg += "\n\nDanach startet der PC neu (20 s Countdown, abbrechbar). Speichere vorher deine Arbeit."
        if not messagebox.askyesno("Änderungen anwenden?", msg):
            return
        self._cc_run(plan, restart)

    def _cc_run(self, plan: list, restart: bool, offer_restart: bool = False):
        self.cc_busy = True
        self.cc_progress.set(0)

        def worker():
            results = []
            total = len(plan)
            for i, (t, on) in enumerate(plan):
                self._cc_progress(i / total, f"{'Wende an' if on else 'Setze zurück'}: {t.name}  ({i + 1}/{total})")
                try:
                    if on:
                        r = self.engine.apply(t)
                        fb = self.FALLBACK.get(t.id)
                        if not r.ok and fb:
                            fb_t = CATALOG_BY_ID[fb]
                            r2 = self.engine.apply(fb_t)
                            if r2.ok:
                                t, r = fb_t, tweaklib.Result(True, f"{fb_t.name} (Ersatz, da der gewünschte Plan nicht verfügbar ist)", True)
                    else:
                        r = self.engine.undo(t) if self.engine.has_backup(t.id) else tweaklib.Result(False, "Kein Backup: von RISEN nicht angewendet.")
                except Exception as exc:  # noqa: BLE001
                    log(f"cc_run {t.id}: {exc}")
                    r = tweaklib.Result(False, f"Unerwarteter Fehler: {exc}")
                results.append((t, on, r))
            self._cc_progress(1.0, "Fertig.")
            if self.alive:
                self.after(0, lambda: self._cc_done(results, restart, offer_restart))
        threading.Thread(target=worker, daemon=True).start()

    def _cc_done(self, results, restart: bool, offer_restart: bool):
        self.cc_busy = False
        ok = [x for x in results if x[2].ok]
        bad = [x for x in results if not x[2].ok]
        self.cc_selected = {}
        self.update_backup_label()
        self.tl_render()
        lines = [f"✔ {t.name}" for t, _, _ in ok] + [f"✖ {t.name}: {r.message}" for t, _, r in bad]
        self.set_status(f"{len(ok)} erfolgreich · {len(bad)} fehlgeschlagen")
        messagebox.showinfo(APP_NAME, f"Fertig: {len(ok)} erfolgreich, {len(bad)} fehlgeschlagen.\n\n" + "\n".join(lines))
        needs_restart = any(t.restart and t.restart != "Nein" for t, on, r in ok) or any(t.kind == "power" for t, _, _ in ok)
        if restart:
            if bad and not messagebox.askyesno("Trotzdem neu starten?", "Einige Änderungen sind fehlgeschlagen. Trotzdem neu starten?"):
                return
            self.cc_restart_countdown()
        elif offer_restart and ok and needs_restart:
            if messagebox.askyesno("Neustart empfohlen", "Für die volle Wirkung einiger Tweaks (z. B. Anmeldeeinstellungen) wird ein Neustart empfohlen. Jetzt neu starten?"):
                self.cc_restart_countdown()

    def cc_restart_countdown(self):
        code, out = run_cmd(["shutdown", "/r", "/t", "20", "/c", "RISEN TWEAKS: Neustart nach Tweaks"], 10)
        if code != 0:
            messagebox.showerror(APP_NAME, f"Neustart konnte nicht geplant werden:\n{out[:300]}")
            return
        win = ctk.CTkToplevel(self)
        win.title("Neustart")
        win.geometry("400x190")
        win.configure(fg_color=BG)
        win.attributes("-topmost", True)
        label = ctk.CTkLabel(win, text="Neustart in 20 s", text_color=CYAN, font=ctk.CTkFont("Segoe UI", 22, "bold"))
        label.pack(pady=(26, 8))
        ctk.CTkLabel(win, text="Speichere offene Arbeit. Du kannst den Neustart abbrechen.", text_color=MUTED).pack()

        def cancel():
            run_cmd(["shutdown", "/a"], 10)
            win.destroy()
            self.set_status("Neustart abgebrochen")
        ctk.CTkButton(win, text="Neustart abbrechen", command=cancel, fg_color=PURPLE, hover_color=PURPLE_HOVER, height=40).pack(pady=18)

        def tick(n=19):
            if n < 0 or not self.alive:
                return
            try:
                label.configure(text=f"Neustart in {n} s")
                win.after(1000, lambda: tick(n - 1))
            except Exception:  # noqa: BLE001
                pass
        win.after(1000, tick)

    def cc_best(self):
        if self.cc_busy:
            return
        self._cc_progress(0.0, "Analysiere dein System …")

        def worker():
            try:
                battery = psutil.sensors_battery() if hasattr(psutil, "sensors_battery") else None
            except Exception:  # noqa: BLE001
                battery = None
            ctx = {"desktop": battery is None,
                   "fortnite_config": tweaklib.fortnite_ini_path().exists(),
                   "fortnite_running": bool(self.find_fortnite()), "admin": is_admin()}
            chosen, skipped = tweaklib.recommend(CATALOG, self.engine, ctx)
            cpu = ""
            try:
                cpu = (self.hw_data.get("CPU") or {}).get("Name", "") if isinstance(self.hw_data.get("CPU"), dict) else ""
            except Exception:  # noqa: BLE001
                pass
            gpu_name = (self.gpu_data or {}).get("name", "GPU unbekannt")
            head = f"System: {cpu or platform.processor() or 'CPU'} · {gpu_name} · {fmt_bytes(psutil.virtual_memory().total)} RAM · {'Desktop' if ctx['desktop'] else 'Notebook'}"
            if self.alive:
                self.after(0, lambda: self._cc_best_ask(head, chosen, skipped))
        threading.Thread(target=worker, daemon=True).start()

    def _cc_best_ask(self, head, chosen, skipped):
        self._cc_progress(0.0, "")
        if not chosen:
            extra = ("\n\nÜbersprungen:\n" + "\n".join(f"· {t.name}: {why}" for t, why in skipped)) if skipped else ""
            messagebox.showinfo(APP_NAME, f"{head}\n\nAlles Empfohlene ist bereits aktiv oder auf diesem System nicht anwendbar.{extra}")
            return
        lines = [f"＋ {t.name}\n     {t.why or t.description}" for t in chosen]
        skip = ("\n\nÜbersprungen:\n" + "\n".join(f"· {t.name}: {why}" for t, why in skipped)) if skipped else ""
        msg = (f"{head}\n\nFür dein System empfohlen ({len(chosen)}):\n\n" + "\n".join(lines) + skip +
               "\n\nNur sichere, umkehrbare Tweaks. Backup vorher, Verifikation danach. Jetzt anwenden?")
        if not messagebox.askyesno("Best Tweaks for my PC", msg):
            return
        for t in chosen:
            self.cc_selected[t.id] = True
            self.cc_active[t.id] = False
        self._cc_run([(t, True) for t in sorted(chosen, key=lambda x: 0 if x.kind == "power" else 1)], False, offer_restart=True)

    # ======================= Tweaks-Tab (V3.4, gecachte Karten) =======================
    TW_PROFILES = (("SAFE", "safe"), ("COMPETITIVE", "competitive"), ("MAX FPS", "maxfps"))
    TW_VIEW_REC = "★ Empfohlen"
    TW_VIEW_ACTIVE = "Aktiv"

    def _toggle_categories(self) -> list:
        return [c for c in tweaklib.CATEGORIES if any(t.kind in tweaklib.TOGGLE_KINDS for t in tweaklib.by_category(CATALOG, c))]

    def _build_tweaks_page(self):
        p = self.pages["Tweaks"]
        self.tw_view = self.TW_VIEW_REC
        self.tw_query = tk.StringVar(value="")
        self.tw_busy = False
        self.tw_states: dict[str, tuple[str, str]] = {}
        self.tw_states_at = 0.0
        self.card_refs: dict[str, list] = {}
        self.card_cache: dict[tuple, ctk.CTkFrame] = {}
        self._tw_filter_job = None
        hero = ctk.CTkFrame(p, fg_color=PANEL, corner_radius=18, border_width=1, border_color=BORDER)
        hero.pack(fill="x", padx=7, pady=(4, 8))
        hero.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(hero, text="TWEAKS", text_color=TEXT, anchor="w",
                     font=ctk.CTkFont("Segoe UI", 28, "bold")).grid(row=0, column=0, sticky="w", padx=22, pady=(16, 0))
        ctk.CTkLabel(hero, text="Schalter umlegen = sofort angewendet (Backup vorher, Prüfung danach, jederzeit umkehrbar). "
                                "Oder nur AUTO OPTIMIZE drücken: RISEN erkennt dein System und setzt alles selbst.",
                     text_color=MUTED, anchor="w", justify="left", wraplength=900,
                     font=ctk.CTkFont("Segoe UI", 10)).grid(row=1, column=0, sticky="w", padx=22)
        self.tw_info = ctk.CTkLabel(hero, text="System wird erkannt …", text_color=CYAN, anchor="w",
                                    font=ctk.CTkFont("Segoe UI", 11, "bold"))
        self.tw_info.grid(row=2, column=0, sticky="w", padx=22, pady=(8, 0))
        self.tw_progress = ctk.CTkProgressBar(hero, height=6, progress_color=CYAN, fg_color="#1A1A2B", corner_radius=3)
        self.tw_progress.set(0)
        self.tw_progress.grid(row=3, column=0, columnspan=2, sticky="ew", padx=22, pady=(8, 0))
        self.tw_progress_label = ctk.CTkLabel(hero, text="", text_color=MUTED, anchor="w", font=ctk.CTkFont("Segoe UI", 9))
        self.tw_progress_label.grid(row=4, column=0, columnspan=2, sticky="w", padx=22)
        ctk.CTkButton(hero, text="Alles zurücksetzen", command=self.tw_undo_all, width=150, height=30, corner_radius=10,
                      fg_color="transparent", hover_color="#1E1E33", text_color=MUTED,
                      font=ctk.CTkFont("Segoe UI", 9)).grid(row=0, column=1, sticky="ne", padx=16, pady=14)
        if not is_admin():
            ctk.CTkButton(hero, text="🔒 Als Administrator neu starten (für alle Tweaks)", command=self.relaunch_as_admin,
                          width=300, height=30, corner_radius=10, fg_color="#3B2A7A", hover_color="#4C3A99", text_color="#ffffff",
                          font=ctk.CTkFont("Segoe UI", 10, "bold")).grid(row=2, column=1, sticky="e", padx=16)
        bar = ctk.CTkFrame(hero, fg_color="transparent")
        bar.grid(row=5, column=0, columnspan=2, sticky="ew", padx=16, pady=(8, 8))
        bar.grid_columnconfigure(0, weight=3)
        bar.grid_columnconfigure((1, 2, 3), weight=1, uniform="prof")
        auto = ctk.CTkButton(bar, text="⚡  AUTO OPTIMIZE  ·  alles automatisch", height=60, corner_radius=16,
                             command=lambda: self.tw_auto("competitive"), fg_color=PURPLE, hover_color=PURPLE_HOVER,
                             text_color="#ffffff", border_width=2, border_color=PURPLE_HOVER,
                             font=ctk.CTkFont("Segoe UI", 15, "bold"))
        auto.grid(row=0, column=0, sticky="ew", padx=6)
        fx.hover_fade(auto, {"border_color": (PURPLE_HOVER, CYAN)})
        for i, (label, key) in enumerate(self.TW_PROFILES, start=1):
            b = ctk.CTkButton(bar, text=label, height=60, corner_radius=16, command=lambda k=key: self.tw_auto(k),
                              fg_color="#1E1E33", hover_color="#2A2A44", text_color="#ffffff", border_width=1,
                              border_color=BORDER, font=ctk.CTkFont("Segoe UI", 12, "bold"))
            b.grid(row=0, column=i, sticky="ew", padx=6)
            fx.hover_fade(b, {"border_color": (BORDER, CYAN)})
            Tooltip(b, tweaklib.PROFILES[key])
        ctk.CTkLabel(hero, text="SAFE = nur Basis · COMPETITIVE = alles Empfohlene · MAX FPS = zusätzlich Effekte/Hintergrund/Telemetrie/MSI",
                     text_color=MUTED, anchor="w", font=ctk.CTkFont("Segoe UI", 8)).grid(row=6, column=0, sticky="w", padx=22, pady=(0, 12))

        tools = ctk.CTkFrame(p, fg_color="transparent")
        tools.pack(fill="x", padx=4, pady=(0, 2))
        tools.grid_columnconfigure(0, weight=1)
        ctk.CTkEntry(tools, textvariable=self.tw_query, placeholder_text="🔍  Alle Tweaks durchsuchen …", height=36, corner_radius=12,
                     fg_color=CARD, border_color=BORDER, text_color=TEXT,
                     font=ctk.CTkFont("Segoe UI", 11)).grid(row=0, column=0, sticky="ew", padx=4)
        self.tw_query.trace_add("write", lambda *_: self._tw_schedule_show())
        chips = ctk.CTkFrame(p, fg_color="transparent")
        chips.pack(fill="x", padx=4, pady=(4, 2))
        self.tw_chip_buttons = {}
        names = [self.TW_VIEW_REC, self.TW_VIEW_ACTIVE] + [self.CHIP_NAMES.get(c, c) for c in self._toggle_categories()]
        for i, name in enumerate(names):
            b = ctk.CTkButton(chips, text=name, height=30, corner_radius=15, border_width=1,
                              command=lambda n=name: self.tw_set_view(n), font=ctk.CTkFont("Segoe UI", 10, "bold"))
            b.grid(row=i // 8, column=i % 8, padx=3, pady=3, sticky="ew")
            self.tw_chip_buttons[name] = b
        for c in range(8):
            chips.grid_columnconfigure(c, weight=1, uniform="twchip")
        self._tw_style_chips()
        self.tw_grid = ctk.CTkFrame(p, fg_color="transparent")
        self.tw_grid.pack(fill="x")
        self.tw_grid.grid_columnconfigure((0, 1), weight=1, uniform="tw")
        self.tw_empty = ctk.CTkLabel(self.tw_grid, text="Keine Tweaks für diese Auswahl.", text_color=MUTED)
        self._tw_detect()

    def _tw_style_chips(self):
        for name, b in self.tw_chip_buttons.items():
            on = name == self.tw_view
            b.configure(fg_color=PURPLE_DARK if on else CARD, hover_color="#2A2A44", text_color="#ffffff" if on else "#B6B8D0",
                        border_color=CYAN if on else BORDER)

    def tw_set_view(self, name: str):
        self.tw_view = name
        self.tw_query.set("")
        self._tw_style_chips()
        self._tw_show()

    def _tw_schedule_show(self):
        if self._tw_filter_job is not None:
            try:
                self.after_cancel(self._tw_filter_job)
            except Exception:  # noqa: BLE001
                pass
        self._tw_filter_job = self.after(260, self._tw_show)

    def on_show_tweaks(self):
        self._tw_show()
        if time.time() - self.tw_states_at > 15:
            self.tw_refresh_states()

    def _tw_items(self) -> list:
        q = self.tw_query.get().strip().lower()
        toggles = [t for t in CATALOG if t.kind in tweaklib.TOGGLE_KINDS]
        if q:
            return [t for t in toggles if q in (t.name + " " + t.description + " " + " ".join(t.categories)).lower()][:14]
        if self.tw_view == self.TW_VIEW_REC:
            return [t for t in toggles if t.recommended or t.id in tweaklib.MAXFPS_EXTRA_IDS]
        if self.tw_view == self.TW_VIEW_ACTIVE:
            return [t for t in toggles if self.tw_states.get(t.id, ("",))[0] == tweaklib.STATUS_ACTIVE]
        rev = {v: k for k, v in self.CHIP_NAMES.items()}
        cat = rev.get(self.tw_view, self.tw_view)
        return [t for t in toggles if cat in t.categories]

    def _tw_show(self):
        if not hasattr(self, "tw_grid"):
            return
        self._layout_cards(self.tw_grid, "tw", self._tw_items(), self.tw_empty)

    def _layout_cards(self, container, scope: str, items: list, empty_label=None):
        for (sc, _tid), frame in self.card_cache.items():
            if sc == scope:
                frame.grid_forget()
        if empty_label is not None:
            empty_label.grid_forget()
            if not items:
                empty_label.grid(row=0, column=0, columnspan=2, pady=30)
        for idx, t in enumerate(items):
            card = self._card(container, scope, t)
            card.grid(row=idx // 2, column=idx % 2, sticky="new", padx=6, pady=6)

    def _card(self, container, scope: str, t):
        key = (scope, t.id)
        if key in self.card_cache:
            return self.card_cache[key]
        locked = t.admin and not is_admin()
        card = ctk.CTkFrame(container, fg_color=CARD, corner_radius=14, border_width=1, border_color=BORDER)
        card.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(card, text=t.name, text_color=TEXT, anchor="w", justify="left", wraplength=380,
                     font=ctk.CTkFont("Segoe UI", 12, "bold")).grid(row=0, column=0, sticky="w", padx=(14, 4), pady=(12, 0))
        var = tk.BooleanVar(value=False)
        sw = ctk.CTkSwitch(card, text="", variable=var, onvalue=True, offvalue=False, width=58, switch_width=50, switch_height=26,
                           progress_color=CYAN, button_color="#ffffff", button_hover_color="#E0F7FF", fg_color="#2A2A44",
                           state="disabled" if locked else "normal",
                           command=lambda i=t.id: self.tw_set(i, bool(self._card_var(i).get())))
        sw.grid(row=0, column=1, padx=(4, 12), pady=(10, 0))
        tags = []
        if t.recommended:
            tags.append("★ EMPFOHLEN")
        if t.admin:
            tags.append("🔒 ADMIN")
        cats = " · ".join(self.CHIP_NAMES.get(c, c) for c in t.categories[:3])
        ctk.CTkLabel(card, text="   ".join(tags + [cats.upper()]), text_color=CYAN if t.recommended else MUTED, anchor="w",
                     font=ctk.CTkFont("Segoe UI", 8, "bold")).grid(row=1, column=0, columnspan=2, sticky="w", padx=14)
        ctk.CTkLabel(card, text=t.description + "\nAuswirkung: " + t.impact, text_color="#9EA1BD", anchor="w", justify="left",
                     wraplength=500, font=ctk.CTkFont("Segoe UI", 9)).grid(row=2, column=0, columnspan=2, sticky="w", padx=14, pady=(4, 2))
        status = ctk.CTkLabel(card, text="○ wird geprüft …", text_color=MUTED, anchor="w", justify="left", wraplength=500,
                              font=ctk.CTkFont("Segoe UI", 9, "bold"))
        status.grid(row=3, column=0, columnspan=2, sticky="w", padx=14, pady=(0, 11))
        self.card_refs.setdefault(t.id, []).append((var, status, locked))
        self.card_cache[key] = card
        fx.hover_fade(card, {"fg_color": (CARD, CARD_HOVER), "border_color": (BORDER, CYAN)}, steps=6, delay=16)
        state = self.tw_states.get(t.id)
        if state:
            self._paint_one(t.id, state[0], state[1], only=(var, status, locked))
        return card

    def _card_var(self, tweak_id: str):
        # Der zuletzt bedienbare Schalter dieses Tweaks (jede Seite hat ihre eigene Karte, Werte werden synchron gehalten).
        return self.card_refs[tweak_id][-1][0] if len(self.card_refs.get(tweak_id, [])) == 1 else self._active_var(tweak_id)

    def _active_var(self, tweak_id: str):
        # Bei zwei Karten (Tweaks-Tab + Fortnite-Seite) die Karte der sichtbaren Seite waehlen.
        order = {"Fortnite": 1}.get(self.active_page, 0)
        refs = self.card_refs[tweak_id]
        return refs[min(order, len(refs) - 1)][0]

    def _paint_one(self, tweak_id: str, state: str, msg: str, only=None):
        color = {tweaklib.STATUS_ACTIVE: GREEN, tweaklib.STATUS_PARTIAL: YELLOW}.get(state, MUTED)
        dot = "●" if state != tweaklib.STATUS_UNKNOWN else "○"
        t = CATALOG_BY_ID[tweak_id]
        if t.admin and not is_admin():
            msg = "Als Administrator starten, um diesen Tweak zu nutzen."
        elif state == tweaklib.STATUS_UNKNOWN and not msg:
            msg = "Auf diesem System nicht verfügbar."
        for ref in ([only] if only else self.card_refs.get(tweak_id, [])):
            var, label, _locked = ref
            try:
                var.set(state == tweaklib.STATUS_ACTIVE)
                label.configure(text=f"{dot} {state}" + (f" · {msg}" if msg else ""), text_color=color)
            except Exception:  # noqa: BLE001
                pass

    def _tw_paint(self, tweak_id: str, state: str, msg: str = ""):
        self._paint_one(tweak_id, state, msg)

    def _tw_context(self) -> dict:
        try:
            battery = psutil.sensors_battery() if hasattr(psutil, "sensors_battery") else None
        except Exception:  # noqa: BLE001
            battery = None
        return {"desktop": battery is None, "fortnite_config": tweaklib.fortnite_ini_path().exists(),
                "fortnite_running": bool(self.find_fortnite()), "admin": is_admin()}

    def _tw_detect(self):
        def worker():
            ctx = self._tw_context()
            gpu = (self.gpu_data or {}).get("name", "GPU wird erkannt")
            text = (f"{'Desktop' if ctx['desktop'] else 'Notebook'} · {gpu} · {fmt_bytes(psutil.virtual_memory().total)} RAM · "
                    f"Fortnite-Config {'gefunden' if ctx['fortnite_config'] else 'nicht gefunden'}")
            if self.alive:
                self.after(0, lambda: self.tw_info.configure(text=text))
        threading.Thread(target=worker, daemon=True).start()

    def tw_refresh_states(self):
        """Status aller Schalter im Hintergrund lesen (powercfg kann dauern); Karten werden nur aktualisiert, nie neu gebaut."""
        def worker():
            states = {}
            for t in CATALOG:
                if t.kind in tweaklib.TOGGLE_KINDS:
                    try:
                        states[t.id] = self.engine.status(t)
                    except Exception as exc:  # noqa: BLE001
                        states[t.id] = (tweaklib.STATUS_UNKNOWN, str(exc))
            if self.alive:
                self.after(0, lambda: self._tw_states_done(states))
        threading.Thread(target=worker, daemon=True).start()

    def _tw_states_done(self, states):
        self.tw_states.update(states)
        self.tw_states_at = time.time()
        for tid, (state, msg) in self.tw_states.items():
            if tid in self.card_refs:
                self._paint_one(tid, state, msg)
        if self.tw_view == self.TW_VIEW_ACTIVE:
            self._tw_show()

    def _tw_progress(self, value: float, text: str = ""):
        if not self.alive:
            return

        def apply():
            try:
                self.tw_progress.set(max(0.0, min(1.0, value)))
                if text:
                    self.tw_progress_label.configure(text=text)
            except Exception:  # noqa: BLE001
                pass
        self.after(0, apply)

    def _tw_apply_one(self, t, on: bool):
        """Wendet einen Tweak an oder macht ihn rueckgaengig. Laeuft im Hintergrund-Thread."""
        try:
            if on:
                r = self.engine.apply(t)
                fb = self.FALLBACK.get(t.id)
                if not r.ok and fb:
                    fb_t = CATALOG_BY_ID[fb]
                    r2 = self.engine.apply(fb_t)
                    if r2.ok:
                        return fb_t, tweaklib.Result(True, f"{fb_t.name} (Ersatz, da der gewünschte Plan nicht verfügbar ist)", True)
                return t, r
            if self.engine.has_backup(t.id):
                return t, self.engine.undo(t)
            return t, tweaklib.Result(False, "Kein Backup: von RISEN nicht angewendet, daher nichts zurückzusetzen.")
        except Exception as exc:  # noqa: BLE001
            log(f"tw_apply {t.id}: {exc}")
            return t, tweaklib.Result(False, f"Unerwarteter Fehler: {exc}")

    def tw_set(self, tweak_id: str, on: bool):
        """Schalter = sofort anwenden. Kein Dialog."""
        if self.tw_busy:
            st = self.tw_states.get(tweak_id, (tweaklib.STATUS_UNKNOWN, ""))
            self._paint_one(tweak_id, st[0], st[1])
            return
        t = CATALOG_BY_ID[tweak_id]
        self.tw_busy = True
        self.set_status(f"{'Wende an' if on else 'Setze zurück'}: {t.name} …")

        def worker():
            used, res = self._tw_apply_one(t, on)
            if not self.alive:
                return
            try:
                state = self.engine.status(used)
            except Exception as exc:  # noqa: BLE001
                state = (tweaklib.STATUS_UNKNOWN, str(exc))
            self.after(0, lambda: self._tw_after_set(t, used, res, state))
        threading.Thread(target=worker, daemon=True).start()

    def _tw_after_set(self, t, used, res, state):
        self.tw_busy = False
        self.tw_states[used.id] = state
        self.update_backup_label()
        self._paint_one(used.id, state[0], res.message[:140] if used.id == t.id else state[1])
        if used.id != t.id:
            self._paint_one(t.id, self.tw_states.get(t.id, state)[0], res.message[:140])
        self.set_status(("✔ " if res.ok else "✖ ") + used.name + ": " + res.message[:120])
        if self.tw_view == self.TW_VIEW_ACTIVE:
            self._tw_show()

    def tw_auto(self, profile: str = "competitive"):
        """Ein Klick: System erkennen, passende Tweaks waehlen, anwenden, aufraeumen. Keine Dialoge."""
        if self.tw_busy:
            return
        self.ensure_built("Tweaks")
        self.tw_busy = True
        self._tw_progress(0.02, "Analysiere dein System …")

        def worker():
            ctx = self._tw_context()
            chosen, skipped = tweaklib.select_for_profile(CATALOG, self.engine, ctx, profile)
            chosen = sorted(chosen, key=lambda x: 0 if x.kind == "power" else 1)
            steps = len(chosen) + 2
            results = []
            if is_admin() and chosen:
                self._tw_progress(0.05, "Erstelle Wiederherstellungspunkt …")
                try:
                    run_ps("Checkpoint-Computer -Description 'RISEN TWEAKS Auto' -RestorePointType MODIFY_SETTINGS", 90)
                except Exception as exc:  # noqa: BLE001
                    log(f"restore point: {exc}")
            for i, t in enumerate(chosen):
                self._tw_progress((i + 1) / steps, f"Wende an: {t.name}  ({i + 1}/{len(chosen)})")
                results.append(self._tw_apply_one(t, True))
            self._tw_progress((steps - 1) / steps, "Räume auf: Temp-Dateien, DNS-Cache …")
            cleaned = self._tw_quiet_cleanup()
            states = {}
            for t in CATALOG:
                if t.kind in tweaklib.TOGGLE_KINDS:
                    try:
                        states[t.id] = self.engine.status(t)
                    except Exception:  # noqa: BLE001
                        pass
            self._tw_progress(1.0, "Fertig.")
            if self.alive:
                self.after(0, lambda: self._tw_auto_done(profile, results, skipped, cleaned, states))
        threading.Thread(target=worker, daemon=True).start()

    def _tw_quiet_cleanup(self) -> str:
        temp = Path(os.environ.get("TEMP", str(Path.home() / "AppData/Local/Temp")))
        deleted = skipped = 0
        if temp.is_dir():
            for item in temp.iterdir():
                try:
                    if item.is_symlink():
                        skipped += 1
                    elif item.is_dir():
                        shutil.rmtree(item)
                        deleted += 1
                    else:
                        item.unlink()
                        deleted += 1
                except Exception:  # noqa: BLE001
                    skipped += 1
        ok, _ = netlab.flush_dns()
        return f"Temp: {deleted} gelöscht, {skipped} übersprungen · DNS-Cache {'geleert' if ok else 'nicht geleert'}"

    def _tw_auto_done(self, profile, results, skipped, cleaned, states):
        self.tw_busy = False
        self._tw_states_done(states)
        self._tw_show()
        self.update_backup_label()
        ok = [r for r in results if r[1].ok]
        bad = [r for r in results if not r[1].ok]
        if not results:
            summary = "Alles Empfohlene ist bereits aktiv oder auf diesem System nicht anwendbar."
        else:
            summary = f"{len(ok)} Tweaks angewendet" + (f" · {len(bad)} fehlgeschlagen" if bad else "")
        restart = [t.name for t, r in ok if t.restart and t.restart != "Nein" and "Neustart" in t.restart]
        hint = f" · Neustart empfohlen ({len(restart)} Tweaks wirken erst danach)" if restart else ""
        skipped_admin = [t for t, why in skipped if "Administrator" in why]
        if skipped_admin:
            hint += f" · {len(skipped_admin)} Tweaks übersprungen: als Administrator starten"
        self.tw_progress_label.configure(text=f"{summary} · {cleaned}{hint}")
        self.set_status(f"AUTO ({profile.upper()}): {summary}")
        if bad:
            messagebox.showwarning(APP_NAME, "Nicht alles ließ sich anwenden:\n\n" + "\n".join(f"✖ {t.name}: {r.message}" for t, r in bad))

    def tw_undo_all(self):
        if self.tw_busy:
            return
        if not messagebox.askyesno(APP_NAME, "Alle von RISEN gesetzten Tweaks auf den gesicherten Originalzustand zurücksetzen?"):
            return
        self.tw_busy = True
        self._tw_progress(0.1, "Setze alles zurück …")

        def worker():
            done, failed = self.engine.undo_all(CATALOG)
            self._tw_progress(1.0, f"Zurückgesetzt: {done} · Fehler: {failed}")
            if self.alive:
                def finish():
                    self.tw_busy = False
                    self.tw_refresh_states()
                    self.update_backup_label()
                self.after(0, finish)
        threading.Thread(target=worker, daemon=True).start()

    def relaunch_as_admin(self):
        if not IS_WINDOWS:
            return
        try:
            import ctypes
            if getattr(sys, "frozen", False):
                exe, params = sys.executable, ""
            else:
                exe, params = sys.executable, f'"{Path(__file__).resolve()}"'
            rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, None, 1)
            if rc > 32:
                self.on_close()
            else:
                self.set_status("Administrator-Neustart abgebrochen.")
        except Exception as exc:  # noqa: BLE001
            log(f"relaunch_as_admin: {exc}")
            self.set_status("Administrator-Neustart nicht möglich.")

    def _build_home_page(self):
        p = self.pages["Home"]
        self.add_page_heading(p, "System Overview", "Live-Überblick deines PCs · aktualisiert alle 2 Sekunden")
        banner = ctk.CTkFrame(p, fg_color=PANEL, corner_radius=18, border_width=1, border_color=BORDER)
        banner.pack(fill="x", padx=7, pady=(0, 8))
        banner.grid_columnconfigure(0, weight=1)
        self.home_headline = ctk.CTkLabel(banner, text="RISEN TWEAKS", text_color=TEXT, anchor="w", font=ctk.CTkFont("Segoe UI", 20, "bold"))
        self.home_headline.grid(row=0, column=0, sticky="w", padx=20, pady=(14, 0))
        self.home_system_label = ctk.CTkLabel(banner, text=f"OS: {platform.platform()}\nCPU: {platform.processor() or 'Erkennung läuft'}\nRAM: {fmt_bytes(psutil.virtual_memory().total)}",
                                              text_color=MUTED, anchor="w", justify="left", font=ctk.CTkFont("Segoe UI", 10))
        self.home_system_label.grid(row=1, column=0, sticky="w", padx=20, pady=(2, 4))
        self.home_status = ctk.CTkLabel(banner, text="", text_color=CYAN, anchor="w", font=ctk.CTkFont("Segoe UI", 10, "bold"))
        self.home_status.grid(row=2, column=0, sticky="w", padx=20, pady=(0, 12))
        quick = ctk.CTkFrame(banner, fg_color="transparent")
        quick.grid(row=0, column=1, rowspan=3, padx=16, pady=12)
        for text, cmd, accent in (("⚡ AUTO OPTIMIZE", self.home_auto, True), ("Fortnite", lambda: self.show_page("Fortnite"), False),
                                  ("Tweaks", lambda: self.show_page("Tweaks"), False)):
            b = ctk.CTkButton(quick, text=text, command=cmd, width=150, height=40, corner_radius=12, border_width=1,
                              fg_color=PURPLE if accent else "#1E1E33", hover_color=PURPLE_HOVER if accent else "#2A2A44",
                              border_color=PURPLE_HOVER if accent else BORDER, font=ctk.CTkFont("Segoe UI", 11, "bold"))
            b.pack(side="left", padx=4)
            fx.hover_fade(b, {"border_color": (PURPLE_HOVER if accent else BORDER, CYAN)}, steps=6)
        row = ctk.CTkFrame(p, fg_color="transparent")
        row.pack(fill="x")
        row.grid_columnconfigure((0, 1, 2, 3), weight=1, uniform="gauge")
        self.gauges = {}
        for idx, (key, title, c1, c2) in enumerate((("cpu", "CPU", "#8B5CF6", "#22D3EE"), ("gpu", "GPU", "#22D3EE", "#34D399"),
                                                    ("ram", "RAM", "#F472B6", "#8B5CF6"), ("disk", "SYSTEMLAUFWERK", "#FBBF24", "#F472B6"))):
            card = ctk.CTkFrame(row, fg_color=PANEL, corner_radius=16, border_width=1, border_color=BORDER)
            card.grid(row=0, column=idx, padx=6, pady=4, sticky="nsew")
            ctk.CTkLabel(card, text=title, text_color=MUTED, font=ctk.CTkFont("Segoe UI", 9, "bold")).pack(pady=(12, 0))
            g = ui_widgets.RingGauge(card, size=150, color=c1, color2=c2, bg=PANEL)
            g.pack(pady=(4, 2))
            lab = ctk.CTkLabel(card, text="…", text_color="#9EA1BD", font=ctk.CTkFont("Segoe UI", 9))
            lab.pack(pady=(0, 12))
            self.gauges[key] = (g, lab)
            fx.hover_fade(card, {"border_color": (BORDER, c2)}, steps=6)
        lower = ctk.CTkFrame(p, fg_color="transparent")
        lower.pack(fill="x", pady=(4, 0))
        lower.grid_columnconfigure(0, weight=3, uniform="low")
        lower.grid_columnconfigure(1, weight=2, uniform="low")
        left = ctk.CTkFrame(lower, fg_color=PANEL, corner_radius=16, border_width=1, border_color=BORDER)
        left.grid(row=0, column=0, padx=6, pady=4, sticky="nsew")
        ctk.CTkLabel(left, text="LIVE-VERLAUF · LETZTE 2 MINUTEN", text_color=MUTED, anchor="w",
                     font=ctk.CTkFont("Segoe UI", 9, "bold")).pack(fill="x", padx=14, pady=(10, 2))
        self.sparks = {}
        for key, label, color in (("cpu", "CPU", "#8B5CF6"), ("gpu", "GPU", "#22D3EE"), ("ram", "RAM", "#F472B6")):
            sp = ui_widgets.Sparkline(left, height=78, color=color, bg=PANEL, label=label)
            sp.pack(fill="x", padx=12, pady=(2, 6))
            self.sparks[key] = sp
        right = ctk.CTkFrame(lower, fg_color=PANEL, corner_radius=16, border_width=1, border_color=BORDER)
        right.grid(row=0, column=1, padx=6, pady=4, sticky="nsew")
        ctk.CTkLabel(right, text="NETZWERK & SENSOREN", text_color=MUTED, anchor="w",
                     font=ctk.CTkFont("Segoe UI", 9, "bold")).pack(fill="x", padx=14, pady=(10, 2))
        for key, label, color in (("down", "↓ Download", "#34D399"), ("up", "↑ Upload", "#FBBF24")):
            sp = ui_widgets.Sparkline(right, height=78, color=color, bg=PANEL, label=label, unit=" MB/s", vmax=None)
            sp.pack(fill="x", padx=12, pady=(2, 6))
            self.sparks[key] = sp
        self.home_sensors = ctk.CTkLabel(right, text="", text_color="#B6B8D0", anchor="w", justify="left", font=ctk.CTkFont("Segoe UI", 10))
        self.home_sensors.pack(fill="x", padx=14, pady=(2, 12))

    def home_auto(self):
        self.show_page("Tweaks")
        self.after(200, lambda: self.tw_auto("competitive"))

    def on_show_home(self):
        for sp in getattr(self, "sparks", {}).values():
            sp.redraw()
        self.refresh_live()

    def _build_fortnite_page(self):
        p = self.pages["Fortnite"]
        self.add_page_heading(p, "Fortnite Optimizer", "Schreibt echte Werte in deine GameUserSettings.ini (mit Kopie und Prüfung) · Fortnite muss dafür beendet sein")
        self.fortnite_state = ctk.CTkLabel(p, text="Fortnite-Prozess wird gesucht …", fg_color="#12121F", corner_radius=10, text_color="#d4d4d4", anchor="w", height=39)
        self.fortnite_state.pack(fill="x", padx=7, pady=(0, 6))
        hero = ctk.CTkFrame(p, fg_color=PANEL, corner_radius=18, border_width=1, border_color=BORDER)
        hero.pack(fill="x", padx=7, pady=(0, 8))
        hero.grid_columnconfigure(0, weight=1)
        self.fn_cfg_label = ctk.CTkLabel(hero, text="Config wird geprüft …", text_color=CYAN, anchor="w", justify="left", wraplength=900,
                                         font=ctk.CTkFont("Segoe UI", 11, "bold"))
        self.fn_cfg_label.grid(row=0, column=0, sticky="w", padx=20, pady=(14, 2))
        ctk.CTkLabel(hero, text="Ein Klick optimiert alles: beendet Fortnite (nach Rückfrage), setzt Grafik auf Leistung, Vollbild, ohne Bewegungsunschärfe, FPS-Limit 240, V-Sync aus "
                                "und die Vollbild-Optimierung. Danach Fortnite normal starten. Die Priorität HOCH setzt RISEN beim Spielstart automatisch.",
                     text_color=MUTED, anchor="w", justify="left", wraplength=900, font=ctk.CTkFont("Segoe UI", 10)).grid(row=1, column=0, sticky="w", padx=20)
        row = ctk.CTkFrame(hero, fg_color="transparent")
        row.grid(row=2, column=0, sticky="ew", padx=14, pady=(10, 6))
        row.grid_columnconfigure(0, weight=3)
        row.grid_columnconfigure((1, 2), weight=1, uniform="fb")
        big = ctk.CTkButton(row, text="⚡  FORTNITE OPTIMIEREN  ·  alles automatisch", height=58, corner_radius=16, command=self.fn_optimize,
                            fg_color=PURPLE, hover_color=PURPLE_HOVER, border_width=2, border_color=PURPLE_HOVER, text_color="#ffffff",
                            font=ctk.CTkFont("Segoe UI", 14, "bold"))
        big.grid(row=0, column=0, sticky="ew", padx=6)
        fx.hover_fade(big, {"border_color": (PURPLE_HOVER, CYAN)})
        for i, (text, cmd) in enumerate((("Fortnite beenden", self.fn_close), ("Config-Ordner", self.open_fortnite_config)), start=1):
            b = ctk.CTkButton(row, text=text, height=58, corner_radius=16, command=cmd, fg_color="#1E1E33", hover_color="#2A2A44", border_width=1,
                              border_color=BORDER, font=ctk.CTkFont("Segoe UI", 11, "bold"))
            b.grid(row=0, column=i, sticky="ew", padx=6)
            fx.hover_fade(b, {"border_color": (BORDER, CYAN)})
        self.fn_auto_var = tk.BooleanVar(value=self.fn_auto_high)
        ctk.CTkSwitch(hero, text="Automatisch: Fortnite-Priorität HOCH, sobald das Spiel läuft", variable=self.fn_auto_var, progress_color=CYAN,
                      command=lambda: setattr(self, "fn_auto_high", bool(self.fn_auto_var.get())),
                      font=ctk.CTkFont("Segoe UI", 10)).grid(row=3, column=0, sticky="w", padx=20, pady=(2, 14))
        ctk.CTkLabel(p, text="FORTNITE-TWEAKS · jeder Schalter wirkt sofort", text_color=MUTED, anchor="w",
                     font=ctk.CTkFont("Segoe UI", 9, "bold")).pack(fill="x", padx=12, pady=(2, 0))
        self.fn_grid = ctk.CTkFrame(p, fg_color="transparent")
        self.fn_grid.pack(fill="x")
        self.fn_grid.grid_columnconfigure((0, 1), weight=1, uniform="fnc")
        grid = ctk.CTkFrame(p, fg_color="transparent")
        grid.pack(fill="x", pady=(8, 0))
        grid.grid_columnconfigure((0, 1), weight=1, uniform="fn")
        network = self.group(grid, "NETWORK & LATENCY LAB", 0, 0)
        self.network_result = ctk.CTkLabel(network, text="Noch kein Netzwerktest ausgeführt.", text_color="#bdbdbd", anchor="w", justify="left", font=ctk.CTkFont("Segoe UI", 9))
        self.network_result.pack(fill="x", padx=8, pady=(4, 7))
        self.action_card(network, "Cloudflare Ping-Test", "Misst 5 ICMP-Pings zu 1.1.1.1 (Durchschnitt, Schwankung, Paketverlust). Das ist NICHT dein Fortnite-Server-Ping.", "Ping-Test starten", lambda: self.run_ping_test("1.1.1.1", "Cloudflare"), accent=True)
        self.action_card(network, "Netzwerkadapter anzeigen", "Zeigt aktive Adapter, Link-Speed und Verbindungsstatus.", "Adapter-Status anzeigen", self.show_network_adapters)
        tools = self.group(grid, "WEITERE TOOLS", 0, 1)
        self.action_card(tools, "Priorität NORMAL", "Stellt die Standard-Priorität des laufenden Fortnite-Prozesses wieder her.", "Auf NORMAL zurücksetzen", self.set_fortnite_normal)
        self.action_card(tools, "Epic Games Launcher", "Öffnet den Epic Games Launcher, falls installiert.", "Epic Games öffnen", self.open_epic)
        self._build_fortnite_live(grid)

    def on_show_fortnite(self):
        ini = tweaklib.fortnite_ini_path()
        if ini.exists():
            self.fn_cfg_label.configure(text=f"✔ Fortnite-Config gefunden: {ini}", text_color=GREEN)
        else:
            self.fn_cfg_label.configure(text="✖ Keine GameUserSettings.ini gefunden. Starte Fortnite einmal bis ins Menü und beende es wieder, dann hier optimieren.", text_color="#d1b26b")
        items = [t for t in tweaklib.by_category(CATALOG, "Fortnite Optimization") if t.kind in tweaklib.TOGGLE_KINDS]
        self._layout_cards(self.fn_grid, "fn", items)
        self.refresh_live()
        if time.time() - self.tw_states_at > 15:
            self.tw_refresh_states()

    def _fn_terminate(self) -> int:
        procs = self._scan_fortnite()
        for pr in procs:
            try:
                pr.terminate()
            except Exception as exc:  # noqa: BLE001
                log(f"fortnite terminate: {exc}")
        if procs:
            gone, alive = psutil.wait_procs(procs, timeout=8)
            for pr in alive:
                try:
                    pr.kill()
                except Exception:  # noqa: BLE001
                    pass
            psutil.wait_procs(alive, timeout=3)
        self._fn_cache = ([], 0.0)
        return len(procs)

    def fn_close(self):
        if not self.find_fortnite():
            messagebox.showinfo(APP_NAME, "Fortnite läuft gerade nicht.")
            return
        if messagebox.askyesno(APP_NAME, "Fortnite jetzt beenden? Nicht gespeicherter Fortschritt im Match geht verloren."):
            threading.Thread(target=lambda: (self._fn_terminate(), self.after(0, lambda: self.set_status("Fortnite beendet."))), daemon=True).start()

    def fn_optimize(self):
        if self.tw_busy:
            return
        if not tweaklib.fortnite_ini_path().exists():
            messagebox.showinfo(APP_NAME, "Keine GameUserSettings.ini gefunden. Starte Fortnite einmal bis ins Hauptmenü, beende es und klicke dann erneut.")
            return
        if self.find_fortnite():
            if not messagebox.askyesno(APP_NAME, "Fortnite läuft und muss für Config-Änderungen beendet werden (sonst überschreibt es die Datei beim Beenden).\n\nJetzt beenden und optimieren?"):
                return
        self.tw_busy = True
        self.fn_cfg_label.configure(text="Optimiere Fortnite …", text_color=CYAN)

        def worker():
            self._fn_terminate()
            items = [t for t in tweaklib.by_category(CATALOG, "Fortnite Optimization")
                     if t.kind in tweaklib.TOGGLE_KINDS and (t.kind == "ini" or t.recommended) and not (t.admin and not is_admin())]
            results = []
            for t in items:
                try:
                    if self.engine.status(t)[0] == tweaklib.STATUS_ACTIVE:
                        continue
                except Exception:  # noqa: BLE001
                    pass
                results.append(self._tw_apply_one(t, True))
            states = {}
            for t in CATALOG:
                if t.kind in tweaklib.TOGGLE_KINDS:
                    try:
                        states[t.id] = self.engine.status(t)
                    except Exception:  # noqa: BLE001
                        pass
            if self.alive:
                self.after(0, lambda: self._fn_optimize_done(results, states))
        threading.Thread(target=worker, daemon=True).start()

    def _fn_optimize_done(self, results, states):
        self.tw_busy = False
        self._tw_states_done(states)
        self.update_backup_label()
        ok = [t for t, r in results if r.ok]
        bad = [(t, r) for t, r in results if not r.ok]
        if not results:
            text = "✔ Alles war schon optimiert. Starte Fortnite jetzt normal."
        else:
            text = f"✔ {len(ok)} Einstellungen geschrieben" + (f" · ✖ {len(bad)} fehlgeschlagen" if bad else "") + " · Starte Fortnite jetzt normal."
        self.fn_cfg_label.configure(text=text, text_color=GREEN if not bad else "#d1b26b")
        self.set_status(text)
        if bad:
            messagebox.showwarning(APP_NAME, "Nicht alles ließ sich anwenden:\n\n" + "\n".join(f"✖ {t.name}: {r.message}" for t, r in bad))

    def _build_process_page(self):
        p = self.pages["Processes"]
        self.add_page_heading(p, "Process Manager", "Live-Prozessliste mit Last-Heatmap · Priorität ändern oder Prozesse beenden")
        self.proc_filter = "Alle"
        tiles = ctk.CTkFrame(p, fg_color="transparent")
        tiles.pack(fill="x")
        tiles.grid_columnconfigure((0, 1, 2, 3), weight=1, uniform="pt")
        self.proc_tiles = {}
        for i, (key, title, color) in enumerate((("count", "PROZESSE", "#8B5CF6"), ("cpu", "CPU GESAMT", "#22D3EE"),
                                                 ("ram", "RAM BELEGT", "#F472B6"), ("top", "TOP-VERBRAUCHER", "#FBBF24"))):
            tile = ctk.CTkFrame(tiles, fg_color=PANEL, corner_radius=14, border_width=1, border_color=BORDER)
            tile.grid(row=0, column=i, padx=6, pady=4, sticky="nsew")
            ctk.CTkLabel(tile, text=title, text_color=MUTED, anchor="w", font=ctk.CTkFont("Segoe UI", 9, "bold")).pack(fill="x", padx=14, pady=(10, 0))
            val = ctk.CTkLabel(tile, text="…", text_color=color, anchor="w", font=ctk.CTkFont("Segoe UI", 20, "bold"))
            val.pack(fill="x", padx=14)
            det = ctk.CTkLabel(tile, text="", text_color="#9EA1BD", anchor="w", font=ctk.CTkFont("Segoe UI", 9))
            det.pack(fill="x", padx=14, pady=(0, 10))
            self.proc_tiles[key] = (val, det)
            fx.hover_fade(tile, {"border_color": (BORDER, color)}, steps=6)
        toolbar = ctk.CTkFrame(p, fg_color="transparent")
        toolbar.pack(fill="x", padx=4, pady=(4, 4))
        self.process_search = ctk.CTkEntry(toolbar, placeholder_text="🔍  Prozess suchen …", width=260, height=34, corner_radius=12,
                                           fg_color=CARD, border_color=BORDER)
        self.process_search.pack(side="left", padx=(2, 8))
        self._proc_search_job = None
        self.process_search.bind("<KeyRelease>", lambda _e: self._proc_debounce())
        self.proc_chip_buttons = {}
        for name in ("Alle", "Hohe Last", "Fortnite/Epic"):
            b = ctk.CTkButton(toolbar, text=name, width=105, height=32, corner_radius=16, border_width=1,
                              command=lambda n=name: self.set_proc_filter(n), font=ctk.CTkFont("Segoe UI", 10, "bold"))
            b.pack(side="left", padx=3)
            self.proc_chip_buttons[name] = b
        for label, key in (("CPU ↓", "cpu"), ("RAM ↓", "memory"), ("Name", "name")):
            ctk.CTkButton(toolbar, text=label, width=70, height=32, corner_radius=16, fg_color="#1E1E33", hover_color="#2A2A44",
                          command=lambda k=key: self.set_process_sort(k), font=ctk.CTkFont("Segoe UI", 10, "bold")).pack(side="right", padx=3)
        self.process_count = ctk.CTkLabel(toolbar, text="", text_color=MUTED, font=ctk.CTkFont("Segoe UI", 9))
        self.process_count.pack(side="right", padx=8)
        self._style_proc_chips()
        table_frame = ctk.CTkFrame(p, fg_color=PANEL, corner_radius=14, border_width=1, border_color=BORDER)
        table_frame.pack(fill="both", expand=True, padx=7, pady=(2, 8))
        cols = ("name", "pid", "cpu", "memory", "threads")
        self.process_tree = ttk.Treeview(table_frame, columns=cols, show="headings", selectmode="browse", height=16)
        for col, label, width, anchor in (("name", "PROZESS", 330, "w"), ("pid", "PID", 70, "center"), ("cpu", "CPU %", 190, "w"),
                                          ("memory", "RAM", 110, "center"), ("threads", "THREADS", 80, "center")):
            self.process_tree.heading(col, text=label)
            self.process_tree.column(col, width=width, anchor=anchor)
        self.process_tree.tag_configure("hot", foreground="#FB7185")
        self.process_tree.tag_configure("warm", foreground="#FBBF24")
        self.process_tree.tag_configure("fn", foreground="#22D3EE")
        self.process_tree.pack(fill="both", expand=True, padx=7, pady=7)
        footer = ctk.CTkFrame(p, fg_color="transparent")
        footer.pack(fill="x", padx=7, pady=(0, 6))
        ctk.CTkButton(footer, text="✖  Beenden", command=self.end_selected_process, fg_color="#7a3036", hover_color="#983d45", width=130,
                      font=ctk.CTkFont("Segoe UI", 10, "bold")).pack(side="left", padx=(0, 10))
        for label, level in (("Priorität HOCH", "high"), ("Normal", "normal"), ("Niedrig", "low")):
            ctk.CTkButton(footer, text=label, command=lambda l=level: self.set_selected_priority(l), width=120, fg_color="#1E1E33",
                          hover_color="#2A2A44", font=ctk.CTkFont("Segoe UI", 10, "bold")).pack(side="left", padx=3)
        ctk.CTkLabel(footer, text="Rechtsklick nicht nötig: Zeile wählen, dann Aktion. Systemprozesse bleiben geschützt.", text_color=MUTED,
                     font=ctk.CTkFont("Segoe UI", 9)).pack(side="left", padx=12)

    def _style_proc_chips(self):
        for name, b in self.proc_chip_buttons.items():
            on = name == self.proc_filter
            b.configure(fg_color=PURPLE_DARK if on else CARD, hover_color="#2A2A44", text_color="#ffffff" if on else "#B6B8D0",
                        border_color=CYAN if on else BORDER)

    def set_proc_filter(self, name: str):
        self.proc_filter = name
        self._style_proc_chips()
        self.refresh_processes()

    def _proc_debounce(self):
        if self._proc_search_job is not None:
            try:
                self.after_cancel(self._proc_search_job)
            except Exception:  # noqa: BLE001
                pass
        self._proc_search_job = self.after(280, self.refresh_processes)

    def set_process_sort(self, key):
        self.sort_by = key
        self.refresh_processes()

    def refresh_processes(self):
        """Prozessliste im Hintergrund einsammeln, Treeview nur im UI-Thread aktualisieren."""
        if not self.alive or not hasattr(self, "process_tree") or self._proc_busy:
            return
        self._proc_busy = True
        ncpu = psutil.cpu_count() or 1

        def work():
            rows = []
            for proc in psutil.process_iter(["pid", "name", "memory_info", "num_threads"]):
                try:
                    info = proc.info
                    mem = info.get("memory_info")
                    rows.append((info.get("name") or "?", int(info["pid"]), proc.cpu_percent(None) / ncpu,
                                 (mem.rss / 1048576) if mem else 0.0, info.get("num_threads") or 0))
                except (psutil.NoSuchProcess, psutil.AccessDenied, ProcessLookupError):
                    continue
                except Exception:  # noqa: BLE001
                    continue
            stats = {"cpu": psutil.cpu_percent(None), "ram": psutil.virtual_memory()}
            if self.alive:
                self.after(0, lambda: self._proc_apply(rows, stats))
            else:
                self._proc_busy = False
        threading.Thread(target=work, daemon=True).start()

    def _proc_apply(self, rows, stats):
        self._proc_busy = False
        try:
            query = self.process_search.get().strip().lower()
            total = len(rows)
            top = max(rows, key=lambda r: r[2], default=None)
            mode = self.proc_filter
            shown = []
            for r in rows:
                name_l = r[0].lower()
                if query and query not in name_l:
                    continue
                if mode == "Hohe Last" and not (r[2] >= 2.0 or r[3] >= 500):
                    continue
                if mode == "Fortnite/Epic" and not any(x in name_l for x in ("fortnite", "epic", "easyanticheat", "eac")):
                    continue
                shown.append(r)
            key = {"cpu": lambda r: r[2], "memory": lambda r: r[3], "name": lambda r: r[0].lower()}.get(self.sort_by, lambda r: r[2])
            shown.sort(key=key, reverse=self.sort_by != "name")
            shown = shown[:150]
            wanted = []
            for name, pid, cpu, mem, threads in shown:
                iid = str(pid)
                wanted.append(iid)
                bars = "▮" * min(10, int(cpu / 3)) if cpu >= 1 else ""
                tag = ("fn",) if "fortnite" in name.lower() else ("hot",) if cpu >= 15 else ("warm",) if cpu >= 4 else ()
                vals = (name, pid, f"{cpu:5.1f}  {bars}", f"{mem:,.0f} MB".replace(",", "."), threads)
                if self.process_tree.exists(iid):
                    self.process_tree.item(iid, values=vals, tags=tag)
                else:
                    self.process_tree.insert("", "end", iid=iid, values=vals, tags=tag)
            keep = set(wanted)
            for iid in self.process_tree.get_children():
                if iid not in keep:
                    self.process_tree.delete(iid)
            for idx, iid in enumerate(wanted):
                self.process_tree.move(iid, "", idx)
            self.process_count.configure(text=f"{len(shown)} von {total} angezeigt")
            ram = stats["ram"]
            self.proc_tiles["count"][0].configure(text=str(total))
            self.proc_tiles["count"][1].configure(text="laufende Prozesse")
            self.proc_tiles["cpu"][0].configure(text=f"{stats['cpu']:.0f}%")
            self.proc_tiles["cpu"][1].configure(text=f"{psutil.cpu_count(logical=False) or '?'} Kerne / {psutil.cpu_count() or '?'} Threads")
            self.proc_tiles["ram"][0].configure(text=f"{ram.percent:.0f}%")
            self.proc_tiles["ram"][1].configure(text=f"{fmt_bytes(ram.used)} von {fmt_bytes(ram.total)}")
            if top:
                self.proc_tiles["top"][0].configure(text=top[0][:18])
                self.proc_tiles["top"][1].configure(text=f"{top[2]:.1f}% CPU · {top[3]:.0f} MB")
        except Exception as exc:  # noqa: BLE001
            log(f"_proc_apply: {exc}")

    def set_selected_priority(self, level: str):
        sel = self.process_tree.selection()
        if not sel:
            messagebox.showinfo(APP_NAME, "Wähle zuerst einen Prozess in der Liste.")
            return
        classes = {"high": getattr(psutil, "HIGH_PRIORITY_CLASS", None), "normal": getattr(psutil, "NORMAL_PRIORITY_CLASS", None),
                   "low": getattr(psutil, "IDLE_PRIORITY_CLASS", None)}
        target = classes.get(level)
        if target is None:
            messagebox.showinfo(APP_NAME, "Prioritätsklassen gibt es nur unter Windows.")
            return
        try:
            proc = psutil.Process(int(sel[0]))
            proc.nice(target)
            self.set_status(f"Priorität von {proc.name()} auf {level.upper()} gesetzt.")
        except Exception as exc:  # noqa: BLE001
            messagebox.showwarning(APP_NAME, f"Priorität konnte nicht geändert werden (evtl. Administrator nötig):\n{exc}")

    def _scan_fortnite(self):
        found = []
        for proc in psutil.process_iter(["name"]):
            try:
                if (proc.info.get("name") or "").lower() == "fortniteclient-win64-shipping.exe":
                    found.append(proc)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        self._fn_cache = (found, time.time())
        return found

    def find_fortnite(self):
        """Gecachte Prozesssuche (max. 3 s alt), damit die Oberflaeche nicht ruckelt."""
        found, ts = self._fn_cache
        if time.time() - ts < 3.0:
            return found
        return self._scan_fortnite()

    def _collect_live(self) -> dict:
        """Laeuft im Hintergrund-Thread. Fasst KEINE Widgets an."""
        d: dict = {}
        d["cpu"] = psutil.cpu_percent(interval=None)
        mem = psutil.virtual_memory()
        d.update(ram=mem.percent, ram_used=mem.used, ram_total=mem.total)
        try:
            root = Path.home().anchor or "C:\\"
            usage = psutil.disk_usage(root)
            d.update(disk=usage.percent, disk_free=usage.free)
        except Exception:  # noqa: BLE001
            d.update(disk=None, disk_free=0)
        now = time.time()
        net = psutil.net_io_counters()
        dt = max(0.001, now - self.last_net_time)
        d["down"] = max(0, net.bytes_recv - self.last_net.bytes_recv) / dt
        d["up"] = max(0, net.bytes_sent - self.last_net.bytes_sent) / dt
        self.last_net, self.last_net_time = net, now
        disk = psutil.disk_io_counters()
        dt_disk = max(0.001, now - self.last_disk_time)
        if disk and self.last_disk:
            self.disk_rates = (max(0, disk.read_bytes - self.last_disk.read_bytes) / dt_disk,
                               max(0, disk.write_bytes - self.last_disk.write_bytes) / dt_disk)
        self.last_disk, self.last_disk_time = disk, now
        d["gpu"] = dict(self.gpu_data or {})
        d["uptime"] = max(0.0, now - psutil.boot_time())
        procs = self._scan_fortnite()
        fn = None
        if procs:
            high = getattr(psutil, "HIGH_PRIORITY_CLASS", None)
            pr = procs[0]
            try:
                fn = {"pid": pr.pid, "cpu": pr.cpu_percent(None) / (psutil.cpu_count() or 1),
                      "ram_mb": pr.memory_info().rss / 1024 / 1024, "high": high is not None and pr.nice() == high, "count": len(procs)}
                if self.fn_auto_high and high is not None:
                    for proc in procs:
                        try:
                            if proc.nice() != high:
                                proc.nice(high)
                                fn["high"] = True
                                fn["boosted"] = True
                        except Exception:  # noqa: BLE001
                            pass
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                fn = {"pid": pr.pid, "cpu": 0.0, "ram_mb": 0.0, "high": False, "count": len(procs)}
        d["fn"] = fn
        return d

    def refresh_live(self):
        if not self.alive or self._live_busy:
            return
        self._live_busy = True

        def work():
            data = None
            try:
                data = self._collect_live()
            except Exception as exc:  # noqa: BLE001
                log(f"Live refresh: {exc}")
            if self.alive:
                self.after(0, lambda: self._live_done(data))
        threading.Thread(target=work, daemon=True).start()

    def _live_done(self, data):
        self._live_busy = False
        if data:
            try:
                self.apply_live(data)
            except Exception as exc:  # noqa: BLE001
                log(f"apply_live: {exc}")

    def apply_live(self, d: dict):
        gpu = d.get("gpu") or {}
        gpu_usage = gpu.get("usage")
        for key, val in (("cpu", d["cpu"]), ("gpu", gpu_usage or 0), ("ram", d["ram"])):
            if key in getattr(self, "sparks", {}):
                self.sparks[key].push(val)
        if "down" in getattr(self, "sparks", {}):
            self.sparks["down"].push(d["down"] / 1e6)
            self.sparks["up"].push(d["up"] / 1e6)
        page = self.active_page
        if page == "Home" and hasattr(self, "gauges"):
            g = self.gauges
            g["cpu"][0].set(d["cpu"] / 100, f"{d['cpu']:.0f}%", f"{psutil.cpu_count(logical=False) or '?'} Kerne · {psutil.cpu_count() or '?'} Threads")
            g["cpu"][1].configure(text="Prozessor-Auslastung")
            g["gpu"][0].set((gpu_usage or 0) / 100 if gpu_usage is not None else None, f"{gpu_usage:.0f}%" if gpu_usage is not None else "N/A",
                            f"{gpu.get('temp')} °C" if gpu.get("temp") is not None else "")
            g["gpu"][1].configure(text=safe_gpu_detail(gpu).split("\n")[0] if gpu_usage is not None else "GPU-Sensor nicht verfügbar")
            g["ram"][0].set(d["ram"] / 100, f"{d['ram']:.0f}%", fmt_bytes(d["ram_used"]))
            g["ram"][1].configure(text=f"von {fmt_bytes(d['ram_total'])}")
            if d.get("disk") is not None:
                g["disk"][0].set(d["disk"] / 100, f"{d['disk']:.0f}%", "belegt")
                g["disk"][1].configure(text=f"{fmt_bytes(d['disk_free'])} frei")
            for sp in self.sparks.values():
                sp.redraw()
            h = int(d["uptime"] // 3600)
            m = int(d["uptime"] % 3600 // 60)
            sensors = [f"Laufzeit seit Start: {h} h {m} min", f"Datenträger: R {fmt_bytes(self.disk_rates[0])}/s · W {fmt_bytes(self.disk_rates[1])}/s",
                       f"GPU-Temperatur: {gpu.get('temp')} °C" if gpu.get("temp") is not None else "GPU-Temperatur: N/A",
                       f"Rechte: {'Administrator' if is_admin() else 'Standardbenutzer (🔒 Tweaks gesperrt)'}"]
            self.home_sensors.configure(text="\n".join(sensors))
            fn = d.get("fn")
            self.home_status.configure(text=(f"● Fortnite läuft · Priorität {'HOCH' if fn['high'] else 'normal'}" if fn else "○ Fortnite läuft nicht"),
                                       text_color=GREEN if fn else MUTED)
        if page == "Performance" and hasattr(self, "perf_labels"):
            for key, (value, detail) in {
                "cpu": (d["cpu"] / 100, f"{d['cpu']:.1f}%"),
                "ram": (d["ram"] / 100, f"{d['ram']:.1f}% · {fmt_bytes(d['ram_used'])}"),
                "gpu": ((gpu_usage or 0) / 100, f"{gpu_usage:.1f}%" if gpu_usage is not None else "N/A"),
                "disk": (0, f"R {fmt_bytes(self.disk_rates[0])}/s · W {fmt_bytes(self.disk_rates[1])}/s"),
                "network": (0, f"↓ {fmt_bytes(d['down'])}/s · ↑ {fmt_bytes(d['up'])}/s"),
            }.items():
                bar, label = self.perf_labels[key]
                bar.set(max(0, min(1, value)))
                label.configure(text=detail)
        if page == "Fortnite" and hasattr(self, "fortnite_state"):
            fn = d.get("fn")
            if fn:
                self.fortnite_state.configure(text=f"Fortnite läuft · {fn['count']} Prozess(e)" + (" · Priorität automatisch auf HOCH gesetzt" if fn.get("boosted") else ""), text_color=GREEN)
                if hasattr(self, "fn_proc_label"):
                    self.fn_proc_label.configure(text=f"Fortnite PID {fn['pid']} · CPU {fn['cpu']:.1f}% (Gesamt) · RAM {fn['ram_mb']:.0f} MB · Priorität {'HOCH' if fn['high'] else 'nicht HOCH'}")
            else:
                self.fortnite_state.configure(text="Fortnite läuft gerade nicht · Config-Tweaks jetzt anwenden", text_color="#d1b26b")
                if hasattr(self, "fn_proc_label"):
                    self.fn_proc_label.configure(text="Fortnite-Prozess: läuft nicht")

    def periodic(self):
        if not self.alive:
            return
        self.refresh_live()
        if self.active_page == "Processes":
            self.refresh_processes()
        if int(time.time()) % 8 < 3:
            self.refresh_gpu_async()
        self.after(2000, self.periodic)

    # ======================= Network Lab (V3) =======================
    def _build_netlab_page(self):
        p = self.pages["Network Lab"]
        self.add_page_heading(p, "Network Lab", "Echte Messungen: ping.exe, TCP-Handshake, DNS-Zeit und HTTPS-Durchsatz. Nichts davon ist ein Fortnite-Server-Ping.")
        grid = ctk.CTkFrame(p, fg_color="transparent")
        grid.pack(fill="x")
        grid.grid_columnconfigure((0, 1), weight=1, uniform="nl")
        self.nl_gateway = None
        left = self.group(grid, "MESSUNGEN", 0, 0)

        def card(title, desc):
            c = ctk.CTkFrame(left, fg_color=CARD, corner_radius=11, border_width=1, border_color="#1E1E33")
            c.pack(fill="x", pady=5)
            ctk.CTkLabel(c, text=title, text_color=TEXT, font=ctk.CTkFont("Segoe UI", 12, "bold"), anchor="w").pack(fill="x", padx=13, pady=(10, 2))
            ctk.CTkLabel(c, text=desc, text_color="#a5a5a5", font=ctk.CTkFont("Segoe UI", 9), anchor="w", justify="left", wraplength=500).pack(fill="x", padx=13, pady=(0, 6))
            return c

        def btn(parent, text, cmd, accent=False, side="left"):
            b = ctk.CTkButton(parent, text=text, command=cmd, height=36, corner_radius=9, fg_color=PURPLE if accent else "#1E1E33",
                              hover_color=PURPLE_HOVER if accent else "#2A2A44", font=ctk.CTkFont("Segoe UI", 10, "bold"))
            b.pack(side=side, padx=4, pady=(0, 6), fill="x", expand=True)
            return b

        def result_label(parent):
            lab = ctk.CTkLabel(parent, text="Noch nicht gemessen.", text_color="#bdbdbd", font=ctk.CTkFont("Segoe UI", 9), anchor="w", justify="left", wraplength=500)
            lab.pack(fill="x", padx=13, pady=(0, 10))
            return lab

        c1 = card("Ping · Jitter · Paketverlust", "20 ICMP-Pings zum Ziel. Jitter = Ø Unterschied aufeinanderfolgender Antworten. Verlust zählt jeden unbeantworteten Ping.")
        self.nl_target = ctk.CTkEntry(c1, height=34)
        self.nl_target.insert(0, "1.1.1.1")
        self.nl_target.pack(fill="x", padx=13, pady=(0, 6))
        r = ctk.CTkFrame(c1, fg_color="transparent"); r.pack(fill="x", padx=9)
        btn(r, "20 Pings starten", self.nl_ping_target, True)
        btn(r, "Abbrechen", self.ping_cancel.set)
        self.nl_ping_label = result_label(c1)

        c2 = card("Router / Gateway (LAN-Qualität)", "Pingt dein Standard-Gateway. Hier sollten bei Ethernet ~0–2 ms und 0 % Verlust stehen. Abweichungen deuten auf Kabel, Port oder Router hin.")
        r = ctk.CTkFrame(c2, fg_color="transparent"); r.pack(fill="x", padx=9)
        btn(r, "Gateway testen", self.nl_ping_gateway, True)
        self.nl_gw_label = result_label(c2)

        c3 = card("DNS-Auflösung & Epic-Endpunkte (TCP)", "Misst DNS-Zeit und TCP-Handshake zu Epic-Webendpunkten. Das ist NICHT die Fortnite-Spielserver-Latenz.")
        r = ctk.CTkFrame(c3, fg_color="transparent"); r.pack(fill="x", padx=9)
        btn(r, "DNS & Epic testen", self.nl_dns_epic, True)
        btn(r, "DNS-Cache leeren", self.flush_dns)
        self.nl_dns_label = result_label(c3)

        c4 = card("Download- / Upload-Test", "Echter HTTPS-Transfer über Cloudflare (ca. 8 s je Richtung, mehrere Streams). Zeigt deinen Durchsatz, keine Spielroute. Verbraucht Datenvolumen (~100–400 MB je nach Anschluss).")
        r = ctk.CTkFrame(c4, fg_color="transparent"); r.pack(fill="x", padx=9)
        btn(r, "Download testen", self.nl_download, True)
        btn(r, "Upload testen", self.nl_upload)
        self.nl_speed_label = result_label(c4)

        right = self.group(grid, "VERBINDUNG", 0, 1)
        self.nl_cfg_label = ctk.CTkLabel(right, text="Netzwerkdaten werden geladen …", text_color="#cccccc", justify="left", anchor="w",
                                         font=ctk.CTkFont("Segoe UI", 9), wraplength=520)
        self.nl_cfg_label.pack(fill="x", padx=8, pady=8)
        self.nl_lan_label = ctk.CTkLabel(right, text="", text_color=GREEN, justify="left", anchor="w", font=ctk.CTkFont("Segoe UI", 10, "bold"), wraplength=520)
        self.nl_lan_label.pack(fill="x", padx=8, pady=(0, 8))
        ctk.CTkButton(right, text="Netzwerkdaten aktualisieren", command=self.nl_refresh_config, height=36, fg_color=PURPLE,
                      hover_color=PURPLE_HOVER).pack(fill="x", padx=8, pady=(0, 8))

    def _nl_set(self, label, text):
        if not self.alive:
            return
        def apply():
            try:
                label.configure(text=text)
            except Exception:  # noqa: BLE001
                pass
        self.after(0, apply)

    def _nl_guard(self) -> bool:
        if self.net_busy:
            messagebox.showinfo(APP_NAME, "Es läuft bereits eine Messung. Bitte warten oder abbrechen.")
            return False
        self.net_busy = True
        return True

    def _nl_free(self):
        self.net_busy = False

    def nl_ping_target(self):
        host = self.nl_target.get().strip()
        if not re.fullmatch(r"[A-Za-z0-9.\-:]+", host or ""):
            messagebox.showwarning(APP_NAME, "Bitte eine gültige IP-Adresse oder einen Hostnamen eingeben.")
            return
        if not self._nl_guard():
            return
        self.ping_cancel.clear()
        def worker():
            try:
                res = netlab.ping_series(host, 20, 0.2, progress=lambda r: self._nl_set(self.nl_ping_label, f"Läuft {r.sent}/20 …\n" + r.summary()), cancel=self.ping_cancel)
                self._nl_set(self.nl_ping_label, res.summary() + "\nAllgemeiner ICMP-Test, kein Fortnite-Server-Ping.")
            finally:
                self._nl_free()
        threading.Thread(target=worker, daemon=True).start()

    def nl_ping_gateway(self):
        gw = self.nl_gateway
        if not gw:
            messagebox.showinfo(APP_NAME, "Kein Gateway erkannt. Aktualisiere zuerst die Netzwerkdaten.")
            return
        if not self._nl_guard():
            return
        def worker():
            try:
                res = netlab.ping_series(gw, 20, 0.2, progress=lambda r: self._nl_set(self.nl_gw_label, f"Läuft {r.sent}/20 …"))
                self._nl_set(self.nl_gw_label, res.summary())
            finally:
                self._nl_free()
        threading.Thread(target=worker, daemon=True).start()

    def nl_dns_epic(self):
        if not self._nl_guard():
            return
        def worker():
            try:
                lines = []
                for host in ("www.epicgames.com", "cloudflare.com"):
                    ms, info = netlab.dns_lookup_ms(host)
                    lines.append(f"DNS {host}: {ms:.1f} ms → {info}" if ms is not None else f"DNS {host}: fehlgeschlagen ({info})")
                for host, port in netlab.EPIC_TCP_TARGETS:
                    res = netlab.tcp_connect_ms(host, port, 5)
                    lines.append(res.summary())
                self._nl_set(self.nl_dns_label, "\n".join(lines) + "\nTCP zu Epic-Webendpunkten ≠ Spielserver-Ping.")
            finally:
                self._nl_free()
        threading.Thread(target=worker, daemon=True).start()

    def nl_download(self):
        if not self._nl_guard():
            return
        self._nl_set(self.nl_speed_label, "Download läuft …")
        def worker():
            try:
                res = netlab.download_test(8.0, 4, progress=lambda m: self._nl_set(self.nl_speed_label, f"Download läuft … {m:.0f} Mbit/s"))
                self._nl_set(self.nl_speed_label, res.summary())
            finally:
                self._nl_free()
        threading.Thread(target=worker, daemon=True).start()

    def nl_upload(self):
        if not self._nl_guard():
            return
        self._nl_set(self.nl_speed_label, "Upload läuft … (ca. 8–25 s)")
        def worker():
            try:
                res = netlab.upload_test(8.0, 3)
                self._nl_set(self.nl_speed_label, res.summary())
            finally:
                self._nl_free()
        threading.Thread(target=worker, daemon=True).start()

    def nl_refresh_config(self):
        def worker():
            cfg = netlab.get_network_config()
            adapters = netlab.psutil_adapters()
            lines = []
            gateway = None
            for c in cfg:
                gateway = gateway or (c["gateway"][0] if c["gateway"] else None)
                lines.append(f"{c['name']} · {c['status'] or 'Up'} · {c['link_speed'] or 'Geschwindigkeit N/A'}\n  {c['description']}\n  IPv4: {', '.join(c['ipv4']) or 'N/A'}\n  Gateway: {', '.join(c['gateway']) or 'N/A'}\n  DNS: {', '.join(c['dns']) or 'N/A'}")
            if not lines:
                lines = ["Keine Daten über Get-NetIPConfiguration (PowerShell) verfügbar."]
            lan = [a for a in adapters if a["kind"] == "LAN" and a["is_up"]]
            wifi = [a for a in adapters if a["kind"] == "WLAN" and a["is_up"]]
            if lan:
                lan_text = "LAN: verbunden · " + ", ".join(f"{a['name']} ({a['speed_mbit']} Mbit/s)" if a["speed_mbit"] else a["name"] for a in lan)
            elif wifi:
                lan_text = "Kein aktives LAN – WLAN aktiv. Für Fortnite wird Ethernet empfohlen."
            else:
                lan_text = "Kein aktiver LAN-/WLAN-Adapter erkannt."
            def apply():
                self.nl_gateway = gateway
                try:
                    self.nl_cfg_label.configure(text="\n\n".join(lines))
                    self.nl_lan_label.configure(text=lan_text)
                except Exception:  # noqa: BLE001
                    pass
            if self.alive:
                self.after(0, apply)
        threading.Thread(target=worker, daemon=True).start()

    # ======================= Fortnite Live-Messung (V3) =======================
    def _build_fortnite_live(self, grid):
        live = self.group(grid, "LIVE-MESSUNG · FPS / FRAMETIME / 1% LOW", 1, 0, 2)
        self.fn_proc_label = ctk.CTkLabel(live, text="Fortnite-Prozess: wird gesucht …", text_color="#cccccc", anchor="w", justify="left", font=ctk.CTkFont("Segoe UI", 10))
        self.fn_proc_label.pack(fill="x", padx=10, pady=(6, 2))
        self.fn_result_label = ctk.CTkLabel(live, text="Noch keine Messung. FPS, Frametime und 1% Low kommen ausschließlich aus echten PresentMon-Daten (Intel, MIT-Lizenz).", text_color="#bdbdbd",
                                            anchor="w", justify="left", font=ctk.CTkFont("Segoe UI", 10), wraplength=1000)
        self.fn_result_label.pack(fill="x", padx=10, pady=(2, 6))
        ctk.CTkLabel(live, text="Eingabelatenz: nicht per Software messbar (dafür braucht es z. B. NVIDIA LDAT/Reflex Analyzer) – RISEN schätzt sie nicht.\nFortnite-Server-Latenz: Windows liefert sie nicht. Nutze die In-Game-Netzwerkstatistik (Einstellungen → Spiel-UI, je nach Version „Netzwerk-Debug-Statistiken“); RISEN leitet sie nicht aus Cloudflare-Pings ab.",
                     text_color=MUTED, anchor="w", justify="left", font=ctk.CTkFont("Segoe UI", 9), wraplength=1000).pack(fill="x", padx=10, pady=(0, 8))
        row = ctk.CTkFrame(live, fg_color="transparent")
        row.pack(fill="x", padx=6, pady=(0, 8))
        for text, cmd, accent in (("30 s Messung starten", self.fn_capture, True), ("Hintergrundlast anzeigen", self.fn_background, False),
                                  ("PresentMon herunterladen", lambda: webbrowser.open("https://github.com/GameTechDev/PresentMon/releases"), False)):
            ctk.CTkButton(row, text=text, command=cmd, height=38, corner_radius=9, fg_color=PURPLE if accent else "#1E1E33",
                          hover_color=PURPLE_HOVER if accent else "#2A2A44", font=ctk.CTkFont("Segoe UI", 10, "bold")).pack(side="left", padx=4, expand=True, fill="x")

    def fn_capture(self):
        if self.fn_busy:
            return
        exe = fnmon.find_presentmon()
        if not exe:
            messagebox.showinfo(APP_NAME, "PresentMon.exe wurde nicht gefunden.\n\nLade PresentMon (Intel, MIT-Lizenz) über den Button herunter und lege PresentMon.exe in denselben Ordner wie RISEN_TWEAKS.exe. Ohne PresentMon zeigt RISEN keine FPS-/Frametime-Werte, statt welche zu erfinden.")
            return
        if not self.find_fortnite():
            messagebox.showinfo(APP_NAME, "Fortnite läuft nicht. Starte ein Match/den Trainingsmodus und drücke dann erneut auf Messung (30 s).")
            return
        self.fn_busy = True
        self.fn_result_label.configure(text="Messung läuft 30 s … bitte im Spiel bleiben.")
        out = APP_DIR / "presentmon_capture.csv"
        def worker():
            try:
                ok, err = fnmon.run_capture(exe, out, 30)
                if not ok:
                    self._nl_set(self.fn_result_label, f"Messung fehlgeschlagen: {err}\n(PresentMon braucht evtl. Administratorrechte.)")
                    return
                stats = fnmon.compute_frame_stats(fnmon.parse_presentmon_csv(out))
                if not stats:
                    self._nl_set(self.fn_result_label, "Zu wenige Frames erfasst (Spiel im Hintergrund oder falscher Prozess?).")
                    return
                snap = fnmon.process_snapshot()
                gpu = self.gpu_data or {}
                extra = []
                if snap:
                    extra.append(f"Fortnite CPU {snap['cpu_pct']:.0f}% (Gesamt-CPU-Anteil) · RAM {snap['ram_mb']:.0f} MB")
                if gpu.get("available"):
                    extra.append(f"GPU {gpu.get('usage', 'N/A')}% · VRAM {gpu.get('mem_used', 'N/A')}/{gpu.get('mem_total', 'N/A')} MiB (aktueller Wert)")
                self._nl_set(self.fn_result_label, "\n".join(stats.lines() + extra))
            finally:
                self.fn_busy = False
        threading.Thread(target=worker, daemon=True).start()

    def fn_background(self):
        self.fn_result_label.configure(text="Analysiere Hintergrundprozesse …")
        def worker():
            rows = fnmon.top_background(8)
            text = "Prozesse mit der höchsten CPU-Last (ohne Fortnite) – nur Anzeige, nichts wird beendet:\n" + "\n".join(f"  {n}: CPU {c:.1f}% · RAM {r:.0f} MB" for n, c, r in rows)
            self._nl_set(self.fn_result_label, text)
        threading.Thread(target=worker, daemon=True).start()

    def set_status(self, text: str):
        self.status_bar.configure(text=text)

    def on_close(self):
        self.alive = False
        self.destroy()


def safe_gpu_detail(data: dict, verbose: bool = False) -> str:
    if not data or not data.get("available"):
        name = data.get("name", "GPU-Telemetrie nicht verfügbar") if isinstance(data, dict) else "GPU-Telemetrie nicht verfügbar"
        return str(name)
    lines = [f"GPU: {data.get('name', 'NVIDIA GPU')}", f"Load: {data.get('usage') if data.get('usage') is not None else 'N/A'}%",
             f"VRAM: {data.get('mem_used') if data.get('mem_used') is not None else 'N/A'} / {data.get('mem_total') if data.get('mem_total') is not None else 'N/A'} MiB",
             f"Temperature: {data.get('temp') if data.get('temp') is not None else 'N/A'} °C"]
    if verbose:
        lines.append(f"Power draw: {data.get('power') if data.get('power') is not None else 'N/A'} W")
        lines.append(f"GPU clock: {data.get('clock') if data.get('clock') is not None else 'N/A'} MHz · Memory clock: {data.get('mem_clock') if data.get('mem_clock') is not None else 'N/A'} MHz")
    return "\n".join(lines)


def selftest() -> int:
    """Headless-Check fuer den CI-Build: prueft, dass die gebaute EXE alle Module,
    Tcl/Tk, winreg und die Kernlogik enthaelt. Oeffnet kein Fenster."""
    import tempfile
    lines, problems = [], []

    def check(name, fn):
        try:
            lines.append(f"OK    {name}: {fn()}")
        except Exception as exc:  # noqa: BLE001
            problems.append(name)
            lines.append(f"FAIL  {name}: {exc!r}")

    check("Tcl/Tk", lambda: tk.Tcl().eval("info patchlevel"))
    check("customtkinter", lambda: ctk.__version__ if hasattr(ctk, "__version__") else "importiert")
    check("psutil", lambda: psutil.__version__)
    check("winreg lesen", lambda: tweaklib.WinRegBackend().read(r"Software\Microsoft\Windows\CurrentVersion\Explorer", "NoSuchValue")[0])
    check("Ping-Parser", lambda: netlab.parse_ping_reply("Reply from 1.1.1.1: bytes=32 time=12ms TTL=57"))
    check("Frame-Statistik", lambda: round(fnmon.compute_frame_stats([4.0] * 100).avg_fps, 1))

    def engine_roundtrip():
        with tempfile.TemporaryDirectory() as d:
            reg = tweaklib.MemoryBackend()
            eng = tweaklib.TweakEngine(reg, Path(d) / "s.json")
            t = CATALOG_BY_ID["game_dvr"]
            a = eng.apply(t)
            u = eng.undo(t)
            if not (a.ok and u.ok):
                raise RuntimeError(f"{a.message} / {u.message}")
            return "apply+undo verifiziert"
    check("Tweak-Engine", engine_roundtrip)
    check("Katalog", lambda: f"{len(CATALOG)} Eintraege")
    try:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        (APP_DIR / "selftest.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    print("\n".join(lines))
    return 2 if problems else 0


def main():
    if "--selftest" in sys.argv:
        return selftest()
    if not IS_WINDOWS:
        print("RISEN TWEAKS is designed for Windows 10/11.")
        return 1
    try:
        app = RisenTweaks()
        app.mainloop()
        return 0
    except Exception as exc:
        log(f"Fatal error: {exc}")
        try:
            messagebox.showerror(APP_NAME, f"App konnte nicht gestartet werden:\n{exc}\n\nLog: {LOG_FILE}")
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
