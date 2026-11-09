from __future__ import annotations

import json
import sys
import tempfile
import unittest
import contextlib
import io
from unittest import mock
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image, ImageDraw, PngImagePlugin

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import image_quality_gate as quality
from distribution_io import file_sha256, image_file_probe
from content_workflow_gate import asset_gate
from social_draft_router import workflow_bundles
import social_draft_router as router
import huazi_content_ops as ops
import distribution_dashboard as dashboard
from workflow_policy import GRAPHIC_PLATFORMS as ACTIVE_PLATFORMS


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


class ImageQualityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.pack = Path(self.temp.name) / "2026-10-05-visual-test-huazi"
        (self.pack / "images").mkdir(parents=True)
        self.board = quality.skeleton(self.pack)
        self.board.update(character_reference="Fixture character", palette="teal/red/white")
        article = "# Five independent facts\n\n"
        for index, row in enumerate(self.board["items"]):
            row.update(section_anchor=f"Section {index}", message=f"Information {index}",
                       layout="comparison" if index % 2 else "scene", prompt=f"Explain information {index}.")
            size = (900, 383) if index == 0 else (1080, 720)
            image = Image.new("RGB", size, (25 + index * 40, 220 - index * 30, 80 + index * 25))
            draw = ImageDraw.Draw(image)
            draw.rectangle((40 + index * 30, 40, 150 + index * 100, 300), fill="white")
            image.save(self.pack / row["image"])
            article += f"## Section {index}\n\nA substantive explanation of this distinct fact, its limitations and practical use.\n\n![panel]({row['image']})\n\n"
        for name in quality.load_standard()["long_article_files"].values():
            (self.pack / name).write_text(article, encoding="utf-8")
        write_json(self.pack / "distribution-plan.json", {
            "items": [{"platform_id": pid} for pid in ACTIVE_PLATFORMS],
        })
        write_json(self.pack / quality.INPUT_FILES[0], self.board)
        self.review = {
            "schema_version": 1, "reviewer": "test-fixture-not-production",
            "reviewed_at": datetime.now(timezone.utc).isoformat(),
            "storyboard_sha256": file_sha256(self.pack / quality.INPUT_FILES[0]),
            "article_sha256": file_sha256(self.pack / "wechat-article.md"),
            "items": [{"image": row["image"], "sha256": file_sha256(self.pack / row["image"]),
                       "scores": {k: 5 for k in quality.load_standard()["review_dimensions"]},
                       "fact_checked": True, "no_engagement_bait": True, "notes": "Synthetic fixture only."}
                      for row in self.board["items"]],
        }
        self.save_review()

    def save_review(self):
        write_json(self.pack / quality.INPUT_FILES[1], self.review)

    def codes(self):
        return {item["code"] for item in quality.build_report(self.pack)["issues"]}

    def test_valid_report_and_asset_gate(self):
        report = quality.build_report(self.pack)
        self.assertTrue(report["passed"], report["issues"])
        self.assertEqual(report["visual_score"], 100)
        self.assertFalse(quality.gate(self.pack)["passed"])
        write_json(self.pack / "image-quality-report.json", report)
        self.assertTrue(quality.gate(self.pack)["passed"])
        self.assertTrue(asset_gate(self.pack)["passed"])
        self.assertFalse(asset_gate(self.pack)["video_required"])

    def test_identical_pixels_different_metadata_are_duplicates(self):
        source = self.pack / self.board["items"][1]["image"]
        target = self.pack / self.board["items"][2]["image"]
        meta = PngImagePlugin.PngInfo()
        meta.add_text("different-file", "same pixels")
        with Image.open(source) as image:
            image.save(target, pnginfo=meta)
        self.assertNotEqual(file_sha256(source), file_sha256(target))
        self.assertIn("IMAGE_PIXEL_DUPLICATE", self.codes())

    def test_flat_image_is_rejected(self):
        Image.new("RGB", (1080, 720), "white").save(self.pack / self.board["items"][1]["image"])
        self.assertIn("IMAGE_BLANK_OR_FLAT", self.codes())

    def test_animated_png_cannot_pass_on_the_reviewed_first_frame(self):
        path = self.pack / self.board["items"][1]["image"]
        with Image.open(path) as source:
            first = source.copy()
        second = Image.new("RGB", first.size, "black")
        first.save(path, format="PNG", save_all=True, append_images=[second], duration=100, loop=0)
        self.review["items"][1]["sha256"] = file_sha256(path)
        self.save_review()
        report = quality.build_report(self.pack)
        self.assertIn("IMAGE_ANIMATED_PNG", {row["code"] for row in report["issues"]})
        item = report["images"][1]
        self.assertEqual(item["frame_count"], 2)
        self.assertFalse(item["technical_passed"])
        self.assertNotEqual(item["review_status"], "verified")
        self.assertIsNone(item["visual_score"])
        self.assertFalse(report["passed"])

    def test_static_png_reports_single_frame_without_changing_review_rules(self):
        report = quality.build_report(self.pack)
        self.assertTrue(report["passed"])
        self.assertTrue(all(row["frame_count"] == 1 for row in report["images"]))
        (self.pack / quality.INPUT_FILES[1]).unlink()
        report = quality.build_report(self.pack)
        self.assertTrue(all(row["technical_passed"] for row in report["images"]))
        self.assertFalse(report["passed"])
        self.assertTrue(all(row["review_status"] == "unverified" for row in report["images"]))

    def test_corrupt_png_fails_deep_probe(self):
        path = self.pack / self.board["items"][1]["image"]
        path.write_bytes(path.read_bytes()[:80])
        self.assertFalse(image_file_probe(path)["decoded"])
        self.assertIn("IMAGE_DECODE_OR_SIZE", self.codes())

    def test_size_mismatch(self):
        Image.new("RGB", (100, 100), "red").save(self.pack / self.board["items"][1]["image"])
        self.assertIn("IMAGE_DECODE_OR_SIZE", self.codes())

    def test_missing_review_not_automatically_approved(self):
        (self.pack / quality.INPUT_FILES[1]).unlink()
        self.assertIn("IMAGE_VISUAL_REVIEW_MISSING", self.codes())

    def test_hash_change_invalidates_review(self):
        self.review["items"][0]["sha256"] = "0" * 64
        self.save_review()
        self.assertIn("IMAGE_VISUAL_HASH_MISMATCH", self.codes())

    def test_each_dimension_has_floor(self):
        self.review["items"][0]["scores"]["legibility"] = 3
        self.save_review()
        self.assertIn("IMAGE_VISUAL_QUALITY_LOW", self.codes())

    def test_booleans_are_not_scores(self):
        self.review["items"][0]["scores"]["legibility"] = True
        self.save_review()
        self.assertIn("IMAGE_SCORE_INVALID", self.codes())

    def test_review_does_not_accept_naive_time_or_missing_fact_check(self):
        self.review["reviewed_at"] = "2026-10-04T12:00:00"
        self.review["items"][0]["fact_checked"] = False
        self.save_review()
        self.assertIn("IMAGE_VISUAL_REVIEW_MISSING", self.codes())
        self.assertIn("IMAGE_VISUAL_REVIEW_INCOMPLETE", self.codes())

    def test_five_at_end_is_not_interleaving(self):
        text = "Article text\n\n" + "\n".join(f"![image]({r['image']})" for r in self.board["items"])
        (self.pack / "toutiao-article.md").write_text(text, encoding="utf-8")
        self.assertIn("IMAGE_NOT_INTERLEAVED", self.codes())

    def test_wrong_order_or_remote_images_rejected(self):
        path = self.pack / "zhihu-article.md"
        path.write_text(path.read_text(encoding="utf-8").replace("images/", "https://example.org/"), encoding="utf-8")
        self.assertIn("IMAGE_REFERENCE_ORDER", self.codes())

    def test_stale_article_and_board_review(self):
        (self.pack / "wechat-article.md").write_text("Changed facts", encoding="utf-8")
        self.assertIn("IMAGE_VISUAL_REVIEW_STALE", self.codes())
        self.assertIn("IMAGE_ANCHOR_MISSING", self.codes())

    def test_extra_image_invalidates_report(self):
        write_json(self.pack / "image-quality-report.json", quality.build_report(self.pack))
        Image.new("RGB", (40, 40), "red").save(self.pack / "images/extra.png")
        self.assertFalse(quality.gate(self.pack)["passed"])
        self.assertIn("IMAGE_INVENTORY", self.codes())

    def test_malformed_board_and_review_fail_without_crash(self):
        write_json(self.pack / quality.INPUT_FILES[0], {"items": [1, "bad"]})
        write_json(self.pack / quality.INPUT_FILES[1], [])
        self.assertFalse(quality.build_report(self.pack)["passed"])

    def test_malformed_anchor_is_an_issue_not_a_crash(self):
        self.board["items"][0]["section_anchor"] = 123
        write_json(self.pack / quality.INPUT_FILES[0], self.board)
        self.assertIn("IMAGE_ANCHOR_MISSING", self.codes())

    def test_fenced_images_do_not_count_as_visible(self):
        path = self.pack / "toutiao-article.md"
        path.write_text("```markdown\n" + path.read_text(encoding="utf-8") + "\n```", encoding="utf-8")
        self.assertIn("IMAGE_REFERENCE_ORDER", self.codes())

    def test_near_duplicate_is_visible_as_warning_not_automatic_approval(self):
        source = self.pack / self.board["items"][1]["image"]
        target = self.pack / self.board["items"][2]["image"]
        with Image.open(source) as image:
            image.putpixel((50, 50), (20, 30, 40))
            image.save(target)
        report = quality.build_report(self.pack)
        self.assertIn("IMAGE_NEAR_DUPLICATE", {r["code"] for r in report["warnings"]})
        self.assertFalse(report["passed"])

    def test_new_pack_skeleton_is_pending_and_has_no_video(self):
        pack = Path(self.temp.name) / "2026-10-06-skeleton-huazi"
        args = ops.build_parser().parse_args([
            "pack", "--out", str(pack), "--root", str(Path(self.temp.name) / "ops"),
            "--title", "Graphic workflow", *[arg for key in ops.WEIGHTS for arg in ("--score", f"{key}=3")],
        ])
        with contextlib.redirect_stdout(io.StringIO()):
            ops.cmd_pack(args)
        self.assertTrue((pack / quality.INPUT_FILES[0]).is_file())
        board = quality.read_object(pack / quality.INPUT_FILES[0])
        for item in board["items"]:
            self.assertEqual(item["visual_explanation"], {
                "subject": "", "relationship": "", "reader_takeaway": "", "avoid_implication": ""})
        self.assertFalse((pack / quality.INPUT_FILES[1]).exists())
        self.assertFalse((pack / "video").exists())
        self.assertFalse(quality.build_report(pack)["passed"])

    def test_dry_run_blocks_missing_visual_review(self):
        registry = router.load_registry(router.DEFAULT_REGISTRY)
        args = router.build_parser().parse_args(["push", "--pack", str(self.pack), "--dry-run"])
        with mock.patch.object(router, "configured", return_value=(True, [], "fixture")), \
                mock.patch.object(router, "dispatch_item", side_effect=lambda item, _: {"platform_id": item["platform_id"], "status": "validated"}), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(router.command_push(args, registry), 1)
        dry = quality.read_object(self.pack / "distribution-dry-run-results.json")
        self.assertTrue(all(item["status"] == "blocked" for item in dry["items"]))
        self.assertFalse((self.pack / "distribution-results.json").exists())

    def test_research_scores_are_bounded_and_correctly_summed(self):
        data = quality.read_object(ROOT / "research/github-content-visual-review-2026-10-04.json")
        self.assertEqual(sum(data["weights"].values()), 100)
        for row in data["candidates"]:
            self.assertEqual(set(row["scores"]), set(data["weights"]))
            self.assertEqual(row["total"], sum(row["scores"].values()))
            self.assertTrue(all(0 <= value <= data["weights"][key] for key, value in row["scores"].items()))
            self.assertEqual(len(row["commit"]), 40)

    def test_duplicate_review_rows_rejected(self):
        self.review["items"][1] = self.review["items"][0]
        self.save_review()
        self.assertIn("IMAGE_VISUAL_REVIEW_MISSING", self.codes())

    def test_legacy_pack_not_forced_into_new_gate(self):
        pack = Path(self.temp.name) / "2026-10-04-existing-huazi"
        pack.mkdir()
        self.assertFalse(quality.required(pack))
        self.assertTrue(quality.gate(pack)["passed"])

    def test_new_files_part_of_dry_run_fingerprint(self):
        plan = {"items": []}
        before = workflow_bundles(self.pack, plan)["quality_gates"]
        self.review["items"][0]["notes"] = "Changed review"
        self.save_review()
        after = workflow_bundles(self.pack, plan)["quality_gates"]
        self.assertNotEqual(before["fingerprint"], after["fingerprint"])

    def test_dashboard_shows_new_gates_and_refuses_stale_ready_flag(self):
        write_json(self.pack / "content-workflow.json", {"ready_for_draft_actions": True})
        gates = dashboard.quality_gates(self.pack)
        self.assertIn("image_quality", {row["key"] for row in gates["items"]})
        self.assertIn("content_quality", {row["key"] for row in gates["items"]})
        self.assertFalse(gates["workflow"]["ready_for_draft_actions"])
        self.assertIn("IMAGE_REPORT_MISSING_OR_STALE", {row.get("code") for row in dashboard.pack_data_health(self.pack)["issues"]})

    def bind_numeric_fixture(self, text="Fixture metric 48%", refs=None):
        self.board["items"][1].update(on_image_text=text, claim_ids=refs if refs is not None else ["C1"])
        write_json(self.pack / quality.INPUT_FILES[0], self.board)
        verification = {"claims": [{"claim_id": "C1", "verdict": "verified", "text": "Fixture metric 48%",
            "source_ids": ["S1"], "metric_context": {
                "evaluation_name": "Fixture test", "metric_name": "Fixture metric", "test_condition": "Fixture only",
                "source_locator": "S1#table-1", "evidence_source_id": "S1", "evidence_excerpt": "Fixture metric 48%",
                "comparison_scope": "One fixture population", "reported_values": [
                    {"label": "Fixture metric", "claim_value": "48%", "source_value": "48%"}],
            }}]}
        write_json(self.pack / "claim-verification.json", verification)
        self.review.update(storyboard_sha256=file_sha256(self.pack / quality.INPUT_FILES[0]),
                           claims_sha256=file_sha256(self.pack / "claim-verification.json"))
        self.save_review()

    def test_numeric_labels_require_single_verified_source_locked_claim(self):
        self.bind_numeric_fixture()
        self.assertTrue(quality.build_report(self.pack)["passed"])
        self.bind_numeric_fixture(refs=[])
        self.assertIn("IMAGE_NUMERIC_CLAIM_REQUIRED", self.codes())

    def test_unrelated_metric_cannot_be_added_to_picture(self):
        self.bind_numeric_fixture(text="Fixture metric 48% versus 0%")
        self.assertIn("IMAGE_NUMERIC_VALUE_MISMATCH", self.codes())
        self.bind_numeric_fixture(text="Fixture metric 48% versus 0%", refs=["C1", "C2"])
        self.assertIn("IMAGE_NUMERIC_CLAIM_REQUIRED", self.codes())

    def test_fullwidth_percentage_cannot_bypass_claim_binding(self):
        self.bind_numeric_fixture(text="Fixture metric \uff14\uff18\uff05", refs=[])
        self.assertIn("IMAGE_NUMERIC_CLAIM_REQUIRED", self.codes())

    def test_duplicate_claim_ids_are_ambiguous(self):
        self.bind_numeric_fixture()
        path = self.pack / "claim-verification.json"
        value = quality.read_object(path)
        value["claims"].append(value["claims"][0])
        write_json(path, value)
        self.assertIn("IMAGE_NUMERIC_CLAIM_REQUIRED", self.codes())

    def test_changed_numeric_evidence_requires_new_visual_review(self):
        self.bind_numeric_fixture()
        path = self.pack / "claim-verification.json"
        value = quality.read_object(path)
        value["claims"][0]["metric_context"]["test_condition"] = "Other fixture condition"
        write_json(path, value)
        self.assertIn("IMAGE_VISUAL_REVIEW_STALE", self.codes())
        self.assertIsNone(quality.build_report(self.pack)["visual_score"])

    def test_numeric_claim_without_source_contract_fails(self):
        self.bind_numeric_fixture()
        path = self.pack / "claim-verification.json"
        value = quality.read_object(path)
        value["claims"][0]["metric_context"].pop("reported_values")
        write_json(path, value)
        self.assertIn("IMAGE_NUMERIC_EVIDENCE_INVALID", self.codes())

    def test_rounded_score_below_threshold_is_rejected(self):
        self.review["items"][0]["scores"] = {k: 4.2499 for k in quality.load_standard()["review_dimensions"]}
        self.save_review()
        self.assertIn("IMAGE_VISUAL_QUALITY_LOW", self.codes())

    def test_report_tampering_does_not_keep_valid_self_hash(self):
        report = quality.build_report(self.pack)
        report["visual_score"] = 99
        write_json(self.pack / "image-quality-report.json", report)
        self.assertFalse(quality.gate(self.pack)["passed"])
        self.assertIsNone(quality.gate(self.pack)["visual_score"])

    def test_stale_image_hides_accepted_score_per_image(self):
        self.review["items"][0]["sha256"] = "0" * 64
        self.save_review()
        row = quality.build_report(self.pack)["images"][0]
        self.assertTrue(row["technical_passed"])
        self.assertEqual(row["review_status"], "stale")
        self.assertIsNone(row["visual_score"])

    def test_raw_html_cannot_hide_markdown_images(self):
        path = self.pack / "toutiao-article.md"
        path.write_text(path.read_text(encoding="utf-8").replace("![panel]", '<span hidden>![panel]'), encoding="utf-8")
        self.assertIn("IMAGE_LAYOUT_RAW_HTML", self.codes())

    def test_invalid_utf8_fails_closed(self):
        (self.pack / "wechat-article.md").write_bytes(b"\xff\xfe")
        self.assertIn("IMAGE_INPUT_UNREADABLE", self.codes())

    def test_empty_plan_cannot_skip_article_checks(self):
        write_json(self.pack / "distribution-plan.json", {"items": []})
        self.assertIn("IMAGE_PLAN_SCOPE_INVALID", self.codes())

    def test_missing_plan_is_not_assumed_complete(self):
        (self.pack / "distribution-plan.json").unlink()
        self.assertIn("IMAGE_PLAN_SCOPE_INVALID", self.codes())

    def test_subset_plan_cannot_hide_unchecked_long_article(self):
        write_json(self.pack / "distribution-plan.json", {"items": [{"platform_id": "xiaohongshu"}]})
        (self.pack / "toutiao-article.md").write_text("No images", encoding="utf-8")
        report = quality.build_report(self.pack)
        self.assertFalse(report["passed"])
        self.assertIn("IMAGE_PLAN_SCOPE_INVALID", {row["code"] for row in report["issues"]})
        self.assertIn("IMAGE_REFERENCE_ORDER", {row["code"] for row in report["issues"]})
        self.assertIn("toutiao-article.md", [row["path"] for row in report["inputs"]])

    def test_unknown_disabled_duplicate_and_malformed_platforms_block(self):
        for replacement in ("unknown", "douyin-video", "wechat", None, [], {}):
            with self.subTest(replacement=replacement):
                items = [{"platform_id": pid} for pid in ACTIVE_PLATFORMS]
                items[-1]["platform_id"] = replacement
                write_json(self.pack / "distribution-plan.json", {"items": items})
                self.assertIn("IMAGE_PLAN_SCOPE_INVALID", self.codes())

    def test_full_plan_order_does_not_matter(self):
        write_json(self.pack / "distribution-plan.json", {
            "items": [{"platform_id": pid} for pid in reversed(ACTIVE_PLATFORMS)],
        })
        self.assertTrue(quality.build_report(self.pack)["passed"])

    def test_legacy_opt_in_keeps_original_subset_scope(self):
        old = self.pack.parent / "2026-10-03-legacy-huazi"
        old.mkdir()
        write_json(old / "distribution-plan.json", {"items": [{"platform_id": "wechat"}]})
        report = quality.build_report(old)
        self.assertNotIn("IMAGE_PLAN_SCOPE_INVALID", {row["code"] for row in report["issues"]})
        self.assertNotIn("toutiao-article.md", [row["path"] for row in report["inputs"]])

    def test_boolean_schema_versions_are_not_valid_versions(self):
        self.board["schema_version"] = True
        self.review["schema_version"] = True
        write_json(self.pack / quality.INPUT_FILES[0], self.board)
        self.review["storyboard_sha256"] = file_sha256(self.pack / quality.INPUT_FILES[0])
        self.save_review()
        self.assertIn("IMAGE_STORYBOARD_INVALID", self.codes())
        self.assertIn("IMAGE_VISUAL_REVIEW_MISSING", self.codes())

    def test_anchor_in_different_section_is_not_enough(self):
        self.board["items"][1]["section_anchor"] = "Section 4"
        write_json(self.pack / quality.INPUT_FILES[0], self.board)
        self.review["storyboard_sha256"] = file_sha256(self.pack / quality.INPUT_FILES[0])
        self.save_review()
        report = quality.build_report(self.pack)
        self.assertIn("IMAGE_ANCHOR_SECTION_MISMATCH", {row["code"] for row in report["issues"]})
        self.assertEqual(report["images"][1]["review_status"], "needs_revision")

    def test_anchor_only_in_code_or_image_alt_is_not_body_context(self):
        for hidden in ("\n```text\nOnly hidden anchor\n```\n", "\n![Only hidden anchor](not-the-panel.png)\n"):
            with self.subTest(hidden=hidden):
                text = "# Heading\n" + hidden + "\n![panel](images/panel.png)\n"
                self.assertNotIn("Only hidden anchor", quality.image_section_contexts(text)["images/panel.png"][0])

    def test_anchor_accepts_visible_formatted_sentence_after_image(self):
        text = "# Main\n\n## News\n\n![panel](./images/panel.png)\n\nA **verified change** matters.\n"
        contexts = quality.image_section_contexts(text)["images/panel.png"]
        self.assertEqual(len(contexts), 1)
        self.assertIn("A verified change matters.", contexts[0])
        self.assertIn("News", contexts[0])

    def test_anchor_does_not_leak_from_peer_section_but_keeps_parent_heading(self):
        text = "# Main\n\n## Mechanism\n\n### Detail\n\n![panel](images/panel.png)\n\n## Other\n\nWrong anchor here.\n"
        context = quality.image_section_contexts(text)["images/panel.png"][0]
        self.assertIn("Mechanism", context)
        self.assertNotIn("Wrong anchor", context)

    def test_concurrent_input_change_fails_closed(self):
        real_snapshot = quality.input_snapshot
        count = 0
        def snapshot(*args):
            nonlocal count
            count += 1
            result = real_snapshot(*args)
            if count > 1:
                result[0]["sha256"] = "changed-fixture"
            return result
        with mock.patch.object(quality, "input_snapshot", side_effect=snapshot):
            self.assertIn("IMAGE_INPUT_CHANGED_DURING_CHECK", self.codes())


if __name__ == "__main__":
    unittest.main()
