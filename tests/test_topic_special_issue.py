import copy
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from topic_candidate_ranker import rubric_for_pack, validate_news_priority


class SpecialIssueTests(unittest.TestCase):
    def setUp(self):
        self.base = json.loads((ROOT / "config/content-workflow-rubric.json").read_text(encoding="utf-8"))

    def test_only_named_pack_gets_category(self):
        current = rubric_for_pack(ROOT / "outputs/2026-10-07-nobel-icecube-huazi", self.base)
        self.assertIn("science_award_news", current["news_priority"]["preferred_topic_types"])
        self.assertNotIn("science_award_news", self.base["news_priority"]["preferred_topic_types"])
        for path in ("outputs/2026-10-08-nobel-icecube-huazi", "outputs/2026-10-07-other-huazi",
                     "work/2026-10-07-nobel-icecube-huazi"):
            self.assertEqual(rubric_for_pack(ROOT / path, self.base), self.base)

    def test_scoring_and_source_requirements_unchanged(self):
        current = rubric_for_pack(ROOT / "outputs/2026-10-07-nobel-icecube-huazi", self.base)
        expected = copy.deepcopy(self.base)
        current.pop("editorial_scope")
        current["news_priority"]["preferred_topic_types"].remove("science_award_news")
        self.assertEqual(current, expected)

    def test_special_topic_still_requires_independent_signals(self):
        current = rubric_for_pack(ROOT / "outputs/2026-10-07-nobel-icecube-huazi", self.base)
        _, _, preferred, blocks = validate_news_priority({"topic_type": "science_award_news"}, current)
        self.assertTrue(preferred)
        self.assertIn("external_heat_signal_missing", blocks)

    def test_user_expo_scope_is_named_and_does_not_relax_requirements(self):
        current = rubric_for_pack(ROOT / "outputs/2026-10-12-ciif-field-notes-huazi", self.base)
        self.assertIn("industry_expo_observation", current["news_priority"]["preferred_topic_types"])
        _, _, preferred, blocks = validate_news_priority({"topic_type": "industry_expo_observation"}, current)
        self.assertTrue(preferred)
        self.assertIn("external_heat_signal_missing", blocks)
        current.pop("editorial_scope")
        current["news_priority"]["preferred_topic_types"].remove("industry_expo_observation")
        self.assertEqual(current, self.base)
        self.assertEqual(rubric_for_pack(ROOT / "outputs/2026-10-13-ciif-field-notes-huazi", self.base), self.base)
        self.assertEqual(rubric_for_pack(ROOT / "work/2026-10-12-ciif-field-notes-huazi", self.base), self.base)


if __name__ == "__main__":
    unittest.main()
