from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("content_compliance", ROOT / "scripts" / "content_compliance.py")
COMPLIANCE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(COMPLIANCE)


class ContentComplianceTests(unittest.TestCase):
    def test_blocks_keyword_exchange(self) -> None:
        result = COMPLIANCE.scan_text("需要六步清单，留言关键词：录活清单。", platform_id="xiaohongshu")
        self.assertFalse(result["passed"])
        self.assertIn("engagement.keyword_exchange", {item["rule_id"] for item in result["findings"]})

    def test_blocks_interaction_for_reward(self) -> None:
        result = COMPLIANCE.scan_text("点赞收藏后即可领取完整模板。", platform_id="toutiao")
        self.assertFalse(result["passed"])
        self.assertIn("engagement.action_for_reward", {item["rule_id"] for item in result["findings"]})

    def test_blocks_direct_comment_prompt_and_mutual_follow(self) -> None:
        result = COMPLIANCE.scan_text("评论区留下你的行业。互关回关。", platform_id="weibo")
        self.assertFalse(result["passed"])
        rule_ids = {item["rule_id"] for item in result["findings"]}
        self.assertIn("engagement.comment_prompt", rule_ids)
        self.assertIn("engagement.mutual_or_growth", rule_ids)

    def test_allows_descriptive_attention_and_self_contained_action(self) -> None:
        text = (
            "这个项目值得关注，但 GitHub Star 只代表社区热度。\n"
            "今天先选一项每周重复两次的工作，把输入、步骤和验收标准写成三行。"
        )
        result = COMPLIANCE.scan_text(text, platform_id="wechat")
        self.assertTrue(result["passed"])
        self.assertEqual(result["findings"], [])

    def test_markdown_source_link_is_not_treated_as_offsite_traffic(self) -> None:
        result = COMPLIANCE.scan_text(
            "事实来源：[Microsoft Skill Recorder](https://github.com/microsoft/skill-recorder)",
            platform_id="zhihu",
        )
        self.assertTrue(result["passed"])

    def test_scan_path_reports_line_and_suggestion(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "copy.md"
            path.write_text("# 标题\n\n欢迎点赞关注。\n", encoding="utf-8")
            result = COMPLIANCE.scan_path(path, platform_id="douyin-image")
            self.assertFalse(result["passed"])
            self.assertEqual(result["findings"][0]["line"], 3)
            self.assertTrue(result["findings"][0]["suggestion"])


if __name__ == "__main__":
    unittest.main()
