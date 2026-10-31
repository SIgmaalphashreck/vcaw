import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import tweaklib as tl  # noqa: E402

PCI = r"HKLM\SYSTEM\CurrentControlSet\Enum\PCI"
GPU = PCI + r"\VEN_10DE&DEV_2803\4&abc&0&0008"
NIC = PCI + r"\VEN_10EC&DEV_8168\4&def&0&0010"
MSI_KEY = GPU + r"\Device Parameters\Interrupt Management\MessageSignaledInterruptProperties"


class AdminTweakTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.reg = tl.MemoryBackend()
        self.admin = True
        self.engine = tl.TweakEngine(self.reg, Path(self.tmp.name) / "s.json", admin_check=lambda: self.admin,
                                     ini_path=Path(self.tmp.name) / "none.ini")
        self.cat = {t.id: t for t in tl.build_catalog()}

    def tearDown(self):
        self.tmp.cleanup()

    def test_refused_without_admin(self):
        self.admin = False
        r = self.engine.apply(self.cat["net_throttle_off"])
        self.assertFalse(r.ok)
        self.assertIn("Administrator", r.message)
        self.assertFalse(self.engine.has_backup("net_throttle_off"))

    def test_hklm_roundtrip_keeps_original(self):
        t = self.cat["net_throttle_off"]
        sp = t.values[0].path
        self.reg.write(sp, "NetworkThrottlingIndex", tl.REG_DWORD, 10)
        self.assertEqual(self.engine.status(t)[0], tl.STATUS_INACTIVE)
        r = self.engine.apply(t)
        self.assertTrue(r.ok and r.verified, r.message)
        self.assertEqual(self.reg.read(sp, "NetworkThrottlingIndex")[2], 4294967295)
        self.assertEqual(self.engine.status(t)[0], tl.STATUS_ACTIVE)
        self.assertTrue(self.engine.undo(t).ok)
        self.assertEqual(self.reg.read(sp, "NetworkThrottlingIndex")[2], 10)
        self.assertFalse(self.reg.read(sp, "SystemResponsiveness")[0])

    def test_games_task_values(self):
        t = self.cat["games_task"]
        self.assertTrue(self.engine.apply(t).ok)
        path = t.values[0].path
        self.assertEqual(self.reg.read(path, "Scheduling Category")[2], "High")
        self.assertEqual(self.reg.read(path, "GPU Priority")[2], 8)

    def test_msi_only_touches_display_devices(self):
        self.reg.write(GPU, "Class", tl.REG_SZ, "Display")
        self.reg.write(NIC, "Class", tl.REG_SZ, "Net")
        t = self.cat["msi_gpu"]
        self.assertEqual(self.engine.status(t)[0], tl.STATUS_INACTIVE)
        r = self.engine.apply(t)
        self.assertTrue(r.ok, r.message)
        self.assertEqual(self.reg.read(MSI_KEY, "MSISupported")[2], 1)
        self.assertEqual(len(self.engine.state["msi_gpu"]["registry"]), 1)  # nur die GPU
        self.assertTrue(self.engine.undo(t).ok)
        self.assertFalse(self.reg.read(MSI_KEY, "MSISupported")[0])

    def test_msi_without_gpu_is_unknown_and_refused(self):
        t = self.cat["msi_gpu"]
        self.assertEqual(self.engine.status(t)[0], tl.STATUS_UNKNOWN)
        r = self.engine.apply(t)
        self.assertFalse(r.ok)
        self.assertFalse(self.engine.has_backup("msi_gpu"))

    def test_recommend_skips_admin_tweaks_without_admin(self):
        ctx = {"desktop": True, "fortnite_config": False, "admin": False}
        chosen, skipped = tl.recommend(list(self.cat.values()), self.engine, ctx)
        ids = {t.id for t in chosen}
        self.assertNotIn("net_throttle_off", ids)
        self.assertIn("net_throttle_off", {t.id for t, _ in skipped})
        self.assertIn("game_mode", ids)
        ctx["admin"] = True
        chosen, _ = tl.recommend(list(self.cat.values()), self.engine, ctx)
        self.assertIn("net_throttle_off", {t.id for t in chosen})

    def test_profiles_scope(self):
        ctx = {"desktop": True, "fortnite_config": False, "admin": True}
        cat = list(self.cat.values())
        safe = {t.id for t in tl.select_for_profile(cat, self.engine, ctx, "safe")[0]}
        comp = {t.id for t in tl.select_for_profile(cat, self.engine, ctx, "competitive")[0]}
        mx = {t.id for t in tl.select_for_profile(cat, self.engine, ctx, "maxfps")[0]}
        self.assertNotIn("fso_global", safe)
        self.assertIn("fso_global", comp)
        self.assertNotIn("diagtrack_off", comp)
        self.assertIn("diagtrack_off", mx)
        self.assertNotIn("msi_gpu", mx)  # keine GPU vorhanden -> nicht gewaehlt

    def test_no_forbidden_services_or_security_values(self):
        blob = " ".join((v.path + v.name).lower() for t in self.cat.values() for v in t.values)
        for word in ("windefend", "sense", "mpssvc", "wuauserv", "easyanticheat", "battleye"):
            self.assertNotIn(word, blob)


if __name__ == "__main__":
    unittest.main()


class FortniteIniTests(unittest.TestCase):
    def setUp(self):
        from test_v3 import INI_TEXT, SECTION
        self.SECTION = SECTION
        self.tmp = tempfile.TemporaryDirectory()
        self.ini = Path(self.tmp.name) / "GameUserSettings.ini"
        self.ini.write_bytes(INI_TEXT.encode())
        self.engine = tl.TweakEngine(tl.MemoryBackend(), Path(self.tmp.name) / "s.json", ini_path=self.ini)
        self.cat = {t.id: t for t in tl.build_catalog()}

    def tearDown(self):
        self.tmp.cleanup()

    def test_graphics_writes_both_sections_and_undo(self):
        t = self.cat["fn_graphics_perf"]
        self.assertTrue(self.engine.apply(t).ok)
        self.assertEqual(tl.ini_read(self.ini, self.SECTION, "sg.ShadowQuality"), "0")
        self.assertEqual(tl.ini_read(self.ini, "ScalabilityGroups", "sg.EffectsQuality"), "0")
        self.assertEqual(tl.ini_read(self.ini, self.SECTION, "Keep"), "me")
        self.assertEqual(self.engine.status(t)[0], tl.STATUS_ACTIVE)
        self.assertTrue(self.engine.undo(t).ok)
        self.assertIsNone(tl.ini_read(self.ini, "ScalabilityGroups", "sg.EffectsQuality"))

    def test_fullscreen_and_motionblur(self):
        for tid in ("fn_fullscreen", "fn_motionblur_off", "fn_menu_fps"):
            self.assertTrue(self.engine.apply(self.cat[tid]).ok, tid)
        self.assertEqual(tl.ini_read(self.ini, self.SECTION, "FullscreenMode"), "0")
        self.assertEqual(tl.ini_read(self.ini, self.SECTION, "bMotionBlur"), "False")
