import copy
import io
import json
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import content_visual_brief as brief
from distribution_io import pack_lock, verify_pack_backup


class ContentVisualBriefTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.pack = self.root / "2026-10-07-example-huazi"
        self.pack.mkdir()
        Image.new("RGB", (32, 32), "blue").save(self.pack / "reference.png")
        self.article = "# News\n\nA verified change matters to a specific reader.\n\n"
        self.article += "\n\n".join(f"## Section {i}\n\nExplanation {i}." for i in range(5))
        (self.pack / "wechat-article.md").write_text(self.article, encoding="utf-8")
        self.board = {
            "schema_version": 1, "character_reference": "reference.png", "palette": "white blue teal",
            "items": [{"image": f"images/2026-10-07-{i + 1:02d}.png", "role": role,
                       "section_anchor": f"Section {i}", "message": f"Distinct message {i}",
                       "layout": f"Layout {i}", "on_image_text": "", "claim_ids": [],
                       "prompt": f"Draw specific scene {i}; preserve its limits."}
                      for i, role in enumerate(brief.ROLES)]}
        self.save_board()
        self.policy = self.root / "policy.json"
        self.policy.write_bytes(brief.POLICY.read_bytes())

    def save_board(self):
        (self.pack / "image-storyboard.json").write_text(json.dumps(self.board), encoding="utf-8")

    def compile(self):
        return brief.compile_brief(self.pack, self.policy, self.root)

    def codes(self, result):
        return {item["code"] for item in result["issues"]}

    def test_five_prompts_and_graphic_platforms_only(self):
        result = self.compile()
        self.assertEqual(result["status"], "prepared")
        self.assertEqual([p["platform"] for p in result["platform_cards"]],
                         ["wechat", "toutiao", "zhihu", "xiaohongshu", "weibo"])
        self.assertEqual([(p["width"], p["height"]) for p in result["image_prompts"]],
                         [(900, 383)] + [(1080, 720)] * 4)
        self.assertEqual([p["role"] for p in result["image_prompts"]], list(brief.ROLES))
        self.assertFalse(result["generation_performed"])
        self.assertFalse(result["visual_review_performed"])
        self.assertNotIn("passed", result)

    def test_labels_numbers_and_source_prompt_preserved(self):
        exact = "48% (n=25, evaluation A only); not a real benchmark in this fixture"
        self.board["items"][2]["on_image_text"] = exact
        self.board["items"][2]["claim_ids"] = ["test-only"]
        self.save_board()
        result = self.compile()
        prompt = result["image_prompts"][2]
        self.assertEqual(prompt["on_image_text"], exact)
        self.assertIn(exact, prompt["final_prompt"])
        self.assertIn(self.board["items"][2]["prompt"], prompt["final_prompt"])
        self.assertIn("BRIEF_LABEL_DENSITY_REVIEW", self.codes(result))
        self.assertEqual(prompt["claim_ids"], ["test-only"])

    def test_blank_labels_explicitly_forbid_text(self):
        self.assertIn("Text-free illustration", self.compile()["image_prompts"][0]["final_prompt"])

    def test_copy_design_enters_prompt_and_visual_queue_without_rewriting(self):
        item = self.board["items"][1]
        item["copy_design"] = {"schema_version": 1, "blocks": [
            {"kind": "headline", "text": "Read with conditions", "font_px": 72},
            {"kind": "body", "text": "Only an example, not a test.", "font_px": 44}]}
        item["on_image_text"] = "Read with conditions\nOnly an example, not a test."
        self.save_board()
        result = self.compile()
        prompt = result["image_prompts"][1]
        self.assertIn("Copy-led editorial graphic", prompt["final_prompt"])
        self.assertEqual(prompt["copy_review"], result["visual_review_queue"][1]["copy_review"])
        self.assertEqual(prompt["on_image_text"], item["on_image_text"])
        self.assertIn("image_copy_reviewer_sha256", result["inputs"])
        self.assertNotIn("BRIEF_LABEL_DENSITY_REVIEW", self.codes(result))
        item["copy_design"]["blocks"][1]["font_px"] = 24
        self.save_board()
        self.assertFalse(brief.brief_is_current(self.pack, result, self.policy, self.root))
        self.assertIn("COPY_PHONE_SIZE_REVIEW", self.codes(self.compile()))

    def test_mismatched_copy_design_needs_input_without_erasing_original_labels(self):
        item = self.board["items"][0]
        item["on_image_text"] = "Only in this test"
        item["copy_design"] = {"schema_version": 1, "blocks": [
            {"kind": "headline", "text": "In every case", "font_px": 72}]}
        self.save_board()
        result = self.compile()
        self.assertEqual(result["status"], "needs_input")
        self.assertIn("COPY_LABEL_MISMATCH", self.codes(result))
        self.assertEqual(result["image_prompts"][0]["on_image_text"], "Only in this test")
        self.assertNotIn("In every case", result["image_prompts"][0]["final_prompt"])

    def test_copy_reviewer_change_invalidates_existing_brief(self):
        result = self.compile()
        original = brief.file_sha256
        with patch.object(brief, "file_sha256", side_effect=lambda p: "changed" if p.name == "image_copy_review.py" else original(p)):
            self.assertFalse(brief.brief_is_current(self.pack, result, self.policy, self.root))

    def test_evidence_mode_is_optional_and_not_a_visual_verdict(self):
        result = self.compile()
        self.assertEqual(result["status"], "prepared")
        self.assertEqual(result["image_prompts"][0]["evidence_review"]["status"], "not_provided")
        self.assertEqual(result["reader_delivery_review"]["status"], "not_provided")
        self.assertIn("BRIEF_EVIDENCE_MODE_OPTIONAL", self.codes(result))

    def test_factual_picture_requires_claim_even_without_numbers(self):
        self.board["items"][0]["evidence_mode"] = "source_fact"
        self.save_board()
        self.assertIn("BRIEF_FACT_CLAIM_REQUIRED", self.codes(self.compile()))

    def test_unsupported_modes_and_malformed_claim_ids_are_advisory(self):
        for mode in (True, {}, [], "factual_proven"):
            result = brief.visual_evidence_review({"evidence_mode": mode}, {})
            self.assertEqual(result["status"], "needs_review")
        for refs in (None, True, {}, [True], [{}], [""], ["c1", "c1"]):
            result = brief.visual_evidence_review({"evidence_mode": "source_fact", "claim_ids": refs}, {})
            self.assertIn("BRIEF_CLAIM_LINK_INVALID", {f["code"] for f in result["findings"]})

    def test_claim_must_resolve_uniquely_and_be_declared_verified(self):
        item = {"evidence_mode": "source_fact", "claim_ids": ["c1"]}
        for claims in ([], [None], [{"claim_id": "other"}], [{"claim_id": "c1"}] * 2):
            result = brief.visual_evidence_review(item, {"claims": claims})
            self.assertIn("BRIEF_CLAIM_LINK_UNRESOLVED", {f["code"] for f in result["findings"]})
        for verdict in ("unverified", "contradicted", "corrected", None):
            result = brief.visual_evidence_review(item, {"claims": [{"claim_id": "c1", "verdict": verdict}]})
            self.assertIn("BRIEF_CLAIM_NOT_VERIFIED", {f["code"] for f in result["findings"]})

    def test_resolved_claim_is_not_grounding_or_fact_verification(self):
        item = {"evidence_mode": "source_fact", "claim_ids": ["c1"]}
        result = brief.visual_evidence_review(item, {"claims": [{"claim_id": "c1", "verdict": "verified", "text": "An unexamined assertion."}]})
        self.assertEqual(result["status"], "specified_for_source_and_visual_review")
        self.assertEqual(result["claim_links"][0]["status"], "resolved_only")
        self.assertFalse(result["fact_verification_performed"])
        self.assertEqual(result["actual_image_review"], "not_performed")
        self.assertNotIn("passed", result)

    def test_nonliteral_modes_need_author_boundary_and_enter_prompt_unchanged(self):
        for mode in ("analogy", "hypothetical", "instruction"):
            item = self.board["items"][2]
            item["evidence_mode"] = mode
            item.pop("visual_explanation", None)
            self.save_board()
            self.assertIn("BRIEF_NONLITERAL_BOUNDARY_REQUIRED", self.codes(self.compile()))
            item["visual_explanation"] = {"subject": "A document", "relationship": "Human checks page order",
                                          "reader_takeaway": "Review before import", "avoid_implication": "Not a measured accuracy gain"}
            self.save_board()
            result = self.compile()
            prompt = result["image_prompts"][2]
            self.assertEqual(prompt["evidence_review"]["status"], "specified_for_source_and_visual_review")
            self.assertIn("Not a measured accuracy gain", prompt["final_prompt"])
            self.assertIn(item["prompt"], prompt["final_prompt"])
            self.assertEqual(prompt["on_image_text"], "")
            self.assertEqual(result["visual_review_queue"][2]["evidence_review"], prompt["evidence_review"])

    def test_delivery_and_claim_changes_invalidate_brief(self):
        plan = {"schema_version": 1, "delivery_anchors": {role: "A verified change matters to a specific reader." for role in ("answer", "action", "boundary")}}
        path = self.pack / "editorial-experiment.json"
        path.write_text(json.dumps(plan), encoding="utf-8")
        result = self.compile()
        self.assertEqual(len(result["reader_delivery_review"]["matches"]), 3)
        self.assertIn("DELIVERY_ROLE_OVERLAP_REVIEW", self.codes(result))
        plan["delivery_anchors"]["answer"] = "A nonexistent substantive answer."
        path.write_text(json.dumps(plan), encoding="utf-8")
        self.assertFalse(brief.brief_is_current(self.pack, result, self.policy, self.root))
        self.assertIn("DELIVERY_ANCHOR_NOT_FOUND", self.codes(self.compile()))
        current = self.compile()
        (self.pack / "claim-verification.json").write_text(json.dumps({"claims": []}), encoding="utf-8")
        self.assertFalse(brief.brief_is_current(self.pack, current, self.policy, self.root))

    def test_missing_items_and_fields_need_input(self):
        self.board["items"] = [{"role": "cover"}, None]
        self.save_board()
        result = self.compile()
        self.assertEqual(result["status"], "needs_input")
        self.assertTrue({"BRIEF_FIVE_ROLES_REQUIRED", "BRIEF_FIELD_REQUIRED", "BRIEF_ITEM_INVALID"} <= self.codes(result))

    def test_wrong_role_name_and_boolean_schema_rejected(self):
        self.board["schema_version"] = True
        self.board["items"][0]["role"] = "scenario"
        self.board["items"][1]["image"] = "images/01-context.png"
        self.save_board()
        self.assertTrue({"BRIEF_STORYBOARD_SCHEMA", "BRIEF_ROLE_OR_NAME"} <= self.codes(self.compile()))

    def test_anchor_in_code_or_alt_is_not_visible(self):
        (self.pack / "wechat-article.md").write_text("# News\n\n![Section 0](image.png)\n\n```\nSection 1\n```", encoding="utf-8")
        result = self.compile()
        issues = [i for i in result["issues"] if i["code"] == "BRIEF_ANCHOR_NOT_VISIBLE"]
        self.assertEqual(len(issues), 5)

    def test_structures_are_suggestions_not_forced_rewrites(self):
        self.board["items"][2]["layout"] = "My precise nonstandard layout"
        self.save_board()
        self.assertIn("My precise nonstandard layout", self.compile()["image_prompts"][2]["final_prompt"])

    def test_repeated_messages_and_layouts_are_advisory(self):
        for item in self.board["items"]:
            item.update(message="Repeated", layout="Repeated")
        self.save_board()
        result = self.compile()
        self.assertEqual(result["status"], "prepared")
        self.assertTrue({"BRIEF_MESSAGE_REPEATED", "BRIEF_LAYOUT_MONOTONY"} <= self.codes(result))

    def test_reference_outside_project_not_read(self):
        self.board["character_reference"] = str(self.root.parent / "external.png")
        self.save_board()
        with patch.object(brief, "image_file_probe", side_effect=AssertionError("Must not read")):
            self.assertIn("BRIEF_REFERENCE_OUTSIDE_PROJECT", self.codes(self.compile()))

    def test_reference_missing_or_wrong_type(self):
        for reference, code in [("missing.png", "BRIEF_REFERENCE_MISSING_OR_LARGE"),
                                ("secrets.json", "BRIEF_REFERENCE_FORMAT"),
                                ([], "BRIEF_REFERENCE_REQUIRED")]:
            with self.subTest(reference=reference):
                self.board["character_reference"] = reference
                self.save_board()
                self.assertIn(code, self.codes(self.compile()))

    def test_deterministic_and_sources_untouched(self):
        original = {p.name: p.read_bytes() for p in self.pack.iterdir() if p.is_file()}
        self.assertEqual(self.compile(), self.compile())
        self.assertEqual(original, {p.name: p.read_bytes() for p in self.pack.iterdir() if p.is_file()})

    def test_all_consumed_inputs_invalidate_cached_brief(self):
        for name in ["wechat-article.md", "image-storyboard.json", "growth-experiment.json", "claim-verification.json"]:
            with self.subTest(name=name):
                result = self.compile()
                path = self.pack / name
                original = path.read_bytes() if path.exists() else None
                path.write_text((path.read_text(encoding="utf-8") + "\n") if path.exists() else "{}", encoding="utf-8")
                self.assertFalse(brief.brief_is_current(self.pack, result, self.policy, self.root))
                if original is None:
                    path.unlink()
                else:
                    path.write_bytes(original)

    def test_policy_reference_and_tampered_prompt_invalidate(self):
        result = self.compile()
        changed = copy.deepcopy(result)
        changed["image_prompts"][0]["final_prompt"] = "tampered"
        self.assertFalse(brief.brief_is_current(self.pack, changed, self.policy, self.root))
        self.policy.write_bytes(self.policy.read_bytes() + b"\n")
        self.assertFalse(brief.brief_is_current(self.pack, result, self.policy, self.root))
        result = self.compile()
        Image.new("RGB", (32, 32), "red").save(self.pack / "reference.png")
        self.assertFalse(brief.brief_is_current(self.pack, result, self.policy, self.root))

    def test_cli_readonly_and_locked_atomic_backup(self):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(brief.main(["--pack", str(self.pack), "--no-write"]), 0)
            self.assertFalse((self.pack / brief.OUTPUT).exists())
            self.assertFalse((self.pack / ".backups").exists())
            self.assertEqual(brief.main(["--pack", str(self.pack)]), 0)
            first = (self.pack / brief.OUTPUT).read_bytes()
            self.assertEqual(brief.main(["--pack", str(self.pack)]), 0)
            self.assertEqual((self.pack / brief.OUTPUT).read_bytes(), first)
        backups = sorted((self.pack / ".backups").iterdir())
        self.assertTrue(verify_pack_backup(backups[-1])["valid"])
        self.assertEqual((backups[-1] / brief.OUTPUT).read_bytes(), first)
        self.assertFalse(list(self.pack.glob("*.tmp")))

    def test_cli_existing_lock_stops_write(self):
        with pack_lock(self.pack, "other-process"):
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(brief.main(["--pack", str(self.pack)]), 2)
        self.assertFalse((self.pack / brief.OUTPUT).exists())

    def test_path_escape_and_malformed_json(self):
        with self.assertRaises(brief.BriefInputError):
            brief.local_path(self.pack, "../outside.json")
        (self.pack / "image-storyboard.json").write_text("[]", encoding="utf-8")
        self.assertFalse(brief.brief_is_current(self.pack, {}, self.policy, self.root))
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(brief.main(["--pack", str(self.pack)]), 2)
        self.assertFalse((self.pack / brief.OUTPUT).exists())

    def test_cli_output_symlink_never_overwrites_article(self):
        link = self.pack / brief.OUTPUT
        try:
            link.symlink_to(self.pack / "wechat-article.md")
        except OSError:
            self.skipTest("Symlink creation unavailable")
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(brief.main(["--pack", str(self.pack)]), 2)
        self.assertEqual((self.pack / "wechat-article.md").read_text(encoding="utf-8"), self.article)

    def test_cli_symlink_guard_without_os_privilege(self):
        original = Path.is_symlink
        def is_symlink(path):
            return path.name == brief.OUTPUT or original(path)
        with patch.object(Path, "is_symlink", is_symlink):
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(brief.main(["--pack", str(self.pack)]), 2)
        self.assertFalse((self.pack / brief.OUTPUT).exists())

    def test_concurrent_input_change_stops_compilation(self):
        original = brief.input_snapshot
        count = 0
        def snapshot(*args):
            nonlocal count
            result = original(*args)
            count += 1
            if count > 1:
                result["fingerprint"] = "changed"
            return result
        with patch.object(brief, "input_snapshot", snapshot):
            with self.assertRaisesRegex(brief.BriefInputError, "BRIEF_INPUT_CHANGED"):
                self.compile()

    def test_review_scores_sum_and_deferred_projects_not_installed(self):
        report = json.loads((brief.ROOT / "research/github-editorial-design-review-2026-10-06.json").read_text(encoding="utf-8"))
        self.assertEqual(sum(report["rubric"].values()), 100)
        self.assertEqual(len(report["candidates"]), 6)
        for candidate in report["candidates"]:
            self.assertEqual(set(candidate["score"]), set(report["rubric"]))
            self.assertEqual(sum(candidate["score"].values()), candidate["total"])
            for key, value in candidate["score"].items():
                self.assertGreaterEqual(value, 0)
                self.assertLessEqual(value, report["rubric"][key])
            if candidate["decision"] in {"defer_runtime", "not_selected_license_unverified"}:
                self.assertEqual(candidate["implemented"], [])
        self.assertEqual(report["local_implementation"]["new_dependencies"], [])

    def test_platform_cards_read_their_own_copy_with_lines(self):
        (self.pack / "toutiao-article.md").write_text("# News title\n\nDated change for readers.\n\n## Why\n\n- First task\n- Second task", encoding="utf-8")
        (self.pack / "xiaohongshu.md").write_text("# Checklist\n\nOne concrete use.\n\n1. Read\n2. Compare", encoding="utf-8")
        cards = {card["platform"]: card for card in self.compile()["platform_cards"]}
        context = cards["toutiao"]["current_draft"]
        self.assertEqual(context["title"], "News title")
        self.assertEqual(context["opening"][0]["text"], "Dated change for readers.")
        self.assertEqual(context["opening"][0]["line"], 3)
        self.assertEqual(context["list_item_count"], 2)
        self.assertEqual(cards["xiaohongshu"]["current_draft"]["list_item_count"], 2)
        self.assertEqual(cards["zhihu"]["current_draft"]["availability"], "missing")

    def test_each_platform_and_short_backup_invalidate_snapshot(self):
        for name in [*brief.PLATFORM_FILES.values(), "weibo.md"]:
            with self.subTest(name=name):
                path = self.pack / name
                path.write_text("# Title\n\nOriginal", encoding="utf-8")
                result = self.compile()
                path.write_text("# Title\n\nModified", encoding="utf-8")
                self.assertFalse(brief.brief_is_current(self.pack, result, self.policy, self.root))

    def test_short_backup_not_a_sixth_platform_action(self):
        (self.pack / "weibo.md").write_text("# Short\n\nA short backup.", encoding="utf-8")
        result = self.compile()
        self.assertEqual(len(result["platform_cards"]), 5)
        self.assertEqual(result["weibo_short_backup"]["posting_action"], "none")
        self.assertEqual(result["weibo_short_backup"]["current_draft"]["title"], "Short")

    def test_identical_visible_bodies_warn_without_forcing_variation(self):
        (self.pack / "toutiao-article.md").write_text(self.article.replace("# News\n", "# Other title\n"), encoding="utf-8")
        result = self.compile()
        self.assertIn(["wechat", "toutiao"], result["identical_body_pairs"])
        self.assertEqual(result["status"], "prepared")
        issue = next(i for i in result["issues"] if i["code"] == "BRIEF_SHARED_BODY_REVIEW")
        self.assertIn("preserve shared facts", issue["action"])

    def test_repeating_one_fact_is_not_identical_body(self):
        shared = "A verified change matters to a specific reader."
        (self.pack / "toutiao-article.md").write_text(f"# News\n\n{shared}\n\nDifferent explanation for this reader.", encoding="utf-8")
        self.assertFalse(self.compile()["identical_body_pairs"])

    def test_long_paragraph_warning_locates_non_wechat_file(self):
        (self.pack / "zhihu-article.md").write_text("# Question\n\n" + "word " * 50, encoding="utf-8")
        issues = self.compile()["issues"]
        self.assertTrue(any(i["code"] == "BRIEF_PARAGRAPH_DENSITY_REVIEW" and i["reference"] == "zhihu-article.md:3" for i in issues))

    def test_placed_image_context_is_actual_section(self):
        image = "images/2026-10-07-01.png"
        text = self.article.replace("Explanation 0.", f"Explanation 0.\n\n![Concept]({image})")
        (self.pack / "wechat-article.md").write_text(text, encoding="utf-8")
        (self.pack / "toutiao-article.md").write_text(f"# News\n\n## New heading\n\nSpecific context.\n\n![Concept]({image})", encoding="utf-8")
        result = self.compile()
        context = result["image_prompts"][0]["article_context_for_review_only"]
        self.assertEqual(context["placement"], "placed")
        self.assertIn("Explanation 0.", context["platform_sections"]["wechat"][0])
        self.assertNotIn("Explanation 1.", context["platform_sections"]["wechat"][0])
        self.assertIn("New heading", context["platform_sections"]["toutiao"][0])
        self.assertFalse(context["fact_verification_performed"])
        self.assertNotIn("Approved author brief", result["image_prompts"][0]["final_prompt"])

    def test_anchor_in_another_section_cannot_justify_placement(self):
        text = self.article.replace("Explanation 1.", "Explanation 1.\n\n![Concept](images/2026-10-07-01.png)")
        (self.pack / "wechat-article.md").write_text(text, encoding="utf-8")
        result = self.compile()
        self.assertIn("BRIEF_ANCHOR_SECTION_MISMATCH", self.codes(result))
        self.assertEqual(result["image_prompts"][0]["article_context_for_review_only"]["placement"], "anchor_mismatch")
        self.assertEqual(result["status"], "needs_input")

    def test_unplaced_image_stays_pending_not_inferred_from_anchor(self):
        result = self.compile()
        context = result["image_prompts"][0]["article_context_for_review_only"]
        self.assertEqual(context["placement"], "not_placed")
        self.assertEqual(context["platform_sections"]["wechat"], [])
        self.assertTrue(context["anchor_matches"])

    def test_duplicate_image_placement_is_ambiguous(self):
        image = "![Concept](images/2026-10-07-01.png)"
        text = self.article.replace("Explanation 0.", f"Explanation 0.\n\n{image}").replace("Explanation 1.", f"Explanation 1.\n\n{image}")
        (self.pack / "wechat-article.md").write_text(text, encoding="utf-8")
        result = self.compile()
        self.assertIn("BRIEF_IMAGE_PLACEMENT_AMBIGUOUS", self.codes(result))

    def test_platform_code_and_alt_are_not_copy_or_images(self):
        context = brief.draft_context("# Visible\n\n```\n# Hidden\n![Alt](hidden.png)\n```\n\n![Caption](real.png)", True)
        self.assertEqual(context["title"], "Visible")
        self.assertEqual(context["paragraphs"], [])
        self.assertEqual(context["inline_image_references"], ["real.png"])

    def test_policy_cannot_redirect_files_or_add_disabled_platform(self):
        original = json.loads(self.policy.read_text(encoding="utf-8"))
        for variant in ("path", "disabled"):
            policy = copy.deepcopy(original)
            if variant == "path":
                policy["platforms"]["wechat"]["file"] = "other.md"
            else:
                policy["platforms"]["douyin"] = {"file": "douyin.md"}
            self.policy.write_text(json.dumps(policy), encoding="utf-8")
            with self.assertRaisesRegex(brief.BriefInputError, "BRIEF_POLICY_PLATFORM_SCOPE"):
                self.compile()

    def test_source_urls_remain_intact_without_density_false_positive(self):
        urls = ["https://example.com/" + "path" * 30, "https://example.org/" + "source" * 30]
        text = "# News\n\nSources:\n" + "\n".join(urls)
        (self.pack / "toutiao-article.md").write_text(text, encoding="utf-8")
        result = self.compile()
        card = next(c for c in result["platform_cards"] if c["platform"] == "toutiao")
        paragraph = card["current_draft"]["paragraphs"][0]
        for url in urls:
            self.assertIn(url, paragraph["text"])
        self.assertLess(paragraph["density_character_count"], 20)
        self.assertFalse(any(i["code"] == "BRIEF_PARAGRAPH_DENSITY_REVIEW" and i["reference"].startswith("toutiao") for i in result["issues"]))

    def test_source_label_does_not_hide_long_explanation(self):
        text = "# News\n\nSources: " + "word " * 50 + "\nhttps://example.com/source"
        (self.pack / "toutiao-article.md").write_text(text, encoding="utf-8")
        self.assertTrue(any(i["code"] == "BRIEF_PARAGRAPH_DENSITY_REVIEW" and i["reference"] == "toutiao-article.md:3" for i in self.compile()["issues"]))

    def test_editorial_advice_covers_platforms_and_short_backup(self):
        for name in [*brief.PLATFORM_FILES.values(), "weibo.md"]:
            (self.pack / name).write_text("# News\n\n在人工智能飞速发展的今天，读者需要新事实。", encoding="utf-8")
        result = self.compile()
        findings = [i for i in result["issues"] if i["code"] == "EDIT_STAGED_OPENING"]
        self.assertEqual({i["reference"] for i in findings}, {f"{name}:3" for name in [*brief.PLATFORM_FILES.values(), "weibo.md"]})
        self.assertEqual(result["weibo_short_backup"]["posting_action"], "none")
        self.assertFalse(result["platform_cards"][0]["editorial_review"]["automatic_rewrite"])

    def test_advisor_code_is_bound_to_brief(self):
        result = self.compile()
        self.assertEqual(len(result["inputs"]["editorial_advisor_sha256"]), 64)
        original = brief.file_sha256
        def changed(path):
            return "changed" if Path(path).name == "editorial_advisor.py" else original(path)
        with patch.object(brief, "file_sha256", changed):
            self.assertFalse(brief.brief_is_current(self.pack, result, self.policy, self.root))

    def test_visual_explanation_missing_optional_not_hard_gate(self):
        result = self.compile()
        self.assertEqual(result["status"], "prepared")
        self.assertIn("BRIEF_VISUAL_EXPLANATION_REVIEW", self.codes(result))
        self.assertEqual(result["image_prompts"][0]["visual_explanation"]["availability"], "not_provided")
        self.assertEqual(len(result["visual_review_queue"]), 5)
        self.assertTrue(all(p["actual_image_review"] == "not_performed" for p in result["visual_review_queue"]))

    def test_visual_intent_exactly_preserved_not_added_as_labels(self):
        intent = {"subject": "A task card", "relationship": "The draft is separated from the outgoing tray.",
                  "reader_takeaway": "Human review is a boundary.", "avoid_implication": "No automatic publication or measured success."}
        self.board["items"][0]["visual_explanation"] = intent
        self.save_board()
        result = self.compile()
        prompt = result["image_prompts"][0]
        self.assertEqual(prompt["visual_explanation"]["author_fields"], intent)
        self.assertEqual(prompt["visual_explanation"]["availability"], "specified")
        for value in intent.values():
            self.assertIn(value, prompt["final_prompt"])
        self.assertIn("not additional on-image text", prompt["final_prompt"])
        self.assertIn("Text-free illustration", prompt["final_prompt"])
        self.assertEqual(prompt["on_image_text"], "")
        self.assertFalse(prompt["visual_explanation"]["fact_verification_performed"])
        self.board["items"][0]["visual_explanation"]["relationship"] += " Changed."
        self.save_board()
        self.assertFalse(brief.brief_is_current(self.pack, result, self.policy, self.root))

    def test_incomplete_or_malformed_visual_intent_is_not_silently_injected(self):
        for value in (True, "text", [], {"subject": "DO NOT INJECT"}, {"subject": 42}):
            with self.subTest(value=value):
                self.board["items"][0]["visual_explanation"] = value
                self.save_board()
                result = self.compile()
                self.assertEqual(result["status"], "prepared")
                prompt = result["image_prompts"][0]
                self.assertEqual(prompt["visual_explanation"]["availability"], "incomplete")
                self.assertNotIn("DO NOT INJECT", prompt["final_prompt"])

    def test_same_visible_relation_needs_review_not_automatic_visual_score(self):
        for item in self.board["items"]:
            item["visual_explanation"] = {"subject": "Huazi", "relationship": "Same arrangement",
                                          "reader_takeaway": item["message"], "avoid_implication": "No unverified claim"}
        self.save_board()
        result = self.compile()
        self.assertIn("BRIEF_SHARED_VISUAL_RELATION_REVIEW", self.codes(result))
        self.assertEqual(result["status"], "prepared")
        self.assertTrue(all("score" not in item for item in result["visual_review_queue"]))

    def test_question_led_experiment_is_optional_and_cannot_approve_drafts(self):
        result = self.compile()
        self.assertEqual(result["editorial_experiment"]["status"], "not_planned")
        self.assertEqual(result["status"], "prepared")
        self.assertFalse(result["editorial_experiment"]["causal_claim_allowed"])
        self.assertEqual(len(result["editorial_experiment"]["tactic_catalog"]), 5)

    def test_experiment_plan_and_images_are_bound_to_brief(self):
        result = self.compile()
        (self.pack / "editorial-experiment.json").write_text('{"schema_version": 1}', encoding="utf-8")
        self.assertFalse(brief.brief_is_current(self.pack, result, self.policy, self.root))
        result = self.compile()
        image_path = self.pack / "images/2026-10-07-01.png"
        image_path.parent.mkdir(exist_ok=True)
        Image.new("RGB", (9, 9), "red").save(image_path)
        self.assertFalse(brief.brief_is_current(self.pack, result, self.policy, self.root))

    def test_experiment_policy_and_module_are_bound(self):
        result = self.compile()
        original = brief.file_sha256
        for name in ("editorial-experiments.json", "editorial_experiments.py"):
            def changed(path):
                return "changed" if Path(path).name == name else original(path)
            with patch.object(brief, "file_sha256", changed):
                self.assertFalse(brief.brief_is_current(self.pack, result, self.policy, self.root))

    def test_professional_direction_does_not_replace_labels_or_layout(self):
        self.board["items"][0]["on_image_text"] = "48% on one benchmark"
        self.board["items"][0]["layout"] = "Author's deliberate composition"
        self.save_board()
        result = self.compile()
        prompt = result["image_prompts"][0]
        self.assertIn("Professional art direction", prompt["final_prompt"])
        self.assertIn("Author's deliberate composition", prompt["final_prompt"])
        self.assertEqual(prompt["on_image_text"], "48% on one benchmark")
        self.assertEqual(len({item["art_direction"] for item in result["image_prompts"]}), 5)
        self.assertTrue(all(item["actual_image_review"] == "not_performed" for item in result["visual_review_queue"]))

    def test_new_github_scores_have_inspected_commit_and_no_runtime_install(self):
        report = json.loads((brief.ROOT / "research/github-ai-editorial-experiments-2026-10-22.json").read_text(encoding="utf-8"))
        self.assertEqual(sum(report["rubric"].values()), 100)
        self.assertEqual(len(report["candidates"]), 5)
        for candidate in report["candidates"]:
            self.assertEqual(len(candidate["commit"]), 40)
            self.assertEqual(sum(candidate["score"].values()), candidate["total"])
            self.assertEqual(set(candidate["score"]), set(report["rubric"]))
            for key, score in candidate["score"].items():
                self.assertLessEqual(score, report["rubric"][key])
            for source in candidate["inspected_files"]:
                self.assertEqual(len(source["blob_sha"]), 40)
        self.assertEqual(report["local_implementation"]["installed_upstream_agents"], [])

    def test_current_github_scoring_is_reproducible_and_honest_about_installation(self):
        report = json.loads((brief.ROOT / "research/github-editorial-advisor-review-2026-10-09.json").read_text(encoding="utf-8"))
        self.assertEqual(sum(report["rubric"].values()), 100)
        self.assertEqual(len(report["candidates"]), 6)
        for candidate in report["candidates"]:
            self.assertEqual(len(candidate["commit"]), 40)
            self.assertIn(candidate["commit"], candidate["inspected_file"]["url"])
            self.assertEqual(len(candidate["inspected_file"]["blob_sha"]), 40)
            self.assertEqual(sum(candidate["score"].values()), candidate["total"])
            self.assertEqual(set(candidate["score"]), set(report["rubric"]))
            for key, score in candidate["score"].items():
                self.assertGreaterEqual(score, 0)
                self.assertLessEqual(score, report["rubric"][key])
            if candidate["license"] is None:
                self.assertEqual(candidate["implemented"], [])
        self.assertEqual(report["local_implementation"]["new_dependencies"], [])
        self.assertEqual(report["local_implementation"]["installed_upstream_agents"], [])


if __name__ == "__main__":
    unittest.main()
