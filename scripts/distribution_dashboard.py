#!/usr/bin/env python3
"""Local read-mostly dashboard for Huazi multi-platform draft distribution."""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import socket
import subprocess
import sys
import threading
import uuid
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse


SCRIPT_ROOT = Path(__file__).resolve().parent
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

from distribution_io import (  # noqa: E402
    LEGACY_FIVE_IMAGE_NAMES,
    PACK_LOCK_ENV,
    PackLockError,
    append_chained_jsonl,
    atomic_write_json,
    canonical_sha256,
    compare_pack_backup,
    create_pack_backup,
    file_bundle_snapshot,
    file_sha256,
    five_image_paths,
    image_file_probe,
    inspect_chained_jsonl,
    list_pack_backups,
    pack_lock,
    read_pack_lock,
    require_valid_chained_jsonl,
    verify_pack_backup,
)


PROJECT_ROOT = SCRIPT_ROOT.parent
STATIC_ROOT = PROJECT_ROOT / "dashboard"
OUTPUTS_ROOT = PROJECT_ROOT / "outputs"
REGISTRY_PATH = PROJECT_ROOT / "config" / "social-platforms.json"
ADAPTERS_PATH = PROJECT_ROOT / "config" / "distribution-adapters.json"
DASHBOARD_STATE_PATH = PROJECT_ROOT / "config" / "distribution-dashboard-state.json"
DASHBOARD_RUNTIME_PATH = PROJECT_ROOT / "config" / "distribution-dashboard-runtime.json"
ROUTER = PROJECT_ROOT / "scripts" / "social_draft_router.py"
SOCIAL_ADAPTER = PROJECT_ROOT / "scripts" / "social_auto_upload_adapter.py"
SERVICE_ID = "huazi-distribution-dashboard"
SERVICE_VERSION = "1.11"
RESULT_SCHEMA_VERSION = 2
EVIDENCE_KINDS = {"api_response", "browser_visual", "browser_state", "account_state", "manual_check"}
IDENTITY_VOLATILE_FIELDS = {
    "generated_at", "updated_at", "started_at", "finished_at", "verified_at",
    "plan_file_sha256",
}
EVIDENCE_STALE_MINUTES = 24 * 60
from workflow_policy import ACTIVE_PLATFORMS, STAGE_PLATFORMS, GRAPHIC_PLATFORMS, PAUSED_PLATFORMS, requires_video

DEFAULT_PLATFORM_IDS = list(ACTIVE_PLATFORMS)
CANONICAL_IMAGES = list(LEGACY_FIVE_IMAGE_NAMES)
SAFE_ACTIONS = {"plan", "dry-run", "audit", "stage", "verify-backup"}
MAX_ACTION_BODY_BYTES = 16 * 1024
MUTABLE_PACK_FILES = [
    "distribution-plan.json",
    "content-compliance-report.json",
    "distribution-dry-run-results.json",
    "distribution-results.json",
    "distribution-audit.json",
    "distribution-evidence.jsonl",
    "distribution-evidence-chain.json",
    "social-auto-upload-jobs.json",
    "distribution-dashboard-actions.jsonl",
    "distribution-dashboard-actions-chain.json",
    "distribution-action-state.json",
]
SERVICE_STARTED_AT = datetime.now().astimezone()
QUALITY_ORDER = ["complete", "partial", "blocked", "unverified", "not_attempted", "invalid", "package_only"]
QUALITY_LABELS = {
    "complete": "草稿完整",
    "partial": "部分完成",
    "blocked": "存在阻塞",
    "unverified": "等待核验",
    "not_attempted": "尚未处理",
    "invalid": "凭证无效",
    "package_only": "仅发布包",
    "not_available": "未纳入",
}
ISSUE_MESSAGES = {
    "text_draft_created_cover_pending": ("补齐封面", "正文已经保存，封面仍待补齐。"),
    "blocked_by_previous_draft": ("处理旧草稿", "存在上次未发布的草稿，需要先处理。"),
    "unverified_editor_timeout": ("重新核验", "编辑器响应超时，当前草稿状态尚未确认。"),
    "needs_credentials": ("恢复登录", "平台登录态失效，需要先重新登录。"),
    "blocked": ("处理账号阻塞", "平台账号或编辑器当前不可用。"),
    "failed_autosave_site_error": ("检查站点状态", "平台自动保存失败，需要打开编辑器复查。"),
    "not_attempted": ("开始草稿", "该平台尚未执行草稿动作。"),
}
ISSUE_PRIORITY = {"invalid": 0, "blocked": 1, "partial": 2, "unverified": 3, "not_attempted": 4}
RETRY_GUIDANCE = {
    "needs_credentials": {"mode": "manual", "label": "重新登录后继续", "severity": "high"},
    "blocked": {"mode": "manual", "label": "处理账号限制", "severity": "high"},
    "blocked_by_previous_draft": {"mode": "manual", "label": "先处理旧草稿", "severity": "high"},
    "text_draft_created_cover_pending": {"mode": "retry", "label": "补图并重新验收", "severity": "medium"},
    "unverified_editor_timeout": {"mode": "retry", "label": "打开编辑器重新核验", "severity": "medium"},
    "failed_autosave_site_error": {"mode": "retry", "label": "站点恢复后重试", "severity": "medium"},
    "not_attempted": {"mode": "start", "label": "开始草稿流程", "severity": "low"},
}
DEFAULT_RETRY_GUIDANCE = {"mode": "manual", "label": "人工复核", "severity": "medium"}
HISTORY_COMPLETE_STATUSES = {"success", "draft_created", "local_draft_created", "drafted", "draft_saved"}
HISTORY_PARTIAL_STATUSES = {"text_draft_created_cover_pending", "partial"}
ACTION_LOCK = threading.Lock()


def read_json(path: Path, default: Any) -> Any:
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def json_file_status(path: Path, expected_schema: int | None = None) -> dict[str, Any]:
    if not path.is_file():
        return {"name": path.name, "status": "missing", "schema_version": None, "message": "文件不存在"}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return {
            "name": path.name,
            "status": "invalid",
            "schema_version": None,
            "message": f"JSON 格式错误：第 {exc.lineno} 行第 {exc.colno} 列",
        }
    except OSError:
        return {"name": path.name, "status": "invalid", "schema_version": None, "message": "文件无法读取"}
    if not isinstance(payload, dict):
        return {"name": path.name, "status": "invalid", "schema_version": None, "message": "根节点必须是对象"}
    schema = payload.get("schema_version")
    if expected_schema is not None and schema != expected_schema:
        return {
            "name": path.name,
            "status": "warning",
            "schema_version": schema,
            "message": f"期望结构 v{expected_schema}，当前为 {schema or '未标记'}",
        }
    return {"name": path.name, "status": "ok", "schema_version": schema, "message": "读取正常"}


def core_document_issues(path: Path, role: str) -> list[str]:
    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    issues: list[str] = []
    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return ["items 必须是数组"]
    invalid_rows = [index for index, item in enumerate(items, start=1) if not isinstance(item, dict) or not item.get("platform_id")]
    if invalid_rows:
        issues.append(f"第 {invalid_rows[0]} 条平台记录缺少 platform_id")
    if role == "dry-run" and payload.get("dry_run") is not True:
        issues.append("dry_run 必须为 true")
    if role == "audit" and not isinstance(payload.get("summary"), dict):
        issues.append("summary 必须是对象")
    if role == "results" and payload.get("dry_run") is True:
        issues.append("正式结果不能标记为 dry_run")
    return issues


def file_timestamp(path: Path) -> str:
    if not path.is_file():
        return ""
    return datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat(timespec="seconds")


def parse_timestamp(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone()
    except ValueError:
        return None


def evidence_reference_type(reference: str) -> str:
    if not reference:
        return "missing"
    if reference.startswith(("http://", "https://")):
        return "url"
    if re.fullmatch(r"\d{5,}", reference):
        return "draft_id"
    if re.fullmatch(r"[A-Za-z0-9_-]{20,}", reference):
        return "response_id"
    if any(reference.lower().endswith(suffix) for suffix in (".png", ".jpg", ".jpeg", ".json", ".md")):
        return "artifact"
    return "record_id"


def evidence_reliability(kind: str, reference_type: str) -> tuple[int, str]:
    if kind not in EVIDENCE_KINDS:
        return 0, "missing"
    if kind == "api_response" and reference_type in {"response_id", "record_id"}:
        score = 100
    elif kind == "browser_visual" and reference_type in {"artifact", "draft_id"}:
        score = 90
    elif kind == "browser_visual" and reference_type != "missing":
        score = 75
    elif kind in {"browser_state", "account_state"} and reference_type != "missing":
        score = 65
    elif kind == "manual_check" and reference_type != "missing":
        score = 60
    else:
        score = 45
    level = "strong" if score >= 85 else "medium" if score >= 60 else "weak"
    return score, level


def evidence_fields(result: dict[str, Any], fallback_time: str = "", pack: Path | None = None) -> dict[str, Any]:
    raw = result.get("evidence") if isinstance(result.get("evidence"), dict) else {}
    kind = str(raw.get("kind", ""))
    verified_at = str(raw.get("verified_at") or result.get("verified_at") or fallback_time)
    summary = str(raw.get("summary") or result.get("proof") or "").strip()
    reference = str(
        raw.get("reference")
        or result.get("draft_url")
        or result.get("media_id")
        or result.get("screenshot_path")
        or ""
    ).strip()
    verified_time = parse_timestamp(verified_at)
    reference_type = evidence_reference_type(reference)
    score, reliability = evidence_reliability(kind, reference_type)
    artifact = {"local": False, "allowed": True, "exists": False, "sha256": "", "path": ""}
    if reference_type == "artifact":
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
    age_minutes = None
    if verified_time:
        age_minutes = max(int((datetime.now().astimezone() - verified_time).total_seconds() / 60), 0)
    try:
        recorded_time = datetime.fromisoformat(str(raw.get("verified_at", "")).replace("Z", "+00:00"))
        timestamp_valid = recorded_time.utcoffset() is not None and (
            datetime.now().astimezone() - recorded_time
        ).total_seconds() >= -300
    except (ValueError, TypeError):
        timestamp_valid = False
    recorded = kind in EVIDENCE_KINDS and timestamp_valid and bool(raw.get("summary"))
    stale = bool(age_minutes is not None and age_minutes > EVIDENCE_STALE_MINUTES)
    reference_recorded = reference_type != "missing"
    artifact["recorded_sha256"] = str(raw.get("sha256", ""))
    artifact["hash_matches"] = bool(artifact["recorded_sha256"]) and artifact["recorded_sha256"] == artifact["sha256"]
    artifact_valid = not artifact["local"] or pack is None or (
        artifact["allowed"] and artifact["exists"] and artifact["hash_matches"]
    )
    if not artifact_valid:
        score, reliability = 0, "missing"
    return {
        "recorded": recorded,
        "reference_recorded": reference_recorded,
        "kind": kind,
        "verified_at": verified_at,
        "summary": summary,
        "reference": reference,
        "reference_type": reference_type,
        "artifact": artifact,
        "score": score if recorded else 0,
        "reliability": reliability if recorded else "missing",
        "age_minutes": age_minutes,
        "stale": stale,
        "fresh": recorded and not stale,
        "trusted": recorded and reference_recorded and artifact_valid and not stale and score >= 60,
    }


def retry_guidance(status: str, quality: str) -> dict[str, str]:
    guidance = dict(RETRY_GUIDANCE.get(status, DEFAULT_RETRY_GUIDANCE))
    if quality == "complete":
        return {"mode": "none", "label": "无需处理", "severity": "none"}
    if quality == "invalid":
        return {"mode": "manual", "label": "补齐可信凭证", "severity": "high"}
    return guidance


def verification_fields(result: dict[str, Any], planned: dict[str, Any] | None = None) -> dict[str, Any]:
    planned = planned or {}
    if planned.get("required_video_count"):
        labels = {
            "video_verified": "视频",
            "audio_verified": "音轨",
            "cover_verified": "封面",
            "draft_verified": "草稿",
        }
    else:
        labels = {
            "image_verified": "图片",
            "cover_verified": "封面",
            "draft_verified": "草稿",
        }
    recorded = all(key in result and isinstance(result.get(key), bool) for key in labels)
    missing = [label for key, label in labels.items() if result.get(key) is not True]
    return {
        "recorded": recorded,
        "passed": recorded and not missing,
        "missing": missing,
    }


def first_heading(path: Path) -> str:
    if not path.is_file():
        return ""
    match = re.search(r"^#\s+(.+)$", path.read_text(encoding="utf-8"), flags=re.M)
    return match.group(1).strip() if match else ""


def image_size(path: Path) -> tuple[int, int] | None:
    probe = image_file_probe(path)
    if probe["decoded"]:
        return int(probe["width"]), int(probe["height"])
    return None


def markdown_ready(path: Path, minimum_chars: int = 40) -> bool:
    if not path.is_file():
        return False
    try:
        return len(path.read_text(encoding="utf-8").strip()) >= minimum_chars
    except OSError:
        return False


def canonical_image_paths(pack: Path) -> list[Path]:
    return five_image_paths(pack)


def canonical_image_validation(pack: Path) -> dict[str, Any]:
    paths = canonical_image_paths(pack)
    items = []
    for index, path in enumerate(paths, start=1):
        expected = (900, 383) if index == 1 else (1080, 720)
        probe = image_file_probe(path)
        actual = (probe["width"], probe["height"]) if probe["decoded"] else None
        items.append({
            "index": index,
            "name": path.name,
            "exists": probe["exists"],
            "decoded": probe["decoded"],
            "format": probe["format"],
            "bytes": probe["bytes"],
            "sha256": probe["sha256"],
            "error": probe["error"],
            "expected_width": expected[0],
            "expected_height": expected[1],
            "width": actual[0] if actual else None,
            "height": actual[1] if actual else None,
            "dimension_valid": actual == expected,
            "unique": True,
            "valid": probe["decoded"] and probe["format"] == "PNG" and actual == expected,
        })
    hashes: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        if item["sha256"]:
            hashes.setdefault(item["sha256"], []).append(item)
    duplicate_groups = []
    for digest, group in hashes.items():
        if len(group) < 2:
            continue
        names = [item["name"] for item in group]
        duplicate_groups.append({"sha256": digest, "files": names})
        for item in group:
            item["unique"] = False
            item["valid"] = False
    return {
        "valid_count": sum(item["valid"] for item in items),
        "existing_count": sum(item["exists"] for item in items),
        "decoded_count": sum(item["decoded"] for item in items),
        "unique_count": len(hashes),
        "duplicate_groups": duplicate_groups,
        "ready": len(items) == 5 and all(item["valid"] for item in items),
        "items": items,
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
    normalized_items = stable_identity_value(results.get("items", []))
    return canonical_sha256({
        "schema_version": results.get("schema_version"),
        "pack": results.get("pack"),
        "title": results.get("title"),
        "dry_run": results.get("dry_run"),
        "inputs": stable_identity_value(results.get("inputs", {})),
        "items": normalized_items,
    })


def audit_identity(audit: dict[str, Any], plan_id: str, result_id: str) -> str:
    return canonical_sha256({
        "pack": audit.get("pack"),
        "summary": audit.get("summary", {}),
        "items": stable_identity_value(audit.get("items", [])),
        "plan_id": plan_id,
        "result_id": result_id,
    })


def workflow_bundles(pack: Path, plan: dict[str, Any]) -> dict[str, Any]:
    content_paths = [Path(str(item.get("content"))) for item in plan.get("items", []) if item.get("content")]
    return {
        "content": file_bundle_snapshot(pack, content_paths),
        "images": file_bundle_snapshot(pack, canonical_image_paths(pack)),
    }


def health_issue(severity: str, code: str, message: str, action: str) -> dict[str, str]:
    return {"severity": severity, "code": code, "message": message, "action": action}


def quality_gates(pack: Path) -> dict[str, Any]:
    plan = read_json(pack / "distribution-plan.json", {"items": []})
    dry = read_json(pack / "distribution-dry-run-results.json", {"items": []})
    audit = read_json(pack / "distribution-audit.json", {"items": []})
    results = read_json(pack / "distribution-results.json", {"items": []})
    image_validation = canonical_image_validation(pack)

    plan_items = plan.get("items", [])
    ready_copies = 0
    for item in plan_items:
        raw_path = Path(str(item.get("content", "")))
        content_path = raw_path if raw_path.is_absolute() else pack / raw_path
        if not content_path.is_file() and raw_path.name:
            content_path = pack / raw_path.name
        ready_copies += int(markdown_ready(content_path))

    dry_items = dry.get("items", [])
    dry_ready = bool(plan_items) and len(dry_items) == len(plan_items) and all(item.get("status") == "validated" for item in dry_items)
    audit_items = audit.get("items", [])
    audit_ready = bool(plan_items) and len(audit_items) == len(plan_items)
    result_index = {item.get("platform_id"): item for item in results.get("items", [])}
    verification_count = sum(
        verification_fields(result_index.get(item.get("platform_id"), {}), item)["recorded"]
        for item in plan_items
    )
    verification_ready = bool(plan_items) and verification_count == len(plan_items)
    results_updated_at = str(results.get("updated_at") or file_timestamp(pack / "distribution-results.json"))
    evidence_count = sum(
        evidence_fields(result_index.get(item.get("platform_id"), {}), results_updated_at, pack)["recorded"]
        for item in plan_items
    )
    evidence_ready = bool(plan_items) and evidence_count == len(plan_items)
    compliance = read_json(pack / "content-compliance-report.json", {})
    compliance_items = compliance.get("items", []) if isinstance(compliance.get("items"), list) else []
    compliance_ready = (
        bool(plan_items)
        and compliance.get("passed") is True
        and len(compliance_items) == len(plan_items)
        and all(item.get("passed") is True for item in compliance_items)
    )
    workflow = read_json(pack / "content-workflow.json", {})
    workflow_gates = workflow.get("gates") if isinstance(workflow.get("gates"), dict) else {}
    workflow_facts = workflow_gates.get("facts") if isinstance(workflow_gates.get("facts"), dict) else {}
    ranking = read_json(pack / "topic-ranking.json", {})
    growth = read_json(pack / "growth-experiment.json", {})
    video_quality = read_json(pack / "video-quality-report.json", {})

    gates = [
        {
            "key": "topic_ranking",
            "group": "preparation",
            "label": "热点候选评分",
            "passed": ranking.get("passed") is True and bool(ranking.get("selected_candidate_id")),
            "detail": f"{ranking.get('candidate_count', 0)} 个候选，最高 {ranking.get('selected_score', 0)} 分",
        },
        {
            "key": "growth_experiment",
            "group": "preparation",
            "label": "单变量增长实验",
            "passed": growth.get("status") == "planned" and bool(growth.get("experiment_id")) and bool(growth.get("variable")) and bool(growth.get("signature_move")),
            "detail": f"变量：{growth.get('variable') or '未设置'}；绝活：{growth.get('signature_move') or '未设置'}",
        },
        {
            "key": "video_quality",
            "group": "preparation",
            "label": "短视频成片质检",
            "passed": video_quality.get("passed") is True,
            "detail": f"抽帧 {len(video_quality.get('sample_frames', []))} 张，黑屏 {video_quality.get('blank_sample_count', 0)} 张",
        },
        {
            "key": "facts",
            "group": "preparation",
            "label": "独立事实核验",
            "passed": workflow_facts.get("passed") is True if workflow_facts else markdown_ready(pack / "fact-check-notes.md"),
            "detail": (
                f"{workflow_facts.get('claim_count', 0)} 条主张，"
                f"{workflow_facts.get('source_domain_count', 0)} 个来源域名"
                if workflow_facts
                else "fact-check-notes.md"
            ),
        },
        {"key": "prediction", "group": "preparation", "label": "发布前预测", "passed": markdown_ready(pack / "prediction.md"), "detail": "prediction.md"},
        {
            "key": "images",
            "group": "preparation",
            "label": "五图尺寸",
            "passed": image_validation["ready"],
            "detail": f"{image_validation['valid_count']} / 5 张尺寸通过",
        },
        {"key": "copies", "group": "preparation", "label": "平台稿件", "passed": bool(plan_items) and ready_copies == len(plan_items), "detail": f"{ready_copies} / {len(plan_items)}"},
        {"key": "dry_run", "group": "preparation", "label": "发布前检查", "passed": dry_ready, "detail": f"{sum(item.get('status') == 'validated' for item in dry_items)} / {len(plan_items)}"},
        {
            "key": "compliance",
            "group": "preparation",
            "label": "全部平台合规",
            "passed": compliance_ready,
            "detail": f"{sum(item.get('passed') is True for item in compliance_items)} / {len(plan_items)}",
        },
        {"key": "audit", "group": "records", "label": "结果可审计", "passed": audit_ready, "detail": f"{len(audit_items)} / {len(plan_items)}"},
        {"key": "verification", "group": "records", "label": "素材与草稿验收", "passed": verification_ready, "detail": f"{verification_count} / {len(plan_items)}"},
        {"key": "evidence", "group": "records", "label": "结构化验收凭证", "passed": evidence_ready, "detail": f"{evidence_count} / {len(plan_items)}"},
    ]
    if not requires_video(pack):
        gates = [item for item in gates if item["key"] != "video_quality"]
    from image_quality_gate import gate as image_quality_gate
    visual = image_quality_gate(pack)
    copy_valid = True
    facts_valid = True
    if visual["required"]:
        from content_workflow_gate import copy_gate, fact_gate
        copy_status = copy_gate(pack)
        copy_valid = copy_status["quality_passed"]
        facts = fact_gate(pack)
        facts_valid = facts["passed"]
        for item in gates:
            if item["key"] == "facts":
                item.update(passed=facts_valid, detail=f"{facts['claim_count']} 条主张；凭证{'当前有效' if facts['evidence_passed'] else '缺失、过期或不匹配'}")
        gates.extend([
            {"key": "image_quality", "group": "preparation", "label": "五图视觉与内容质检",
             "passed": visual["passed"],
             "detail": f"目视评分 {visual.get('visual_score') if visual.get('visual_score') is not None else '未核验'}；问题 {len(visual['issues'])} 项"},
            {"key": "content_quality", "group": "preparation", "label": "内容结构与去复读",
             "passed": copy_valid, "detail": f"结构检查 {copy_status['quality_score'] if copy_status['quality_score'] is not None else '未核验'}；不代表事实或流量评分"},
        ])
    passed = sum(int(item["passed"]) for item in gates)
    groups = []
    for key, label in (("preparation", "发布包准备"), ("records", "验收记录")):
        items = [item for item in gates if item["group"] == key]
        group_passed = sum(int(item["passed"]) for item in items)
        groups.append({"key": key, "label": label, "passed": group_passed, "total": len(items), "ready": group_passed == len(items)})
    return {
        "passed": passed,
        "total": len(gates),
        "ready": passed == len(gates),
        "groups": groups,
        "items": gates,
        "image_quality": visual,
        "workflow": {
            "workflow_id": workflow.get("workflow_id", ""),
            "stage": workflow.get("highest_contiguous_stage", "not_started"),
            "ready_for_draft_actions": workflow.get("ready_for_draft_actions") is True and visual["passed"] and copy_valid and facts_valid,
        },
    }


def ensure_pack(outputs_root: Path, name: str) -> Path:
    root = outputs_root.resolve()
    candidate = (root / name).resolve()
    if candidate.parent != root or not candidate.is_dir():
        raise ValueError("invalid pack")
    return candidate


def pack_title(pack: Path) -> str:
    return first_heading(pack / "README.md") or first_heading(pack / "wechat-article.md") or pack.name


def pack_directories(outputs_root: Path) -> list[Path]:
    if not outputs_root.is_dir():
        return []
    packs = []
    for path in outputs_root.iterdir():
        if not path.is_dir():
            continue
        if any((path / name).is_file() for name in ["distribution-plan.json", "wechat-article.md", "prediction.md"]):
            packs.append(path)
    return sorted(packs, key=lambda path: path.stat().st_mtime, reverse=True)


def quality_label(audit: dict[str, Any], results: dict[str, Any]) -> str:
    summary = audit.get("summary", {})
    total = summary.get("total", 0)
    complete = summary.get("complete", 0)
    if total and complete == total:
        return "complete"
    if complete:
        return "in_progress"
    if results.get("items"):
        return "needs_attention"
    return "prepared"


def list_packs(outputs_root: Path) -> list[dict[str, Any]]:
    rows = []
    for pack in pack_directories(outputs_root):
        audit = read_json(pack / "distribution-audit.json", {})
        results = read_json(pack / "distribution-results.json", {})
        dry = read_json(pack / "distribution-dry-run-results.json", {"items": []})
        derived_summary = history_summary(pack)
        canonical_image_count = sum(path.is_file() for path in canonical_image_paths(pack))
        date_match = re.match(r"^(\d{4}-\d{2}-\d{2})", pack.name)
        updated_at = datetime.fromtimestamp(pack.stat().st_mtime).astimezone()
        rows.append({
            "name": pack.name,
            "title": pack_title(pack),
            "date": date_match.group(1) if date_match else updated_at.date().isoformat(),
            "updated_at": updated_at.isoformat(timespec="seconds"),
            "quality": quality_label(audit, results),
            "image_count": canonical_image_count,
            "summary": derived_summary,
            "completion_rate": completion_rate(derived_summary),
            "dry_run_passed": bool(dry.get("items")) and all(item.get("status") == "validated" for item in dry["items"]),
            "has_audit": bool(audit.get("items")),
        })
    return rows


def completion_rate(summary: dict[str, Any]) -> int:
    eligible = max(int(summary.get("total", 0)) - int(summary.get("package_only", 0)), 0)
    if not eligible:
        return 0
    return round(int(summary.get("complete", 0)) / eligible * 100)


def pack_evidence_rate(pack: Path) -> int:
    plan = read_json(pack / "distribution-plan.json", {"items": []})
    results = read_json(pack / "distribution-results.json", {"items": []})
    plan_items = plan.get("items", [])
    if not plan_items:
        return 0
    result_index = {item.get("platform_id"): item for item in results.get("items", [])}
    fallback_time = str(results.get("updated_at") or results.get("generated_at") or file_timestamp(pack / "distribution-results.json"))
    recorded = sum(
        evidence_fields(result_index.get(item.get("platform_id"), {}), fallback_time, pack)["recorded"]
        for item in plan_items
    )
    return round(recorded / len(plan_items) * 100)


def pack_fresh_evidence_rate(pack: Path) -> int:
    plan = read_json(pack / "distribution-plan.json", {"items": []})
    results = read_json(pack / "distribution-results.json", {"items": []})
    plan_items = plan.get("items", [])
    if not plan_items:
        return 0
    result_index = {item.get("platform_id"): item for item in results.get("items", [])}
    fallback_time = str(results.get("updated_at") or results.get("generated_at") or file_timestamp(pack / "distribution-results.json"))
    trusted = sum(
        evidence_fields(result_index.get(item.get("platform_id"), {}), fallback_time, pack)["trusted"]
        for item in plan_items
    )
    return round(trusted / len(plan_items) * 100)


def pack_data_health(pack: Path) -> dict[str, Any]:
    files = [
        json_file_status(pack / "distribution-plan.json", RESULT_SCHEMA_VERSION),
        json_file_status(pack / "distribution-dry-run-results.json", RESULT_SCHEMA_VERSION),
        json_file_status(pack / "distribution-results.json", RESULT_SCHEMA_VERSION),
        json_file_status(pack / "distribution-audit.json", RESULT_SCHEMA_VERSION),
    ]
    issues: list[dict[str, str]] = []
    from image_quality_gate import gate as image_quality_gate
    visual = image_quality_gate(pack)
    for row in visual["issues"]:
        issues.append(health_issue("error", row["code"], row["reference"], row["action"]))
    if visual["required"]:
        from content_workflow_gate import fact_gate
        for code in fact_gate(pack)["issues"]:
            issues.append(health_issue("error", code, "独立事实核验未通过", "核对当前稿件与分镜，更新真实事实凭证并重跑 research_evidence_gate.py、dry-run 和工作流评估"))
    for item in files:
        if item["status"] in {"invalid", "warning"}:
            issues.append({"severity": "error" if item["status"] == "invalid" else "warning", "message": f"{item['name']}：{item['message']}"})
        elif item["status"] == "missing":
            issues.append({"severity": "warning", "message": f"{item['name']}：文件不存在"})
    for filename, role in (
        ("distribution-plan.json", "plan"),
        ("distribution-dry-run-results.json", "dry-run"),
        ("distribution-results.json", "results"),
        ("distribution-audit.json", "audit"),
    ):
        for message in core_document_issues(pack / filename, role):
            issues.append({"severity": "error", "message": f"{filename}：{message}"})

    plan = read_json(pack / "distribution-plan.json", {"items": []})
    dry_run = read_json(pack / "distribution-dry-run-results.json", {"items": []})
    results = read_json(pack / "distribution-results.json", {"items": []})
    audit = read_json(pack / "distribution-audit.json", {"items": []})
    workflow = read_json(pack / "content-workflow.json", {})
    workflow_enforced = bool(re.match(r"^\d{4}-\d{2}-\d{2}", pack.name)) and pack.name[:10] >= "2026-08-07"
    if workflow_enforced and not workflow:
        issues.append(health_issue(
            "error",
            "workflow.state_missing",
            "新发布包缺少内容工作流状态",
            "运行 content_workflow_gate.py evaluate",
        ))
    elif workflow_enforced and workflow.get("ready_for_draft_actions") is not True:
        issues.append(health_issue(
            "error",
            "workflow.draft_gate_not_ready",
            "选题、内容、素材或统一预检尚未全部通过",
            "按 content-workflow.json 的 issues 修复后重新评估",
        ))
    plan_ids = [str(item.get("platform_id", "")) for item in plan.get("items", []) if item.get("platform_id")]
    result_ids = [str(item.get("platform_id", "")) for item in results.get("items", []) if item.get("platform_id")]
    for label, values in (("计划", plan_ids), ("结果", result_ids)):
        duplicates = sorted({value for value in values if values.count(value) > 1})
        if duplicates:
            issues.append({"severity": "error", "message": f"{label}中存在重复平台：{'、'.join(duplicates)}"})
    missing_results = sorted(set(plan_ids) - set(result_ids))
    unexpected_results = sorted(set(result_ids) - set(plan_ids))
    if missing_results:
        issues.append({"severity": "warning", "message": f"缺少平台结果：{'、'.join(missing_results)}"})
    if unexpected_results:
        issues.append({"severity": "warning", "message": f"存在计划外结果：{'、'.join(unexpected_results)}"})

    results_time = str(results.get("updated_at") or results.get("generated_at") or file_timestamp(pack / "distribution-results.json"))
    for raw_result in results.get("items", []):
        evidence = evidence_fields(raw_result, results_time, pack)
        artifact = evidence.get("artifact", {})
        if artifact.get("local") and not (artifact.get("allowed") and artifact.get("exists")):
            issues.append(health_issue(
                "error",
                "evidence.artifact_unavailable",
                f"{raw_result.get('platform_id') or '平台'} 的本地凭证文件缺失或位于发布包外",
                "把凭证文件放入发布包并重新审计",
            ))
        elif artifact.get("local") and not artifact.get("hash_matches"):
            issues.append(health_issue(
                "error", "evidence.artifact_hash_mismatch",
                f"{raw_result.get('platform_id') or '平台'} 的凭证哈希缺失或已改变",
                "重新核验平台草稿，使用结果登记器记录新凭证及 SHA-256，再运行 audit；不要只补填哈希",
            ))

    provenance = []
    current_plan_id = plan_identity(plan) if plan.get("items") else ""
    recorded_plan_id = str(plan.get("plan_id", ""))
    if plan.get("items"):
        plan_current = bool(recorded_plan_id) and recorded_plan_id == current_plan_id
        provenance.append({
            "source": "distribution-plan.json",
            "derived": "plan_id",
            "kind": "fingerprint",
            "current": plan_current,
            "expected": current_plan_id,
            "recorded": recorded_plan_id,
        })
        if not recorded_plan_id:
            issues.append(health_issue("warning", "provenance.plan_id_missing", "分发计划缺少内容指纹", "重新运行分发计划"))
        elif not plan_current:
            issues.append(health_issue("error", "provenance.plan_id_mismatch", "分发计划内容与 plan_id 不一致", "重新生成计划并检查是否被手动修改"))

    dry_run_id = str(dry_run.get("result_id", ""))
    current_dry_run_id = result_identity(dry_run) if dry_run.get("items") else ""
    dry_inputs = dry_run.get("inputs") if isinstance(dry_run.get("inputs"), dict) else {}
    if dry_run.get("items"):
        dry_identity_current = bool(dry_run_id) and dry_run_id == current_dry_run_id
        provenance.append({
            "source": "distribution-dry-run-results.json",
            "derived": "result_id",
            "kind": "fingerprint",
            "current": dry_identity_current,
            "expected": current_dry_run_id,
            "recorded": dry_run_id,
        })
        if not dry_run_id:
            issues.append(health_issue("warning", "provenance.dry_run_id_missing", "统一预检缺少结果指纹", "重新运行 dry-run"))
        elif not dry_identity_current:
            issues.append(health_issue("error", "provenance.dry_run_id_mismatch", "统一预检内容与 result_id 不一致", "重新运行 dry-run 并检查文件完整性"))
        if not dry_inputs:
            issues.append(health_issue("warning", "provenance.dry_run_inputs_missing", "统一预检缺少输入快照", "重新运行 dry-run"))
        else:
            current_bundles = workflow_bundles(pack, plan)
            fingerprint_checks = [
                ("plan_id", recorded_plan_id, "预检引用的计划已经变化", "重新运行 dry-run"),
                ("plan_file_sha256", file_sha256(pack / "distribution-plan.json") if (pack / "distribution-plan.json").is_file() else "", "预检后的计划文件发生变化", "重新运行 dry-run"),
                ("content_bundle", current_bundles["content"]["fingerprint"], "预检后的平台稿件发生变化", "重新运行 dry-run"),
                ("image_bundle", current_bundles["images"]["fingerprint"], "预检后的五图发生变化", "重新运行 dry-run"),
            ]
            for key, expected, message, action in fingerprint_checks:
                recorded = dry_inputs.get(key, {})
                if isinstance(recorded, dict):
                    recorded = recorded.get("fingerprint", "")
                current = bool(recorded) and str(recorded) == str(expected)
                provenance.append({
                    "source": key,
                    "derived": "distribution-dry-run-results.json",
                    "kind": "fingerprint",
                    "current": current,
                    "expected": expected,
                    "recorded": str(recorded),
                })
                if not current:
                    issues.append(health_issue("warning", f"provenance.{key}_stale", message, action))

    current_result_id = result_identity(results) if results.get("items") else ""
    recorded_result_id = str(results.get("result_id", ""))
    if results.get("items"):
        result_current = bool(recorded_result_id) and recorded_result_id == current_result_id
        provenance.append({
            "source": "distribution-results.json",
            "derived": "result_id",
            "kind": "fingerprint",
            "current": result_current,
            "expected": current_result_id,
            "recorded": recorded_result_id,
        })
        if not recorded_result_id:
            issues.append(health_issue("warning", "provenance.result_id_missing", "平台结果缺少内容指纹", "重新运行结果审计以迁移结果"))
        elif not result_current:
            issues.append(health_issue("error", "provenance.result_id_mismatch", "平台结果内容与 result_id 不一致", "检查结果文件后重新审计"))

    audit_inputs = audit.get("inputs") if isinstance(audit.get("inputs"), dict) else {}
    recorded_audit_id = str(audit.get("audit_id", ""))
    if audit.get("items"):
        current_audit_id = audit_identity(audit, recorded_plan_id or current_plan_id, recorded_result_id or current_result_id)
        audit_current = bool(recorded_audit_id) and recorded_audit_id == current_audit_id
        provenance.append({
            "source": "distribution-audit.json",
            "derived": "audit_id",
            "kind": "fingerprint",
            "current": audit_current,
            "expected": current_audit_id,
            "recorded": recorded_audit_id,
        })
        if not audit_current:
            issues.append(health_issue("warning" if not recorded_audit_id else "error", "provenance.audit_id_stale", "审计记录缺少有效内容指纹", "重新运行 audit"))
        audit_checks = [
            ("plan_id", recorded_plan_id, "审计引用的计划已变化"),
            ("dry_run_id", dry_run_id, "审计引用的 dry-run 已变化"),
            ("result_id", recorded_result_id, "审计引用的平台结果已变化"),
            ("plan_file_sha256", file_sha256(pack / "distribution-plan.json") if (pack / "distribution-plan.json").is_file() else "", "审计后的计划文件已变化"),
            ("dry_run_file_sha256", file_sha256(pack / "distribution-dry-run-results.json") if (pack / "distribution-dry-run-results.json").is_file() else "", "审计后的 dry-run 文件已变化"),
            ("results_file_sha256", file_sha256(pack / "distribution-results.json") if (pack / "distribution-results.json").is_file() else "", "审计后的平台结果文件已变化"),
        ]
        for key, expected, message in audit_checks:
            recorded = str(audit_inputs.get(key, ""))
            current = bool(recorded) and recorded == str(expected)
            provenance.append({
                "source": key,
                "derived": "distribution-audit.json",
                "kind": "fingerprint",
                "current": current,
                "expected": expected,
                "recorded": recorded,
            })
            if not current:
                issues.append(health_issue("warning", f"provenance.audit_{key}_stale", message, "重新运行 audit"))

    stage = read_json(pack / "social-auto-upload-jobs.json", {})
    stage_inputs = stage.get("inputs") if isinstance(stage.get("inputs"), dict) else {}
    if stage.get("jobs"):
        stage_checks = [
            ("plan_id", recorded_plan_id),
            ("dry_run_id", dry_run_id),
            ("plan_file_sha256", file_sha256(pack / "distribution-plan.json") if (pack / "distribution-plan.json").is_file() else ""),
            ("dry_run_file_sha256", file_sha256(pack / "distribution-dry-run-results.json") if (pack / "distribution-dry-run-results.json").is_file() else ""),
        ]
        for key, expected in stage_checks:
            recorded = str(stage_inputs.get(key, ""))
            current = bool(recorded) and recorded == str(expected)
            provenance.append({
                "source": key,
                "derived": "social-auto-upload-jobs.json",
                "kind": "fingerprint",
                "current": current,
                "expected": expected,
                "recorded": recorded,
            })
            if not current:
                issues.append(health_issue("warning", f"provenance.stage_{key}_stale", "安全暂存任务引用了旧计划或旧预检", "重新生成适配任务"))

    for source_name, derived_name, label in (
        ("distribution-plan.json", "distribution-dry-run-results.json", "dry-run 早于最新分发计划"),
        ("distribution-results.json", "distribution-audit.json", "结果审计早于最新平台结果"),
    ):
        source = pack / source_name
        derived = pack / derived_name
        if not source.is_file() or not derived.is_file():
            continue
        source_time = source.stat().st_mtime
        derived_time = derived.stat().st_mtime
        current = derived_time >= source_time
        provenance.append({
            "source": source_name,
            "derived": derived_name,
            "kind": "timestamp",
            "current": current,
            "source_updated_at": file_timestamp(source),
            "derived_updated_at": file_timestamp(derived),
        })
        if not current:
            issues.append({"severity": "warning", "message": label})

    ledger_path = pack / "distribution-evidence.jsonl"
    anchor_path = pack / "distribution-evidence-chain.json"
    ledger = inspect_chained_jsonl(ledger_path, anchor_path)
    ledger_entries = len(ledger["records"])
    ledger_invalid = ledger["invalid_lines"]
    ledger_chain = ledger["chain"]
    ledger_anchor = ledger["anchor"]
    anchor_valid = ledger_anchor["valid"]
    action_log = inspect_chained_jsonl(
        pack / "distribution-dashboard-actions.jsonl",
        pack / "distribution-dashboard-actions-chain.json",
    )
    for scope, label, inspection in (("evidence", "凭证台账", ledger), ("actions", "操作日志", action_log)):
        if not inspection["valid"]:
            issues.append({
                "severity": "error", "code": f"{scope}.{inspection['issue_code']}",
                "message": f"{label}校验失败：{inspection['issue_code']}，已禁止自动重建",
                "action": "停止追加并保留原文件；只读核验最新恢复点，对比台账和锚点后人工决定恢复，不删除或自动重算历史链。参见 docs/draft-resume.md。",
            })

    backups = list_pack_backups(pack, limit=1, verify=True)
    latest_backup = backups[0] if backups else None
    backup_health = latest_backup.get("verification", {}) if latest_backup else {"valid": True, "verified_files": 0, "total_files": 0, "issues": []}
    backup_comparison = latest_backup.get("comparison", {}) if latest_backup else {"valid": True, "unchanged": 0, "changed": 0, "missing": 0, "added": 0, "files": []}
    if latest_backup and not backup_health.get("valid"):
        issues.append({"severity": "warning", "message": "最近恢复点未通过逐文件哈希校验"})
    for issue in issues:
        issue.setdefault("code", f"health.{canonical_sha256(issue.get('message', ''))[:10]}")
        issue.setdefault("action", "按提示修复后重新运行预检或审计")
    status = "error" if any(item["severity"] == "error" for item in issues) else "warning" if issues else "healthy"
    return {
        "status": status,
        "files": files,
        "issues": issues,
        "ledger_entries": ledger_entries,
        "ledger_invalid": ledger_invalid,
        "ledger_chain": ledger_chain,
        "ledger_anchor": {**ledger_anchor, "valid": anchor_valid, "exists": anchor_path.is_file()},
        "action_log": {key: value for key, value in action_log.items() if key != "records"},
        "backup_health": backup_health,
        "backup_comparison": backup_comparison,
        "provenance": provenance,
        "planned_platforms": len(plan_ids),
        "result_platforms": len(result_ids),
    }


def history_quality(status: str) -> str:
    if status in HISTORY_COMPLETE_STATUSES:
        return "complete"
    if status in HISTORY_PARTIAL_STATUSES:
        return "partial"
    if status.startswith("blocked") or status in {"blocked", "needs_credentials"}:
        return "blocked"
    if status in {"unverified", "unverified_editor_timeout", "failed", "failed_autosave_site_error"}:
        return "unverified"
    if status.startswith("package_only"):
        return "package_only"
    return "not_attempted"


def history_summary(pack: Path) -> dict[str, Any]:
    audit = read_json(pack / "distribution-audit.json", {"summary": {}})
    if int(audit.get("summary", {}).get("total", 0)):
        return audit["summary"]
    plan = read_json(pack / "distribution-plan.json", {"items": []})
    results = read_json(pack / "distribution-results.json", {"items": []})
    result_index = {item.get("platform_id"): item for item in results.get("items", [])}
    counts = {quality: 0 for quality in QUALITY_ORDER}
    for item in plan.get("items", []):
        raw = result_index.get(item.get("platform_id"), {})
        counts[history_quality(str(raw.get("status", "not_attempted")))] += 1
    counts["total"] = len(plan.get("items", []))
    return counts


def pack_history(outputs_root: Path, selected_name: str, limit: int = 7) -> list[dict[str, Any]]:
    rows = []
    for pack in pack_directories(outputs_root):
        summary = history_summary(pack)
        total = int(summary.get("total", 0))
        if not total:
            continue
        canonical_images = sum(path.is_file() for path in canonical_image_paths(pack))
        date_match = re.match(r"^(\d{4}-\d{2}-\d{2})", pack.name)
        date = date_match.group(1) if date_match else datetime.fromtimestamp(pack.stat().st_mtime).date().isoformat()
        rows.append({
            "name": pack.name,
            "title": pack_title(pack),
            "date": date,
            "selected": pack.name == selected_name,
            "complete": int(summary.get("complete", 0)),
            "eligible": max(total - int(summary.get("package_only", 0)), 0),
            "completion_rate": completion_rate(summary),
            "image_rate": round(canonical_images / 5 * 100),
            "evidence_rate": pack_evidence_rate(pack),
            "fresh_evidence_rate": pack_fresh_evidence_rate(pack),
        })
        if len(rows) == limit:
            break
    rows = list(reversed(rows))
    for index, row in enumerate(rows):
        row["completion_delta"] = None if index == 0 else row["completion_rate"] - rows[index - 1]["completion_rate"]
    return rows


def history_platform_states(pack: Path) -> dict[str, dict[str, str]]:
    plan = read_json(pack / "distribution-plan.json", {"items": []})
    results = read_json(pack / "distribution-results.json", {"items": []})
    audit = read_json(pack / "distribution-audit.json", {"items": []})
    result_index = {item.get("platform_id"): item for item in results.get("items", [])}
    audit_index = {item.get("platform_id"): item for item in audit.get("items", [])}
    result_time = str(results.get("updated_at") or results.get("generated_at") or file_timestamp(pack / "distribution-results.json"))
    states = {}
    for item in plan.get("items", []):
        platform_id = item.get("platform_id", "")
        if not platform_id:
            continue
        audited = audit_index.get(platform_id, {})
        quality = audited.get("quality") or history_quality(str(result_index.get(platform_id, {}).get("status", "not_attempted")))
        states[platform_id] = {
            "platform": item.get("platform", platform_id),
            "quality": quality,
            "label": QUALITY_LABELS.get(quality, quality),
        }
    return states


def platform_history_matrix(outputs_root: Path, history: list[dict[str, Any]]) -> dict[str, Any]:
    registry = read_json(REGISTRY_PATH, {"platforms": []})
    registry_index = {item.get("id"): item for item in registry.get("platforms", [])}
    state_by_pack = {}
    for column in history:
        pack = ensure_pack(outputs_root, column["name"])
        state_by_pack[column["name"]] = history_platform_states(pack)

    rows = []
    historical_ids = dict.fromkeys([*GRAPHIC_PLATFORMS, *(pid for states in state_by_pack.values() for pid in states)])
    for platform_id in historical_ids:
        cells = []
        complete_count = 0
        observed_count = 0
        fallback_name = registry_index.get(platform_id, {}).get("name", platform_id)
        for column in history:
            state = state_by_pack[column["name"]].get(platform_id)
            if state:
                quality = state["quality"]
                label = state["label"]
                observed_count += 1
                complete_count += int(quality == "complete")
                fallback_name = state.get("platform") or fallback_name
            else:
                quality = "not_available"
                label = QUALITY_LABELS[quality]
            cells.append({
                "pack": column["name"],
                "date": column["date"],
                "quality": quality,
                "label": label,
            })
        rows.append({
            "platform_id": platform_id,
            "platform": fallback_name,
            "complete_count": complete_count,
            "observed_count": observed_count,
            "cells": cells,
        })
    return {"columns": history, "rows": rows}


def merge_platform_rows(pack: Path) -> list[dict[str, Any]]:
    registry = read_json(REGISTRY_PATH, {"platforms": []})
    plan = read_json(pack / "distribution-plan.json", {"items": []})
    dry = read_json(pack / "distribution-dry-run-results.json", {"items": []})
    results = read_json(pack / "distribution-results.json", {"items": []})
    audit = read_json(pack / "distribution-audit.json", {"items": []})
    registry_index = {item["id"]: item for item in registry.get("platforms", [])}
    dry_index = {item.get("platform_id"): item for item in dry.get("items", [])}
    result_index = {item.get("platform_id"): item for item in results.get("items", [])}
    audit_index = {item.get("platform_id"): item for item in audit.get("items", [])}
    result_time = str(
        results.get("updated_at")
        or results.get("generated_at")
        or file_timestamp(pack / "distribution-results.json")
    )
    rows = []
    for item in plan.get("items", []):
        platform_id = item["platform_id"]
        raw_result = result_index.get(platform_id, {})
        verification = verification_fields(raw_result, item)
        audited = audit_index.get(platform_id, {})
        registry_item = registry_index.get(platform_id, {})
        quality = audited.get("quality", "not_attempted")
        evidence = evidence_fields(raw_result, result_time, pack)
        recovery = retry_guidance(str(raw_result.get("status", "not_attempted")), quality)
        if platform_id in PAUSED_PLATFORMS:
            recovery = {"mode": "none", "label": "暂时停用，等待用户明确恢复", "severity": "none"}
        channel_state = {
            "complete": "草稿已留存",
            "partial": "部分内容已留存",
            "blocked": "当前存在阻塞",
            "unverified": "草稿状态待确认",
            "invalid": "验收凭证无效",
            "package_only": "仅生成发布包",
        }.get(quality, dry_index.get(platform_id, {}).get("readiness_status", item.get("status", "尚未处理")))
        rows.append({
            "id": platform_id,
            "name": item.get("platform", platform_id),
            "title": item.get("title", ""),
            "title_chars": item.get("title_chars", 0),
            "content": Path(item.get("content", "")).name if item.get("content") else "",
            "content_chars": item.get("content_chars", 0),
            "delivery": item.get("delivery", ""),
            "readiness": dry_index.get(platform_id, {}).get("readiness_status", item.get("status", "")),
            "channel_state": channel_state,
            "workflow_paused": platform_id in PAUSED_PLATFORMS,
            "quality": quality,
            "raw_status": raw_result.get("status", "not_attempted"),
            "image_count": raw_result.get("image_count", item.get("image_count", 0)),
            "image_verified": raw_result.get("image_verified") is True,
            "video_count": raw_result.get("video_count", item.get("video_count", 0)),
            "video_verified": raw_result.get("video_verified") is True,
            "audio_verified": raw_result.get("audio_verified") is True,
            "media_type": "video" if item.get("required_video_count") else "images",
            "cover_verified": raw_result.get("cover_verified") is True,
            "draft_verified": raw_result.get("draft_verified") is True,
            "verification_recorded": verification["recorded"],
            "verification_passed": verification["passed"],
            "verification_missing": verification["missing"],
            "evidence": evidence,
            "recovery": recovery,
            "proof": raw_result.get("proof", ""),
            "problems": item.get("problems", []) + audited.get("issues", []),
            "editor_url": item.get("editor_url", ""),
            "adapter": item.get("automation_adapter", registry_item.get("automation_adapter", "")),
            "adapter_mode": item.get("adapter_mode", registry_item.get("adapter_mode", "")),
        })
    return rows


def insight_payload(pack: Path, platforms: list[dict[str, Any]]) -> dict[str, Any]:
    audit = read_json(pack / "distribution-audit.json", {"summary": {}, "items": []})
    dry = read_json(pack / "distribution-dry-run-results.json", {"items": []})
    summary = audit.get("summary", {})
    total = len(platforms) or int(summary.get("total", 0))
    dry_validated = sum(1 for item in dry.get("items", []) if item.get("status") == "validated")
    draft_verified = sum(1 for item in platforms if item.get("draft_verified") is True)
    images_verified = sum(1 for item in platforms if item.get("image_verified") is True)
    videos_verified = sum(1 for item in platforms if item.get("video_verified") is True)
    media_verified = sum(
        1 for item in platforms
        if item.get("video_verified") is True or item.get("image_verified") is True
    )
    covers_verified = sum(1 for item in platforms if item.get("cover_verified") is True)
    verification_recorded = sum(1 for item in platforms if item.get("verification_recorded") is True)
    verification_passed = sum(1 for item in platforms if item.get("verification_passed") is True)
    evidence_recorded = sum(1 for item in platforms if item.get("evidence", {}).get("recorded") is True)
    evidence_referenced = sum(1 for item in platforms if item.get("evidence", {}).get("reference_recorded") is True)
    evidence_trusted = sum(1 for item in platforms if item.get("evidence", {}).get("trusted") is True)
    weak_evidence = sum(1 for item in platforms if item.get("evidence", {}).get("reliability") in {"weak", "missing"})
    stale_evidence = sum(1 for item in platforms if item.get("evidence", {}).get("stale") is True)
    complete = int(summary.get("complete", 0))

    status_distribution = []
    for quality in QUALITY_ORDER:
        count = int(summary.get(quality, 0))
        if count:
            status_distribution.append({
                "quality": quality,
                "label": QUALITY_LABELS[quality],
                "count": count,
                "percent": round(count / total * 100) if total else 0,
            })

    issue_queue = []
    for item in platforms:
        quality = item.get("quality", "not_attempted")
        if quality not in ISSUE_PRIORITY:
            continue
        raw_status = item.get("raw_status", "not_attempted")
        recovery = item.get("recovery") or retry_guidance(str(raw_status), quality)
        action, message = ISSUE_MESSAGES.get(
            raw_status,
            (QUALITY_LABELS.get(quality, "检查状态"), "该平台需要人工检查草稿结果。"),
        )
        if item.get("problems"):
            message = str(item["problems"][0])
        action = recovery.get("label") or action
        missing = list(item.get("verification_missing", []))
        if not item.get("verification_recorded"):
            if quality not in {"blocked", "invalid"}:
                action = "补录验收"
            message = f"{message.rstrip('。')}；缺少统一验收记录，待确认：{'、'.join(missing) or '图片、封面、草稿'}。"
        elif missing:
            message = f"{message.rstrip('。')}；待验收：{'、'.join(missing)}。"
        evidence = item.get("evidence", {})
        if not evidence.get("recorded"):
            message = f"{message.rstrip('。')}；缺少结构化凭证。"
        elif not evidence.get("reference_recorded"):
            message = f"{message.rstrip('。')}；凭证缺少可复核引用。"
        elif evidence.get("stale"):
            message = f"{message.rstrip('。')}；凭证已超过 24 小时。"
        issue_queue.append({
            "platform_id": item.get("id", ""),
            "platform": item.get("name", ""),
            "quality": quality,
            "label": QUALITY_LABELS.get(quality, quality),
            "action": action,
            "message": message,
            "image_count": int(item.get("image_count", 0)),
            "video_count": int(item.get("video_count", 0)),
            "media_type": item.get("media_type", "images"),
            "verification_missing": missing,
            "verification_recorded": bool(item.get("verification_recorded")),
            "evidence": evidence,
            "recovery": recovery,
            "editor_url": item.get("editor_url", ""),
        })
    severity_order = {"high": 0, "medium": 1, "low": 2, "none": 3}
    issue_queue.sort(key=lambda item: (
        0 if item.get("evidence", {}).get("stale") else 1,
        severity_order.get(item.get("recovery", {}).get("severity", "medium"), 1),
        ISSUE_PRIORITY[item["quality"]],
        item["platform"],
    ))

    return {
        "completion_rate": completion_rate(summary),
        "verification": {
            "total": total,
            "recorded": verification_recorded,
            "passed": verification_passed,
            "images": images_verified,
            "videos": videos_verified,
            "media": media_verified,
            "covers": covers_verified,
            "drafts": draft_verified,
            "updated_at": file_timestamp(pack / "distribution-results.json"),
            "evidence_recorded": evidence_recorded,
            "evidence_referenced": evidence_referenced,
            "evidence_trusted": evidence_trusted,
            "weak_evidence": weak_evidence,
            "stale_evidence": stale_evidence,
        },
        "recovery": {
            "retryable": sum(1 for item in issue_queue if item.get("recovery", {}).get("mode") == "retry"),
            "manual": sum(1 for item in issue_queue if item.get("recovery", {}).get("mode") == "manual"),
            "start": sum(1 for item in issue_queue if item.get("recovery", {}).get("mode") == "start"),
            "evidence_missing": sum(1 for item in platforms if not item.get("evidence", {}).get("recorded")),
            "reference_missing": sum(1 for item in platforms if not item.get("evidence", {}).get("reference_recorded")),
            "stale": stale_evidence,
        },
        "funnel": [
            {"key": "target", "label": "目标平台", "value": total},
            {"key": "preflight", "label": "预检通过", "value": dry_validated},
            {"key": "draft", "label": "草稿已验收", "value": draft_verified},
            {"key": "images", "label": "图片已验收", "value": images_verified},
            {"key": "media", "label": "视觉素材已验收", "value": media_verified},
            {"key": "complete", "label": "验收闭环", "value": min(verification_passed, complete)},
        ],
        "status_distribution": status_distribution,
        "issue_queue": issue_queue,
    }


def adapter_rows() -> list[dict[str, Any]]:
    config = read_json(ADAPTERS_PATH, {"adapters": []})
    rows = []
    for adapter in config.get("adapters", []):
        item = dict(adapter)
        if item["id"] == "wechat-official":
            item["installed"] = (PROJECT_ROOT / "scripts" / "wechat_draft_publisher.py").is_file()
        elif item["id"] == "wechatsync":
            item["installed"] = (PROJECT_ROOT / "work" / "Wechatsync-latest" / "packages" / "cli" / "dist" / "index.js").is_file()
        elif item["id"] == "social-auto-upload":
            item["installed"] = (PROJECT_ROOT / "research" / "social-auto-upload" / "sau_cli.py").is_file()
        rows.append(item)
    return rows


def delivery_flow(pack: Path, gates: dict[str, Any], platforms: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Describe the selected pack's draft pipeline using only local, auditable files."""
    plan = read_json(pack / "distribution-plan.json", {"items": []})
    dry = read_json(pack / "distribution-dry-run-results.json", {"items": []})
    audit = read_json(pack / "distribution-audit.json", {"items": []})
    staged = read_json(pack / "social-auto-upload-jobs.json", {"jobs": []})

    plan_items = plan.get("items", [])
    dry_items = dry.get("items", [])
    audit_items = audit.get("items", [])
    staged_jobs = staged.get("jobs", [])
    plan_count = len(plan_items)
    dry_count = sum(item.get("status") == "validated" for item in dry_items)
    draft_count = sum(item.get("draft_verified") is True for item in platforms)
    verification_count = sum(item.get("verification_passed") is True for item in platforms)
    supported_stage_ids = set(STAGE_PLATFORMS)
    stage_targets = sum(item.get("platform_id") in supported_stage_ids for item in plan_items)

    def state(done: bool, started: bool = False) -> str:
        if done:
            return "complete"
        return "attention" if started else "pending"

    return [
        {
            "key": "quality",
            "label": "质量门禁",
            "state": state(bool(gates.get("ready")), bool(gates.get("passed"))),
            "detail": f"{gates.get('passed', 0)} / {gates.get('total', 0)} 项通过",
        },
        {
            "key": "plan",
            "label": "分发计划",
            "state": state(plan_count > 0),
            "detail": f"{plan_count} 个平台已纳入" if plan_count else "尚未生成计划",
        },
        {
            "key": "dry_run",
            "label": "统一预检",
            "state": state(plan_count > 0 and dry_count == plan_count, bool(dry_items)),
            "detail": f"{dry_count} / {plan_count} 项验证通过",
        },
        {
            "key": "stage",
            "label": "安全暂存",
            "state": state((stage_targets == 0) or (len(staged_jobs) == stage_targets), bool(staged_jobs)),
            "detail": "本包无需暂存" if stage_targets == 0 else f"{len(staged_jobs)} / {stage_targets} 个任务，仅预览",
        },
        {
            "key": "draft",
            "label": "平台草稿",
            "state": state(plan_count > 0 and draft_count == plan_count, draft_count > 0),
            "detail": f"{draft_count} / {plan_count} 个平台有草稿",
        },
        {
            "key": "audit",
            "label": "结果验收",
            "state": state(
                plan_count > 0 and len(audit_items) == plan_count and verification_count == plan_count,
                bool(audit_items),
            ),
            "detail": f"{verification_count} / {plan_count} 个平台三项闭环",
        },
    ]


def channel_coverage(adapters: list[dict[str, Any]], platforms: list[dict[str, Any]]) -> list[dict[str, Any]]:
    platform_index = {item.get("id"): item for item in platforms}
    rows = []
    covered = set()
    for adapter in adapters:
        ids = [platform_id for platform_id in adapter.get("platforms", []) if platform_id in platform_index]
        if not ids:
            continue
        covered.update(ids)
        rows.append({
            "id": adapter.get("id", ""),
            "name": adapter.get("name", ""),
            "mode": adapter.get("mode", ""),
            "installed": bool(adapter.get("installed")),
            "platforms": [platform_index[platform_id].get("name", platform_id) for platform_id in ids],
            "complete": sum(platform_index[platform_id].get("quality") == "complete" for platform_id in ids),
            "total": len(ids),
        })
    manual_ids = [platform_id for platform_id in platform_index if platform_id not in covered]
    if manual_ids:
        rows.append({
            "id": "browser-draft",
            "name": "已登录浏览器草稿",
            "mode": "manual_draft",
            "installed": True,
            "platforms": [platform_index[platform_id].get("name", platform_id) for platform_id in manual_ids],
            "complete": sum(platform_index[platform_id].get("quality") == "complete" for platform_id in manual_ids),
            "total": len(manual_ids),
        })
    return rows


def system_health(pack: Path | None, adapters: list[dict[str, Any]], activity: list[dict[str, Any]]) -> dict[str, Any]:
    now = datetime.now().astimezone()
    updated_at = datetime.fromtimestamp(pack.stat().st_mtime).astimezone() if pack else None
    runtime = read_json(DASHBOARD_RUNTIME_PATH, {})
    return {
        "service_id": SERVICE_ID,
        "service_version": SERVICE_VERSION,
        "url": runtime.get("url", ""),
        "port": runtime.get("port"),
        "status": "online",
        "started_at": SERVICE_STARTED_AT.isoformat(timespec="seconds"),
        "uptime_seconds": max(int((now - SERVICE_STARTED_AT).total_seconds()), 0),
        "installed_adapters": sum(adapter.get("installed") for adapter in adapters),
        "total_adapters": len(adapters),
        "pack_freshness_minutes": max(int((now - updated_at).total_seconds() / 60), 0) if updated_at else None,
        "last_action_at": activity[0].get("at", "") if activity else "",
        "draft_only": True,
        "direct_publish": False,
        "browser_state_access": False,
        "safe_actions": sorted(SAFE_ACTIONS),
    }


def activity_rows(pack: Path) -> list[dict[str, Any]]:
    log_path = pack / "distribution-dashboard-actions.jsonl"
    if not log_path.is_file():
        return []
    rows = []
    for line in log_path.read_text(encoding="utf-8").splitlines()[-20:]:
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return list(reversed(rows))


def effective_action_state(pack: Path, lock_state: dict[str, Any]) -> dict[str, Any]:
    state = read_json(pack / "distribution-action-state.json", {})
    if state.get("status") == "running" and (not lock_state.get("active") or lock_state.get("stale")):
        return {
            **state,
            "status": "interrupted",
            "reconciled": True,
            "recovery": "上次动作未正常结束，可从对应恢复点继续检查",
        }
    return {**state, "reconciled": False}


def automation_readiness(
    gates: dict[str, Any],
    data_health: dict[str, Any],
    lock_state: dict[str, Any],
    images: dict[str, Any],
    dry_run_passed: bool,
) -> dict[str, Any]:
    blockers = []
    preparation = next((group for group in gates.get("groups", []) if group.get("key") == "preparation"), {})
    if not preparation.get("ready"):
        blockers.append("发布包准备门禁未完成")
    if not images.get("ready"):
        blockers.append("五图未通过深度解码、尺寸或重复检查")
    if not dry_run_passed:
        blockers.append("统一 dry-run 尚未通过")
    if data_health.get("status") != "healthy":
        blockers.append("数据健康尚未恢复正常")
    if lock_state.get("active"):
        blockers.append("当前有安全动作正在运行")
    return {
        "ready": not blockers,
        "label": "可进入自动草稿流程" if not blockers else "暂缓自动草稿流程",
        "blockers": blockers,
    }


def pack_detail(pack: Path) -> dict[str, Any]:
    audit = read_json(pack / "distribution-audit.json", {"summary": {}})
    dry = read_json(pack / "distribution-dry-run-results.json", {"items": []})
    images = []
    image_root = pack / "images"
    canonical = canonical_image_paths(pack)
    canonical_order = {path.resolve(): index for index, path in enumerate(canonical, start=1)}
    image_roles = ["封面", "背景与事实", "用户场景", "方法结构", "风险与行动"]
    from image_quality_gate import required as image_quality_required
    if image_quality_required(pack):
        image_roles = ["封面", "新闻事实", "机制与知识", "实际场景", "行动与边界"]
    if image_root.is_dir():
        candidates = [image for image in image_root.iterdir() if image.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}]
        for image in sorted(candidates, key=lambda path: (canonical_order.get(path.resolve(), 999), path.name)):
            if image.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
                continue
            probe = image_file_probe(image)
            size = (probe["width"], probe["height"]) if probe["decoded"] else None
            canonical_index = canonical_order.get(image.resolve())
            expected_size = (900, 383) if canonical_index == 1 else (1080, 720) if canonical_index else None
            images.append({
                "name": image.name,
                "width": size[0] if size else None,
                "height": size[1] if size else None,
                "bytes": image.stat().st_size,
                "decoded": probe["decoded"],
                "format": probe["format"],
                "sha256": probe["sha256"],
                "decode_error": probe["error"],
                "url": f"/api/image?pack={pack.name}&file={image.name}&sha256={probe['sha256']}",
                "canonical_index": canonical_index,
                "role": image_roles[canonical_index - 1] if canonical_index else "附加素材",
                "expected_width": expected_size[0] if expected_size else None,
                "expected_height": expected_size[1] if expected_size else None,
                "dimension_valid": size == expected_size if expected_size else True,
            })
    files = []
    for path in sorted(pack.iterdir()):
        if path.is_file() and path.suffix.lower() in {".md", ".json"}:
            files.append({"name": path.name, "bytes": path.stat().st_size})
    platforms = merge_platform_rows(pack)
    gates = quality_gates(pack)
    activity = activity_rows(pack)
    lock_state = read_pack_lock(pack)
    action_state = effective_action_state(pack, lock_state)
    backups = list_pack_backups(pack)
    image_validation = canonical_image_validation(pack)
    data_health = pack_data_health(pack)
    if backups:
        backups[0]["verification"] = data_health.get("backup_health", {})
        backups[0]["comparison"] = data_health.get("backup_comparison", {})
    dry_run_passed = bool(dry.get("items")) and all(item.get("status") == "validated" for item in dry["items"])
    return {
        "name": pack.name,
        "title": pack_title(pack),
        "path": str(pack),
        "updated_at": datetime.fromtimestamp(pack.stat().st_mtime).astimezone().isoformat(timespec="seconds"),
        "verification_updated_at": file_timestamp(pack / "distribution-results.json"),
        "image_naming": {
            "scheme": "YYYY-MM-DD-01.png 至 YYYY-MM-DD-05.png",
            "active": [path.name for path in canonical],
            "dated": bool(re.match(r"^\d{4}-\d{2}-\d{2}-\d{2}\.png$", canonical[0].name)) if canonical else False,
        },
        "summary": audit.get("summary", {}),
        "dry_run_passed": dry_run_passed,
        "platforms": platforms,
        "insights": insight_payload(pack, platforms),
        "quality_gates": gates,
        "data_health": data_health,
        "image_validation": image_validation,
        "image_quality": gates.get("image_quality", {}),
        "action_state": action_state,
        "lock_state": lock_state,
        "backups": backups,
        "automation_readiness": automation_readiness(gates, data_health, lock_state, image_validation, dry_run_passed),
        "delivery_flow": delivery_flow(pack, gates, platforms),
        "images": images,
        "files": files,
        "activity": activity,
        "staged_jobs": read_json(pack / "social-auto-upload-jobs.json", {}),
    }


def artifact_inventory(pack: Path) -> list[dict[str, Any]]:
    candidates = [path for path in pack.iterdir() if path.is_file() and path.suffix.lower() in {".md", ".json", ".jsonl"}]
    candidates.extend(path for path in canonical_image_paths(pack) if path.is_file())
    results = read_json(pack / "distribution-results.json", {"items": []})
    for item in results.get("items", []):
        evidence = evidence_fields(item, pack=pack)
        artifact = evidence.get("artifact", {})
        if artifact.get("local") and artifact.get("allowed") and artifact.get("exists"):
            candidates.append(pack / artifact["path"])
    rows = []
    for path in sorted({candidate.resolve() for candidate in candidates}, key=lambda item: str(item)):
        rows.append({
            "path": str(path.relative_to(pack.resolve())).replace("\\", "/"),
            "bytes": path.stat().st_size,
            "sha256": file_sha256(path),
        })
    return rows


def evidence_manifest(pack: Path) -> dict[str, Any]:
    detail = pack_detail(pack)
    manifest = {
        "schema_version": 1,
        "exported_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "policy": "draft_only_never_publish",
        "pack": {
            "name": detail["name"],
            "title": detail["title"],
            "path": detail["path"],
            "updated_at": detail["updated_at"],
        },
        "quality_gates": detail["quality_gates"],
        "data_health": detail["data_health"],
        "image_validation": detail["image_validation"],
        "action_state": detail["action_state"],
        "automation_readiness": detail["automation_readiness"],
        "workflow_ids": {
            "plan_id": read_json(pack / "distribution-plan.json", {}).get("plan_id", ""),
            "dry_run_id": read_json(pack / "distribution-dry-run-results.json", {}).get("result_id", ""),
            "result_id": read_json(pack / "distribution-results.json", {}).get("result_id", ""),
            "audit_id": read_json(pack / "distribution-audit.json", {}).get("audit_id", ""),
        },
        "latest_backup": detail["backups"][0] if detail["backups"] else None,
        "artifacts": artifact_inventory(pack),
        "platforms": [
            {
                "platform_id": item["id"],
                "platform": item["name"],
                "quality": item["quality"],
                "raw_status": item["raw_status"],
                "image_verified": item["image_verified"],
                "video_verified": item["video_verified"],
                "audio_verified": item["audio_verified"],
                "cover_verified": item["cover_verified"],
                "draft_verified": item["draft_verified"],
                "evidence": item["evidence"],
                "recovery": item["recovery"],
            }
            for item in detail["platforms"]
        ],
    }
    manifest["manifest_id"] = canonical_sha256({key: value for key, value in manifest.items() if key != "exported_at"})
    return manifest


def audit_report_markdown(pack: Path) -> str:
    detail = pack_detail(pack)
    summary = detail.get("summary", {})
    verification = detail.get("insights", {}).get("verification", {})
    data_health = detail.get("data_health", {})
    lines = [
        f"# {detail.get('title', pack.name)}：多平台草稿验收报告",
        "",
        f"- 发布包：`{pack.name}`",
        f"- 生成时间：{datetime.now().astimezone().isoformat(timespec='seconds')}",
        f"- 平台数量：{summary.get('total', len(detail.get('platforms', [])))}",
        f"- 完整草稿：{summary.get('complete', 0)}",
        f"- 素材与草稿验收闭环：{verification.get('passed', 0)}",
        f"- 结构化凭证：{verification.get('evidence_recorded', 0)}",
        f"- 新鲜可信凭证：{verification.get('evidence_trusted', 0)}",
        f"- 数据健康：{data_health.get('status', 'unknown')}，台账 {data_health.get('ledger_entries', 0)} 条",
        f"- 凭证哈希链：{'完整且锚点匹配' if data_health.get('ledger_chain', {}).get('valid') and data_health.get('ledger_anchor', {}).get('valid') else '需检查'}，链头 `{data_health.get('ledger_chain', {}).get('head_hash') or '--'}`",
        f"- 五图尺寸：{detail.get('image_validation', {}).get('valid_count', 0)} / 5 张通过",
        "- 策略：只保存草稿，不直接发布",
        "",
        "| 平台 | 状态 | 素材 | 封面 | 草稿 | 凭证可信度 | 引用 | 时效 | 下一步 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for item in detail.get("platforms", []):
        evidence = item.get("evidence", {})
        recovery = item.get("recovery", {})
        lines.append(
            "| {platform} | {quality} | {image} | {cover} | {draft} | {evidence} | {reference} | {freshness} | {next_step} |".format(
                platform=str(item.get("name", "")).replace("|", "\\|"),
                quality=QUALITY_LABELS.get(item.get("quality", ""), item.get("quality", "")),
                image="通过" if (item.get("video_verified") if item.get("media_type") == "video" else item.get("image_verified")) else "待验收",
                cover="通过" if item.get("cover_verified") else "待验收",
                draft="通过" if item.get("draft_verified") else "待验收",
                evidence=f"{evidence.get('reliability', 'missing')} / {evidence.get('score', 0)}",
                reference="有" if evidence.get("reference_recorded") else "缺失",
                freshness="过期" if evidence.get("stale") else "新鲜" if evidence.get("fresh") else "未知",
                next_step=str(recovery.get("label", "--")).replace("|", "\\|"),
            )
        )
    if data_health.get("issues"):
        lines.extend(["", "## 数据健康", ""])
        for issue in data_health["issues"]:
            lines.append(f"- **{issue.get('severity', 'warning')}**：{issue.get('message', '')}")
    lines.extend(["", "## 凭证摘要", ""])
    for item in detail.get("platforms", []):
        evidence = item.get("evidence", {})
        reference = evidence.get("reference") or "无引用"
        lines.append(f"- **{item.get('name', '')}**：{evidence.get('summary') or '未记录凭证摘要'}（引用：`{reference}`）")
    return "\n".join(lines) + "\n"


def dashboard_payload(outputs_root: Path, pack_name: str = "") -> dict[str, Any]:
    packs = list_packs(outputs_root)
    selected_name = pack_name or (packs[0]["name"] if packs else "")
    selected = pack_detail(ensure_pack(outputs_root, selected_name)) if selected_name else None
    history = pack_history(outputs_root, selected_name) if selected_name else []
    adapters = adapter_rows()
    selected_pack = ensure_pack(outputs_root, selected_name) if selected_name else None
    health = system_health(selected_pack, adapters, selected["activity"] if selected else [])
    if selected:
        selected["system_health"] = health
        selected["channel_coverage"] = channel_coverage(adapters, selected["platforms"])
    return {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "draft_only": True,
        "packs": packs,
        "selected": selected,
        "system_health": health,
        "dashboard_state": read_json(DASHBOARD_STATE_PATH, {}),
        "history": history,
        "platform_matrix": platform_history_matrix(outputs_root, history) if history else {"columns": [], "rows": []},
        "adapters": adapters,
        "sources": [
            {"name": "social-auto-upload", "url": "https://github.com/dreammis/social-auto-upload", "license": "MIT"},
            {"name": "Wechatsync", "url": "https://github.com/wechatsync/Wechatsync", "license": "GPL-3.0"},
        ],
    }


def dashboard_revision(outputs_root: Path, pack_name: str = "") -> str:
    """Hash quality inputs, retaining metadata-only polling for historical image packs."""
    rows: list[dict[str, Any]] = []
    watched_names = {
        "README.md",
        "distribution-plan.json",
        "distribution-dry-run-results.json",
        "distribution-results.json",
        "distribution-audit.json",
        "distribution-evidence.jsonl",
        "distribution-evidence-chain.json",
        "distribution-dashboard-actions.jsonl",
        "distribution-dashboard-actions-chain.json",
        "distribution-action-state.json",
        "social-auto-upload-jobs.json",
        "wechat-article.md", "toutiao-article.md", "zhihu-article.md", "xiaohongshu.md", "weibo-article.md", "weibo.md",
        "content-quality-report.json", "content-compliance-report.json", "content-workflow.json", "content-approval.json",
        "claim-verification.json", "research-evidence-report.json", "image-storyboard.json", "image-visual-review.json",
        "image-quality-report.json", "topic-candidates.json", "topic-ranking.json", "growth-experiment.json",
    }
    roots = [path for path in outputs_root.iterdir() if path.is_dir()] if outputs_root.is_dir() else []
    selected = next((path for path in roots if path.name == pack_name), None)
    if selected is None:
        selected = next(iter(pack_directories(outputs_root)), None)
    for pack in roots:
        paths = [path for path in pack.iterdir() if path.is_file() and path.name in watched_names]
        from source_evidence import snapshot_paths, snapshot_path
        source_paths = set()
        for name in snapshot_paths(pack):
            try:
                path = snapshot_path(pack, name)
                if path.is_file():
                    paths.append(path)
                    source_paths.add(path)
                else:
                    rows.append({"path": f"{pack.name}/{name}", "error": "source_missing"})
            except ValueError:
                rows.append({"path": f"{pack.name}/@invalid-source-reference", "error": "source_path_invalid"})
        image_root = pack / "images"
        if image_root.is_dir():
            paths.extend(path for path in image_root.iterdir() if path.is_file())
        if selected is None or pack == selected:
            backup_root = pack / ".backups"
            if backup_root.is_dir():
                paths.extend(path for path in backup_root.glob("*/backup-manifest.json") if path.is_file())
        for path in paths:
            if not path.resolve().is_relative_to(pack.resolve()):
                rows.append({"path": str(path), "error": "outside_pack"})
                continue
            stat = path.stat()
            rows.append({
                "path": str(path.relative_to(outputs_root)).replace("\\", "/"),
                "bytes": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
                "sha256": file_sha256(path) if path.name in watched_names or pack == selected or path in source_paths else "",
            })
    policy_paths = [PROJECT_ROOT / "config" / name for name in (
        "image-quality-standard.json", "content-quality-standard.json", "research-evidence-standard.json",
        "content-workflow-standard.json", "draft-workflow.json")]
    for path in (DASHBOARD_STATE_PATH, STATIC_ROOT / "index.html", STATIC_ROOT / "app.js", STATIC_ROOT / "styles.css", *policy_paths):
        if path.is_file():
            stat = path.stat()
            rows.append({"path": str(path), "bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns, "sha256": file_sha256(path)})
    # Evidence can expire without a file changing. Re-evaluate at least once a minute.
    rows.append({"path": "@evidence-clock", "minute": int(datetime.now().timestamp() // 60)})
    return canonical_sha256(sorted(rows, key=lambda item: item["path"]))


def safe_output(text: str, limit: int = 6000) -> str:
    text = re.sub(
        r"(?i)(token|access_token|secret|appsecret|password|cookie|authorization|api[_-]?key)(\s*[:=]\s*)\S+",
        r"\1\2[redacted]",
        text,
    )
    text = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]+=*", "Bearer [redacted]", text)
    return text[-limit:]


def origin_allowed(origin: str, host: str) -> bool:
    """Allow browser mutations only from this local dashboard origin."""
    if not origin:
        return True
    parsed = urlparse(origin)
    return (
        parsed.scheme == "http"
        and parsed.netloc == host
        and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    )


def host_allowed(host: str) -> bool:
    try:
        hostname = urlparse(f"http://{host}").hostname
    except ValueError:
        return False
    return hostname in {"127.0.0.1", "localhost", "::1"}


def run_safe_action(pack: Path, action: str) -> dict[str, Any]:
    if action not in SAFE_ACTIONS:
        raise ValueError("unsupported action")
    plan = read_json(pack / "distribution-plan.json", {"items": []})
    platform_ids = [item.get("platform_id") for item in plan.get("items", []) if item.get("platform_id")]
    platforms = ",".join(platform_ids or DEFAULT_PLATFORM_IDS)
    command: list[str] | None = None
    if action == "plan":
        command = [sys.executable, str(ROUTER), "plan", "--pack", str(pack), "--platforms", platforms]
    elif action == "dry-run":
        command = [sys.executable, str(ROUTER), "push", "--dry-run", "--pack", str(pack), "--platforms", platforms]
    elif action == "audit":
        command = [sys.executable, str(ROUTER), "audit", "--pack", str(pack)]
    elif action == "stage":
        command = [sys.executable, str(SOCIAL_ADAPTER), "stage", "--pack", str(pack)]
    action_id = uuid.uuid4().hex
    started_at = datetime.now().astimezone().isoformat(timespec="seconds")
    with pack_lock(pack, action) as lock:
        for name in ("distribution-dashboard-actions", "distribution-evidence"):
            require_valid_chained_jsonl(pack / f"{name}.jsonl", pack / f"{name}-chain.json")
        if action == "verify-backup":
            existing_backups = list_pack_backups(pack, limit=1)
            backup = Path(existing_backups[0]["path"]) if existing_backups else None
        else:
            backup = create_pack_backup(pack, action, MUTABLE_PACK_FILES)
        action_state = {
            "schema_version": 1,
            "action_id": action_id,
            "action": action,
            "status": "running",
            "started_at": started_at,
            "finished_at": "",
            "returncode": None,
            "backup": str(backup) if backup else "",
            "lock": {key: lock.get(key) for key in ("pid", "action", "started_at", "inherited")},
        }
        atomic_write_json(pack / "distribution-action-state.json", action_state)
        environment = os.environ.copy()
        environment[PACK_LOCK_ENV] = str(pack.resolve())
        try:
            if action == "verify-backup":
                verification = verify_pack_backup(backup) if backup else {
                    "valid": False, "verified_files": 0, "total_files": 0, "issues": ["没有可验证的恢复点"],
                }
                comparison = compare_pack_backup(pack, backup) if backup else {
                    "valid": False, "unchanged": 0, "changed": 0, "missing": 0, "added": 0, "files": [],
                }
                returncode = 0 if verification["valid"] else 1
                output = json.dumps({"integrity": verification, "comparison": comparison}, ensure_ascii=False)
            else:
                completed = subprocess.run(
                    command,
                    cwd=PROJECT_ROOT,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=180,
                    check=False,
                    env=environment,
                )
                returncode = completed.returncode
                output = safe_output(completed.stdout or completed.stderr)
            status = "ok" if returncode == 0 else "attention"
        except subprocess.TimeoutExpired as exc:
            returncode = -1
            output = safe_output(str(exc))
            status = "timeout"
        finished_at = datetime.now().astimezone().isoformat(timespec="seconds")
        row = {
            "action_id": action_id,
            "at": finished_at,
            "started_at": started_at,
            "action": action,
            "returncode": returncode,
            "status": status,
            "backup": str(backup) if backup else "",
            "output": output,
        }
        atomic_write_json(pack / "distribution-action-state.json", {
            **action_state,
            "status": status,
            "finished_at": finished_at,
            "returncode": returncode,
            "output": output,
        })
        append_chained_jsonl(
            pack / "distribution-dashboard-actions.jsonl",
            pack / "distribution-dashboard-actions-chain.json",
            [row],
        )
    return row


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = f"HuaziDistribution/{SERVICE_VERSION}"

    @property
    def outputs_root(self) -> Path:
        return self.server.outputs_root  # type: ignore[attr-defined]

    def send_json(self, payload: Any, status: int = HTTPStatus.OK, etag: str = "") -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "private, no-cache" if etag else "no-store")
        if etag:
            self.send_header("ETag", etag)
        self.send_security_headers()
        self.end_headers()
        self.wfile.write(body)

    def send_not_modified(self, etag: str) -> None:
        self.send_response(HTTPStatus.NOT_MODIFIED)
        self.send_header("Cache-Control", "private, no-cache")
        self.send_header("ETag", etag)
        self.send_security_headers()
        self.end_headers()

    def send_text(self, text: str, filename: str) -> None:
        body = text.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/markdown; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_security_headers()
        self.end_headers()
        self.wfile.write(body)

    def send_json_download(self, payload: Any, filename: str) -> None:
        body = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_security_headers()
        self.end_headers()
        self.wfile.write(body)

    def send_security_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; base-uri 'none'; form-action 'self'; object-src 'none'; img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'",
        )

    def send_static(self, path: Path) -> None:
        if not path.is_file() or path.parent.resolve() != STATIC_ROOT.resolve():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        body = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_security_headers()
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if not host_allowed(self.headers.get("Host", "")):
            self.send_json({"error": "non-local host rejected"}, HTTPStatus.MISDIRECTED_REQUEST)
            return
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        try:
            if parsed.path == "/api/health":
                now = datetime.now().astimezone()
                self.send_json({
                    "status": "ok",
                    "service_id": SERVICE_ID,
                    "service_version": SERVICE_VERSION,
                    "url": getattr(self.server, "public_url", ""),
                    "port": self.server.server_address[1],
                    "draft_only": True,
                    "direct_publish": False,
                    "uptime_seconds": max(int((now - SERVICE_STARTED_AT).total_seconds()), 0),
                })
                return
            if parsed.path == "/api/dashboard":
                pack_name = query.get("pack", [""])[0]
                etag = f'"{dashboard_revision(self.outputs_root, pack_name)}"'
                if self.headers.get("If-None-Match", "") == etag:
                    self.send_not_modified(etag)
                    return
                self.send_json(dashboard_payload(self.outputs_root, pack_name), etag=etag)
                return
            if parsed.path == "/api/report":
                pack = ensure_pack(self.outputs_root, query.get("pack", [""])[0])
                self.send_text(audit_report_markdown(pack), f"{pack.name}-audit.md")
                return
            if parsed.path == "/api/manifest":
                pack = ensure_pack(self.outputs_root, query.get("pack", [""])[0])
                self.send_json_download(evidence_manifest(pack), f"{pack.name}-evidence-manifest.json")
                return
            if parsed.path == "/api/image":
                pack = ensure_pack(self.outputs_root, query.get("pack", [""])[0])
                filename = Path(query.get("file", [""])[0]).name
                image = (pack / "images" / filename).resolve()
                if image.parent != (pack / "images").resolve() or not image.is_file():
                    raise ValueError("invalid image")
                body = image.read_bytes()
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", mimetypes.guess_type(image.name)[0] or "application/octet-stream")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "private, max-age=60")
                self.send_security_headers()
                self.end_headers()
                self.wfile.write(body)
                return
            if parsed.path in {"/", "/index.html"}:
                self.send_static(STATIC_ROOT / "index.html")
                return
            if parsed.path in {"/app.js", "/styles.css"}:
                self.send_static(STATIC_ROOT / parsed.path.lstrip("/"))
                return
            self.send_error(HTTPStatus.NOT_FOUND)
        except ValueError as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)

    def do_POST(self) -> None:  # noqa: N802
        if not host_allowed(self.headers.get("Host", "")):
            self.send_json({"error": "non-local host rejected"}, HTTPStatus.MISDIRECTED_REQUEST)
            return
        if urlparse(self.path).path != "/api/action":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if not origin_allowed(self.headers.get("Origin", ""), self.headers.get("Host", "")):
            self.send_json({"error": "cross-origin action rejected"}, HTTPStatus.FORBIDDEN)
            return
        if not self.headers.get("Content-Type", "").lower().startswith("application/json"):
            self.send_json({"error": "application/json is required"}, HTTPStatus.UNSUPPORTED_MEDIA_TYPE)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > MAX_ACTION_BODY_BYTES:
                self.send_json({"error": "request body too large"}, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
                return
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            pack = ensure_pack(self.outputs_root, body.get("pack", ""))
            action = body.get("action", "")
            if not ACTION_LOCK.acquire(blocking=False):
                self.send_json({"error": "another dashboard action is already running"}, HTTPStatus.CONFLICT)
                return
            try:
                result = run_safe_action(pack, action)
            finally:
                ACTION_LOCK.release()
            self.send_json({"result": result, "dashboard": dashboard_payload(self.outputs_root, pack.name)})
        except (ValueError, json.JSONDecodeError) as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except PackLockError as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.CONFLICT)

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stdout.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Huazi multi-platform distribution dashboard")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--outputs", default=str(OUTPUTS_ROOT))
    parser.add_argument("--port-attempts", type=int, default=20, help="Ports to try when the preferred port is occupied")
    return parser


def port_is_listening(host: str, port: int, timeout: float = 0.2) -> bool:
    if port == 0:
        return False
    probe_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    try:
        with socket.create_connection((probe_host, port), timeout=timeout):
            return True
    except OSError:
        return False


def create_server(host: str, preferred_port: int, attempts: int = 20) -> ThreadingHTTPServer:
    candidates = [0] if preferred_port == 0 else range(preferred_port, preferred_port + max(attempts, 1))
    last_error: OSError | None = None
    for port in candidates:
        if port_is_listening(host, port):
            last_error = OSError(f"port {port} is occupied")
            continue
        try:
            return ThreadingHTTPServer((host, port), DashboardHandler)
        except OSError as exc:
            last_error = exc
    if last_error:
        raise last_error
    raise OSError("no dashboard port available")


def write_runtime_file(host: str, port: int) -> dict[str, Any]:
    public_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    payload = {
        "service_id": SERVICE_ID,
        "service_version": SERVICE_VERSION,
        "pid": os.getpid(),
        "host": host,
        "port": port,
        "url": f"http://{public_host}:{port}/",
        "started_at": SERVICE_STARTED_AT.isoformat(timespec="seconds"),
    }
    DASHBOARD_RUNTIME_PATH.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(DASHBOARD_RUNTIME_PATH, payload)
    return payload


def main() -> None:
    args = build_parser().parse_args()
    server = create_server(args.host, args.port, args.port_attempts)
    server.outputs_root = Path(args.outputs).resolve()  # type: ignore[attr-defined]
    runtime = write_runtime_file(args.host, server.server_address[1])
    server.public_url = runtime["url"]  # type: ignore[attr-defined]
    if server.server_address[1] != args.port:
        print(f"Port {args.port} is occupied; using {server.server_address[1]} instead.")
    print(f"Huazi distribution dashboard: {runtime['url']}")
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
