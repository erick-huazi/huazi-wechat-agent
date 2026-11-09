from __future__ import annotations

import argparse
import html
import json
import mimetypes
import os
import re
import sys
from pathlib import Path
from urllib import parse, request


API_BASE = "https://mp.toutiao.com"
PUBLISH_URL = f"{API_BASE}/mp/agw/article/publish?source=mp&type=article"
UPLOAD_URL = (
    f"{API_BASE}/mp/agw/article_material/photo/upload_picture"
    "?type=ueditor&pgc_watermark=1&action=uploadimage&encode=utf-8"
)
MEDIA_INFO_URL = f"{API_BASE}/mp/agw/media/get_media_info"


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def main() -> int:
    load_dotenv(Path.cwd() / ".env")

    parser = argparse.ArgumentParser(
        description="Create a Toutiao article draft from a Markdown file using a browser cookie."
    )
    parser.add_argument("--article", required=True, help="Markdown article path.")
    parser.add_argument("--title", required=True, help="Toutiao article title.")
    parser.add_argument("--cover", default="", help="Optional local cover image path.")
    parser.add_argument("--cookie", default=os.getenv("TOUTIAO_COOKIE", ""), help="Toutiao cookie string.")
    parser.add_argument("--dry-run", action="store_true", help="Render payload without uploading or publishing.")
    parser.add_argument(
        "--output",
        default="",
        help="Optional path to write dry-run payload or publish result JSON.",
    )
    args = parser.parse_args()

    article_path = Path(args.article).resolve()
    markdown = article_path.read_text(encoding="utf-8")
    cover_path = Path(args.cover).resolve() if args.cover else None

    if args.dry_run:
        content = markdown_to_toutiao_html(markdown, article_path.parent, image_uploader=None)
        payload = build_publish_payload(args.title, content, cover=None)
        write_result(payload, args.output)
        return 0

    if not args.cookie:
        raise SystemExit("Set TOUTIAO_COOKIE or pass --cookie before publishing.")

    client = ToutiaoClient(args.cookie)
    media = get_media_info(client)

    uploaded_cover = None
    if cover_path:
        uploaded_cover = upload_image(client, cover_path)

    def upload_inline(path: Path) -> dict:
        return upload_image(client, path)

    content = markdown_to_toutiao_html(markdown, article_path.parent, image_uploader=upload_inline)
    payload = build_publish_payload(args.title, content, cover=uploaded_cover)
    result = post_article(client, payload)

    out = {
        "status": "success",
        "media": media,
        "result": result,
        "draft_link": build_draft_link(result),
    }
    write_result(out, args.output)
    return 0


def load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


class ToutiaoClient:
    def __init__(self, cookie: str):
        self.cookie = cookie

    def request(self, url: str, method: str = "GET", data: bytes | None = None, headers: dict | None = None) -> str:
        merged = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0 Safari/537.36"
            ),
            "Referer": "https://mp.toutiao.com/profile_v4/graphic/publish",
            "Cookie": self.cookie,
        }
        if headers:
            merged.update(headers)
        req = request.Request(url, data=data, headers=merged, method=method)
        with request.urlopen(req, timeout=60) as resp:
            return resp.read().decode("utf-8", errors="replace")

    def get_json(self, url: str) -> dict:
        return parse_json_text(self.request(url))

    def post_form(self, url: str, data: dict) -> dict:
        body = parse.urlencode(data).encode("utf-8")
        text = self.request(
            url,
            method="POST",
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"},
        )
        return parse_json_text(text)

    def post_multipart(self, url: str, field: str, path: Path, mime: str) -> dict:
        boundary = "----CodexToutiaoBoundary7MA4YWxkTrZu0gW"
        file_bytes = path.read_bytes()
        head = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{field}"; filename="{path.name}"\r\n'
            f"Content-Type: {mime}\r\n\r\n"
        ).encode("utf-8")
        tail = f"\r\n--{boundary}--\r\n".encode("utf-8")
        body = head + file_bytes + tail
        text = self.request(
            url,
            method="POST",
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        return parse_json_text(text)


def get_media_info(client: ToutiaoClient) -> dict:
    data = client.get_json(MEDIA_INFO_URL)
    if not data.get("data"):
        raise RuntimeError(f"Toutiao login check failed: {data}")
    user = data["data"].get("user", {})
    return {
        "uid": user.get("id"),
        "name": user.get("screen_name"),
    }


def upload_image(client: ToutiaoClient, path: Path) -> dict:
    if not path.is_file():
        raise FileNotFoundError(path)
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    data = client.post_multipart(UPLOAD_URL, "upfile", path, mime)
    if data.get("state") != "SUCCESS":
        raise RuntimeError(f"Toutiao image upload failed for {path}: {data}")
    return data


def parse_json_text(text: str) -> dict:
    text = text.strip()
    try:
        return json.loads(text)
    except ValueError:
        match = re.search(r"\{.*\}", text, flags=re.S)
        if not match:
            raise RuntimeError(f"Expected JSON, got: {text[:500]}")
        return json.loads(match.group(0))


def build_publish_payload(title: str, content: str, cover: dict | None) -> dict:
    covers = []
    if cover:
        origin_uri = cover.get("origin_web_uri") or cover.get("uri") or cover.get("original", "")
        covers.append(
            {
                "id": 0,
                "url": cover.get("url", ""),
                "uri": origin_uri,
                "origin_uri": origin_uri,
                "ic_uri": "",
                "thumb_width": cover.get("width") or cover.get("img_width") or 900,
                "thumb_height": cover.get("height") or cover.get("img_height") or 383,
            }
        )

    return {
        "title": title,
        "article_ad_type": 2,
        "article_type": 0,
        "from_diagnosis": 0,
        "origin_debut_check_pgc_normal": 0,
        "tree_plan_article": 0,
        "save": 0,
        "pgc_id": 0,
        "content": content,
        "pgc_feed_covers": json.dumps(covers, ensure_ascii=False),
    }


def post_article(client: ToutiaoClient, payload: dict) -> dict:
    data = client.post_form(PUBLISH_URL, payload)
    if not data.get("data"):
        raise RuntimeError(f"Toutiao draft creation failed: {data}")
    return data


def build_draft_link(result: dict) -> str:
    pgc_id = (result.get("data") or {}).get("pgc_id")
    if not pgc_id:
        return ""
    return f"https://mp.toutiao.com/profile_v4/graphic/publish?pgc_id={pgc_id}"


def markdown_to_toutiao_html(markdown: str, base_dir: Path, image_uploader) -> str:
    lines = markdown.splitlines()
    blocks: list[str] = []
    paragraph: list[str] = []
    in_list = False

    def flush_paragraph() -> None:
        nonlocal paragraph
        if paragraph:
            text = " ".join(item.strip() for item in paragraph if item.strip())
            if text:
                blocks.append(f"<p>{inline_markup(text)}</p>")
            paragraph = []

    def close_list() -> None:
        nonlocal in_list
        if in_list:
            blocks.append("</ul>")
            in_list = False

    for raw in lines:
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped:
            flush_paragraph()
            close_list()
            continue

        image_match = re.match(r"!\[(.*?)\]\((.*?)\)", stripped)
        if image_match:
            flush_paragraph()
            close_list()
            alt, src = image_match.groups()
            img_src, attrs = resolve_image(src, base_dir, image_uploader)
            attr_text = " ".join(f'{key}="{html.escape(str(value))}"' for key, value in attrs.items() if value)
            if attr_text:
                attr_text = " " + attr_text
            blocks.append(f'<p><img src="{html.escape(img_src)}" alt="{html.escape(alt)}"{attr_text}></p>')
            continue

        heading = re.match(r"^(#{1,3})\s+(.+)$", stripped)
        if heading:
            flush_paragraph()
            close_list()
            level = min(len(heading.group(1)), 3)
            blocks.append(f"<h{level}>{inline_markup(heading.group(2))}</h{level}>")
            continue

        bullet = re.match(r"^[-*]\s+(.+)$", stripped)
        if bullet:
            flush_paragraph()
            if not in_list:
                blocks.append("<ul>")
                in_list = True
            blocks.append(f"<li>{inline_markup(bullet.group(1))}</li>")
            continue

        paragraph.append(stripped)

    flush_paragraph()
    close_list()
    return "\n".join(blocks)


def resolve_image(src: str, base_dir: Path, image_uploader) -> tuple[str, dict]:
    if re.match(r"^https?://", src):
        return src, {}
    path = Path(src)
    if not path.is_absolute():
        path = (base_dir / path).resolve()
    if image_uploader is None:
        return path.as_posix(), {}
    uploaded = image_uploader(path)
    attrs = {}
    origin_uri = uploaded.get("origin_web_uri") or uploaded.get("uri") or uploaded.get("original")
    if origin_uri:
        attrs["web_uri"] = origin_uri
    if uploaded.get("width"):
        attrs["img_width"] = uploaded["width"]
        attrs["data-width"] = uploaded["width"]
    if uploaded.get("height"):
        attrs["img_height"] = uploaded["height"]
        attrs["data-height"] = uploaded["height"]
    return uploaded.get("url", ""), attrs


def inline_markup(text: str) -> str:
    escaped = html.escape(text)
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)
    escaped = re.sub(r"`([^`]+)`", r"<code>\1</code>", escaped)
    return escaped


def write_result(data: dict, output: str) -> None:
    text = json.dumps(data, ensure_ascii=False, indent=2)
    if output:
        Path(output).write_text(text + "\n", encoding="utf-8")
    print(text)


if __name__ == "__main__":
    raise SystemExit(main())
