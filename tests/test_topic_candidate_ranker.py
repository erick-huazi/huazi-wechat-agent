from __future__ import annotations

import importlib.util
import json
import unittest
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("topic_candidate_ranker", ROOT / "scripts" / "topic_candidate_ranker.py")
RANKER = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(RANKER)


class TopicCandidateRankerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.rubric = json.loads((ROOT / "config" / "content-workflow-rubric.json").read_text(encoding="utf-8"))
        cls.now = datetime.fromisoformat("2026-08-06T08:00:00+08:00")

    def candidate(self, index: int, total_delta: int = 0) -> dict:
        scores = {
            "ordinary_person_value": 15,
            "hot_window": 20,
            "breakout_potential": 15,
            "signature_move_strength": 10,
            "information_gain": 10,
            "fact_credibility": 15,
            "persona_fit": 5,
            "platform_distribution": 5,
            "risk_control": 5,
        }
        remaining = total_delta
        for key in ("ordinary_person_value", "hot_window"):
            reduction = min(scores[key], remaining)
            scores[key] -= reduction
            remaining -= reduction
        return {
            "title": f"Candidate {index}",
            "topic_type": "ai_model_release" if index < 3 else "evergreen_knowledge",
            "published_at": "2026-08-06T00:00:00+08:00",
            "source_urls": [f"https://github.com/example/repo-{index}"],
            "primary_source": True,
            "duplicate_within_days": False,
            "scores": scores,
            "heat_signals": [
                {
                    "kind": "official_release",
                    "summary": "The vendor published a material AI update.",
                    "reference": f"https://official.example.com/release-{index}",
                },
                {
                    "kind": "community_discussion",
                    "summary": "Independent discussion shows broad user attention.",
                    "reference": f"https://community.example.net/thread-{index}",
                },
            ] if index < 3 else [],
            "knowledge_layer": {
                "why_it_matters": "It changes an ordinary user's workflow.",
                "mental_model": "Separate the event, its mechanism, and its consequence.",
                "reader_action": "Audit one affected workflow today.",
                "fact_boundary": "Do not generalize beyond the cited release.",
            },
            "experiment": {
                "variable": "opening_hook",
                "hypothesis": "A conflict-first opening improves qualified reading.",
                "control": "A descriptive opening used by recent posts.",
                "signature_move": f"One sharp counter-intuitive judgment {index}.",
                "breakout_reason": "The topic has an unresolved practical conflict.",
                "audience_signal": "Qualified reads and completion rate.",
                "kill_condition": "Stop when reach rises but completion collapses.",
            },
        }

    def test_selects_highest_verified_candidate(self) -> None:
        payload = {"candidates": [self.candidate(index, index) for index in range(5)]}
        result = RANKER.rank_payload(payload, self.rubric, self.now)
        self.assertTrue(result["passed"])
        self.assertEqual(result["selected_title"], "Candidate 0")
        self.assertEqual(result["selection_mode"], "ai_news_first")
        self.assertEqual(result["candidate_count"], 5)
        self.assertEqual(result["news_candidate_count"], 3)

    def test_duplicate_and_non_primary_candidates_are_blocked(self) -> None:
        candidates = [self.candidate(index) for index in range(5)]
        candidates[0]["duplicate_within_days"] = True
        candidates[1]["primary_source"] = False
        result = RANKER.rank_payload({"candidates": candidates}, self.rubric, self.now)
        index = {item["title"]: item for item in result["candidates"]}
        self.assertIn("duplicate_within_7_days", index["Candidate 0"]["hard_blocks"])
        self.assertIn("primary_source_not_confirmed", index["Candidate 1"]["hard_blocks"])

    def test_below_threshold_uses_best_verified_fallback(self) -> None:
        candidates = [self.candidate(index, 20) for index in range(5)]
        result = RANKER.rank_payload({"candidates": candidates}, self.rubric, self.now)
        self.assertTrue(result["passed"])
        self.assertEqual(result["selection_mode"], "best_verified_below_threshold")

    def test_missing_experiment_field_blocks_candidate(self) -> None:
        candidates = [self.candidate(index) for index in range(5)]
        candidates[0]["experiment"].pop("signature_move")
        result = RANKER.rank_payload({"candidates": candidates}, self.rubric, self.now)
        index = {item["title"]: item for item in result["candidates"]}
        self.assertIn("experiment_field_missing:signature_move", index["Candidate 0"]["hard_blocks"])

    def test_builds_stable_selected_growth_experiment(self) -> None:
        candidates = [self.candidate(index, index) for index in range(5)]
        result = RANKER.rank_payload({"candidates": candidates}, self.rubric, self.now)
        growth = RANKER.load_default_growth_strategy()
        experiment = RANKER.growth_experiment_payload(result, growth, self.now)
        later = RANKER.growth_experiment_payload(
            result,
            growth,
            datetime.fromisoformat("2026-08-06T09:00:00+08:00"),
        )
        self.assertEqual(experiment["status"], "planned")
        self.assertEqual(experiment["ranking_id"], result["ranking_id"])
        self.assertEqual(experiment["variable"], "opening_hook")
        self.assertTrue(experiment["experiment_id"])
        self.assertEqual(experiment["experiment_id"], later["experiment_id"])

    def test_news_candidate_outranks_higher_scoring_evergreen_candidate(self) -> None:
        candidates = [
            self.candidate(0, 8),
            self.candidate(1, 9),
            self.candidate(2, 10),
            self.candidate(3, 0),
            self.candidate(4, 0),
        ]
        result = RANKER.rank_payload({"candidates": candidates}, self.rubric, self.now)
        self.assertTrue(result["passed"])
        self.assertTrue(result["selected_news_priority"])
        self.assertEqual(result["selected_title"], "Candidate 0")

    def test_missing_knowledge_layer_blocks_candidate(self) -> None:
        candidates = [self.candidate(index) for index in range(5)]
        candidates[0]["knowledge_layer"].pop("mental_model")
        result = RANKER.rank_payload({"candidates": candidates}, self.rubric, self.now)
        index = {item["title"]: item for item in result["candidates"]}
        self.assertIn("knowledge_field_missing:mental_model", index["Candidate 0"]["hard_blocks"])

    def test_insufficient_ai_news_candidates_stops_workflow(self) -> None:
        candidates = [self.candidate(index) for index in range(5)]
        for index in range(1, 5):
            candidates[index]["topic_type"] = "evergreen_knowledge"
            candidates[index]["heat_signals"] = []
        result = RANKER.rank_payload({"candidates": candidates}, self.rubric, self.now)
        self.assertFalse(result["passed"])
        self.assertIn("ai_news_candidate_count_below_minimum:1<3", result["issues"])
        self.assertIn("ai_news_candidate_ratio_below_minimum:0.20<0.60", result["issues"])

    def test_news_candidate_requires_independent_heat_signal(self) -> None:
        candidates = [self.candidate(index) for index in range(5)]
        candidates[0]["heat_signals"] = [candidates[0]["heat_signals"][0]]
        result = RANKER.rank_payload({"candidates": candidates}, self.rubric, self.now)
        index = {item["title"]: item for item in result["candidates"]}
        self.assertIn("external_heat_signal_missing", index["Candidate 0"]["hard_blocks"])

    def test_news_candidate_requires_independent_heat_signal_domains(self) -> None:
        candidates = [self.candidate(index) for index in range(5)]
        candidates[0]["heat_signals"][1]["reference"] = "https://official.example.com/community"
        result = RANKER.rank_payload({"candidates": candidates}, self.rubric, self.now)
        index = {item["title"]: item for item in result["candidates"]}
        self.assertIn(
            "heat_signal_domain_count_below_minimum:1<2",
            index["Candidate 0"]["hard_blocks"],
        )


if __name__ == "__main__":
    unittest.main()
