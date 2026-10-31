import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import fx  # noqa: E402


class FxTests(unittest.TestCase):
    def test_lerp(self):
        self.assertEqual(fx.lerp_color("#000000", "#ffffff", 0.0), "#000000")
        self.assertEqual(fx.lerp_color("#000000", "#ffffff", 1.0), "#ffffff")
        self.assertEqual(fx.lerp_color("#000000", "#ffffff", 0.5), "#808080")
        self.assertEqual(fx.lerp_color("#102030", "#102030", 0.3), "#102030")
        self.assertEqual(fx.lerp_color("#000000", "#ffffff", 7), "#ffffff")  # geklemmt

    def test_gradient(self):
        stops = fx.gradient_stops(["#8B5CF6", "#22D3EE"], 50)
        self.assertEqual(len(stops), 50)
        self.assertEqual(stops[0].lower(), "#8b5cf6")
        self.assertEqual(stops[-1].lower(), "#22d3ee")
        self.assertEqual(fx.gradient_stops(["#112233"], 3), ["#112233"] * 3)

    def test_hover_fade_survives_destroyed_widget(self):
        class W:
            def __init__(self): self.cfg = {}
            def winfo_children(self): return []
            def bind(self, *a, **k): self.handlers = getattr(self, "handlers", []) + [a[0]]
            def configure(self, **k): self.cfg.update(k)
            def after(self, ms, fn): fn()      # synchron
            def winfo_pointerxy(self): return (-5, -5)
            def winfo_rootx(self): return 0
            def winfo_rooty(self): return 0
            def winfo_width(self): return 10
            def winfo_height(self): return 10
        w = W()
        fx.hover_fade(w, {"fg_color": ("#000000", "#ffffff")}, steps=4)
        self.assertEqual(w.handlers, ["<Enter>", "<Leave>"])
        # Simuliere Enter: nach der Animation ist die Zielfarbe erreicht
        enter = None

        class W2(W):
            def bind(self, seq, fn, add=None):
                if seq == "<Enter>":
                    self.enter = fn
                else:
                    self.leave = fn
        w2 = W2()
        fx.hover_fade(w2, {"fg_color": ("#000000", "#ffffff")}, steps=4)
        w2.enter()
        self.assertEqual(w2.cfg["fg_color"], "#ffffff")
        w2.leave()
        self.assertEqual(w2.cfg["fg_color"], "#000000")


if __name__ == "__main__":
    unittest.main()
