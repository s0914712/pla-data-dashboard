import importlib.util
from datetime import date, datetime, timezone
from pathlib import Path
import sys
import unittest

import pandas as pd

MODULE = Path(__file__).resolve().parents[1] / "scripts" / "features" / "build_navwarn_features.py"
spec = importlib.util.spec_from_file_location("navwarn_features", MODULE)
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)


class NavWarnFeatureTests(unittest.TestCase):
    def test_exact_publication_and_lead(self):
        row = pd.Series({
            "title": "实弹射击—TEST",
            "publish_date": "2026-08-19",
            "time_periods": "08/21 0700-1500",
            "start_date": "2026-08-21", "end_date": "2026-08-21",
            "coordinates": "21.7833,113.6333;21.7833,113.6833;21.7333,113.6833;21.7333,113.6333",
            "content_preview": "发布时间：2026-08-19 18:55 实弹射击。",
            "url": "https://example/a", "scraped_at": "2026-08-20T03:10:00",
        })
        ev = mod.build_event_record(row)
        self.assertEqual(ev["published_at"], "2026-08-19T18:55:00+08:00")
        self.assertAlmostEqual(ev["source_lead_hours"], 36.083, places=3)
        self.assertAlmostEqual(ev["lead_hours"], 19.833, places=3)

    def test_dms_coordinates_recovered(self):
        row = pd.Series({
            "title": "实弹射击—DMS", "publish_date": "2026-08-18",
            "time_periods": "08/19-08/21 每日 0630-1830",
            "start_date": "2026-08-19", "end_date": "2026-08-21", "coordinates": "",
            "content_preview": "以下四点：（1）23-04-33N 116-33-14E; （2）23-06-43N 116-37-11E; （3）23-01-10N 116-36-00E; （4）23-04-00N 116-33-05E。实弹射击",
            "url": "https://example/b", "scraped_at": "2026-08-19T03:10:00",
        })
        ev = mod.build_event_record(row)
        self.assertEqual(ev["coordinate_count"], 4)
        self.assertEqual(ev["polygon_count"], 1)
        self.assertGreater(ev["area_km2"], 0)
        self.assertGreater(ev["coordinate_confidence"], 0.8)

    def test_two_polygons(self):
        coords = ";".join([
            "34.9315,119.4463", "34.9315,119.4683", "34.9135,119.4683", "34.9135,119.4463",
            "34.9175,119.4598", "34.9175,119.4818", "34.8993,119.4818", "34.8993,119.4598",
        ])
        row = pd.Series({
            "title": "军事活动", "publish_date": "2026-08-19",
            "time_periods": "08/21-08/25 每日 0800-2000", "start_date": "2026-08-21", "end_date": "2026-08-25",
            "coordinates": coords, "content_preview": "四点连线范围内以及另四点连线范围内进行军事活动",
            "url": "https://example/c", "scraped_at": "2026-08-20T03:10:00",
        })
        ev = mod.build_event_record(row)
        self.assertEqual(ev["polygon_count"], 2)
        self.assertGreater(ev["area_km2"], ev["area_max_km2"])

    def test_daily_duration(self):
        row = pd.Series({
            "title": "军事活动", "publish_date": "2026-08-19",
            "time_periods": "08/21-08/25 每日 0800-2000", "start_date": "2026-08-21", "end_date": "2026-08-25",
            "coordinates": "34.9,119.4;34.9,119.5;34.8,119.5;34.8,119.4",
            "content_preview": "军事活动", "url": "https://example/d", "scraped_at": "2026-08-20T03:10:00",
        })
        ev = mod.build_event_record(row)
        self.assertEqual(ev["duration_hours"], 60.0)
        self.assertEqual(ev["window_span_hours"], 108.0)

    def test_pit_cutoff_excludes_future_first_seen(self):
        rows = pd.DataFrame([
            {"title":"实弹射击 A","publish_date":"2026-08-19","time_periods":"08/21 0700-1500","start_date":"2026-08-21","end_date":"2026-08-21","coordinates":"23,119;23,119.1;23.1,119.1;23.1,119","content_preview":"实弹射击","url":"https://example/e","scraped_at":"2026-08-20T01:00:00"},
            {"title":"实弹射击 B","publish_date":"2026-08-19","time_periods":"08/21 0700-1500","start_date":"2026-08-21","end_date":"2026-08-21","coordinates":"23,119;23,119.1;23.1,119.1;23.1,119","content_preview":"实弹射击","url":"https://example/f","scraped_at":"2026-08-20T03:00:00"},
        ])
        events = mod.build_event_features(rows)
        ann, agg = mod.aggregate_for_target(
            events, date(2026, 8, 21), datetime(2026, 8, 20, 2, 0, tzinfo=timezone.utc)
        )
        self.assertEqual(agg["navwarn_new_24h"], 1)
        self.assertEqual(agg["navwarn_active_target"], 1)
        self.assertEqual(int(ann["new_warning_last_24h"].sum()), 1)


if __name__ == "__main__":
    unittest.main()
