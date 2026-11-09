import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import content_revision_review as review
import pack_content_writer as writer
from distribution_io import file_sha256, pack_lock, verify_pack_backup


class RevisionReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.pack = self.root / "2026-10-09-review-test-huazi"
        self.source = self.root / "staged"
        self.pack.mkdir()
        self.source.mkdir()

    def stage(self, name, before, after):
        if before is not None:
            path = self.pack / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(before, encoding="utf-8", newline="\n")
        path = self.source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(after, encoding="utf-8", newline="\n")

    def codes(self, before, after):
        return {f["code"] for f in review.review_markdown(before, after)}

    def invoke(self, *args):
        output = io.StringIO()
        argv = ["writer", "--pack", str(self.pack), "--source", str(self.source), *args]
        with patch.object(sys, "argv", argv), contextlib.redirect_stdout(output):
            writer.main()
        return output.getvalue()

    def test_unchanged_numbers_changed_metric_scope_is_not_silent(self):
        before = "模型甲在评测 A 的越权率为48%，模型乙为0%；仅该条件。"
        after = "模型甲在评测 B 的成功率为48%，模型乙为0%；覆盖全部场景。"
        codes = self.codes(before, after)
        self.assertIn("REV_NUMERIC_CONTEXT_CHANGED", codes)
        self.assertIn("REV_PROSE_CHANGED", codes)
        self.assertNotIn("REV_NUMERIC_VALUES_CHANGED", codes)

    def test_editorial_experiment_changes_are_reviewed_and_context_bound(self):
        self.stage("editorial-experiment.json", '{"tactic":"reader_problem"}', '{"tactic":"decision_cost"}')
        findings = review.review_file("editorial-experiment.json", '{"tactic":"reader_problem"}', '{"tactic":"decision_cost"}')
        self.assertEqual(findings[0]["code"], "REV_EDITORIAL_EXPERIMENT_CHANGED")
        self.assertIn("editorial-experiment.json", review.CONTEXT_FILES)
        prepared, _ = review.prepare_revision(self.pack, self.source)
        (self.pack / "editorial-experiment.json").write_text('{"tactic":"scenario_question"}', encoding="utf-8")
        self.assertFalse(review.revision_is_current(self.pack, self.source, prepared))

    def test_swapping_model_assignment_with_same_values_is_flagged(self):
        before = "模型甲48%，模型乙0%。"
        after = "模型甲0%，模型乙48%。"
        self.assertIn("REV_NUMERIC_CONTEXT_CHANGED", self.codes(before, after))

    def test_dropping_denominator_or_condition_needs_review(self):
        self.assertIn("REV_NUMERIC_VALUES_CHANGED", self.codes("样本 n=25，48%，仅条件A。", "48%。"))
        self.assertIn("REV_NUMERIC_CONTEXT_CHANGED", self.codes("48%，仅条件A。", "48%，全部条件。"))

    def test_unicode_digits_units_and_versions(self):
        codes = self.codes("模型v1.2，９８％，耗时20ms。", "模型v1.3，９９％，耗时20秒。")
        self.assertIn("REV_NUMERIC_VALUES_CHANGED", codes)

    def test_changed_quote_and_link_target_with_same_label(self):
        before = '> 原句“仅限早期访问”。\n\n[公告](https://example.com/first)'
        after = '> 原句“全面开放”。\n\n[公告](https://example.com/second)'
        self.assertTrue({"REV_QUOTATION_CHANGED", "REV_SOURCE_LINK_CHANGED"} <= self.codes(before, after))

    def test_ai_disclosure_removed_or_changed(self):
        self.assertIn("REV_AI_DISCLOSURE_CHANGED", self.codes("正文。\n\n本文由 AI 辅助整理。", "正文。"))

    def test_image_order_and_alt_change(self):
        before = "![概念](images/01.png)\n\n![边界](images/02.png)"
        after = "![实测](images/02.png)\n\n![概念](images/01.png)"
        self.assertIn("REV_IMAGE_REFERENCE_CHANGED", self.codes(before, after))

    def test_pure_markdown_emphasis_does_not_change_number_context(self):
        codes = self.codes("模型A 48% (n=25，仅测试X)。", "模型A **48%** (n=25，仅测试X)。")
        self.assertEqual(codes, {"REV_MARKUP_OR_UNPARSED_CHANGED"})

    def test_non_numeric_qualifier_change_is_visible_with_lines(self):
        findings = review.review_markdown("# 标题\n\n可能有效，但未实测。", "# 标题\n\n确定有效，已实测。")
        diff = next(f for f in findings if f["code"] == "REV_PROSE_CHANGED")
        self.assertEqual(diff["blocks"]["items"][0]["before"]["items"][0]["line"], 3)

    def test_code_and_html_changes_are_not_false_equivalence(self):
        for before, after in [("```\n48%\n```", "```\n0%\n```"), ("<div>48%</div>", "<div>0%</div>")]:
            self.assertIn("REV_MARKUP_OR_UNPARSED_CHANGED", self.codes(before, after))

    def test_table_numeric_cells_are_read(self):
        before = "| 模型 | 数值 |\n|---|---|\n| A | 48% |"
        after = before.replace("48%", "0%")
        self.assertIn("REV_NUMERIC_VALUES_CHANGED", self.codes(before, after))

    def test_no_op_is_not_a_fact_or_visual_pass(self):
        self.stage("wechat-article.md", "# Same\n\n正文。", "# Same\n\n正文。")
        first, _ = review.prepare_revision(self.pack, self.source)
        second, _ = review.prepare_revision(self.pack, self.source)
        self.assertEqual(first, second)
        self.assertFalse(first["requires_human_review"])
        self.assertFalse(first["fact_verification_performed"])
        self.assertFalse(first["visual_review_performed"])
        self.assertNotIn("passed", first)
        self.assertFalse((self.pack / ".backups").exists())

    def test_storyboard_and_experiment_differences_locate_fields(self):
        for name, code in [("image-storyboard.json", "REV_STORYBOARD_CHANGED"), ("growth-experiment.json", "REV_EXPERIMENT_CHANGED")]:
            before = json.dumps({"items": [{"on_image_text": "48%"}], "variable": "opening_hook"})
            after = json.dumps({"items": [{"on_image_text": "0%"}], "variable": "emotion_tension"})
            result = review.review_file(name, before, after)[0]
            self.assertEqual(result["code"], code)
            self.assertTrue(any('"on_image_text"' in row["path"] for row in result["changes"]["items"]))

    def test_json_reformat_does_not_forge_semantic_changes(self):
        self.assertEqual(review.review_file("a.json", '{"x":1}', '{ "x": 1 }'), [])
        changes = review.json_changes({"x": 1}, {"x": True})
        self.assertTrue(changes)

    def test_duplicate_or_nonfinite_json_rejected(self):
        for text in ('{"x":1,"x":2}', '{"x":NaN}', '{"x":Infinity}'):
            self.assertRaises(ValueError, review.strict_json, text)

    def test_capped_findings_are_explicit(self):
        before = "\n\n".join(f"旧条件{i}，48%。" for i in range(20))
        after = "\n\n".join(f"新条件{i}，48%。" for i in range(20))
        contexts = next(x for x in review.review_markdown(before, after) if x["code"] == "REV_NUMERIC_CONTEXT_CHANGED")
        self.assertTrue(contexts["before"]["truncated"])
        self.assertEqual(contexts["before"]["total"], 20)

    def test_review_only_no_backup_no_write(self):
        self.stage("wechat-article.md", "before", "after")
        before = file_sha256(self.pack / "wechat-article.md")
        result = json.loads(self.invoke("--review-only"))
        self.assertTrue(result["requires_human_review"])
        self.assertEqual(before, file_sha256(self.pack / "wechat-article.md"))
        self.assertFalse((self.pack / ".backups").exists())
        self.assertFalse((self.pack / ".distribution-action.lock").exists())

    def test_review_id_then_write_readback_and_backup(self):
        self.stage("wechat-article.md", "before", "after")
        result = json.loads(self.invoke("--review-only"))
        self.invoke("--expect-review-id", result["review_id"])
        self.assertEqual((self.pack / "wechat-article.md").read_text(), "after")
        backups = sorted((self.pack / ".backups").iterdir())
        self.assertTrue(verify_pack_backup(backups[-1])["valid"])
        self.assertEqual((backups[-1] / "wechat-article.md").read_text(), "before")

    def test_staged_or_bound_pack_change_invalidates_id_before_backup(self):
        for changed in ["staging", "article", "image", "approval", "extra-staged-file"]:
            with self.subTest(changed=changed):
                self.stage("wechat-article.md", "before", "after")
                result, _ = review.prepare_revision(self.pack, self.source)
                if changed == "staging":
                    (self.source / "wechat-article.md").write_text("later")
                elif changed == "article":
                    (self.pack / "zhihu-article.md").write_text("later")
                elif changed == "image":
                    (self.pack / "images").mkdir(exist_ok=True)
                    (self.pack / "images/2026-10-09-01.png").write_bytes(b"not an actual PNG, hash fixture only")
                elif changed == "approval":
                    (self.pack / "content-approval.json").write_text('{"decision":"hold"}')
                else:
                    (self.source / "new.md").write_text("new")
                with self.assertRaisesRegex(SystemExit, "REV_REVIEW_ID_MISMATCH"):
                    self.invoke("--expect-review-id", result["review_id"])
                self.assertEqual((self.pack / "wechat-article.md").read_text(), "before")
                self.assertFalse((self.pack / ".backups").exists())

    def test_entire_batch_preflight_before_any_overwrite(self):
        self.stage("a.md", "before", "after")
        (self.source / "z.json").write_text("bad json")
        self.assertRaises(ValueError, self.invoke)
        self.assertEqual((self.pack / "a.md").read_text(), "before")
        self.assertFalse((self.pack / ".backups").exists())

    def test_overlapping_staging_and_hidden_files_refused(self):
        with self.assertRaisesRegex(ValueError, "REV_DISJOINT"):
            review.prepare_revision(self.pack, self.pack)
        (self.source / ".hidden.json").write_text("{}")
        self.assertRaises(ValueError, review.prepare_revision, self.pack, self.source)

    def test_unreadable_utf8_unsupported_and_large_input(self):
        for name, value in [("bad.md", b"\xff"), ("bad.png", b"x"), ("bad.txt", b"x" * (review.MAX_FILE_BYTES + 1))]:
            path = self.source / name
            path.write_bytes(value)
            with self.assertRaises((ValueError, UnicodeError)):
                review.prepare_revision(self.pack, self.source)
            path.unlink()

    def test_destination_directory_and_symlink_guard(self):
        self.stage("a.md", None, "after")
        (self.pack / "a.md").mkdir()
        self.assertRaises(ValueError, self.invoke)
        (self.pack / "a.md").rmdir()
        original = Path.is_symlink
        with patch.object(Path, "is_symlink", lambda p: p.name == "a.md" or original(p)):
            self.assertRaises(ValueError, self.invoke)

    def test_source_link_guard(self):
        self.stage("a.md", None, "after")
        original = Path.is_symlink
        with patch.object(Path, "is_symlink", lambda p: p == self.source / "a.md" or original(p)):
            self.assertRaises(ValueError, self.invoke)

    def test_destination_parent_traversal_rejected(self):
        self.assertRaises(ValueError, review.checked_path, self.pack, "../outside.md")

    def test_conflicting_lock_never_writes(self):
        self.stage("a.md", "before", "after")
        with pack_lock(self.pack, "fixture"):
            self.assertRaises(Exception, self.invoke)
        self.assertEqual((self.pack / "a.md").read_text(), "before")

    def test_change_during_review_stops_before_backup(self):
        self.stage("a.md", "before", "after")
        with patch.object(writer, "revision_is_current", return_value=False):
            self.assertRaises(SystemExit, self.invoke)
        self.assertFalse((self.pack / ".backups").exists())

    def test_write_uses_reviewed_bytes_not_a_later_staged_read(self):
        self.stage("a.md", "before", "after")
        original = writer.atomic_write_text
        def write(destination, text):
            (self.source / "a.md").write_text("unreviewed late content")
            original(destination, text)
        with patch.object(writer, "atomic_write_text", side_effect=write):
            self.invoke()
        self.assertEqual((self.pack / "a.md").read_text(), "after")

    def test_json_depth_is_bounded(self):
        before, after = 0, 1
        for _ in range(30):
            before, after = {"x": before}, {"x": after}
        self.assertRaises(ValueError, review.json_changes, before, after)

    def test_windows_newline_conversion_is_bound_to_actual_write(self):
        self.stage("a.md", "same\ntext", "same\ntext")
        (self.pack / "a.md").write_bytes(b"same\r\ntext")
        (self.source / "a.md").write_bytes(b"same\r\ntext")
        result, _ = review.prepare_revision(self.pack, self.source)
        self.assertTrue(result["requires_human_review"])
        self.invoke("--expect-review-id", result["review_id"])
        self.assertEqual(file_sha256(self.pack / "a.md"), result["files"][0]["write_sha256"])

    def test_github_scores_and_license_scope_are_explicit(self):
        root = Path(__file__).resolve().parents[1]
        data = json.loads((root / "research/github-revision-safety-review-2026-10-09.json").read_text(encoding="utf-8"))
        self.assertEqual(sum(data["rubric"].values()), 100)
        for item in data["candidates"]:
            self.assertEqual(len(item["commit"]), 40)
            self.assertIn(item["commit"], item["source"]["url"])
            self.assertEqual(sum(item["score"].values()), item["total"])
            self.assertEqual(set(item["score"]), set(data["rubric"]))
            for key, value in item["score"].items():
                self.assertTrue(0 <= value <= data["rubric"][key])
            if item["decision"] != "method_integrated":
                self.assertEqual(item["implemented"], [])
        skill = next(item for item in data["candidates"] if item["repository"] == "anthropics/skills")
        self.assertIn("skills/skill-creator/LICENSE.txt", skill["license_scope_url"])
        self.assertEqual(data["local_implementation"]["new_dependencies"], [])


if __name__ == "__main__":
    unittest.main()
