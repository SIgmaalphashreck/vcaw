import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import fnmon  # noqa: E402
import netlab  # noqa: E402
import tweaklib as tl  # noqa: E402

EN = "Reply from 1.1.1.1: bytes=32 time=12ms TTL=57"
DE = "Antwort von 1.1.1.1: Bytes=32 Zeit=14ms TTL=57"
DE_FAST = "Antwort von 192.168.0.1: Bytes=32 Zeit<1ms TTL=64"
TIMEOUT_EN = "Request timed out."
TIMEOUT_DE = "Zeitüberschreitung der Anforderung."
UNREACH = "Reply from 192.168.0.5: Destination host unreachable."


class PingParse(unittest.TestCase):
    def test_english(self):
        self.assertEqual(netlab.parse_ping_reply(EN), 12.0)

    def test_german(self):
        self.assertEqual(netlab.parse_ping_reply(DE), 14.0)

    def test_sub_ms(self):
        self.assertEqual(netlab.parse_ping_reply(DE_FAST), 0.5)

    def test_no_reply(self):
        for text in (TIMEOUT_EN, TIMEOUT_DE, UNREACH):
            self.assertIsNone(netlab.parse_ping_reply(text))

    def test_jitter(self):
        self.assertAlmostEqual(netlab.calc_jitter([10, 12, 11, 15]), (2 + 1 + 4) / 3)
        self.assertIsNone(netlab.calc_jitter([10]))

    def test_result_loss(self):
        r = netlab.PingResult("x", sent=10, samples=[10.0] * 9)
        self.assertAlmostEqual(r.loss_pct, 10.0)
        self.assertIn("Verlust 10%", r.summary())
        self.assertIn("keine Antwort", netlab.PingResult("x", sent=5).summary())

    def test_netipconfig(self):
        raw = json.dumps({
            "InterfaceAlias": "Ethernet", "InterfaceDescription": "Realtek",
            "IPv4Address": ["192.168.0.20"],
            "IPv4DefaultGateway": [{"NextHop": "192.168.0.1"}],
            "DNSServer": [{"ServerAddresses": ["1.1.1.1", "fe80::1"]}],
            "NetAdapter": {"Status": "Up", "LinkSpeed": "1 Gbps"},
        })
        cfg = netlab.parse_netipconfig(raw)
        self.assertEqual(cfg[0]["gateway"], ["192.168.0.1"])
        self.assertEqual(cfg[0]["dns"], ["1.1.1.1"])
        self.assertEqual(cfg[0]["link_speed"], "1 Gbps")
        self.assertEqual(netlab.parse_netipconfig("kein json"), [])


class FrameStats(unittest.TestCase):
    def test_constant(self):
        s = fnmon.compute_frame_stats([4.0] * 1000)
        self.assertAlmostEqual(s.avg_fps, 250.0)
        self.assertAlmostEqual(s.low1_fps, 250.0)
        self.assertEqual(s.stutters, 0)

    def test_one_percent_low(self):
        # 990 Frames mit 4 ms, 10 Frames mit 20 ms -> 1 % Low = 50 FPS
        s = fnmon.compute_frame_stats([4.0] * 990 + [20.0] * 10)
        self.assertAlmostEqual(s.low1_fps, 50.0)
        self.assertEqual(s.stutters, 10)
        self.assertEqual(s.max_ms, 20.0)

    def test_too_few(self):
        self.assertIsNone(fnmon.compute_frame_stats([4.0] * 10))

    def test_csv_v1_and_v2(self):
        with tempfile.TemporaryDirectory() as d:
            p1 = Path(d) / "v1.csv"
            p1.write_text("Application,ProcessID,MsBetweenPresents\nf.exe,1,4.0\nf.exe,1,5.5\n")
            self.assertEqual(fnmon.parse_presentmon_csv(p1), [4.0, 5.5])
            p2 = Path(d) / "v2.csv"
            p2.write_text("Application,ProcessID,FrameTime,CPUBusy\nf.exe,1,4.2,1\n")
            self.assertEqual(fnmon.parse_presentmon_csv(p2), [4.2])
            p3 = Path(d) / "bad.csv"
            p3.write_text("a,b\n1,2\n")
            self.assertEqual(fnmon.parse_presentmon_csv(p3), [])


class FakePower:
    """Simuliert powercfg."""
    BALANCED = "381b4222-f694-41f0-9685-ff5bb260df2e"

    def __init__(self, ultimate_ok=True):
        self.active = self.BALANCED
        self.schemes = {self.BALANCED, tl.HIGH_PERF_GUID}
        self.ultimate_ok = ultimate_ok
        self.counter = 0

    def __call__(self, args, timeout=12):
        if args[1] == "/getactivescheme":
            return 0, f"Power Scheme GUID: {self.active}  (Plan)"
        if args[1] == "/setactive":
            target = tl.HIGH_PERF_GUID if args[2] == "SCHEME_MIN" else args[2]
            if target in self.schemes:
                self.active = target
                return 0, ""
            return 1, "Invalid"
        if args[1] == "-duplicatescheme":
            if not self.ultimate_ok:
                return 1, "Der Wert ist nicht vorhanden."
            self.counter += 1
            guid = f"aaaaaaaa-0000-0000-0000-00000000000{self.counter}"
            self.schemes.add(guid)
            return 0, f"Power Scheme GUID: {guid}  (Ultimate Performance)"
        if args[1] == "/delete":
            self.schemes.discard(args[2])
            return 0, ""
        return 1, "?"


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "state.json"
        self.reg = tl.MemoryBackend()
        self.power = FakePower()
        self.engine = tl.TweakEngine(self.reg, self.state, runner=self.power)
        self.catalog = {t.id: t for t in tl.build_catalog()}

    def tearDown(self):
        self.tmp.cleanup()

    def test_apply_verify_undo_existing_value(self):
        t = self.catalog["game_dvr"]
        self.reg.write(r"System\GameConfigStore", "GameDVR_Enabled", tl.REG_DWORD, 1)
        self.assertEqual(self.engine.status(t)[0], tl.STATUS_INACTIVE)
        r = self.engine.apply(t)
        self.assertTrue(r.ok and r.verified, r.message)
        self.assertEqual(self.engine.status(t)[0], tl.STATUS_ACTIVE)
        self.assertEqual(self.reg.read(r"System\GameConfigStore", "GameDVR_Enabled")[2], 0)
        u = self.engine.undo(t)
        self.assertTrue(u.ok, u.message)
        self.assertEqual(self.reg.read(r"System\GameConfigStore", "GameDVR_Enabled")[2], 1)
        # AppCaptureEnabled existierte nicht -> muss wieder geloescht sein
        self.assertFalse(self.reg.read(r"Software\Microsoft\Windows\CurrentVersion\GameDVR", "AppCaptureEnabled")[0])
        self.assertFalse(self.engine.has_backup("game_dvr"))

    def test_backup_survives_restart_and_not_overwritten(self):
        t = self.catalog["mouse_accel"]
        self.reg.write(r"Control Panel\Mouse", "MouseSpeed", tl.REG_SZ, "1")
        self.engine.apply(t)
        self.engine.apply(t)  # zweites Apply darf das Original nicht ueberschreiben
        engine2 = tl.TweakEngine(self.reg, self.state, runner=self.power)
        self.assertTrue(engine2.undo(t).ok)
        self.assertEqual(self.reg.read(r"Control Panel\Mouse", "MouseSpeed")[2], "1")

    def test_undo_without_backup_is_refused(self):
        r = self.engine.undo(self.catalog["game_mode"])
        self.assertFalse(r.ok)
        self.assertIn("Kein Backup", r.message)

    def test_verification_failure_rolls_back(self):
        class Lying(tl.MemoryBackend):
            def write(self, path, name, rtype, value):
                if name == "AutoGameModeEnabled":
                    return  # Schreibversuch wird verschluckt
                super().write(path, name, rtype, value)
        reg = Lying()
        eng = tl.TweakEngine(reg, self.state, runner=self.power)
        r = eng.apply(self.catalog["game_mode"])
        self.assertFalse(r.ok)
        self.assertIn("Verifikation fehlgeschlagen", r.message)
        self.assertFalse(eng.has_backup("game_mode"))

    def test_partial_status(self):
        t = self.catalog["game_mode"]
        self.reg.write(r"Software\Microsoft\GameBar", "AutoGameModeEnabled", tl.REG_DWORD, 1)
        self.assertEqual(self.engine.status(t)[0], tl.STATUS_PARTIAL)

    def test_power_high_roundtrip(self):
        t = self.catalog["power_high"]
        self.assertTrue(self.engine.apply(t).ok)
        self.assertEqual(self.engine.status(t)[0], tl.STATUS_ACTIVE)
        self.assertTrue(self.engine.undo(t).ok)
        self.assertEqual(self.power.active, FakePower.BALANCED)

    def test_power_ultimate_roundtrip_and_cleanup(self):
        t = self.catalog["power_ultimate"]
        r = self.engine.apply(t)
        self.assertTrue(r.ok, r.message)
        made = self.engine.state["power_ultimate"]["created_scheme"]
        self.assertEqual(self.engine.status(t)[0], tl.STATUS_ACTIVE)
        self.assertTrue(self.engine.undo(t).ok)
        self.assertNotIn(made, self.power.schemes)
        self.assertEqual(self.power.active, FakePower.BALANCED)

    def test_power_ultimate_unavailable(self):
        self.power.ultimate_ok = False
        r = self.engine.apply(self.catalog["power_ultimate"])
        self.assertFalse(r.ok)
        self.assertEqual(self.power.active, FakePower.BALANCED)
        self.assertFalse(self.engine.has_backup("power_ultimate"))

    def test_hook_called_on_apply_and_undo(self):
        calls = []
        eng = tl.TweakEngine(self.reg, self.state, runner=self.power, hooks={"mouse_live": lambda b: calls.append(1)})
        eng.apply(self.catalog["mouse_accel"])
        eng.undo(self.catalog["mouse_accel"])
        self.assertEqual(len(calls), 2)

    def test_startup_toggle(self):
        self.reg.write(tl.RUN_KEY, "Discord", tl.REG_SZ, r"C:\x\Discord.exe")
        self.reg.write(tl.RUN_KEY, "Steam", tl.REG_SZ, r"C:\x\steam.exe")
        entries = {e.name: e for e in tl.list_startup(self.reg)}
        self.assertTrue(entries["Discord"].enabled)
        r = tl.set_startup_enabled(self.reg, "Discord", False)
        self.assertTrue(r.ok and r.verified)
        entries = {e.name: e for e in tl.list_startup(self.reg)}
        self.assertFalse(entries["Discord"].enabled)
        self.assertTrue(entries["Steam"].enabled)
        self.assertTrue(self.reg.read(tl.RUN_KEY, "Discord")[0])  # Eintrag wurde nicht geloescht
        self.assertTrue(tl.set_startup_enabled(self.reg, "Discord", True).ok)

    def test_catalog_integrity(self):
        cat = tl.build_catalog()
        ids = [t.id for t in cat]
        self.assertEqual(len(ids), len(set(ids)))
        for t in cat:
            self.assertTrue(t.name and t.description and t.impact, t.id)
            for c in t.categories:
                self.assertIn(c, tl.CATEGORIES)
            if t.kind == "registry":
                self.assertTrue(t.values or t.dynamic)
        for c in tl.CATEGORIES:
            self.assertTrue(tl.by_category(cat, c), f"Kategorie leer: {c}")
        # Sicherheitsregeln: keine verbotenen Bereiche im Katalog
        banned = ("defender", "firewall", "windowsupdate", "hpet", "tcpack", "nagle",
                  "easyanticheat", "battleye", "bcdedit", "tcpnodelay")
        blob = json.dumps([[v.path.lower(), v.name.lower()] for t in cat for v in t.values])
        for word in banned:
            self.assertNotIn(word, blob)


if __name__ == "__main__":
    unittest.main(verbosity=2)
