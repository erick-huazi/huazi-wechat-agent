#!/usr/bin/env python3
"""Upload a Markdown article with local images to a WeChat Official Account draft."""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import struct
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from html.parser import HTMLParser

SCRIPT_ROOT = Path(__file__).resolve().parent
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

from wechat_credentials import hydrate_wechat_environment
from article_layout import parser as article_parser, render_article


API = "https://api.weixin.qq.com/cgi-bin"


def request_json(url: str, data: bytes | None = None, headers: dict | None = None) -> dict:
    req = urllib.request.Request(url, data=data, headers=headers or {})
    with urllib.request.urlopen(req, timeout=60) as resp:
        raw = resp.read().decode("utf-8")
    try:
        result = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(raw) from exc
    if result.get("errcode") not in (None, 0):
        raise RuntimeError(json.dumps(result, ensure_ascii=False))
    return result


def token(app_id: str, app_secret: str) -> str:
    q = urllib.parse.urlencode({
        "grant_type": "client_credential",
        "appid": app_id,
        "secret": app_secret,
    })
    return request_json(f"{API}/token?{q}")["access_token"]


def multipart(field: str, path: Path) -> tuple[bytes, str]:
    boundary = "----codexwechatboundary"
    ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    body = []
    body.append(f"--{boundary}\r\n".encode())
    body.append(
        f'Content-Disposition: form-data; name="{field}"; filename="{path.name}"\r\n'
        f"Content-Type: {ctype}\r\n\r\n".encode()
    )
    body.append(path.read_bytes())
    body.append(f"\r\n--{boundary}--\r\n".encode())
    return b"".join(body), boundary


def upload_inline_image(access_token: str, path: Path) -> str:
    data, boundary = multipart("media", path)
    url = f"{API}/media/uploadimg?access_token={access_token}"
    result = request_json(url, data=data, headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    return result["url"]


def upload_cover(access_token: str, path: Path) -> str:
    data, boundary = multipart("media", path)
    url = f"{API}/material/add_material?access_token={access_token}&type=thumb"
    result = request_json(url, data=data, headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    return result["media_id"]


def md_inline(text: str) -> str:
    return article_parser().renderInline(text)


def markdown_to_html(article_path: Path, access_token: str | None = None) -> tuple[str, list[Path]]:
    uploader = (lambda path: upload_inline_image(access_token, path)) if access_token else None
    return render_article(article_path, uploader)


def add_draft(access_token: str, payload: dict) -> dict:
    url = f"{API}/draft/add?access_token={access_token}"
    return request_json(url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), headers={"Content-Type": "application/json"})


def update_draft(access_token: str, media_id: str, article: dict) -> dict:
    url = f"{API}/draft/update?access_token={access_token}"
    payload = {"media_id": media_id, "index": 0, "articles": article}
    return request_json(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )


def get_draft(access_token: str, media_id: str) -> dict:
    url = f"{API}/draft/get?access_token={access_token}"
    return request_json(
        url,
        data=json.dumps({"media_id": media_id}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )


def probe_remote_image(url: str) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=60) as response:
        payload = response.read()
        return {
            "status": response.status,
            "content_type": response.headers.get("Content-Type", ""),
            "bytes": len(payload),
        }


class VisibleContent(HTMLParser):
    """Compare text and ordered image references without trusting HTML styling."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.text = []
        self.images = []
        self.hidden_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden_depth += 1
        if tag == "img" and not self.hidden_depth:
            self.images.append(dict(attrs).get("src", ""))

    def handle_endtag(self, tag):
        if tag in {"script", "style"} and self.hidden_depth:
            self.hidden_depth -= 1

    def handle_data(self, data):
        if not self.hidden_depth:
            self.text.append(data)


def content_snapshot(value: str) -> tuple[str, list[str]]:
    parsed = VisibleContent()
    parsed.feed(value)
    return "".join("".join(parsed.text).split()), parsed.images


def verify_draft(
    result: dict,
    expected_title: str,
    expected_images: int,
    require_show_cover: bool,
    image_probe=probe_remote_image,
    expected_html: str | None = None,
) -> dict:
    items = result.get("news_item") or result.get("item") or []
    if len(items) != 1:
        raise RuntimeError(f"draft/get returned {len(items)} articles, expected 1")

    article = items[0]
    if article.get("title") != expected_title:
        raise RuntimeError("draft/get title does not match the requested title")

    content = article.get("content", "")
    observed_text, image_urls = content_snapshot(content)
    if len(image_urls) != expected_images:
        raise RuntimeError(
            f"draft/get returned {len(image_urls)} inline images, expected {expected_images}"
        )

    if expected_html is not None:
        expected_text, expected_urls = content_snapshot(expected_html)
        if observed_text != expected_text:
            raise RuntimeError("draft/get visible text does not match submitted content")
        if image_urls != expected_urls:
            raise RuntimeError("draft/get inline image references differ from submitted order")

    image_checks = [image_probe(url) for url in image_urls]
    for index, check in enumerate(image_checks, start=1):
        content_type = str(check.get("content_type", "")).lower()
        if check.get("status") != 200 or not content_type.startswith("image/") or int(check.get("bytes", 0)) <= 0:
            raise RuntimeError(f"inline image {index} is not reachable")

    thumb_present = bool(article.get("thumb_url"))
    if not thumb_present:
        raise RuntimeError("draft/get did not return a cover URL")
    thumb_check = image_probe(article["thumb_url"])
    thumb_content_type = str(thumb_check.get("content_type", "")).lower()
    if (
        thumb_check.get("status") != 200
        or not thumb_content_type.startswith("image/")
        or int(thumb_check.get("bytes", 0)) <= 0
    ):
        raise RuntimeError("draft cover image is not reachable")

    show_cover = int(article.get("show_cover_pic") or 0) == 1
    if require_show_cover and not show_cover:
        raise RuntimeError("draft/get reports show_cover_pic=0")

    return {
        "passed": True,
        "article_count": len(items),
        "inline_image_count": len(image_urls),
        "inline_images_reachable": True,
        "image_checks": image_checks,
        "thumb_url_present": thumb_present,
        "thumb_reachable": True,
        "thumb_check": thumb_check,
        "show_cover_pic": show_cover,
        "submitted_text_matches": True if expected_html is not None else None,
        "submitted_image_order_matches": True if expected_html is not None else None,
        "visual_cover_reviewed": False,
    }


def png_size(path: Path) -> tuple[int, int] | None:
    raw = path.read_bytes()[:24]
    if len(raw) == 24 and raw[:8] == b"\x89PNG\r\n\x1a\n":
        return struct.unpack(">II", raw[16:24])
    return None


def validate_preflight(
    article: Path,
    cover: Path,
    expected_images: int = 5,
    cover_size: tuple[int, int] = (900, 383),
) -> tuple[str, list[Path]]:
    if not article.is_file():
        raise FileNotFoundError(f"article not found: {article}")
    if not cover.is_file():
        raise FileNotFoundError(f"cover not found: {cover}")
    actual_cover_size = png_size(cover)
    if actual_cover_size != cover_size:
        raise ValueError(f"cover must be PNG {cover_size[0]}x{cover_size[1]}, found {actual_cover_size}")
    content, images = markdown_to_html(article, None)
    if len(images) != expected_images:
        raise ValueError(f"article must reference exactly {expected_images} local images, found {len(images)}")
    return content, images


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--article", required=True)
    parser.add_argument("--cover", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--author", default="")
    parser.add_argument("--digest", default="")
    parser.add_argument("--source-url", default="")
    parser.add_argument("--show-cover", action="store_true")
    parser.add_argument("--update-media-id", default="")
    parser.add_argument("--expected-images", type=int, default=5)
    parser.add_argument("--cover-width", type=int, default=900)
    parser.add_argument("--cover-height", type=int, default=383)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--emit-identity", action="store_true", help="Emit the draft ID immediately before readback")
    args = parser.parse_args()

    article = Path(args.article).resolve()
    cover = Path(args.cover).resolve()
    try:
        preflight_content, preflight_images = validate_preflight(
            article,
            cover,
            args.expected_images,
            (args.cover_width, args.cover_height),
        )
    except (FileNotFoundError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc

    if args.dry_run:
        print(json.dumps({
            "title": args.title,
            "author": args.author,
            "digest": args.digest,
            "cover": str(cover),
            "images": [str(p) for p in preflight_images],
            "content_preview": preflight_content[:1000],
        }, ensure_ascii=False, indent=2))
        return

    hydrate_wechat_environment()
    app_id = os.environ.get("WECHAT_APP_ID")
    app_secret = os.environ.get("WECHAT_APP_SECRET")
    if not app_id or not app_secret:
        raise SystemExit(
            "WeChat credentials are required in the process environment or Windows Credential Manager"
        )
    access_token = token(app_id, app_secret)
    content, _ = markdown_to_html(article, access_token)
    thumb_media_id = upload_cover(access_token, cover)
    article_payload = {
        "article_type": "news",
        "title": args.title,
        "author": args.author,
        "digest": args.digest,
        "content": content,
        "content_source_url": args.source_url,
        "thumb_media_id": thumb_media_id,
        "need_open_comment": 0,
        "only_fans_can_comment": 0,
        "show_cover_pic": 1 if args.show_cover else 0,
    }
    if args.update_media_id:
        update_draft(access_token, args.update_media_id, article_payload)
        media_id = args.update_media_id
        operation = "updated"
    else:
        response = add_draft(access_token, {"articles": [article_payload]})
        media_id = response["media_id"]
        operation = "created"

    if args.emit_identity:
        print(json.dumps({"record_type": "draft_identity", "media_id": media_id, "operation": operation}, ensure_ascii=False), flush=True)

    try:
        verification = verify_draft(
            get_draft(access_token, media_id),
            expected_title=args.title,
            expected_images=args.expected_images,
            require_show_cover=args.show_cover,
            expected_html=content,
        )
        failure_type = None
    except Exception as exc:
        # The draft already exists: retain its ID without printing credentials or raw API errors.
        verification = {"passed": False}
        failure_type = type(exc).__name__
    print(
        json.dumps(
            {
                "media_id": media_id,
                "operation": operation,
                "verification": {k: v for k, v in verification.items() if k not in {"image_checks", "thumb_check"}},
                "verification_error_type": failure_type,
            },
            ensure_ascii=False,
            indent=None if args.emit_identity else 2,
        )
    )
    if failure_type is not None:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
