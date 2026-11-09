import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import editorial_health_check as health


class EditorialHealthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.pack = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        defaults = [
            patch.object(health, "inspect_chained_jsonl", return_value={"valid": True, "issue_code": ""}),
            patch.object(health, "list_pack_backups", return_value=[{"path": str(self.pack / ".backups" / "latest")}]),
            patch.object(health, "verify_pack_backup", return_value={"valid": True}),
            patch.object(health, "compare_pack_backup", return_value={"unchanged": ["a"], "changed": [], "missing": [], "added": []}),
            patch.object(health, "evaluate_pack", return_value={"ready_for_draft_actions": True, "stage_checks": {"draft_verified": True}, "issues": []}),
            patch.object(health, "resume_context", return_value=({"plan_id": "p"}, {}, {})),
            patch.object(health, "resume_report", return_value={"plan_id": "p", "items": [{"platform_id": "wechat", "action": "skip_verified"}]}),
        ]
        for p in defaults:
            p.start()
            self.addCleanup(p.stop)

    def test_ready_is_not_authorization(self):
        result = health.inspect_pack(self.pack)
        self.assertFalse(result["requires_attention"])
        self.assertEqual("read_only_diagnostic", result["authority"])
        self.assertEqual(1, result["latest_backup"]["comparison_counts"]["unchanged"])

    def test_relative_pack_normalized_before_inspection(self):
        with patch.object(health, "evaluate_pack", return_value={"ready_for_draft_actions": True}) as check:
            relative = Path.cwd().relative_to(Path.cwd()) / Path(__file__).resolve().relative_to(Path.cwd()).parent
            health.inspect_pack(relative)
            self.assertEqual(relative.resolve(), check.call_args.args[0])

    def test_unknown_remote_is_never_dispatch(self):
        with patch.object(health, "resume_context", side_effect=ValueError("secret payload must not appear")):
            result = health.inspect_pack(self.pack)
        self.assertTrue(result["requires_attention"])
        self.assertEqual("inspect_existing_before_any_remote_write", result["resume"]["action"])
        self.assertNotIn("secret payload", str(result))

    def test_verify_existing_and_chain_failure_require_attention(self):
        with patch.object(health, "resume_report", return_value={"items": [{"action": "verify_existing"}]}):
            self.assertTrue(health.inspect_pack(self.pack)["requires_attention"])
        with patch.object(health, "inspect_chained_jsonl", return_value={"valid": False, "issue_code": "BROKEN"}):
            self.assertTrue(health.inspect_pack(self.pack)["requires_attention"])

    def test_missing_or_invalid_backup_requires_attention(self):
        with patch.object(health, "list_pack_backups", return_value=[]):
            self.assertEqual("missing", health.inspect_pack(self.pack)["latest_backup"]["status"])
            self.assertTrue(health.inspect_pack(self.pack)["requires_attention"])
        with patch.object(health, "verify_pack_backup", return_value={"valid": False}):
            self.assertEqual("invalid", health.inspect_pack(self.pack)["latest_backup"]["status"])
            self.assertTrue(health.inspect_pack(self.pack)["requires_attention"])

    def test_workflow_failure_is_unknown_not_success(self):
        with patch.object(health, "evaluate_pack", side_effect=ValueError("private details")):
            result = health.inspect_pack(self.pack)
        self.assertEqual("invalid_or_unreadable", result["workflow"]["status"])
        self.assertTrue(result["requires_attention"])
        self.assertNotIn("private details", str(result))

    def test_delivery_separates_pending_verified_and_unknown(self):
        def state(verified, actions):
            return health.delivery_state({"status": "evaluated", "stage_checks": {"draft_verified": verified}},
                {"status": "evaluated", "items": [{"platform_id": f"p{i}", "action": action} for i, action in enumerate(actions)]})
        self.assertEqual("pending", state(False, ["dispatch"])["status"])
        self.assertEqual("verified", state(True, ["skip_verified"])["status"])
        self.assertEqual("needs_review", state(False, ["verify_existing"])["status"])
        self.assertEqual("needs_review", state(False, ["wait_for_input"])["status"])
        self.assertEqual("inconsistent", state(False, ["skip_verified"])["status"])
        self.assertEqual("inconsistent", state(True, ["dispatch"])["status"])
        self.assertEqual("unknown", state(False, ["unexpected"])["status"])
        self.assertEqual("unknown", state(False, [])["status"])

    def test_duplicate_ids_or_missing_audit_fail_closed(self):
        resume = {"status": "evaluated", "items": [{"platform_id": "wechat", "action": "dispatch"},
                                                       {"platform_id": "wechat", "action": "dispatch"}]}
        self.assertEqual("unknown", health.delivery_state({"status": "evaluated", "stage_checks": {"draft_verified": False}}, resume)["status"])
        resume["items"].pop()
        self.assertEqual("unknown", health.delivery_state({"status": "evaluated", "stage_checks": {}}, resume)["status"])


if __name__ == "__main__":
    unittest.main()
