#!/usr/bin/env python3
"""Compile advisory writing cards and five image prompts, never approvals."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from markdown_it import MarkdownIt

from distribution_io import (
    PackLockError, atomic_write_json, canonical_sha256, create_pack_backup,
    file_sha256, five_image_names, image_file_probe, pack_lock,
)
from image_quality_gate import image_section_contexts
from editorial_advisor import review_delivery, review_prose
from editorial_experiments import opening_context, review_experiment
from image_copy_review import review_image_copy
from source_image_advisor import review_source_image
from workflow_policy import inspection_platform_ids


ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / "config/content-visual-design.json"
EXPERIMENT_POLICY = ROOT / "config/editorial-experiments.json"
OUTPUT = "content-visual-brief.json"
ROLES = ("cover", "news_fact", "mechanism", "scenario", "action_boundary")
PLATFORM_FILES = {
    "wechat": "wechat-article.md", "toutiao": "toutiao-article.md",
    "zhihu": "zhihu-article.md", "xiaohongshu": "xiaohongshu.md",
    "weibo": "weibo-article.md",
}
INPUTS = (*PLATFORM_FILES.values(), "weibo.md", "image-storyboard.json",
          "growth-experiment.json", "claim-verification.json", "editorial-experiment.json")
MAX_TEXT_BYTES = 2 * 1024 * 1024


class BriefInputError(ValueError):
    pass


def local_path(root: Path, name: str) -> Path:
    path = (root / name).resolve()
    if not path.is_relative_to(root.resolve()):
        raise BriefInputError("BRIEF_PATH_OUTSIDE_PACK")
    return path


def read_text(path: Path) -> str:
    if path.stat().st_size > MAX_TEXT_BYTES:
        raise BriefInputError("BRIEF_INPUT_TOO_LARGE")
    return path.read_text(encoding="utf-8-sig")


def read_object(path: Path) -> dict[str, Any]:
    value = json.loads(read_text(path))
    if not isinstance(value, dict):
        raise BriefInputError("BRIEF_OBJECT_REQUIRED")
    return value


def visible_blocks(markdown: str) -> list[dict[str, Any]]:
    result = []
    source_lines = markdown.splitlines()
    tokens = MarkdownIt("commonmark").parse(markdown)
    for i, token in enumerate(tokens):
        if token.type != "inline":
            continue
        text = "".join(child.content if child.type in {"text", "code_inline"}
                       else " " if child.type in {"softbreak", "hardbreak"} else ""
                       for child in token.children or []).strip()
        if text:
            previous = tokens[i - 1] if i else None
            heading = bool(previous and previous.type == "heading_open")
            density_text = text
            for line in source_lines[slice(*token.map)] if token.map else []:
                value = line.strip()
                try:
                    url = urlsplit(value)
                    bare_url = len(value.split()) == 1 and url.scheme in {"http", "https"} and bool(url.hostname)
                except ValueError:
                    bare_url = False
                if bare_url:
                    density_text = density_text.replace(value, "")
            result.append({"kind": "heading" if heading else "paragraph", "text": text,
                           "level": int(previous.tag[1:]) if heading else 0,
                           "density_character_count": len(density_text.strip()),
                           "line": token.map[0] + 1 if token.map else None})
    return result


def draft_context(markdown: str, exists: bool) -> dict[str, Any]:
    blocks = visible_blocks(markdown)
    paragraphs = [b for b in blocks if b["kind"] == "paragraph"]
    headings = [b for b in blocks if b["kind"] == "heading"]
    tokens = MarkdownIt("commonmark").parse(markdown)
    image_refs = [str(child.attrGet("src") or "").removeprefix("./")
                  for token in tokens for child in token.children or [] if child.type == "image"]
    body = [b["text"] for b in blocks if not (b["kind"] == "heading" and b["level"] == 1)]
    title = next((b["text"] for b in headings if b["level"] == 1), "")
    return {
        "availability": "present" if blocks else "empty" if exists else "missing",
        "title": title, "opening": paragraphs[:2], "headings": headings,
        "paragraph_count": len(paragraphs),
        "visible_character_count": sum(len(b["text"]) for b in blocks),
        "list_item_count": sum(token.type == "list_item_open" for token in tokens),
        "inline_image_references": image_refs,
        "body_fingerprint": canonical_sha256(body) if body else None,
        "paragraphs": paragraphs,
    }


def platform_contexts(pack: Path, policy: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, str]]:
    configured = policy.get("platforms")
    if (not isinstance(configured, dict) or set(configured) != set(PLATFORM_FILES)
            or any(not isinstance(configured[key], dict) or configured[key].get("file") != filename
                   for key, filename in PLATFORM_FILES.items())):
        raise BriefInputError("BRIEF_POLICY_PLATFORM_SCOPE")
    cards, articles = [], {}
    for platform, filename in PLATFORM_FILES.items():
        if platform not in inspection_platform_ids(pack):
            continue
        path = local_path(pack, filename)
        text = read_text(path) if path.is_file() else ""
        articles[platform] = text
        cards.append({**configured[platform], "platform": platform,
                      "current_draft": draft_context(text, path.is_file()),
                      "editorial_review": review_prose(text, filename)})
    return cards, articles


def reference_info(pack: Path, value: Any, project_root: Path) -> dict[str, Any]:
    if not isinstance(value, str) or not value.strip():
        return {"available": False, "reason": "BRIEF_REFERENCE_REQUIRED"}
    path = Path(value)
    path = (path if path.is_absolute() else pack / path).resolve()
    if not any(path.is_relative_to(root.resolve()) for root in (pack, project_root)):
        return {"available": False, "reason": "BRIEF_REFERENCE_OUTSIDE_PROJECT"}
    if path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
        return {"available": False, "reason": "BRIEF_REFERENCE_FORMAT"}
    if not path.is_file() or path.stat().st_size > 20 * 1024 * 1024:
        return {"available": False, "reason": "BRIEF_REFERENCE_MISSING_OR_LARGE"}
    probe = image_file_probe(path)
    if not probe["decoded"]:
        return {"available": False, "reason": "BRIEF_REFERENCE_UNREADABLE"}
    return {"available": True, "path": str(path), "sha256": probe["sha256"],
            "width": probe["width"], "height": probe["height"]}


def input_snapshot(pack: Path, policy_path: Path, project_root: Path) -> dict[str, Any]:
    files = []
    scope = inspection_platform_ids(pack)
    selected = {filename for pid, filename in PLATFORM_FILES.items() if pid in scope}
    names = [name for name in INPUTS if name not in PLATFORM_FILES.values() and name != "weibo.md" or name in selected]
    if "weibo" in scope:
        names.append("weibo.md")
    if (pack / "image-count.json").exists():
        names.append("image-count.json")
    for name in ["distribution-plan.json", *names, *[f"images/{name}" for name in five_image_names(pack)]]:
        path = local_path(pack, name)
        files.append({"path": name, "sha256": file_sha256(path) if path.is_file() else None})
    board_path = local_path(pack, "image-storyboard.json")
    board = read_object(board_path) if board_path.is_file() else {}
    inputs = {"files": files, "policy_sha256": file_sha256(policy_path),
              "compiler_sha256": file_sha256(Path(__file__)),
              "editorial_advisor_sha256": file_sha256(ROOT / "scripts/editorial_advisor.py"),
              "experiment_advisor_sha256": file_sha256(ROOT / "scripts/editorial_experiments.py"),
              "image_copy_reviewer_sha256": file_sha256(ROOT / "scripts/image_copy_review.py"),
              "experiment_policy_sha256": file_sha256(EXPERIMENT_POLICY),
              "section_parser_sha256": file_sha256(ROOT / "scripts/image_quality_gate.py"),
              "character_reference": reference_info(pack, board.get("character_reference"), project_root)}
    board_items = board.get("items")
    if isinstance(board_items, list) and any(isinstance(row, dict) and "source_image" in row for row in board_items):
        inputs["source_image_advisor_sha256"] = file_sha256(ROOT / "scripts/source_image_advisor.py")
    return {**inputs, "fingerprint": canonical_sha256(inputs)}


def visual_explanation(item: dict[str, Any]) -> dict[str, Any]:
    fields = ("subject", "relationship", "reader_takeaway", "avoid_implication")
    supplied = item.get("visual_explanation")
    values = supplied if isinstance(supplied, dict) else {}
    missing = [key for key in fields if not isinstance(values.get(key), str) or not values[key].strip()]
    return {
        "availability": "not_provided" if supplied is None else "incomplete" if missing else "specified",
        "author_fields": {key: values[key] for key in fields if isinstance(values.get(key), str)},
        "missing_fields": missing,
        "authority": "author_intent_only_not_verified", "fact_verification_performed": False,
    }


def visual_evidence_review(item: dict[str, Any], verification: dict[str, Any]) -> dict[str, Any]:
    """Resolve declared links only; original source and numeric gates still own verification."""
    instructions = {
        "source_fact": "Show only the linked source-scoped fact; do not invent an improvement, architecture, ranking or product UI.",
        "analogy": "This is an explanatory analogy, not a measured result or literal product architecture. Preserve the author's avoid_implication boundary.",
        "hypothetical": "This is a hypothetical task, not an actual test, customer result or personal experience. Preserve the author's avoid_implication boundary.",
        "instruction": "This is editorial action guidance, not an official requirement or a promise of success. Preserve the author's avoid_implication boundary.",
    }
    mode = item.get("evidence_mode")
    result: dict[str, Any] = {"mode": mode, "status": "not_provided", "findings": [], "claim_links": [],
                              "fact_verification_performed": False, "actual_image_review": "not_performed",
                              "authority": "declared_intent_and_link_inspection_only", "prompt_constraint": ""}

    def issue(code: str, action: str) -> None:
        result["findings"].append({"code": code, "severity": "advisory",
                                   "reference": str(item.get("image", "")), "action": action})

    if mode is None:
        issue("BRIEF_EVIDENCE_MODE_OPTIONAL", "For new storyboards, declare source_fact, analogy, hypothetical or instruction. Existing packs need no migration.")
        return result
    if not isinstance(mode, str) or mode not in instructions:
        issue("BRIEF_EVIDENCE_MODE_INVALID", "Choose one supported evidence mode; no score or string can certify factual accuracy.")
        result["status"] = "needs_review"
        return result
    result["prompt_constraint"] = instructions[mode]
    if mode != "source_fact" and visual_explanation(item)["availability"] != "specified":
        issue("BRIEF_NONLITERAL_BOUNDARY_REQUIRED", "Describe subject, relationship, takeaway and avoid_implication before generation. Never present a metaphor or hypothetical scene as source evidence.")
    refs = item.get("claim_ids", [])
    valid_refs = isinstance(refs, list) and all(isinstance(ref, str) and ref.strip() for ref in refs)
    if not valid_refs or len(set(refs)) != len(refs):
        issue("BRIEF_CLAIM_LINK_INVALID", "Use unique nonempty claim IDs, not invented evidence or malformed values.")
        refs = []
    if mode == "source_fact" and not refs:
        issue("BRIEF_FACT_CLAIM_REQUIRED", "Link the visible factual message to its claim IDs, even when the image has no numerical labels.")
    claims = verification.get("claims")
    claims = claims if isinstance(claims, list) else []
    for ref in refs:
        matches = [row for row in claims if isinstance(row, dict) and row.get("claim_id") == ref]
        if len(matches) != 1:
            issue("BRIEF_CLAIM_LINK_UNRESOLVED", f"Claim ID {ref!r} is missing or ambiguous; resolve it before treating the image as factual.")
            continue
        claim = matches[0]
        declared = claim.get("verdict")
        result["claim_links"].append({"claim_id": ref, "declared_verdict": declared,
                                      "text": claim.get("text"), "source_ids": claim.get("source_ids"),
                                      "status": "resolved_only"})
        if declared != "verified":
            issue("BRIEF_CLAIM_NOT_VERIFIED", f"Claim {ref!r} is not marked verified. Reconcile the claim and image; a corrected claim needs review of its final wording.")
    result["status"] = "needs_review" if result["findings"] else "specified_for_source_and_visual_review"
    if result["findings"]:
        result["prompt_constraint"] += " Unresolved authoring findings: stop for review before generation; do not fill missing evidence."
    return result


def compile_brief(pack: Path, policy_path: Path = POLICY, project_root: Path = ROOT) -> dict[str, Any]:
    pack = pack.resolve()
    before = input_snapshot(pack, policy_path, project_root)
    policy = read_object(policy_path)
    experiment_policy = read_object(EXPERIMENT_POLICY)
    cards, articles = platform_contexts(pack, policy)
    board_path = local_path(pack, "image-storyboard.json")
    board = read_object(board_path) if board_path.is_file() else {}
    claims_path = local_path(pack, "claim-verification.json")
    verification = read_object(claims_path) if claims_path.is_file() else {}
    article = articles["wechat"]
    blocks = visible_blocks(article)
    sections = {platform: image_section_contexts(text) for platform, text in articles.items()
                if platform != "xiaohongshu"}
    issues: list[dict[str, str]] = []

    def issue(code: str, reference: str, action: str, severity: str = "needs_input") -> None:
        issues.append({"code": code, "reference": reference, "action": action, "severity": severity})

    if not blocks:
        issue("BRIEF_ARTICLE_REQUIRED", "wechat-article.md", "Write the source-grounded article first.")
    for card in cards:
        context = card["current_draft"]
        issues.extend(card["editorial_review"]["findings"])
        if context["availability"] != "present":
            issue("BRIEF_PLATFORM_COPY_PENDING", card["file"], "Write this platform's copy using the shared facts and its reader task.", "advisory")
        for paragraph in context["paragraphs"]:
            if paragraph["density_character_count"] > policy["paragraph_review_threshold"]:
                issue("BRIEF_PARAGRAPH_DENSITY_REVIEW", f"{card['file']}:{paragraph['line']}",
                      "Check sentence jobs and split only where meaning permits; preserve evidence and qualifications.", "advisory")
    shared_bodies = []
    for index, left in enumerate(cards):
        fingerprint = left["current_draft"]["body_fingerprint"]
        for right in cards[index + 1:]:
            if fingerprint and fingerprint == right["current_draft"]["body_fingerprint"]:
                pair = [left["platform"], right["platform"]]
                shared_bodies.append(pair)
                issue("BRIEF_SHARED_BODY_REVIEW", ",".join(pair),
                      "Visible body text is identical after excluding H1 titles. Check platform adaptation; preserve shared facts and the experiment control, not uniqueness for its own sake.", "advisory")
    backup_path = local_path(pack, "weibo.md")
    backup_required = "weibo" in inspection_platform_ids(pack)
    backup_text = read_text(backup_path) if backup_required and backup_path.is_file() else ""
    backup_context = draft_context(backup_text, backup_required and backup_path.is_file())
    backup_review = review_prose(backup_text, "weibo.md")
    issues.extend(backup_review["findings"])
    if type(board.get("schema_version")) is not int or board.get("schema_version") != 1:
        issue("BRIEF_STORYBOARD_SCHEMA", "image-storyboard.json", "Use the current storyboard schema version 1.")
    reference = before["character_reference"]
    if not reference["available"]:
        issue(reference["reason"], "character_reference", "Select a readable local character image within the project.")
    palette = board.get("palette")
    if not isinstance(palette, str) or not palette.strip():
        issue("BRIEF_PALETTE_REQUIRED", "palette", "Specify one shared series palette.")
        palette = ""

    raw_items = board.get("items")
    items = raw_items if isinstance(raw_items, list) else []
    names = five_image_names(pack)
    if len(items) != len(names):
        issue("BRIEF_FIVE_ROLES_REQUIRED", "items", "Fill all declared ordered storyboard roles.")
    roles = [item.get("role") if isinstance(item, dict) else None for item in items]
    if not roles or roles[0] != "cover" or [role for role in roles if role != "supplement"] != list(ROLES):
        issue("BRIEF_ROLE_OR_NAME", "items", "Keep the five core roles in order, with optional supplements between them.")
    prompts = []
    messages = []
    layouts = []
    for index, item in enumerate(items[:len(names)]):
        location = f"items[{index}]"
        if not isinstance(item, dict):
            issue("BRIEF_ITEM_INVALID", location, "Use an object for each image brief.")
            continue
        expected_image = f"images/{names[index]}"
        role = item.get("role") if isinstance(item.get("role"), str) else "supplement"
        if item.get("image") != expected_image:
            issue("BRIEF_ROLE_OR_NAME", location, "Restore the fixed role order and date-based image path.")
        for field in ("section_anchor", "message", "layout", "prompt"):
            if not isinstance(item.get(field), str) or not item[field].strip():
                issue("BRIEF_FIELD_REQUIRED", f"{location}.{field}", "Write a specific author decision; do not auto-fill a passing review.")
        if not isinstance(item.get("on_image_text"), str):
            issue("BRIEF_LABELS_REQUIRED", location, "List exact bitmap text, or an empty string for a text-free image.")
        anchor = item.get("section_anchor")
        if isinstance(anchor, str) and anchor and not any(anchor in b["text"] for b in blocks):
            issue("BRIEF_ANCHOR_NOT_VISIBLE", location, "Bind to visible article text, not code or alt text.")
        placed_contexts = sections["wechat"].get(expected_image, [])
        placement = "placed" if placed_contexts else "not_placed"
        if len(placed_contexts) > 1:
            placement = "ambiguous"
            issue("BRIEF_IMAGE_PLACEMENT_AMBIGUOUS", expected_image, "Keep each image in one intended section before generating or reviewing it.")
        elif placed_contexts and isinstance(anchor, str) and anchor not in placed_contexts[0]:
            placement = "anchor_mismatch"
            issue("BRIEF_ANCHOR_SECTION_MISMATCH", expected_image, "Check the image's actual section, not another section containing the anchor.")
        elif not placed_contexts:
            issue("BRIEF_IMAGE_PLACEMENT_PENDING", expected_image, "Place this image reference in its intended section; existence of the anchor alone does not establish placement.", "advisory")
        labels = item.get("on_image_text") if isinstance(item.get("on_image_text"), str) else ""
        width, height = (900, 383) if index == 0 else (1080, 720)
        role_policy = policy["roles"].get(role, {"reader_question": "What additional evidence or context does this image provide?", "structure_hint": "Use a source-bound, clearly labelled supplementary visual."})
        art_direction = experiment_policy["visual_direction"]["roles"].get(role, "Provide additional source-bound context distinct from the other images.")
        messages.append(item.get("message"))
        layouts.append(item.get("layout"))
        label_instruction = ("Render exactly these labels, preserving every value, unit and qualifier:\n" + labels
                             if labels else "Text-free illustration: do not render any letters, numbers or labels.")
        explanation = visual_explanation(item)
        evidence_review = visual_evidence_review(item, verification)
        issues.extend(evidence_review["findings"])
        source_image_review = review_source_image(item, article)
        for code in source_image_review["issues"]:
            issue(code, expected_image, "Complete the source and adjacent caption declaration; independently check rights and facts.", "advisory")
        copy_review = review_image_copy(item, width, height)
        issues.extend(copy_review["findings"])
        if (copy_review["status"] in {"not_provided", "invalid"}
                and len(labels) > policy["label_review_threshold"]):
            issue("BRIEF_LABEL_DENSITY_REVIEW", location, "Review label density at phone size; move explanations into the caption without silently truncating labels.", "advisory")
        if explanation["availability"] != "specified":
            issue("BRIEF_VISUAL_EXPLANATION_REVIEW", f"{location}.visual_explanation",
                  "Optional authoring aid: specify the subject, visible relationship, reader takeaway and implication to avoid. Existing prompts remain authoritative; do not invent facts to fill fields.", "advisory")
        explanation_prompt = []
        if explanation["availability"] == "specified":
            explanation_prompt = [
                "Author-specified visual explanation (not verified; not additional on-image text):\n"
                + "\n".join(f"{key}: {value}" for key, value in explanation["author_fields"].items())
                + "\nIf this conflicts with labels, source scope or the chosen layout, stop for author review; do not resolve by inventing facts."
            ]
        final_prompt = "\n\n".join([
            f"Huazi editorial series, image {index + 1}/{len(names)}. Target PNG: {width}x{height}.",
            f"Character reference: {reference.get('path', 'MISSING: select a reference before generation')}\nShared palette: {palette}",
            "Author-supplied brief (not approved by this compiler; preserve scope and verify separately):\n" + str(item.get("prompt", "")),
            f"One message: {item.get('message', '')}\nChosen layout: {item.get('layout', '')}",
            *explanation_prompt,
            "Evidence mode (not extra bitmap labels):\n" + (evidence_review["prompt_constraint"] or "Not specified; review whether the image is factual, illustrative or hypothetical before generation."),
            label_instruction,
            *([copy_review["prompt_instruction"]] if copy_review["prompt_instruction"] else []),
            "Series constraints:\n" + "\n".join(policy["shared_constraints"]),
            "Professional art direction (not extra labels; author layout remains authoritative):\n"
            + "\n".join(experiment_policy["visual_direction"]["shared"])
            + "\n" + art_direction,
        ])
        prompts.append({"image": expected_image, "role": role, "width": width, "height": height,
                        "message": item.get("message", ""),
                        "section_anchor": anchor, "on_image_text": labels,
                        "claim_ids": item.get("claim_ids", []), "source_prompt": item.get("prompt", ""),
                        "reader_question": role_policy["reader_question"],
                        "structure_suggestion_only": role_policy["structure_hint"],
                        "visual_explanation": explanation,
                        "evidence_review": evidence_review,
                        "source_image_review": source_image_review,
                        "copy_review": copy_review,
                        "art_direction": art_direction,
                        "article_context_for_review_only": {
                            "placement": placement,
                            "anchor_matches": [b for b in blocks if isinstance(anchor, str) and anchor and anchor in b["text"]],
                            "platform_sections": {platform: contexts.get(expected_image, [])
                                                  for platform, contexts in sections.items()},
                            "fact_verification_performed": False,
                        },
                        "final_prompt": final_prompt})
    if len(messages) == 5 and len({m for m in messages if isinstance(m, str)}) < 5:
        issue("BRIEF_MESSAGE_REPEATED", "items", "Give each image a different information job; compare meanings manually.", "advisory")
    if len(layouts) == 5 and all(isinstance(x, str) for x in layouts) and len(set(layouts)) == 1:
        issue("BRIEF_LAYOUT_MONOTONY", "items", "Review whether different roles need different layouts; do not vary style arbitrarily.", "advisory")
    for index, left in enumerate(prompts):
        intent = left["visual_explanation"]
        if intent["availability"] != "specified":
            continue
        for right in prompts[index + 1:]:
            other = right["visual_explanation"]
            if other["availability"] == "specified" and intent["author_fields"]["relationship"].strip() == other["author_fields"]["relationship"].strip():
                issue("BRIEF_SHARED_VISUAL_RELATION_REVIEW", f"{left['image']},{right['image']}",
                      "These images specify the same visible relationship. Check whether each role adds information; shared identity is expected and exact text matching is not semantic or visual verification.", "advisory")
    paragraphs = [b["text"] for b in blocks if b["kind"] == "paragraph"]
    experiment_path = local_path(pack, "growth-experiment.json")
    experiment = read_object(experiment_path) if experiment_path.is_file() else None
    if experiment is None:
        issue("BRIEF_EXPERIMENT_PENDING", "growth-experiment.json", "Complete ranked single-variable experiment before drafting variants.", "advisory")
    test_path = local_path(pack, "editorial-experiment.json")
    test_plan = read_object(test_path) if test_path.is_file() else None
    delivery_review = review_delivery(article, test_plan.get("delivery_anchors") if test_plan else None)
    issues.extend(delivery_review["findings"])
    freeze_names = {"image-storyboard.json", "claim-verification.json", "growth-experiment.json",
                    *[f"images/{name}" for name in names]}
    if (pack / "image-count.json").exists():
        freeze_names.add("image-count.json")
    frozen_inputs = {row["path"]: row["sha256"] for row in before["files"] if row["path"] in freeze_names}
    frozen_inputs["article_except_opening"] = opening_context(article)["frozen_body_sha256"]
    frozen_inputs["character_reference"] = before["character_reference"].get("sha256")
    experiment_review = review_experiment(test_plan, experiment, article, frozen_inputs, experiment_policy)
    issues.extend(experiment_review["findings"])
    after = input_snapshot(pack, policy_path, project_root)
    if before != after:
        raise BriefInputError("BRIEF_INPUT_CHANGED")
    result = {
        "schema_version": 2, "policy_id": policy["policy_id"], "inputs": before,
        "status": "needs_input" if any(i["severity"] == "needs_input" for i in issues) else "prepared",
        "authority": "authoring_aid_only_not_fact_visual_or_draft_approval",
        "generation_performed": False, "visual_review_performed": False,
        "article_opening": paragraphs[:2], "editing_invariants": policy["editing_invariants"],
        "paragraph_jobs": ["verified_fact", "explanation", "labeled_example", "action_or_boundary"],
        "editing_instruction": "Assign a useful job to each paragraph; remove duplication, not verified scope. Review the opening for dated news and reader relevance.",
        "experiment_reference": experiment,
        "editorial_experiment": experiment_review,
        "reader_delivery_review": delivery_review,
        "platform_cards": cards, "identical_body_pairs": shared_bodies,
        "weibo_short_backup": {"file": "weibo.md", "required": backup_required, "posting_action": "none", "current_draft": backup_context,
                               "editorial_review": backup_review},
        "visual_review_queue": [{"image": p["image"], "role": p["role"],
                                 "message": p["message"],
                                 "labels": p["on_image_text"],
                                 "author_intent": p["visual_explanation"],
                                 "evidence_review": p["evidence_review"],
                                 "copy_review": p["copy_review"],
                                 "review_questions": [p["reader_question"],
                                     "Does the rendered image actually explain its message at phone size?",
                                     "Could an arrow, chart, badge or metaphor imply an unverified result?",
                                     p["art_direction"]],
                                 "actual_image_review": "not_performed"}
                                for p in prompts],
        "image_prompts": prompts, "issues": issues,
    }
    return {**result, "brief_id": "brief-" + canonical_sha256(result)[:24]}


def brief_is_current(pack: Path, brief: dict[str, Any], policy_path: Path = POLICY, project_root: Path = ROOT) -> bool:
    try:
        return brief == compile_brief(pack, policy_path, project_root)
    except (OSError, ValueError, TypeError, KeyError):
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pack", required=True)
    parser.add_argument("--no-write", action="store_true", help="Print the brief without writing or backing up the pack")
    args = parser.parse_args(argv)
    pack = Path(args.pack).resolve()
    try:
        if not pack.is_dir():
            raise BriefInputError("BRIEF_PACK_MISSING")
        if args.no_write:
            result = compile_brief(pack)
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            with pack_lock(pack, "content-visual-brief"):
                if (pack / OUTPUT).is_symlink():
                    raise BriefInputError("BRIEF_OUTPUT_INVALID")
                output = local_path(pack, OUTPUT)
                if output.exists() and not output.is_file():
                    raise BriefInputError("BRIEF_OUTPUT_INVALID")
                result = compile_brief(pack)
                create_pack_backup(pack, "content-visual-brief", [OUTPUT], keep=10)
                if not brief_is_current(pack, result):
                    raise BriefInputError("BRIEF_INPUT_CHANGED")
                atomic_write_json(output, result)
            print(json.dumps({"brief_id": result["brief_id"], "status": result["status"], "path": str(output),
                              "issues": result["issues"]}, ensure_ascii=False, indent=2))
        return 0 if result["status"] == "prepared" else 1
    except (OSError, ValueError, TypeError, KeyError, PackLockError) as exc:
        print(f"Content visual brief stopped: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
