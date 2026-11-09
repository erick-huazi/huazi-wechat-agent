from __future__ import annotations

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
sys.path.insert(0, str(ROOT / "scripts"))
import source_evidence as sources
import research_evidence_gate as gate
import image_quality_gate as images
import social_draft_router as router
import distribution_dashboard as dashboard
import content_workflow_gate as workflow
from distribution_io import canonical_sha256, file_sha256, PackLockError, list_pack_backups
from test_research_evidence_gate import write_pack

NOW = datetime.fromisoformat("2026-10-07T08:00:00+08:00")
QUOTE = "In evaluation A, Model A returned a rate of 48% and Model B returned 0%."
TEXT = "Official evaluation notes, with scope and limitations.\n" + QUOTE + "\nThese rates describe this evaluation only."


class SourceEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.pack = Path(self.temp.name) / "2026-10-07-grounding-huazi"
        self.pack.mkdir()
        write_pack(self.pack)
        self.claim_path = self.pack / "claim-verification.json"
        self.payload = json.loads(self.claim_path.read_text(encoding="utf-8"))
        self.payload.update(schema_version=3, verified_at=NOW.isoformat())
        self.input = self.pack.parent / "public-source.txt"
        self.input.write_text(TEXT, encoding="utf-8")
        for source, claim in zip(self.payload["sources"], self.payload["claims"]):
            source.update(checked_at=NOW.isoformat(), evidence_excerpt=QUOTE)
            source["source_snapshot"] = self.capture(source)
            claim["evidence_spans"] = [{"source_id": source["source_id"], **sources.locate(TEXT, QUOTE)}]
        self.save()

    def capture(self, source, **kwargs):
        return sources.import_source(self.pack, self.input, source_id=source["source_id"],
                                     url=source["url"], title=source["title"],
                                     captured_at=kwargs.pop("captured_at", NOW.isoformat()), now=NOW, **kwargs)

    def save(self):
        self.claim_path.write_text(json.dumps(self.payload), encoding="utf-8")

    def report(self):
        self.save()
        return gate.evaluate_pack(self.pack, now=NOW)

    def numeric(self):
        c = self.payload["claims"][0]
        c["text"] = "Model A: 48%; Model B: 0%."
        c["metric_context"] = {
            "evaluation_name": "A", "metric_name": "evaluation A rate", "test_condition": "same evaluation",
            "source_locator": "Official evaluation notes", "evidence_source_id": "S1", "evidence_excerpt": QUOTE,
            "comparison_scope": "Model A and Model B in evaluation A only",
            "reported_values": [{"label": "A", "claim_value": "48%", "source_value": "48%"},
                                {"label": "B", "claim_value": "0%", "source_value": "0%"}],
        }

    def test_grounded_claims_are_accepted_without_claiming_truth(self):
        report = self.report()
        self.assertTrue(report["passed"], report["issues"])
        self.assertEqual(report["source_grounding"]["grounded_claim_count"], 2)
        self.assertIn("not_semantic_truth", report["source_grounding"]["assurance"])
        self.assertTrue(gate.report_is_current(report, self.pack, now=NOW))

    def test_old_pack_does_not_require_retroactive_migration(self):
        old = self.pack.parent / "2026-10-06-legacy-huazi"
        old.mkdir()
        write_pack(old)
        payload = json.loads((old / "claim-verification.json").read_text(encoding="utf-8"))
        payload["schema_version"] = 3
        (old / "claim-verification.json").write_text(json.dumps(payload), encoding="utf-8")
        report = gate.evaluate_pack(old, now=datetime.fromisoformat("2026-08-23T08:00:00+08:00"))
        self.assertTrue(report["passed"], report["issues"])
        self.assertNotIn("source_grounding", report)

    def test_opt_in_cannot_be_disabled_by_false_flag(self):
        self.payload["source_grounding_required"] = False
        self.assertTrue(sources.policy_required(Path("2026-09-01-old"), self.payload))

    def test_new_pack_requires_snapshots_and_spans(self):
        for row in self.payload["sources"]:
            row.pop("source_snapshot")
        for row in self.payload["claims"]:
            row.pop("evidence_spans")
        issues = self.report()["issues"]
        self.assertIn("SOURCE_SNAPSHOT_REQUIRED:S1", issues)
        self.assertIn("CLAIM_EVIDENCE_SPAN_REQUIRED:C1", issues)

    def test_fabricated_source_quote_blocks(self):
        self.payload["sources"][0]["evidence_excerpt"] = "An invented conclusion that the source never made."
        self.assertIn("SOURCE_EXCERPT_NOT_FOUND:S1", self.report()["issues"])

    def test_invalid_offsets_types_and_quotes_fail_closed(self):
        good = dict(self.payload["claims"][0]["evidence_spans"][0])
        for fields in ({"start": -1}, {"start": True}, {"end": 9999999}, {"end": "3"},
                       {"quote": QUOTE.lower()}, {"source_id": "S9"}, {"source_id": []}):
            with self.subTest(fields=fields):
                self.payload["claims"][0]["evidence_spans"][0] = {**good, **fields}
                self.assertIn("CLAIM_EVIDENCE_SPAN_INVALID:C1", self.report()["issues"])

    def test_every_cited_source_needs_grounded_span(self):
        self.payload["claims"][0]["source_ids"].append("S2")
        self.assertIn("CLAIM_CITED_SOURCE_UNGROUNDED:C1", self.report()["issues"])

    def test_numeric_quote_must_be_from_same_source_and_span(self):
        self.numeric()
        self.assertTrue(self.report()["passed"])
        self.payload["claims"][0]["metric_context"]["evidence_source_id"] = "S2"
        self.assertIn("NUMERIC_EXCERPT_NOT_GROUNDED:C1", self.report()["issues"])

    def test_same_numbers_but_invented_metric_excerpt_blocked(self):
        self.numeric()
        self.payload["claims"][0]["metric_context"]["evidence_excerpt"] = "Unauthorized goal overreach: A 48%, B 0%."
        claim = self.payload["claims"][0]
        # The older value-only contract accepts these same numbers; grounding must not.
        self.assertEqual(gate.validate_numeric_evidence_contract("C1", claim["text"], claim["source_ids"], claim["metric_context"]), [])
        self.assertIn("NUMERIC_EXCERPT_NOT_GROUNDED:C1", self.report()["issues"])

    def test_snapshot_same_size_same_mtime_edit_invalidates_report(self):
        report = self.report()
        path = self.pack / self.payload["sources"][0]["source_snapshot"]["path"]
        stat = path.stat()
        path.write_bytes(path.read_bytes().replace(b"48%", b"49%"))
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        self.assertFalse(gate.report_is_current(report, self.pack, now=NOW))
        self.assertIn("SOURCE_SNAPSHOT_HASH_MISMATCH:S1", self.report()["issues"])

    def test_rehashing_changed_document_does_not_refresh_spans(self):
        ref = self.payload["sources"][0]["source_snapshot"]
        path = self.pack / ref["path"]
        doc = json.loads(path.read_text(encoding="utf-8"))
        doc["text"] = doc["text"].replace("48%", "49%")
        doc["snapshot_id"] = canonical_sha256({k: v for k, v in doc.items() if k != "snapshot_id"})
        path.write_text(json.dumps(doc), encoding="utf-8")
        ref["sha256"] = file_sha256(path)
        self.assertIn("CLAIM_EVIDENCE_SPAN_INVALID:C1", self.report()["issues"])

    def test_source_identity_mismatch_blocks(self):
        self.payload["sources"][0]["url"] = "https://example.org/different-report"
        self.assertIn("SOURCE_SNAPSHOT_IDENTITY_MISMATCH:S1", self.report()["issues"])

    def test_external_and_traversal_paths_never_read(self):
        for name in ("../secret.json", "sources/../../secret.json", "C:/secret.json", "/tmp/secret.json", "sources/a:stream.json"):
            with self.subTest(name=name), mock.patch.object(Path, "open", side_effect=AssertionError("must not read")):
                with self.assertRaisesRegex(ValueError, "PATH_INVALID"):
                    sources.read_snapshot(self.pack, {"path": name, "sha256": "0" * 64})

    def test_symlink_outside_pack_rejected_before_read(self):
        path = self.pack / "sources/link.json"
        with mock.patch.object(Path, "resolve", autospec=True, side_effect=lambda p: self.pack.parent / "outside.json" if p == path else p):
            with self.assertRaisesRegex(ValueError, "PATH_INVALID"):
                sources.read_snapshot(self.pack, {"path": "sources/link.json"})

    def test_missing_corrupt_and_non_object_snapshots_block(self):
        ref = self.payload["sources"][0]["source_snapshot"]
        path = self.pack / ref["path"]
        for raw in (b"broken json", b"[]", b"{}"):
            path.write_bytes(raw)
            ref["sha256"] = file_sha256(path)
            self.assertFalse(self.report()["passed"])
        path.unlink()
        self.assertIn("SOURCE_SNAPSHOT_UNREADABLE:S1", self.report()["issues"])

    def test_old_capture_and_future_capture_block(self):
        source = self.payload["sources"][0]
        source["source_snapshot"] = self.capture(source, captured_at=(NOW - timedelta(days=4)).isoformat())
        self.assertIn("SOURCE_SNAPSHOT_STALE:S1", self.report()["issues"])
        with self.assertRaisesRegex(ValueError, "CAPTURE_TIME_INVALID"):
            self.capture(source, captured_at=(NOW + timedelta(hours=1)).isoformat())

    def test_timezone_required_and_capture_is_not_publication_time(self):
        with self.assertRaisesRegex(ValueError, "CAPTURE_TIME_INVALID"):
            self.capture(self.payload["sources"][0], captured_at="2026-10-07T08:00:00")
        doc, _ = sources.read_snapshot(self.pack, self.payload["sources"][0]["source_snapshot"])
        self.assertNotIn("published_at", doc)

    def test_import_idempotent_and_immutable(self):
        source = self.payload["sources"][0]
        before = list_pack_backups(self.pack)
        self.assertEqual(self.capture(source), source["source_snapshot"])
        self.assertEqual(len(list_pack_backups(self.pack)), len(before))
        (self.pack / source["source_snapshot"]["path"]).write_text("modified", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "IMMUTABLE_CONFLICT"):
            self.capture(source)

    def test_lock_conflict_never_writes_source(self):
        with mock.patch.object(sources, "pack_lock", side_effect=PackLockError("busy")), mock.patch.object(sources, "atomic_write_json") as writer:
            with self.assertRaises(PackLockError):
                self.capture(self.payload["sources"][0])
            writer.assert_not_called()

    def test_text_import_never_calls_network_or_trafilatura(self):
        with mock.patch("urllib.request.urlopen", side_effect=AssertionError("no network")), mock.patch("trafilatura.extract", side_effect=AssertionError("no extraction")):
            self.capture(self.payload["sources"][0])

    def test_real_html_extractor_preserves_numeric_claim(self):
        self.input.write_text("<html><nav>HOME</nav><article><h1>Official evaluation</h1><p>" + TEXT + "</p><p>"
                              + "The testing conditions are specific to this report. " * 6 + "</p></article><script>privateCode()</script></html>", encoding="utf-8")
        ref = self.capture(self.payload["sources"][0], input_format="html")
        doc, _ = sources.read_snapshot(self.pack, ref)
        self.assertIn(QUOTE, doc["text"])
        self.assertNotIn("privateCode", doc["text"])
        self.assertEqual(doc["extractor"], "trafilatura:2.3.0")

    def test_empty_extraction_no_fallback_to_navigation(self):
        with mock.patch("trafilatura.extract", return_value=None):
            with self.assertRaisesRegex(ValueError, "SOURCE_TEXT_EMPTY"):
                self.capture(self.payload["sources"][0], input_format="html")

    def test_credential_urls_and_local_urls_are_rejected(self):
        for url in ("https://user:secret@example.com/", "https://example.com/?access_token=secret", "file:///secret",
                    "http://example.com/", "https://127.0.0.1/", "https://localhost/", "https://a.local/", "https://example.com:444/"):
            with self.subTest(url=url):
                self.assertFalse(sources.public_url(url))

    def test_ambiguous_quote_needs_explicit_character_offset(self):
        with self.assertRaisesRegex(ValueError, "AMBIGUOUS"):
            sources.locate(QUOTE + "\n" + QUOTE, QUOTE)
        self.assertEqual(sources.locate(QUOTE + "\n" + QUOTE, QUOTE, len(QUOTE) + 1)["start"], len(QUOTE) + 1)
        with self.assertRaisesRegex(ValueError, "NOT_FOUND"):
            sources.locate(TEXT, "invented source quotation")

    def test_input_too_large_blocks_before_extraction(self):
        with mock.patch.object(sources, "MAX_BYTES", 10):
            with self.assertRaisesRegex(ValueError, "SOURCE_INPUT_TOO_LARGE"):
                self.capture(self.payload["sources"][0])

    def test_snapshot_included_in_image_and_dryrun_fingerprints(self):
        a = images.input_snapshot(self.pack, images.load_standard())
        b = router.workflow_bundles(self.pack, {"items": []})["quality_gates"]
        snapshot_name = self.payload["sources"][0]["source_snapshot"]["path"]
        self.assertIn(snapshot_name, [x["path"] for x in a])
        self.assertIn(snapshot_name, [x["path"] for x in b["files"]])
        (self.pack / snapshot_name).write_text("changed", encoding="utf-8")
        self.assertNotEqual(a, images.input_snapshot(self.pack, images.load_standard()))
        self.assertNotEqual(b["fingerprint"], router.workflow_bundles(self.pack, {"items": []})["quality_gates"]["fingerprint"])

    def test_image_label_cannot_use_ungrounded_quote(self):
        self.numeric()
        self.payload["claims"][0]["metric_context"]["evidence_excerpt"] = "Invented metric with 48% and 0%."
        rows = images.numeric_label_issues([{"image": "test.png", "on_image_text": "48% vs 0%", "claim_ids": ["C1"]}], self.payload, self.pack)
        self.assertEqual(rows[0]["code"], "IMAGE_NUMERIC_SOURCE_UNGROUNDED")

    def test_dashboard_detects_source_change_even_with_old_size_and_mtime(self):
        before = dashboard.dashboard_revision(self.pack.parent, self.pack.name)
        path = self.pack / self.payload["sources"][0]["source_snapshot"]["path"]
        stat = path.stat()
        path.write_bytes(path.read_bytes().replace(b"48%", b"49%"))
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        self.assertNotEqual(before, dashboard.dashboard_revision(self.pack.parent, self.pack.name))

    def test_unicode_offsets_are_codepoints_not_utf8_bytes(self):
        quote = "\u8fd9\u4e2a\u6307\u6807\u53ea\u9002\u7528\u4e8e\u540c\u4e00\u6d4b\u8bd5\u6761\u4ef6: 48% and 0%."
        text = "\u65b0\u95fb\U0001f4cc\n" + quote
        span = sources.locate(text, quote)
        self.assertEqual(span["start"], 4)
        self.assertTrue(sources.checked_span(text, span))

    def test_malformed_url_types_fail_closed(self):
        for value in (None, False, 42, [], {}, ["https://example.org"]):
            with self.subTest(value=value):
                self.assertFalse(sources.public_url(value))

    def test_boolean_snapshot_schema_is_not_integer_version(self):
        ref = self.payload["sources"][0]["source_snapshot"]
        path = self.pack / ref["path"]
        doc = json.loads(path.read_text(encoding="utf-8"))
        doc["schema_version"] = True
        doc["snapshot_id"] = canonical_sha256({k: v for k, v in doc.items() if k != "snapshot_id"})
        path.write_text(json.dumps(doc), encoding="utf-8")
        ref["sha256"] = file_sha256(path)
        self.assertIn("SOURCE_SNAPSHOT_INVALID:S1", self.report()["issues"])

    def test_fact_workflow_gate_closes_when_snapshot_changes(self):
        notes = "# Independent review\n" + "The report has scoped findings and limitations.\n" * 4
        notes += "\n".join(row["url"] for row in self.payload["sources"])
        (self.pack / "fact-check-notes.md").write_text(notes, encoding="utf-8")
        (self.pack / "research-evidence-report.json").write_text(json.dumps(self.report()), encoding="utf-8")
        with mock.patch.object(gate, "datetime", wraps=datetime) as clock:
            clock.now.return_value = NOW
            self.assertTrue(workflow.fact_gate(self.pack)["passed"])
            (self.pack / self.payload["sources"][0]["source_snapshot"]["path"]).write_text("changed", encoding="utf-8")
            self.assertFalse(workflow.fact_gate(self.pack)["passed"])

    def test_actual_cli_import_and_locate_do_not_mark_claims_verified(self):
        before = self.claim_path.read_bytes()
        script = str(ROOT / "scripts/source_evidence.py")
        command = [sys.executable, "-X", "utf8", script, "import", "--pack", str(self.pack),
                   "--input", str(self.input), "--source-id", "CLI", "--url", "https://example.org/report",
                   "--title", "Public test source", "--captured-at", datetime.now().astimezone().isoformat(),
                   "--public-source-confirmed"]
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        ref = json.loads(result.stdout)
        result = subprocess.run([sys.executable, "-X", "utf8", script, "locate", "--pack", str(self.pack),
                                 "--snapshot", ref["path"], "--sha256", ref["sha256"], "--quote", QUOTE],
                                capture_output=True, text=True, encoding="utf-8", timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(json.loads(result.stdout)["quote"], QUOTE)
        self.assertEqual(self.claim_path.read_bytes(), before)

    def rewrite_snapshot(self, change):
        ref = self.payload["sources"][0]["source_snapshot"]
        path = self.pack / ref["path"]
        doc = json.loads(path.read_text(encoding="utf-8"))
        change(doc)
        doc["snapshot_id"] = canonical_sha256({k: v for k, v in doc.items() if k != "snapshot_id"})
        path.write_text(json.dumps(doc), encoding="utf-8")
        ref["sha256"] = file_sha256(path)
        return ref, path

    def test_malformed_verdict_generates_report_not_exception(self):
        for verdict in ([], {}, None, 4):
            with self.subTest(verdict=verdict):
                self.payload["claims"][0]["verdict"] = verdict
                report = self.report()
                self.assertFalse(report["passed"])
                self.assertTrue(any(i.startswith("claim_verdict_invalid:C1") for i in report["issues"]))

    def test_whitespace_verdict_cannot_bypass_required_quote_spans(self):
        self.payload["claims"][0]["verdict"] = " verified "
        self.payload["claims"][0].pop("evidence_spans")
        self.assertIn("CLAIM_EVIDENCE_SPAN_REQUIRED:C1", self.report()["issues"])

    def test_expired_or_mismatched_source_not_counted_as_grounded(self):
        self.payload["sources"][0]["url"] = "https://example.org/wrong"
        report = self.report()
        self.assertEqual(report["source_grounding"]["grounded_claim_count"], 1)
        self.assertIn("CLAIM_CITED_SOURCE_UNGROUNDED:C1", report["issues"])

    def test_snapshot_metadata_cannot_be_removed_then_rehashed(self):
        source = self.payload["sources"][0]
        for key in ("source_id", "title", "extractor", "input_sha256", "provenance"):
            with self.subTest(key=key):
                source["source_snapshot"] = self.capture(source)
                ref, path = self.rewrite_snapshot(lambda doc: doc.pop(key))
                try:
                    with self.assertRaisesRegex(ValueError, "SOURCE_SNAPSHOT_INVALID"):
                        sources.read_snapshot(self.pack, ref)
                finally:
                    path.unlink()

    def test_duplicate_json_keys_rejected_even_if_last_value_and_hash_match(self):
        ref = self.payload["sources"][0]["source_snapshot"]
        path = self.pack / ref["path"]
        doc = json.loads(path.read_text(encoding="utf-8"))
        raw = '{"text":"Contradictory alternate text",' + json.dumps(doc)[1:]
        path.write_text(raw, encoding="utf-8")
        ref["sha256"] = file_sha256(path)
        with self.assertRaisesRegex(ValueError, "SOURCE_SNAPSHOT_INVALID"):
            sources.read_snapshot(self.pack, ref)

    def test_nonfinite_snapshot_values_rejected(self):
        ref, _ = self.rewrite_snapshot(lambda doc: doc.update(extra=float("nan")))
        with self.assertRaisesRegex(ValueError, "SOURCE_SNAPSHOT_INVALID"):
            sources.read_snapshot(self.pack, ref)

    def test_import_output_size_checked_before_backup_or_write(self):
        self.input.write_text(TEXT + "\x00" * 300, encoding="utf-8")
        with mock.patch.object(sources, "MAX_BYTES", 1000), \
                mock.patch.object(sources, "create_pack_backup") as backup, \
                mock.patch.object(sources, "atomic_write_json") as writer:
            with self.assertRaisesRegex(ValueError, "SOURCE_SNAPSHOT_TOO_LARGE"):
                self.capture(self.payload["sources"][0])
            backup.assert_not_called()
            writer.assert_not_called()

    def test_bad_import_metadata_types_fail_without_io(self):
        for fields in ({"source_id": []}, {"title": None}, {"title": {}}, {"source_id": None}):
            args = {"source_id": "S3", "title": "Public source", "url": "https://example.org/source",
                    "captured_at": NOW.isoformat(), "now": NOW, **fields}
            with self.subTest(fields=fields), mock.patch.object(Path, "open", side_effect=AssertionError("no IO")):
                with self.assertRaisesRegex(ValueError, "SOURCE_IMPORT_"):
                    sources.import_source(self.pack, self.input, **args)

    def test_actual_locate_cli_rejects_missing_identity_without_traceback(self):
        ref, _ = self.rewrite_snapshot(lambda doc: doc.pop("source_id"))
        result = subprocess.run([sys.executable, "-X", "utf8", str(ROOT / "scripts/source_evidence.py"),
                                 "locate", "--pack", str(self.pack), "--snapshot", ref["path"],
                                 "--sha256", ref["sha256"], "--quote", QUOTE],
                                capture_output=True, text=True, encoding="utf-8", timeout=15)
        self.assertEqual(result.returncode, 1)
        self.assertIn("SOURCE_SNAPSHOT_INVALID", result.stdout)
        self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
