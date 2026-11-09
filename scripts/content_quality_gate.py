#!/usr/bin/env python3
"""Deterministic content-quality checks for Huazi multi-platform copy."""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


SCRIPT_ROOT = Path(__file__).resolve().parent
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

from distribution_io import atomic_write_json, canonical_sha256, create_pack_backup, file_sha256, five_image_names, pack_date_prefix, pack_lock  # noqa: E402
from workflow_policy import ACTIVE_PLATFORMS, VIDEO_PLATFORMS, POLICY as WORKFLOW_POLICY  # noqa: E402
from workflow_policy import inspection_platform_ids, plan_platform_ids, scope_matches_daily_policy
from editorial_advisor import prose_blocks  # noqa: E402


PROJECT_ROOT = SCRIPT_ROOT.parent
DEFAULT_STANDARD = PROJECT_ROOT / "config" / "content-quality-standard.json"
MARKDOWN_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
MARKDOWN_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
TITLE_LINE = re.compile(r"^#\s+(.+?)\s*$", flags=re.M)
LIST_LINE = re.compile(r"^\s*(?:[-*+]\s+|\d+[.、)]\s*)", flags=re.M)
HEADING_LINE = re.compile(r"^##+\s+", flags=re.M)
LATIN_WORD = re.compile(r"[A-Za-z][A-Za-z0-9.+/-]*")


class ContentInputError(ValueError):
    def __init__(self, code: str, reference: str):
        self.code = code
        self.reference = Path(reference).name
        super().__init__(f"{code}: {self.reference}")


def local_bytes(pack: Path, name: str) -> bytes | None:
    path = pack / name
    try:
        resolved = path.resolve()
        if not resolved.is_relative_to(pack.resolve()):
            raise ContentInputError("CONTENT_INPUT_OUTSIDE_PACK", name)
        if not resolved.exists():
            return None
        return resolved.read_bytes()
    except OSError as exc:
        raise ContentInputError("CONTENT_INPUT_UNREADABLE", name) from exc


def local_text(pack: Path, name: str) -> tuple[str, str]:
    raw = local_bytes(pack, name)
    if raw is None:
        return "", ""
    try:
        return raw.decode("utf-8"), hashlib.sha256(raw).hexdigest()
    except UnicodeError as exc:
        raise ContentInputError("CONTENT_INPUT_UNREADABLE", name) from exc


def quality_scope(pack: Path) -> tuple[list[str], bool, list[str]]:
    raw = local_bytes(pack, "distribution-plan.json")
    daily = (pack_date_prefix(pack) or "") >= WORKFLOW_POLICY["effective_from"]
    valid = True
    ids = plan_platform_ids(pack)
    if raw is not None:
        try:
            plan = json.loads(raw)
            rows = plan.get("items") if isinstance(plan, dict) else None
            valid = isinstance(rows, list) and bool(rows) and all(isinstance(row, dict) for row in rows)
            ids = [row.get("platform_id") for row in rows] if valid else []
            valid = valid and all(isinstance(pid, str) and pid.strip() for pid in ids)
            valid = valid and len(set(ids)) == len(ids)
        except (ValueError, UnicodeError):
            valid, ids = False, []
    if daily:
        valid = valid and raw is not None and scope_matches_daily_policy(pack, ids)
        ids = inspection_platform_ids(pack)
    elif not valid:
        ids = []
    platforms = [value for pid in ids for value in (["weibo_article", "weibo"] if pid == "weibo" else [pid.replace("-", "_")])]
    return platforms, bool(set(ids) & VIDEO_PLATFORMS), [] if valid else ["CONTENT_PLAN_SCOPE_INVALID"]


def inspection_snapshot(pack: Path, policy: dict[str, Any]) -> list[dict[str, Any]]:
    platforms, video, _ = quality_scope(pack)
    names = ["distribution-plan.json"] + [str(spec["file"]) for pid, spec in policy["platforms"].items() if pid in platforms]
    if (pack / "image-count.json").exists():
        names.append("image-count.json")
    if video:
        names.append(str(policy["video_script"]["file"]))
    return [{"path": name, "sha256": hashlib.sha256(raw).hexdigest() if raw is not None else ""}
            for name in names for raw in [local_bytes(pack, name)]]


def load_standard(path: Path = DEFAULT_STANDARD) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload.get("platforms"), dict) or not payload.get("policy_id"):
        raise ValueError("content quality standard is invalid")
    return payload


def clean_markdown(text: str) -> str:
    text = MARKDOWN_IMAGE.sub("", text)
    text = MARKDOWN_LINK.sub(r"\1", text)
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.M)
    text = re.sub(r"[*_`>]", "", text)
    return text.strip()


def compact_length(text: str) -> int:
    return len(re.sub(r"\s+", "", clean_markdown(text)))


def visual_title_length(title: str) -> int:
    latin_spans = list(LATIN_WORD.finditer(title))
    covered: set[int] = set()
    for match in latin_spans:
        covered.update(range(match.start(), match.end()))
    non_latin = sum(1 for index, char in enumerate(title) if index not in covered and not char.isspace())
    return non_latin + len(latin_spans)


def split_title_body(text: str) -> tuple[str, str]:
    match = TITLE_LINE.search(text)
    title = match.group(1).strip() if match else ""
    body = text[match.end():].strip() if match else text.strip()
    return title, body


def first_meaningful_text(body: str, limit: int = 120) -> str:
    lines = []
    for raw in body.splitlines():
        line = clean_markdown(raw).strip()
        if not line or line.startswith("核验日期：") or line.startswith("事实来源："):
            continue
        lines.append(line)
        if compact_length("".join(lines)) >= limit:
            break
    return "".join(lines)[:limit]


def paragraphs(body: str) -> list[str]:
    return [clean_markdown(item).strip() for item in re.split(r"\n\s*\n", body) if clean_markdown(item).strip()]


def contains_any(text: str, patterns: list[str]) -> bool:
    return any(pattern in text for pattern in patterns)


def editorial_enabled(pack: Path, standard: dict[str, Any]) -> bool:
    upgrade = standard.get("editorial_upgrade", {})
    return bool(upgrade) and (pack_date_prefix(pack) or "") >= upgrade["effective_from"]


def repeated_paragraphs(body: str, minimum: int = 30, threshold: float = 0.94) -> list[dict[str, Any]]:
    values = [(index + 1, re.sub(r"\s+", "", text)) for index, text in enumerate(paragraphs(body))]
    values = [(i, text) for i, text in values if len(text) >= minimum]
    duplicates = []
    for offset, (index, left) in enumerate(values):
        for other_index, right in values[offset + 1:]:
            ratio = difflib.SequenceMatcher(None, left, right, autojunk=False).ratio()
            if ratio >= threshold:
                duplicates.append({"paragraphs": [index, other_index], "similarity": round(ratio, 3)})
    return duplicates


def platform_item(pack: Path, platform_id: str, spec: dict[str, Any], standard: dict[str, Any],
                  document: tuple[str, str] | None = None) -> dict[str, Any]:
    path = pack / str(spec["file"])
    text, source_hash = document if document is not None else local_text(pack, str(spec["file"]))
    if not source_hash:
        return {
            "platform_id": platform_id,
            "source": str(path),
            "source_sha256": "",
            "score": 0,
            "passed": False,
            "blocking": ["content_file_missing"],
            "warnings": [],
            "metrics": {},
        }

    title, body = split_title_body(text)
    title_length = visual_title_length(title)
    body_length = compact_length(body)
    headings = len(HEADING_LINE.findall(text))
    list_items = len(LIST_LINE.findall(body))
    image_count = len(MARKDOWN_IMAGE.findall(text))
    first_120 = first_meaningful_text(body)
    body_plain = clean_markdown(body)
    paragraph_lengths = [compact_length(item) for item in paragraphs(body)]
    max_paragraph = max(paragraph_lengths, default=0)
    english_title_terms = len(LATIN_WORD.findall(title))

    checks = {
        "title_length": spec["title_max"] == 0 or spec["title_min"] <= title_length <= spec["title_max"],
        "body_length": spec["body_min"] <= body_length <= spec["body_max"],
        "heading_structure": headings >= spec["min_headings"],
        "list_structure": list_items >= spec["min_list_items"],
        "image_references": image_count == (len(five_image_names(pack)) if platform_id == "wechat" else spec["required_images"]) if spec["required_images"] else True,
        "readable_paragraphs": max_paragraph <= spec["max_paragraph"],
    }
    blocking = [name for name, passed in checks.items() if not passed]
    warnings: list[str] = []
    upgraded = editorial_enabled(pack, standard)
    repetition = []
    if upgraded:
        rules = standard["editorial_upgrade"]
        repetition = repeated_paragraphs(body, rules["minimum_repeat_length"], rules["near_repeat_similarity"])
        if repetition:
            blocking.append("CONTENT_REPEATED_PARAGRAPHS")

    if platform_id in {"wechat", "toutiao", "zhihu", "weibo_article"}:
        if not contains_any(first_120, standard["hook_patterns"]):
            # A fact-led news opening is valid. Keyword absence is a review cue,
            # not evidence that an article needs an invented conflict.
            (warnings if upgraded else blocking).append(
                "opening_review_news_relevance" if upgraded else "opening_has_no_reader_tension")
        if not contains_any(body_plain, standard["scenario_patterns"]):
            blocking.append("concrete_scenario_missing")
        if not contains_any(body_plain, standard["risk_patterns"]):
            blocking.append("risk_boundary_missing")
    ending = ""
    if platform_id == "wechat":
        # References and disclosures must neither hide nor supply the action.
        ending = ("\n".join(block["text"] for block in prose_blocks(body)
                            if block["kind"] == "paragraph") if upgraded else body_plain)[-260:]
        if not contains_any(ending, standard["action_patterns"]):
            blocking.append("self_contained_action_missing")
    if english_title_terms > 3:
        warnings.append("title_english_terms_overloaded")
    for opener in standard["soft_openers"]:
        if opener in first_120:
            warnings.append(f"soft_opener:{opener}")

    score = max(0, 100 - len(set(blocking)) * 12 - len(set(warnings)) * 4)
    return {
        "platform_id": platform_id,
        "source": str(path),
        "source_sha256": source_hash,
        "score": score,
        "passed": not blocking,
        "blocking": sorted(set(blocking)),
        "warnings": sorted(set(warnings)),
        "checks": checks,
        "metrics": {
            "title": title,
            "title_visual_length": title_length,
            "title_english_terms": english_title_terms,
            "body_compact_length": body_length,
            "heading_count": headings,
            "list_item_count": list_items,
            "image_reference_count": image_count,
            "max_paragraph_length": max_paragraph,
            "opening_sample": first_120,
            "ending_sample": ending,
            "repeated_paragraphs": repetition,
        },
    }


def video_script_item(pack: Path, spec: dict[str, Any], standard: dict[str, Any],
                      document: tuple[str, str] | None = None) -> dict[str, Any]:
    path = pack / str(spec["file"])
    text, source_hash = document if document is not None else local_text(pack, str(spec["file"]))
    if not source_hash:
        return {
            "source": str(path), "source_sha256": "", "passed": False,
            "score": 0, "blocking": ["video_script_missing"], "warnings": [], "metrics": {},
        }
    narration_match = re.search(r"^##\s+旁白\s*$\n(.*?)(?=^##\s+|\Z)", text, flags=re.M | re.S)
    narration = narration_match.group(1).strip() if narration_match else ""
    narration_length = compact_length(narration)
    first_sentence = re.split(r"[。！？!?]", clean_markdown(narration), maxsplit=1)[0].strip()
    scene_match = re.search(r"^##\s+镜头\s*$\n(.*?)(?=^##\s+|\Z)", text, flags=re.M | re.S)
    scene_text = scene_match.group(1) if scene_match else ""
    scene_count = len(re.findall(r"^\s*\d+[.、)]", scene_text, flags=re.M))
    checks = {
        "narration_length": spec["narration_min"] <= narration_length <= spec["narration_max"],
        "three_second_hook_length": compact_length(first_sentence) <= spec["first_sentence_max"],
        "three_second_hook_has_tension": contains_any(first_sentence, standard["hook_patterns"]),
        "scene_count": spec["min_scenes"] <= scene_count <= spec["max_scenes"],
    }
    blocking = [name for name, passed in checks.items() if not passed]
    score = max(0, 100 - len(blocking) * 15)
    return {
        "source": str(path),
        "source_sha256": source_hash,
        "passed": not blocking,
        "score": score,
        "blocking": blocking,
        "warnings": [],
        "checks": checks,
        "metrics": {
            "narration_compact_length": narration_length,
            "first_sentence": first_sentence,
            "first_sentence_length": compact_length(first_sentence),
            "scene_count": scene_count,
        },
    }


def text_similarity(text_a: str, text_b: str) -> float:
    left = re.sub(r"\s+", "", clean_markdown(text_a))
    right = re.sub(r"\s+", "", clean_markdown(text_b))
    return round(difflib.SequenceMatcher(None, left, right, autojunk=False).ratio(), 3)


def build_report(pack: Path, standard: dict[str, Any] | None = None) -> dict[str, Any]:
    policy = standard or load_standard()
    before = []
    try:
        before = inspection_snapshot(pack, policy)
        report = _build_report(pack, policy)
        after = inspection_snapshot(pack, policy)
        analyzed = report["inputs"]
        expected = [{"path": str((pack / row["path"]).resolve()), "sha256": row["sha256"]} for row in before[1:]]
        if before != after or analyzed != expected:
            raise ContentInputError("CONTENT_INPUT_CHANGED_DURING_CHECK", "pack")
    except ContentInputError as exc:
        report = {
            "schema_version": 1, "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "policy_id": policy["policy_id"], "effective_from": policy["effective_from"],
            "pack": str(pack.resolve()), "pack_score": 0, "minimum_pack_score": policy["minimum_pack_score"],
            "passed": False, "blocking_count": 1, "items": [], "platform_ids": [],
            "video_script": {"required": False, "status": "not_evaluated", "blocking": []},
            "platform_similarity": [], "inputs": [], "input_fingerprint": canonical_sha256([]),
            "issues": [{"code": exc.code, "reference": exc.reference,
                        "action": "Keep inputs within the pack, stop concurrent edits, repair unreadable files and rerun quality checks under the pack lock."}],
        }
    report["inspection_inputs"] = before
    if editorial_enabled(pack, policy):
        report["editorial_policy_id"] = policy["editorial_upgrade"]["policy_id"]
        report["standard_sha256"] = canonical_sha256(policy)
        report["score_kind"] = "structural_checks_not_fact_or_traffic_score"
        report["report_id"] = report_identity(report)
    return report


def _build_report(pack: Path, policy: dict[str, Any]) -> dict[str, Any]:
    platform_ids, video_required, scope_issues = quality_scope(pack)
    names = [str(spec["file"]) for pid, spec in policy["platforms"].items() if pid in platform_ids]
    if video_required:
        names.append(str(policy["video_script"]["file"]))
    # Scores, opening samples and cross-platform comparisons use the same bytes.
    documents = {name: local_text(pack, name) for name in names}

    items = [
        platform_item(pack, platform_id, spec, policy, document=documents[str(spec["file"])])
        for platform_id, spec in policy["platforms"].items()
        if platform_id in platform_ids
    ]
    long_documents = {
        platform_id: documents[str(policy["platforms"][platform_id]["file"])]
        for platform_id in ("wechat", "toutiao", "zhihu", "weibo_article")
        if platform_id in platform_ids
    }
    similarity = []
    keys = list(long_documents)
    for index, left in enumerate(keys):
        for right in keys[index + 1:]:
            if long_documents[left][1] and long_documents[right][1]:
                ratio = text_similarity(long_documents[left][0], long_documents[right][0])
                similarity.append({"left": left, "right": right, "ratio": ratio, "passed": ratio < 0.88})

    video = (video_script_item(pack, policy["video_script"], policy, document=documents[str(policy["video_script"]["file"])])
             if video_required else {"required": False, "status": "not_required", "blocking": []})
    all_scores = [item["score"] for item in items] + ([video["score"]] if "score" in video else [])
    pack_score = round(sum(all_scores) / len(all_scores), 1) if all_scores else 0.0
    blocking_count = sum(len(item["blocking"]) for item in items) + len(video["blocking"])
    duplicate_pairs = [item for item in similarity if not item["passed"]]
    blocking_count += len(duplicate_pairs) + len(scope_issues)
    inputs = [
        {"path": str(Path(item["source"]).resolve()), "sha256": item["source_sha256"]}
        for item in items
    ]
    if "source" in video:
        inputs.append({"path": str(Path(video["source"]).resolve()), "sha256": video["source_sha256"]})
    if (pack / "image-count.json").exists():
        inputs.append({"path": str((pack / "image-count.json").resolve()), "sha256": file_sha256(pack / "image-count.json")})
    report = {
        "schema_version": 1,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "policy_id": policy["policy_id"],
        "effective_from": policy["effective_from"],
        "pack": str(pack.resolve()),
        "pack_score": pack_score,
        "minimum_pack_score": policy["minimum_pack_score"],
        "passed": blocking_count == 0 and pack_score >= policy["minimum_pack_score"],
        "blocking_count": blocking_count,
        "items": items,
        "platform_ids": platform_ids,
        "video_script": video,
        "platform_similarity": similarity,
        "inputs": inputs,
        "issues": [{"code": code, "reference": "distribution-plan.json",
                    "action": "Generate the complete current-policy plan; preserve valid historical scope without deleting results."} for code in scope_issues],
    }
    report["input_fingerprint"] = canonical_sha256(inputs)
    return report


def report_identity(report: dict[str, Any]) -> str:
    return canonical_sha256({k: v for k, v in report.items() if k not in {"report_id", "generated_at"}})


def report_is_current(report: dict[str, Any], pack: Path | None = None) -> bool:
    if not isinstance(report, dict) or not isinstance(report.get("pack"), str):
        return False
    root = (pack or Path(report["pack"])).resolve()
    if not root.is_dir() or Path(report["pack"]).resolve() != root:
        return False
    standard = load_standard()
    strict = editorial_enabled(root, standard)
    if strict:
        if (report.get("standard_sha256") != canonical_sha256(standard)
                or report.get("editorial_policy_id") != standard["editorial_upgrade"]["policy_id"]):
            return False
    inputs = report.get("inputs") if isinstance(report.get("inputs"), list) else []
    if not inputs or not all(isinstance(row, dict) for row in inputs):
        return False
    try:
        platforms, video, _ = quality_scope(root)
        names = [str(spec["file"]) for pid, spec in standard["platforms"].items() if pid in platforms]
        if video:
            names.append(str(standard["video_script"]["file"]))
        if (root / "image-count.json").exists():
            names.append("image-count.json")
        expected = []
        for name in names:
            path = (root / name).resolve()
            if root not in path.parents or not path.is_file():
                return False
            expected.append({"path": str(path), "sha256": file_sha256(path)})
        if inputs != expected or canonical_sha256(expected) != report.get("input_fingerprint"):
            return False
        if strict:
            current = build_report(root, standard)
            # A self-consistent checksum is not proof that a score is correct.
            return report.get("report_id") == report_identity(report) == current.get("report_id")
        return True
    except (OSError, ValueError, TypeError):
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate Huazi content quality")
    parser.add_argument("--pack", required=True)
    parser.add_argument("--output")
    parser.add_argument("--no-write", action="store_true")
    args = parser.parse_args()
    pack = Path(args.pack).resolve()
    if not pack.is_dir():
        print(f"Publish pack not found: {pack}", file=sys.stderr)
        return 2
    output = Path(args.output).resolve() if args.output else pack / "content-quality-report.json"
    if pack not in output.parents:
        parser.error("Output must be inside the publish pack")
    with pack_lock(pack, "content-quality"):
        report = build_report(pack)
        if not args.no_write:
            create_pack_backup(pack, "content-quality", [output.relative_to(pack).as_posix()], keep=10)
            atomic_write_json(output, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
