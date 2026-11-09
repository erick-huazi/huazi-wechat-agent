from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import distribution_io as storage
import distribution_dashboard as dashboard
import social_draft_router as router


class LedgerIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.pack = Path(self.temp.name)
        self.log = self.pack / "distribution-evidence.jsonl"
        self.anchor = self.pack / "distribution-evidence-chain.json"

    def snapshot(self):
        return {p.relative_to(self.pack).as_posix(): p.read_bytes()
                for p in self.pack.rglob("*") if p.is_file()}

    def seed(self):
        storage.append_chained_jsonl(self.log, self.anchor, [{"event": "first"}])

    def assert_refused(self, code):
        before = self.snapshot()
        self.assertEqual(storage.inspect_chained_jsonl(self.log, self.anchor)["issue_code"], code)
        with self.assertRaisesRegex(ValueError, code):
            storage.append_chained_jsonl(self.log, self.anchor, [{"event": "new"}])
        self.assertEqual(before, self.snapshot())

    def test_pristine_pair_and_empty_append_are_read_only(self):
        self.assertTrue(storage.inspect_chained_jsonl(self.log, self.anchor)["valid"])
        self.assertEqual(storage.append_chained_jsonl(self.log, self.anchor, [])["appended"], 0)
        self.assertEqual(self.snapshot(), {})

    def test_legacy_records_require_explicit_review_not_automatic_migration(self):
        self.log.write_text('{"event":"old"}\n', encoding="utf-8")
        self.assert_refused("LEDGER_LEGACY_REVIEW_REQUIRED")

    def test_mixed_chain_is_not_rebuilt(self):
        self.seed()
        with self.log.open("a", encoding="utf-8") as stream:
            stream.write('{"event":"unchained"}\n')
        self.assert_refused("LEDGER_MIXED_CHAIN")

    def test_missing_anchor_is_not_recreated(self):
        self.seed()
        self.anchor.unlink()
        self.assert_refused("LEDGER_ANCHOR_MISSING")

    def test_orphan_anchor_including_empty_history_is_not_ignored(self):
        for entries, head in ((0, ""), (1, "a" * 64)):
            storage.atomic_write_json(self.anchor, {"schema_version": 1, "entries": entries, "head_hash": head})
            self.assert_refused("LEDGER_FILE_MISSING")

    def test_invalid_anchor_shapes_have_stable_errors(self):
        self.seed()
        for payload in ([], None, "text", {"schema_version": 1, "entries": True, "head_hash": "a" * 64},
                        {"schema_version": 1, "entries": 1, "head_hash": []}):
            with self.subTest(payload=payload):
                storage.atomic_write_json(self.anchor, payload)
                self.assert_refused("LEDGER_ANCHOR_INVALID")

    def test_truncated_or_modified_history_cannot_be_repaired_by_append(self):
        self.seed()
        original = self.log.read_bytes()
        self.log.write_bytes(b"")
        self.assert_refused("LEDGER_ANCHOR_MISMATCH")
        self.log.write_bytes(original.replace(b"first", b"other"))
        self.assert_refused("LEDGER_HASH_CHAIN_BROKEN")

    def test_bad_records_and_bad_encoding_do_not_crash_inspection(self):
        for content, code in ((b"[]\n", "LEDGER_RECORD_INVALID"),
                              (b"{\n", "LEDGER_RECORD_INVALID"),
                              (b"\xff\xfe", "LEDGER_READ_ERROR")):
            self.log.write_bytes(content)
            self.assert_refused(code)

    def test_append_preserves_existing_bytes_spacing_and_line_endings(self):
        self.seed()
        row = json.loads(self.log.read_text(encoding="utf-8"))
        for ending in ("\r\n\r\n", "", "\n"):
            with self.subTest(ending=ending):
                self.log.write_bytes((json.dumps(row, separators=(", ", ": ")) + ending).encode("utf-8"))
                storage.atomic_write_json(self.anchor, {"schema_version": 1, "entries": 1, "head_hash": row["record_hash"]})
                before = self.log.read_bytes()
                storage.append_chained_jsonl(self.log, self.anchor, [{"event": "second"}])
                self.assertTrue(self.log.read_bytes().startswith(before))
                inspected = storage.inspect_chained_jsonl(self.log, self.anchor)
                self.assertTrue(inspected["valid"])
                self.assertEqual(inspected["records"][1]["previous_hash"], row["record_hash"])

    def test_empty_append_does_not_touch_existing_pair(self):
        self.seed()
        before = self.snapshot()
        times = [p.stat().st_mtime_ns for p in (self.log, self.anchor)]
        storage.append_chained_jsonl(self.log, self.anchor, [])
        self.assertEqual(before, self.snapshot())
        self.assertEqual(times, [p.stat().st_mtime_ns for p in (self.log, self.anchor)])

    def test_anchor_write_interruption_is_detected_on_next_attempt(self):
        self.seed()
        with mock.patch.object(storage, "atomic_write_json", side_effect=OSError("simulated interruption")):
            with self.assertRaises(OSError):
                storage.append_chained_jsonl(self.log, self.anchor, [{"event": "second"}])
        self.assert_refused("LEDGER_ANCHOR_MISMATCH")

    def test_new_invalid_row_cannot_modify_a_valid_chain(self):
        self.seed()
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, "LEDGER_RECORD_INVALID"):
            storage.append_chained_jsonl(self.log, self.anchor, [None])
        self.assertEqual(before, self.snapshot())

    def test_evidence_deduplication_still_checks_a_missing_anchor(self):
        results = {"items": [{"platform_id": "weibo", "status": "unverified"}]}
        audit = {"items": [{"platform_id": "weibo", "quality": "unverified"}]}
        self.assertEqual(router.append_evidence_ledger(self.pack, results, audit), 1)
        before = self.snapshot()
        self.assertEqual(router.append_evidence_ledger(self.pack, results, audit), 0)
        self.assertEqual(before, self.snapshot())
        self.anchor.unlink()
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, "LEDGER_ANCHOR_MISSING"):
            router.append_evidence_ledger(self.pack, results, audit)
        self.assertEqual(before, self.snapshot())

    def audit_args(self):
        return router.build_parser().parse_args(["audit", "--pack", str(self.pack)])

    def seed_core_files(self):
        storage.atomic_write_json(self.pack / "distribution-plan.json", {"title": "Test", "items": [{"platform_id": "weibo"}]})
        storage.atomic_write_json(self.pack / "distribution-results.json", {"items": [{"platform_id": "weibo", "status": "not_attempted"}]})

    def test_audit_rejects_chain_before_id_backfill_backup_or_report_writes(self):
        self.seed_core_files()
        self.seed()
        self.log.unlink()
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, "LEDGER_FILE_MISSING"):
            router.command_audit(self.audit_args())
        self.assertEqual(before, self.snapshot())

    def test_valid_audit_retains_prewrite_recovery_point(self):
        self.seed_core_files()
        before = (self.pack / "distribution-plan.json").read_bytes()
        with contextlib.redirect_stdout(io.StringIO()):
            router.command_audit(self.audit_args())
        backups = storage.list_pack_backups(self.pack, verify=True)
        self.assertEqual(len(backups), 1)
        self.assertTrue(backups[0]["verification"]["valid"])
        backup = Path(backups[0]["path"])
        self.assertEqual((backup / "distribution-plan.json").read_bytes(), before)
        self.assertTrue(storage.inspect_chained_jsonl(self.log, self.anchor)["valid"])

    def test_dashboard_rejects_broken_action_chain_before_subprocess_or_mutation(self):
        self.anchor = self.pack / "distribution-dashboard-actions-chain.json"
        self.log = self.pack / "distribution-dashboard-actions.jsonl"
        self.seed()
        self.log.unlink()
        before = self.snapshot()
        with mock.patch.object(dashboard.subprocess, "run") as process:
            with self.assertRaisesRegex(ValueError, "LEDGER_FILE_MISSING"):
                dashboard.run_safe_action(self.pack, "audit")
            process.assert_not_called()
        self.assertEqual(before, self.snapshot())

    def test_dashboard_rejects_broken_evidence_chain_before_subprocess(self):
        self.seed()
        self.anchor.unlink()
        before = self.snapshot()
        with mock.patch.object(dashboard.subprocess, "run") as process:
            with self.assertRaisesRegex(ValueError, "LEDGER_ANCHOR_MISSING"):
                dashboard.run_safe_action(self.pack, "dry-run")
            process.assert_not_called()
        self.assertEqual(before, self.snapshot())

    def test_dashboard_reports_orphan_anchor_with_stable_code_and_action(self):
        self.seed()
        self.log.unlink()
        health = dashboard.pack_data_health(self.pack)
        issue = next(row for row in health["issues"] if row["code"] == "evidence.LEDGER_FILE_MISSING")
        self.assertEqual(issue["severity"], "error")
        self.assertTrue(issue["action"])
        self.assertFalse(health["ledger_anchor"]["valid"])

    def test_audit_rejects_broken_action_chain_before_writes(self):
        self.seed_core_files()
        storage.atomic_write_json(self.pack / "distribution-dashboard-actions-chain.json",
                                  {"schema_version": 1, "entries": 0, "head_hash": ""})
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, "LEDGER_FILE_MISSING"):
            router.command_audit(self.audit_args())
        self.assertEqual(before, self.snapshot())

    def test_ledger_status_is_read_only_and_does_not_load_credentials(self):
        self.seed()
        self.anchor.unlink()
        before = self.snapshot()
        argv = ["router", "ledger-status", "--pack", str(self.pack)]
        output = io.StringIO()
        with mock.patch.object(sys, "argv", argv), mock.patch.object(router, "load_dotenv") as credentials, contextlib.redirect_stdout(output):
            self.assertEqual(router.main(), 2)
            credentials.assert_not_called()
        report = json.loads(output.getvalue())
        self.assertEqual(report["chains"]["evidence"]["issue_code"], "LEDGER_ANCHOR_MISSING")
        self.assertTrue(report["read_only"])
        self.assertNotIn("records", report["chains"]["evidence"])
        self.assertEqual(before, self.snapshot())

    def test_ledger_status_verifies_only_latest_backup_without_creating_one(self):
        self.seed()
        self.seed_core_files()
        backup = storage.create_pack_backup(self.pack, "test", ["distribution-plan.json"])
        before = self.snapshot()
        argv = router.build_parser().parse_args(["ledger-status", "--pack", str(self.pack), "--verify-backup"])
        output = io.StringIO()
        with mock.patch.object(storage, "verify_pack_backup", wraps=storage.verify_pack_backup) as verify, contextlib.redirect_stdout(output):
            self.assertEqual(router.command_ledger_status(argv), 0)
            verify.assert_called_once_with(backup)
        self.assertTrue(json.loads(output.getvalue())["latest_backup"]["verification"]["valid"])
        self.assertEqual(before, self.snapshot())

    def test_ledger_status_reports_missing_requested_backup_without_writing(self):
        args = router.build_parser().parse_args(["ledger-status", "--pack", str(self.pack), "--verify-backup"])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(router.command_ledger_status(args), 2)
        self.assertIsNone(json.loads(output.getvalue())["latest_backup"])
        self.assertEqual(self.snapshot(), {})

    def test_audit_does_not_load_credentials(self):
        self.seed_core_files()
        argv = ["router", "audit", "--pack", str(self.pack)]
        with mock.patch.object(sys, "argv", argv), mock.patch.object(router, "load_dotenv") as credentials, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(router.main(), 0)
            credentials.assert_not_called()


if __name__ == "__main__":
    unittest.main()
