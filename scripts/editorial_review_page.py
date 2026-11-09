"""Export a hash-bound, offline editorial review page; never modifies a draft."""
from __future__ import annotations

import argparse
import base64
import hashlib
import html
import io
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from PIL import Image

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from article_layout import atomic_write_bytes
from content_visual_brief import ROOT, compile_brief, local_path
from distribution_io import atomic_write_json, file_sha256, five_image_names, pack_lock
from source_evidence import checked_span, read_snapshot

PAGE = "editorial-review.html"
MANIFEST = "editorial-review-manifest.json"
ROLES = {"cover": "封面", "news_fact": "新闻事实", "mechanism": "原理", "scenario": "场景", "action_boundary": "行动与边界"}
MODES = {"source_fact": "事实表达", "analogy": "解释类比", "hypothetical": "假设场景", "instruction": "编辑建议"}
CSS = """
*{box-sizing:border-box}html{scroll-behavior:auto}body{margin:0;background:#fff;color:#262b29;
font-family:'Microsoft YaHei',sans-serif;font-size:15px;line-height:1.75;letter-spacing:0}
main{max-width:1120px;margin:auto;padding:24px 24px 64px}header{border-bottom:2px solid #176b62;padding-bottom:20px}
.brand{font-weight:700;color:#176b62;font-size:17px}h1{font-size:26px;line-height:1.5;margin:12px 0;overflow-wrap:anywhere}
h2{font-size:20px;margin:0 0 16px}h3{font-size:16px;margin:0 0 8px}p{margin:0 0 12px;overflow-wrap:anywhere}
nav{display:flex;flex-wrap:wrap;gap:8px 24px;margin-top:16px}a{color:#176b62;text-underline-offset:3px}
.status,.note,dt,figcaption{color:#5a625e;font-size:13px}.status{border-left:3px solid #b34e3f;padding-left:12px}
section{padding:28px 0;border-bottom:1px solid #dce1de}.questions{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:24px}
.questions>div{min-width:0}.label{font-size:12px;font-weight:700;color:#176b62;margin-bottom:6px}
dl{margin:0}dt{margin:12px 0 3px}dd{margin:0;overflow-wrap:anywhere;white-space:pre-wrap}
.image-row{display:grid;grid-template-columns:minmax(0,360px) minmax(0,1fr);gap:32px;border-top:1px solid #dce1de;padding:24px 0}
figure{margin:0;min-width:0}figure img{display:block;width:100%;max-width:360px;height:auto;border:1px solid #dce1de}
figcaption{margin-top:8px}article{min-width:0}.warn{color:#9c382b}code{font-size:12px;overflow-wrap:anywhere;white-space:pre-wrap}
ul{margin:8px 0;padding-left:20px}li{margin-bottom:8px;overflow-wrap:anywhere}details{margin-top:16px}
summary{cursor:pointer;color:#176b62;overflow-wrap:anywhere}.claim{border-left:2px solid #d8e4df;padding:0 0 0 14px;margin:12px 0 20px}
.source-row{padding:12px 0;border-top:1px solid #e1e5e3}.source-row p{margin:2px 0}.empty{color:#79612c}
.quote-context,.article-context{white-space:pre-wrap;overflow-wrap:anywhere;border-left:2px solid #b6ccc5;padding-left:12px;margin:12px 0}
.quote-context mark{background:#edf3ca;color:#262b29}.quote-context .surrounding{color:#5a625e}
@media(max-width:720px){main{padding:16px 16px 40px}h1{font-size:22px}.questions,.image-row{grid-template-columns:minmax(0,1fr);gap:20px}figure img{margin:auto}nav{gap:6px 18px}}
"""


def text(value: Any, fallback: str = "未填写") -> str:
    return value if isinstance(value, str) and value.strip() else fallback


def esc(value: Any, fallback: str = "未填写") -> str:
    return html.escape(text(value, fallback), quote=True)


def rows(value: Any) -> list[dict[str, Any]]:
    return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []


def source_url(value: Any) -> str:
    """Do not put credentials or query-bearing links into this offline review surface."""
    if not isinstance(value, str):
        return "链接未展示"
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment):
            return "链接未展示"
        return value
    except ValueError:
        return "链接未展示"


def read_bound(pack: Path, name: str, hashes: dict[str, Any], *, required: bool = False) -> bytes | None:
    expected = hashes.get(name)
    if expected is None and not required:
        return None
    path = local_path(pack, name)
    if not path.is_file() or path.stat().st_size > 20 * 1024 * 1024:
        raise ValueError("REVIEW_INPUT_MISSING_OR_LARGE")
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != expected:
        raise ValueError("REVIEW_INPUT_CHANGED")
    return data


def object_bound(pack: Path, name: str, hashes: dict[str, Any]) -> dict[str, Any]:
    data = read_bound(pack, name, hashes)
    value = json.loads(data.decode("utf-8-sig")) if data is not None else {}
    if not isinstance(value, dict):
        raise ValueError("REVIEW_OBJECT_REQUIRED")
    return value


def citation_context(pack: Path, verification: dict[str, Any]) -> dict[str, Any]:
    """Expose exact local quotation context, never a factual or semantic verdict."""
    sources = rows(verification.get("sources"))
    source_ids = [source.get("source_id") for source in sources if isinstance(source.get("source_id"), str)]
    documents, files = {}, {}
    for source in sources:
        if "source_snapshot" not in source:
            continue
        reference = source["source_snapshot"]
        document, digest = read_snapshot(pack, reference)
        if document["source_id"] != source.get("source_id") or document["url"] != source.get("url"):
            raise ValueError("SOURCE_SNAPSHOT_IDENTITY_MISMATCH")
        documents[document["source_id"]] = document
        files[reference["path"]] = digest
    claims = []
    for claim in rows(verification.get("claims")):
        spans = claim.get("evidence_spans")
        refs = claim.get("source_ids")
        refs = refs if isinstance(refs, list) else []
        excerpts = []
        if not isinstance(spans, list) or not spans:
            excerpts.append({"issue": "REVIEW_QUOTE_NOT_PROVIDED"})
        else:
            for span in spans:
                sid = span.get("source_id") if isinstance(span, dict) else None
                document = documents.get(sid) if isinstance(sid, str) else None
                if not isinstance(sid, str) or sid not in refs or source_ids.count(sid) != 1:
                    excerpts.append({"issue": "REVIEW_QUOTE_SOURCE_UNRESOLVED"})
                elif document is None:
                    excerpts.append({"issue": "REVIEW_QUOTE_SNAPSHOT_MISSING"})
                elif not checked_span(document["text"], span):
                    excerpts.append({"issue": "REVIEW_QUOTE_SPAN_INVALID"})
                elif len(span["quote"]) > 4000:
                    excerpts.append({"issue": "REVIEW_QUOTE_TOO_LONG_FOR_PREVIEW"})
                else:
                    start, end = span["start"], span["end"]
                    left, right = max(0, start - 180), min(len(document["text"]), end + 180)
                    excerpts.append({"source_id": sid, "start": start, "end": end, "quote": span["quote"],
                                     "before": document["text"][left:start], "after": document["text"][end:right],
                                     "left_clipped": left > 0, "right_clipped": right < len(document["text"]),
                                     "captured_at": document.get("captured_at")})
        covered = {row["source_id"] for row in excerpts if "source_id" in row}
        if any(not isinstance(ref, str) or ref not in covered for ref in refs):
            excerpts.append({"issue": "REVIEW_CITED_SOURCE_NOT_SHOWN"})
        claims.append(excerpts)
    return {"inputs": {"reader_sha256": file_sha256(SCRIPTS / "source_evidence.py"),
                       "files": [{"path": name, "sha256": digest} for name, digest in sorted(files.items())]},
            "claims": claims}


def render_excerpts(excerpts: list[dict[str, Any]]) -> str:
    rendered = []
    for row in excerpts:
        if "issue" in row:
            rendered.append(f'<p class="warn"><code>{esc(row["issue"])}</code> · 原句未展示，需回查。</p>')
            continue
        before = ("…" if row["left_clipped"] else "") + row["before"]
        after = row["after"] + ("…" if row["right_clipped"] else "")
        rendered.append(f'<p class="note">{esc(row["source_id"])} · 字符区间 [{row["start"]}, {row["end"]})'
                        f' · 采集时间 {esc(row["captured_at"])}</p>'
                        '<p class="note">原句与本地快照匹配；上下文为节选，不代表事实、语义或时效已复核。</p>'
                        f'<div class="quote-context"><span class="surrounding">{esc(before, "")}</span>'
                        f'<mark>{esc(row["quote"])}</mark><span class="surrounding">{esc(after, "")}</span></div>')
    return "".join(rendered)


def render_page(brief: dict[str, Any], verification: dict[str, Any], images: list[bytes], citations: dict[str, Any]) -> str:
    card = next(card for card in brief["platform_cards"] if card["platform"] == "wechat")
    problem = brief["editorial_experiment"].get("problem", {})
    problem = problem if isinstance(problem, dict) else {}
    delivery = brief["reader_delivery_review"]
    sections = []
    question_rows = [("新变化", "verified_change", "这次到底新增了什么？"),
                     ("读者问题", "question", "这与读者的哪项具体工作有关？"),
                     ("信息增量", "information_gain", "除公告之外，本文解释了什么？")]
    questions = "".join(f'<div><p class="label">{label}</p><p>{esc(problem.get(key))}</p><p class="note">{prompt}</p></div>'
                        for label, key, prompt in question_rows)
    deliveries = ""
    for role, label in (("answer", "直接答案"), ("action", "可执行动作"), ("boundary", "边界与反例")):
        matches = [row for row in delivery["matches"] if row["role"] == role]
        value = matches[0]["quote"] if matches else problem.get(role)
        state = esc(matches[0]["reference"]) if matches else "计划声明；未定位正文原句"
        deliveries += f'<dt>{label} · {state}</dt><dd>{esc(value)}</dd>'
    sections.append(f'<section id="questions"><h2>问题与交付</h2><div class="questions">{questions}</div><dl>{deliveries}</dl></section>')
    claims = rows(verification.get("claims"))
    sources = rows(verification.get("sources"))
    image_rows = []
    for index, (prompt, data) in enumerate(zip(brief["image_prompts"], images), 1):
        mode = prompt["evidence_review"].get("mode")
        mode_label = MODES.get(mode, "未声明") if isinstance(mode, str) else "未声明"
        explanation = prompt["visual_explanation"]["author_fields"]
        refs = prompt.get("claim_ids")
        refs = refs if isinstance(refs, list) else []
        linked = []
        for ref in refs:
            found = [(position, claim) for position, claim in enumerate(claims) if isinstance(ref, str) and claim.get("claim_id") == ref]
            if len(found) != 1:
                linked.append(f'<p class="warn">主张关联缺失或歧义：{esc(ref)}</p>')
            else:
                position, claim = found[0]
                linked.append(f'<p>{esc(claim.get("text"))}</p><p class="note">{esc(ref)} · 声明状态：{esc(claim.get("verdict"))}</p>'
                              + render_excerpts(citations["claims"][position]))
        linked_html = "".join(linked) or '<p class="empty">没有关联主张；是否需要补证据待复核。</p>'
        context = prompt["article_context_for_review_only"]
        actual_sections = context["platform_sections"].get("wechat", [])
        section_text = (actual_sections[0] if len(actual_sections) == 1 and context["placement"] == "placed"
                        else "图片未唯一定位到匹配正文，需先复核位置。")
        encoded = base64.b64encode(data).decode("ascii")
        image_rows.append(
            f'<div class="image-row" data-image-row="{index}"><figure><img src="data:image/png;base64,{encoded}" '
            f'width="{prompt["width"]}" height="{prompt["height"]}" alt="{index:02d} {ROLES[prompt["role"]]}">'
            f'<figcaption>{index:02d} · {ROLES[prompt["role"]]} · 原图 {prompt["width"]} × {prompt["height"]}</figcaption></figure>'
            f'<article><h3>{esc(prompt["message"])}</h3><p class="label">{mode_label}</p><dl>'
            f'<dt>正文锚点</dt><dd>{esc(prompt["section_anchor"])}</dd>'
            f'<dt>图中文字（分镜声明）</dt><dd>{esc(prompt["on_image_text"], "无字图")}</dd>'
            f'<dt>不能暗示</dt><dd>{esc(explanation.get("avoid_implication"))}</dd></dl>'
            f'<details class="body-context"><summary>图片所在正文</summary><div class="article-context">{esc(section_text)}</div>'
            '<p class="note">读者检查：这幅图增加了什么解释？去掉它是否损失理解？画面有没有暗示正文未支持的结果？</p></details>'
            f'<details><summary>关联主张</summary>{linked_html}</details>'
            '<p class="note">待复核：手机尺寸可读性、人物一致性、构图、信息差异、事实边界。</p></article></div>'
        )
    sections.append('<section id="images"><h2>配图与正文</h2>' + "".join(image_rows) + '</section>')
    source_rows = "".join(
        f'<div class="source-row"><p><strong>{esc(source.get("source_id"))}</strong> · {esc(source.get("title"))}</p>'
        f'<p class="note">类型声明：{esc(source.get("kind"))}</p><p><code>{esc(source_url(source.get("url")))}</code></p></div>'
        for source in sources
    ) or '<p class="empty">没有可展示的来源记录。</p>'
    source_ids = [source.get("source_id") for source in sources if isinstance(source.get("source_id"), str)]
    claim_rows = []
    for position, claim in enumerate(claims):
        ids = claim.get("source_ids")
        ids = ids if isinstance(ids, list) else []
        associations = [f'{ref}（关联唯一）' if source_ids.count(ref) == 1 else f'{ref}（缺失或歧义）'
                        for ref in ids if isinstance(ref, str)]
        claim_rows.append(f'<div class="claim"><p>{esc(claim.get("text"))}</p>'
                          f'<p class="note">{esc(claim.get("claim_id"))} · 声明状态：{esc(claim.get("verdict"))}</p>'
                          f'<p class="note">{esc("；".join(associations), "没有来源关联")}</p>'
                          + render_excerpts(citations["claims"][position]) + '</div>')
    sections.append('<section id="evidence"><h2>来源与主张</h2>'
                    '<p class="note">待复核：来源是否出自同一发布方？结论有哪些限制或反例？主张是否超出了原文条件？</p>'
                    + source_rows + '<details><summary>主张原文与来源关联</summary>' + "".join(claim_rows) + '</details></section>')
    findings = "".join(f'<li><code>{esc(row.get("code"))}</code> · {esc(row.get("reference"))}<br>{esc(row.get("action"))}</li>'
                       for row in brief["issues"])
    sections.append('<section id="findings"><h2>待复核项</h2><ul>' + (findings or '<li>未发现规则提示；不代表质量通过。</li>') + '</ul></section>')
    return ('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; img-src data:; style-src \'unsafe-inline\'; base-uri \'none\'; form-action \'none\'">'
            f'<title>华子社长 · 编辑复核</title><style>{CSS}</style></head><body><main><header>'
            '<div class="brand">华子社长 · 编辑复核</div>'
            f'<h1>{esc(card["current_draft"]["title"])}</h1>'
            '<p class="status">只读快照 · 本页未作事实或视觉验收 · 未执行平台保存或发布</p>'
            '<nav aria-label="复核章节"><a href="#questions">问题与交付</a><a href="#images">配图</a>'
            '<a href="#evidence">来源与主张</a><a href="#findings">待复核项</a></nav></header>'
            + "".join(sections) + '</main></body></html>')


def export(pack: Path, output: Path) -> dict[str, Any]:
    pack, output = pack.resolve(), output.resolve()
    if (not pack.is_dir() or output.is_relative_to(pack) or pack.is_relative_to(output)
            or any(output.is_relative_to(ROOT / name) for name in ("outputs", "archive"))):
        raise ValueError("REVIEW_OUTPUT_OUTSIDE_PACK_REQUIRED")
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise ValueError("REVIEW_FRESH_OUTPUT_REQUIRED")
    with pack_lock(pack, "editorial-review-read"):
        builder_hash = file_sha256(Path(__file__))
        brief = compile_brief(pack)
        hashes = {row["path"].replace("\\", "/"): row["sha256"] for row in brief["inputs"]["files"]}
        verification = object_bound(pack, "claim-verification.json", hashes)
        citations = citation_context(pack, verification)
        images = []
        if len(brief["image_prompts"]) != len(five_image_names(pack)):
            raise ValueError("REVIEW_FIVE_IMAGES_REQUIRED")
        for prompt in brief["image_prompts"]:
            data = read_bound(pack, prompt["image"], hashes, required=True)
            with Image.open(io.BytesIO(data)) as image:
                if (image.format != "PNG" or image.size != (prompt["width"], prompt["height"])
                        or getattr(image, "n_frames", 1) != 1):
                    raise ValueError("REVIEW_IMAGE_INVALID")
                image.load()
            images.append(data)
        content = render_page(brief, verification, images, citations).encode("utf-8")
        if (compile_brief(pack)["brief_id"] != brief["brief_id"] or file_sha256(Path(__file__)) != builder_hash
                or citation_context(pack, verification) != citations):
            raise ValueError("REVIEW_INPUT_CHANGED")
        atomic_write_bytes(output / PAGE, content)
        manifest = {"schema_version": 2, "status": "local_review_only", "pack": str(pack),
                    "created_at": datetime.now(timezone.utc).isoformat(), "brief_id": brief["brief_id"],
                    "builder_sha256": builder_hash, "inputs": brief["inputs"],
                    "source_inputs": citations["inputs"],
                    "page": PAGE, "page_sha256": hashlib.sha256(content).hexdigest(), "image_count": len(images),
                    "fact_review_performed": False, "visual_review_performed": False,
                    "platform_action_performed": False}
        atomic_write_json(output / MANIFEST, manifest)
        return manifest


def verify(output: Path) -> dict[str, Any]:
    output = output.resolve()
    manifest = json.loads((output / MANIFEST).read_text(encoding="utf-8"))
    if (not isinstance(manifest, dict) or type(manifest.get("schema_version")) is not int
            or manifest.get("schema_version") != 2 or manifest.get("status") != "local_review_only"
            or manifest.get("page") != PAGE or type(manifest.get("image_count")) is not int or manifest.get("image_count") < 5
            or any(manifest.get(field) is not False for field in ("fact_review_performed", "visual_review_performed", "platform_action_performed"))):
        raise ValueError("REVIEW_MANIFEST_INVALID")
    page = output / PAGE
    if page.is_symlink() or file_sha256(page) != manifest.get("page_sha256"):
        raise ValueError("REVIEW_PAGE_CHANGED")
    if file_sha256(Path(__file__)) != manifest.get("builder_sha256"):
        raise ValueError("REVIEW_BUILDER_CHANGED")
    pack = Path(manifest["pack"]).resolve()
    with pack_lock(pack, "editorial-review-verify-read"):
        if manifest["image_count"] != len(five_image_names(pack)):
            raise ValueError("REVIEW_INPUT_CHANGED")
        brief = compile_brief(pack)
        if brief["brief_id"] != manifest.get("brief_id") or brief["inputs"] != manifest.get("inputs"):
            raise ValueError("REVIEW_INPUT_CHANGED")
        hashes = {row["path"].replace("\\", "/"): row["sha256"] for row in brief["inputs"]["files"]}
        verification = object_bound(pack, "claim-verification.json", hashes)
        if citation_context(pack, verification)["inputs"] != manifest.get("source_inputs"):
            raise ValueError("REVIEW_SOURCE_INPUT_CHANGED")
    return {"status": "current_local_snapshot_only", "brief_id": brief["brief_id"], "platform_action_performed": False}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pack", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--verify", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.verify and not args.pack and not args.output:
            result = verify(args.verify)
        elif args.pack and args.output and not args.verify:
            result = export(args.pack, args.output)
            result = {key: result[key] for key in ("status", "brief_id", "image_count", "platform_action_performed")}
        else:
            parser.error("Use --pack and --output, or --verify alone")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, KeyError, TypeError, StopIteration) as error:
        print(f"editorial review unavailable: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
