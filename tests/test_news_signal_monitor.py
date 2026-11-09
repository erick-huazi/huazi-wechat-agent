import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from news_signal_monitor import evaluate

NOW = datetime(2026, 11, 4, 8, tzinfo=timezone.utc)


def observation(id, timestamp="2026-11-04T07:00:00Z", **overrides):
    result = dict(id=id, event_id="e1", status="observed", observed_at=timestamp,
                  source_url="https://example.org/signal", provider="example", region="CN",
                  query="model", window="24h", signal_type="search_trend", evidence_note="observed",
                  metric="index", unit="index", scale="same-series", value=10)
    result.update(overrides)
    return result


def check(rows, comparisons=None):
    return evaluate({"event_id": "e1", "observations": rows, "comparisons": comparisons or []}, NOW)


class SignalMonitorTests(unittest.TestCase):
    def test_valid_comparable_values(self):
        r = check([observation("a", "2026-11-04T05:00:00Z"), observation("b", value=12)],
                  [{"baseline_id": "a", "current_id": "b"}])
        self.assertEqual(2, r["comparisons"][0]["difference"])

    def test_different_scope_or_query_is_never_compared(self):
        for k in ("provider", "region", "query", "window", "signal_type", "metric", "unit", "scale"):
            r = check([observation("a", "2026-11-04T05:00:00Z"), observation("b", **{k: "other"})],
                      [{"baseline_id": "a", "current_id": "b"}])
            self.assertIsNone(r["comparisons"][0]["difference"])
            self.assertIn("COMPARISON_SCOPE_MISMATCH", r["comparisons"][0]["issues"])

    def test_stale_future_naive_and_missing_are_not_valid(self):
        rows = [observation("a", "2026-11-02T05:00:00Z"), observation("b", "2026-11-05T05:00:00Z"),
                observation("c", "2026-11-04T05:00:00"), observation("d", None)]
        result = check(rows)["observations"]
        self.assertEqual(["OBSERVATION_STALE", "TIME_IN_FUTURE", "TIME_MISSING_OR_NAIVE", "TIME_MISSING_OR_NAIVE"],
                         [r["issues"][0] for r in result])

    def test_unavailable_is_not_zero_or_absence(self):
        r = check([observation("a", status="unavailable")])["observations"][0]
        self.assertFalse(r["usable_for_manual_review"])
        self.assertEqual("unavailable", r["status"])

    def test_duplicate_id_blocks_comparison(self):
        r = check([observation("a"), observation("a")], [{"baseline_id": "a", "current_id": "a"}])
        self.assertIn("OBSERVATION_ID_INVALID", r["observations"][1]["issues"])
        self.assertIsNone(r["comparisons"][0]["difference"])

    def test_invalid_values_or_reverse_time(self):
        for value in (True, float("nan"), float("inf"), "10", 10**400):
            r = check([observation("a", "2026-11-04T05:00:00Z"), observation("b", value=value)],
                      [{"baseline_id": "a", "current_id": "b"}])
            self.assertIn("COMPARISON_VALUE_INVALID", r["comparisons"][0]["issues"])
        r = check([observation("a"), observation("b", "2026-11-04T05:00:00Z")],
                  [{"baseline_id": "a", "current_id": "b"}])
        self.assertIn("COMPARISON_ORDER_INVALID", r["comparisons"][0]["issues"])

    def test_overflow_and_invalid_configuration_are_rejected(self):
        r = check([observation("a", "2026-11-04T05:00:00Z", value=-1e308),
                   observation("b", value=1e308)],
                  [{"baseline_id": "a", "current_id": "b"}])
        self.assertIsNone(r["comparisons"][0]["difference"])
        self.assertIn("COMPARISON_VALUE_INVALID", r["comparisons"][0]["issues"])
        with self.assertRaises(ValueError):
            evaluate({"event_id": "e1", "observations": [], "comparisons": {}}, NOW)
        with self.assertRaises(ValueError):
            evaluate({"event_id": "e1", "observations": []}, NOW, float("nan"))

    def test_invalid_url_and_event(self):
        r = check([observation("a", source_url="https://u:p@example.org", event_id="e2")])["observations"][0]
        self.assertIn("SOURCE_URL_INVALID", r["issues"])
        self.assertIn("EVENT_MISMATCH", r["issues"])


if __name__ == "__main__":
    unittest.main()
