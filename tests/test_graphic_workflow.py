from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import content_workflow_gate as gate
import content_quality_gate as quality
import distribution_dashboard as dashboard
import research_evidence_gate as research
import social_draft_router as router
import social_auto_upload_adapter as adapter
import huazi_content_ops as ops
from workflow_policy import ACTIVE_PLATFORMS, GRAPHIC_PLATFORMS, DISABLED_PLATFORMS, plan_platform_ids
from test_content_workflow_gate import write_ready_pack
from test_content_quality_gate import write_valid_pack


class GraphicWorkflowTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.pack = self.root / "2026-08-06-graphic-huazi"
        self.pack.mkdir()

    def test_graphic_pack_ready_without_any_video_files(self):
        write_ready_pack(self.pack, video_required=False)
        report = gate.evaluate_pack(self.pack)
        self.assertTrue(report["ready_for_draft_actions"], report["issues"])
        self.assertFalse((self.pack / "video").exists())
        self.assertFalse(report["gates"]["assets"]["video_required"])
        self.assertEqual(report["gates"]["preflight"]["platform_count"], 5)

    def test_real_plan_dry_run_resume_and_stage_without_video(self):
        write_ready_pack(self.pack, video_required=False)
        write_valid_pack(self.pack)
        for path in self.pack.glob("*.md"):
            path.write_text(path.read_text(encoding="utf-8").replace("2026-08-22", "2026-08-06"), encoding="utf-8")
        router.atomic_write_json(self.pack / "content-quality-report.json", quality.build_report(self.pack))
        registry = router.load_registry(router.DEFAULT_REGISTRY)
        args = router.build_parser().parse_args(["push", "--pack", str(self.pack), "--dry-run", "--platforms", ",".join(GRAPHIC_PLATFORMS)])
        with mock.patch.object(router, "configured", return_value=(True, [], "test")), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(router.command_push(args, registry), 0)
        self.assertTrue(gate.evaluate_pack(self.pack)["ready_for_draft_actions"])
        plan, dry, results = router.resume_context(self.pack)
        self.assertEqual(len(plan["items"]), 5)
        self.assertTrue(all(row["status"] == "not_attempted" for row in results["items"]))
        self.assertEqual(dry["inputs"]["video_asset_bundle"]["files"], [])
        self.assertNotIn("video-quality-report.json", [row["path"] for row in dry["inputs"]["quality_gate_bundle"]["files"]])
        staged = adapter.stage(self.pack, self.root / "no-upstream", list(adapter.DEFAULT_TARGETS))
        self.assertEqual(staged["jobs"], [])
        self.assertEqual(staged["inputs"]["plan_id"], plan["plan_id"])

    def test_disabled_dispatch_cannot_bypass_command_guard(self):
        args = router.build_parser().parse_args(["push", "--pack", str(self.pack), "--confirm-drafts"])
        with self.assertRaisesRegex(ValueError, "PLATFORM_DISABLED_BY_WORKFLOW"):
            router.dispatch_item({"platform_id": "douyin-video"}, args)

    def test_same_count_wrong_platforms_and_duplicates_are_rejected(self):
        write_ready_pack(self.pack, video_required=False)
        path = self.pack / "distribution-dry-run-results.json"
        payload = json.loads(path.read_text())
        payload["items"][-1]["platform_id"] = "wechat"
        router.atomic_write_json(path, payload)
        self.assertIn("dry_run_platform_coverage_mismatch", gate.preflight_gate(self.pack)["issues"])
        path = self.pack / "content-compliance-report.json"
        payload = json.loads(path.read_text())
        payload["items"][-1]["platform_id"] = "wechat"
        router.atomic_write_json(path, payload)
        self.assertFalse(gate.copy_gate(self.pack)["passed"])

    def test_malformed_plan_does_not_crash_dashboard_or_invent_scope(self):
        write_ready_pack(self.pack, video_required=False)
        (self.pack / "distribution-plan.json").write_text("{broken", encoding="utf-8")
        self.assertEqual(plan_platform_ids(self.pack), [])
        self.assertFalse(gate.preflight_gate(self.pack)["passed"])
        self.assertFalse(dashboard.quality_gates(self.pack)["ready"])

    def test_empty_video_fingerprint_is_still_checked(self):
        write_ready_pack(self.pack, video_required=False)
        path = self.pack / "distribution-dry-run-results.json"
        payload = json.loads(path.read_text())
        payload["inputs"]["video_asset_bundle"]["fingerprint"] = "tampered"
        router.atomic_write_json(path, payload)
        self.assertFalse(gate.preflight_gate(self.pack)["passed"])

    def test_legacy_video_and_cover_hash_are_still_required(self):
        write_ready_pack(self.pack)
        self.assertTrue(gate.asset_gate(self.pack)["passed"])
        (self.pack / "video" / "2026-08-06-video-cover.png").write_bytes(b"changed")
        self.assertIn("video_quality_report_stale", gate.asset_gate(self.pack)["issues"])
        self.assertIn("video_quality", {item["key"] for item in dashboard.quality_gates(self.pack)["items"]})

    def test_quality_and_fact_scope_has_weibo_short_not_video(self):
        write_valid_pack(self.pack)
        report = quality.build_report(self.pack)
        self.assertTrue(report["passed"], report)
        self.assertEqual(len(report["items"]), 6)
        self.assertFalse(report["video_script"]["required"])
        self.assertTrue(all("video" not in item["path"] for item in report["inputs"]))
        files = research.required_article_files(self.pack, research.load_standard())
        self.assertIn("weibo.md", files)
        self.assertNotIn("visual-video-pack.md", files)

    def test_legacy_quality_scope_keeps_video_script(self):
        write_valid_pack(self.pack)
        ids = [*GRAPHIC_PLATFORMS, "douyin-video", "video-channel", "kuaishou-video"]
        router.atomic_write_json(self.pack / "distribution-plan.json", {"items": [{"platform_id": key} for key in ids]})
        report = quality.build_report(self.pack)
        self.assertEqual(len(report["items"]), 9)
        self.assertIn("source", report["video_script"])
        self.assertIn("visual-video-pack.md", research.required_article_files(self.pack, research.load_standard()))

    def test_new_pack_skeleton_has_no_video_artifacts(self):
        args = ops.build_parser().parse_args([
            "pack", "--out", str(self.pack), "--root", str(self.root / "ops"),
            "--title", "Graphic workflow",
            *[arg for key in ops.WEIGHTS for arg in ("--score", f"{key}=3")],
        ])
        with contextlib.redirect_stdout(io.StringIO()):
            ops.cmd_pack(args)
        self.assertTrue((self.pack / "wechat-article.md").is_file())
        self.assertFalse((self.pack / "weibo.md").exists())
        self.assertFalse((self.pack / "video").exists())
        self.assertFalse(any("video" in file.name or "douyin" in file.name or "kuaishou" in file.name for file in self.pack.iterdir()))
        self.assertEqual(plan_platform_ids(self.pack), list(GRAPHIC_PLATFORMS))

    def test_disabled_push_fails_before_remote_or_pack_writes(self):
        registry = router.load_registry(router.DEFAULT_REGISTRY)
        for key in DISABLED_PLATFORMS:
            args = router.build_parser().parse_args(["push", "--pack", str(self.pack), "--platforms", key, "--confirm-drafts"])
            with self.subTest(platform=key), mock.patch.object(router, "dispatch_item") as remote:
                with self.assertRaisesRegex(ValueError, "PLATFORM_DISABLED_BY_WORKFLOW"):
                    router.command_resume_push(args, registry)
                remote.assert_not_called()
        self.assertFalse((self.pack / "distribution-results.json").exists())

    def test_scope_change_cannot_discard_historical_results(self):
        write_ready_pack(self.pack)
        router.atomic_write_json(self.pack / "distribution-results.json", {"keep": "historical"})
        before = (self.pack / "distribution-plan.json").read_bytes()
        with self.assertRaisesRegex(ValueError, "PLAN_SCOPE_CHANGE_REQUIRES_RECONCILIATION"):
            router.protect_plan_scope(self.pack, [{"id": key} for key in ACTIVE_PLATFORMS])
        self.assertEqual(before, (self.pack / "distribution-plan.json").read_bytes())

    def test_five_platform_audit_completion_and_incorrect_ids(self):
        write_ready_pack(self.pack, video_required=False)
        audit = {"complete": True, "summary": {"complete": 5, "total": 5}, "items": [{"platform_id": key} for key in GRAPHIC_PLATFORMS]}
        path = self.pack / "distribution-audit.json"
        router.atomic_write_json(path, audit)
        self.assertTrue(gate.result_gate(self.pack)["passed"])
        audit["items"][-1]["platform_id"] = "wechat"
        router.atomic_write_json(path, audit)
        self.assertFalse(gate.result_gate(self.pack)["passed"])

    def test_staging_requires_current_input_ids_and_hashes(self):
        write_ready_pack(self.pack, video_required=False)
        preflight = gate.preflight_gate(self.pack)
        data = {"jobs": [{"platform_id": "xiaohongshu", "status": "ready_for_manual_adapter_review"}], "inputs": {
            "plan_id": preflight["plan_id"], "dry_run_id": preflight["dry_run_id"],
            "plan_file_sha256": preflight["plan_sha256"], "dry_run_file_sha256": preflight["dry_run_sha256"],
        }}
        router.atomic_write_json(self.pack / "social-auto-upload-jobs.json", data)
        # Historical staging validation remains testable after an explicit restore.
        with mock.patch.object(gate, "STAGE_PLATFORMS", ("xiaohongshu",)):
            self.assertTrue(gate.evaluate_pack(self.pack)["stage_checks"]["staged"])
        data["inputs"]["dry_run_file_sha256"] = "stale"
        router.atomic_write_json(self.pack / "social-auto-upload-jobs.json", data)
        self.assertFalse(gate.evaluate_pack(self.pack)["stage_checks"]["staged"])


if __name__ == "__main__":
    unittest.main()
