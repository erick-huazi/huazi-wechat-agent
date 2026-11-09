from __future__ import annotations

import importlib.util
import tempfile
import unittest
import io
import json
import sys
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "wechat_draft_publisher", ROOT / "scripts" / "wechat_draft_publisher.py"
)
PUBLISHER = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(PUBLISHER)


def png_header(width: int, height: int) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + b"\x00" * 8 + width.to_bytes(4, "big") + height.to_bytes(4, "big")


class WechatDraftPublisherTests(unittest.TestCase):
    def test_preflight_accepts_exactly_five_local_images_and_cover_size(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cover = root / "cover.png"
            cover.write_bytes(png_header(900, 383))
            refs = []
            for index in range(5):
                image = root / f"{index}.png"
                image.write_bytes(png_header(1080, 720))
                refs.append(f"![图{index}]({image.name})")
            article = root / "article.md"
            article.write_text("# 标题\n" + "\n".join(refs), encoding="utf-8")
            _, images = PUBLISHER.validate_preflight(article, cover)
            self.assertEqual(len(images), 5)

            rendered, _ = PUBLISHER.markdown_to_html(article)
            self.assertIn('class="rich_pages wxw-img"', rendered)
            self.assertIn('data-w="1080"', rendered)
            self.assertIn('data-ratio="0.666667"', rendered)

    def test_preflight_rejects_wrong_image_count(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cover = root / "cover.png"
            cover.write_bytes(png_header(900, 383))
            image = root / "one.png"
            image.write_bytes(png_header(1080, 720))
            article = root / "article.md"
            article.write_text("# 标题\n![一张](one.png)", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "exactly 5"):
                PUBLISHER.validate_preflight(article, cover)

    def test_preflight_rejects_wrong_cover_dimensions(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cover = root / "cover.png"
            cover.write_bytes(png_header(900, 400))
            article = root / "article.md"
            article.write_text("# 标题", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "900x383"):
                PUBLISHER.validate_preflight(article, cover)

    def test_verify_draft_requires_reachable_images_and_visible_cover(self) -> None:
        result = {
            "news_item": [
                {
                    "title": "标题",
                    "content": "".join(
                        f'<p><img src="https://mmbiz.qpic.cn/{index}.png"/></p>'
                        for index in range(5)
                    ),
                    "thumb_url": "https://mmbiz.qpic.cn/cover.png",
                    "show_cover_pic": 1,
                }
            ]
        }
        verification = PUBLISHER.verify_draft(
            result,
            expected_title="标题",
            expected_images=5,
            require_show_cover=True,
            image_probe=lambda _url: {
                "status": 200,
                "content_type": "image/png",
                "bytes": 10,
            },
        )
        self.assertTrue(verification["passed"])
        self.assertEqual(verification["inline_image_count"], 5)
        self.assertTrue(verification["show_cover_pic"])

    def test_verify_draft_rejects_hidden_cover(self) -> None:
        result = {
            "news_item": [
                {
                    "title": "标题",
                    "content": "".join(
                        f'<img src="https://mmbiz.qpic.cn/{index}.png"/>'
                        for index in range(5)
                    ),
                    "thumb_url": "https://mmbiz.qpic.cn/cover.png",
                    "show_cover_pic": 0,
                }
            ]
        }
        with self.assertRaisesRegex(RuntimeError, "show_cover_pic=0"):
            PUBLISHER.verify_draft(
                result,
                expected_title="标题",
                expected_images=5,
                require_show_cover=True,
                image_probe=lambda _url: {
                    "status": 200,
                    "content_type": "image/png",
                    "bytes": 10,
                },
            )

    def test_readback_checks_full_text_and_image_order(self) -> None:
        expected = '<p>正文 42</p><img src="https://example.test/1"><img src="https://example.test/2">'
        article = {"title": "标题", "content": expected, "thumb_url": "https://example.test/cover"}
        probe = lambda _: {"status": 200, "content_type": "image/png", "bytes": 10}
        result = {"news_item": [article]}
        verification = PUBLISHER.verify_draft(result, "标题", 2, False, probe, expected_html=expected)
        self.assertTrue(verification["submitted_text_matches"])
        self.assertTrue(verification["submitted_image_order_matches"])
        self.assertFalse(verification["visual_cover_reviewed"])
        article["content"] = expected.replace("正文 42", "正文 43")
        with self.assertRaisesRegex(RuntimeError, "visible text"):
            PUBLISHER.verify_draft(result, "标题", 2, False, probe, expected_html=expected)
        article["content"] = '<p>正文 42</p><img src="https://example.test/2"><img src="https://example.test/1">'
        with self.assertRaisesRegex(RuntimeError, "image references"):
            PUBLISHER.verify_draft(result, "标题", 2, False, probe, expected_html=expected)

    def test_created_draft_id_survives_readback_failure_without_raw_error(self) -> None:
        output = io.StringIO()
        argv = ["publisher", "--article", "article.md", "--cover", "cover.png", "--title", "标题", "--emit-identity"]
        with mock.patch.object(sys, "argv", argv), mock.patch("sys.stdout", output), \
             mock.patch.dict("os.environ", {"WECHAT_APP_ID": "fixture-id", "WECHAT_APP_SECRET": "fixture-secret"}), \
             mock.patch.object(PUBLISHER, "hydrate_wechat_environment"), \
             mock.patch.object(PUBLISHER, "validate_preflight", return_value=("body", [])), \
             mock.patch.object(PUBLISHER, "token", return_value="fixture-token"), \
             mock.patch.object(PUBLISHER, "markdown_to_html", return_value=("body", [])), \
             mock.patch.object(PUBLISHER, "upload_cover", return_value="fixture-cover"), \
             mock.patch.object(PUBLISHER, "add_draft", return_value={"media_id": "fixture-draft"}), \
             mock.patch.object(PUBLISHER, "get_draft", side_effect=RuntimeError("fixture-secret")):
            with self.assertRaises(SystemExit) as error:
                PUBLISHER.main()
        self.assertEqual(error.exception.code, 1)
        identity, payload = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(identity["record_type"], "draft_identity")
        self.assertEqual(identity["media_id"], "fixture-draft")
        self.assertEqual(payload["media_id"], "fixture-draft")
        self.assertFalse(payload["verification"]["passed"])
        self.assertNotIn("fixture-secret", output.getvalue())


if __name__ == "__main__":
    unittest.main()
