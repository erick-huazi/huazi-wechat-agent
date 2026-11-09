"""Local, non-rewriting prose advice. Findings are not truth or quality verdicts."""

from __future__ import annotations

import re
from typing import Any

from markdown_it import MarkdownIt


SOURCE_HEADING = re.compile(r"^(?:来源(?:与说明)?|一手资料|一手来源|参考资料|参考来源|参考文献|资料来源|Sources?(?: and notes)?|References?)(?:\s*[:：].*)?$", re.I)
DISCLOSURE = re.compile(r"(?:AI|人工智能).{0,12}(?:辅助|生成|参与|标注|声明)|(?:辅助|生成).{0,8}(?:AI|人工智能)", re.I)
QUOTED = re.compile(r'“[^”]*”|「[^」]*」|『[^』]*』|‘[^’]*’|"[^"\n]*"')
URL = re.compile(r"https?://[^\s<>]+", re.I)
STAGED_OPENING = re.compile(r"^(?:在(?:这个)?(?:日新月异|飞速发展|快速发展)的.{0,20}(?:时代|今天)|在人工智能飞速发展的今天|随着人工智能的飞速发展)")
PROMISE_ONLY_OPENING = re.compile(r"^(?:本文将|这篇文章将|接下来(?:我们)?将|下面(?:我们)?将)(?:.{0,60})(?:带你|介绍|探讨|讲解|揭晓|分析)")
EDITORIAL_CAVEAT = re.compile(r"(?:不是|不代表|不等于|不能当作|不能视为).{0,24}(?:独立实测|性能实测|真实测试|亲身体验|官方保证|所有.{0,6}都)")
CONTRAST = re.compile(r"不是[^。！？\n\x00]{1,60}而是")
GENERIC_HEADINGS = {"未来已来", "写在最后", "总结与展望", "结语"}


def prose_blocks(markdown: str) -> list[dict[str, Any]]:
    """Keep source block locations; exclude quoted, linked and code content."""
    lines = markdown.splitlines()
    # Metadata is not an article opening. Preserve line numbers while parsing.
    parse_lines = list(lines)
    if lines and lines[0].strip() == "---":
        end = next((i for i in range(1, len(lines)) if lines[i].strip() in {"---", "..."}), None)
        if end is not None:
            parse_lines[:end + 1] = [""] * (end + 1)
    tokens = MarkdownIt("commonmark").parse("\n".join(parse_lines))
    result = []
    quote_depth = 0
    source_level = None
    for index, token in enumerate(tokens):
        if token.type == "blockquote_open":
            quote_depth += 1
        elif token.type == "blockquote_close":
            quote_depth -= 1
        if token.type != "inline" or not token.map or quote_depth:
            continue
        if any(child.type == "html_inline" for child in token.children or []):
            continue
        previous = tokens[index - 1] if index else None
        heading = bool(previous and previous.type == "heading_open")
        level = int(previous.tag[1:]) if heading else 0
        pieces = []
        link_depth = strong_depth = strong_count = 0
        for child in token.children or []:
            if child.type == "link_open":
                link_depth += 1
                pieces.append("\x00")
            elif child.type == "link_close":
                link_depth -= 1
            elif link_depth:
                continue
            elif child.type == "strong_open":
                strong_depth += 1
            elif child.type == "strong_close":
                strong_depth -= 1
            elif child.type == "text":
                value = QUOTED.sub("\x00", URL.sub("\x00", child.content))
                pieces.append(value)
                if strong_depth:
                    strong_count += len(value.replace("\x00", "").strip())
            elif child.type in {"softbreak", "hardbreak"}:
                pieces.append("\n")
            elif child.type not in {"em_open", "em_close"}:
                pieces.append("\x00")
        text = QUOTED.sub("\x00", "".join(pieces)).strip()
        # Quotes crossing emphasis tokens cannot be reliably assigned bold spans.
        if text != "".join(pieces).strip():
            strong_count = 0
        if heading:
            if source_level is not None and level <= source_level:
                source_level = None
            if SOURCE_HEADING.fullmatch(text):
                source_level = level
        if source_level is not None or DISCLOSURE.search(text) or not text.replace("\x00", "").strip():
            continue
        result.append({
            "kind": "heading" if heading else "paragraph", "level": level,
            "text": text, "line": token.map[0] + 1, "end_line": token.map[1],
            "source_excerpt": "\n".join(lines[token.map[0]:token.map[1]])[:240],
            "strong_characters": strong_count,
        })
    return result


def review_prose(markdown: str, filename: str) -> dict[str, Any]:
    blocks = prose_blocks(markdown)
    paragraphs = [b for b in blocks if b["kind"] == "paragraph"]
    findings = []

    def add(code: str, block: dict[str, Any], observation: str, action: str) -> None:
        findings.append({
            "code": code, "severity": "advisory", "reference": f"{filename}:{block['line']}",
            "line": block["line"], "end_line": block["end_line"],
            "source_excerpt": block["source_excerpt"], "observation": observation, "action": action,
        })

    if paragraphs and STAGED_OPENING.search(paragraphs[0]["text"]):
        add("EDIT_STAGED_OPENING", paragraphs[0], "开头先铺陈时代背景，读者尚未看到具体新变化。",
            "核对后优先写事件、时间和读者关系；不要为补细节虚构新闻或亲历。")
    if paragraphs and PROMISE_ONLY_OPENING.search(paragraphs[0]["text"]):
        add("EDIT_PROMISE_OPENING_REVIEW", paragraphs[0], "开头先介绍文章将做什么，可能延迟交付新闻或答案。",
            "人工核对同段是否已经交代具体变化和读者价值；若没有，在证据支持下提前给出，保留必要限定。")
    for block in blocks:
        if block["kind"] == "heading" and block["level"] > 1 and block["text"] in GENERIC_HEADINGS:
            add("EDIT_GENERIC_HEADING", block, "小标题只说明段落位置，没有说明本节的信息。",
                "检查能否用本节真实结论或问题命名；保留华子栏目人设，不强制套模板。")
    contrasts = [b for b in paragraphs if CONTRAST.search(b["text"])]
    if len(contrasts) >= 3:
        for block in contrasts:
            add("EDIT_REPEATED_CONTRAST", block, f"本文有 {len(contrasts)} 个段落使用同类否定转折。",
                "逐处判断反面是否在纠正真实误解；保留有信息的对照、否定和边界，只精简空转折。")
    caveats = [b for b in paragraphs if EDITORIAL_CAVEAT.search(b["text"])]
    if len(caveats) >= 3:
        for block in caveats:
            add("EDIT_CAVEAT_REPETITION_REVIEW", block, f"本文有 {len(caveats)} 个段落使用类似的证据或实测限定。",
                "人工区分不同主张的必要边界与重复表述；必要限定必须保留，不能为精简改变事实范围。")
    for block in paragraphs:
        length = len(block["text"].replace("\x00", "").strip())
        if length >= 60 and block["strong_characters"] / length >= 0.7:
            add("EDIT_EMPHASIS_DENSITY", block, "这个较长段落的大部分可检查文字都加粗了。",
                "检查重点是否被全段加粗淹没；只调整强调范围，不删除数字、条件、来源或 AI 标注。")
    heading = None
    for block in blocks:
        if block["kind"] == "heading":
            heading = block if block["level"] > 1 else None
        elif heading is not None:
            # An exact echo is only an editing hint; deliberate emphasis may be useful.
            label = heading["text"].strip().rstrip("。.!！?？:：")
            sentence = re.split(r"[。!?！？\n]", block["text"], maxsplit=1)[0].strip().rstrip(".")
            if len(label) >= 6 and "\x00" not in label and sentence == label:
                add("EDIT_HEADING_ECHO", block, "本节第一个正文句与小标题重复，没有新增信息。",
                    "判断重复是否有意强调；无信息增量才精简，不删除随后事实、条件、来源或AI说明。")
            heading = None
    findings.sort(key=lambda item: (item["line"], item["code"]))
    return {
        "authority": "advisory_only", "automatic_rewrite": False, "fact_verification_performed": False,
        "scope": "Markdown prose; excludes quotes, code, links, HTML, source sections and AI disclosures",
        "limitations": "Pattern hints can miss issues or flag intentional rhetoric; zero findings is not approval.",
        "reviewed_block_count": len(blocks), "findings": findings,
    }


def review_delivery(markdown: str, anchors: Any, filename: str = "wechat-article.md") -> dict[str, Any]:
    """Locate author-selected prose; a matching quote is not a useful or true answer."""
    roles = ("answer", "action", "boundary")
    result: dict[str, Any] = {
        "authority": "authoring_advice_only_not_semantic_or_fact_approval",
        "status": "not_provided", "matches": [], "findings": [],
        "semantic_review_performed": False, "automatic_rewrite": False,
        "review_questions": [
            "Does the answer resolve the stated reader question rather than repeat the news?",
            "Can the reader complete the action and inspect its result?",
            "Does the boundary describe a real exception or stop condition?",
        ],
    }

    def issue(code: str, role: str, action: str) -> None:
        result["findings"].append({"code": code, "severity": "advisory",
                                   "reference": "editorial-experiment.json:delivery_anchors." + role,
                                   "action": action})

    if anchors is None:
        issue("DELIVERY_ANCHORS_OPTIONAL", "root", "For new articles, select exact answer/action/boundary quotes from the prose; do not retrofit historical drafts.")
        return result
    values = anchors if isinstance(anchors, dict) else {}
    paragraphs = [block for block in prose_blocks(markdown) if block["kind"] == "paragraph"
                  and not re.match(r"^(?:图\s*\d*|图片|配图|图注)\s*[:：|｜]", block["text"])]
    for role in roles:
        quote = values.get(role)
        if (not isinstance(quote, str) or not 12 <= len(quote.strip()) <= 600
                or "\x00" in quote or quote != quote.strip()):
            issue("DELIVERY_ANCHOR_INVALID", role, "Use an exact visible prose quote of 12-600 characters, without surrounding whitespace; do not fill this with a heading or a plan promise.")
            continue
        found = [block for block in paragraphs for _ in range(block["text"].count(quote))]
        if len(found) != 1:
            issue("DELIVERY_ANCHOR_NOT_FOUND" if not found else "DELIVERY_ANCHOR_AMBIGUOUS", role,
                  "Select a quote occurring exactly once in one prose block. Quoted/source/disclosure/link/code/HTML/alt content is excluded; review meaning manually.")
            continue
        block = found[0]
        result["matches"].append({"role": role, "quote": quote, "reference": f"{filename}:{block['line']}",
                                  "line": block["line"], "end_line": block["end_line"],
                                  "source_excerpt": block["source_excerpt"], "status": "located_only"})
    supplied = [values.get(role) for role in roles if isinstance(values.get(role), str)]
    if len(set(supplied)) != len(supplied):
        issue("DELIVERY_ROLE_OVERLAP_REVIEW", "root", "The same quote serves multiple jobs. Review whether it actually delivers each; do not create filler to satisfy separate fields.")
    result["status"] = "needs_review" if result["findings"] else "located_for_semantic_review"
    return result
