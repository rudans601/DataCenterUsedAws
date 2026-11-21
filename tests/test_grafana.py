import unittest
from datetime import datetime
from urllib.parse import parse_qs

from app.detection_ui import dashboard_period


class ExperimentWindowTests(unittest.TestCase):
    def test_window_covers_baseline_all_rounds_and_delayed_recovery(self):
        row = {
            "baseline_at": "2026-11-19T13:00:00+09:00",
            "episodes": [
                {"injection_started": "2026-11-19T13:01:00+09:00", "measured_until": "2026-11-19T13:02:00+09:00"},
                {"injection_started": "2026-11-19T13:04:00+09:00", "measured_until": "2026-11-19T13:05:00+09:00"},
            ],
            "recovery_verified_at": "2026-11-19T13:10:00+09:00",
        }
        start, last, query = dashboard_period(row)
        bounds = parse_qs(query)
        self.assertEqual(start.minute, 1)
        self.assertEqual(last.minute, 5)
        self.assertEqual(int(bounds["from"][0]), int(datetime.fromisoformat(row["baseline_at"]).timestamp() - 120) * 1000)
        self.assertEqual(int(bounds["to"][0]), int(datetime.fromisoformat(row["recovery_verified_at"]).timestamp() + 180) * 1000)

    def test_old_or_interrupted_case_uses_only_its_own_timestamps(self):
        row = {"episodes": [{"injection_started": "2026-11-19T13:00:00+09:00"}],
               "probes": [{"at": "2026-11-19T13:01:30+09:00"}]}
        start, last, query = dashboard_period(row)
        self.assertEqual((last - start).total_seconds(), 90)
        self.assertIsNone(dashboard_period({"started": row["probes"][0]["at"]}))
