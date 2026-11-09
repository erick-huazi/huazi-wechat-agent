from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "tests")]
import content_workflow_gate as gate
import topic_candidate_ranker as ranker
import social_draft_router as router
from distribution_io import canonical_sha256, file_sha256, file_bundle_snapshot, pack_lock, verify_pack_backup
import test_topic_candidate_ranker as fixtures
from test_content_workflow_gate import write_ready_pack

NOW = datetime.fromisoformat("2026-10-07T08:00:00+08:00")


def write_json(path, payload):
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


class RankingIntegrityTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.pack = Path(temp.name) / "2026-10-07-integrity-huazi"
        self.pack.mkdir()
        self.rubric = json.loads(ranker.DEFAULT_RUBRIC.read_text(encoding="utf-8"))
        self.growth = ranker.load_default_growth_strategy()
        fixture = fixtures.TopicCandidateRankerTests()
        self.candidates = [fixture.candidate(i, i) for i in range(5)]
        for candidate in self.candidates:
            candidate["published_at"] = "2026-10-07T00:00:00+08:00"
        self.payload = {"candidates": self.candidates}

    def write_ranking(self):
        path = self.pack / "topic-candidates.json"
        write_json(path, self.payload)
        report = ranker.rank_payload(self.payload, self.rubric, NOW, self.growth,
                                     input_sha256=file_sha256(path))
        write_json(self.pack / "topic-ranking.json", report)
        write_json(self.pack / "growth-experiment.json", ranker.growth_experiment_payload(report, self.growth, NOW))
        return report

    def test_bound_ranking_valid_and_gate_uses_it(self):
        report = self.write_ranking()
        self.assertTrue(ranker.validate_ranking(self.pack, report, now=NOW)["passed"])
        with mock.patch.object(ranker, "datetime", wraps=datetime) as clock:
            clock.now.return_value = NOW
            self.assertTrue(gate.topic_gate(self.pack)["passed"])
            self.assertTrue(gate.growth_gate(self.pack)["passed"])

    def test_candidate_change_same_size_and_mtime_invalidates_ranking(self):
        report = self.write_ranking()
        path = self.pack / "topic-candidates.json"
        stat = path.stat()
        path.write_bytes(path.read_bytes().replace(b"Candidate 0", b"Candidate X"))
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        self.assertIn("TOPIC_RANKING_INPUT_CHANGED", ranker.validate_ranking(self.pack, report, now=NOW)["issues"])

    def test_payload_preserving_file_edit_still_requires_new_ranking(self):
        report = self.write_ranking()
        path = self.pack / "topic-candidates.json"
        path.write_bytes(path.read_bytes() + b"\n")
        self.assertFalse(ranker.validate_ranking(self.pack, report, now=NOW)["passed"])

    def test_rehashing_fabricated_scores_or_selection_does_not_pass(self):
        report = self.write_ranking()
        for fields in ({"selected_score": 999}, {"selected_candidate_id": "invented"},
                       {"candidate_count": 10}, {"selected_title": "Other news"}, {"passed": False}):
            with self.subTest(fields=fields):
                tampered = {**report, **fields}
                tampered["ranking_id"] = ranker.ranking_identity(tampered)
                self.assertFalse(ranker.validate_ranking(self.pack, tampered, now=NOW)["passed"])

    def test_rubric_change_invalidates_report_even_with_same_id(self):
        report = self.write_ranking()
        changed = {**self.rubric, "minimum_score": 99}
        self.assertIn("TOPIC_RANKING_POLICY_CHANGED", ranker.validate_ranking(self.pack, report, now=NOW, rubric=changed)["issues"])

    def test_selected_news_ages_out_without_touching_files(self):
        report = self.write_ranking()
        self.assertIn("TOPIC_SELECTED_NEWS_EXPIRED", ranker.validate_ranking(self.pack, report, now=NOW + timedelta(days=3))["issues"])

    def test_legacy_report_not_migrated_but_new_pack_cannot_downgrade(self):
        old = self.pack.parent / "2026-10-06-legacy-huazi"
        old.mkdir()
        legacy = {"schema_version": 1, "passed": True}
        self.assertFalse(ranker.validate_ranking(old, legacy, now=NOW)["required"])
        self.assertFalse(ranker.validate_ranking(self.pack, legacy, now=NOW)["passed"])

    def test_changed_experiment_or_rehashed_id_cannot_drift_from_selected_topic(self):
        self.write_ranking()
        path = self.pack / "growth-experiment.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["variable"] = "information_density"
        payload["experiment_id"] = canonical_sha256({k: v for k, v in payload.items() if k not in {"experiment_id", "generated_at"}})
        write_json(path, payload)
        with mock.patch.object(ranker, "datetime", wraps=datetime) as clock:
            clock.now.return_value = NOW
            self.assertIn("growth_experiment_content_mismatch", gate.growth_gate(self.pack)["issues"])

    def test_future_and_naive_publication_times_are_not_fresh_news(self):
        for value in ("2026-11-01T00:00:00+08:00", "2026-10-07T00:00:00"):
            with self.subTest(value=value):
                row = copy.deepcopy(self.candidates[0])
                row["published_at"] = value
                self.assertFalse(ranker.score_candidate(row, self.rubric, NOW, self.growth)["verified"])

    def test_duplicate_candidate_ids_cannot_fill_candidate_pool(self):
        result = ranker.rank_payload({"candidates": [self.candidates[0]] * 5}, self.rubric, NOW, self.growth)
        self.assertFalse(result["passed"])
        self.assertTrue(any(i.startswith("candidate_id_duplicate:") for i in result["issues"]))

    def test_non_string_experiment_and_knowledge_fields_not_counted_as_complete(self):
        for container, field in (("experiment", "hypothesis"), ("knowledge_layer", "mental_model")):
            row = copy.deepcopy(self.candidates[0])
            row[container][field] = {"placeholder": True}
            with self.subTest(container=container):
                self.assertFalse(ranker.score_candidate(row, self.rubric, NOW, self.growth)["verified"])

    def test_invalid_source_types_and_urls_produce_blocks_not_exceptions(self):
        for value in (None, 123, {}, ["https://[broken"], [None]):
            row = copy.deepcopy(self.candidates[0])
            row["source_urls"] = value
            with self.subTest(value=value):
                self.assertIn("missing_or_invalid_source_url", ranker.score_candidate(row, self.rubric, NOW, self.growth)["hard_blocks"])

    def test_malformed_ranking_counts_do_not_crash_workflow(self):
        for value in ([], {}, "five", True):
            write_json(self.pack / "topic-ranking.json", {"passed": True, "candidate_count": value, "candidates": [None]})
            with self.subTest(value=value):
                self.assertFalse(gate.topic_gate(self.pack)["passed"])

    def test_malformed_reported_issues_do_not_crash_failed_ranking(self):
        for value in (None, 42, {}, "failed", [None, {}]):
            write_json(self.pack / "topic-ranking.json", {"passed": False, "issues": value})
            with self.subTest(value=value):
                self.assertIn("topic_ranking_failed", gate.topic_gate(self.pack)["issues"])

    def run_cli(self, *extra):
        return subprocess.run(
            [sys.executable, "-X", "utf8", str(ROOT / "scripts" / "topic_candidate_ranker.py"),
             "--input", str(self.pack / "topic-candidates.json"), "--now", NOW.isoformat(), *extra],
            capture_output=True, text=True, encoding="utf-8", timeout=30,
        )

    def test_cli_writes_bound_pair_with_valid_restore_point_and_stable_ids(self):
        self.write_ranking()
        before = {name: file_sha256(self.pack / name)
                  for name in ("topic-ranking.json", "growth-experiment.json")}
        first = self.run_cli()
        self.assertEqual(first.returncode, 0, first.stderr)
        latest = sorted((self.pack / ".backups").iterdir())[-1]
        verified = verify_pack_backup(latest)
        self.assertTrue(verified["valid"])
        self.assertEqual(verified["verified_files"], 2)
        self.assertEqual({name: file_sha256(latest / name) for name in before}, before)
        report = json.loads((self.pack / "topic-ranking.json").read_text(encoding="utf-8"))
        self.assertTrue(ranker.validate_ranking(self.pack, report, now=NOW)["passed"])
        repeat = self.run_cli()
        self.assertEqual(repeat.returncode, 0, repeat.stderr)
        self.assertEqual(json.loads((self.pack / "topic-ranking.json").read_text(encoding="utf-8"))["ranking_id"],
                         report["ranking_id"])

    def test_cli_cross_process_lock_conflict_preserves_outputs(self):
        self.write_ranking()
        before = file_sha256(self.pack / "topic-ranking.json")
        with pack_lock(self.pack, "test-owner"):
            result = self.run_cli()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("TOPIC_PACK_BUSY", result.stderr)
        self.assertEqual(file_sha256(self.pack / "topic-ranking.json"), before)
        self.assertFalse((self.pack / ".backups").exists())

    def test_cli_rejects_alias_or_external_outputs_before_writing(self):
        self.write_ranking()
        before = file_sha256(self.pack / "topic-candidates.json")
        for path in (self.pack / "topic-candidates.json", self.pack.parent / "outside.json"):
            with self.subTest(path=path):
                result = self.run_cli("--output", str(path))
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("TOPIC_OUTPUT_PATH_INVALID", result.stderr)
                self.assertEqual(file_sha256(self.pack / "topic-candidates.json"), before)
        self.assertFalse((self.pack / ".backups").exists())

    def test_cli_bad_input_does_not_replace_previous_ranking(self):
        self.write_ranking()
        before = file_sha256(self.pack / "topic-ranking.json")
        for content in (b"[]", b"{broken", b" " * (ranker.MAX_CANDIDATE_BYTES + 1)):
            (self.pack / "topic-candidates.json").write_bytes(content)
            result = self.run_cli()
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("TOPIC_RANKING_INPUT_INVALID", result.stderr)
            self.assertEqual(file_sha256(self.pack / "topic-ranking.json"), before)
        self.assertFalse((self.pack / ".backups").exists())

    def test_new_quality_bundle_tracks_raw_candidates(self):
        self.write_ranking()
        plan = {"items": []}
        before = router.workflow_bundles(self.pack, plan)["quality_gates"]
        self.assertTrue(any(row["path"] == "topic-candidates.json" for row in before["files"]))
        path = self.pack / "topic-candidates.json"
        path.write_bytes(path.read_bytes() + b"\n")
        after = router.workflow_bundles(self.pack, plan)["quality_gates"]
        self.assertNotEqual(before["fingerprint"], after["fingerprint"])

    def test_fractional_ranking_time_can_be_recomputed(self):
        path = self.pack / "topic-candidates.json"
        write_json(path, self.payload)
        report = ranker.rank_payload(self.payload, self.rubric, NOW.replace(second=17, microsecond=999999),
                                     self.growth, input_sha256=file_sha256(path))
        self.assertTrue(ranker.validate_ranking(self.pack, report, now=NOW + timedelta(minutes=1))["passed"])


class ApprovalAndPreflightIntegrityTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.pack = Path(temp.name) / "2026-08-06-preflight-huazi"
        self.pack.mkdir()
        write_ready_pack(self.pack, video_required=False)

    def test_absent_approval_retains_policy_auto(self):
        self.assertTrue(gate.review_gate(self.pack)["passed"])

    def test_complete_canonical_preflight_accepts_empty_video_bundle(self):
        result = gate.preflight_gate(self.pack)
        self.assertTrue(result["passed"], result["issues"])

    def test_corrupt_approval_does_not_silently_become_policy_auto(self):
        for value in ("{broken", "[]", "{}", '{"decision":null}', '{"decision":"typo"}'):
            (self.pack / "content-approval.json").write_text(value, encoding="utf-8")
            with self.subTest(value=value):
                self.assertIn("manual_review_invalid", gate.review_gate(self.pack)["issues"])

    def test_padded_hold_is_blocked(self):
        write_json(self.pack / "content-approval.json", {"schema_version": 1, "decision": " hold "})
        self.assertFalse(gate.review_gate(self.pack)["passed"])

    def test_changed_decision_invalidates_existing_approval_id(self):
        payload = gate.write_review(self.pack, "hold", "tester", "needs correction")
        payload["decision"] = "approved"
        write_json(self.pack / "content-approval.json", payload)
        self.assertIn("manual_review_identity_invalid", gate.review_gate(self.pack)["issues"])

    def test_approved_and_auto_records_still_pass(self):
        for decision in ("approved", "policy_auto"):
            gate.write_review(self.pack, decision, "tester", "Fixture only")
            self.assertTrue(gate.review_gate(self.pack)["passed"])

    def test_preflight_cannot_omit_source_from_recorded_bundle(self):
        path = self.pack / "distribution-dry-run-results.json"
        dry = json.loads(path.read_text(encoding="utf-8"))
        dry["inputs"]["quality_gate_bundle"] = file_bundle_snapshot(self.pack, ["fact-check-notes.md"])
        dry["result_id"] = router.result_identity(dry)
        write_json(path, dry)
        self.assertIn("dry_run_quality_gate_bundle_stale_or_missing", gate.preflight_gate(self.pack)["issues"])

    def test_preflight_checks_identity_and_dryrun_marker(self):
        path = self.pack / "distribution-dry-run-results.json"
        original = json.loads(path.read_text(encoding="utf-8"))
        for field, value in (("result_id", "forged"), ("dry_run", False)):
            with self.subTest(field=field):
                write_json(path, {**original, field: value})
                self.assertFalse(gate.preflight_gate(self.pack)["passed"])
                self.assertTrue(gate.preflight_gate(self.pack)["issues"])

    def test_malformed_bundle_rows_fail_without_crash(self):
        path = self.pack / "distribution-dry-run-results.json"
        original = json.loads(path.read_text(encoding="utf-8"))
        for files in ([None], [42], [{}], "not-a-list"):
            dry = copy.deepcopy(original)
            dry["inputs"]["quality_gate_bundle"]["files"] = files
            write_json(path, dry)
            with self.subTest(files=files):
                self.assertFalse(gate.preflight_gate(self.pack)["passed"])


if __name__ == "__main__":
    unittest.main()
