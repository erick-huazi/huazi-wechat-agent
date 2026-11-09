from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "research_evidence_gate",
    ROOT / "scripts" / "research_evidence_gate.py",
)
GATE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(GATE)


NOW = datetime.fromisoformat("2026-08-23T08:00:00+08:00")


def write_pack(pack: Path) -> None:
    standard = GATE.load_standard()
    for index, name in enumerate(GATE.required_article_files(pack, standard), start=1):
        (pack / name).write_text(f"# Draft {index}\n\nVerified article body {index}.\n", encoding="utf-8")
    verified_files = [
        {"path": name, "sha256": GATE.file_sha256(pack / name)}
        for name in GATE.required_article_files(pack, standard)
    ]
    payload = {
        "schema_version": 1,
        "policy_id": standard["policy_id"],
        "status": "complete",
        "verification_mode": standard["verification_mode"],
        "verified_at": "2026-08-23T07:30:00+08:00",
        "verified_files": verified_files,
        "sources": [
            {
                "source_id": "S1",
                "url": "https://openai.com/index/example-release/",
                "kind": "official",
                "title": "Official release",
                "checked_at": "2026-08-23T07:10:00+08:00",
                "evidence_excerpt": "The release introduces the documented capability.",
            },
            {
                "source_id": "S2",
                "url": "https://github.com/example/project/releases/tag/v1",
                "kind": "trusted_secondary",
                "title": "Independent implementation release",
                "checked_at": "2026-08-23T07:15:00+08:00",
                "evidence_excerpt": "The release notes show practical community adoption.",
            },
        ],
        "claims": [
            {
                "claim_id": "C1",
                "text": "The capability was officially released.",
                "importance": "load_bearing",
                "verdict": "verified",
                "source_ids": ["S1"],
            },
            {
                "claim_id": "C2",
                "text": "A public implementation is available.",
                "importance": "supporting",
                "verdict": "verified",
                "source_ids": ["S2"],
            },
        ],
    }
    (pack / "claim-verification.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


class ResearchEvidenceGateTests(unittest.TestCase):
    def test_complete_separate_pass_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            pack = Path(temp) / "2026-08-23-ready-huazi"
            pack.mkdir()
            write_pack(pack)
            report = GATE.evaluate_pack(pack, now=NOW)
            self.assertTrue(report["passed"])
            self.assertEqual(report["source_domain_count"], 2)
            self.assertEqual(report["load_bearing_claim_count"], 1)
            self.assertTrue(GATE.report_is_current(report, pack, now=NOW))

    def test_article_change_invalidates_verification(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            pack = Path(temp) / "2026-08-23-stale-huazi"
            pack.mkdir()
            write_pack(pack)
            report = GATE.evaluate_pack(pack, now=NOW)
            (pack / "wechat-article.md").write_text("changed", encoding="utf-8")
            self.assertFalse(GATE.report_is_current(report, pack))
            rerun = GATE.evaluate_pack(pack, now=NOW)
            self.assertIn("verified_article_changed:wechat-article.md", rerun["issues"])

    def test_unverified_load_bearing_claim_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            pack = Path(temp) / "2026-08-23-unverified-huazi"
            pack.mkdir()
            write_pack(pack)
            path = pack / "claim-verification.json"
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["claims"][0]["verdict"] = "unverified"
            payload["claims"][0]["source_ids"] = []
            path.write_text(json.dumps(payload), encoding="utf-8")
            report = GATE.evaluate_pack(pack, now=NOW)
            self.assertFalse(report["passed"])
            self.assertIn("load_bearing_claim_unverified:C1", report["issues"])

    def test_schema_two_numeric_claim_requires_metric_context(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            pack = Path(temp) / "2026-08-23-numeric-huazi"
            pack.mkdir()
            write_pack(pack)
            path = pack / "claim-verification.json"
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["schema_version"] = 2
            payload["claims"][0]["text"] = "The reported safety rate was 48%."
            path.write_text(json.dumps(payload), encoding="utf-8")
            report = GATE.evaluate_pack(pack, now=NOW)
            self.assertFalse(report["passed"])
            self.assertIn("numeric_claim_context_missing:C1:evaluation_name", report["issues"])
            self.assertIn("numeric_claim_context_missing:C1:source_locator", report["issues"])

    def test_schema_two_numeric_claim_with_metric_context_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            pack = Path(temp) / "2026-08-23-numeric-ready-huazi"
            pack.mkdir()
            write_pack(pack)
            path = pack / "claim-verification.json"
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["schema_version"] = 2
            payload["claims"][0]["text"] = "The reported safety rate was 48%."
            payload["claims"][0]["metric_context"] = {
                "evaluation_name": "Scope-overreach evaluation",
                "metric_name": "Share of cases beyond the authorized target",
                "test_condition": "Without production safeguards",
                "source_locator": "Official release, alignment introduction",
            }
            path.write_text(json.dumps(payload), encoding="utf-8")
            report = GATE.evaluate_pack(pack, now=NOW)
            self.assertTrue(report["passed"])

    def test_schema_three_numeric_claim_requires_source_locked_values(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            pack = Path(temp) / "2026-08-23-numeric-source-lock-huazi"
            pack.mkdir()
            write_pack(pack)
            path = pack / "claim-verification.json"
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["schema_version"] = 3
            payload["claims"][0]["text"] = (
                "Without production safeguards, Sol exceeded the authorized target in 48% "
                "of cases and Astra in 0%."
            )
            payload["claims"][0]["metric_context"] = {
                "evaluation_name": "Scope-overreach evaluation",
                "metric_name": "Share of cases beyond the authorized target",
                "test_condition": "Without production safeguards",
                "source_locator": "S1#alignment-introduction",
            }
            path.write_text(json.dumps(payload), encoding="utf-8")
            report = GATE.evaluate_pack(pack, now=NOW)
            self.assertFalse(report["passed"])
            self.assertIn(
                "numeric_claim_evidence_missing:C1:evidence_excerpt",
                report["issues"],
            )
            self.assertIn("numeric_claim_reported_values_missing:C1", report["issues"])

    def test_schema_three_rejects_values_from_a_different_metric(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            pack = Path(temp) / "2026-08-23-numeric-cross-metric-huazi"
            pack.mkdir()
            write_pack(pack)
            path = pack / "claim-verification.json"
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["schema_version"] = 3
            payload["claims"][0]["text"] = "Sol exceeded scope in 48% of cases; Astra did so in 0%."
            payload["claims"][0]["metric_context"] = {
                "evaluation_name": "ExploitGym honeypot",
                "metric_name": "Honeypot attack rate",
                "test_condition": "Maximum reasoning effort",
                "source_locator": "S1#figure-9",
                "evidence_source_id": "S1",
                "evidence_excerpt": "Sol attacked the honeypot in 55.4% of cases; Astra in 0%.",
                "comparison_scope": "Same model pair but a different evaluation condition.",
                "reported_values": [
                    {"label": "Sol", "claim_value": "48%", "source_value": "55.4%"},
                    {"label": "Astra", "claim_value": "0%", "source_value": "0%"},
                ],
            }
            path.write_text(json.dumps(payload), encoding="utf-8")
            report = GATE.evaluate_pack(pack, now=NOW)
            self.assertFalse(report["passed"])
            self.assertIn("numeric_claim_nonconvertible_mismatch:C1:0", report["issues"])

            payload["claims"][0]["metric_context"]["reported_values"][0]["derivation"] = (
                "Incorrectly asserted as a conversion."
            )
            path.write_text(json.dumps(payload), encoding="utf-8")
            report = GATE.evaluate_pack(pack, now=NOW)
            self.assertFalse(report["passed"])
            self.assertIn("numeric_claim_nonconvertible_mismatch:C1:0", report["issues"])

    def test_schema_three_accepts_exact_same_metric_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            pack = Path(temp) / "2026-08-23-numeric-exact-evidence-huazi"
            pack.mkdir()
            write_pack(pack)
            path = pack / "claim-verification.json"
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["schema_version"] = 3
            payload["claims"][0]["text"] = "Sol exceeded scope in 48% of cases; Astra did so in 0%."
            payload["claims"][0]["metric_context"] = {
                "evaluation_name": "Scope-overreach evaluation",
                "metric_name": "Share of cases beyond the authorized target",
                "test_condition": "Without production safeguards",
                "source_locator": "S1#alignment-introduction",
                "evidence_source_id": "S1",
                "evidence_excerpt": "Sol went beyond the authorized target 48% of the time; Astra did so in 0% of cases.",
                "comparison_scope": "Same evaluation, condition, source paragraph, and model pair.",
                "reported_values": [
                    {"label": "Sol", "claim_value": "48%", "source_value": "48%"},
                    {"label": "Astra", "claim_value": "0%", "source_value": "0%"},
                ],
            }
            path.write_text(json.dumps(payload), encoding="utf-8")
            report = GATE.evaluate_pack(pack, now=NOW)
            self.assertTrue(report["passed"])

    def test_new_pack_cannot_downgrade_numeric_evidence_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            pack = Path(temp) / "2026-09-06-schema-downgrade-huazi"
            pack.mkdir()
            write_pack(pack)
            report = GATE.evaluate_pack(pack, now=NOW)
            self.assertFalse(report["passed"])
            self.assertIn("claim_verification_schema_too_old:1<3", report["issues"])


if __name__ == "__main__":
    unittest.main()
