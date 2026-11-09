import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from news_editorial_advisor import diagnose, host


class NewsEditorialAdvisorTests(unittest.TestCase):
    def candidate(self, signals):
        return {"candidate_id": "x", "title": "Update", "source_urls": ["https://example.org/news"],
                "topic_type": "open_source_release", "heat_signals": signals}

    def test_index_does_not_become_demand(self):
        result = diagnose(self.candidate([{"kind": "product_status", "reference": "https://index.org/x", "summary": "listed"}]))
        self.assertIn("NEWS_DEMAND_NOT_EVIDENCED", result["issues"])
        self.assertIn("COVERAGE_NOT_DEMAND", result["issues"])
        self.assertIn("TOOL_DETAIL_REQUIRES_BROAD_RELEVANCE_REVIEW", result["issues"])
        self.assertEqual([], result["reader_demand_references"])

    def test_demand_is_declared_not_certified(self):
        result = diagnose(self.candidate([{"kind": "community_discussion", "reference": "https://community.org/x", "summary": "discussion"}]))
        self.assertNotIn("NEWS_DEMAND_NOT_EVIDENCED", result["issues"])
        self.assertEqual(1, len(result["reader_demand_references"]))

    def test_same_source_subdomain_is_not_independent(self):
        result = diagnose(self.candidate([{"kind": "search_trend", "reference": "https://docs.example.org/x", "summary": "claims"}]))
        self.assertIn("SAME_SOURCE_SIGNAL", result["issues"])
        self.assertIn("NEWS_DEMAND_NOT_EVIDENCED", result["issues"])

    def test_unrelated_suffix_is_not_same_domain(self):
        result = diagnose(self.candidate([{"kind": "community_discussion", "reference": "https://notexample.org/x", "summary": "discussion"}]))
        self.assertEqual(1, len(result["reader_demand_references"]))

    def test_missing_summary_or_invalid_link_needs_review(self):
        result = diagnose(self.candidate([{"kind": "search_trend", "reference": "javascript:foo", "summary": "trend"},
                                          {"kind": "github_trending", "reference": "https://else.org/x", "summary": ""}]))
        self.assertIn("SIGNAL_INCOMPLETE", result["issues"])
        self.assertEqual([], result["reader_demand_references"])

    def test_bad_signal_and_url_are_handled(self):
        self.assertEqual("", host("https://user:password@example.org/"))
        self.assertIn("SIGNAL_INCOMPLETE", diagnose(self.candidate([None]))["issues"])


if __name__ == "__main__":
    unittest.main()
