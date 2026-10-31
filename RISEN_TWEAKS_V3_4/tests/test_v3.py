import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import tweaklib as tl  # noqa: E402

SECTION = "/Script/FortniteGame.FortGameUserSettings"
INI_TEXT = (
    "[Other]\r\nFoo=1\r\n\r\n"
    f"[{SECTION}]\r\nFrameRateLimit=0.000000\r\nbUseVSync=True\r\nKeep=me\r\n\r\n[Tail]\r\nX=2\r\n"
)


class FakePowercfg:
    """Simuliert powercfg inkl. /query und /setacvalueindex (EN-Format)."""
    ACTIVE = "381b4222-f694-41f0-9685-ff5bb260df2e"

    def __init__(self):
        self.values = {("SUB_PROCESSOR", "PROCTHROTTLEMIN"): 5, ("SUB_PCIE", "ASPM"): 2}
        self.dc = 7
        self.fail_set = False

    def __call__(self, args, timeout=12):
        cmd = args[1]
        if cmd == "/getactivescheme":
            return 0, f"Power Scheme GUID: {self.ACTIVE}  (Balanced)"
        if cmd == "/query":
            key = (args[3], args[4])
            if key not in self.values:
                return 1, "Der Wert ist nicht vorhanden."
            ac = self.values[key]
            return 0, ("Subgroup\n  Minimum Possible Setting: 0x00000000\n  Maximum Possible Setting: 0x00000064\n"
                       f"  Current AC Power Setting Index: 0x{ac:08x}\n  Current DC Power Setting Index: 0x{self.dc:08x}")
        if cmd == "/setacvalueindex":
            if self.fail_set:
                return 1, "Zugriff verweigert"
            self.values[(args[3], args[4])] = int(args[5])
            return 0, ""
        if cmd == "/setactive":
            return 0, ""
        return 1, "?"


class PowerSettingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fake = FakePowercfg()
        self.engine = tl.TweakEngine(tl.MemoryBackend(), Path(self.tmp.name) / "s.json", runner=self.fake,
                                     ini_path=Path(self.tmp.name) / "none.ini")
        self.cat = {t.id: t for t in tl.build_catalog()}

    def tearDown(self):
        self.tmp.cleanup()

    def test_roundtrip(self):
        t = self.cat["ps_min_cpu"]
        self.assertEqual(self.engine.status(t)[0], tl.STATUS_INACTIVE)
        r = self.engine.apply(t)
        self.assertTrue(r.ok and r.verified, r.message)
        self.assertEqual(self.fake.values[("SUB_PROCESSOR", "PROCTHROTTLEMIN")], 100)
        self.assertEqual(self.engine.status(t)[0], tl.STATUS_ACTIVE)
        self.assertEqual(self.fake.dc, 7)  # Akkubetrieb bleibt unberuehrt
        u = self.engine.undo(t)
        self.assertTrue(u.ok, u.message)
        self.assertEqual(self.fake.values[("SUB_PROCESSOR", "PROCTHROTTLEMIN")], 5)

    def test_unavailable_setting(self):
        t = self.cat["ps_usb_suspend_off"]  # nicht in FakePowercfg
        self.assertEqual(self.engine.status(t)[0], tl.STATUS_UNKNOWN)
        r = self.engine.apply(t)
        self.assertFalse(r.ok)
        self.assertFalse(self.engine.has_backup(t.id))

    def test_set_failure_rolls_back(self):
        self.fake.fail_set = True
        r = self.engine.apply(self.cat["ps_pcie_aspm_off"])
        self.assertFalse(r.ok)
        self.assertEqual(self.fake.values[("SUB_PCIE", "ASPM")], 2)


class IniTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ini = Path(self.tmp.name) / "GameUserSettings.ini"
        self.ini.write_bytes(INI_TEXT.encode("utf-8"))
        self.running = False
        self.engine = tl.TweakEngine(tl.MemoryBackend(), Path(self.tmp.name) / "s.json", ini_path=self.ini,
                                     fortnite_running=lambda: self.running)
        self.cat = {t.id: t for t in tl.build_catalog()}

    def tearDown(self):
        self.tmp.cleanup()

    def test_apply_undo_preserves_rest_and_crlf(self):
        t = self.cat["fn_fps_limit"]
        self.assertTrue(self.engine.apply(t).ok)
        raw = self.ini.read_bytes().decode()
        self.assertIn("FrameRateLimit=240.000000\r\n", raw)
        self.assertIn("Keep=me\r\n", raw)
        self.assertIn("[Tail]\r\nX=2\r\n", raw)
        self.assertNotIn("\n\n", raw.replace("\r\n", ""))  # keine LF-only-Zeilen eingeschleppt
        self.assertEqual(self.engine.status(t)[0], tl.STATUS_ACTIVE)
        self.assertTrue(list((Path(self.tmp.name) / "config_backups").glob("GameUserSettings.*.ini")))
        self.assertTrue(self.engine.undo(t).ok)
        self.assertEqual(tl.ini_read(self.ini, SECTION, "FrameRateLimit"), "0.000000")

    def test_missing_key_is_added_and_removed_on_undo(self):
        self.ini.write_bytes(f"[{SECTION}]\r\nKeep=me\r\n".encode())
        t = self.cat["fn_vsync_off"]
        self.assertTrue(self.engine.apply(t).ok)
        self.assertEqual(tl.ini_read(self.ini, SECTION, "bUseVSync"), "False")
        self.assertTrue(self.engine.undo(t).ok)
        self.assertIsNone(tl.ini_read(self.ini, SECTION, "bUseVSync"))
        self.assertEqual(tl.ini_read(self.ini, SECTION, "Keep"), "me")

    def test_refuses_while_fortnite_runs(self):
        self.running = True
        r = self.engine.apply(self.cat["fn_vsync_off"])
        self.assertFalse(r.ok)
        self.assertIn("Fortnite läuft", r.message)
        self.assertEqual(tl.ini_read(self.ini, SECTION, "bUseVSync"), "True")

    def test_missing_file(self):
        self.ini.unlink()
        self.assertEqual(self.engine.status(self.cat["fn_vsync_off"])[0], tl.STATUS_UNKNOWN)
        self.assertFalse(self.engine.apply(self.cat["fn_vsync_off"]).ok)

    def test_bom_is_kept(self):
        self.ini.write_bytes(b"\xef\xbb\xbf" + INI_TEXT.encode())
        self.assertTrue(self.engine.apply(self.cat["fn_vsync_off"]).ok)
        self.assertTrue(self.ini.read_bytes().startswith(b"\xef\xbb\xbf"))


class RecommendTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fake = FakePowercfg()
        self.fake.values[("2a737441-1930-4402-8d77-b2bebba308a3", "48e6b7a6-50f5-4782-a5d4-53bb8f07e226")] = 1
        self.ini = Path(self.tmp.name) / "g.ini"
        self.ini.write_bytes(INI_TEXT.encode())
        self.engine = tl.TweakEngine(tl.MemoryBackend(), Path(self.tmp.name) / "s.json", runner=self.fake, ini_path=self.ini)
        self.cat = tl.build_catalog()

    def tearDown(self):
        self.tmp.cleanup()

    def ids(self, ctx):
        chosen, skipped = tl.recommend(self.cat, self.engine, ctx)
        return {t.id for t in chosen}, {t.id: r for t, r in skipped}

    def test_desktop_with_fortnite(self):
        chosen, skipped = self.ids({"desktop": True, "fortnite_config": True, "fortnite_running": False})
        for must in ("game_mode", "game_dvr", "mouse_accel", "ps_min_cpu", "fn_fps_limit", "fn_vsync_off", "power_ultimate"):
            self.assertIn(must, chosen)
        # kosmetische Tweaks werden NICHT automatisch empfohlen
        for never in ("visualfx", "transparency_off", "window_anim_off", "power_high"):
            self.assertNotIn(never, chosen)

    def test_laptop_skips_power_tweaks(self):
        chosen, skipped = self.ids({"desktop": False, "fortnite_config": True})
        self.assertNotIn("ps_min_cpu", chosen)
        self.assertIn("ps_min_cpu", skipped)
        self.assertIn("game_mode", chosen)

    def test_no_fortnite_config_or_running(self):
        _, skipped = self.ids({"desktop": True, "fortnite_config": False})
        self.assertIn("fn_vsync_off", skipped)
        _, skipped = self.ids({"desktop": True, "fortnite_config": True, "fortnite_running": True})
        self.assertIn("Fortnite läuft", skipped["fn_fps_limit"])

    def test_already_active_not_listed(self):
        self.engine.apply(next(t for t in self.cat if t.id == "game_mode"))
        chosen, _ = self.ids({"desktop": True, "fortnite_config": True})
        self.assertNotIn("game_mode", chosen)


if __name__ == "__main__":
    unittest.main(verbosity=2)
