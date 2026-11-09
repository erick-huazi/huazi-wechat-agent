"""Export local article previews, without changing delivery packages."""

import argparse
import base64
import html
import sys
from pathlib import Path

SCRIPT_ROOT = Path(__file__).resolve().parent
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

from article_layout import STYLES, THEMES, atomic_write_bytes, huazi_masthead, huazi_signoff, parse_article, render_article
from distribution_io import atomic_write_json, file_sha256, five_image_paths, pack_lock
from workflow_policy import POLICY_PATH, inspection_platform_ids

FILES = {
    "wechat": "wechat-article.md", "toutiao": "toutiao-article.md",
    "zhihu": "zhihu-article.md", "xiaohongshu": "xiaohongshu.md",
    "weibo": "weibo-article.md",
}
LABELS = ["公众号", "头条", "知乎", "小红书", "微博"]


def data_image(path):
    return "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode("ascii")


def export(pack, output, theme="huazi"):
    pack, output = pack.resolve(), output.resolve()
    project = Path(__file__).resolve().parents[1]
    if output.is_relative_to(pack) or any(output.is_relative_to(project / name) for name in ("outputs", "archive")):
        raise ValueError("LAYOUT_PREVIEW_OUTSIDE_PACK_REQUIRED")
    with pack_lock(pack, "article-layout-preview-read"):
        files = {pid: name for pid, name in FILES.items() if pid in inspection_platform_ids(pack)}
        expected = [p.resolve() for p in five_image_paths(pack)]
        inputs = [pack / name for name in files.values()] + expected
        declaration = pack / "image-count.json"
        declaration_hash = file_sha256(declaration) if declaration.is_file() else None
        hashes = {str(p.relative_to(pack)): file_sha256(p) for p in inputs}
        pages = {}
        themes = {}
        nav = " · ".join(f'<a href="{key}.html">{label}</a>' for key, label in zip(FILES, LABELS) if key in files)
        for platform, name in files.items():
            article = pack / name
            title, _ = parse_article(article.read_text(encoding="utf-8"))
            platform_theme = "huazi-light" if theme == "huazi" and platform != "wechat" else theme
            themes[platform] = platform_theme
            body, images = render_article(article, data_image, keep_title=platform != "xiaohongshu",
                                          theme="plain" if platform == "xiaohongshu" else platform_theme)
            if not title:
                raise ValueError("LAYOUT_MISSING_TITLE")
            if platform == "xiaohongshu":
                if images:
                    raise ValueError("LAYOUT_XHS_EXPECTS_SEPARATE_GALLERY")
                gallery = "".join(f'<img src="{data_image(p)}" alt="配图{i}" style="display:block;width:100%;height:auto;margin:18px 0;">' for i, p in enumerate(expected, 1))
                heading = f'<h1 style="{STYLES["h1"]}">{html.escape(title)}</h1>'
                body = heading + gallery + body
                if platform_theme != "plain":
                    body = huazi_masthead(light=True) + body + huazi_signoff(light=True)
            elif images != expected:
                raise ValueError(f"LAYOUT_IMAGE_ORDER: {name}")
            pages[platform] = (
                '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
                '<meta name="viewport" content="width=device-width,initial-scale=1">'
                f'<title>{html.escape(title)}</title><style>'
                '*{box-sizing:border-box}body{margin:0;background:white;color:#242424;letter-spacing:0;'
                'font-family:"Microsoft YaHei",sans-serif}main{max-width:720px;margin:auto;padding:24px 20px 48px}'
                'nav{font-size:13px;line-height:2;border-bottom:1px solid #d6ddda;padding-bottom:12px;margin-bottom:24px}'
                'nav a{color:#176b62}h1{font-size:26px;line-height:1.5;margin:0 0 24px;overflow-wrap:anywhere}'
                f'</style></head><body><main><nav>{nav}</nav>{body}</main></body></html>'
            ).encode("utf-8")
        if hashes != {str(p.relative_to(pack)): file_sha256(p) for p in inputs} or (file_sha256(declaration) if declaration.is_file() else None) != declaration_hash:
            raise ValueError("LAYOUT_INPUT_CHANGED_DURING_RENDER")
        for platform, content in pages.items():
            atomic_write_bytes(output / f"{platform}.html", content)
        manifest = {
            "schema_version": 1, "status": "local_preview_only", "platform_drafts_modified": False,
            "themes": themes, "platform_native_styling_verified": False,
            "pack": str(pack), "inputs_sha256": hashes,
            "inspection_platforms": list(files),
            "workflow_policy_sha256": file_sha256(POLICY_PATH),
            "plan_sha256": file_sha256(pack / "distribution-plan.json") if (pack / "distribution-plan.json").is_file() else None,
            "renderer_sha256": file_sha256(Path(__file__).with_name("article_layout.py")),
            "preview_builder_sha256": file_sha256(Path(__file__)),
            "previews": {p: {"file": f"{p}.html", "sha256": file_sha256(output / f"{p}.html"), "image_count": len(expected)} for p in pages},
        }
        if declaration_hash is not None:
            manifest["image_count_sha256"] = declaration_hash
        atomic_write_json(output / "layout-preview-manifest.json", manifest)
        return manifest


if __name__ == "__main__":
    cli = argparse.ArgumentParser()
    cli.add_argument("--pack", required=True, type=Path)
    cli.add_argument("--output", required=True, type=Path)
    cli.add_argument("--theme", choices=sorted(THEMES), default="huazi")
    args = cli.parse_args()
    result = export(args.pack, args.output, args.theme)
    print(f"local_preview_only: {len(result['previews'])} platforms; source hashes unchanged")
