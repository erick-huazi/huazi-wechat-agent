from __future__ import annotations

import contextlib
import copy
import io
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import social_draft_router as router
import social_result_recorder as recorder
import social_auto_upload_adapter as adapter
import distribution_dashboard as dashboard
import workflow_policy


class DraftResumeTests(unittest.TestCase):
    def setUp(self):
        # Exercise the retained browser engine under an explicitly restored policy.
        mock.patch.object(workflow_policy, "ACTIVE_PLATFORMS", workflow_policy.GRAPHIC_PLATFORMS).start()
        mock.patch.object(router, "DISABLED_PLATFORMS", workflow_policy.DISABLED_PLATFORMS - workflow_policy.PAUSED_PLATFORMS).start()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.pack = Path(self.temp.name)
        self.bundles = {key: {"valid": True, "fingerprint": key, "files": []}
                        for key in ("content", "images", "video_assets", "quality_gates")}
        self.bundle_mock = mock.patch.object(router, "workflow_bundles", return_value=self.bundles).start()
        mock.patch.object(router, "evaluate_content_workflow", return_value={"ready_for_draft_actions": True}).start()
        self.addCleanup(mock.patch.stopall)
        self.registry = router.load_registry(router.DEFAULT_REGISTRY)
        self.plan = {"schema_version": 2, "pack": str(self.pack), "title": "Test pack",
                     "draft_only_policy": True, "items": [
                         {"platform_id": key, "platform": key, "delivery": "browser_draft",
                          "required_image_count": 5, "cover": "cover.png", "problems": []}
                         for key in ("toutiao", "weibo")]}
        self.plan["plan_id"] = router.plan_identity(self.plan)
        router.atomic_write_json(self.pack / "distribution-plan.json", self.plan)
        self.inputs = {"plan_id": self.plan["plan_id"],
                       "plan_file_sha256": router.file_sha256(self.pack / "distribution-plan.json"),
                       **{name: self.bundles[key] for name, key in (
                           ("content_bundle", "content"), ("image_bundle", "images"),
                           ("video_asset_bundle", "video_assets"), ("quality_gate_bundle", "quality_gates"))}}
        self.dry = {"schema_version": 2, "pack": str(self.pack), "title": "Test pack", "dry_run": True,
                    "inputs": self.inputs, "items": [{"platform_id": key, "status": "validated"}
                                                     for key in ("toutiao", "weibo")]}
        self.dry["result_id"] = router.result_identity(self.dry)
        router.atomic_write_json(self.pack / "distribution-dry-run-results.json", self.dry)
        self.args = router.build_parser().parse_args([
            "push", "--pack", str(self.pack), "--platforms", "weibo", "--confirm-drafts"])

    def saved(self, platform="toutiao"):
        return {"platform_id": platform, "status": "draft_created", "image_count": 5,
                "image_verified": True, "cover_verified": True, "draft_verified": True,
                "proof": "Reopened the saved draft with all five images and its cover",
                "draft_url": "https://example.com/draft/12345",
                "evidence": {"kind": "browser_visual", "verified_at": datetime.now().astimezone().isoformat(),
                             "summary": "Saved draft reopened", "reference": "12345"}}

    def results(self, rows):
        payload = {"schema_version": 2, "pack": str(self.pack), "title": "Test pack", "dry_run": False,
                   "inputs": self.inputs, "items": rows}
        payload["result_id"] = router.result_identity(payload)
        router.atomic_write_json(self.pack / "distribution-results.json", payload)
        return payload

    def push(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return router.command_push(self.args, self.registry)

    def test_subset_keeps_full_plan_and_other_platform_result(self):
        first = self.saved()
        self.results([first, {"platform_id": "weibo", "status": "not_attempted"}])
        before = (self.pack / "distribution-plan.json").read_bytes()
        with mock.patch.object(router, "dispatch_item", return_value=self.saved("weibo")) as call:
            self.assertEqual(self.push(), 0)
            self.assertEqual(call.call_count, 1)
        result = json.loads((self.pack / "distribution-results.json").read_text())
        self.assertEqual(result["items"][0], first)
        self.assertEqual(before, (self.pack / "distribution-plan.json").read_bytes())
        self.assertEqual(len(result["items"]), 2)

    def test_completed_platform_is_not_dispatched_twice(self):
        self.results([self.saved(), self.saved("weibo")])
        before = (self.pack / "distribution-results.json").read_bytes()
        with mock.patch.object(router, "dispatch_item") as call:
            self.assertEqual(self.push(), 0)
            self.assertEqual(self.push(), 0)
            call.assert_not_called()
        self.assertEqual(before, (self.pack / "distribution-results.json").read_bytes())

    def browser_begin(self, retry=False):
        argv = ["browser-begin", "--pack", str(self.pack), "--platform", "weibo"]
        if retry:
            argv.append("--retry")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = router.command_browser_begin(router.build_parser().parse_args(argv))
        return code, json.loads(output.getvalue())

    def record_browser(self, status="partial", dispatch_id=None, **options):
        argv = ["record", "--pack", str(self.pack), "--platform", "weibo", "--status", status,
                "--image-verified", "false", "--cover-verified", "false", "--draft-verified", "false",
                "--evidence-kind", "browser_state", "--evidence-summary", "Observed remote editor state",
                "--evidence-reference", "https://example.com/draft/12345"]
        if dispatch_id is not None:
            argv += ["--dispatch-id", dispatch_id]
        options.setdefault("verified_at", datetime.now().astimezone().isoformat())
        for key, value in options.items():
            argv += ["--" + key.replace("_", "-"), value]
        with mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(io.StringIO()):
            return recorder.main()

    def test_browser_begin_persists_full_plan_intent_without_remote_calls(self):
        saved = self.saved()
        self.results([saved, {"platform_id": "weibo", "status": "not_attempted"}])
        before = (self.pack / "distribution-plan.json").read_bytes()
        with mock.patch.object(router, "dispatch_item") as remote:
            code, report = self.browser_begin()
            remote.assert_not_called()
        current = json.loads((self.pack / "distribution-results.json").read_text())
        self.assertEqual(code, 0)
        self.assertTrue(report["execute_browser_action"])
        self.assertEqual(current["items"][0], saved)
        self.assertEqual(current["items"][1]["dispatch_mode"], "browser")
        self.assertEqual(current["items"][1]["dispatch_state"], "started")
        self.assertEqual(current["items"][1]["dispatch_id"], report["dispatch_id"])
        self.assertEqual(before, (self.pack / "distribution-plan.json").read_bytes())
        self.assertEqual(current["result_id"], router.result_identity(current))

    def test_browser_begin_second_invocation_cannot_replay_even_with_retry(self):
        self.browser_begin()
        before = (self.pack / "distribution-results.json").read_bytes()
        code, report = self.browser_begin(retry=True)
        self.assertEqual(code, 2)
        self.assertFalse(report["execute_browser_action"])
        self.assertEqual(report["code"], "DISPATCH_OUTCOME_UNKNOWN")
        self.assertEqual(before, (self.pack / "distribution-results.json").read_bytes())

    def test_browser_begin_saved_and_stale_never_authorize_creation(self):
        for stale in (False, True):
            row = self.saved("weibo")
            if stale:
                row["evidence"]["verified_at"] = (datetime.now().astimezone() - timedelta(hours=25)).isoformat()
            self.results([self.saved(), row])
            _, report = self.browser_begin(retry=True)
            self.assertFalse(report["execute_browser_action"])
            self.assertEqual(report["action"], "verify_existing" if stale else "skip_verified")

    def test_browser_begin_requires_resolved_blocker_retry(self):
        self.results([self.saved(), {"platform_id": "weibo", "status": "blocked"}])
        self.assertFalse(self.browser_begin()[1]["execute_browser_action"])
        self.assertTrue(self.browser_begin(retry=True)[1]["execute_browser_action"])

    def test_browser_begin_rejects_hold_and_changed_content_before_write(self):
        with mock.patch.object(router, "evaluate_content_workflow", return_value={"ready_for_draft_actions": False}):
            with self.assertRaisesRegex(ValueError, "RESUME_WORKFLOW_BLOCKED"):
                self.browser_begin()
        changed = copy.deepcopy(self.bundles)
        changed["images"]["fingerprint"] = "modified"
        self.bundle_mock.return_value = changed
        with self.assertRaisesRegex(ValueError, "RESUME_INPUT_CHANGED"):
            self.browser_begin()
        self.assertFalse((self.pack / "distribution-results.json").exists())

    def test_browser_begin_never_loads_credentials(self):
        argv = ["router", "browser-begin", "--pack", str(self.pack), "--platform", "weibo"]
        with mock.patch.object(sys, "argv", argv), mock.patch.object(router, "load_dotenv") as credentials, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(router.main(), 0)
            credentials.assert_not_called()

    def test_browser_begin_rejects_wechat_and_retired_platforms(self):
        for platform in ("wechat", "douyin-video", "video-channel", "kuaishou-video", "bilibili-column"):
            args = router.build_parser().parse_args(["browser-begin", "--pack", str(self.pack), "--platform", platform])
            with self.assertRaises(ValueError):
                router.command_browser_begin(args)
        self.assertFalse((self.pack / "distribution-results.json").exists())

    def test_browser_record_requires_exact_attempt_and_preserves_checkpoint_on_rejection(self):
        _, report = self.browser_begin()
        before = (self.pack / "distribution-results.json").read_bytes()
        for attempt, error in ((None, "RESULT_DISPATCH_ID_REQUIRED"), ("wrong", "RESULT_DISPATCH_MISMATCH")):
            with self.assertRaisesRegex(ValueError, error):
                self.record_browser(dispatch_id=attempt)
            self.assertEqual(before, (self.pack / "distribution-results.json").read_bytes())
        self.record_browser(dispatch_id=report["dispatch_id"], draft_url="https://example.com/draft/12345")
        current = json.loads((self.pack / "distribution-results.json").read_text())
        self.assertEqual(current["items"][1]["status"], "partial")
        self.assertFalse(self.browser_begin(retry=True)[1]["execute_browser_action"])

    def test_browser_timeout_cannot_be_relabeled_as_pre_dispatch_failure(self):
        _, report = self.browser_begin()
        for status in ("blocked", "not_attempted", "needs_credentials", "manual_required"):
            with self.assertRaisesRegex(ValueError, "RESULT_REMOTE_OUTCOME_UNRESOLVED"):
                self.record_browser(status=status, dispatch_id=report["dispatch_id"])
        self.record_browser(status="unverified", dispatch_id=report["dispatch_id"])
        self.assertEqual(self.browser_begin(retry=True)[1]["action"], "verify_existing")

    def test_browser_reverification_still_requires_original_attempt(self):
        _, report = self.browser_begin()
        self.record_browser(dispatch_id=report["dispatch_id"])
        before = (self.pack / "distribution-results.json").read_bytes()
        with self.assertRaisesRegex(ValueError, "RESULT_DISPATCH_ID_REQUIRED"):
            self.record_browser(status="unverified")
        self.assertEqual(before, (self.pack / "distribution-results.json").read_bytes())

    def test_plan_bootstrap_cannot_discard_remote_identity_under_unattempted_label(self):
        for evidence in ({"media_id": "existing-id"}, {"draft_url": "https://example.com/draft/1"},
                         {"dispatch_state": "started"}, {"draft_verified": True}):
            with self.subTest(evidence=evidence):
                current = self.results([self.saved(), {"platform_id": "weibo", "status": "not_attempted", **evidence}])
                current["items"][0] = {"platform_id": "toutiao", "status": "not_attempted"}
                current["inputs"] = {**self.inputs, "plan_id": "older-plan"}
                with self.assertRaisesRegex(ValueError, "RESULT_PLAN_CHANGED"):
                    recorder.bootstrap_results_from_dry_run(self.pack, current)

    def test_browser_record_rejects_changed_inputs(self):
        _, report = self.browser_begin()
        before = (self.pack / "distribution-results.json").read_bytes()
        changed = copy.deepcopy(self.bundles)
        changed["content"]["fingerprint"] = "changed"
        self.bundle_mock.return_value = changed
        with self.assertRaisesRegex(ValueError, "RESUME_INPUT_CHANGED"):
            self.record_browser(dispatch_id=report["dispatch_id"])
        self.assertEqual(before, (self.pack / "distribution-results.json").read_bytes())

    def test_browser_record_requires_actual_observation_time(self):
        _, report = self.browser_begin()
        before = (self.pack / "distribution-results.json").read_bytes()
        with self.assertRaisesRegex(ValueError, "RESULT_OBSERVATION_TIME_REQUIRED"):
            self.record_browser(dispatch_id=report["dispatch_id"], verified_at="")
        self.assertEqual(before, (self.pack / "distribution-results.json").read_bytes())

    def test_recording_uncertainty_preserves_saved_identity_and_other_rows(self):
        saved = self.saved("weibo")
        saved["media_id"] = "existing-draft-id"
        self.results([self.saved(), saved])
        with self.assertRaisesRegex(ValueError, "RESULT_REMOTE_OUTCOME_UNRESOLVED"):
            self.record_browser(status="blocked")
        for options in ({"media_id": ""}, {"media_id": "different"}, {"draft_url": ""}):
            with self.assertRaisesRegex(ValueError, "RESULT_REMOTE_ID_CHANGED"):
                self.record_browser(status="unverified", **options)
        self.record_browser(status="unverified")
        current = json.loads((self.pack / "distribution-results.json").read_text())
        self.assertEqual(current["items"][1]["media_id"], "existing-draft-id")
        self.assertEqual(current["items"][1]["draft_url"], saved["draft_url"])
        self.assertEqual(current["items"][1]["proof"], saved["proof"])
        self.assertEqual(current["items"][0]["platform_id"], "toutiao")

    def test_inconsistent_not_attempted_row_with_remote_id_never_dispatches(self):
        for key, value in (("media_id", "12345"), ("draft_url", "https://example.com/draft/12345"), ("draft_verified", True)):
            row = {"status": "not_attempted", key: value}
            decision = router.resume_decision(row, {"quality": "not_attempted"}, retry=True)
            self.assertEqual(decision["action"], "verify_existing")

    def test_observation_time_is_not_refreshed_when_recording_old_evidence(self):
        self.results([self.saved(), {"platform_id": "weibo", "status": "not_attempted"}])
        old = (datetime.now().astimezone() - timedelta(hours=25)).isoformat(timespec="seconds")
        with self.assertRaisesRegex(ValueError, "EVIDENCE_STALE"):
            self.record_browser(status="success", verified_at=old)
        self.record_browser(status="unverified", verified_at=old)
        current = json.loads((self.pack / "distribution-results.json").read_text())
        self.assertEqual(current["items"][1]["evidence"]["verified_at"], old)

    def test_invalid_observation_time_does_not_write(self):
        self.results([self.saved(), {"platform_id": "weibo", "status": "not_attempted"}])
        before = (self.pack / "distribution-results.json").read_bytes()
        for value in ("not-a-date", datetime.now().isoformat(), (datetime.now().astimezone() + timedelta(days=1)).isoformat()):
            with self.assertRaisesRegex(ValueError, "EVIDENCE_TIME_INVALID"):
                self.record_browser(verified_at=value)
            self.assertEqual(before, (self.pack / "distribution-results.json").read_bytes())

    def test_checkpoint_precedes_side_effect_and_timeout_is_not_replayed(self):
        def fail(item, args):
            checkpoint = json.loads((self.pack / "distribution-results.json").read_text())
            self.assertEqual(checkpoint["items"][1]["dispatch_state"], "started")
            raise TimeoutError("DO_NOT_SAVE_THIS_SECRET")
        with mock.patch.object(router, "dispatch_item", side_effect=fail) as call:
            self.assertEqual(self.push(), 2)
            self.args.retry_platforms = "weibo"
            self.assertEqual(self.push(), 2)
            self.assertEqual(call.call_count, 1)
        raw = (self.pack / "distribution-results.json").read_text()
        self.assertNotIn("DO_NOT_SAVE_THIS_SECRET", raw)
        report = router.resume_report(self.pack, self.plan, json.loads(raw))
        self.assertEqual(report["items"][1]["code"], "DISPATCH_OUTCOME_UNKNOWN")

    def test_completed_first_platform_survives_second_platform_crash(self):
        self.args.platforms = "toutiao,weibo"
        with mock.patch.object(router, "dispatch_item", side_effect=[self.saved(), RuntimeError("timeout")]):
            self.assertEqual(self.push(), 2)
        result = json.loads((self.pack / "distribution-results.json").read_text())
        self.assertEqual(result["items"][0]["status"], "draft_created")
        self.assertEqual(result["items"][1]["dispatch_state"], "started")

    def test_changed_inputs_block_before_any_dispatch(self):
        changed = copy.deepcopy(self.bundles)
        changed["content"]["fingerprint"] = "changed"
        self.bundle_mock.return_value = changed
        with mock.patch.object(router, "dispatch_item") as call:
            with self.assertRaisesRegex(ValueError, "RESUME_INPUT_CHANGED"):
                self.push()
            call.assert_not_called()

    def test_repeated_pre_dispatch_blocker_requires_explicit_bounded_retry(self):
        blocked = {"platform_id": "weibo", "status": "needs_credentials", "dispatch_attempts": 1}
        self.results([self.saved(), blocked])
        with mock.patch.object(router, "dispatch_item", return_value=blocked) as call:
            self.assertEqual(self.push(), 2)
            call.assert_not_called()
            self.args.retry_platforms = "weibo"
            self.assertEqual(self.push(), 2)
            self.assertEqual(self.push(), 2)
            self.assertEqual(call.call_count, 1)

    def test_stale_success_is_reverification_not_creation(self):
        stale = self.saved("weibo")
        stale["evidence"]["verified_at"] = (datetime.now().astimezone() - timedelta(hours=25)).isoformat()
        self.results([self.saved(), stale])
        with mock.patch.object(router, "dispatch_item") as call:
            self.assertEqual(self.push(), 2)
            call.assert_not_called()
        audit = router.audit_distribution(self.pack, self.plan, {"items": [stale]})
        self.assertIn("evidence_stale", audit["items"][1]["issues"])

    def test_local_proof_hash_is_bound_and_tampering_rejected(self):
        proof = self.pack / "proof.json"
        proof.write_text("{}")
        saved = self.saved("weibo")
        saved["evidence"].update(reference="proof.json", sha256=router.file_sha256(proof))
        self.assertTrue(router.audit_distribution(self.pack, self.plan, {"items": [self.saved(), saved]})["complete"])
        proof.write_text('{"modified":true}')
        audit = router.audit_distribution(self.pack, self.plan, {"items": [self.saved(), saved]})
        self.assertFalse(audit["complete"])
        self.assertIn("evidence_artifact_hash_missing_or_changed", audit["items"][1]["issues"])

    def test_missing_hash_naive_time_and_future_time_are_not_complete(self):
        proof = self.pack / "proof.json"
        proof.write_text("{}")
        for timestamp, reference in (
            (datetime.now().astimezone().isoformat(), "proof.json"),
            (datetime.now().isoformat(), "12345"),
            ((datetime.now().astimezone() + timedelta(days=1)).isoformat(), "12345"),
        ):
            saved = self.saved("weibo")
            saved["evidence"].update(verified_at=timestamp, reference=reference)
            audit = router.audit_distribution(self.pack, self.plan, {"items": [self.saved(), saved]})
            self.assertFalse(audit["complete"])
            self.assertFalse(dashboard.evidence_fields(saved, pack=self.pack)["trusted"])

    def test_result_corruption_and_duplicate_rows_fail_closed(self):
        self.results([self.saved(), self.saved("weibo")])
        path = self.pack / "distribution-results.json"
        payload = json.loads(path.read_text())
        payload["items"].append(self.saved("weibo"))
        payload["result_id"] = router.result_identity(payload)
        router.atomic_write_json(path, payload)
        with self.assertRaisesRegex(ValueError, "RESUME_RESULTS_INVALID"):
            self.push()
        path.write_text("{")
        with self.assertRaises(json.JSONDecodeError):
            self.push()

    def test_hold_prevents_dispatch(self):
        with mock.patch.object(router, "evaluate_content_workflow", return_value={"ready_for_draft_actions": False, "issues": ["manual_review_hold"]}), mock.patch.object(router, "dispatch_item") as call:
            with self.assertRaisesRegex(ValueError, "RESUME_WORKFLOW_BLOCKED"):
                self.push()
            call.assert_not_called()

    def test_plan_mismatch_does_not_erase_real_results_in_recorder(self):
        old = {"schema_version": 2, "inputs": {"plan_id": "old"}, "items": [self.saved()]}
        with self.assertRaisesRegex(ValueError, "RESULT_PLAN_CHANGED"):
            recorder.bootstrap_results_from_dry_run(self.pack, old)

    def test_unconfirmed_push_does_not_write_formal_results(self):
        self.args.confirm_drafts = False
        with mock.patch.object(router, "dispatch_item") as call:
            self.assertEqual(self.push(), 2)
            call.assert_not_called()
        self.assertFalse((self.pack / "distribution-results.json").exists())

    def test_user_deferred_platform_cannot_be_forced_by_retry(self):
        decision = router.resume_decision({"status": "skipped"}, {"quality": "not_attempted"}, retry=True)
        self.assertEqual(decision, {"action": "wait_for_input", "code": "USER_DEFERRED_PLATFORM"})

    def test_resolved_credentials_do_not_require_mutating_plan(self):
        item = {**self.plan["items"][0], "missing_credentials": ["EXAMPLE_CREDENTIAL"]}
        with mock.patch.object(router, "configured", return_value=(True, [], "test")) as check:
            outcome = router.dispatch_item(item, self.args)
        self.assertEqual(outcome["status"], "manual_required")
        check.assert_called_once_with(["EXAMPLE_CREDENTIAL"])
        self.assertEqual(item["missing_credentials"], ["EXAMPLE_CREDENTIAL"])

    def test_stage_removes_upload_preview_for_all_non_dispatch_decisions(self):
        self.results([self.saved(), self.saved("weibo")])
        router.atomic_write_json(self.pack / "distribution-plan.json", {
            **self.plan, "items": self.plan["items"] + [{"platform_id": "xiaohongshu"}],
        })
        for action in ("skip_verified", "verify_existing", "wait_for_input"):
            with mock.patch.object(adapter, "build_job", return_value={
                "platform_id": "xiaohongshu", "status": "ready_for_manual_adapter_review",
                "command_preview": ["do-not-run"], "problems": [], "execution_enabled": False,
            }), mock.patch.object(adapter, "evaluate_content_workflow", return_value={"ready_for_draft_actions": True}), mock.patch.object(router, "resume_context", return_value=(self.plan, self.dry, {})), mock.patch.object(router, "resume_report", return_value={"items": [
                {"platform_id": "xiaohongshu", "action": action, "code": "test"}
            ]}):
                stage = adapter.stage(self.pack, self.pack / "no-upstream", ["xiaohongshu"])
            self.assertEqual(stage["jobs"][0]["status"], action)
            self.assertEqual(stage["jobs"][0]["command_preview"], [])
            self.assertTrue(stage["inputs"]["results_file_sha256"])

    def test_dashboard_does_not_trust_missing_or_changed_hash(self):
        proof = self.pack / "proof.json"
        proof.write_text("{}")
        saved = self.saved()
        saved["evidence"]["reference"] = "proof.json"
        self.assertFalse(dashboard.evidence_fields(saved, pack=self.pack)["trusted"])
        saved["evidence"]["sha256"] = router.file_sha256(proof)
        self.assertTrue(dashboard.evidence_fields(saved, pack=self.pack)["trusted"])
        proof.write_text("modified")
        self.assertFalse(dashboard.evidence_fields(saved, pack=self.pack)["trusted"])

    def test_review_scores_and_pinned_sources_are_consistent(self):
        root = Path(__file__).resolve().parents[1]
        report = json.loads((root / "research/github-agent-review-2026-09-28.json").read_text())
        maxima = list(report["weights"].values())
        self.assertEqual(sum(maxima), 100)
        for candidate in report["candidates"]:
            self.assertEqual(len(candidate["scores"]), len(maxima))
            self.assertEqual(sum(candidate["scores"]), candidate["total"])
            self.assertTrue(all(0 <= score <= maximum for score, maximum in zip(candidate["scores"], maxima)))
            self.assertRegex(candidate["commit"], r"^[a-f0-9]{40}$")
            self.assertIn(candidate["commit"], candidate["source"])


if __name__ == "__main__":
    unittest.main()
