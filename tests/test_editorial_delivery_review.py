import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from editorial_advisor import prose_blocks, review_delivery


class EditorialDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.anchors = {
            "answer": "阅读长度增加不等于每页识别准确率提高。",
            "action": "取一份有原件的文档，逐页核对页码与表格。",
            "boundary": "遇到金额或页码不一致时停止自动入库。",
        }
        self.article = "# 模型更新\n\n新模型公开了文档处理能力。\n\n## 华子判断\n\n" + "\n\n".join(self.anchors.values())

    def codes(self, article=None, anchors=None):
        return {row["code"] for row in review_delivery(article if article is not None else self.article,
                                                       anchors if anchors is not None else self.anchors)["findings"]}

    def test_locates_three_actual_quotes_and_lines_without_approval(self):
        result = review_delivery(self.article, self.anchors)
        self.assertEqual(result["status"], "located_for_semantic_review")
        self.assertEqual([r["line"] for r in result["matches"]], [7, 9, 11])
        self.assertEqual([r["quote"] for r in result["matches"]], list(self.anchors.values()))
        self.assertTrue(all(r["status"] == "located_only" for r in result["matches"]))
        self.assertFalse(result["semantic_review_performed"])
        self.assertNotIn("passed", result)
        self.assertNotIn("score", result)

    def test_missing_is_optional_for_history(self):
        result = review_delivery(self.article, None)
        self.assertEqual(result["status"], "not_provided")
        self.assertEqual(result["matches"], [])

    def test_plan_promises_do_not_count_as_article_delivery(self):
        self.assertIn("DELIVERY_ANCHOR_NOT_FOUND", self.codes("# 标题\n\n只介绍产品，没有正文回答。"))

    def test_title_quotes_alt_code_links_sources_disclosure_and_captions_excluded(self):
        phrase = self.anchors["answer"]
        wrappers = (f"## {phrase}", f"> {phrase}", f"`{phrase}`", f"```\n{phrase}\n```",
                    f"    {phrase}", f"![{phrase}](image.png)", f"[{phrase}](https://example.org)",
                    f"<p>{phrase}</p>", f"“{phrase}”", f"## 来源与说明\n\n{phrase}",
                    f"## Sources and notes\n\n{phrase}", f"本文由AI辅助整理。{phrase}",
                    f"图注：{phrase}", f"图 1｜{phrase}")
        for wrapped in wrappers:
            with self.subTest(wrapped=wrapped):
                article = "# 标题\n\n" + wrapped
                self.assertIn("DELIVERY_ANCHOR_NOT_FOUND", self.codes(article))
                self.assertEqual(review_delivery(article, self.anchors)["matches"], [])

    def test_inline_code_cannot_join_text_around_it(self):
        article = self.article.replace("增加不等于", "增加`忽略`不等于")
        self.assertIn("DELIVERY_ANCHOR_NOT_FOUND", self.codes(article))

    def test_frontmatter_crlf_and_visible_emphasis_preserve_locations(self):
        article = "---\ntitle: test\n---\n" + self.article.replace(self.anchors["answer"], "**" + self.anchors["answer"] + "**")
        result = review_delivery(article.replace("\n", "\r\n"), self.anchors)
        self.assertEqual(result["status"], "located_for_semantic_review")
        self.assertEqual(result["matches"][0]["line"], 10)

    def test_duplicate_in_one_or_multiple_paragraphs_is_ambiguous(self):
        for glue in ("", "\n\n"):
            article = self.article.replace(self.anchors["answer"], (self.anchors["answer"] + glue) * 2)
            self.assertIn("DELIVERY_ANCHOR_AMBIGUOUS", self.codes(article))

    def test_malformed_short_long_or_missing_anchors(self):
        for bad in ([], {}, True, "answer", 2):
            self.assertIn("DELIVERY_ANCHOR_INVALID", self.codes(anchors=bad))
        for bad in (True, [], "很有用", " " + self.anchors["answer"], "x" * 601, "x" * 20 + "\x00"):
            self.assertIn("DELIVERY_ANCHOR_INVALID", self.codes(anchors={**self.anchors, "answer": bad}))

    def test_reusing_one_quote_needs_semantic_review_not_forced_filler(self):
        anchors = {role: self.anchors["answer"] for role in self.anchors}
        self.assertIn("DELIVERY_ROLE_OVERLAP_REVIEW", self.codes(anchors=anchors))

    def test_quote_cannot_span_multiple_paragraphs(self):
        self.assertIn("DELIVERY_ANCHOR_NOT_FOUND", self.codes(anchors={**self.anchors, "answer": self.anchors["answer"] + "\n\n" + self.anchors["action"]}))

    def test_exact_but_irrelevant_statement_does_not_prove_answer_quality(self):
        irrelevant = "This text is unrelated to the reader's question."
        result = review_delivery(self.article + "\n\n" + irrelevant, {**self.anchors, "answer": irrelevant})
        self.assertEqual(result["status"], "located_for_semantic_review")
        self.assertFalse(result["semantic_review_performed"])

    def test_source_section_ends_at_peer_heading(self):
        text = "## 来源与说明\n\n来源文字。\n\n### 附注\n\n附注文字。\n\n## 华子行动\n\n" + self.anchors["action"]
        blocks = prose_blocks(text)
        self.assertEqual([b["text"] for b in blocks], ["华子行动", self.anchors["action"]])

    def test_no_mutation_or_numerical_rewrite(self):
        article = self.article + "\n\n限定模型A，48%（n=25），只含条件X。"
        before = copy.deepcopy((article, self.anchors))
        self.assertEqual(review_delivery(article, self.anchors), review_delivery(article, self.anchors))
        self.assertEqual(before, (article, self.anchors))


if __name__ == "__main__":
    unittest.main()
