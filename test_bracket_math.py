import unittest

from bracket_math import compute_bracket_distances, compute_bracket_table, clamp_size_for_min_stop


class TestBracketMath(unittest.TestCase):
    def test_mes_per_position_bracket_math(self):
        res1 = compute_bracket_distances(50, 100, 1)
        self.assertAlmostEqual(res1["sl_points_raw"], 10)
        self.assertEqual(res1["sl_ticks"], 40)
        self.assertAlmostEqual(res1["sl_points"], 10)
        self.assertEqual(res1["tp_ticks"], 80)

        res2 = compute_bracket_distances(50, 100, 2)
        self.assertAlmostEqual(res2["sl_points_raw"], 5)
        self.assertEqual(res2["sl_ticks"], 20)
        self.assertAlmostEqual(res2["sl_points"], 5)

        res3 = compute_bracket_distances(50, 100, 3)
        self.assertAlmostEqual(res3["sl_points_raw"], 50 / (5 * 3))
        self.assertIn(res3["sl_ticks"], {13, 14})
        self.assertAlmostEqual(res3["sl_points"], res3["sl_ticks"] * 0.25)

    def test_min_sl_points_gate(self):
        table = compute_bracket_table(50, 100, sizes=(1, 2, 3))
        allowed_sizes = [
            size_key for size_key, payload in table.items() if payload["sl_points"] >= 6
        ]
        self.assertIn("size_1", allowed_sizes)
        self.assertNotIn("size_2", allowed_sizes)
        self.assertNotIn("size_3", allowed_sizes)

        clamped, distances = clamp_size_for_min_stop(
            3,
            50,
            100,
            min_sl_points=6,
        )
        self.assertEqual(clamped, 1)
        self.assertIsNotNone(distances)
        self.assertGreaterEqual(distances["sl_points"], 6)


if __name__ == "__main__":
    unittest.main()
