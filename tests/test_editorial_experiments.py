import copy
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import editorial_experiments as experiments


ROOT = Path(__file__).resolve().parents[1]
POLICY = json.loads((ROOT / "config/editorial-experiments.json").read_text(encoding="utf-8"))


class EditorialExperimentTests(unittest.TestCase):
    def setUp(self):
        self.article = "# A verified update\n\nThe model changed.\n\n## Explanation\n\nSame evidence and limits.\n\nAI assisted.\n"
        self.frozen = {"article_except_opening": experiments.opening_context(self.article)["frozen_body_sha256"],
                       "images/01.png": "a" * 64}
        self.growth = {key: "A predeclared condition." for key in experiments.GROWTH_FIELDS}
        self.growth["variable"] = "opening_hook"
        self.plan = {
            "schema_version": 1, "variable": "opening_hook", "tactic": "reader_problem",
            "control_opening": "The model changed.",
            "treatment_opening": "What task does the model change affect?",
            "problem": {**{key: "An explicit answer." for key in experiments.PROBLEM_FIELDS},
                        "audience_basis": "editorial_hypothesis", "source_urls": ["https://example.org/source"]},
            "frozen_inputs": copy.deepcopy(self.frozen),
            "measurement": {"design": "observational", **{key: "Predeclared; missing data stays unknown." for key in experiments.MEASUREMENT_FIELDS}},
        }

    def review(self):
        return experiments.review_experiment(self.plan, self.growth, self.article, self.frozen, POLICY)

    def codes(self):
        return {finding["code"] for finding in self.review()["findings"]}

    def test_valid_is_editor_review_not_experimental_success(self):
        result = self.review()
        self.assertEqual(result["status"], "ready_for_editor_review")
        self.assertEqual(result["experiment_result"], "inconclusive")
        self.assertFalse(result["causal_claim_allowed"])
        self.assertFalse(result["traffic_collected"])
        self.assertEqual(result["actual_image_review"], "not_performed")
        self.assertEqual(len(result["variant_previews"]), 2)
        self.assertEqual(result["variant_previews"][0]["article_sha256"], experiments.text_hash(self.article))

    def test_missing_plan_is_optional_and_no_migration(self):
        self.plan = None
        self.assertEqual(self.review()["status"], "not_planned")
        self.assertIn("EXP_PLAN_OPTIONAL", self.codes())

    def test_bool_and_malformed_schema(self):
        for value in (True, [], "1", 2, None):
            self.plan["schema_version"] = value
            self.assertIn("EXP_SCHEMA", self.codes())

    def test_only_one_supported_tactic_and_matching_variable(self):
        self.plan["tactic"] = ["reader_problem", "scenario_question"]
        self.plan["variable"] = ["opening_hook", "format"]
        self.assertIn("EXP_TACTIC", self.codes())
        self.assertIn("EXP_VARIABLE_MISMATCH", self.codes())

    def test_growth_mismatch_and_incomplete_record(self):
        self.growth = {"variable": "emotion_tension"}
        self.assertIn("EXP_VARIABLE_MISMATCH", self.codes())
        self.assertIn("EXP_GROWTH_INCOMPLETE", self.codes())

    def test_evidence_cannot_be_demographic_guess(self):
        self.plan["problem"]["audience_basis"] = "public_evidence"
        self.assertIn("EXP_AUDIENCE_EVIDENCE", self.codes())
        self.plan["problem"]["audience_evidence_urls"] = ["https://example.org/discussion"]
        self.assertNotIn("EXP_AUDIENCE_EVIDENCE", self.codes())

    def test_non_string_audience_basis_is_diagnosed_not_crashed(self):
        for value in ([], {}, True, 5):
            self.plan["problem"]["audience_basis"] = value
            self.assertIn("EXP_AUDIENCE_BASIS", self.codes())

    def test_source_requires_noncredential_url(self):
        for value in ([], ["file:///private"], ["https://name:secret@example.org"], ["https://["]):
            self.plan["problem"]["source_urls"] = value
            self.assertIn("EXP_SOURCE_REQUIRED", self.codes())

    def test_identical_invalid_or_expanded_opening(self):
        self.plan["treatment_opening"] = self.plan["control_opening"]
        self.assertIn("EXP_NO_CHANGE", self.codes())
        for value in ("# New heading", "New\n\nMore", "![New](image.png)", "<img src='x'>", "[Link](https://example.org)", "**Stronger**"):
            self.plan["treatment_opening"] = value
            self.assertIn("EXP_OPENING_INVALID", self.codes())

    def test_numbers_cannot_be_replaced_as_hook(self):
        self.plan["control_opening"] = "Score 48%."
        self.plan["treatment_opening"] = "Score 0%."
        self.assertIn("EXP_NUMERIC_SCOPE_CHANGED", self.codes())

    def test_same_numbers_do_not_prove_same_meaning(self):
        self.plan["control_opening"] = "Score 48%."
        self.plan["treatment_opening"] = "Failure 48%."
        self.assertIn("same factual claims and qualifications", self.review()["semantic_review_required"])
        self.assertEqual(self.review()["fact_review"], "not_performed")

    def test_article_must_contain_one_declared_variant(self):
        self.article = self.article.replace("The model changed.", "A third opening.")
        self.assertIn("EXP_OPENING_NOT_CURRENT", self.codes())

    def test_frozen_body_preserves_title_images_qualifiers_and_disclosure(self):
        base = experiments.opening_context(self.article)["frozen_body_sha256"]
        for old, new in (("# A verified update", "# Another title"), ("Same evidence and limits.", "New evidence."), ("AI assisted.", "")):
            changed = self.article.replace(old, new)
            self.assertNotEqual(experiments.opening_context(changed)["frozen_body_sha256"], base)
        changed = self.article.replace("The model changed.", "Another opening.")
        self.assertEqual(experiments.opening_context(changed)["frozen_body_sha256"], base)

    def test_frozen_images_changed_missing_extra_or_absent(self):
        for new in ({"other": "hash"}, None, {}, {**self.frozen, "unplanned": "hash"}):
            self.plan["frozen_inputs"] = new
            self.assertIn("EXP_CONTROL_DRIFT", self.codes())
        self.frozen["images/01.png"] = None
        self.assertIn("EXP_FREEZE_INCOMPLETE", self.codes())

    def test_measurement_does_not_claim_random_assignment(self):
        self.plan["measurement"]["design"] = "randomized"
        del self.plan["measurement"]["denominator"]
        self.assertIn("EXP_DESIGN_UNSUPPORTED", self.codes())
        self.assertIn("EXP_MEASUREMENT_INCOMPLETE", self.codes())

    def test_cannot_inject_result_or_auto_publish(self):
        self.plan["result"] = "win"
        self.plan["auto_publish"] = True
        self.assertEqual(self.review()["experiment_result"], "inconclusive")
        self.assertFalse(self.review()["automatic_rewrite"])

    def test_handles_frontmatter_cover_crlf_and_repeated_text(self):
        article = "---\r\ntitle: Metadata\r\n---\r\n# Real title\r\n\r\n![Cover](01.png)\r\n\r\nOpening.\r\n\r\nOpening.\r\n"
        context = experiments.opening_context(article)
        self.assertEqual(context["opening"], "Opening.")
        self.assertEqual(context["line_ending"], "\r\n")
        self.assertEqual(article[context["end"]:], "\r\nOpening.\r\n")

    def test_no_guess_inside_quotes_lists_html_or_no_title(self):
        for value in ("No title.", "# Title\n\n> Quote\n\nLater.", "# Title\n\n- List\n\nLater.", "# Title\n\n<p>HTML</p>\n\nLater.", "# Title\n\n```\nCode\n```\n\nLater."):
            self.assertIsNone(experiments.opening_span(value))

    def test_no_mutation_and_determinism(self):
        before = copy.deepcopy((self.plan, self.growth, self.frozen))
        self.assertEqual(self.review(), self.review())
        self.assertEqual(before, (self.plan, self.growth, self.frozen))


if __name__ == "__main__":
    unittest.main()
