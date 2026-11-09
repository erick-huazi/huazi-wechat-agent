#!/usr/bin/env python3
"""Read-only WeChat draft inspection; never records visual approval or publishes."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from PIL import Image

from article_layout import parse_article, render_article
from article_render_integrity import content_shapes, remove_decoration, tree
from distribution_io import five_image_paths, pack_lock
from wechat_credentials import hydrate_wechat_environment
from wechat_draft_publisher import content_snapshot, get_draft, token

MAX_IMAGE_BYTES = 20 * 1024 * 1024
ALLOWED_IMAGE_DOMAINS = ("qpic.cn", "qlogo.cn", "weixin.qq.com")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch_image(url: str) -> bytes:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    if (parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in (None, 443)
            or not any(host == domain or host.endswith("." + domain) for domain in ALLOWED_IMAGE_DOMAINS)):
        raise ValueError("REMOTE_IMAGE_ORIGIN_NOT_ALLOWED")
    with build_opener(NoRedirect).open(Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=30) as response:
        if response.status != 200 or not response.headers.get("Content-Type", "").lower().startswith("image/"):
            raise ValueError("REMOTE_IMAGE_RESPONSE_INVALID")
        raw = response.read(MAX_IMAGE_BYTES + 1)
    if not raw or len(raw) > MAX_IMAGE_BYTES:
        raise ValueError("REMOTE_IMAGE_SIZE_INVALID")
    return raw


def pixels(raw: bytes) -> dict:
    with Image.open(io.BytesIO(raw)) as image:
        if getattr(image, "n_frames", 1) != 1:
            raise ValueError("REMOTE_IMAGE_ANIMATED")
        if image.width * image.height > 20_000_000:
            raise ValueError("REMOTE_IMAGE_DIMENSIONS_INVALID")
        image.load()
        rgba = image.convert("RGBA")
        white = Image.new("RGBA", rgba.size, "white")
        white.alpha_composite(rgba)
        rgb = white.convert("RGB")
        return {"width": image.width, "height": image.height, "format": image.format,
                "sha256": hashlib.sha256(raw).hexdigest(),
                "pixel_sha256": hashlib.sha256(rgb.tobytes()).hexdigest()}


def compare_structure(expected_html: str, actual_html: str) -> dict:
    """Report structural differences without exposing link URLs or claiming platform usability."""
    expected, actual = tree(expected_html), tree(actual_html)
    remove_decoration(expected)
    remove_decoration(actual)
    original, retained = content_shapes(expected), content_shapes(actual)
    return {
        "headings_match": original["headings"] == retained["headings"],
        "links_match_in_order": original["links"] == retained["links"],
        "ordered_list_starts_match": original["ordered_starts"] == retained["ordered_starts"],
        "expected_link_count": len(original["links"]), "retained_link_count": len(retained["links"]),
        "authority": "structural_comparison_only_not_native_clickability",
    }


def inspect_response(pack: Path, result: dict, expected_title: str, fetch=fetch_image) -> dict:
    items = result.get("news_item") or result.get("item") or []
    if len(items) != 1 or not isinstance(items[0], dict):
        raise ValueError("DRAFT_ARTICLE_COUNT_INVALID")
    item = items[0]
    article = pack / "wechat-article.md"
    local_title, _ = parse_article(article.read_text(encoding="utf-8"))
    if local_title != expected_title:
        raise ValueError("LOCAL_TITLE_MISMATCH")
    expected_html, paths = render_article(article)
    if [p.resolve() for p in paths] != [p.resolve() for p in five_image_paths(pack)]:
        raise ValueError("LOCAL_IMAGE_ORDER_INVALID")
    expected_text, _ = content_snapshot(expected_html)
    actual_text, urls = content_snapshot(item.get("content", ""))
    if len(urls) != len(paths):
        raise ValueError("REMOTE_IMAGE_COUNT_MISMATCH")
    checks = []
    for index, (path, url) in enumerate(zip(paths, urls), 1):
        local, remote = pixels(path.read_bytes()), pixels(fetch(url))
        checks.append({"index": index, "image": path.relative_to(pack).as_posix(),
                       "dimensions_match": (local["width"], local["height"]) == (remote["width"], remote["height"]),
                       "pixels_match": local["pixel_sha256"] == remote["pixel_sha256"],
                       "local_sha256": local["sha256"], "remote_sha256": remote["sha256"]})
    cover_url = item.get("thumb_url")
    if not isinstance(cover_url, str) or not cover_url:
        raise ValueError("REMOTE_COVER_MISSING")
    cover = pixels(fetch(cover_url))
    return {
        "title_matches": item.get("title") == expected_title,
        "body_text_matches": actual_text == expected_text,
        "image_count": len(checks), "images": checks,
        "all_images_match_in_order": all(c["dimensions_match"] and c["pixels_match"] for c in checks),
        "structure": compare_structure(expected_html, item.get("content", "")),
        "cover_decoded": True, "cover_dimensions": [cover["width"], cover["height"]],
        "cover_sha256": cover["sha256"], "cover_visual_review": "not_performed",
        "native_ai_setting": "not_exposed_review_before_publication",
        "platform_draft_verified": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pack", required=True, type=Path)
    args = parser.parse_args()
    try:
        pack = args.pack.resolve(strict=True)
        with pack_lock(pack, "wechat-retention-read-only"):
            results_path = pack / "distribution-results.json"
            if not results_path.resolve().is_relative_to(pack):
                raise ValueError("RESULT_PATH_OUTSIDE_PACK")
            results = json.loads(results_path.read_text(encoding="utf-8"))
            rows = [r for r in results.get("items", []) if r.get("platform_id") == "wechat"]
            if len(rows) != 1 or not isinstance(rows[0].get("media_id"), str) or not rows[0]["media_id"]:
                raise ValueError("DRAFT_ID_MISSING")
            title = results.get("title")
            if not isinstance(title, str) or not title:
                raise ValueError("DRAFT_TITLE_MISSING")
            hydrate_wechat_environment()
            access = token(os.environ["WECHAT_APP_ID"], os.environ["WECHAT_APP_SECRET"])
            report = inspect_response(pack, get_draft(access, rows[0]["media_id"]), title)
        report.update(schema_version=1, verified_at=datetime.now().astimezone().isoformat(timespec="seconds"),
                      media_id=rows[0]["media_id"], scope="read_only_no_visual_approval", actual_publication=False)
        print(json.dumps(report, ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({"status": "incomplete", "error_type": type(exc).__name__,
                          "platform_draft_verified": False, "remote_write_attempted": False}))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
