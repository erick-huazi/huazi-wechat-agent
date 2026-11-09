from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import timedelta
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import content_quality_gate as content
import research_evidence_gate as evidence
import distribution_dashboard as dashboard
import content_workflow_gate as workflow
from distribution_io import canonical_sha256
from test_content_quality_gate import write_valid_pack
from test_research_evidence_gate import write_pack, NOW


class ContentReportIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.pack = Path(self.temp.name) / "2026-10-05-integrity-huazi"
        self.pack.mkdir()
        write_valid_pack(self.pack)

    def test_changing_passed_and_scores_cannot_approve_failing_copy(self):
        report = content.build_report(self.pack)
        self.assertFalse(report["passed"])
        report["passed"] = True
        report["pack_score"] = 100
        report["blocking_count"] = 0
        self.assertFalse(content.report_is_current(report))

    def test_omitting_changed_copy_from_input_list_does_not_hide_change(self):
        report = content.build_report(self.pack)
        report["inputs"] = [row for row in report["inputs"] if not row["path"].endswith("weibo.md")]
        report["input_fingerprint"] = canonical_sha256(report["inputs"])
        (self.pack / "weibo.md").write_text("Changed", encoding="utf-8")
        self.assertFalse(content.report_is_current(report))

    def test_malformed_input_row_fails_closed(self):
        report = content.build_report(self.pack)
        report["inputs"] = [42]
        self.assertFalse(content.report_is_current(report))

    def test_rehashing_fabricated_score_cannot_approve_copy(self):
        report = content.build_report(self.pack)
        report["passed"] = True
        report["pack_score"] = 100
        report["blocking_count"] = 0
        report["report_id"] = content.report_identity(report)
        self.assertFalse(content.report_is_current(report, self.pack))

    def test_foreign_pack_report_is_rejected(self):
        report = content.build_report(self.pack)
        other = self.pack.parent / "2026-10-05-other-huazi"
        other.mkdir()
        write_valid_pack(other)
        self.assertFalse(content.report_is_current(report, other))

    def test_malformed_quality_score_does_not_crash_workflow_or_get_displayed(self):
        (self.pack / "content-quality-report.json").write_text(json.dumps({"pack_score": "not-a-number", "passed": True}), encoding="utf-8")
        (self.pack / "content-compliance-report.json").write_text(json.dumps({"platform_count": {}, "blocking_count": []}), encoding="utf-8")
        result = workflow.copy_gate(self.pack)
        self.assertFalse(result["passed"])
        self.assertIsNone(result["quality_score"])


class FactReportIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.pack = Path(self.temp.name) / "2026-08-23-evidence-huazi"
        self.pack.mkdir()
        write_pack(self.pack)

    def test_missing_verification_returns_false_not_exception(self):
        report = evidence.evaluate_pack(self.pack, now=NOW)
        (self.pack / "claim-verification.json").unlink()
        self.assertFalse(evidence.report_is_current(report, self.pack))

    def test_future_verification_is_rejected(self):
        path = self.pack / "claim-verification.json"
        value = json.loads(path.read_text(encoding="utf-8"))
        value["verified_at"] = (NOW + timedelta(days=2)).isoformat()
        path.write_text(json.dumps(value), encoding="utf-8")
        self.assertFalse(evidence.evaluate_pack(self.pack, now=NOW)["passed"])

    def test_unchanged_files_do_not_keep_expired_fact_report_current(self):
        report = evidence.evaluate_pack(self.pack, now=NOW)
        self.assertTrue(evidence.report_is_current(report, self.pack, now=NOW))
        self.assertFalse(evidence.report_is_current(report, self.pack, now=NOW + timedelta(days=2)))

    def test_rehashed_false_fact_approval_is_rejected(self):
        path = self.pack / "claim-verification.json"
        value = json.loads(path.read_text(encoding="utf-8"))
        value["claims"][0]["verdict"] = "unverified"
        path.write_text(json.dumps(value), encoding="utf-8")
        report = evidence.evaluate_pack(self.pack, now=NOW)
        self.assertFalse(report["passed"])
        report.update(passed=True, issues=[])
        report["report_id"] = canonical_sha256({k: v for k, v in report.items() if k not in {"generated_at", "report_id"}})
        self.assertFalse(evidence.report_is_current(report, self.pack, now=NOW))

    def test_future_source_check_is_rejected(self):
        path = self.pack / "claim-verification.json"
        value = json.loads(path.read_text(encoding="utf-8"))
        value["sources"][0]["checked_at"] = (NOW + timedelta(days=1)).isoformat()
        path.write_text(json.dumps(value), encoding="utf-8")
        self.assertIn("source_checked_at_in_future:S1", evidence.evaluate_pack(self.pack, now=NOW)["issues"])

    def test_naive_verification_time_is_not_assumed_local(self):
        self.assertIsNone(evidence.parse_time("2026-10-04T08:00:00"))

    def test_fullwidth_numeric_tokens_are_normalized_before_matching(self):
        self.assertEqual(evidence.numeric_values("\uff14\uff18\uff05 and \uff10\uff05"), ["48%", "0%"])

    def test_fullwidth_metric_still_requires_context(self):
        path = self.pack / "claim-verification.json"
        value = json.loads(path.read_text(encoding="utf-8"))
        value["schema_version"] = 3
        value["claims"][0]["text"] = "Fixture \uff14\uff18\uff05"
        path.write_text(json.dumps(value), encoding="utf-8")
        self.assertIn("numeric_claim_context_missing:C1:metric_name", evidence.evaluate_pack(self.pack, now=NOW)["issues"])

    def test_bad_schema_is_reported_without_exception(self):
        path = self.pack / "claim-verification.json"
        value = json.loads(path.read_text(encoding="utf-8"))
        value["schema_version"] = "invalid"
        path.write_text(json.dumps(value), encoding="utf-8")
        self.assertIn("claim_verification_schema_invalid", evidence.evaluate_pack(self.pack, now=NOW)["issues"])

    def test_new_storyboard_is_part_of_independent_fact_scope(self):
        new_pack = self.pack.parent / "2026-10-05-new-huazi"
        self.assertIn("image-storyboard.json", evidence.required_article_files(new_pack, evidence.load_standard()))

    def test_malformed_fact_counts_do_not_crash_workflow(self):
        (self.pack / "research-evidence-report.json").write_text(json.dumps({"claim_count": {}, "source_domain_count": []}), encoding="utf-8")
        self.assertFalse(workflow.fact_gate(self.pack)["passed"])


class DashboardFreshnessTests(unittest.TestCase):
    def test_same_size_same_mtime_copy_and_review_edits_invalidate_cache(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            pack = root / "2026-10-05-refresh-huazi"
            pack.mkdir()
            (pack / "wechat-article.md").write_text("Fixture article", encoding="utf-8")
            for name in ("zhihu-article.md", "image-visual-review.json", "claim-verification.json", "image-quality-report.json"):
                with self.subTest(name=name):
                    path = pack / name
                    path.write_bytes(b"first")
                    times = path.stat()
                    before = dashboard.dashboard_revision(root, pack.name)
                    path.write_bytes(b"other")
                    os.utime(path, ns=(times.st_atime_ns, times.st_mtime_ns))
                    self.assertNotEqual(before, dashboard.dashboard_revision(root, pack.name))

    def test_clock_expiry_invalidates_cache_without_file_change(self):
        with tempfile.TemporaryDirectory() as temp:
            with mock.patch.object(dashboard, "datetime") as clock:
                clock.now.return_value = NOW
                before = dashboard.dashboard_revision(Path(temp))
                clock.now.return_value = NOW + timedelta(minutes=1)
                self.assertNotEqual(before, dashboard.dashboard_revision(Path(temp)))

    def test_saved_ready_flag_cannot_hide_current_fact_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            pack = Path(temp) / "2026-10-05-ready-fixture"
            pack.mkdir()
            (pack / "content-workflow.json").write_text(json.dumps({"ready_for_draft_actions": True,
                "gates": {"facts": {"passed": True}}}), encoding="utf-8")
            with mock.patch("image_quality_gate.gate", return_value={"required": True, "passed": True, "issues": []}), \
                    mock.patch("content_workflow_gate.copy_gate", return_value={"quality_passed": True, "quality_score": 100}):
                gates = dashboard.quality_gates(pack)
            self.assertFalse(gates["workflow"]["ready_for_draft_actions"])
            self.assertFalse(next(row for row in gates["items"] if row["key"] == "facts")["passed"])


if __name__ == "__main__":
    unittest.main()
