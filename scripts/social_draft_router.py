#!/usr/bin/env python3
"""Plan and dispatch Huazi content packs to domestic social platforms."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import unquote


SCRIPT_ROOT = Path(__file__).resolve().parent
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

from distribution_io import (  # noqa: E402
    FIVE_IMAGE_COUNT,
    LEGACY_FIVE_IMAGE_NAMES,
    atomic_write_json,
    canonical_sha256,
    create_pack_backup,
    append_chained_jsonl,
    file_bundle_snapshot,
    file_sha256,
    five_image_paths,
    image_file_probe,
    inspect_chained_jsonl,
    list_pack_backups,
    pack_date_prefix,
    pack_lock,
    require_valid_chained_jsonl,
    video_file_probe,
)
from content_compliance import build_report as build_compliance_report, load_rules as load_compliance_rules, scan_path  # noqa: E402
from content_workflow_gate import evaluate_pack as evaluate_content_workflow  # noqa: E402
from wechat_credentials import WECHAT_CREDENTIAL_NAMES, hydrate_wechat_environment  # noqa: E402


PROJECT_ROOT = SCRIPT_ROOT.parent
DEFAULT_REGISTRY = PROJECT_ROOT / "config" / "social-platforms.json"
from workflow_policy import ACTIVE_PLATFORMS, DISABLED_PLATFORMS, require_active

DEFAULT_PLATFORMS = ",".join(ACTIVE_PLATFORMS)
COMPLETE_RESULT_STATUSES = {"success", "draft_created", "local_draft_created"}
PARTIAL_RESULT_STATUSES = {"text_draft_created_cover_pending", "partial"}
BLOCKED_RESULT_STATUSES = {"blocked", "blocked_by_previous_draft", "needs_credentials"}
UNVERIFIED_RESULT_STATUSES = {"unverified", "unverified_editor_timeout", "failed"}
RESULT_SCHEMA_VERSION = 2
# Historical compatibility for undated packs and older test fixtures only.
CANONICAL_IMAGE_NAMES = list(LEGACY_FIVE_IMAGE_NAMES)
EVIDENCE_KINDS = {"api_response", "browser_visual", "browser_state", "account_state", "manual_check"}
IDENTITY_VOLATILE_FIELDS = {
    "generated_at", "updated_at", "started_at", "finished_at", "verified_at",
    "plan_file_sha256",
}

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def structured_evidence(result: dict[str, Any], pack: Path | None = None) -> dict[str, Any]:
    evidence = result.get("evidence") if isinstance(result.get("evidence"), dict) else {}
    kind = str(evidence.get("kind", ""))
    verified_at = str(evidence.get("verified_at", ""))
    summary = str(evidence.get("summary", "")).strip()
    reference = str(evidence.get("reference", "")).strip()
    stale = False
    try:
        timestamp = datetime.fromisoformat(verified_at.replace("Z", "+00:00"))
        age = datetime.now().astimezone() - timestamp
        timestamp_valid = timestamp.utcoffset() is not None and age >= -timedelta(minutes=5)
        stale = age > timedelta(hours=24)
    except (ValueError, TypeError):
        timestamp_valid = False
    artifact = {"local": False, "allowed": True, "exists": False, "sha256": "", "path": ""}
    if reference and not re.match(r"^[a-z][a-z0-9+.-]*://", reference, flags=re.I):
        suffix = Path(reference).suffix.lower()
        if suffix in {".png", ".jpg", ".jpeg", ".webp", ".json", ".md"}:
            artifact["local"] = True
            if pack is not None:
                root = pack.resolve()
                candidate = Path(reference)
                if not candidate.is_absolute():
                    candidate = root / candidate
                resolved = candidate.resolve()
                try:
                    relative = resolved.relative_to(root).as_posix()
                except ValueError:
                    artifact.update({"allowed": False, "path": f"external:{resolved.name}"})
                else:
                    exists = resolved.is_file()
                    artifact.update({
                        "path": relative,
                        "exists": exists,
                        "sha256": file_sha256(resolved) if exists else "",
                    })
    recorded_hash = str(evidence.get("sha256", ""))
    artifact["hash_matches"] = bool(recorded_hash) and recorded_hash == artifact["sha256"]
    return {
        "recorded": kind in EVIDENCE_KINDS and timestamp_valid and bool(summary),
        "reference_recorded": bool(reference),
        "kind": kind,
        "verified_at": verified_at,
        "summary": summary,
        "reference": reference,
        "artifact": artifact,
        "stale": stale,
        "sha256": recorded_hash,
    }


def evidence_fingerprint(item: dict[str, Any]) -> str:
    stable = json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(stable.encode("utf-8")).hexdigest()


def append_evidence_ledger(pack: Path, results: dict[str, Any], audit: dict[str, Any]) -> int:
    ledger_path = pack / "distribution-evidence.jsonl"
    anchor_path = pack / "distribution-evidence-chain.json"
    existing = require_valid_chained_jsonl(ledger_path, anchor_path)["records"]
    known = {str(row["fingerprint"]) for row in existing if row.get("fingerprint")}
    audit_index = {item.get("platform_id"): item for item in audit.get("items", [])}
    appended: list[dict[str, Any]] = []
    logged_at = datetime.now().astimezone().isoformat(timespec="seconds")
    for raw in results.get("items", []):
        platform_id = str(raw.get("platform_id", ""))
        evidence = structured_evidence(raw, pack)
        snapshot = {
            "schema_version": RESULT_SCHEMA_VERSION,
            "platform_id": platform_id,
            "status": raw.get("status", "missing_result"),
            "quality": audit_index.get(platform_id, {}).get("quality", "not_attempted"),
            "image_verified": raw.get("image_verified") is True,
            "video_verified": raw.get("video_verified") is True,
            "audio_verified": raw.get("audio_verified") is True,
            "cover_verified": raw.get("cover_verified") is True,
            "draft_verified": raw.get("draft_verified") is True,
            "evidence": evidence,
        }
        fingerprint = evidence_fingerprint(snapshot)
        if fingerprint in known:
            continue
        record = {"logged_at": logged_at, "fingerprint": fingerprint, **snapshot}
        appended.append(record)
        known.add(fingerprint)
    append_chained_jsonl(ledger_path, anchor_path, appended)
    return len(appended)


def load_registry(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data.get("platforms"), list):
        raise ValueError("platform registry must contain a platforms list")
    return data


def platform_index(registry: dict[str, Any]) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for platform in registry["platforms"]:
        keys = [platform["id"], platform["name"], *platform.get("aliases", [])]
        for key in keys:
            index[key.strip().lower()] = platform
    return index


def resolve_platforms(raw: str, registry: dict[str, Any]) -> list[dict[str, Any]]:
    index = platform_index(registry)
    resolved: list[dict[str, Any]] = []
    seen: set[str] = set()
    for token in re.split(r"[,，]", raw):
        key = token.strip().lower()
        if not key:
            continue
        platform = index.get(key)
        if not platform:
            raise ValueError(f"unknown platform: {token.strip()}")
        if platform["id"] not in seen:
            resolved.append(platform)
            seen.add(platform["id"])
    return resolved


def first_heading(path: Path) -> str:
    if not path.is_file():
        return ""
    match = re.search(r"^#\s+(.+)$", path.read_text(encoding="utf-8"), flags=re.M)
    return match.group(1).strip() if match else ""


def find_content(pack: Path, platform: dict[str, Any]) -> Path | None:
    for name in platform.get("content_files", []):
        candidate = pack / name
        if candidate.is_file() and candidate.stat().st_size > 0:
            return candidate
    return None


def png_size(path: Path) -> tuple[int, int] | None:
    probe = image_file_probe(path)
    if probe["decoded"] and probe["format"] == "PNG":
        return int(probe["width"]), int(probe["height"])
    return None


def markdown_images(path: Path) -> list[str]:
    if not path.is_file():
        return []
    return re.findall(r"!\[[^\]]*\]\(([^)]+)\)", path.read_text(encoding="utf-8"))


def resolve_markdown_image(article: Path, raw_ref: str) -> Path | None:
    ref = raw_ref.strip().strip("<>")
    if re.match(r"^[a-z][a-z0-9+.-]*://", ref, flags=re.I):
        return None
    ref = ref.split(" \"", 1)[0].split(" '", 1)[0]
    return (article.parent / unquote(ref)).resolve()


def markdown_body_chars(path: Path) -> int:
    if not path.is_file():
        return 0
    text = path.read_text(encoding="utf-8")
    text = re.sub(r"^#\s+.+$", "", text, count=1, flags=re.M)
    text = re.sub(r"!\[[^\]]*\]\([^)]+\)", "", text)
    text = re.sub(r"[`*_>#\-]", "", text)
    return len(re.sub(r"\s+", "", text))


def canonical_image_paths(pack: Path) -> list[Path]:
    return five_image_paths(pack)


def configured_cover_path(pack: Path, raw_path: str) -> Path:
    image_date = pack_date_prefix(pack)
    if "{pack_date}" in raw_path:
        if image_date:
            raw_path = raw_path.replace("{pack_date}", image_date)
        elif raw_path.replace("\\", "/").startswith("video/"):
            raw_path = raw_path.replace("{pack_date}", "short")
        else:
            raw_path = "images/00-cover.png"
    return pack / raw_path


def configured_video_path(pack: Path, raw_path: str) -> Path:
    video_date = pack_date_prefix(pack)
    if "{pack_date}" in raw_path:
        raw_path = raw_path.replace("{pack_date}", video_date) if video_date else "video/short-video.mp4"
    return pack / raw_path


def video_quality_status(pack: Path, video: Path | None, cover: Path | None) -> dict[str, Any]:
    report_path = pack / "video-quality-report.json"
    try:
        report = json.loads(report_path.read_text(encoding="utf-8")) if report_path.is_file() else {}
    except json.JSONDecodeError:
        report = {}
    current_video_sha = file_sha256(video) if video and video.is_file() else ""
    current_cover_sha = file_sha256(cover) if cover and cover.is_file() else ""
    current = (
        bool(current_video_sha)
        and bool(current_cover_sha)
        and report.get("video_sha256") == current_video_sha
        and report.get("cover_sha256") == current_cover_sha
    )
    return {
        "required": True,
        "path": str(report_path),
        "exists": report_path.is_file(),
        "report_id": str(report.get("report_id", "")),
        "passed": report.get("passed") is True and current,
        "current": current,
        "video_sha256": current_video_sha,
        "cover_sha256": current_cover_sha,
        "technical_probe": report.get("technical_probe", {}),
        "checks": report.get("checks", {}),
        "errors": report.get("errors", []),
    }


def plan_identity(plan: dict[str, Any]) -> str:
    return canonical_sha256({
        "schema_version": plan.get("schema_version"),
        "registry_verified_at": plan.get("registry_verified_at"),
        "pack": plan.get("pack"),
        "title": plan.get("title"),
        "draft_only_policy": plan.get("draft_only_policy"),
        "items": plan.get("items", []),
    })


def workflow_bundles(pack: Path, plan: dict[str, Any]) -> dict[str, Any]:
    content_paths = [Path(str(item.get("content"))) for item in plan.get("items", []) if item.get("content")]
    video_paths = [Path(str(item.get("video"))) for item in plan.get("items", []) if item.get("video")]
    inline_paths = [pack / video["path"] for item in plan.get("items", []) for video in item.get("inline_videos", [])]
    video_covers = [
        Path(str(item.get("cover")))
        for item in plan.get("items", [])
        if item.get("required_video_count") and item.get("cover")
    ]
    quality_gate_paths = [
        "topic-ranking.json",
        "growth-experiment.json",
        "fact-check-notes.md",
    ]
    from topic_candidate_ranker import DEFAULT_RUBRIC
    ranking_path = pack / "topic-ranking.json"
    # Schema v2 and new packs bind candidate input as part of the shared dry-run.
    try:
        ranking = json.loads(ranking_path.read_text(encoding="utf-8"))
        ranking = ranking if isinstance(ranking, dict) else {}
    except (OSError, ValueError):
        ranking = {}
    rubric = json.loads(DEFAULT_RUBRIC.read_text(encoding="utf-8"))
    if ((pack_date_prefix(pack) or "") >= rubric["integrity_required_from"]
            or "inputs" in ranking or ranking.get("schema_version") == 2):
        quality_gate_paths.append("topic-candidates.json")
    if video_paths:
        quality_gate_paths.append("video-quality-report.json")
    if inline_paths:
        quality_gate_paths.extend(["topic-request.json", "inline-video-review.json"])
    if video_paths and (pack / "video-audio-profile.json").is_file():
        quality_gate_paths.append("video-audio-profile.json")
    if (pack / "content-quality-report.json").is_file():
        quality_gate_paths.append("content-quality-report.json")
    if (pack / "claim-verification.json").is_file():
        quality_gate_paths.append("claim-verification.json")
        from source_evidence import snapshot_paths
        quality_gate_paths.extend(snapshot_paths(pack))
    if (pack / "research-evidence-report.json").is_file():
        quality_gate_paths.append("research-evidence-report.json")
    from image_quality_gate import required as image_quality_required
    if image_quality_required(pack):
        quality_gate_paths.extend(["image-storyboard.json", "image-visual-review.json", "image-quality-report.json"])
    return {
        "content": file_bundle_snapshot(pack, content_paths),
        "images": file_bundle_snapshot(pack, canonical_image_paths(pack)),
        "video_assets": file_bundle_snapshot(pack, video_paths + video_covers + inline_paths),
        "quality_gates": file_bundle_snapshot(
            pack,
            quality_gate_paths,
        ),
    }


def stable_identity_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: stable_identity_value(item)
            for key, item in value.items()
            if key not in IDENTITY_VOLATILE_FIELDS
        }
    if isinstance(value, list):
        return [stable_identity_value(item) for item in value]
    return value


def result_identity(results: dict[str, Any]) -> str:
    return canonical_sha256({
        "schema_version": results.get("schema_version"),
        "pack": results.get("pack"),
        "title": results.get("title"),
        "dry_run": results.get("dry_run"),
        "inputs": stable_identity_value(results.get("inputs", {})),
        "items": stable_identity_value(results.get("items", [])),
    })


def pack_images(pack: Path) -> list[Path]:
    images = pack / "images"
    if not images.is_dir():
        return []
    canonical = canonical_image_paths(pack)
    if all(path.is_file() for path in canonical):
        return canonical
    supported = {".png", ".jpg", ".jpeg", ".webp"}
    return sorted(path for path in images.iterdir() if path.is_file() and path.suffix.lower() in supported)


def locate_wechatsync() -> list[str] | None:
    executable = shutil.which("wechatsync")
    if executable:
        return [executable]

    dist = PROJECT_ROOT / "work" / "Wechatsync-latest" / "packages" / "cli" / "dist" / "index.js"
    bundled_node = (
        Path.home()
        / ".cache"
        / "codex-runtimes"
        / "codex-primary-runtime"
        / "dependencies"
        / "node"
        / "bin"
        / "node.exe"
    )
    node = os.environ.get("NODE_EXE") or shutil.which("node")
    if not node and bundled_node.is_file():
        node = str(bundled_node)
    if dist.is_file() and node:
        return [node, str(dist)]
    return None


def configured(requirements: list[str]) -> tuple[bool, list[str], str]:
    credential_source = ""
    if any(name in requirements for name in WECHAT_CREDENTIAL_NAMES):
        credential_source = hydrate_wechat_environment()
    missing = [name for name in requirements if not os.environ.get(name)]
    if requirements and not missing and not credential_source:
        credential_source = "environment"
    return not missing, missing, credential_source


def plan_item(
    pack: Path,
    title: str,
    platform: dict[str, Any],
    title_is_override: bool = False,
) -> dict[str, Any]:
    content = find_content(pack, platform)
    delivery = platform["delivery"]
    requirements = platform.get("requirements", [])
    from requested_topic import request_for_pack
    request = request_for_pack(pack) if platform["id"] == "wechat" else None
    if request and request.get("browser_video_draft") is True:
        delivery = "browser_draft"
        requirements = []
    requirements_ok, missing_env, credential_source = configured(requirements)
    bridge = locate_wechatsync() if delivery == "wechatsync_bridge" else None
    required_image_count = platform.get("required_image_count")
    images = pack_images(pack) if required_image_count is not None else []
    required_video_count = platform.get("required_video_count")
    video = configured_video_path(pack, platform["video"]) if platform.get("video") else None
    video_probe = video_file_probe(video) if video else {}
    canonical_images = canonical_image_paths(pack)
    if platform["id"] == "wechat" and (pack / "image-count.json").exists():
        required_image_count = len(canonical_images)
    configured_cover = configured_cover_path(pack, platform["cover"]) if platform.get("cover") else None
    cover = canonical_images[0] if required_image_count == 5 or platform["id"] == "wechat" else configured_cover
    if cover and not cover.is_file() and images and not pack_date_prefix(pack):
        cover = images[0]
    preferred_content = pack / platform.get("content_files", [""])[0]
    video_quality = (
        video_quality_status(pack, video, cover)
        if required_video_count is not None and platform.get("require_video_quality_report")
        else {"required": False, "passed": True, "current": True}
    )
    quality_probe = video_quality.get("technical_probe", {})
    if (
        required_video_count is not None
        and not video_probe.get("decoded")
        and video_probe.get("error") == "ffprobe_unavailable"
        and video_quality.get("passed")
        and quality_probe.get("decoded")
        and quality_probe.get("sha256") == video_quality.get("video_sha256")
    ):
        video_probe = {**quality_probe, "source": "current_video_quality_report"}
    platform_title = title if title_is_override else (first_heading(content) if content else "") or title
    body_chars = markdown_body_chars(content) if content else 0
    compliance_rules = load_compliance_rules()
    compliance = scan_path(content, platform_id=platform["id"], rules=compliance_rules) if content else {
        "policy_id": compliance_rules.get("policy_id", ""),
        "platform_id": platform["id"],
        "source": "",
        "passed": False,
        "finding_count": 0,
        "blocking_count": 0,
        "findings": [],
    }

    problems: list[str] = []
    if not content:
        problems.append("missing_content")
    if platform.get("require_dedicated_content") and (
        not preferred_content.is_file() or preferred_content.stat().st_size == 0
    ):
        problems.append(f"missing_dedicated_content:{preferred_content.name}")
    if cover and not cover.is_file():
        problems.append("missing_cover")
    if required_video_count is not None:
        actual_video_count = int(bool(video and video.is_file()))
        if actual_video_count != required_video_count:
            problems.append(f"requires_{required_video_count}_videos_found_{actual_video_count}")
        elif not video_probe.get("decoded"):
            problems.append(f"invalid_video:{video_probe.get('error') or 'decode_failed'}")
        else:
            expected_width = platform.get("expected_video_width")
            expected_height = platform.get("expected_video_height")
            if expected_width and expected_height and (
                video_probe.get("width"), video_probe.get("height")
            ) != (expected_width, expected_height):
                problems.append(
                    f"invalid_video_dimensions:{video_probe.get('width')}x{video_probe.get('height')}"
                    f"!={expected_width}x{expected_height}"
                )
            duration = float(video_probe.get("duration") or 0)
            minimum = float(platform.get("video_duration_min_seconds") or 0)
            maximum = float(platform.get("video_duration_max_seconds") or 0)
            if minimum and duration < minimum:
                problems.append(f"video_too_short:{duration:.1f}<{minimum:.1f}")
            if maximum and duration > maximum:
                problems.append(f"video_too_long:{duration:.1f}>{maximum:.1f}")
            if platform.get("require_audio") and not video_probe.get("has_audio"):
                problems.append("video_audio_missing")
        if cover and cover.is_file():
            expected_cover = (platform.get("cover_width"), platform.get("cover_height"))
            if all(expected_cover) and png_size(cover) != expected_cover:
                problems.append(
                    f"invalid_video_cover_dimensions:{png_size(cover)}"
                    f"!={expected_cover[0]}x{expected_cover[1]}"
                )
        if video_quality.get("required") and not video_quality.get("exists"):
            problems.append("video_quality_report_missing")
        elif video_quality.get("required") and not video_quality.get("current"):
            problems.append("video_quality_report_stale")
        elif video_quality.get("required") and not video_quality.get("passed"):
            problems.append("video_quality_report_failed")
    if required_image_count is not None and len(images) != required_image_count:
        problems.append(f"requires_{required_image_count}_images_found_{len(images)}")
    if required_image_count == FIVE_IMAGE_COUNT or platform["id"] == "wechat":
        missing_names = [path.name for path in canonical_images if not path.is_file()]
        if missing_names:
            problems.append("missing_canonical_images:" + ",".join(missing_names))
        invalid_dimensions = []
        image_hashes: dict[str, list[str]] = {}
        for index, path in enumerate(canonical_images, start=1):
            expected = (900, 383) if index == 1 else (1080, 720)
            probe = image_file_probe(path)
            actual = (probe["width"], probe["height"]) if probe["decoded"] else None
            if path.is_file() and (probe["format"] != "PNG" or actual != expected):
                invalid_dimensions.append(f"{path.name}!={expected[0]}x{expected[1]}")
            if probe["sha256"]:
                image_hashes.setdefault(probe["sha256"], []).append(path.name)
        if invalid_dimensions:
            problems.append("invalid_canonical_image_dimensions:" + ",".join(invalid_dimensions))
        duplicates = [names for names in image_hashes.values() if len(names) > 1]
        if duplicates:
            problems.append("duplicate_canonical_images:" + ";".join(",".join(names) for names in duplicates))
    title_min = platform.get("title_min_chars")
    title_max = platform.get("title_max_chars")
    if title_min is not None and len(platform_title) < title_min:
        problems.append(f"title_too_short:{len(platform_title)}<{title_min}")
    if title_max is not None and len(platform_title) > title_max:
        problems.append(f"title_too_long:{len(platform_title)}>{title_max}")
    content_min = platform.get("content_min_chars")
    content_max = platform.get("content_max_chars")
    if content_min is not None and body_chars < content_min:
        problems.append(f"content_too_short:{body_chars}<{content_min}")
    if content_max is not None and body_chars > content_max:
        problems.append(f"content_too_long:{body_chars}>{content_max}")
    for finding in compliance.get("findings", []):
        if finding.get("severity") == "block":
            problems.append(
                f"content_compliance:{finding.get('rule_id', 'unknown')}:line_{finding.get('line', 0)}"
            )
    if platform["id"] == "wechat" and content:
        refs = markdown_images(content)
        if len(refs) != len(canonical_images):
            problems.append(f"wechat_requires_{len(canonical_images)}_images_found_{len(refs)}")
        resolved_refs = [resolve_markdown_image(content, ref) for ref in refs]
        if any(path is None or not path.is_file() for path in resolved_refs):
            problems.append("wechat_contains_missing_or_remote_images")
        expected_refs = [path.resolve() for path in canonical_images]
        if resolved_refs != expected_refs:
            problems.append("wechat_images_must_follow_canonical_01_to_05_order")
        if cover and png_size(cover) != (900, 383):
            problems.append("wechat_cover_must_be_900x383")
    if problems:
        status = "blocked"
    elif delivery == "official_draft_api" and missing_env:
        status = "needs_credentials"
    elif delivery == "wechatsync_bridge" and not bridge:
        status = "needs_wechatsync_setup"
    elif delivery == "wechatsync_bridge" and not os.environ.get("WECHATSYNC_TOKEN"):
        status = "needs_wechatsync_token"
    elif delivery in {"browser_draft", "browser_publish"}:
        status = "manual_browser"
    elif delivery == "official_direct_publish_api":
        has_video = any(pack.rglob("*.mp4"))
        status = "ready_direct_publish" if requirements_ok and has_video else "needs_video_or_api_setup"
    else:
        status = "ready"

    return {
        "platform_id": platform["id"],
        "platform": platform["name"],
        "title": platform_title,
        "delivery": delivery,
        **({"request_id": request["request_id"]} if request else {}),
        **({"inline_videos": request["videos"]} if request and request.get("browser_video_draft") is True else {}),
        "public_api": platform.get("public_api", "unknown"),
        "status": status,
        "content": str(content) if content else "",
        "cover": str(cover) if cover and cover.is_file() else "",
        "image_count": len(images),
        "required_image_count": required_image_count,
        "images": [str(path) for path in images],
        "video": str(video) if video and video.is_file() else "",
        "video_count": int(bool(video and video.is_file())),
        "required_video_count": required_video_count,
        "video_probe": video_probe,
        "video_quality": video_quality,
        "bridge_id": platform.get("bridge_id", ""),
        "fallback": platform.get("fallback", ""),
        "automation_adapter": platform.get("automation_adapter", ""),
        "adapter_mode": platform.get("adapter_mode", ""),
        "editor_url": platform.get("editor_url", ""),
        "problems": problems,
        "missing_credentials": missing_env,
        "credential_source": credential_source,
        "content_chars": body_chars,
        "title_chars": len(platform_title),
        "dedicated_content": bool(content and content.resolve() == preferred_content.resolve()),
        "compliance": compliance,
        "notes": platform.get("notes", ""),
    }


def build_plan(
    pack: Path,
    title: str,
    platforms: list[dict[str, Any]],
    registry: dict[str, Any],
    title_is_override: bool = False,
) -> dict[str, Any]:
    plan = {
        "schema_version": 2,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "registry_verified_at": registry.get("verified_at", ""),
        "pack": str(pack),
        "title": title,
        "draft_only_policy": True,
        "items": [plan_item(pack, title, platform, title_is_override) for platform in platforms],
    }
    plan["plan_id"] = plan_identity(plan)
    return plan


def compliance_report_from_plan(pack: Path, plan: dict[str, Any]) -> dict[str, Any]:
    platform_files: list[tuple[str, Path]] = []
    for item in plan.get("items", []):
        raw = Path(str(item.get("content", "")))
        path = raw if raw.is_absolute() else pack / raw
        platform_files.append((str(item.get("platform_id", "")), path))
    return build_compliance_report(pack, platform_files)


def safe_output(text: str, limit: int = 2000) -> str:
    text = re.sub(r"(?i)(token|secret|cookie)(\s*[:=]\s*)\S+", r"\1\2[redacted]", text)
    return text[-limit:]


def run_command(command: list[str], env: dict[str, str]) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            command,
            cwd=PROJECT_ROOT,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout or ""
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="replace")
        return {"returncode": 124, "stdout": safe_output(output), "stderr": "", "timed_out": True}
    return {
        "returncode": completed.returncode,
        "stdout": safe_output(completed.stdout),
        "stderr": safe_output(completed.stderr),
    }


def dispatch_item(item: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    if not args.dry_run:
        require_active([item["platform_id"]])
    result: dict[str, Any] = {
        "platform_id": item["platform_id"],
        "platform": item["platform"],
        "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    if item["problems"]:
        return {**result, "status": "blocked", "problems": item["problems"]}

    if args.dry_run:
        return {
            **result,
            "status": "validated",
            "delivery": item["delivery"],
            "readiness_status": item["status"],
            "image_count": item["image_count"],
            "video_count": item.get("video_count", 0),
            "video_probe": item.get("video_probe", {}),
            "content_chars": item["content_chars"],
            "title_chars": item["title_chars"],
            "dedicated_content": item["dedicated_content"],
            "compliance_passed": item.get("compliance", {}).get("passed") is True,
            "compliance_policy_id": item.get("compliance", {}).get("policy_id", ""),
        }

    if not args.confirm_drafts:
        return {**result, "status": "not_executed", "reason": "pass --confirm-drafts"}

    missing_credentials = []
    if item.get("missing_credentials"):
        # Credentials are runtime state, not immutable content; a resolved login
        # blocker must not require changing the full content plan.
        _, missing_credentials, _ = configured(item["missing_credentials"])
    if missing_credentials:
        return {
            **result,
            "status": "needs_credentials",
            "missing_credentials": missing_credentials,
        }

    if item["delivery"] == "official_draft_api" and item["platform_id"] == "wechat":
        command = [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "wechat_draft_publisher.py"),
            "--article", item["content"],
            "--cover", item["cover"],
            "--title", item["title"],
            "--author", args.author,
            "--digest", args.digest,
            "--expected-images", str(item["required_image_count"]),
            "--emit-identity",
        ]
        execution = run_command(command, os.environ.copy())
        response: dict[str, Any] = {}
        identity = ""
        for line in execution["stdout"].splitlines():
            try:
                parsed = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(parsed, dict):
                continue
            if parsed.get("record_type") == "draft_identity":
                identity = parsed.get("media_id") if isinstance(parsed.get("media_id"), str) else ""
            else:
                response = parsed
        if identity and response.get("media_id") not in (None, identity):
            response = {"error": "draft_identity_mismatch"}
            identity = ""
        elif identity and not response.get("media_id"):
            response["media_id"] = identity
        verification = response.get("verification") if isinstance(response.get("verification"), dict) else {}
        basic_readback = (execution["returncode"] == 0 and verification.get("passed") is True
                          and verification.get("submitted_text_matches") is True
                          and verification.get("submitted_image_order_matches") is True)
        # A returned ID establishes a draft to inspect, not a fully verified cover or image pixels.
        media_id = response.get("media_id")
        if not isinstance(media_id, str) or not media_id.strip() or len(media_id) > 512:
            media_id = ""
        status = "partial" if media_id else "failed"
        return {
            **result,
            "status": status,
            "image_count": item["image_count"],
            "image_verified": False,
            "cover_verified": False,
            "draft_verified": False,
            "media_id": media_id,
            "proof": "草稿ID已取得；需要独立回读正文、逐图和封面后验收" if status == "partial" else "",
            "evidence": {
                "kind": "api_response",
                "verified_at": datetime.now().astimezone().isoformat(timespec="seconds") if basic_readback else "",
                "summary": "基础回读已检查正文和图片引用；逐图内容与封面待独立验收" if basic_readback else "草稿ID已取得但基础回读失败；先核验已有草稿" if status == "partial" else "接口结果未知；先核验已有草稿",
                "reference": media_id,
            },
            "response": response,
            "execution": execution,
        }

    if item["delivery"] == "wechatsync_bridge":
        if not os.environ.get("WECHATSYNC_TOKEN"):
            return {**result, "status": "needs_wechatsync_token"}
        bridge = locate_wechatsync()
        if not bridge:
            return {**result, "status": "needs_wechatsync_setup"}
        command = [*bridge, "sync", item["content"], "--platforms", item["bridge_id"], "--title", item["title"]]
        if item["cover"]:
            command.extend(["--cover", Path(item["cover"]).resolve().as_uri()])
        execution = run_command(command, os.environ.copy())
        status = "success" if execution["returncode"] == 0 else "failed"
        return {**result, "status": status, "image_count": item["image_count"], "execution": execution}

    return {
        **result,
        "status": "manual_required",
        "editor_url": item.get("editor_url", ""),
        "reason": "browser or direct-publish platforms require explicit human review",
    }


def print_capabilities(registry: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(registry, ensure_ascii=False, indent=2))
        return
    print(f"{'平台':<12} {'通道':<28} {'公开能力':<34}")
    print("-" * 80)
    for item in registry["platforms"]:
        print(f"{item['name']:<12} {item['delivery']:<28} {item.get('public_api', 'unknown'):<34}")


def resolve_title(pack: Path, supplied: str) -> str:
    if supplied:
        return supplied.strip()
    return first_heading(pack / "README.md") or first_heading(pack / "wechat-article.md") or pack.name


def protect_plan_scope(pack: Path, platforms: list[dict]) -> None:
    selected = {item["id"] for item in platforms}
    path = pack / "distribution-plan.json"
    existing = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    previous = {item["platform_id"] for item in existing.get("items", [])}
    if previous and previous != selected and (pack / "distribution-results.json").is_file():
        raise ValueError("PLAN_SCOPE_CHANGE_REQUIRES_RECONCILIATION: preserve existing draft results and full plan")
    if previous != selected:
        require_active(selected)


def command_plan(args: argparse.Namespace, registry: dict[str, Any]) -> int:
    pack = Path(args.pack).resolve()
    if not pack.is_dir():
        raise SystemExit(f"pack not found: {pack}")
    with pack_lock(pack, "plan"):
        platforms = resolve_platforms(args.platforms, registry)
        protect_plan_scope(pack, platforms)
        plan = build_plan(pack, resolve_title(pack, args.title), platforms, registry, bool(args.title.strip()))
        output = Path(args.output).resolve() if args.output else pack / "distribution-plan.json"
        atomic_write_json(output, plan)
        atomic_write_json(pack / "content-compliance-report.json", compliance_report_from_plan(pack, plan))
    print(json.dumps(plan, ensure_ascii=False, indent=2))
    print(f"Wrote plan: {output}")
    return 0


def command_push(args: argparse.Namespace, registry: dict[str, Any]) -> int:
    pack = Path(args.pack).resolve()
    if not pack.is_dir():
        raise SystemExit(f"pack not found: {pack}")
    if not args.dry_run:
        return command_resume_push(args, registry)
    with pack_lock(pack, "dry-run" if args.dry_run else "push"):
        platforms = resolve_platforms(args.platforms, registry)
        protect_plan_scope(pack, platforms)
        plan_output = pack / "distribution-plan.json"
        candidate_plan = build_plan(
            pack,
            resolve_title(pack, args.title),
            platforms,
            registry,
            bool(args.title.strip()),
        )
        existing_plan = (
            json.loads(plan_output.read_text(encoding="utf-8"))
            if plan_output.is_file()
            else {}
        )
        if existing_plan.get("plan_id") == candidate_plan.get("plan_id"):
            plan = existing_plan
        else:
            plan = candidate_plan
            atomic_write_json(plan_output, plan)
        atomic_write_json(pack / "content-compliance-report.json", compliance_report_from_plan(pack, plan))
        bundles = workflow_bundles(pack, plan)
        results = {
            "schema_version": RESULT_SCHEMA_VERSION,
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "pack": str(pack),
            "title": plan["title"],
            "dry_run": args.dry_run,
            "inputs": {
                "plan_id": plan["plan_id"],
                "plan_file_sha256": file_sha256(plan_output),
                "content_bundle": bundles["content"],
                "image_bundle": bundles["images"],
                "video_asset_bundle": bundles["video_assets"],
                "quality_gate_bundle": bundles["quality_gates"],
            },
            "items": [dispatch_item(item, args) for item in plan["items"]],
        }
        from image_quality_gate import gate as image_quality_gate
        visual = image_quality_gate(pack)
        if visual["required"] and not visual["passed"]:
            for item in results["items"]:
                if item.get("status") == "validated":
                    item["status"] = "blocked"
                    item.setdefault("problems", []).extend(row["code"] for row in visual["issues"])
            results["image_quality_issues"] = visual["issues"]
        results["result_id"] = result_identity(results)
        default_name = "distribution-dry-run-results.json"
        output = Path(args.output).resolve() if args.output else pack / default_name
        if output == pack / "distribution-results.json":
            raise ValueError("DRY_RUN_OUTPUT_UNSAFE: dry-run cannot replace formal results")
        atomic_write_json(output, results)
        workflow = evaluate_content_workflow(pack)
        atomic_write_json(pack / "content-workflow.json", workflow)
    print(json.dumps(results, ensure_ascii=False, indent=2))
    print(f"Wrote results: {output}")
    failures = [item for item in results["items"] if item["status"] in {"blocked", "failed"}]
    if failures:
        return 1
    return 0


def resume_context(pack: Path) -> tuple[dict, dict, dict]:
    """Validate the full immutable preflight before considering a subset dispatch."""
    plan_path = pack / "distribution-plan.json"
    dry_path = pack / "distribution-dry-run-results.json"
    if not plan_path.is_file() or not dry_path.is_file():
        raise ValueError("RESUME_PREFLIGHT_MISSING: run full plan and dry-run first")
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    dry = json.loads(dry_path.read_text(encoding="utf-8"))
    if not plan.get("plan_id") or plan["plan_id"] != plan_identity(plan):
        raise ValueError("RESUME_PLAN_INVALID: regenerate full plan and dry-run")
    planned_ids = [row["platform_id"] for row in plan["items"]]
    dry_ids = [row["platform_id"] for row in dry.get("items", [])]
    if (not planned_ids or len(set(planned_ids)) != len(planned_ids)
            or sorted(planned_ids) != sorted(dry_ids)
            or dry.get("dry_run") is not True
            or any(row.get("status") != "validated" for row in dry.get("items", []))
            or dry.get("result_id") != result_identity(dry)):
        raise ValueError("RESUME_DRY_RUN_INVALID: repair full preflight")
    bundles = workflow_bundles(pack, plan)
    inputs = {
        "plan_id": plan["plan_id"],
        "plan_file_sha256": file_sha256(plan_path),
        "content_bundle": bundles["content"],
        "image_bundle": bundles["images"],
        "video_asset_bundle": bundles["video_assets"],
        "quality_gate_bundle": bundles["quality_gates"],
    }
    if any(dry.get("inputs", {}).get(key) != value for key, value in inputs.items()):
        raise ValueError("RESUME_INPUT_CHANGED: rerun plan and dry-run; do not replay old drafts")
    path = pack / "distribution-results.json"
    if path.is_file():
        results = json.loads(path.read_text(encoding="utf-8"))
        ids = [row["platform_id"] for row in results.get("items", [])]
        if (results.get("schema_version") != 2 or results.get("dry_run") is not False
                or sorted(ids) != sorted(planned_ids)
                or results.get("result_id") != result_identity(results)):
            raise ValueError("RESUME_RESULTS_INVALID: reconcile existing results; never reset them")
        if any(results.get("inputs", {}).get(key) != value for key, value in inputs.items()):
            raise ValueError("RESUME_RESULTS_INPUT_CHANGED: reverify existing drafts against current inputs")
    else:
        results = {
            "schema_version": 2, "pack": str(pack), "title": plan["title"],
            "dry_run": False, "inputs": inputs,
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "items": [{"platform_id": key, "status": "not_attempted"} for key in planned_ids],
        }
        results["result_id"] = result_identity(results)
    return plan, dry, results


def remote_outcome_possible(raw: dict) -> bool:
    """A label change must not make a possibly saved remote draft replayable."""
    return bool(
        raw.get("dispatch_state") == "started"
        or raw.get("media_id") or raw.get("draft_url") or raw.get("draft_verified")
        or raw.get("status") in COMPLETE_RESULT_STATUSES | PARTIAL_RESULT_STATUSES | UNVERIFIED_RESULT_STATUSES
    )


def resume_decision(raw: dict, audited: dict, retry: bool = False) -> dict[str, str]:
    if raw.get("platform_id") in DISABLED_PLATFORMS:
        return {"action": "wait_for_input", "code": "PLATFORM_DISABLED_BY_WORKFLOW"}
    if raw.get("status") in {"skipped", "skipped_by_user", "deferred_by_user"}:
        return {"action": "wait_for_input", "code": "USER_DEFERRED_PLATFORM"}
    if audited.get("quality") == "complete":
        return {"action": "skip_verified", "code": "DRAFT_ALREADY_VERIFIED"}
    if raw.get("dispatch_state") == "started":
        return {"action": "verify_existing", "code": "DISPATCH_OUTCOME_UNKNOWN"}
    if remote_outcome_possible(raw):
        return {"action": "verify_existing", "code": "DRAFT_REVERIFY_REQUIRED"}
    if raw.get("status") in {"not_attempted", "not_executed"}:
        return {"action": "dispatch", "code": "DRAFT_NOT_ATTEMPTED"}
    # Only these outcomes are known to occur before any remote write.
    safe_retry = {"blocked", "needs_credentials", "needs_wechatsync_token", "needs_wechatsync_setup", "manual_required"}
    if raw.get("status") in safe_retry and not (raw.get("media_id") or raw.get("draft_url") or raw.get("draft_verified")):
        if retry and int(raw.get("dispatch_attempts", 0)) < 2:
            return {"action": "dispatch", "code": "BLOCKER_RETRY_EXPLICIT"}
        return {"action": "wait_for_input", "code": "BLOCKER_REQUIRES_REVIEW"}
    return {"action": "verify_existing", "code": "DRAFT_REVERIFY_REQUIRED"}


def dispatch_marker(pack: Path, plan: dict, dry: dict, raw: dict, mode: str) -> dict:
    attempt = int(raw.get("dispatch_attempts", 0)) + 1
    return {
        "platform_id": raw["platform_id"], "status": "unverified", "dispatch_state": "started",
        "dispatch_mode": mode, "dispatch_attempts": attempt,
        "dispatch_id": canonical_sha256({
            "pack": str(pack), "plan_id": plan["plan_id"], "dry_run_id": dry["result_id"],
            "platform_id": raw["platform_id"], "attempt": attempt,
        }),
        "issue_code": "DISPATCH_OUTCOME_UNKNOWN",
    }


def command_browser_begin(args: argparse.Namespace) -> int:
    """Persist local intent immediately before the first browser content write."""
    pack = Path(args.pack).resolve()
    require_active([args.platform])
    if args.platform == "wechat":
        from requested_topic import request_for_pack
        request = request_for_pack(pack)
        if not request or request.get("browser_video_draft") is not True:
            raise ValueError("BROWSER_CHANNEL_INVALID: WeChat must use the official draft API")
    with pack_lock(pack, "browser-begin"):
        plan, dry, results = resume_context(pack)
        row_index = next((i for i, row in enumerate(results["items"])
                          if row["platform_id"] == args.platform), None)
        if row_index is None:
            raise ValueError("RESUME_PLATFORM_MISMATCH: platform is not in the full plan")
        workflow = evaluate_content_workflow(pack)
        if not workflow.get("ready_for_draft_actions"):
            raise ValueError("RESUME_WORKFLOW_BLOCKED: " + ",".join(workflow.get("issues", [])))
        report = resume_report(pack, plan, results, {args.platform} if args.retry else set())
        decision = next(row for row in report["items"] if row["platform_id"] == args.platform)
        response = {**decision, "execute_browser_action": False, "plan_id": plan["plan_id"],
                    "dry_run_id": dry["result_id"]}
        if decision["action"] == "dispatch":
            create_pack_backup(pack, "browser-begin", [
                "distribution-plan.json", "distribution-dry-run-results.json", "distribution-results.json",
                "distribution-audit.json", "distribution-evidence.jsonl", "distribution-evidence-chain.json",
            ], keep=10)
            marker = dispatch_marker(pack, plan, dry, results["items"][row_index], "browser")
            results["items"][row_index] = marker
            results["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
            results["result_id"] = result_identity(results)
            atomic_write_json(pack / "distribution-results.json", results)
            response.update(execute_browser_action=True, dispatch_id=marker["dispatch_id"],
                            result_id=results["result_id"])
    print(json.dumps(response, ensure_ascii=False, indent=2))
    return 0 if response["execute_browser_action"] or decision["action"] == "skip_verified" else 2


def resume_report(pack: Path, plan: dict, results: dict, retries: set[str] | None = None) -> dict:
    audited = {row["platform_id"]: row for row in audit_distribution(pack, plan, results)["items"]}
    return {
        "plan_id": plan["plan_id"],
        "items": [{"platform_id": row["platform_id"], **resume_decision(
            row, audited[row["platform_id"]], row["platform_id"] in (retries or set())
        )} for row in results["items"]],
    }


def command_resume_push(args: argparse.Namespace, registry: dict[str, Any]) -> int:
    pack = Path(args.pack).resolve()
    require_active(row["id"] for row in resolve_platforms(args.platforms, registry))
    if args.output and Path(args.output).resolve() != pack / "distribution-results.json":
        raise ValueError("RESUME_OUTPUT_UNSAFE: formal checkpoints must use the pack result file")
    with pack_lock(pack, "resume-drafts"):
        plan, dry, results = resume_context(pack)
        selected = {row["id"] for row in resolve_platforms(args.platforms, registry)}
        planned = {row["platform_id"]: row for row in plan["items"]}
        retries = {value.strip() for value in getattr(args, "retry_platforms", "").split(",") if value.strip()}
        if not selected.issubset(planned) or not retries.issubset(selected):
            raise ValueError("RESUME_PLATFORM_MISMATCH: select only platforms in the full preflight")
        if args.title.strip() and args.title.strip() != plan["title"]:
            raise ValueError("RESUME_TITLE_CHANGED: regenerate plan and dry-run before changing title")
        workflow = evaluate_content_workflow(pack)
        if not workflow.get("ready_for_draft_actions"):
            raise ValueError("RESUME_WORKFLOW_BLOCKED: " + ",".join(workflow.get("issues", [])))
        report = resume_report(pack, plan, results, retries)
        if not args.confirm_drafts:
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 2
        decisions = {row["platform_id"]: row for row in report["items"]}
        output = pack / "distribution-results.json"
        backed_up = False

        def checkpoint() -> None:
            results["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
            results["result_id"] = result_identity(results)
            atomic_write_json(output, results)

        for index, raw in enumerate(results["items"]):
            platform = raw["platform_id"]
            if platform not in selected or decisions[platform]["action"] != "dispatch":
                continue
            if not backed_up:
                create_pack_backup(pack, "resume-drafts", [
                    "distribution-plan.json", "distribution-dry-run-results.json", "distribution-results.json",
                    "distribution-audit.json", "distribution-evidence.jsonl", "distribution-evidence-chain.json",
                ], keep=10)
                backed_up = True
            marker = dispatch_marker(pack, plan, dry, raw, "adapter")
            results["items"][index] = marker
            checkpoint()  # Persist intent before a remote side effect can happen.
            try:
                outcome = dispatch_item(planned[platform], args)
            except Exception:
                # The remote side may already have saved a draft; never replay or log secrets.
                continue
            results["items"][index] = {
                **outcome, "dispatch_state": "recorded", "dispatch_attempts": marker["dispatch_attempts"],
                "dispatch_id": marker["dispatch_id"], "dispatch_mode": marker["dispatch_mode"],
            }
            checkpoint()
        atomic_write_json(pack / "content-workflow.json", evaluate_content_workflow(pack))
        final = resume_report(pack, plan, results)
    print(json.dumps(final, ensure_ascii=False, indent=2))
    return 0 if all(row["action"] == "skip_verified" for row in final["items"] if row["platform_id"] in selected) else 2


def command_resume_status(args: argparse.Namespace) -> int:
    pack = Path(args.pack).resolve()
    with pack_lock(pack, "resume-status"):
        plan, _, results = resume_context(pack)
        print(json.dumps(resume_report(pack, plan, results), ensure_ascii=False, indent=2))
    return 0


def command_ledger_status(args: argparse.Namespace) -> int:
    """Read chain health and optionally the latest backup without recording an action."""
    pack = Path(args.pack).resolve()
    if not pack.is_dir():
        raise ValueError("LEDGER_PACK_MISSING: select an existing pack")
    with pack_lock(pack, "ledger-status"):
        chains = {}
        for scope, name in (("evidence", "distribution-evidence"), ("actions", "distribution-dashboard-actions")):
            report = inspect_chained_jsonl(pack / f"{name}.jsonl", pack / f"{name}-chain.json")
            chains[scope] = {
                "valid": report["valid"], "issue_code": report["issue_code"],
                "invalid_lines": report["invalid_lines"], "chain": report["chain"],
                "anchor": {key: report["anchor"].get(key) for key in ("exists", "valid", "entries", "head_hash")},
            }
        backups = list_pack_backups(pack, limit=1, verify=True) if args.verify_backup else []
        backup = backups[0] if backups else None
        latest = {key: backup.get(key) for key in ("name", "verification", "comparison")} if backup else None
        valid = all(report["valid"] for report in chains.values())
        if args.verify_backup and not (latest and latest["verification"].get("valid")):
            valid = False
        report = {"pack": str(pack), "read_only": True, "valid": valid, "chains": chains,
                  "backup_requested": args.verify_backup, "latest_backup": latest}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if valid else 2


def classify_result(status: str) -> str:
    if status in COMPLETE_RESULT_STATUSES:
        return "complete"
    if status in PARTIAL_RESULT_STATUSES:
        return "partial"
    if status in BLOCKED_RESULT_STATUSES or status.startswith("blocked"):
        return "blocked"
    if status in UNVERIFIED_RESULT_STATUSES:
        return "unverified"
    if status.startswith("package_only"):
        return "package_only"
    return "not_attempted"


def audit_distribution(pack: Path, plan: dict[str, Any], results: dict[str, Any]) -> dict[str, Any]:
    result_index = {item.get("platform_id"): item for item in results.get("items", [])}
    audited: list[dict[str, Any]] = []
    quality_names = [
        "complete", "partial", "blocked", "unverified", "package_only", "not_attempted", "invalid"
    ]
    counts = {key: 0 for key in quality_names}

    for planned in plan.get("items", []):
        platform_id = planned["platform_id"]
        raw = result_index.get(platform_id, {})
        raw_status = raw.get("status", "missing_result")
        quality = classify_result(raw_status)
        evidence = structured_evidence(raw, pack)
        issues: list[str] = []
        if not raw:
            issues.append("missing_result")
        if raw and evidence["recorded"] and not evidence["reference_recorded"]:
            issues.append("missing_evidence_reference")
        artifact = evidence.get("artifact", {})
        if artifact.get("local") and (not artifact.get("allowed") or not artifact.get("exists")):
            issues.append("missing_or_external_evidence_artifact")
        if quality == "complete":
            if evidence.get("stale"):
                issues.append("evidence_stale")
            if artifact.get("local") and not artifact.get("hash_matches"):
                issues.append("evidence_artifact_hash_missing_or_changed")
            expected_images = planned.get("required_image_count")
            actual_images = raw.get("image_count")
            if expected_images is not None and actual_images != expected_images:
                issues.append(f"result_image_count:{actual_images}!={expected_images}")
            if expected_images is not None and raw.get("image_verified") is not True:
                issues.append("images_not_visually_verified")
            expected_videos = planned.get("required_video_count")
            if planned.get("inline_videos"):
                expected_videos = len(planned["inline_videos"])
            actual_videos = raw.get("video_count")
            if expected_videos is not None and actual_videos != expected_videos:
                issues.append(f"result_video_count:{actual_videos}!={expected_videos}")
            if expected_videos is not None and raw.get("video_verified") is not True:
                issues.append("video_not_visually_verified")
            if expected_videos is not None and planned.get("video_probe", {}).get("has_audio") and raw.get("audio_verified") is not True:
                issues.append("audio_not_verified")
            if any(v.get("has_audio") for v in planned.get("inline_videos", [])) and raw.get("audio_verified") is not True:
                issues.append("audio_not_verified")
            if planned.get("cover") and raw.get("cover_verified") is not True:
                issues.append("cover_not_verified")
            if raw.get("draft_verified") is not True:
                issues.append("draft_retention_not_verified")
            if platform_id == "wechat" and not raw.get("media_id"):
                issues.append("missing_media_id")
            if not evidence["recorded"]:
                issues.append("missing_structured_evidence")
            delivery_used = raw.get("delivery_used", planned.get("delivery"))
            if delivery_used == "browser_draft" and not raw.get("proof"):
                issues.append("missing_browser_draft_proof")
        if issues and quality == "complete":
            quality = "invalid"
        counts[quality] += 1
        audited.append({
            "platform_id": platform_id,
            "platform": planned.get("platform", ""),
            "raw_status": raw_status,
            "quality": quality,
            "image_count": raw.get("image_count"),
            "image_verified": raw.get("image_verified") is True,
            "video_count": raw.get("video_count"),
            "video_verified": raw.get("video_verified") is True,
            "audio_verified": raw.get("audio_verified") is True,
            "cover_verified": raw.get("cover_verified") is True,
            "draft_verified": raw.get("draft_verified") is True,
            "evidence": evidence,
            "issues": issues,
        })

    total = len(audited)
    audit = {
        "schema_version": RESULT_SCHEMA_VERSION,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "pack": str(pack),
        "complete": total > 0 and counts["complete"] == total,
        "summary": {"total": total, **counts},
        "items": audited,
    }
    audit["audit_id"] = canonical_sha256({
        "pack": audit["pack"],
        "summary": audit["summary"],
        "items": stable_identity_value(audit["items"]),
        "plan_id": plan.get("plan_id") or plan_identity(plan),
        "result_id": results.get("result_id") or result_identity(results),
    })
    return audit


def command_audit(args: argparse.Namespace) -> int:
    pack = Path(args.pack).resolve()
    plan_path = Path(args.plan).resolve() if args.plan else pack / "distribution-plan.json"
    results_path = Path(args.results).resolve() if args.results else pack / "distribution-results.json"
    if not plan_path.is_file():
        raise SystemExit(f"plan not found: {plan_path}")
    if not results_path.is_file():
        raise SystemExit(f"results not found: {results_path}")
    with pack_lock(pack, "audit"):
        for name in ("distribution-evidence", "distribution-dashboard-actions"):
            require_valid_chained_jsonl(pack / f"{name}.jsonl", pack / f"{name}-chain.json")
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        results = json.loads(results_path.read_text(encoding="utf-8"))
        computed_plan_id = plan_identity(plan)
        if plan.get("plan_id") and plan.get("plan_id") != computed_plan_id:
            raise ValueError("distribution plan fingerprint mismatch")
        computed_result_id = result_identity(results)
        if results.get("result_id") and results.get("result_id") != computed_result_id:
            raise ValueError("distribution results fingerprint mismatch")
        dry_run_path = pack / "distribution-dry-run-results.json"
        dry_run = json.loads(dry_run_path.read_text(encoding="utf-8")) if dry_run_path.is_file() else {}
        create_pack_backup(pack, "audit", [
            "distribution-plan.json", "distribution-results.json", "distribution-audit.json",
            "distribution-evidence.jsonl", "distribution-evidence-chain.json",
        ], keep=10)
        if not plan.get("plan_id"):
            plan["plan_id"] = computed_plan_id
            atomic_write_json(plan_path, plan)
        if not results.get("result_id"):
            results["result_id"] = computed_result_id
            atomic_write_json(results_path, results)
        audit = audit_distribution(pack, plan, results)
        audit["inputs"] = {
            "plan_id": plan.get("plan_id") or plan_identity(plan),
            "plan_file_sha256": file_sha256(plan_path),
            "dry_run_id": dry_run.get("result_id", ""),
            "dry_run_file_sha256": file_sha256(dry_run_path) if dry_run_path.is_file() else "",
            "result_id": results.get("result_id") or result_identity(results),
            "results_file_sha256": file_sha256(results_path),
        }
        appended = append_evidence_ledger(pack, results, audit)
        audit["evidence_ledger_appended"] = appended
        output = Path(args.output).resolve() if args.output else pack / "distribution-audit.json"
        atomic_write_json(output, audit)
    print(json.dumps(audit, ensure_ascii=False, indent=2))
    print(f"Wrote audit: {output}")
    return 1 if args.strict and not audit["complete"] else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Huazi domestic social draft router")
    parser.add_argument("--registry", default=str(DEFAULT_REGISTRY))
    sub = parser.add_subparsers(dest="command", required=True)

    capabilities = sub.add_parser("capabilities", help="Show platform delivery capabilities")
    capabilities.add_argument("--json", action="store_true")

    resume = sub.add_parser("resume-status", help="Inspect checkpoint decisions without platform actions")
    resume.add_argument("--pack", required=True)

    ledger = sub.add_parser("ledger-status", help="Read both chains and optionally verify only the latest backup; never repair or write reports")
    ledger.add_argument("--pack", required=True)
    ledger.add_argument("--verify-backup", action="store_true")

    browser = sub.add_parser("browser-begin", help="Record intent before browser upload/autosave; never operates the browser")
    browser.add_argument("--pack", required=True)
    browser.add_argument("--platform", required=True)
    browser.add_argument("--retry", action="store_true", help="Only after resolving a known pre-dispatch blocker")

    audit = sub.add_parser("audit", help="Audit saved draft results against the distribution plan")
    audit.add_argument("--pack", required=True)
    audit.add_argument("--plan", default="")
    audit.add_argument("--results", default="")
    audit.add_argument("--output", default="")
    audit.add_argument("--strict", action="store_true")

    for name in ["plan", "push"]:
        command = sub.add_parser(name)
        command.add_argument("--pack", required=True)
        command.add_argument("--platforms", default=DEFAULT_PLATFORMS)
        command.add_argument("--title", default="")
        command.add_argument("--output", default="")
        if name == "push":
            command.add_argument("--author", default="华子社长")
            command.add_argument("--digest", default="")
            command.add_argument("--dry-run", action="store_true")
            command.add_argument("--confirm-drafts", action="store_true")
            command.add_argument("--retry-platforms", default="", help="Explicitly retry resolved pre-dispatch blockers only; at most two attempts")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.command == "resume-status":
        return command_resume_status(args)
    if args.command == "browser-begin":
        return command_browser_begin(args)
    if args.command == "ledger-status":
        return command_ledger_status(args)
    if args.command == "audit":
        return command_audit(args)
    load_dotenv(PROJECT_ROOT / ".env")
    registry = load_registry(Path(args.registry).resolve())
    if args.command == "capabilities":
        print_capabilities(registry, args.json)
        return 0
    if args.command == "plan":
        return command_plan(args, registry)
    return command_push(args, registry)


if __name__ == "__main__":
    raise SystemExit(main())
