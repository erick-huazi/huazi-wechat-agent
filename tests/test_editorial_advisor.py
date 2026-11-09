import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from editorial_advisor import review_prose


class EditorialAdvisorTests(unittest.TestCase):
    def review(self, text):
        before = copy.deepcopy(text)
        result = review_prose(text, "wechat-article.md")
        self.assertEqual(before, text)
        self.assertFalse(result["automatic_rewrite"])
        self.assertFalse(result["fact_verification_performed"])
        self.assertNotIn("score", result)
        self.assertNotIn("passed", result)
        return result["findings"]

    def test_chinese_opening_heading_and_source_lines(self):
        text = "# 新闻\n\n在人工智能飞速发展的今天，我们需要理解工具。\n\n## 写在最后\n\n检查任务范围。"
        issues = self.review(text)
        self.assertEqual([i["code"] for i in issues], ["EDIT_STAGED_OPENING", "EDIT_GENERIC_HEADING"])
        self.assertEqual([i["line"] for i in issues], [3, 5])
        self.assertEqual(issues[0]["source_excerpt"], text.splitlines()[2])
        self.assertEqual(issues[1]["reference"], "wechat-article.md:5")

    def test_one_or_two_valid_contrasts_do_not_trigger(self):
        self.assertEqual(self.review("不是模型能力，而是账号权限。\n\n不是公开发布，而是草稿。"), [])

    def test_repeated_contrasts_are_review_not_rewrite(self):
        issues = self.review("不是速度，而是权限。\n\n不是范围，而是条件。\n\n不是证明，而是线索。")
        self.assertEqual(len(issues), 3)
        self.assertTrue(all(i["code"] == "EDIT_REPEATED_CONTRAST" and i["severity"] == "advisory" for i in issues))
        self.assertIn("保留", issues[0]["action"])

    def test_code_quotes_links_and_image_alt_are_not_prose(self):
        phrase = "不是速度，而是权限。"
        for wrapped in (f"> {phrase}", f"`{phrase}`", f"```\n{phrase}\n```", f"    {phrase}",
                        f"“{phrase}”", f'"{phrase}"', f"「{phrase}」", f"[{phrase}](https://example.com)",
                        f"![{phrase}](img.png)", f"<span>{phrase}</span>", f"“不是**速度**，而是权限。”"):
            with self.subTest(wrapped=wrapped):
                self.assertEqual(self.review((wrapped + "\n\n") * 3), [])

    def test_inline_code_never_joins_surrounding_words(self):
        self.assertEqual(self.review(("不是 `ignored` 而是权限。\n\n") * 3), [])

    def test_sources_nested_until_peer_heading(self):
        text = "# 标题\n\n## 来源\n\n不是一，而是二。\n\n### 原句\n\n不是三，而是四。\n\n不是五，而是六。\n\n## 分析\n\n不是结果，而是线索。"
        self.assertEqual(self.review(text), [])
        text += "\n\n不是权限，而是身份。\n\n不是效率，而是质量。"
        self.assertEqual(len(self.review(text)), 3)

    def test_frontmatter_and_disclosure_stay_untouched(self):
        text = "---\ntitle: 不是速度，而是权限\n---\n\n" + ("本文由 AI 辅助，不是实测，而是解读。\n\n" * 3)
        self.assertEqual(self.review(text), [])

    def test_metadata_does_not_change_line_locations(self):
        text = "---\ntitle: fixture\n---\n\n# 新闻\n\n在人工智能飞速发展的今天，应核验。"
        self.assertEqual(self.review(text)[0]["line"], 7)

    def test_emphasis_density_ignores_short_useful_labels(self):
        self.assertEqual(self.review("**适用边界**：仍需人工复核。"), [])
        text = "**" + "先检查权限范围，保留人工复核。" * 6 + "**"
        self.assertEqual(self.review(text)[0]["code"], "EDIT_EMPHASIS_DENSITY")

    def test_numerical_evidence_ai_disclosure_no_false_verdict(self):
        text = "# 核对\n\n模型 A 48% (n=25，仅测试 X)，模型 B 0% (同条件)；不是现实频率，而是该项测量。\n\n[原文](https://example.com/v1?x=48)\n\n本文由 AI 辅助整理，未实测。"
        self.assertEqual(self.review(text), [])

    def test_multiline_excerpt_is_source_not_synthesized_quote(self):
        text = "在人工智能飞速发展的今天，\n**需要核验**。"
        issue = self.review(text)[0]
        self.assertEqual((issue["line"], issue["end_line"]), (1, 2))
        self.assertEqual(issue["source_excerpt"], text)

    def test_empty_or_unmatched_pattern_does_not_claim_quality(self):
        self.assertEqual(self.review(""), [])
        self.assertEqual(self.review("# 华子观察\n\n仅是一个短句，不证明文章合格。"), [])

    def test_exact_heading_echo_is_advisory_and_keeps_following_facts(self):
        text = "## 长文档仍需人工核对\n\n长文档仍需人工核对。限定模型A，48%（n=25），条件X。"
        findings = self.review(text)
        self.assertEqual([row["code"] for row in findings], ["EDIT_HEADING_ECHO"])
        self.assertEqual(findings[0]["line"], 3)
        self.assertIn("48%（n=25），条件X", findings[0]["source_excerpt"])

    def test_heading_echo_checks_only_first_prose_block_and_never_h1(self):
        for text in ("# 长文档仍需人工核对\n\n长文档仍需人工核对。",
                     "## 长文档仍需人工核对\n\n先检查输入范围。\n\n长文档仍需人工核对。",
                     "## 先核对\n\n先核对。"):
            self.assertNotIn("EDIT_HEADING_ECHO", {row["code"] for row in self.review(text)})

    def test_heading_echo_does_not_use_sources_quotes_or_html(self):
        phrase = "长文档仍需人工核对"
        for body in (f"> {phrase}。", f"`{phrase}`。", f"“{phrase}”。", f"<p>{phrase}。</p>",
                     f"## 来源与说明\n\n{phrase}。"):
            self.assertNotIn("EDIT_HEADING_ECHO", {row["code"] for row in self.review(f"## {phrase}\n\n{body}")})

    def test_new_heading_replaces_old_echo_target(self):
        text = "## 长文档仍需人工核对\n\n### 原始文件需要保留\n\n长文档仍需人工核对。"
        self.assertNotIn("EDIT_HEADING_ECHO", {row["code"] for row in self.review(text)})

    def test_heading_prefix_is_not_exact_echo(self):
        self.assertNotIn("EDIT_HEADING_ECHO", {row["code"] for row in self.review("## 长文档仍需人工核对\n\n长文档仍需人工核对页码与金额。")})

    def test_heading_echo_preserves_inline_emphasis_location(self):
        text = "## 原始文件需要保留\n\n**原始文件需要保留**。\n\n正文新信息。"
        findings = self.review(text)
        self.assertEqual([row["code"] for row in findings], ["EDIT_HEADING_ECHO"])
        self.assertEqual(findings[0]["source_excerpt"], "**原始文件需要保留**。")

    def test_promise_opening_is_advisory_only(self):
        issues = self.review('# 新闻\n\n本文将介绍新模型。\n\n模型今天开放。')
        self.assertEqual([i['code'] for i in issues], ['EDIT_PROMISE_OPENING_REVIEW'])
        self.assertEqual(issues[0]['line'], 3)
        self.assertIn('人工核对', issues[0]['action'])

    def test_first_paragraph_containing_news_not_matching_promise_pattern(self):
        issues = self.review('# 新闻\n\n官方发布新模型。本文将介绍它的使用条件。')
        self.assertNotIn('EDIT_PROMISE_OPENING_REVIEW', {i['code'] for i in issues})

    def test_repeated_caveats_flag_without_removing_conditions(self):
        text = ('这不是独立实测，模型A的结果仍需复核。\n\n'
                '这不代表所有用户都能得到相同结果。\n\n'
                '该例不等于真实性能实测，仍需检查。')
        issues = self.review(text)
        self.assertEqual([i['code'] for i in issues], ['EDIT_CAVEAT_REPETITION_REVIEW'] * 3)
        self.assertTrue(all('必要限定必须保留' in i['action'] for i in issues))

    def test_caveats_in_quotes_sources_and_disclosures_are_excluded(self):
        text = ('“这不是独立实测。”\n\n'
                '本文由AI辅助生成，不代表所有用户都能使用。\n\n'
                '## 来源\n\n这不是独立实测。\n\n这不是官方保证。')
        self.assertNotIn('EDIT_CAVEAT_REPETITION_REVIEW', {i['code'] for i in self.review(text)})


if __name__ == "__main__":
    unittest.main()
