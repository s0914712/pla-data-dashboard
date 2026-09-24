#!/usr/bin/env python3
"""Pure-function tests for Classifier 4.0 Phase 1."""

import unittest

import numpy as np
import pandas as pd

from scripts.analysis.classifier4_cutoff import apply_feature_cutoff, assert_point_in_time
from scripts.analysis.classifier4_features import build_classifier4_features
from scripts.collectors.official_signals import build_event_ledger


class Classifier4Phase1Tests(unittest.TestCase):
    def _sample(self):
        dates = pd.date_range("2026-01-01", periods=220, freq="D")
        x = np.arange(len(dates), dtype=float)
        return pd.DataFrame({
            "date": dates,
            "pla_aircraft_sorties": 10 + (x % 9),
            "plan_vessel_sorties": 4 + (x % 4),
            "宮古": (x % 17 == 0),
            "與那國": (x % 29 == 0),
            "大禹": False,
            "對馬": (x % 41 == 0),
            "聯合演訓": (x % 53 == 0),
            "艦通過": (x % 13 == 0),
            "航母活動": (x % 71 == 0),
        })

    def test_features_are_deterministic(self):
        df = self._sample()
        a = build_classifier4_features(df)
        b = build_classifier4_features(df)
        pd.testing.assert_frame_equal(a, b, check_exact=True)

    def test_incremental_append_does_not_change_prior_features(self):
        df = self._sample()
        old = build_classifier4_features(df.iloc[:-1])
        new = build_classifier4_features(df)
        common = old.index.intersection(new.index)
        pd.testing.assert_frame_equal(old.loc[common], new.loc[common], check_exact=True)

    def test_cutoff_rejects_future_rows(self):
        events = pd.DataFrame({
            "published_at": ["2026-08-12T07:00:00+08:00", "2026-08-12T09:00:00+08:00"],
            "value": [1, 2],
        })
        filtered, result = apply_feature_cutoff(events, "2026-08-12T08:00:00+08:00")
        self.assertEqual(len(filtered), 1)
        self.assertEqual(result.dropped_future_rows, 1)
        with self.assertRaises(AssertionError):
            assert_point_in_time(events, "2026-08-12T08:00:00+08:00")

    def test_event_ledger_keeps_publication_time(self):
        df = self._sample().iloc[:1]
        ledger = build_event_ledger(df)
        self.assertFalse(ledger.empty)
        self.assertIn("published_at", ledger.columns)
        self.assertIn("event_time", ledger.columns)
        self.assertTrue(ledger["published_at"].notna().all())


if __name__ == "__main__":
    unittest.main()
