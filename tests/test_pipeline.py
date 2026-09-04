import importlib.util
import unittest
from pathlib import Path

SPEC = importlib.util.spec_from_file_location("pipeline", Path(__file__).parents[1] / "scripts" / "update_tracker.py")
pipeline = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pipeline)


class PipelineTests(unittest.TestCase):
    def test_band_boundaries(self):
        self.assertEqual(pipeline.band(0), "Very low")
        self.assertEqual(pipeline.band(20), "Low")
        self.assertEqual(pipeline.band(80), "Very high")

    def test_direction(self):
        rising = [{"value": v} for v in [1, 1, 1, 2, 2, 2]]
        self.assertEqual(pipeline.direction(rising), "Rising")

    def test_missing_number(self):
        self.assertIsNone(pipeline.number(""))
        self.assertEqual(pipeline.number("1.5"), 1.5)

    def test_incomplete_wastewater_week_is_held_back(self):
        series = [{"date": f"2026-07-{day:02d}", "value": 4, "sites": 40, "population_covered": 10_000_000} for day in (1, 8, 15, 22, 29)]
        series.append({"date": "2026-08-05", "value": 2, "sites": 12, "population_covered": 2_000_000})
        selected, status = pipeline.select_wastewater_week(series)
        self.assertEqual(selected, 4)
        self.assertTrue(status["reporting_delayed"])

    def test_complete_wastewater_week_is_used(self):
        series = [{"date": f"2026-07-{day:02d}", "value": 4, "sites": 40, "population_covered": 10_000_000} for day in (1, 8, 15, 22, 29)]
        series.append({"date": "2026-08-05", "value": 7, "sites": 38, "population_covered": 9_500_000})
        selected, status = pipeline.select_wastewater_week(series)
        self.assertEqual(selected, 5)
        self.assertFalse(status["reporting_delayed"])


if __name__ == "__main__":
    unittest.main()
