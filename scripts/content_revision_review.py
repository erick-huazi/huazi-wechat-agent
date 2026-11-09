"""Review staged editorial changes before writing. Never certify their truth."""

from __future__ import annotations

from collections import Counter
import difflib
import hashlib
import json
from pathlib import Path
import re
from typing import Any

from markdown_it import MarkdownIt

from distribution_io import canonical_sha256, file_sha256, five_image_names


SUFFIXES = {".md", ".txt", ".json"}
COPY_FILES = ("wechat-article.md", "toutiao-article.md", "zhihu-article.md", "xiaohongshu.md", "weibo-article.md", "weibo.md")
CONTEXT_FILES = (*COPY_FILES, "image-storyboard.json", "growth-experiment.json", "claim-verification.json",
                 "content-approval.json", "distribution-plan.json", "topic-ranking.json", "editorial-experiment.json")
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_BATCH_BYTES = 32 * 1024 * 1024
NUMBER = re.compile(r"[0-9０-９]+(?:[.,．，][0-9０-９]+)*(?:\s*(?:%|％|倍|ms|MB|GB|秒|元))?", re.I)
QUOTATION = re.compile(r'“[^”]+”|「[^」]+」|『[^』]+』|‘[^’]+’|"[^"\n]+"')
URL = re.compile(r"https?://[^\s<>\[\]]+", re.I)
DISCLOSURE = re.compile(r"(?:AI|人工智能).{0,12}(?:辅助|生成|参与|标注|声明)|(?:辅助|生成).{0,8}(?:AI|人工智能)", re.I)


class RevisionInputError(ValueError):
    pass


def checked_path(root: Path, relative: str) -> Path:
    candidate = root / relative
    if not candidate.resolve().is_relative_to(root.resolve()):
        raise RevisionInputError(f"REV_PATH_ESCAPE: {relative}")
    for path in [candidate, *candidate.parents]:
        if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
            raise RevisionInputError(f"REV_LINK_NOT_ALLOWED: {relative}")
        if path == root:
            break
    return candidate


def list_staged_files(source: Path) -> list[Path]:
    paths = []
    for path in source.rglob("*"):
        relative = path.relative_to(source).as_posix()
        checked_path(source, relative)
        if any(part.startswith(".") or ":" in part for part in Path(relative).parts):
            raise RevisionInputError(f"REV_HIDDEN_OR_STREAM_PATH: {relative}")
        if path.is_file():
            if path.suffix.lower() not in SUFFIXES:
                raise RevisionInputError(f"REV_UNSUPPORTED_FILE: {relative}")
            paths.append(path)
        if len(paths) > 256:
            raise RevisionInputError("REV_TOO_MANY_FILES")
    return sorted(paths)


def read_bytes(path: Path) -> bytes:
    if path.stat().st_size > MAX_FILE_BYTES:
        raise RevisionInputError(f"REV_FILE_TOO_LARGE: {path.name}")
    value = path.read_bytes()
    if len(value) > MAX_FILE_BYTES:
        raise RevisionInputError(f"REV_FILE_TOO_LARGE: {path.name}")
    return value


def decode(value: bytes) -> str:
    return value.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")


def strict_json(text: str) -> Any:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise RevisionInputError("REV_DUPLICATE_JSON_KEY")
            result[key] = value
        return result

    def nonfinite(_):
        raise RevisionInputError("REV_NONFINITE_JSON")

    return json.loads(text.lstrip("\ufeff"), object_pairs_hook=unique, parse_constant=nonfinite)


def compact(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def markdown_inventory(text: str) -> dict[str, Any]:
    tokens = MarkdownIt("commonmark").enable("table").parse(text.lstrip("\ufeff"))
    blocks, links, images, quotes = [], [], [], []
    quote_depth = 0
    for token in tokens:
        if token.type == "blockquote_open":
            quote_depth += 1
        elif token.type == "blockquote_close":
            quote_depth -= 1
        if token.type != "inline":
            continue
        parts = []
        for child in token.children or []:
            if child.type in {"text", "code_inline"}:
                parts.append(child.content)
                if child.type == "text":
                    links.extend(URL.findall(child.content))
            elif child.type in {"softbreak", "hardbreak"}:
                parts.append(" ")
            elif child.type == "link_open":
                links.append(str(child.attrGet("href") or ""))
            elif child.type == "image":
                images.append({"src": str(child.attrGet("src") or ""), "alt": child.content})
        visible = compact("".join(parts))
        if visible:
            blocks.append({"line": token.map[0] + 1 if token.map else None, "text": visible})
            if len(blocks) > 5000:
                raise RevisionInputError("REV_TOO_MANY_TEXT_BLOCKS")
            quotes.extend(QUOTATION.findall(visible))
            if quote_depth:
                quotes.append(visible)
    return {"blocks": blocks, "links": links, "images": images, "quotes": quotes,
            "numbers": [m.group() for block in blocks for m in NUMBER.finditer(block["text"])],
            "numeric_contexts": [block["text"] for block in blocks if NUMBER.search(block["text"])],
            "disclosures": [block["text"] for block in blocks if DISCLOSURE.search(block["text"])]}


def sample(values: list[Any], limit: int = 12) -> dict[str, Any]:
    return {"items": values[:limit], "total": len(values), "truncated": len(values) > limit}


def changed_blocks(before: list[dict], after: list[dict]) -> list[dict]:
    matcher = difflib.SequenceMatcher(a=[b["text"] for b in before], b=[b["text"] for b in after], autojunk=False)
    return [{"before": sample(before[i:j]), "after": sample(after[k:l])}
            for tag, i, j, k, l in matcher.get_opcodes() if tag != "equal"]


def review_markdown(before: str, after: str) -> list[dict[str, Any]]:
    old, new = markdown_inventory(before), markdown_inventory(after)
    findings = []
    checks = [
        ("numbers", "REV_NUMERIC_VALUES_CHANGED", "核对数值、单位、分母和模型版本；必要更正必须重新核验来源。"),
        ("numeric_contexts", "REV_NUMERIC_CONTEXT_CHANGED", "数字所在语句有变化；即使数值相同，也须检查指标、对象、条件和比较范围。"),
        ("quotes", "REV_QUOTATION_CHANGED", "回到原文核对引文，不把润色后的转述仍标成直接引用。"),
        ("links", "REV_SOURCE_LINK_CHANGED", "核对来源身份和链接目标；新链接不等于新证据。"),
        ("disclosures", "REV_AI_DISCLOSURE_CHANGED", "核对 AI 披露是否仍完整，不为润色删除声明。"),
    ]
    for key, code, action in checks:
        removed = list((Counter(old[key]) - Counter(new[key])).elements())
        added = list((Counter(new[key]) - Counter(old[key])).elements())
        if removed or added:
            findings.append({"code": code, "before": sample(removed), "after": sample(added), "action": action})
    if old["images"] != new["images"]:
        findings.append({"code": "REV_IMAGE_REFERENCE_CHANGED", "before": sample(old["images"]), "after": sample(new["images"]),
                         "action": "核对图片顺序、文件引用和替代说明，重新确认五图与正文对应。"})
    changes = changed_blocks(old["blocks"], new["blocks"])
    if changes:
        findings.append({"code": "REV_PROSE_CHANGED", "blocks": sample(changes),
                         "action": "逐段对照新增、删减和强弱限定；机器没有判断这些修改是否正确或更好。"})
    if before != after and not findings:
        findings.append({"code": "REV_MARKUP_OR_UNPARSED_CHANGED",
                         "action": "字节有变化而上述可见结构未检出差异；仍需检查代码、HTML、标记和渲染，不视为等义证明。"})
    return findings


def json_changes(before: Any, after: Any, path: str = "$", depth: int = 0) -> list[dict]:
    if depth > 24:
        raise RevisionInputError("REV_JSON_TOO_DEEP")
    if not isinstance(before, (dict, list)) and type(before) is type(after) and before == after:
        return []
    if isinstance(before, dict) and isinstance(after, dict):
        rows = []
        for key in sorted(before.keys() | after.keys()):
            location = f"{path}[{json.dumps(key, ensure_ascii=False)}]"
            if key in before and key in after:
                rows.extend(json_changes(before[key], after[key], location, depth + 1))
            else:
                rows.append({"path": location, "before_exists": key in before, "after_exists": key in after,
                             "before": before.get(key), "after": after.get(key)})
        return rows
    if isinstance(before, list) and isinstance(after, list) and len(before) == len(after):
        return [row for i, (left, right) in enumerate(zip(before, after))
                for row in json_changes(left, right, f"{path}[{i}]", depth + 1)]
    return [{"path": path, "before": before, "after": after}]


def review_file(name: str, before: str | None, after: str) -> list[dict]:
    if name.endswith(".json"):
        new = strict_json(after)
        old = strict_json(before) if before is not None else None
        changes = json_changes(old, new)
        if not changes:
            return []
        code, action = {
            "image-storyboard.json": ("REV_STORYBOARD_CHANGED", "检查标签、锚点、人物参考、画面关系与限制；改后重新生成或实际看图，不能沿用旧视觉评分。"),
            "growth-experiment.json": ("REV_EXPERIMENT_CHANGED", "核对唯一变量与对照，不能把新增变化藏进润色；按原规则重跑排名与实验检查。"),
            "editorial-experiment.json": ("REV_EDITORIAL_EXPERIMENT_CHANGED", "核对读者问题、开头对照、冻结输入与预定指标；不得静默更新基线掩盖混变，重新运行创作编排。"),
            "claim-verification.json": ("REV_EVIDENCE_CHANGED", "逐项独立核验来源及指标范围，不因填写 verified 就认为属实。"),
        }.get(name, ("REV_JSON_CHANGED", "检查结构化输入变化；此处没有给予批准或改变发布门禁。"))
        return [{"code": code, "changes": sample(changes, 40), "action": action}]
    if before is None:
        return [{"code": "REV_NEW_CONTENT", "action": "这是新增稿件，不是已核验基线；执行完整事实、配图与内容检查。"}]
    if before == after:
        return []
    return review_markdown(before, after)


def prepare_revision(pack: Path, source: Path) -> tuple[dict[str, Any], dict[str, str]]:
    pack, source = pack.resolve(), source.resolve()
    if not pack.is_dir() or not source.is_dir():
        raise RevisionInputError("REV_DIRECTORIES_REQUIRED")
    if source.is_relative_to(pack) or pack.is_relative_to(source):
        raise RevisionInputError("REV_DISJOINT_STAGING_REQUIRED")
    files = list_staged_files(source)
    if not files:
        raise RevisionInputError("REV_NO_STAGED_FILES")
    rows, staged, total = [], {}, 0
    for path in files:
        name = path.relative_to(source).as_posix()
        destination = checked_path(pack, name)
        if destination.exists() and not destination.is_file():
            raise RevisionInputError(f"REV_DESTINATION_NOT_FILE: {name}")
        raw = read_bytes(path)
        old_raw = read_bytes(destination) if destination.is_file() else None
        total += len(raw) + len(old_raw or b"")
        if total > MAX_BATCH_BYTES:
            raise RevisionInputError("REV_BATCH_TOO_LARGE")
        text = decode(raw)
        staged[name] = text
        rows.append({"path": name,
                     "before_sha256": hashlib.sha256(old_raw).hexdigest() if old_raw is not None else None,
                     "staged_sha256": hashlib.sha256(raw).hexdigest(),
                     "write_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                     "changed": old_raw != text.encode("utf-8"),
                     "findings": review_file(name, decode(old_raw) if old_raw is not None else None, text)})
    context = {}
    for name in (*CONTEXT_FILES, *(f"images/{name}" for name in five_image_names(pack))):
        path = checked_path(pack, name)
        if path.exists() and not path.is_file():
            raise RevisionInputError(f"REV_CONTEXT_NOT_FILE: {name}")
        context[name] = file_sha256(path) if path.is_file() else None
    report = {
        "schema_version": 1, "authority": "revision_diff_only_not_approval",
        "pack": str(pack), "source": str(source), "files": rows, "context_sha256": context,
        "reviewer_code_sha256": file_sha256(Path(__file__)),
        "writer_code_sha256": file_sha256(Path(__file__).with_name("pack_content_writer.py")),
        "requires_human_review": any(row["changed"] for row in rows),
        "fact_verification_performed": False, "visual_review_performed": False, "draft_actions_performed": False,
        "limits": ["No semantic equivalence or factual verdict; unchanged numbers can describe different metrics.",
                   "No pixel inspection. Image labels and visual intent are author-supplied, not visual proof.",
                   "Some excerpts are capped; inspect original files whenever truncated is true.",
                   "A matching review ID binds inputs, not human approval or freshness of sources."]}
    return {**report, "review_id": "revision-" + canonical_sha256(report)[:24]}, staged


def revision_is_current(pack: Path, source: Path, expected: dict[str, Any]) -> bool:
    current, _ = prepare_revision(pack, source)
    return current == expected
