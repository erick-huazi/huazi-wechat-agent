#!/usr/bin/env python3
"""Five-image planning, pixel checks, layout and hash-bound visual review.

Pixel metrics are technical checks, not an aesthetic or factual judgment.
Visual scores must be supplied after inspecting the actual five images.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops, ImageStat

from distribution_io import (
    atomic_write_json, canonical_sha256, create_pack_backup, file_sha256,
    five_image_names, image_file_probe, pack_date_prefix, pack_lock,
)
from workflow_policy import ACTIVE_PLATFORMS, POLICY as WORKFLOW_POLICY, plan_platform_ids
from workflow_policy import inspection_platform_ids, scope_matches_daily_policy

STANDARD = Path(__file__).resolve().parents[1] / "config/image-quality-standard.json"
INPUT_FILES = ("image-storyboard.json", "image-visual-review.json")


def load_standard() -> dict[str, Any]:
    return json.loads(STANDARD.read_text(encoding="utf-8"))


def required(pack: Path) -> bool:
    return ((pack_date_prefix(pack) or "") >= load_standard()["effective_from"]
            or (pack / INPUT_FILES[0]).exists())


def read_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def skeleton(pack: Path) -> dict[str, Any]:
    return {
        "schema_version": 1, "policy_id": load_standard()["policy_id"],
        "character_reference": "", "palette": "", "items": [
            {"image": f"images/{name}", "role": role, "section_anchor": "",
             "message": "", "layout": "", "on_image_text": "", "claim_ids": [], "prompt": "",
             "visual_explanation": {"subject": "", "relationship": "", "reader_takeaway": "", "avoid_implication": ""}}
            for index, name in enumerate(five_image_names(pack))
            for role in [load_standard()["roles"][index] if index < 5 else "supplement"]
        ],
    }


def rows(value: Any) -> list[dict[str, Any]]:
    return value if isinstance(value, list) and all(isinstance(x, dict) for x in value) else []


def daily_scope_required(pack: Path) -> bool:
    return (pack_date_prefix(pack) or "") >= WORKFLOW_POLICY["effective_from"]


def layout_platform_ids(pack: Path) -> list[str]:
    # New packs cannot reduce their inspection scope by omitting plan entries.
    return inspection_platform_ids(pack)


def plan_scope_valid(pack: Path) -> bool:
    items = rows(read_object(pack / "distribution-plan.json").get("items"))
    ids = [row.get("platform_id") for row in items]
    if not ids or any(not isinstance(pid, str) or not pid.strip() for pid in ids):
        return False
    if len(set(ids)) != len(ids):
        return False
    return scope_matches_daily_policy(pack, ids)


def input_snapshot(pack: Path, policy: dict[str, Any]) -> list[dict[str, str]]:
    # Include the inventory and plan: adding files or changing platform scope invalidates QA.
    names = set(INPUT_FILES) | {"distribution-plan.json", "wechat-article.md", "claim-verification.json"}
    if (pack / "image-count.json").exists():
        names.add("image-count.json")
    if (pack / "claim-verification.json").resolve().is_relative_to(pack.resolve()):
        from source_evidence import snapshot_paths
        names.update(snapshot_paths(pack))
    names.update(f"images/{name}" for name in five_image_names(pack))
    image_dir = pack / "images"
    if image_dir.is_dir() and image_dir.resolve().is_relative_to(pack.resolve()):
        names.update(p.relative_to(pack).as_posix() for p in image_dir.rglob("*") if p.is_file())
    elif image_dir.exists():
        names.add("images")
    # Scope is read only after checking the plan stays inside this pack.
    platforms = layout_platform_ids(pack) if (pack / "distribution-plan.json").resolve().is_relative_to(pack.resolve()) else []
    names.update(name for pid, name in policy["long_article_files"].items() if pid in platforms)
    result = []
    for name in sorted(names):
        path = pack / name
        item = {"path": name, "sha256": ""}
        if not path.resolve().is_relative_to(pack.resolve()):
            item["error"] = "outside_pack"
        else:
            try:
                item["sha256"] = file_sha256(path) if path.is_file() else ""
            except OSError:
                item["error"] = "unreadable"
        result.append(item)
    return result


def layout_issues(pack: Path, filename: str, expected: list[str]) -> list[str]:
    from markdown_it import MarkdownIt

    path = pack / filename
    if not path.is_file():
        return ["IMAGE_ARTICLE_MISSING"]
    text = path.read_text(encoding="utf-8")
    actual, positions = [], []
    visible_length = 0
    raw_html = False
    # Parse rendered Markdown, not image-like strings inside fenced code blocks.
    for block in MarkdownIt("commonmark").parse(text):
        raw_html = raw_html or block.type == "html_block"
        for token in block.children or []:
            raw_html = raw_html or token.type == "html_inline"
            if token.type == "image":
                actual.append(str(token.attrGet("src") or "").removeprefix("./"))
                positions.append(visible_length)
            elif token.type in {"text", "code_inline"}:
                visible_length += len(token.content.strip())
    issues = []
    if raw_html:
        issues.append("IMAGE_LAYOUT_RAW_HTML")
    if actual != expected:
        issues.append("IMAGE_REFERENCE_ORDER")
    if any(right - left < 20 for left, right in zip(positions, positions[1:])):
        issues.append("IMAGE_NOT_INTERLEAVED")
    return issues


def image_section_contexts(text: str) -> dict[str, list[str]]:
    from markdown_it import MarkdownIt

    sections = [{"text": [], "images": []}]
    headings: list[tuple[int, str]] = []
    heading_level = None
    for block in MarkdownIt("commonmark").parse(text):
        if block.type == "heading_open":
            heading_level = int(block.tag[1:])
        if block.type != "inline":
            continue
        children = block.children or []
        visible = "".join(token.content if token.type in {"text", "code_inline"} else " "
                          if token.type in {"softbreak", "hardbreak"} else "" for token in children)
        if heading_level is not None:
            headings = [(level, title) for level, title in headings if level < heading_level]
            headings.append((heading_level, visible))
            sections.append({"text": [title for _, title in headings], "images": []})
            heading_level = None
        else:
            sections[-1]["text"].append(visible)
        sections[-1]["images"].extend(str(token.attrGet("src") or "").removeprefix("./")
                                       for token in children if token.type == "image")
    result: dict[str, list[str]] = {}
    for section in sections:
        context = "\n".join(section["text"])
        for name in section["images"]:
            result.setdefault(name, []).append(context)
    return result


def numeric_label_issues(board_items: list[dict], verification: dict, pack: Path | None = None) -> list[dict[str, str]]:
    from research_evidence_gate import numeric_values, NUMERIC_CONTEXT_FIELDS, validate_numeric_evidence_contract

    issues = []
    claims = rows(verification.get("claims"))
    grounding_invalid = False
    if pack is not None:
        from source_evidence import policy_required, validate_grounding
        from research_evidence_gate import load_standard
        standard = load_standard()
        if policy_required(pack, verification, standard):
            grounding_invalid = bool(validate_grounding(pack, verification, standard, datetime.now().astimezone())["issues"])
    for row in board_items:
        text = row.get("on_image_text")
        refs = row.get("claim_ids", [])
        code = ""
        if not isinstance(text, str) or not isinstance(refs, list) or any(not isinstance(x, str) for x in refs):
            code = "IMAGE_LABEL_CONTRACT_INVALID"
        elif numeric_values(text):
            # A numeric comparison must come from one scoped claim, never joined metrics.
            matches = [c for c in claims if len(refs) == 1 and c.get("claim_id") == refs[0]]
            if len(matches) != 1:
                code = "IMAGE_NUMERIC_CLAIM_REQUIRED"
            else:
                claim = matches[0]
                context = claim.get("metric_context")
                claim_text = claim.get("text", "")
                source_ids = claim.get("source_ids")
                if (claim.get("verdict") != "verified" or not isinstance(claim_text, str)
                        or not isinstance(context, dict) or not isinstance(source_ids, list)
                        or not source_ids or any(not isinstance(s, str) for s in source_ids)
                        or any(not isinstance(context.get(k), str) or not context[k].strip() for k in NUMERIC_CONTEXT_FIELDS)
                        or validate_numeric_evidence_contract(refs[0], claim_text, source_ids, context)):
                    code = "IMAGE_NUMERIC_EVIDENCE_INVALID"
                elif Counter(numeric_values(text)) - Counter(numeric_values(claim_text)):
                    code = "IMAGE_NUMERIC_VALUE_MISMATCH"
                elif grounding_invalid:
                    code = "IMAGE_NUMERIC_SOURCE_UNGROUNDED"
        if code:
            issues.append({"code": code, "reference": str(row.get("image", "")),
                           "action": "Verify the exact image labels, units and comparison scope against one source-backed claim; record its claim_id. Do not merge unrelated metrics."})
    return issues


def report_identity(report: dict[str, Any]) -> str:
    return canonical_sha256({k: v for k, v in report.items() if k not in {"generated_at", "report_id"}})


def build_report(pack: Path) -> dict[str, Any]:
    policy = load_standard()
    source_advisor_before = file_sha256(Path(__file__).with_name("source_image_advisor.py"))
    inputs = input_snapshot(pack, policy)
    unsafe = [row for row in inputs if row.get("error")]
    try:
        if unsafe:
            raise ValueError("invalid pack input")
        report = _build_report(pack)
    except (OSError, ValueError, SyntaxError):
        report = {
            "schema_version": 1, "policy_id": policy["policy_id"], "policy_sha256": canonical_sha256(policy),
            "pack": str(pack.resolve()), "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "passed": False, "issues": [{"code": "IMAGE_INPUT_UNREADABLE", "reference": row["path"] if unsafe else "pack",
                "action": "Keep all inputs inside this pack; repair unreadable files and rerun QA."} for row in (unsafe or [{}])],
            "warnings": [], "images": [], "visual_score": None, "inputs": inputs,
            "input_fingerprint": canonical_sha256(inputs), "score_kind": "reviewer_assessment_not_traffic_prediction",
        }
    if inputs != input_snapshot(pack, policy):
        report["passed"] = False
        report["visual_score"] = None
        report["issues"].append({"code": "IMAGE_INPUT_CHANGED_DURING_CHECK", "reference": "pack",
                                 "action": "Stop concurrent edits, acquire the pack lock and rerun quality checks."})
        for item in report["images"]:
            item.update(review_status="stale", visual_score=None)
    if (report.get("source_image_advisor_sha256")
            and (report["source_image_advisor_sha256"] != source_advisor_before
                 or source_advisor_before != file_sha256(Path(__file__).with_name("source_image_advisor.py")))):
        report["passed"] = False
        report["visual_score"] = None
        report["issues"].append({"code": "IMAGE_SOURCE_REVIEWER_CHANGED_DURING_CHECK", "reference": "source_image_advisor.py",
                                 "action": "Stop concurrent edits and rerun source-image and visual checks."})
        for item in report["images"]:
            item.update(review_status="stale", visual_score=None)
    report["report_id"] = report_identity(report)
    return report


def _build_report(pack: Path) -> dict[str, Any]:
    policy = load_standard()
    expected = [f"images/{name}" for name in five_image_names(pack)]
    issues: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []

    def issue(code: str, reference: str, action: str) -> None:
        issues.append({"code": code, "reference": reference, "action": action})

    board = read_object(pack / INPUT_FILES[0])
    board_items = rows(board.get("items"))
    verification = read_object(pack / "claim-verification.json")
    issues.extend(numeric_label_issues(board_items, verification, pack))
    roles = [r.get("role") for r in board_items]
    if (type(board.get("schema_version")) is not int or board.get("schema_version") != 1
            or board.get("policy_id") != policy["policy_id"]
            or [r.get("image") for r in board_items] != expected
            or not roles or roles[0] != "cover"
            or [role for role in roles if role != "supplement"] != policy["roles"]):
        issue("IMAGE_STORYBOARD_INVALID", INPUT_FILES[0], "Write the five ordered image roles using the pack skeleton.")
    if not all(isinstance(board.get(k), str) and board[k].strip() for k in ("character_reference", "palette")):
        issue("IMAGE_ART_DIRECTION_MISSING", INPUT_FILES[0], "Specify a real character reference and consistent palette.")
    article_path = pack / "wechat-article.md"
    article = article_path.read_text(encoding="utf-8") if article_path.is_file() else ""
    section_contexts = image_section_contexts(article)
    from source_image_advisor import review_source_image
    for row in board_items:
        source_review = review_source_image(row, article)
        for code in source_review["issues"]:
            issue(code, str(row.get("image", "")), "Complete the declared source, use-basis and adjacent caption; independently verify rights and facts before reuse.")
        if not all(isinstance(row.get(k), str) and row[k].strip()
                   for k in ("section_anchor", "message", "layout", "prompt")):
            issue("IMAGE_BRIEF_INCOMPLETE", str(row.get("image", "")), "Describe one message, composition, section anchor and final generation prompt.")
        anchor = row.get("section_anchor")
        contexts = section_contexts.get(str(row.get("image", "")), [])
        if not isinstance(anchor, str) or not anchor.strip():
            issue("IMAGE_ANCHOR_MISSING", str(row.get("image", "")), "Bind the image to an actual source article heading or sentence.")
        elif not any(anchor in context for context in contexts):
            code = "IMAGE_ANCHOR_SECTION_MISMATCH" if anchor in article else "IMAGE_ANCHOR_MISSING"
            issue(code, str(row.get("image", "")), "Use a visible heading or sentence in this image's section; code blocks and image alt text are not section anchors.")

    image_dir = pack / "images"
    actual_images = sorted(p.relative_to(pack).as_posix() for p in image_dir.rglob("*")
                           if p.is_file() and p.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".gif"}) if image_dir.is_dir() else []
    if actual_images != expected:
        issue("IMAGE_INVENTORY", "images", "Keep exactly the five dated PNGs in images; retain alternatives outside the delivery directory.")
    checks = []
    thumbnails: list[tuple[str, Image.Image]] = []
    pixel_hashes: dict[str, str] = {}
    for index, name in enumerate(expected):
        probe = image_file_probe(pack / name)
        size = [900, 383] if index == 0 else [1080, 720]
        item = {"image": name, "sha256": probe["sha256"], "probe": probe, "expected": size}
        checks.append(item)
        if (not probe["decoded"] or probe["format"] != "PNG"
                or [probe["width"], probe["height"]] != size):
            issue("IMAGE_DECODE_OR_SIZE", name, "Regenerate or repair the PNG at its exact required dimensions.")
            continue
        with Image.open(pack / name) as raw:
            item["frame_count"] = getattr(raw, "n_frames", 1)
            if item["frame_count"] != 1 or getattr(raw, "is_animated", False):
                issue("IMAGE_ANIMATED_PNG", name, "Use a single-frame PNG; reviewing the first frame does not verify later animation frames.")
                continue
            raw.load()
            rgba = raw.convert("RGBA")
            visible = Image.new("RGBA", rgba.size, "white")
            visible.alpha_composite(rgba)
            rgb = visible.convert("RGB")
            pixel_sha = hashlib.sha256(str(rgb.size).encode() + rgb.tobytes()).hexdigest()
            item["pixel_sha256"] = pixel_sha
            thumb = rgb.resize((64, 64), Image.Resampling.LANCZOS)
            stddev = max(ImageStat.Stat(thumb).stddev)
            item["pixel_stddev"] = round(stddev, 3)
            if stddev < policy["minimum_image_stddev"]:
                issue("IMAGE_BLANK_OR_FLAT", name, "Inspect the image; replace blank, transparent or flat-color placeholders.")
            if pixel_sha in pixel_hashes:
                issue("IMAGE_PIXEL_DUPLICATE", name, f"Replace the duplicate of {pixel_hashes[pixel_sha]}; metadata changes do not create a new picture.")
            pixel_hashes[pixel_sha] = name
            for previous, other in thumbnails:
                error = sum(ImageStat.Stat(ImageChops.difference(thumb, other)).mean) / 3
                if error <= policy["near_duplicate_mean_error"]:
                    warnings.append({"code": "IMAGE_NEAR_DUPLICATE", "reference": f"{previous}|{name}",
                                     "action": "Visually compare for genuinely different information; pixel similarity alone is not a semantic judgment."})
            thumbnails.append((name, thumb))

    for pid, filename in policy["long_article_files"].items():
        if pid in layout_platform_ids(pack):
            for code in layout_issues(pack, filename, expected):
                issue(code, filename, "Insert all five local images in order, with substantive paragraphs between them.")
    if not plan_scope_valid(pack):
        issue("IMAGE_PLAN_SCOPE_INVALID", "distribution-plan.json", "Generate the complete current-policy plan; preserve original historical scope. Missing, subset, duplicate or unknown entries cannot skip layout checks.")

    review = read_object(pack / INPUT_FILES[1])
    review_items = rows(review.get("items"))
    dimensions = policy["review_dimensions"]
    try:
        reviewed = datetime.fromisoformat(str(review.get("reviewed_at", "")))
        time_ok = reviewed.tzinfo is not None and reviewed <= datetime.now(timezone.utc) + timedelta(minutes=5)
    except ValueError:
        time_ok = False
    if (type(review.get("schema_version")) is not int or review.get("schema_version") != 1
            or not isinstance(review.get("reviewer"), str)
            or not review.get("reviewer", "").strip() or not time_ok
            or [r.get("image") for r in review_items] != expected):
        issue("IMAGE_VISUAL_REVIEW_MISSING", INPUT_FILES[1], "Inspect each actual image and record reviewer, timezone-aware time and five ordered review rows.")
    board_hash = file_sha256(pack / INPUT_FILES[0]) if (pack / INPUT_FILES[0]).is_file() else ""
    article_hash = file_sha256(article_path) if article_path.is_file() else ""
    if (not board_hash or review.get("storyboard_sha256") != board_hash
            or not article_hash or review.get("article_sha256") != article_hash):
        issue("IMAGE_VISUAL_REVIEW_STALE", INPUT_FILES[1], "Recheck factual labels against the current storyboard and source article; record their current SHA-256.")
    from research_evidence_gate import numeric_values
    if any(numeric_values(row.get("on_image_text", "")) for row in board_items if isinstance(row.get("on_image_text"), str)):
        claim_path = pack / "claim-verification.json"
        claim_hash = file_sha256(claim_path) if claim_path.is_file() else ""
        if not claim_hash or review.get("claims_sha256") != claim_hash:
            issue("IMAGE_VISUAL_REVIEW_STALE", INPUT_FILES[1], "Reinspect numeric labels against the current verified claims and record claims_sha256.")
    scores = []
    hashes = {item["image"]: item["sha256"] for item in checks}
    for row in review_items:
        name = str(row.get("image", ""))
        values = row.get("scores") if isinstance(row.get("scores"), dict) else {}
        valid = (set(values) == set(dimensions)
                 and all(type(v) in (int, float) and 0 <= v <= 5 for v in values.values()))
        if not valid:
            issue("IMAGE_SCORE_INVALID", name, "Score each of the five documented dimensions from 0 to 5 after visual inspection.")
            continue
        score = sum(values[k] / 5 * weight for k, weight in dimensions.items())
        scores.append(round(score, 1))
        if min(values.values()) < policy["minimum_dimension_score"] or score < policy["minimum_visual_score"]:
            issue("IMAGE_VISUAL_QUALITY_LOW", name, "Revise the image against the specific weak dimensions, then inspect again.")
        if not hashes.get(name) or row.get("sha256") != hashes.get(name):
            issue("IMAGE_VISUAL_HASH_MISMATCH", name, "Inspect the current image, not a previous render, and bind its SHA-256.")
        if (row.get("fact_checked") is not True or row.get("no_engagement_bait") is not True
                or not isinstance(row.get("notes"), str) or not row["notes"].strip()):
            issue("IMAGE_VISUAL_REVIEW_INCOMPLETE", name, "Record observed strengths/problems and explicitly check factual labels and engagement bait.")
    global_review_invalid = any(i["reference"] in INPUT_FILES for i in issues)
    for item in checks:
        codes = {i["code"] for i in issues if i["reference"] == item["image"]}
        item["technical_passed"] = not bool(codes & {"IMAGE_DECODE_OR_SIZE", "IMAGE_ANIMATED_PNG", "IMAGE_BLANK_OR_FLAT", "IMAGE_PIXEL_DUPLICATE"})
        review_row = next((r for r in review_items if r.get("image") == item["image"]), None)
        status = "unverified" if not review_row else "stale" if global_review_invalid or "IMAGE_VISUAL_HASH_MISMATCH" in codes else "needs_revision" if codes else "verified"
        item["review_status"] = status
        item["visual_score"] = round(sum(review_row["scores"][k] / 5 * weight for k, weight in dimensions.items()), 1) if status == "verified" else None
    inputs = input_snapshot(pack, policy)
    report = {
        "schema_version": 1, "policy_id": policy["policy_id"],
        "policy_sha256": canonical_sha256(policy), "pack": str(pack.resolve()),
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "passed": not issues, "issues": issues, "warnings": warnings, "images": checks,
        "visual_score": round(sum(scores) / len(scores), 1) if len(scores) == len(expected) and all(i["review_status"] == "verified" for i in checks) else None,
        "score_kind": "reviewer_assessment_not_traffic_prediction",
        "inputs": inputs, "input_fingerprint": canonical_sha256(inputs),
    }
    if any("source_image" in row for row in board_items):
        report["source_image_advisor_sha256"] = file_sha256(Path(__file__).with_name("source_image_advisor.py"))
    report["report_id"] = report_identity(report)
    return report


def gate(pack: Path) -> dict[str, Any]:
    if not required(pack):
        return {"required": False, "passed": True, "issues": []}
    path = pack / "image-quality-report.json"
    saved = read_object(path)
    current = build_report(pack)
    valid = bool(saved) and saved.get("report_id") == report_identity(saved) == current["report_id"] and saved.get("passed") is True
    issues = list(current["issues"])
    if not valid:
        issues.append({"code": "IMAGE_REPORT_MISSING_OR_STALE", "reference": path.name,
                       "action": "Run scripts/image_quality_gate.py --pack <pack>, fix all issues, then rerun dry-run."})
    return {"required": True, "passed": current["passed"] and valid, "issues": issues,
            "warnings": current["warnings"], "visual_score": current["visual_score"] if valid else None,
            "report_current": valid, "images": current["images"], "report_id": current["report_id"]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pack", type=Path, required=True)
    parser.add_argument("--no-write", action="store_true")
    args = parser.parse_args()
    pack = args.pack.resolve()
    if not pack.is_dir() or not pack_date_prefix(pack):
        parser.error("Existing dated publish pack required")
    with pack_lock(pack, "image-quality"):
        report = build_report(pack)
        if not args.no_write:
            create_pack_backup(pack, "image-quality", ["image-quality-report.json"], keep=10)
            atomic_write_json(pack / "image-quality-report.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
