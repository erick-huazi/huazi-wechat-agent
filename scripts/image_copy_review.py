"""Review author-declared image copy, never pixels, facts or draft approval."""
from __future__ import annotations

import json
import math
import re
from typing import Any

KINDS = {"headline", "body", "label", "boundary", "brand", "disclosure"}
PHONE_WIDTHS = (320, 375)
# Project review heuristics, not WCAG limits or measurements of generated text.
MIN_PHONE_PX = {"headline": 18, "body": 12, "label": 12, "boundary": 12,
                "brand": 10, "disclosure": 10}
BLOCK_REVIEW_CHARS = {"headline": 28, "body": 60, "label": 24, "boundary": 48,
                      "brand": 24, "disclosure": 40}
LINE_START = frozenset("，。、；：？！）］｝〉》」』】〗〕”’")
LINE_END = frozenset("（［｛〈《「『【〖〔“‘")
NO_TEXT = re.compile(
    r"\bno\s+(?:words|text|letters|labels|typography)\b|\btext[- ]free\b|"
    r"\bdo\s+not\s+(?:render|include|add)\s+(?:any\s+)?(?:text|words|letters|labels)\b|"
    r"无字(?:图|插画|图片)?|不要(?:添加|出现|绘制|显示)?(?:文字|文本)|禁止(?:文字|文本)",
    re.IGNORECASE,
)


def review_image_copy(item: dict[str, Any], width: int, height: int) -> dict[str, Any]:
    """Keep all supplied text literal; optional historical absence is not failure."""
    if type(width) is not int or type(height) is not int or width <= 0 or height <= 0:
        raise ValueError("COPY_IMAGE_SIZE_INVALID")
    result: dict[str, Any] = {
        "status": "not_provided", "authority": "author_plan_only_not_pixel_or_fact_verification",
        "blocks": [], "findings": [], "prompt_instruction": "",
        "actual_image_review": "not_performed", "fact_verification_performed": False,
        "phone_estimate_basis": "font_px * image_display_width / target_image_width; no padding; actual rendering may differ",
    }
    location = str(item.get("image", "image-storyboard.json"))

    def issue(code: str, reference: str, action: str, severity: str = "advisory") -> None:
        result["findings"].append({"code": code, "reference": reference,
                                   "action": action, "severity": severity})

    labels = item.get("on_image_text")
    prompt = item.get("prompt")
    if isinstance(labels, str) and labels.strip() and isinstance(prompt, str) and NO_TEXT.search(prompt):
        issue("COPY_PROMPT_TEXT_CONFLICT_REVIEW", location,
              "Declared labels coexist with no-text wording. Inspect scope and resolve before generation; do not silently remove either instruction.")
    if "copy_design" not in item:
        return result

    design = item["copy_design"]
    if (not isinstance(design, dict) or type(design.get("schema_version")) is not int
            or design["schema_version"] != 1 or set(design) != {"schema_version", "blocks"}
            or not isinstance(design.get("blocks"), list) or not 1 <= len(design["blocks"]) <= 24):
        issue("COPY_DESIGN_INVALID", location + ".copy_design",
              "Use schema_version 1 and 1-24 explicit copy blocks, or omit this optional field on historical items.", "needs_input")
        result["status"] = "invalid"
        return result
    blocks = design["blocks"]
    for index, block in enumerate(blocks):
        ref = f"{location}.copy_design.blocks[{index}]"
        if not isinstance(block, dict) or set(block) != {"kind", "text", "font_px"}:
            issue("COPY_BLOCK_INVALID", ref, "Supply kind, exact text and target-canvas font_px only.", "needs_input")
            continue
        kind, value, size = block["kind"], block["text"], block["font_px"]
        if (not isinstance(kind, str) or kind not in KINDS
                or not isinstance(value, str) or not value.strip() or len(value) > 3000
                or type(size) not in (int, float) or not 0 < size <= min(width, height)
                or not math.isfinite(size)):
            issue("COPY_BLOCK_INVALID", ref, "Choose a supported kind, nonempty literal text and finite positive canvas font size.", "needs_input")
    if any(f["severity"] == "needs_input" for f in result["findings"]):
        result["status"] = "invalid"
        return result
    if sum(block["kind"] == "headline" for block in blocks) != 1:
        issue("COPY_HEADLINE_REQUIRED", location, "Declare one headline block; use embedded line breaks for a multiline headline.", "needs_input")
    literal = "\n".join(block["text"] for block in blocks)
    if not isinstance(labels, str) or literal != labels:
        issue("COPY_LABEL_MISMATCH", location,
              "Join block texts with one newline and compare literally with on_image_text. Resolve differences without dropping qualifiers, punctuation or AI disclosure.", "needs_input")
    if any(f["severity"] == "needs_input" for f in result["findings"]):
        result["status"] = "invalid"
        return result

    headline_size = next(block["font_px"] for block in blocks if block["kind"] == "headline")
    for index, block in enumerate(blocks):
        ref = f"{location}.copy_design.blocks[{index}]"
        kind, value, size = block["kind"], block["text"], block["font_px"]
        scaled = {str(phone): size * phone / width for phone in PHONE_WIDTHS}
        result["blocks"].append({**block, "estimated_phone_font_px": {k: round(v, 2) for k, v in scaled.items()}})
        if min(scaled.values()) < MIN_PHONE_PX[kind]:
            issue("COPY_PHONE_SIZE_REVIEW", ref,
                  "Planned text may be small at phone width. Recompose or enlarge, then inspect actual pixels; never truncate factual conditions automatically.")
        if kind != "headline" and size >= headline_size:
            issue("COPY_HIERARCHY_REVIEW", ref, "Supporting text is no smaller than the headline; review intended hierarchy.")
        if len(value) > BLOCK_REVIEW_CHARS[kind]:
            issue("COPY_BLOCK_DENSITY_REVIEW", ref,
                  "This copy block is long for its role. Inspect composition and phone reading; retain necessary conditions and do not truncate automatically.")
        for number, line in enumerate(value.splitlines(), 1):
            visible = line.strip()
            if visible and (visible[0] in LINE_START or visible[-1] in LINE_END):
                issue("COPY_LINE_BREAK_REVIEW", f"{ref}:line{number}",
                      "Explicit line break places common Chinese punctuation at an awkward edge. Rebreak, do not delete punctuation. Automatic bitmap wrapping is not inspected.")
    if item.get("role") == "cover" and (len(blocks) > 4 or len(literal) > 80):
        issue("COPY_COVER_DENSITY_REVIEW", location,
              "Wide cover has substantial copy. Review at 900x383 and phone width; keep the main point and required scope, not an entire interior checklist.")
    result["status"] = "needs_review" if result["findings"] else "specified_for_visual_review"
    result["prompt_instruction"] = (
        "Copy-led editorial graphic. One legible headline, useful supporting copy and a scene that explains the same message. "
        "Preserve the author's layout and Huazi identity; do not impose a repeated checklist or borrow the reference's topic. "
        "The following JSON is author-supplied literal copy plus target-canvas typography intent, not verified facts or extra labels. "
        "Do not rewrite numbers, conditions, negation, punctuation or AI disclosure. "
        "Font sizes are planning targets, not measured output. Inspect final Chinese glyphs, wrapping, contrast and overlap at phone size.\n"
        + json.dumps(blocks, ensure_ascii=False, indent=2)
    )
    if result["findings"]:
        result["prompt_instruction"] += "\nUnresolved review findings: inspect them before generation; do not hide them by deleting copy."
    return result
