import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import tweaklib as tl  # noqa: E402
from test_v3 import FakePowercfg, INI_TEXT  # noqa: E402


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        ini = Path(self.tmp.name) / "g.ini"
        ini.write_bytes(INI_TEXT.encode())
        fake = FakePowercfg()
        fake.values[("2a737441-1930-4402-8d77-b2bebba308a3", "48e6b7a6-50f5-4782-a5d4-53bb8f07e226")] = 1
        self.engine = tl.TweakEngine(tl.MemoryBackend(), Path(self.tmp.name) / "s.json", runner=fake, ini_path=ini)
        self.cat = tl.build_catalog()
        self.ctx = {"desktop": True, "fortnite_config": True, "fortnite_running": False}

    def tearDown(self):
        self.tmp.cleanup()

    def ids(self, profile):
        chosen, _ = tl.select_for_profile(self.cat, self.engine, self.ctx, profile)
        return {t.id for t in chosen}

    def test_safe_only_baseline(self):
        ids = self.ids("safe")
        self.assertIn("game_mode", ids)
        self.assertNotIn("ps_min_cpu", ids)
        self.assertNotIn("fn_vsync_off", ids)

    def test_competitive_equals_recommend(self):
        base = {t.id for t in tl.recommend(self.cat, self.engine, self.ctx)[0]}
        self.assertEqual(self.ids("competitive"), base)

    def test_maxfps_adds_cosmetics(self):
        ids = self.ids("maxfps")
        for c in tl.COSMETIC_IDS:
            self.assertIn(c, ids)
        self.assertTrue(self.ids("competitive") <= ids)

    def test_unknown_profile_falls_back(self):
        self.assertEqual(self.ids("nonsense"), self.ids("competitive"))


if __name__ == "__main__":
    unittest.main()
