"""CommonMark structure with platform-safe Huazi editorial typography."""

from __future__ import annotations

import html
import os
import struct
import tempfile
from pathlib import Path
from typing import Callable
from urllib.parse import unquote, urlsplit

from markdown_it import MarkdownIt
from markdown_it.token import Token


BODY_STYLE = (
    "font-family:-apple-system,BlinkMacSystemFont,'Microsoft YaHei',sans-serif;"
    "font-size:16px;line-height:1.85;color:#242424;letter-spacing:0;"
    "text-align:left;overflow-wrap:anywhere;word-break:normal;"
)
STYLES = {
    "p": "margin:0 0 18px;line-height:1.85;text-align:left;",
    "h1": "font-size:24px;line-height:1.45;margin:28px 0 18px;font-weight:700;color:#171717;",
    "h2": "font-size:20px;line-height:1.5;margin:32px 0 16px;font-weight:700;color:#171717;",
    "h3": "font-size:17px;line-height:1.6;margin:24px 0 12px;font-weight:700;color:#171717;",
    "h4": "font-size:16px;line-height:1.6;margin:20px 0 10px;font-weight:700;",
    "h5": "font-size:16px;line-height:1.6;margin:20px 0 10px;font-weight:700;",
    "h6": "font-size:16px;line-height:1.6;margin:20px 0 10px;font-weight:700;",
    "ul": "margin:0 0 18px;padding-left:26px;",
    "ol": "margin:0 0 18px;padding-left:30px;",
    "li": "margin:0 0 8px;line-height:1.85;",
    "blockquote": "margin:20px 0;padding:4px 0 4px 16px;border-left:3px solid #9caaa7;color:#505956;",
    "strong": "font-weight:700;color:#171717;",
    "a": "color:#176b62;text-decoration:underline;overflow-wrap:anywhere;",
    "table": "width:100%;border-collapse:collapse;font-size:14px;line-height:1.65;margin:18px 0;table-layout:fixed;",
    "th": "padding:8px;border:1px solid #d6ddda;text-align:left;background:#edf3f1;font-weight:700;",
    "td": "padding:8px;border:1px solid #d6ddda;text-align:left;vertical-align:top;",
    "hr": "margin:24px 0;border:0;border-top:1px solid #d6ddda;",
}
CODE_STYLE = "font-family:Consolas,monospace;font-size:14px;background:#f2f4f3;white-space:pre-wrap;overflow-wrap:anywhere;"
THEMES = {"plain", "huazi", "huazi-light"}
SOURCE_HEADINGS = {"一手资料", "参考资料", "参考来源", "资料来源", "Sources", "References"}
HUAZI_STYLES = {
    "h2": "font-size:21px;line-height:1.55;margin:0 0 18px;padding-left:12px;border-left:4px solid #ef614f;font-weight:700;color:#122e57;",
    "h3": "font-size:17px;line-height:1.65;margin:26px 0 12px;font-weight:700;color:#172d31;",
    "strong": "font-weight:700;color:#172d31;border-bottom:2px solid #edb5aa;",
    "blockquote": "margin:22px 0;padding:10px 14px;border-left:3px solid #ef614f;background:#f1f6fa;color:#344653;",
    "a": "color:#176b62;text-decoration:none;border-bottom:1px solid #9abdb8;overflow-wrap:anywhere;",
    "hr": "margin:28px 0;border:0;border-top:1px solid #bbcfcb;",
}


def ornament(text: str, style: str = "") -> str:
    return (f'<span data-huazi-decoration="true" aria-hidden="true"'
            f' style="{style}">{html.escape(text)}</span>')


def huazi_masthead(*, light: bool = False) -> str:
    seal = "" if light else ornament(
        "华", "display:inline-block;width:34px;height:34px;line-height:34px;text-align:center;"
        "background:#124cc2;color:white;font-size:20px;font-weight:700;vertical-align:middle;margin-right:10px;border-radius:3px;"
    )
    return (
        '<section data-huazi-decoration="true" aria-label="华子社长栏目"'
        ' style="margin:0 0 22px;padding:0 0 12px;border-bottom:2px solid #124cc2;letter-spacing:0;">'
        f'{seal}<span style="font-size:20px;font-weight:700;color:#172d31;vertical-align:middle;">华子社长</span>'
        '<span style="display:block;margin-top:6px;font-size:12px;line-height:1.8;color:#586969;">'
        'AI 新闻 · 知识拆解'
        '<span style="color:#bf4b36;padding:0 8px;">/</span>华子观察簿</span></section>'
    )


def huazi_signoff(*, light: bool = False) -> str:
    motto = "" if light else '<span style="display:block;margin-top:6px;font-size:13px;color:#586969;">看见变化，弄懂原理。</span>'
    return (
        '<section data-huazi-decoration="true" aria-label="栏目署名"'
        ' style="margin-top:32px;padding-top:18px;border-top:1px solid #bdceca;text-align:left;">'
        '<span style="display:inline-block;width:18px;height:4px;background:#176b62;vertical-align:middle;margin-right:4px;"></span>'
        '<span style="display:inline-block;width:8px;height:4px;background:#d56550;vertical-align:middle;margin-right:10px;"></span>'
        f'<span style="font-size:15px;font-weight:700;color:#172d31;">华子社长</span>{motto}</section>'
    )


def parser() -> MarkdownIt:
    # No smart punctuation, auto-spacing or raw HTML execution in verified copy.
    return MarkdownIt("commonmark", {"html": False, "typographer": False}).enable("table")


def inline_text(tokens: list[Token]) -> str:
    return "".join(
        "\n" if token.type in {"softbreak", "hardbreak"} else
        token.content if token.type in {"text", "code_inline"} else ""
        for token in tokens
    )


def parse_article(source: str) -> tuple[str, list[Token]]:
    tokens = parser().parse(source.lstrip("\ufeff"))
    title = ""
    if len(tokens) >= 3 and tokens[0].type == "heading_open" and tokens[0].tag == "h1":
        title = inline_text(tokens[1].children or [])
        tokens = tokens[3:]
    return title, tokens


def local_image(base: Path, reference: str) -> Path:
    url = urlsplit(reference)
    if url.scheme or url.netloc or url.query or url.fragment:
        raise ValueError(f"LAYOUT_EXTERNAL_IMAGE: {reference}")
    path = (base / unquote(url.path)).resolve()
    if not path.is_relative_to(base.resolve()):
        raise ValueError(f"LAYOUT_IMAGE_OUTSIDE_PACK: {reference}")
    if not path.is_file():
        raise FileNotFoundError(path)
    return path


def article_images(tokens: list[Token], base: Path) -> list[Path]:
    return [
        local_image(base, child.attrGet("src") or "")
        for token in tokens for child in token.children or [] if child.type == "image"
    ]


def safe_link(url: str) -> str:
    parsed = urlsplit(url)
    if not (parsed.scheme in {"http", "https"} and parsed.netloc) and not url.startswith("#"):
        raise ValueError(f"LAYOUT_UNSUPPORTED_LINK: {url}")
    return url


def atomic_write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as handle:
            temp_path = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path and temp_path.exists():
            temp_path.unlink()


def render_article(
    article: Path,
    image_url: Callable[[Path], str] | None = None,
    *,
    keep_title: bool = False,
    theme: str = "huazi",
) -> tuple[str, list[Path]]:
    if theme not in THEMES:
        raise ValueError(f"LAYOUT_UNKNOWN_THEME: {theme}")
    branded = theme != "plain"
    light = theme == "huazi-light"
    title, tokens = parse_article(article.read_text(encoding="utf-8"))
    # Validate every local reference before an uploader can have any side effect.
    images = article_images(tokens, article.parent)
    md = parser()

    def decorate(items: list[Token]) -> None:
        for item in items:
            if item.tag in STYLES and (item.nesting == 1 or item.type == "hr"):
                existing = item.attrGet("style") or ""
                style = HUAZI_STYLES.get(item.tag, STYLES[item.tag]) if branded else STYLES[item.tag]
                item.attrSet("style", style + existing + "letter-spacing:0;")
            if item.type == "link_open":
                safe_link(item.attrGet("href") or "")
            if item.children:
                decorate(item.children)

    decorate(tokens)
    source_level = None
    in_sources = False
    # Only an explicit, top-level source heading establishes a bibliography.
    for index, token in enumerate(tokens):
        if token.type == "heading_open" and token.level == 0:
            level = int(token.tag[1:])
            if source_level is not None and level <= source_level:
                source_level = None
                in_sources = False
            if index + 1 < len(tokens) and inline_text(tokens[index + 1].children or []).strip() in SOURCE_HEADINGS:
                source_level, in_sources = level, True
                token.meta["source_heading"] = True
        if branded and in_sources:
            if token.type in {"bullet_list_open", "ordered_list_open"} and token.level == 0:
                token.attrSet("data-huazi-sources", "true")
                style = "margin:0 0 20px;line-height:1.7;"
                style += "padding-left:0;list-style:none;" if token.tag == "ul" else "padding-left:26px;"
                token.attrSet("style", style)
            elif token.type == "list_item_open" and token.level == 1:
                token.attrSet("style", "margin:0;padding:10px 0;border-bottom:1px solid #e0e8e5;line-height:1.7;")
    if branded:
        # The cover is commonly an image-only paragraph before the actual lead.
        for index, token in enumerate(tokens):
            if token.type == "heading_open" and token.level == 0:
                break
            if token.type != "paragraph_open" or token.level != 0:
                continue
            children = tokens[index + 1].children or []
            if any(child.type == "image" for child in children):
                continue
            if inline_text(children).strip():
                lead_style = "color:#283f42;" if light else "padding:10px 12px;border-left:3px solid #124cc2;background:#f1f6fa;color:#263b4e;"
                token.attrSet("style", STYLES["p"] + lead_style)
                break
    section_index = 0
    image_index = 0
    link_targets: list[str] = []

    def render_heading(items, index, options, env):
        nonlocal section_index
        token = items[index]
        if branded and (light or token.meta.get("source_heading")):
            token.attrSet("style", STYLES[token.tag] + "color:#172d31;")
        opening = md.renderer.renderToken(items, index, options, env)
        if not branded:
            return opening
        if token.meta.get("source_heading"):
            return opening + ornament("↗", "color:#176b62;font-weight:400;margin-right:8px;")
        if token.tag == "h2":
            section_index += 1
            if light:
                return opening + ornament(f"{section_index:02d}", "font-size:14px;color:#176b62;margin-right:9px;vertical-align:baseline;")
            kicker = ornament(
                f"{section_index:02d} / 华子拆解",
                "display:block;font-size:12px;line-height:1.8;color:#124cc2;"
                "margin:34px 0 8px;font-weight:700;letter-spacing:0;"
            )
            return kicker + opening
        return opening + ("" if light else ornament("↳", "color:#bf4b36;font-weight:400;margin-right:8px;"))

    def render_link_open(items, index, options, env):
        link_targets.append(items[index].attrGet("href") or "")
        return md.renderer.renderToken(items, index, options, env)

    def render_link_close(items, index, options, env):
        target = link_targets.pop() if link_targets else ""
        external = urlsplit(target).scheme in {"http", "https"}
        marker = ornament("↗", "font-size:12px;color:#176b62;margin-left:3px;white-space:nowrap;") if branded and external else ""
        return marker + md.renderer.renderToken(items, index, options, env)

    def render_image(items, index, options, env):
        nonlocal image_index
        image_index += 1
        item = items[index]
        reference = item.attrGet("src") or ""
        path = local_image(article.parent, reference)
        src = image_url(path) if image_url else reference
        with path.open("rb") as stream:
            header = stream.read(24)
        dimensions = ""
        if header.startswith(b"\x89PNG\r\n\x1a\n") and len(header) == 24:
            width, height = struct.unpack(">II", header[16:24])
            if width and height:
                dimensions = f' width="{width}" height="{height}" data-w="{width}" data-ratio="{height / width:.6f}"'
        src = html.escape(src, quote=True)
        caption = ornament(
            f"图 {image_index:02d}" if light else f"◇ 华子图解 / {image_index:02d}",
            "display:block;margin:0 0 20px;font-size:12px;line-height:1.8;color:#526978;"
        ) if branded else ""
        margin = "16px auto 8px" if branded else "18px auto 22px"
        return (
            f'<img class="rich_pages wxw-img" src="{src}" data-src="{src}"'
            f' data-type="{html.escape(path.suffix[1:])}" data-s="300,640"{dimensions}'
            f' alt="{html.escape(item.content, quote=True)}"'
            f' style="display:block;width:100%;max-width:100%;height:auto;margin:{margin};" />' + caption
        )

    def render_code(items, index, options, env):
        token = items[index]
        code = f'<code style="{CODE_STYLE}">{html.escape(token.content)}</code>'
        return code if token.type == "code_inline" else f'<pre style="margin:18px 0;padding:12px;{CODE_STYLE}">{code}</pre>\n'

    md.renderer.rules["image"] = render_image
    md.renderer.rules["heading_open"] = render_heading
    md.renderer.rules["link_open"] = render_link_open
    md.renderer.rules["link_close"] = render_link_close
    for kind in ("code_inline", "code_block", "fence"):
        md.renderer.rules[kind] = render_code
    body = md.renderer.render(tokens, md.options, {})
    if keep_title and title:
        body = f'<h1 style="{STYLES["h1"]}">{html.escape(title)}</h1>\n' + body
    if branded:
        body = huazi_masthead(light=light) + body + huazi_signoff(light=light)
    return f'<section style="{BODY_STYLE}">\n{body}</section>', images
