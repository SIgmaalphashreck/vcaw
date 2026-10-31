"""RISEN TWEAKS - Tweak-Bibliothek.

Jeder Registry-Tweak:
  * aendert ausschliesslich HKEY_CURRENT_USER (keine Admin-Rechte noetig),
  * sichert vor der ersten Aenderung den Originalzustand (tweak_state.json),
  * liest den Wert nach dem Schreiben zurueck und meldet, ob er stimmt
    (bei Abweichung wird automatisch zurueckgesetzt),
  * kann ueber Undo auf den gesicherten Zustand zurueckgesetzt werden.

Bewusst NICHT enthalten: HPET/BCD, MSI-Mode, TCP/Nagle, Defender, Firewall,
Windows Update, Telemetrie-Hacks, Treiber, Anti-Cheat.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional, Protocol

IS_WINDOWS = os.name == "nt"

REG_SZ, REG_BINARY, REG_DWORD = 1, 3, 4

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APPROVED_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"

CATEGORIES = [
    "Gaming Optimization", "Fortnite Optimization", "Windows Optimization",
    "Mouse Optimization", "Keyboard Optimization", "Power Optimization",
    "Network Optimization", "NVIDIA Optimization", "AMD Optimization",
    "CPU Tweaks", "RAM Tweaks", "Cleanup", "Startup Optimization", "Advanced Tweaks",
]


# --------------------------------------------------------------------------
# Registry-Backends
# --------------------------------------------------------------------------
class RegBackend(Protocol):
    def read(self, path: str, name: str) -> tuple[bool, Optional[int], object]: ...
    def write(self, path: str, name: str, rtype: int, value: object) -> None: ...
    def delete(self, path: str, name: str) -> None: ...
    def enum(self, path: str) -> list[tuple[str, object, int]]: ...


class WinRegBackend:
    """HKEY_CURRENT_USER; Pfade mit Praefix 'HKLM\\' gehen nach HKEY_LOCAL_MACHINE (Adminrechte noetig).
    Nur unter Windows nutzbar."""

    def __init__(self):
        import winreg  # noqa: PLC0415
        self.w = winreg

    def _root(self, path):
        if path.upper().startswith("HKLM\\"):
            return self.w.HKEY_LOCAL_MACHINE, path[5:]
        return self.w.HKEY_CURRENT_USER, path

    def read(self, path, name):
        root, sub = self._root(path)
        try:
            with self.w.OpenKey(root, sub, 0, self.w.KEY_READ) as key:
                value, rtype = self.w.QueryValueEx(key, name)
                return True, int(rtype), value
        except OSError:
            return False, None, None

    def write(self, path, name, rtype, value):
        root, sub = self._root(path)
        with self.w.CreateKeyEx(root, sub, 0, self.w.KEY_SET_VALUE) as key:
            self.w.SetValueEx(key, name, 0, rtype, value)

    def delete(self, path, name):
        root, sub = self._root(path)
        try:
            with self.w.OpenKey(root, sub, 0, self.w.KEY_SET_VALUE) as key:
                self.w.DeleteValue(key, name)
        except FileNotFoundError:
            pass

    def enum(self, path):
        root, sub = self._root(path)
        out = []
        try:
            with self.w.OpenKey(root, sub, 0, self.w.KEY_READ) as key:
                i = 0
                while True:
                    try:
                        n, v, t = self.w.EnumValue(key, i)
                    except OSError:
                        break
                    out.append((n, v, int(t)))
                    i += 1
        except OSError:
            pass
        return out

    def subkeys(self, path):
        root, sub = self._root(path)
        out = []
        try:
            with self.w.OpenKey(root, sub, 0, self.w.KEY_READ) as key:
                i = 0
                while True:
                    try:
                        out.append(self.w.EnumKey(key, i))
                    except OSError:
                        break
                    i += 1
        except OSError:
            pass
        return out


class MemoryBackend:
    """Fake-Registry fuer Tests (Namen wie in Windows: case-insensitiv, Schreibweise bleibt erhalten)."""

    def __init__(self):
        self.data: dict[tuple[str, str], tuple[str, int, object]] = {}

    def read(self, path, name):
        item = self.data.get((path.lower(), name.lower()))
        return (True, item[1], item[2]) if item else (False, None, None)

    def write(self, path, name, rtype, value):
        self.data[(path.lower(), name.lower())] = (name, rtype, value)

    def delete(self, path, name):
        self.data.pop((path.lower(), name.lower()), None)

    def enum(self, path):
        return [(v[0], v[2], v[1]) for k, v in self.data.items() if k[0] == path.lower()]

    def subkeys(self, path):
        prefix = path.lower() + "\\"
        seen: dict[str, str] = {}
        for (p, _n), _v in self.data.items():
            if p.startswith(prefix):
                seg = p[len(prefix):].split("\\")[0]
                seen.setdefault(seg, seg)
        return list(seen)


def read_hklm(path: str, name: str):
    """Reiner Lesezugriff auf HKLM (fuer Statusanzeigen)."""
    if not IS_WINDOWS:
        return None
    try:
        import winreg  # noqa: PLC0415
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path, 0, winreg.KEY_READ) as key:
            return winreg.QueryValueEx(key, name)[0]
    except OSError:
        return None


# --------------------------------------------------------------------------
# Datenmodell
# --------------------------------------------------------------------------
@dataclass
class RegValue:
    path: str
    name: str
    rtype: int
    value: object


@dataclass
class Tweak:
    id: str
    name: str
    categories: list[str]
    description: str
    impact: str                      # Erklaerung der Auswirkungen / Nebenwirkungen
    kind: str = "registry"           # registry | power | action | info
    values: list[RegValue] = field(default_factory=list)
    restart: str = "Nein"            # Hinweis zur Wirksamkeit
    hook: str = ""                   # z. B. "mouse_live"
    power_target: str = ""           # high | ultimate
    action_id: str = ""              # fuer kind == action
    action_label: str = "Ausfuehren"
    info_text: str = ""              # fuer kind == info
    status_fn: Optional[Callable[[], str]] = None
    open_uri: str = ""               # Button-Ziel fuer info-Karten
    open_label: str = ""
    ps_sub: str = ""                 # powersetting: Untergruppe (Alias oder GUID)
    ps_setting: str = ""             # powersetting: Einstellung (Alias oder GUID)
    ps_value: int = 0                # powersetting: Zielwert (Netzbetrieb)
    ini_section: str = ""            # ini: Abschnitt in GameUserSettings.ini
    ini_values: dict = field(default_factory=dict)
    recommended: str = ""            # "" | baseline | desktop | fortnite  (fuer 'Best Tweaks')
    why: str = ""                    # Kurzbegruendung fuer die Empfehlung
    admin: bool = False              # braucht Administratorrechte (HKLM)
    dynamic: Optional[Callable] = None   # liefert RegValue-Liste zur Laufzeit (z. B. GPU-Geraete)


TOGGLE_KINDS = ("registry", "power", "powersetting", "ini")


@dataclass
class Result:
    ok: bool
    message: str
    verified: bool = False


STATUS_ACTIVE, STATUS_INACTIVE, STATUS_PARTIAL, STATUS_UNKNOWN = "aktiv", "nicht aktiv", "teilweise", "unbekannt"


def _same(a, b) -> bool:
    if isinstance(a, (bytes, bytearray)) or isinstance(b, (bytes, bytearray)):
        return bytes(a or b"") == bytes(b or b"")
    if isinstance(a, int) or isinstance(b, int):
        try:
            return int(a) == int(b)
        except (TypeError, ValueError):
            return False
    return str(a) == str(b)


def _enc(v):
    import base64
    return {"__bytes__": base64.b64encode(bytes(v)).decode("ascii")} if isinstance(v, (bytes, bytearray)) else v


def _dec(v):
    import base64
    return base64.b64decode(v["__bytes__"]) if isinstance(v, dict) and "__bytes__" in v else v


GUID_RE = re.compile(r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})")
HIGH_PERF_GUID = "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c"
ULTIMATE_SRC_GUID = "e9a42b02-d5df-448d-aa00-03f14749eb61"


def is_admin() -> bool:
    if not IS_WINDOWS:
        return True
    try:
        import ctypes  # noqa: PLC0415
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001
        return False


def default_runner(args: list[str], timeout: int = 12) -> tuple[int, str]:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if IS_WINDOWS else 0
    try:
        p = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=timeout, shell=False, creationflags=flags)
        return p.returncode, ((p.stdout or "") + (p.stderr or "")).strip()
    except Exception as exc:  # noqa: BLE001
        return 1, str(exc)


# --------------------------------------------------------------------------
# INI-Hilfen (Fortnite GameUserSettings.ini)
# --------------------------------------------------------------------------
def fortnite_ini_path() -> Path:
    base = os.environ.get("LOCALAPPDATA", str(Path.home()))
    return Path(base) / "FortniteGame" / "Saved" / "Config" / "WindowsClient" / "GameUserSettings.ini"


def _ini_load(path: Path) -> tuple[list[str], str, bool]:
    raw = path.read_bytes()
    bom = raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8-sig", errors="replace")
    eol = "\r\n" if "\r\n" in text else "\n"
    return text.splitlines(keepends=True), eol, bom


def _ini_store(path: Path, lines: list[str], bom: bool) -> None:
    data = "".join(lines).encode("utf-8")
    path.write_bytes((b"\xef\xbb\xbf" if bom else b"") + data)


def _section_bounds(lines: list[str], section: str) -> Optional[tuple[int, int]]:
    start = None
    for i, ln in enumerate(lines):
        st = ln.strip()
        if st.lower() == f"[{section}]".lower():
            start = i
        elif start is not None and st.startswith("["):
            return start, i
    return (start, len(lines)) if start is not None else None


FORT_SECTION = "/Script/FortniteGame.FortGameUserSettings"


def _sk(t, key: str) -> tuple[str, str]:
    """ini_values-Schluessel 'Abschnitt|Name' -> (Abschnitt, Name); ohne Praefix gilt t.ini_section."""
    if "|" in key:
        sec, name = key.split("|", 1)
        return sec, name
    return t.ini_section, key


def ini_read(path: Path, section: str, key: str) -> Optional[str]:
    if not path.exists():
        return None
    lines, _, _ = _ini_load(path)
    b = _section_bounds(lines, section)
    if not b:
        return None
    for ln in lines[b[0] + 1:b[1]]:
        k, sep, v = ln.partition("=")
        if sep and k.strip().lower() == key.lower():
            return v.strip()
    return None


def ini_write(path: Path, section: str, key: str, value: Optional[str]) -> None:
    """Setzt (oder loescht bei None) einen Schluessel; alle anderen Zeilen bleiben unveraendert."""
    lines, eol, bom = _ini_load(path)
    b = _section_bounds(lines, section)
    if b is None:
        if value is None:
            return
        if lines and not lines[-1].endswith(("\n", "\r")):
            lines[-1] += eol
        lines += [f"[{section}]{eol}", f"{key}={value}{eol}"]
        _ini_store(path, lines, bom)
        return
    for i in range(b[0] + 1, b[1]):
        k, sep, _ = lines[i].partition("=")
        if sep and k.strip().lower() == key.lower():
            if value is None:
                del lines[i]
            else:
                lines[i] = f"{k.rstrip()}={value}{eol}"
            _ini_store(path, lines, bom)
            return
    if value is None:
        return
    insert_at = b[1]
    while insert_at - 1 > b[0] and not lines[insert_at - 1].strip():
        insert_at -= 1
    if insert_at > 0 and not lines[insert_at - 1].endswith(("\n", "\r")):
        lines[insert_at - 1] += eol
    lines.insert(insert_at, f"{key}={value}{eol}")
    _ini_store(path, lines, bom)


def _same_text(a: Optional[str], b: Optional[str]) -> bool:
    if a is None or b is None:
        return a is b
    try:
        return abs(float(a) - float(b)) < 1e-6
    except ValueError:
        return a.strip().lower() == b.strip().lower()


# --------------------------------------------------------------------------
# Engine
# --------------------------------------------------------------------------
class TweakEngine:
    def __init__(self, backend: RegBackend, state_path: Path,
                 runner: Callable[[list[str]], tuple[int, str]] = default_runner,
                 hooks: Optional[dict[str, Callable[[RegBackend], None]]] = None,
                 logger: Callable[[str], None] = lambda m: None,
                 ini_path: Optional[Path] = None,
                 fortnite_running: Callable[[], bool] = lambda: False,
                 admin_check: Optional[Callable[[], bool]] = None):
        self.admin_check = admin_check if admin_check is not None else is_admin
        self.ini_path = ini_path if ini_path is not None else fortnite_ini_path()
        self.fortnite_running = fortnite_running
        self.backend = backend
        self.state_path = state_path
        self.runner = runner
        self.hooks = hooks if hooks is not None else {}
        self.log = logger
        self.state: dict = {}
        self._load()

    # ---- Zustand -------------------------------------------------------
    def _load(self):
        try:
            if self.state_path.exists():
                data = json.loads(self.state_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    self.state = data
        except Exception as exc:  # noqa: BLE001
            self.log(f"Tweak-State lesen fehlgeschlagen: {exc}")

    def _save(self):
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.state_path)

    def has_backup(self, tweak_id: str) -> bool:
        return tweak_id in self.state

    def _vals(self, t: Tweak) -> list:
        if t.dynamic is not None:
            try:
                return list(t.dynamic(self.backend))
            except Exception as exc:  # noqa: BLE001
                self.log(f"dynamic {t.id}: {exc}")
                return []
        return t.values

    # ---- Status ----------------------------------------------------------
    def status(self, t: Tweak) -> tuple[str, str]:
        try:
            if t.kind == "registry":
                vals = self._vals(t)
                if not vals:
                    return STATUS_UNKNOWN, "Kein passendes Geraet/Eintrag auf diesem System gefunden."
                matches = 0
                for v in vals:
                    exists, _, cur = self.backend.read(v.path, v.name)
                    if exists and _same(cur, v.value):
                        matches += 1
                if matches == len(vals):
                    return STATUS_ACTIVE, "Alle Werte entsprechen dem Tweak."
                if matches == 0:
                    return STATUS_INACTIVE, "Standard bzw. unveraendert."
                return STATUS_PARTIAL, f"{matches}/{len(vals)} Werte gesetzt."
            if t.kind == "power":
                cur = self._active_guid()
                if cur is None:
                    return STATUS_UNKNOWN, "Aktiver Energieplan nicht lesbar."
                if t.power_target == "high":
                    return (STATUS_ACTIVE, "Höchstleistung ist aktiv.") if cur.lower() == HIGH_PERF_GUID \
                        else (STATUS_INACTIVE, "Anderer Energieplan aktiv.")
                made = self.state.get(t.id, {}).get("created_scheme")
                if made and cur.lower() == made.lower():
                    return STATUS_ACTIVE, "Ultimative Leistung ist aktiv."
                return STATUS_INACTIVE, "Anderer Energieplan aktiv."
            if t.kind == "powersetting":
                cur = self._ps_query(self._active_guid() or "SCHEME_CURRENT", t)
                if cur is None:
                    return STATUS_UNKNOWN, "Einstellung auf diesem System nicht verfügbar."
                return (STATUS_ACTIVE, f"Wert {cur} (Ziel {t.ps_value}).") if cur == t.ps_value \
                    else (STATUS_INACTIVE, f"Aktueller Wert {cur}, Ziel {t.ps_value}.")
            if t.kind == "ini":
                if not self.ini_path.exists():
                    return STATUS_UNKNOWN, "Fortnite-Config nicht gefunden (Fortnite einmal starten)."
                hits = sum(1 for k, v in t.ini_values.items() if _same_text(ini_read(self.ini_path, *_sk(t, k)), str(v)))
                if hits == len(t.ini_values):
                    return STATUS_ACTIVE, "Werte stehen in der Config."
                return (STATUS_INACTIVE, "Standard bzw. andere Werte.") if hits == 0 else (STATUS_PARTIAL, f"{hits}/{len(t.ini_values)} Werte.")
            if t.kind == "info" and t.status_fn:
                return STATUS_UNKNOWN, t.status_fn()
        except Exception as exc:  # noqa: BLE001
            return STATUS_UNKNOWN, str(exc)
        return STATUS_UNKNOWN, ""

    # ---- Apply -----------------------------------------------------------
    def apply(self, t: Tweak) -> Result:
        if t.admin and not self.admin_check():
            return Result(False, "Administratorrechte nötig: RISEN TWEAKS per Rechtsklick 'Als Administrator ausführen' starten.")
        if t.kind == "registry":
            return self._apply_registry(t)
        if t.kind == "power":
            return self._apply_power(t)
        if t.kind == "powersetting":
            return self._apply_powersetting(t)
        if t.kind == "ini":
            return self._apply_ini(t)
        return Result(False, "Dieser Eintrag ist keine aenderbare Einstellung.")

    def _apply_registry(self, t: Tweak) -> Result:
        vals = self._vals(t)
        if not vals:
            return Result(False, "Kein passendes Geraet/Eintrag auf diesem System gefunden.")
        if t.id not in self.state:
            originals = []
            for v in vals:
                exists, rtype, val = self.backend.read(v.path, v.name)
                originals.append({"path": v.path, "name": v.name, "existed": exists,
                                  "type": rtype, "value": _enc(val)})
            self.state[t.id] = {"registry": originals, "saved_at": datetime.now().isoformat(timespec="seconds")}
            self._save()  # Backup MUSS vor der ersten Aenderung auf der Platte liegen
        try:
            for v in vals:
                self.backend.write(v.path, v.name, v.rtype, v.value)
            self._run_hook(t)
        except Exception as exc:  # noqa: BLE001
            self.log(f"Apply {t.id} Fehler: {exc}")
            self.undo(t)
            return Result(False, f"Schreiben fehlgeschlagen, Original wiederhergestellt: {exc}")
        bad = []
        for v in vals:
            exists, _, cur = self.backend.read(v.path, v.name)
            if not (exists and _same(cur, v.value)):
                bad.append(v.name)
        if bad:
            self.undo(t)
            return Result(False, f"Verifikation fehlgeschlagen ({', '.join(bad)}). Original wurde wiederhergestellt.")
        self.log(f"Tweak angewendet: {t.id}")
        note = f" Wirksam: {t.restart}." if t.restart and t.restart != "Nein" else ""
        return Result(True, "Angewendet und per Rücklesen verifiziert." + note, verified=True)

    def _run_hook(self, t: Tweak):
        if t.hook and t.hook in self.hooks:
            self.hooks[t.hook](self.backend)

    def _active_guid(self) -> Optional[str]:
        code, out = self.runner(["powercfg", "/getactivescheme"])
        m = GUID_RE.search(out)
        return m.group(1) if code == 0 and m else None

    def _apply_power(self, t: Tweak) -> Result:
        current = self._active_guid()
        if not current:
            return Result(False, "Aktiver Energieplan konnte nicht gelesen werden.")
        entry = self.state.setdefault(t.id, {})
        entry.setdefault("power_plan", current)
        entry.setdefault("saved_at", datetime.now().isoformat(timespec="seconds"))
        self._save()
        if t.power_target == "high":
            target = HIGH_PERF_GUID
            code, out = self.runner(["powercfg", "/setactive", "SCHEME_MIN"])
            if code != 0:
                self._drop_if_unchanged(t.id, current)
                return Result(False, f"Energieplan 'Höchstleistung' nicht verfügbar: {out[:200]}")
        else:
            code, out = self.runner(["powercfg", "-duplicatescheme", ULTIMATE_SRC_GUID])
            m = GUID_RE.search(out)
            if code != 0 or not m:
                self._drop_if_unchanged(t.id, current)
                return Result(False, f"'Ultimative Leistung' nicht verfügbar auf diesem System: {out[:200]}")
            target = m.group(1)
            entry["created_scheme"] = target
            self._save()
            code, out = self.runner(["powercfg", "/setactive", target])
            if code != 0:
                self.runner(["powercfg", "/delete", target])
                self._drop_if_unchanged(t.id, current)
                return Result(False, f"Aktivieren fehlgeschlagen: {out[:200]}")
        now = self._active_guid()
        if not now or now.lower() != target.lower():
            self.undo(t)
            return Result(False, "Verifikation fehlgeschlagen: Energieplan ist nicht aktiv. Zurückgesetzt.")
        return Result(True, "Energieplan gesetzt und verifiziert. Mehr Verbrauch/Wärme möglich; FPS-Gewinn nicht garantiert.", True)

    # ---- powercfg-Einzeleinstellungen ----------------------------------
    def _ps_query(self, scheme: str, t: Tweak) -> Optional[int]:
        code, out = self.runner(["powercfg", "/query", scheme, t.ps_sub, t.ps_setting])
        hexes = re.findall(r"0x[0-9a-fA-F]{1,8}", out)
        if code != 0 or len(hexes) < 2:
            return None
        return int(hexes[-2], 16)  # die letzten zwei Werte sind AC und DC

    def _apply_powersetting(self, t: Tweak) -> Result:
        scheme = self._active_guid()
        if not scheme:
            return Result(False, "Aktiver Energieplan konnte nicht gelesen werden.")
        orig = self._ps_query(scheme, t)
        if orig is None:
            return Result(False, "Diese Energieeinstellung ist auf diesem System nicht verfügbar.")
        if t.id not in self.state:
            self.state[t.id] = {"scheme": scheme, "ac_orig": orig, "saved_at": datetime.now().isoformat(timespec="seconds")}
            self._save()
        scheme = self.state[t.id]["scheme"]
        code, out = self.runner(["powercfg", "/setacvalueindex", scheme, t.ps_sub, t.ps_setting, str(t.ps_value)])
        if code == 0:
            code, out = self.runner(["powercfg", "/setactive", scheme])
        if code != 0 or self._ps_query(scheme, t) != t.ps_value:
            self._undo_powersetting(t, self.state[t.id])
            return Result(False, f"Verifikation fehlgeschlagen oder Fehler: {out[:160]}. Zurückgesetzt.")
        return Result(True, "Gesetzt und per powercfg verifiziert (Netzbetrieb).", True)

    def _undo_powersetting(self, t: Tweak, entry: dict) -> Result:
        scheme, orig = entry["scheme"], int(entry["ac_orig"])
        code, out = self.runner(["powercfg", "/setacvalueindex", scheme, t.ps_sub, t.ps_setting, str(orig)])
        if code == 0:
            code, out = self.runner(["powercfg", "/setactive", scheme])
        if code != 0 or self._ps_query(scheme, t) != orig:
            return Result(False, f"Zurücksetzen nicht verifiziert: {out[:160]}. Backup bleibt erhalten.")
        self.state.pop(t.id, None)
        self._save()
        return Result(True, "Originalwert wiederhergestellt und verifiziert.", True)

    # ---- Fortnite-INI ----------------------------------------------------
    def _apply_ini(self, t: Tweak) -> Result:
        if self.fortnite_running():
            return Result(False, "Fortnite läuft. Beende das Spiel zuerst, sonst überschreibt es die Datei beim Beenden.")
        if not self.ini_path.exists():
            return Result(False, "GameUserSettings.ini nicht gefunden. Starte Fortnite einmal, damit sie erzeugt wird.")
        if t.id not in self.state:
            originals = {k: ini_read(self.ini_path, *_sk(t, k)) for k in t.ini_values}
            backup_dir = self.state_path.parent / "config_backups"
            backup_dir.mkdir(parents=True, exist_ok=True)
            copy = backup_dir / f"GameUserSettings.{datetime.now():%Y%m%d-%H%M%S}.ini"
            shutil.copy2(self.ini_path, copy)
            self.state[t.id] = {"ini": originals, "file_copy": str(copy), "saved_at": datetime.now().isoformat(timespec="seconds")}
            self._save()
        try:
            for k, v in t.ini_values.items():
                ini_write(self.ini_path, *_sk(t, k), str(v))
        except OSError as exc:
            self._undo_ini(t, self.state[t.id])
            return Result(False, f"Schreiben fehlgeschlagen: {exc}")
        bad = [k for k, v in t.ini_values.items() if not _same_text(ini_read(self.ini_path, *_sk(t, k)), str(v))]
        if bad:
            self._undo_ini(t, self.state[t.id])
            return Result(False, f"Verifikation fehlgeschlagen ({', '.join(bad)}). Zurückgesetzt.")
        return Result(True, "In GameUserSettings.ini geschrieben (Kopie der Datei gesichert). Ob Fortnite den Wert übernimmt, siehst du im Spiel.", True)

    def _undo_ini(self, t: Tweak, entry: dict) -> Result:
        if self.fortnite_running():
            return Result(False, "Fortnite läuft. Beende das Spiel zuerst.")
        try:
            for k, v in entry.get("ini", {}).items():
                ini_write(self.ini_path, *_sk(t, k), v)
        except OSError as exc:
            return Result(False, f"Zurücksetzen fehlgeschlagen: {exc}")
        bad = [k for k, v in entry.get("ini", {}).items() if not _same_text(ini_read(self.ini_path, *_sk(t, k)), v)]
        if bad:
            return Result(False, f"Zurücksetzen unvollständig: {', '.join(bad)}. Backup-Kopie: {entry.get('file_copy')}")
        self.state.pop(t.id, None)
        self._save()
        return Result(True, "Originalwerte wiederhergestellt und verifiziert.", True)

    def _drop_if_unchanged(self, tid: str, current: str):
        entry = self.state.get(tid, {})
        if entry.get("power_plan") == current and not entry.get("created_scheme"):
            self.state.pop(tid, None)
            self._save()

    # ---- Undo ------------------------------------------------------------
    def undo(self, t: Tweak) -> Result:
        entry = self.state.get(t.id)
        if not entry:
            return Result(False, "Kein Backup vorhanden: RISEN hat diesen Tweak nicht angewendet, daher gibt es nichts zurückzusetzen.")
        if t.kind == "registry":
            return self._undo_registry(t, entry)
        if t.kind == "power":
            return self._undo_power(t, entry)
        if t.kind == "powersetting":
            return self._undo_powersetting(t, entry)
        if t.kind == "ini":
            return self._undo_ini(t, entry)
        return Result(False, "Nicht umkehrbar.")

    def _undo_registry(self, t: Tweak, entry: dict) -> Result:
        failed = []
        for rec in entry.get("registry", []):
            try:
                if rec["existed"]:
                    self.backend.write(rec["path"], rec["name"], int(rec["type"]), _dec(rec["value"]))
                else:
                    self.backend.delete(rec["path"], rec["name"])
            except Exception as exc:  # noqa: BLE001
                failed.append(f"{rec['name']}: {exc}")
        try:
            self._run_hook(t)
        except Exception as exc:  # noqa: BLE001
            self.log(f"Hook nach Undo {t.id}: {exc}")
        bad = []
        for rec in entry.get("registry", []):
            exists, _, cur = self.backend.read(rec["path"], rec["name"])
            if rec["existed"]:
                if not (exists and _same(cur, _dec(rec["value"]))):
                    bad.append(rec["name"])
            elif exists:
                bad.append(rec["name"])
        if failed or bad:
            return Result(False, f"Zurücksetzen unvollständig: {', '.join(failed + bad)}. Backup bleibt erhalten.")
        self.state.pop(t.id, None)
        self._save()
        self.log(f"Tweak zurückgesetzt: {t.id}")
        return Result(True, "Originalzustand wiederhergestellt und verifiziert.", True)

    def _undo_power(self, t: Tweak, entry: dict) -> Result:
        orig = entry.get("power_plan")
        if not orig:
            return Result(False, "Kein ursprünglicher Energieplan gesichert.")
        code, out = self.runner(["powercfg", "/setactive", orig])
        if code != 0:
            return Result(False, f"Ursprünglicher Plan konnte nicht aktiviert werden: {out[:200]}")
        now = self._active_guid()
        if not now or now.lower() != orig.lower():
            return Result(False, "Verifikation fehlgeschlagen: ursprünglicher Plan ist nicht aktiv.")
        made = entry.get("created_scheme")
        if made:
            self.runner(["powercfg", "/delete", made])
        self.state.pop(t.id, None)
        self._save()
        return Result(True, "Ursprünglicher Energieplan wieder aktiv (verifiziert).", True)

    def undo_all(self, tweaks: list[Tweak]) -> tuple[int, int]:
        ok = bad = 0
        for t in tweaks:
            if t.id in self.state:
                r = self.undo(t)
                ok += 1 if r.ok else 0
                bad += 0 if r.ok else 1
        return ok, bad


# --------------------------------------------------------------------------
# Autostart (HKCU Run + StartupApproved - exakt wie der Task-Manager)
# --------------------------------------------------------------------------
@dataclass
class StartupEntry:
    name: str
    command: str
    enabled: bool


def list_startup(backend: RegBackend) -> list[StartupEntry]:
    entries = []
    approved = {n.lower(): v for n, v, _ in backend.enum(APPROVED_KEY)}
    for name, value, rtype in backend.enum(RUN_KEY):
        flag = approved.get(name.lower())
        disabled = bool(flag) and isinstance(flag, (bytes, bytearray)) and len(flag) > 0 and flag[0] % 2 == 1
        entries.append(StartupEntry(name, str(value), not disabled))
    return sorted(entries, key=lambda e: e.name.lower())


def set_startup_enabled(backend: RegBackend, name: str, enabled: bool) -> Result:
    flag = (b"\x02" if enabled else b"\x03") + b"\x00" * 11
    try:
        backend.write(APPROVED_KEY, name, REG_BINARY, flag)
    except Exception as exc:  # noqa: BLE001
        return Result(False, f"Fehlgeschlagen: {exc}")
    now = next((e for e in list_startup(backend) if e.name == name), None)
    if now is None or now.enabled != enabled:
        return Result(False, "Verifikation fehlgeschlagen.")
    return Result(True, ("Autostart aktiviert" if enabled else "Autostart deaktiviert") + " (verifiziert). Der Eintrag bleibt erhalten und kann jederzeit umgeschaltet werden.", True)


# --------------------------------------------------------------------------
# Live-Hooks
# --------------------------------------------------------------------------
def mouse_live_hook(backend: RegBackend) -> None:
    """Uebernimmt die Maus-Registry-Werte sofort (ohne Neuanmeldung)."""
    if not IS_WINDOWS:
        return
    import ctypes  # noqa: PLC0415

    def val(name: str) -> int:
        exists, _, v = backend.read(r"Control Panel\Mouse", name)
        try:
            return int(v) if exists else 0
        except (TypeError, ValueError):
            return 0

    arr = (ctypes.c_int * 3)(val("MouseThreshold1"), val("MouseThreshold2"), val("MouseSpeed"))
    SPI_SETMOUSE, SPIF_UPDATEINIFILE, SPIF_SENDCHANGE = 0x0004, 0x01, 0x02
    ctypes.windll.user32.SystemParametersInfoW(SPI_SETMOUSE, 0, arr, SPIF_UPDATEINIFILE | SPIF_SENDCHANGE)


DEFAULT_HOOKS = {"mouse_live": mouse_live_hook}


# --------------------------------------------------------------------------
# Statusfunktionen fuer Info-Karten (nur lesen)
# --------------------------------------------------------------------------
def _ps(script: str, timeout: int = 15) -> str:
    if not IS_WINDOWS:
        return ""
    code, out = default_runner(["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive",
                                "-ExecutionPolicy", "Bypass", "-Command", script], timeout)
    return out if code == 0 else ""


def status_nvidia_driver() -> str:
    exe = shutil.which("nvidia-smi")
    if not exe:
        return "nvidia-smi nicht gefunden (Treiber nicht installiert oder nicht im Pfad)."
    code, out = default_runner([exe, "--query-gpu=name,driver_version", "--format=csv,noheader"], 6)
    return f"GPU/Treiber laut nvidia-smi: {out.splitlines()[0]}" if code == 0 and out else "nvidia-smi liefert keine Daten."


def status_ram_modules() -> str:
    out = _ps("Get-CimInstance Win32_PhysicalMemory | ForEach-Object { '{0} GB;{1};{2}' -f ([math]::Round($_.Capacity/1GB)),$_.Speed,$_.ConfiguredClockSpeed }")
    rows = []
    for line in out.splitlines():
        parts = line.strip().split(";")
        if len(parts) == 3:
            rows.append(f"{parts[0]}: Nennwert {parts[1]} MT/s, aktuell {parts[2]} MT/s")
    if not rows:
        return "RAM-Modulinfos nicht verfügbar."
    note = ""
    try:
        speeds = [(int(r.split("Nennwert ")[1].split()[0]), int(r.split("aktuell ")[1].split()[0])) for r in rows]
        if any(cur < nom for nom, cur in speeds):
            note = "\nHinweis: aktuell < Nennwert. Prüfe im BIOS, ob DOCP/XMP aktiv ist."
    except (IndexError, ValueError):
        pass
    return "\n".join(rows) + note


def status_lan() -> str:
    try:
        import psutil  # noqa: PLC0415
        stats = psutil.net_if_stats()
        lines = []
        for name, st in stats.items():
            low = name.lower()
            if st.isup and any(k in low for k in ("ethernet", "eth", "lan")) and "virtual" not in low:
                lines.append(f"{name}: verbunden, {st.speed} Mbit/s" if st.speed else f"{name}: verbunden (Geschwindigkeit unbekannt)")
        return "\n".join(lines) or "Kein aktiver LAN-Adapter erkannt."
    except Exception as exc:  # noqa: BLE001
        return f"Nicht lesbar: {exc}"


def status_hags() -> str:
    v = read_hklm(r"SYSTEM\CurrentControlSet\Control\GraphicsDrivers", "HwSchMode")
    if v is None:
        return "Hardware-beschleunigte GPU-Planung: Status nicht lesbar (nicht gesetzt oder nicht unterstützt)."
    return {2: "Hardware-beschleunigte GPU-Planung: AN", 1: "Hardware-beschleunigte GPU-Planung: AUS"}.get(int(v), f"HwSchMode={v}")


def status_cpu() -> str:
    try:
        import psutil  # noqa: PLC0415
        f = psutil.cpu_freq()
        clock = f"{f.current:.0f} MHz" if f else "Takt N/A"
        return f"{psutil.cpu_count(logical=False)} Kerne / {psutil.cpu_count(logical=True)} Threads · {clock} (Temperatur: N/A ohne Sensor-Treiber)"
    except Exception:  # noqa: BLE001
        return "CPU-Info nicht verfügbar."


# --------------------------------------------------------------------------
# Katalog
# --------------------------------------------------------------------------
def _dw(path, name, value):
    return RegValue(path, name, REG_DWORD, value)


def _sz(path, name, value):
    return RegValue(path, name, REG_SZ, value)



def msi_gpu_values(backend) -> list:
    """MSI-Modus fuer alle Grafikkarten (PCI-Geraeteklasse 'Display'), zur Laufzeit ermittelt."""
    base = r"HKLM\SYSTEM\CurrentControlSet\Enum\PCI"
    out = []
    for dev in backend.subkeys(base):
        for inst in backend.subkeys(base + "\\" + dev):
            path = base + "\\" + dev + "\\" + inst
            ok, _, cls = backend.read(path, "Class")
            if ok and str(cls).lower() == "display":
                out.append(RegValue(path + r"\Device Parameters\Interrupt Management\MessageSignaledInterruptProperties",
                                    "MSISupported", REG_DWORD, 1))
    return out


def build_catalog() -> list[Tweak]:
    C = CATEGORIES
    tweaks: list[Tweak] = [
        Tweak("game_mode", "Windows Game Mode", [C[0], C[1]],
              "Aktiviert den Windows-Spielemodus für deinen Benutzer.",
              "Windows priorisiert das Spiel und hält Update-/Treiberinstallationen zurück. Messbarer FPS-Effekt hängt vom System ab und ist oft klein.",
              values=[_dw(r"Software\Microsoft\GameBar", "AutoGameModeEnabled", 1),
                      _dw(r"Software\Microsoft\GameBar", "AllowAutoGameMode", 1)], recommended="baseline", why="Standard für Spiele, kein Risiko."),
        Tweak("game_dvr", "Game DVR / Hintergrundaufnahme aus", [C[0], C[1]],
              "Schaltet die Windows-Hintergrundaufnahme (Game DVR) für deinen Benutzer aus.",
              "Spart Aufnahme-Overhead. Nachteil: Xbox-Game-Bar-Clips und 'Letzte 30 Sekunden aufnehmen' funktionieren nicht mehr.",
              values=[_dw(r"System\GameConfigStore", "GameDVR_Enabled", 0),
                      _dw(r"Software\Microsoft\Windows\CurrentVersion\GameDVR", "AppCaptureEnabled", 0)],
              restart="nach Neuanmeldung/Neustart des Spiels", recommended="baseline", why="Entfernt Hintergrundaufnahme-Overhead."),
        Tweak("gamebar_nexus", "Game-Bar-Start per Controller aus", [C[0]],
              "Verhindert, dass ein Controller-Knopf die Xbox Game Bar öffnet.",
              "Verhindert versehentliche Game-Bar-Einblendungen während des Spiels. Keine Leistungswirkung zu erwarten.",
              values=[_dw(r"Software\Microsoft\GameBar", "UseNexusForGameBarEnabled", 0)], recommended="baseline", why="Verhindert Game-Bar-Einblendungen im Match."),
        Tweak("visualfx", "Visuelle Effekte: Beste Leistung", [C[2]],
              "Setzt die Windows-Effektvoreinstellung auf 'Für optimale Leistung anpassen'.",
              "Fensteranimationen und einige Effekte werden reduziert. Für Fortnite kaum FPS-relevant, spart nur Desktop-Ressourcen.",
              values=[_dw(r"Software\Microsoft\Windows\CurrentVersion\Explorer\VisualEffects", "VisualFXSetting", 2)],
              restart="nach Abmelden/Anmelden"),
        Tweak("transparency_off", "Transparenzeffekte aus", [C[2]],
              "Schaltet Fenster-/Taskleisten-Transparenz in Windows 11 aus.",
              "Spart etwas GPU-Last auf dem Desktop (DWM). Windows sieht flacher aus.",
              values=[_dw(r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize", "EnableTransparency", 0)]),
        Tweak("window_anim_off", "Fensteranimationen aus", [C[2]],
              "Deaktiviert Minimieren/Maximieren-Animationen.",
              "Fenster reagieren optisch direkter. Kein Einfluss auf Spiele.",
              values=[_sz(r"Control Panel\Desktop\WindowMetrics", "MinAnimate", "0")],
              restart="nach Abmelden/Anmelden"),
        Tweak("mouse_accel", "Mausbeschleunigung aus (Zeigerpräzision)", [C[3], C[1]],
              "Setzt MouseSpeed und beide Schwellenwerte auf 0 und übernimmt sie sofort.",
              "Gleichmäßigere Zeigerbewegung auf dem Desktop. Fortnite nutzt Raw Input: Der Ingame-Effekt kann null sein.",
              values=[_sz(r"Control Panel\Mouse", "MouseSpeed", "0"),
                      _sz(r"Control Panel\Mouse", "MouseThreshold1", "0"),
                      _sz(r"Control Panel\Mouse", "MouseThreshold2", "0")],
              hook="mouse_live", recommended="baseline", why="Gleichmäßige Zeigerbewegung, voll umkehrbar."),
        Tweak("sticky_keys_off", "Einrast-/Umschalt-/Anschlagverzögerung-Popups aus", [C[4], C[1]],
              "Schaltet die Tastenkombination-Popups (5x Shift usw.) aus.",
              "Verhindert, dass Windows mitten im Spiel ein Dialogfenster einblendet und das Spiel minimiert. Bedienungshilfen bleiben über Einstellungen weiter nutzbar.",
              values=[_sz(r"Control Panel\Accessibility\StickyKeys", "Flags", "506"),
                      _sz(r"Control Panel\Accessibility\ToggleKeys", "Flags", "58"),
                      _sz(r"Control Panel\Accessibility\Keyboard Response", "Flags", "122")],
              restart="nach Abmelden/Anmelden", recommended="baseline", why="Verhindert Popups, die Fortnite minimieren."),
        Tweak("power_high", "Energieplan: Höchstleistung", [C[5], C[1]],
              "Aktiviert den Windows-Energieplan 'Höchstleistung'.",
              "Verhindert aggressives Taktabsenken. Mehr Stromverbrauch und Wärme; auf manchen Notebooks/Modern-Standby-PCs nicht vorhanden. FPS-Gewinn nicht garantiert.",
              kind="power", power_target="high"),
        Tweak("power_ultimate", "Energieplan: Ultimative Leistung", [C[5]],
              "Legt den versteckten Windows-Plan 'Ultimative Leistung' an und aktiviert ihn.",
              "Wie Höchstleistung, aber ohne Mikro-Energiesparen. Beim Zurücksetzen wird der angelegte Plan wieder gelöscht.",
              kind="power", power_target="ultimate", recommended="desktop", why="Stabilere Taktung am Desktop-PC (Fallback: Höchstleistung)."),

        Tweak("ps_min_cpu", "Prozessor-Mindestzustand 100 %", [C[9], C[5], C[0], C[1]],
              "Hält die CPU-Taktung im Netzbetrieb dauerhaft oben (powercfg PROCTHROTTLEMIN).",
              "Der Ryzen taktet nicht mehr herunter und reagiert bei Lastwechseln ohne Hochtakt-Verzögerung. Mehr Leerlaufverbrauch und Wärme. Wirkung auf FPS: meist klein, auf Frametime-Spitzen eher messbar.",
              kind="powersetting", ps_sub="SUB_PROCESSOR", ps_setting="PROCTHROTTLEMIN", ps_value=100,
              recommended="desktop", why="Verhindert Takt-Absenken beim Spielen (nur Desktop)."),
        Tweak("ps_pcie_aspm_off", "PCIe-Energiesparen (ASPM) aus", [C[5], C[7], C[13]],
              "Schaltet die Link-State-Energieverwaltung für PCIe im Netzbetrieb aus.",
              "Die Verbindung zur Grafikkarte wechselt nicht in Schlafzustände, was seltene Aufwachverzögerungen vermeidet. Kaum höherer Verbrauch.",
              kind="powersetting", ps_sub="SUB_PCIE", ps_setting="ASPM", ps_value=0,
              recommended="desktop", why="Vermeidet Aufwach-Mikroruckler der GPU-Verbindung."),
        Tweak("ps_usb_suspend_off", "USB Selective Suspend aus", [C[3], C[4], C[5]],
              "Verhindert, dass Windows USB-Geräte (Maus/Tastatur) in den Ruhemodus schickt.",
              "Maus und Tastatur sind immer sofort einsatzbereit. Minimal höherer Verbrauch.",
              kind="powersetting", ps_sub="2a737441-1930-4402-8d77-b2bebba308a3",
              ps_setting="48e6b7a6-50f5-4782-a5d4-53bb8f07e226", ps_value=0,
              recommended="desktop", why="Keine Aufwachverzögerung bei Maus/Tastatur."),
        Tweak("fn_fps_limit", "Fortnite: FPS-Limit 240", [C[1]],
              "Setzt FrameRateLimit in GameUserSettings.ini auf 240 (passend zu deinem 244-Hz-Monitor).",
              "Verhindert unnötige GPU-Last über der Bildwiederholrate und glättet die Frametime. Fortnite muss beendet sein; Fortnite kann den Wert beim nächsten Speichern der Einstellungen ändern. RISEN sichert eine Kopie der Datei.",
              kind="ini", ini_section="/Script/FortniteGame.FortGameUserSettings",
              ini_values={"FrameRateLimit": "240.000000"}, recommended="fortnite",
              why="FPS-Deckel nahe der Monitor-Hz für stabile Frametimes."),
        Tweak("fn_graphics_perf", "Fortnite: Grafik auf Leistung (Schatten, Effekte, Post, Laub, Shading, GI, Reflexionen)", [C[1], C[0]],
              "Setzt die Qualitätsstufen für Schatten, Nachbearbeitung, Effekte, Laub, Shading, Global Illumination und Reflexionen auf 'Niedrig' (Wert 0).",
              "Das ist der größte Config-Hebel für FPS und 1%-Lows. Sichtweite und Texturen bleiben unverändert (Gegner früh sehen, kein Matsch). Wirkt erst nach dem nächsten Spielstart; Fortnite muss beendet sein.",
              kind="ini", ini_section=FORT_SECTION,
              ini_values={"/Script/FortniteGame.FortGameUserSettings|sg.ShadowQuality": "0", "ScalabilityGroups|sg.ShadowQuality": "0", "/Script/FortniteGame.FortGameUserSettings|sg.PostProcessQuality": "0", "ScalabilityGroups|sg.PostProcessQuality": "0", "/Script/FortniteGame.FortGameUserSettings|sg.EffectsQuality": "0", "ScalabilityGroups|sg.EffectsQuality": "0", "/Script/FortniteGame.FortGameUserSettings|sg.FoliageQuality": "0", "ScalabilityGroups|sg.FoliageQuality": "0", "/Script/FortniteGame.FortGameUserSettings|sg.ShadingQuality": "0", "ScalabilityGroups|sg.ShadingQuality": "0", "/Script/FortniteGame.FortGameUserSettings|sg.GlobalIlluminationQuality": "0", "ScalabilityGroups|sg.GlobalIlluminationQuality": "0", "/Script/FortniteGame.FortGameUserSettings|sg.ReflectionQuality": "0", "ScalabilityGroups|sg.ReflectionQuality": "0"}, recommended="fortnite",
              why="Weniger GPU-/CPU-Last durch Schatten und Effekte, sichtbar in 1%-Lows."),
        Tweak("fn_fullscreen", "Fortnite: Vollbild-Modus (nicht Fenster-Vollbild)", [C[1], C[0]],
              "Setzt FullscreenMode, PreferredFullscreenMode und LastConfirmedFullscreenMode auf 0 (Vollbild).",
              "Echtes Vollbild kann die Eingabelatenz senken und umgeht DWM-Zusatzlast. Alt-Tab ist etwas träger.",
              kind="ini", ini_section=FORT_SECTION,
              ini_values={"FullscreenMode": "0", "PreferredFullscreenMode": "0", "LastConfirmedFullscreenMode": "0"},
              recommended="fortnite", why="Direkter Vollbild-Pfad statt Fenster-Vollbild."),
        Tweak("fn_motionblur_off", "Fortnite: Bewegungsunschärfe aus", [C[1], C[0]],
              "Setzt bMotionBlur auf False.",
              "Klareres Bild beim Drehen und etwas weniger Nachbearbeitung. Reine Geschmacks- und Leistungssache.",
              kind="ini", ini_section=FORT_SECTION, ini_values={"bMotionBlur": "False"}, recommended="fortnite",
              why="Klareres Bild beim Zielen/Drehen."),
        Tweak("fn_menu_fps", "Fortnite: Menü-/Lobby-FPS auf 60", [C[1]],
              "Begrenzt die Bildrate im Menü (MenuFrameRateLimit_Unified) auf 60.",
              "Spart Strom, Wärme und Lüfterlärm in der Lobby. Kein Einfluss auf die Matches.",
              kind="ini", ini_section=FORT_SECTION, ini_values={"MenuFrameRateLimit_Unified": "60.000000"},
              why="Lobby muss nicht mit 240 FPS laufen."),
        Tweak("fn_vsync_off", "Fortnite: V-Sync aus", [C[1]],
              "Setzt bUseVSync=False in GameUserSettings.ini.",
              "V-Sync fügt Eingabelatenz hinzu. Ohne V-Sync ist Tearing möglich (mit G-SYNC/FreeSync kaum sichtbar). Fortnite muss beendet sein.",
              kind="ini", ini_section="/Script/FortniteGame.FortGameUserSettings",
              ini_values={"bUseVSync": "False"}, recommended="fortnite",
              why="V-Sync erhöht die Eingabelatenz."),
        # --- Aktionen ohne Undo ---
        Tweak("clean_temp", "Temporäre Benutzerdateien löschen", [C[11]],
              "Löscht Dateien im Benutzer-Temp-Ordner. Gesperrte Dateien werden übersprungen.",
              "Gibt Speicher frei. NICHT umkehrbar. FPS-Wirkung ist praktisch null.",
              kind="action", action_id="clean_temp", action_label="Temp jetzt bereinigen"),
        Tweak("flush_dns", "DNS-Cache leeren", [C[6], C[11]],
              "Leert den lokalen DNS-Resolver-Cache.",
              "Hilft bei Namensauflösungsproblemen. Senkt den Ingame-Ping nicht. Nicht umkehrbar, aber folgenlos (Cache baut sich neu auf).",
              kind="action", action_id="flush_dns", action_label="DNS-Cache leeren"),
        Tweak("startup_manager", "Autostart-Programme verwalten", [C[12]],
              "Einzelne Benutzer-Autostart-Einträge ein-/ausschalten (wie im Task-Manager).",
              "Weniger Hintergrundprogramme nach dem Login. Einträge werden nur umgeschaltet, nie gelöscht.",
              kind="action", action_id="open_startup", action_label="Autostart-Liste öffnen"),
        # --- Info-/Prüfkarten (keine Änderungen am System) ---
        Tweak("info_nvidia", "NVIDIA RTX: sinnvolle Einstellungen prüfen", [C[7], C[1]],
              "Keine Registry-Änderung. Einstellungen werden pro Spiel im offiziellen Treiber gesetzt.",
              "In Fortnite: NVIDIA Reflex auf 'Ein + Boost' (reduziert Render-Latenz, FPS-neutral bis leicht negativ). "
              "Im NVIDIA Control Panel → Programmeinstellungen → Fortnite: Energieverwaltung 'Maximale Leistung bevorzugen'. "
              "Bei aktivem Reflex wird 'Niedrige Latenz' ignoriert. Texturen mittel halten: die 8-GB-Karte läuft mit PCIe 3.0 (Ryzen 5 5500) auf x8, VRAM-Überlauf kostet dort besonders viel.",
              kind="info", status_fn=status_nvidia_driver, open_uri="https://www.nvidia.com/Download/index.aspx",
              open_label="NVIDIA-Treiberseite"),
        Tweak("info_amd", "AMD Ryzen/Radeon: Hinweise", [C[8]],
              "Keine Registry-Änderung.",
              "Für den Ryzen 5 5500: AMD-Chipsatztreiber aktuell halten (installiert die AMD-Energiepläne) und das BIOS des Gigabyte A520M K V2 über die Gigabyte-Seite prüfen. "
              "Radeon-Software ist bei einer NVIDIA-GPU nicht relevant.",
              kind="info", status_fn=status_cpu, open_uri="https://www.amd.com/en/support", open_label="AMD-Support"),
        Tweak("info_cpu", "CPU: Fortnite-Priorität & Hintergrundlast", [C[9]],
              "Keine dauerhafte Änderung.",
              "Der Ryzen 5 5500 (6 Kerne) ist in Fortnite oft der Engpass. Hilfreicher als Tweaks: unnötige Hintergrundprogramme schließen (Prozess-Manager) und Fortnite nur auf 'Hoch' setzen (Fortnite-Seite), niemals 'Echtzeit'. CPU-Temperatur ist ohne Sensor-Treiber nicht auslesbar und wird deshalb als N/A gezeigt.",
              kind="info", status_fn=status_cpu),
        Tweak("info_ram", "RAM: DOCP/XMP prüfen", [C[10]],
              "Keine Änderung. Prüft nur, ob der RAM mit Nenntakt läuft.",
              "Läuft der Arbeitsspeicher unter dem Nenntakt, ist im BIOS meist DOCP/XMP aus. Mit 64 GB ist Speichermenge kein Problem; für Fortnite zählen Takt/Timings (Zen 3 profitiert von DDR4-3200+). BIOS-Änderungen immer selbst und bewusst vornehmen.",
              kind="info", status_fn=status_ram_modules),
        Tweak("info_lan", "LAN-Verbindung prüfen", [C[6]],
              "Keine Änderung. Zeigt Link-Status und Geschwindigkeit.",
              "Erwartung bei Ethernet: 1000 Mbit/s oder mehr. 100 Mbit/s deutet meist auf Kabel/Port hin. TCP-/Nagle-/Registry-Netzwerk-Hacks werden bewusst nicht angeboten: kein belegter Nutzen, Risiko für Verbindungsprobleme.",
              kind="info", status_fn=status_lan),
        Tweak("info_hags", "Hardware-beschleunigte GPU-Planung (nur Anzeige)", [C[13], C[7]],
              "Reiner Statusblick (HKLM, nur lesen).",
              "Ändern kannst du das in Windows: Einstellungen → System → Anzeige → Grafik → Standard-Grafikeinstellungen. Der Effekt hängt von Treiber und Spiel ab (Frame Generation setzt es teils voraus). RISEN ändert diesen Wert absichtlich nicht.",
              kind="info", status_fn=status_hags, open_uri="ms-settings:display-advancedgraphics",
              open_label="Grafik-Einstellungen"),
        # ---------------- V3.3: Tweaks mit Adminrechten (HKLM) und weitere Latenz-Tweaks ----------------
        Tweak("net_throttle_off", "Netzwerk-Drosselung & Multimedia-Reaktionszeit", [C[6], C[0], C[13]],
              "Schaltet die Multimedia-Netzwerkdrosselung ab und setzt die Systemreaktionszeit auf 0 %.",
              "Windows drosselt standardmäßig Netzwerkpakete, solange Multimedia-Programme laufen. Der Tweak entfernt das; Hintergrunddienste bekommen dafür minimal weniger CPU-Zeit. Wirkung: weniger Mikroruckler bei gleichzeitigem Streaming/Musik, kleiner Effekt ohne Hintergrundlast.",
              values=[_dw(r"HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia\SystemProfile", "NetworkThrottlingIndex", 4294967295),
                      _dw(r"HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia\SystemProfile", "SystemResponsiveness", 0)],
              admin=True, restart="nach Neustart", recommended="baseline",
              why="Weniger Netzwerk-/Scheduler-Drosselung beim Spielen, gut belegt und umkehrbar."),
        Tweak("games_task", "Spiele-Scheduler: GPU-/CPU-Priorität hoch", [C[0], C[1], C[9]],
              "Setzt für die Multimedia-Aufgabe 'Games' hohe GPU- und CPU-Priorität und hohe Scheduling-Kategorie.",
              "Windows plant Spiele-Threads bevorzugt ein (MMCSS). Das hilft vor allem bei Hintergrundlast und 1%-Lows; bei freiem System bleibt der Effekt klein.",
              values=[_dw(r"HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia\SystemProfile\Tasks\Games", "GPU Priority", 8),
                      _dw(r"HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia\SystemProfile\Tasks\Games", "Priority", 6),
                      _sz(r"HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia\SystemProfile\Tasks\Games", "Scheduling Category", "High"),
                      _sz(r"HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia\SystemProfile\Tasks\Games", "SFIO Priority", "High")],
              admin=True, restart="nach Neustart", recommended="baseline",
              why="Spiele werden vom Windows-Scheduler bevorzugt."),
        Tweak("win32_prio", "Prozess-Scheduling auf Vordergrund-Programme", [C[9], C[0]],
              "Setzt Win32PrioritySeparation auf 0x26 (kurze, feste Zeitscheiben, starke Vordergrund-Bevorzugung).",
              "Das aktive Fenster (dein Spiel) bekommt bevorzugt Rechenzeit; Hintergrundprogramme etwas weniger. Auf Desktop-PCs gängig, Wirkung messbar vor allem bei Hintergrundlast.",
              values=[_dw(r"HKLM\SYSTEM\CurrentControlSet\Control\PriorityControl", "Win32PrioritySeparation", 38)],
              admin=True, restart="nach Neustart", recommended="desktop",
              why="Vordergrund-Spiel bekommt Rechenzeit vor Hintergrundprogrammen."),
        Tweak("power_throttling_off", "Windows Power Throttling aus", [C[5], C[9]],
              "Verhindert, dass Windows Programme per Power Throttling auf Effizienzkerne/niedrigen Takt drosselt.",
              "Mehr konstante Leistung für Spiel und Launcher, minimal höherer Stromverbrauch. Auf Notebooks nicht automatisch empfohlen (Akkulaufzeit).",
              values=[_dw(r"HKLM\SYSTEM\CurrentControlSet\Control\Power\PowerThrottling", "PowerThrottlingOff", 1)],
              admin=True, restart="nach Neustart", recommended="desktop",
              why="Konstante Taktung am Desktop-PC."),
        Tweak("fso_global", "Vollbild-Optimierungen: Exklusiv-Vollbild-Verhalten", [C[0], C[1], C[13]],
              "Setzt das Windows-Vollbildverhalten für Spiele auf 'echtes Vollbild' statt DWM-Vollbild-Optimierung.",
              "Kann Eingabelatenz in DirectX-Spielen senken. Alt-Tab kann dadurch etwas langsamer werden. Fortnite im Vollbild-Modus (nicht 'Fenster-Vollbild') nutzen.",
              values=[_dw(r"System\GameConfigStore", "GameDVR_FSEBehaviorMode", 2),
                      _dw(r"System\GameConfigStore", "GameDVR_HonorUserFSEBehaviorMode", 1),
                      _dw(r"System\GameConfigStore", "GameDVR_DXGIHonorFSEWindowsCompatible", 1)],
              restart="beim nächsten Spielstart", recommended="competitive",
              why="Direkterer Vollbild-Pfad für DirectX-Spiele."),
        Tweak("keyboard_fast", "Tastaturwiederholung: schnellste Einstellung", [C[4]],
              "Setzt Wiederholverzögerung auf kürzeste Stufe und Wiederholrate auf Maximum.",
              "Gehaltene Tasten reagieren schneller. Wird auf dem Desktop nach der nächsten Anmeldung sicher aktiv.",
              values=[_sz(r"Control Panel\Keyboard", "KeyboardDelay", "0"),
                      _sz(r"Control Panel\Keyboard", "KeyboardSpeed", "31")],
              restart="nach Abmelden/Anmelden", recommended="competitive",
              why="Schnellere Tastenwiederholung, völlig harmlos."),
        Tweak("bg_apps_off", "Hintergrund-Apps aus", [C[2], C[9], C[10]],
              "Verbietet Store-Apps, im Hintergrund zu laufen.",
              "Spart CPU, RAM und Netzwerk im Hintergrund. Benachrichtigungen mancher Store-Apps kommen nur noch beim Öffnen.",
              values=[_dw(r"Software\Microsoft\Windows\CurrentVersion\BackgroundAccessApplications", "GlobalUserDisabled", 1),
                      _dw(r"Software\Microsoft\Windows\CurrentVersion\Search", "BackgroundAppGlobalToggle", 0)],
              restart="nach Abmelden/Anmelden", why="Weniger Hintergrundlast."),
        Tweak("telemetry_policy", "Telemetrie auf Minimum (Richtlinie)", [C[2], C[13]],
              "Setzt die Diagnosedaten-Richtlinie auf 'Sicherheit/Minimum' (Windows Home nutzt 'Erforderlich').",
              "Weniger Hintergrund-Datenversand. Kein Einfluss auf Windows Update oder Sicherheit. Auf Home-Editionen greift nur die niedrigste erlaubte Stufe.",
              values=[_dw(r"HKLM\SOFTWARE\Policies\Microsoft\Windows\DataCollection", "AllowTelemetry", 0)],
              admin=True, restart="nach Neustart", why="Weniger Telemetrie-Hintergrundlast."),
        Tweak("diagtrack_off", "Telemetrie-Dienst (DiagTrack) deaktivieren", [C[2], C[13], C[0]],
              "Stellt den Dienst 'Benutzererfahrungen und Telemetrie im verbundenen Modus' auf 'Deaktiviert'.",
              "Der Dienst sammelt und sendet Diagnosedaten im Hintergrund. Ohne ihn: etwas weniger Disk-/Netzlast. Umkehrbar per Undo (Starttyp wird zurückgesetzt).",
              values=[_dw(r"HKLM\SYSTEM\CurrentControlSet\Services\DiagTrack", "Start", 4)],
              admin=True, restart="nach Neustart", why="Weniger Hintergrunddienste."),
        Tweak("delivery_opt_off", "Windows-Updates nicht über das Internet teilen", [C[6], C[2]],
              "Deaktiviert Delivery Optimization (Updates von/zu anderen PCs).",
              "Verhindert, dass dein PC Update-Daten hochlädt und deine Leitung belastet. Updates laden weiter normal von Microsoft.",
              values=[_dw(r"HKLM\SOFTWARE\Policies\Microsoft\Windows\DeliveryOptimization", "DODownloadMode", 0)],
              admin=True, restart="nach Neustart", why="Kein Upload im Hintergrund = stabilerer Ping."),
        Tweak("msi_gpu", "GPU im MSI-Modus (Message Signaled Interrupts)", [C[7], C[8], C[13]],
              "Aktiviert den MSI-Modus für alle erkannten Grafikkarten (PCI-Klasse Display).",
              "MSI verteilt Interrupts effizienter als der alte Line-Based-Modus und kann DPC-Latenz senken. Aktuelle NVIDIA-RTX-Treiber nutzen MSI oft schon; dann ändert sich nichts. Wirkt erst nach Neustart. Undo entfernt den Wert wieder.",
              dynamic=msi_gpu_values, admin=True, restart="nach Neustart",
              why="Niedrigere GPU-Interrupt-Latenz."),
    ]
    return tweaks


def by_category(tweaks: list[Tweak], category: str) -> list[Tweak]:
    return [t for t in tweaks if category in t.categories]


# --------------------------------------------------------------------------
# "Best Tweaks for my PC"
# --------------------------------------------------------------------------
def applicable(t: Tweak, ctx: dict) -> tuple[bool, str]:
    if t.recommended == "desktop" and not ctx.get("desktop", True):
        return False, "Notebook erkannt: Dauer-Hochtakt/Energiesparen aus wird nicht automatisch empfohlen."
    if t.admin and not ctx.get("admin", True):
        return False, "Braucht Administratorrechte (RISEN als Administrator starten)."
    if t.kind == "ini":
        if not ctx.get("fortnite_config", False):
            return False, "Fortnite-Config nicht gefunden (Fortnite einmal starten)."
        if ctx.get("fortnite_running", False):
            return False, "Fortnite läuft (zum Anwenden beenden)."
    return True, ""


def recommend(catalog: list[Tweak], engine: "TweakEngine", ctx: dict) -> tuple[list[Tweak], list[tuple[Tweak, str]]]:
    """Waehlt aus dem Katalog die fuer dieses System sinnvollen, noch nicht aktiven Tweaks."""
    chosen: list[Tweak] = []
    skipped: list[tuple[Tweak, str]] = []
    for t in catalog:
        if t.kind not in TOGGLE_KINDS or not t.recommended:
            continue
        ok, reason = applicable(t, ctx)
        if not ok:
            skipped.append((t, reason))
            continue
        state, _ = engine.status(t)
        if state == STATUS_ACTIVE:
            continue
        if state == STATUS_UNKNOWN and t.kind in ("powersetting", "ini"):
            skipped.append((t, "Auf diesem System nicht verfügbar."))
            continue
        chosen.append(t)
    return chosen, skipped


# --------------------------------------------------------------------------
# Profile fuer den Auto-Modus (V3.2)
# --------------------------------------------------------------------------
PROFILES = {
    "safe": "Nur Basis-Tweaks (Game Mode, Game DVR aus, Maus, Popups aus). Minimales Risiko.",
    "competitive": "Alles Empfohlene fuer dein System (Basis + Desktop-Energie + Fortnite-Config).",
    "maxfps": "Competitive + Effekte aus, Hintergrund-Apps/Telemetrie aus, GPU-MSI-Modus (teils Adminrechte).",
}
COSMETIC_IDS = ("visualfx", "transparency_off", "window_anim_off")
MAXFPS_EXTRA_IDS = ("bg_apps_off", "telemetry_policy", "diagtrack_off", "delivery_opt_off", "msi_gpu")


def select_for_profile(catalog: list[Tweak], engine: "TweakEngine", ctx: dict,
                       profile: str = "competitive") -> tuple[list[Tweak], list[tuple[Tweak, str]]]:
    """Waehlt Tweaks fuer ein Profil. Bereits aktive Tweaks werden nie erneut gewaehlt."""
    if profile not in PROFILES:
        profile = "competitive"
    chosen, skipped = recommend(catalog, engine, ctx)
    if profile == "safe":
        skipped = skipped + [(t, "Nicht im Profil SAFE.") for t in chosen if t.recommended != "baseline"]
        chosen = [t for t in chosen if t.recommended == "baseline"]
    elif profile == "maxfps":
        have = {t.id for t in chosen}
        for t in catalog:
            if t.id in COSMETIC_IDS + MAXFPS_EXTRA_IDS and t.id not in have and t.kind in TOGGLE_KINDS:
                ok, reason = applicable(t, ctx)
                if not ok:
                    skipped.append((t, reason))
                    continue
                state = engine.status(t)[0]
                if state != STATUS_ACTIVE and state != STATUS_UNKNOWN:
                    chosen.append(t)
    return chosen, skipped
