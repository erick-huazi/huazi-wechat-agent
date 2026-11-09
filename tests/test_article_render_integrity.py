import base64
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import article_render_integrity as integrity

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+nmZkAAAAASUVORK5CYII=")


class ArticleRenderIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.pack = Path(self.tmp.name)
        (self.pack / "a.png").write_bytes(PNG)
        (self.pack / "b.png").write_bytes(PNG)
        self.article = self.pack / "wechat-article.md"
        self.article.write_text("# 标题\n\n## 事实\n\n**48%** 仅在条件A，不能推及其他人。 [来源](https://example.org/a)\n\n"
                                "![甲](a.png)\n\n3. 条件\n4. 结果\n\n![乙](b.png)\n\n"
                                "AI辅助写作。\n", encoding="utf-8")

    def test_preserves_body_headings_links_images_and_list_number(self):
        result = integrity.inspect_article(self.pack)
        self.assertTrue(result["passed"])
        self.assertEqual(2, result["image_count"])
        self.assertTrue(all(result["checks"].values()))

    def test_changed_numeric_scope_detected(self):
        original = integrity.render_article
        def changed(*args, **kwargs):
            rendered, paths = original(*args, **kwargs)
            return rendered.replace("条件A", "条件B"), paths
        with patch.object(integrity, "render_article", side_effect=changed):
            result = integrity.inspect_article(self.pack)
        self.assertFalse(result["passed"])
        self.assertFalse(result["checks"]["body_text_whitespace_normalized"])

    def test_changed_link_and_alt_detected(self):
        original = integrity.render_article
        def changed(*args, **kwargs):
            rendered, paths = original(*args, **kwargs)
            return rendered.replace('href="https://example.org/a"', 'href="https://example.org/b"').replace('alt="甲"', 'alt="乙"'), paths
        with patch.object(integrity, "render_article", side_effect=changed):
            result = integrity.inspect_article(self.pack)
        self.assertFalse(result["checks"]["links_in_order"])
        self.assertFalse(result["checks"]["image_references_and_alt_in_order"])

    def test_changed_ordered_list_start_detected(self):
        original = integrity.render_article
        def changed(*args, **kwargs):
            rendered, paths = original(*args, **kwargs)
            return rendered.replace('start="3"', 'start="1"'), paths
        with patch.object(integrity, "render_article", side_effect=changed):
            result = integrity.inspect_article(self.pack)
        self.assertFalse(result["checks"]["ordered_list_starts"])

    def test_missing_title_and_external_image_fail_before_network(self):
        self.article.write_text("仅正文", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "ARTICLE_TITLE_MISSING"):
            integrity.inspect_article(self.pack)
        self.article.write_text("# 标题\n\n![图片](https://example.org/x.png)", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "LAYOUT_EXTERNAL_IMAGE"):
            integrity.inspect_article(self.pack)

    def test_source_changes_are_not_accepted(self):
        original = integrity.render_article
        def changed(*args, **kwargs):
            result = original(*args, **kwargs)
            self.article.write_text(self.article.read_text(encoding="utf-8") + "\n变更", encoding="utf-8")
            return result
        with patch.object(integrity, "render_article", side_effect=changed):
            self.assertFalse(integrity.inspect_article(self.pack)["checks"]["source_unchanged"])


if __name__ == "__main__":
    unittest.main()
