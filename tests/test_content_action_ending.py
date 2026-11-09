import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import content_quality_gate as gate


class ActionEndingTests(unittest.TestCase):
    def check(self, body, expected, date="2026-10-17"):
        policy = gate.load_standard()
        spec = policy["platforms"]["wechat"]
        item = gate.platform_item(Path(date + "-test"), "wechat", spec, policy,
                                  ("# Test title\n\n" + body, "test-hash"))
        self.assertEqual("self_contained_action_missing" not in item["blocking"], expected)
        return item

    def test_long_references_do_not_hide_action(self):
        for heading in ("一手资料", "来源", "参考资料", "References"):
            with self.subTest(heading=heading):
                item = self.check("今天先列出需要交付的内容。\n\n## " + heading + "\n\n" + "资料说明。" * 100, True)
                self.assertIn("今天先", item["metrics"]["ending_sample"])

    def test_reference_action_does_not_supply_action(self):
        self.check("本次更新值得观察。\n\n## 一手资料\n\n检查模型说明。", False)

    def test_non_prose_action_does_not_supply_action(self):
        for suffix in ("```\n检查\n```", "![检查](image.png)", "[检查](https://example.org)",
                       "> 检查全部结果", "AI辅助整理，今天先检查。", "<div>检查</div>"):
            with self.subTest(suffix=suffix):
                self.check("本次更新值得观察。\n\n" + suffix, False)

    def test_action_in_actual_list_and_after_sources(self):
        self.check("## 来源\n\n出处。\n\n## 行动\n\n- 检查最终结果。", True)

    def test_legacy_keeps_existing_behavior(self):
        self.check("今天先列出要求。\n\n## 来源\n\n" + "资料。" * 100, False, "2026-08-22")


if __name__ == "__main__":
    unittest.main()
