"""Smoke-Test ohne Windows/GUI: ersetzt customtkinter/tkinter durch Mocks und
fuehrt den kompletten UI-Aufbau sowie die neuen Handler aus. Prueft NUR, dass
der Code ohne NameError/AttributeError/TypeError durchlaeuft - kein visueller Test."""
import sys, types, threading, time
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

class Base:
    def __init__(self, *a, **k): pass
    def after(self, ms, fn=None, *a):
        if fn and ms == 0:
            fn()
        return "id"
    def __getattr__(self, name):
        m = MagicMock(name=name)
        m.winfo_children.return_value = []
        return m

ctk = MagicMock()
ctk.CTk = Base
sys.modules["customtkinter"] = ctk
try:
    import tkinter  # noqa
except Exception:
    tk = MagicMock()
    sys.modules["tkinter"] = tk
    sys.modules["tkinter.ttk"] = tk.ttk
    sys.modules["tkinter.messagebox"] = tk.messagebox
    sys.modules["tkinter.filedialog"] = tk.filedialog

class FakeVar:
    def __init__(self, *a, value=None, **k): self.v = value if value is not None else ""
    def get(self): return self.v
    def set(self, v): self.v = v
    def trace_add(self, *a, **k): pass

class FakeEntry:
    def get(self): return ""
    def __getattr__(self, name): return lambda *a, **k: None

import tkinter as _tk
_tk.StringVar = FakeVar
_tk.BooleanVar = FakeVar
ctk.CTkEntry = lambda *a, **k: FakeEntry()
sys.modules["ui_widgets"] = MagicMock()  # echte Canvas-Widgets brauchen ein Display
import tweaklib
tweaklib.WinRegBackend = tweaklib.MemoryBackend

import main
main.messagebox = MagicMock()
main.messagebox.askyesno.return_value = True
main.APP_DIR = Path("/tmp/risen_smoke"); main.APP_DIR.mkdir(exist_ok=True)
main.TWEAK_STATE_FILE = main.APP_DIR / "tweak_state.json"
if main.TWEAK_STATE_FILE.exists(): main.TWEAK_STATE_FILE.unlink()

app = main.RisenTweaks.__new__(main.RisenTweaks)
# __init__ ruft periodic/after etc. -> normal ausfuehren
main.RisenTweaks.__init__(app)
app.engine.runner = lambda args, timeout=12: (1, "no powercfg in test")

errors = []
def step(name, fn):
    try:
        fn()
        print("ok  ", name)
    except Exception as exc:
        errors.append((name, exc)); print("FAIL", name, repr(exc))

app.ensure_built("Control Center")
for cat in main.tweaklib.CATEGORIES:
    step(f"render {cat}", lambda c=cat: (setattr(app, "tl_category", c), app.tl_render()))
step("apply game_mode", lambda: app.engine_apply_dialog("game_mode"))
calls = []
main.run_cmd = lambda args, timeout=12: (calls.append(args) or (0, ""))
app.cc_vars = {}
def cc_flow():
    app.tl_category = "Gaming Optimization"; app.tl_render(); time.sleep(0.3)
    app.cc_toggle("game_dvr", True); app.cc_toggle("mouse_accel", True)
    plan = app.cc_plan(); assert {t.id for t, _ in plan} == {"game_dvr", "mouse_accel"}, plan
    app.cc_apply(restart=True); time.sleep(1.0)
    assert any(c[:2] == ["shutdown", "/r"] for c in calls), calls
    assert app.cc_busy is False
    assert app.engine.has_backup("game_dvr") and app.engine.has_backup("mouse_accel")
    # Undo per Schalter
    app.cc_active["game_dvr"] = True
    app.cc_toggle("game_dvr", False); app.cc_apply(False); time.sleep(1.0)
    assert not app.engine.has_backup("game_dvr")
step("control center flow", cc_flow)
def best_flow():
    import tweaklib as T
    app.engine.runner = lambda args, timeout=12: (1, "n/a")
    app.cc_best(); time.sleep(1.5)
step("best tweaks flow", best_flow)

def pages_flow():
    for name in list(app.PAGE_BUILDERS):
        app.show_page(name)
    time.sleep(0.5)
    assert set(app.PAGE_BUILDERS) <= app._built
step("alle Seiten oeffnen (lazy build)", pages_flow)

def tw_flow():
    app.show_page("Tweaks"); time.sleep(0.5)
    assert app.tw_states, "keine Status gelesen"
    for view in [app.TW_VIEW_REC, app.TW_VIEW_ACTIVE, "Gaming", "Fortnite", "Netzwerk", "CPU"]:
        app.tw_set_view(view)
    app.tw_set_view("Gaming")
    app.tw_set("game_dvr", True); time.sleep(0.6)
    assert app.engine.has_backup("game_dvr"), "tw_set hat nicht angewendet"
    app.tw_set("game_dvr", False); time.sleep(0.6)
    assert not app.engine.has_backup("game_dvr"), "tw_set hat nicht zurueckgesetzt"
    app.engine.runner = lambda args, timeout=12: (1, "n/a")
    app._tw_quiet_cleanup = lambda: "cleanup-mock"
    app.tw_auto("safe"); time.sleep(1.5)
    assert app.tw_busy is False
    assert app.engine.has_backup("game_mode"), "AUTO hat game_mode nicht gesetzt"
step("tweaks tab flow", tw_flow)

def home_flow():
    app.show_page("Home")
    d = app._collect_live()
    assert "cpu" in d and "ram" in d and "fn" in d
    app.apply_live(d)
    app.refresh_live(); time.sleep(0.5)
step("home live", home_flow)

def proc_flow():
    app.show_page("Processes"); time.sleep(1.0)
    for f in ("Alle", "Hohe Last", "Fortnite/Epic"):
        app.set_proc_filter(f); time.sleep(0.5)
    for k in ("cpu", "memory", "name"):
        app.set_process_sort(k); time.sleep(0.4)
step("process manager", proc_flow)

def fn_flow():
    app.show_page("Fortnite"); time.sleep(0.4)
    app.fn_optimize()
step("fortnite page", fn_flow)
step("apply mouse", lambda: app.engine_apply_dialog("mouse_accel"))
time.sleep(0.5)
step("backup label", lambda: app.update_backup_label())
step("undo game_mode", lambda: app.engine_undo_dialog("game_mode"))
step("restore_all", lambda: app.restore_all())
step("startup toggle", lambda: (app.engine.backend.write(main.tweaklib.RUN_KEY, "Foo", 1, "x.exe"), app.tl_toggle_startup("Foo", False)))
step("nl_refresh_config", app.nl_refresh_config)
step("nl_dns_epic", app.nl_dns_epic); time.sleep(3)
step("fn_background", app.fn_background); time.sleep(2)
step("fn_capture (no presentmon)", app.fn_capture)
step("run_ping_test", lambda: app.run_ping_test("127.0.0.1", "Loopback")); time.sleep(4)
step("hw accept", lambda: app._accept_hardware({"CPU": {"Name": "Ryzen", "NumberOfCores": 6, "NumberOfLogicalProcessors": 12}, "Mem": [{"Capacity": 34359738368, "Speed": 3200, "ConfiguredClockSpeed": 2133}]}))
step("gpu detail", lambda: print(main.safe_gpu_detail({"available": True, "name": "RTX", "usage": 10, "mem_used": 1, "mem_total": 8, "temp": 40, "power": 50, "clock": 2500, "mem_clock": 14000}, True)))
print("ERRORS:", errors)
sys.exit(1 if errors else 0)
