#!/usr/bin/env python3
"""Read-only Markdown-to-WeChat semantic comparison; not platform retention proof."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from lxml import html

from article_layout import article_images, parse_article, parser, render_article
from distribution_io import PackLockError, file_sha256, pack_lock


def tree(fragment: str):
    return html.fragment_fromstring(fragment, create_parent="div")


def remove_decoration(root):
    for node in root.xpath('//*[@data-huazi-decoration="true"]'):
        node.drop_tree()


def normal_text(root):
    return " ".join(root.text_content().split())


def content_shapes(root):
    return {
        "headings": [(n.tag, " ".join(n.text_content().split())) for n in root.xpath(".//h1|.//h2|.//h3|.//h4|.//h5|.//h6")],
        "links": [n.get("href") for n in root.xpath(".//a")],
        "images": [(n.get("src"), n.get("alt", "")) for n in root.xpath(".//img")],
        "ordered_starts": [n.get("start", "1") for n in root.xpath(".//ol")],
    }


def inspect_article(pack: Path) -> dict:
    pack = pack.resolve()
    if not pack.is_dir():
        raise ValueError("PACK_MISSING")
    article = pack / "wechat-article.md"
    with pack_lock(pack, "article-render-integrity"):
        if not article.resolve().is_relative_to(pack) or not article.is_file():
            raise ValueError("ARTICLE_INVALID")
        before = file_sha256(article)
        source = article.read_text(encoding="utf-8")
        title, tokens = parse_article(source)
        if not title:
            raise ValueError("ARTICLE_TITLE_MISSING")
        paths = article_images(tokens, pack)
        images_before = [(str(p.relative_to(pack)), file_sha256(p)) for p in paths]
        md = parser()
        expected = tree(md.renderer.render(tokens, md.options, {}))
        rendered, actual_paths = render_article(article, theme="huazi")
        actual = tree(rendered)
        remove_decoration(actual)
        expected_shape, actual_shape = content_shapes(expected), content_shapes(actual)
        checks = {
            "body_text_whitespace_normalized": normal_text(expected) == normal_text(actual),
            "headings": expected_shape["headings"] == actual_shape["headings"],
            "links_in_order": expected_shape["links"] == actual_shape["links"],
            "image_references_and_alt_in_order": expected_shape["images"] == actual_shape["images"],
            "ordered_list_starts": expected_shape["ordered_starts"] == actual_shape["ordered_starts"],
            "source_unchanged": before == file_sha256(article),
            "image_inputs_unchanged": images_before == [(str(p.relative_to(pack)), file_sha256(p)) for p in paths],
            "resolved_image_paths_match": paths == actual_paths,
        }
    return {"schema_version": 1, "authority": "local_render_integrity_only",
            "article_sha256": before, "image_count": len(paths),
            "image_hashes": [{"path": name, "sha256": digest} for name, digest in images_before],
            "checks": checks, "passed": all(checks.values()),
            "limitations": [
                "Whitespace-normalized text comparison cannot detect all layout or typography changes.",
                "Both views use the project parser; agreement does not certify parser correctness.",
                "Does not verify truth, aesthetics, AI disclosures, mobile rendering or platform retention.",
                "Local preview is not a saved draft or publication receipt.",
            ]}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--pack", required=True)
    args = p.parse_args()
    try:
        result = inspect_article(Path(args.pack))
    except (OSError, ValueError, UnicodeError, PackLockError):
        raise SystemExit("RENDER_INSPECTION_UNAVAILABLE: invalid, changed or locked inputs") from None
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result["passed"] else 2)


if __name__ == "__main__":
    main()
