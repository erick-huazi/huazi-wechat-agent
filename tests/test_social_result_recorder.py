from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "social_result_recorder",
    ROOT / "scripts" / "social_result_recorder.py",
)
RECORDER = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(RECORDER)


class SocialResultRecorderTests(unittest.TestCase):
    def test_bootstrap_creates_formal_shell_when_results_are_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            pack = Path(temp_dir)
            (pack / "distribution-plan.json").write_text(
                json.dumps({"plan_id": "full-plan", "title": "Title"}),
                encoding="utf-8",
            )
            (pack / "distribution-dry-run-results.json").write_text(
                json.dumps({
                    "schema_version": 2,
                    "title": "Title",
                    "inputs": {"plan_id": "full-plan"},
                    "items": [
                        {"platform_id": "wechat", "status": "validated", "image_count": 5},
                        {"platform_id": "toutiao", "status": "validated", "image_count": 5},
                    ],
                }),
                encoding="utf-8",
            )

            recovered = RECORDER.bootstrap_results_from_dry_run(pack, {})

            self.assertEqual(recovered["schema_version"], 2)
            self.assertFalse(recovered["dry_run"])
            self.assertEqual(recovered["inputs"]["plan_id"], "full-plan")
            self.assertEqual([item["platform_id"] for item in recovered["items"]], ["wechat", "toutiao"])
            self.assertTrue(all(item["status"] == "not_attempted" for item in recovered["items"]))

    def test_bootstrap_recovers_full_plan_after_subset_push(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            pack = Path(temp_dir)
            (pack / "distribution-plan.json").write_text(
                json.dumps({"plan_id": "full-plan", "title": "标题"}),
                encoding="utf-8",
            )
            (pack / "distribution-dry-run-results.json").write_text(
                json.dumps({
                    "schema_version": 2,
                    "title": "标题",
                    "inputs": {"plan_id": "full-plan"},
                    "items": [
                        {"platform_id": "wechat", "status": "validated", "image_count": 5},
                        {"platform_id": "toutiao", "status": "validated", "image_count": 5},
                    ],
                }),
                encoding="utf-8",
            )

            recovered = RECORDER.bootstrap_results_from_dry_run(
                pack,
                {"schema_version": 2, "inputs": {"plan_id": "wechat-only"}, "items": []},
            )

            self.assertEqual(recovered["inputs"]["plan_id"], "full-plan")
            self.assertEqual([item["platform_id"] for item in recovered["items"]], ["wechat", "toutiao"])
            self.assertTrue(all(item["status"] == "not_attempted" for item in recovered["items"]))
            self.assertTrue(recovered["recovered_from_dry_run"])

    def test_recording_real_result_clears_dry_run_flag(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            pack = Path(temp_dir)
            evidence = pack / "evidence" / "browser-state.json"
            evidence.parent.mkdir()
            evidence.write_text("{}", encoding="utf-8")
            (pack / "distribution-results.json").write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "dry_run": True,
                        "items": [{"platform_id": "toutiao", "status": "not_attempted", "issue_code": "AUTH_REQUIRED", "missing_credentials": ["example"]}],
                    }
                ),
                encoding="utf-8",
            )

            argv = [
                "social_result_recorder.py",
                "--pack",
                str(pack),
                "--platform",
                "toutiao",
                "--status",
                "success",
                "--image-verified",
                "true",
                "--cover-verified",
                "true",
                "--draft-verified",
                "true",
                "--image-count",
                "5",
                "--evidence-kind",
                "browser_state",
                "--evidence-summary",
                "草稿已核验",
                "--evidence-reference",
                "evidence/browser-state.json",
            ]
            with mock.patch.object(sys, "argv", argv):
                self.assertEqual(RECORDER.main(), 0)

            result = json.loads((pack / "distribution-results.json").read_text(encoding="utf-8"))
            self.assertFalse(result["dry_run"])
            self.assertTrue(result["result_id"])
            self.assertEqual(result["items"][0]["evidence"]["sha256"], RECORDER.file_sha256(evidence))
            self.assertNotIn("issue_code", result["items"][0])
            self.assertNotIn("missing_credentials", result["items"][0])


if __name__ == "__main__":
    unittest.main()
