#!/usr/bin/env python3
"""Evaluate the research-to-draft gates for a Huazi content pack."""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


SCRIPT_ROOT = Path(__file__).resolve().parent
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

PROJECT_ROOT = SCRIPT_ROOT.parent
GROWTH_STRATEGY_PATH = PROJECT_ROOT / "config" / "content-growth-strategy.json"

from distribution_io import (  # noqa: E402
    atomic_write_json,
    canonical_sha256,
    create_pack_backup,
    file_bundle_snapshot,
    file_sha256,
    five_image_paths,
    image_file_probe,
    pack_date_prefix,
    pack_lock,
)
from content_quality_gate import load_standard as load_content_quality_standard  # noqa: E402
from content_quality_gate import report_is_current as content_quality_report_is_current  # noqa: E402
from research_evidence_gate import load_standard as load_research_evidence_standard  # noqa: E402
from research_evidence_gate import report_is_current as research_evidence_report_is_current  # noqa: E402
from video_voice_profile import load_voice_policy, validate_voice_profile  # noqa: E402
from workflow_policy import STAGE_PLATFORMS, exact_platform_coverage, plan_platform_ids, quality_platform_ids, requires_video


VOICE_PROFILE_REQUIRED_FROM = "2026-08-22"


STAGES = (
    "research_complete",
    "topic_selected",
    "copy_reviewed",
    "assets_verified",
    "preflight_passed",
    "staged",
    "draft_verified",
)


def read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def source_url_count(path: Path) -> int:
    if not path.is_file():
        return 0
    try:
        return len(set(re.findall(r"https?://[^\s)>\]]+", path.read_text(encoding="utf-8"))))
    except (OSError, ValueError):
        return 0


def record_count(value: Any) -> int:
    return value if type(value) is int and value >= 0 else 0


def topic_gate(pack: Path) -> dict[str, Any]:
    from topic_candidate_ranker import validate_ranking
    from requested_topic import request_for_pack

    request = request_for_pack(pack)
    minimum_count = 1 if request else 5

    path = pack / "topic-ranking.json"
    payload = read_json(path)
    selected_id = str(payload.get("selected_candidate_id", ""))
    candidates = payload.get("candidates") if isinstance(payload.get("candidates"), list) else []
    selected = next((item for item in candidates if isinstance(item, dict) and item.get("candidate_id") == selected_id), {})
    integrity = validate_ranking(pack, payload)
    count = record_count(payload.get("candidate_count"))
    score = payload.get("selected_score")
    passed = (
        payload.get("passed") is True
        and bool(selected_id)
        and selected.get("verified") is True
        and count == len(candidates) and count >= minimum_count
        and all(isinstance(item, dict) for item in candidates)
        and type(score) is int and 0 <= score <= 100
        and integrity["passed"]
    )
    issues: list[str] = []
    if not path.is_file():
        issues.append("topic_ranking_missing")
    if path.is_file() and payload.get("passed") is not True:
        reported_issues = payload.get("issues")
        if isinstance(reported_issues, list):
            issues.extend(item for item in reported_issues if isinstance(item, str) and item)
        issues.append("topic_ranking_failed")
    if not selected_id:
        issues.append("selected_topic_missing")
    if selected_id and selected.get("verified") is not True:
        issues.append("selected_topic_not_verified")
    if count != len(candidates) or count < minimum_count:
        issues.append("topic_candidate_count_invalid")
    if any(not isinstance(item, dict) for item in candidates):
        issues.append("topic_candidate_record_invalid")
    if type(score) is not int or not 0 <= score <= 100:
        issues.append("topic_score_invalid")
    issues.extend(integrity["issues"])
    return {
        "passed": passed,
        "path": str(path),
        "sha256": file_sha256(path) if path.is_file() else "",
        "ranking_id": str(payload.get("ranking_id", "")),
        "candidate_count": count,
        "selection_basis": "explicit_user_topic" if request else "daily_candidate_competition",
        "request_id": request["request_id"] if request else "",
        "selected_candidate_id": selected_id,
        "selected_title": str(payload.get("selected_title", "")),
        "selected_score": score if type(score) is int and 0 <= score <= 100 else None,
        "integrity_required": integrity["required"],
        "integrity_passed": integrity["passed"],
        "issues": sorted(set(issues)),
    }


def growth_gate(pack: Path) -> dict[str, Any]:
    from topic_candidate_ranker import growth_experiment_payload, parse_time, validate_ranking

    path = pack / "growth-experiment.json"
    experiment = read_json(path)
    ranking = read_json(pack / "topic-ranking.json")
    strategy = read_json(GROWTH_STRATEGY_PATH)
    config = strategy.get("experiment") if isinstance(strategy.get("experiment"), dict) else {}
    required_fields = [str(item) for item in config.get("required_fields", [])]
    missing = [field for field in required_fields
               if not isinstance(experiment.get(field), str) or not experiment[field].strip()]
    variable = str(experiment.get("variable", ""))
    allowed = {str(item) for item in config.get("allowed_variables", [])}
    issues: list[str] = []
    if not path.is_file():
        issues.append("growth_experiment_missing")
    if missing:
        issues.extend(f"growth_experiment_field_missing:{field}" for field in missing)
    if variable and variable not in allowed:
        issues.append(f"growth_experiment_variable_not_allowed:{variable}")
    if experiment.get("strategy_id") != strategy.get("strategy_id"):
        issues.append("growth_experiment_strategy_mismatch")
    if experiment.get("mode") != strategy.get("active_mode"):
        issues.append("growth_experiment_mode_mismatch")
    if experiment.get("ranking_id") != ranking.get("ranking_id"):
        issues.append("growth_experiment_ranking_mismatch")
    if experiment.get("selected_candidate_id") != ranking.get("selected_candidate_id"):
        issues.append("growth_experiment_candidate_mismatch")
    if experiment.get("status") != "planned":
        issues.append("growth_experiment_not_planned")
    if not experiment.get("experiment_id"):
        issues.append("growth_experiment_id_missing")
    integrity = validate_ranking(pack, ranking)
    if integrity["required"]:
        if not integrity["passed"]:
            issues.append("growth_experiment_ranking_not_current")
        else:
            expected = growth_experiment_payload(ranking, strategy, parse_time(ranking["generated_at"]))
            actual_content = {k: v for k, v in experiment.items() if k != "generated_at"}
            expected_content = {k: v for k, v in expected.items() if k != "generated_at"}
            if actual_content != expected_content:
                issues.append("growth_experiment_content_mismatch")
    passed = path.is_file() and not issues
    return {
        "passed": passed,
        "path": str(path),
        "sha256": file_sha256(path) if path.is_file() else "",
        "experiment_id": str(experiment.get("experiment_id", "")),
        "strategy_id": str(experiment.get("strategy_id", "")),
        "mode": str(experiment.get("mode", "")),
        "variable": variable,
        "signature_move": str(experiment.get("signature_move", "")),
        "issues": sorted(set(issues)),
    }


def fact_gate(pack: Path) -> dict[str, Any]:
    path = pack / "fact-check-notes.md"
    count = source_url_count(path)
    notes_passed = path.is_file() and path.stat().st_size > 100 and count >= 2
    report_path = pack / "research-evidence-report.json"
    report = read_json(report_path)
    standard = load_research_evidence_standard()
    evidence_required = (pack_date_prefix(pack) or "") >= str(standard.get("effective_from", ""))
    evidence_current = report_path.is_file() and research_evidence_report_is_current(report, pack, standard)
    evidence_valid = (
        report_path.is_file()
        and report.get("policy_id") == standard.get("policy_id")
        and report.get("passed") is True
        and evidence_current
    )
    evidence_passed = evidence_valid or (not evidence_required and not report_path.is_file())
    issues: list[str] = []
    if not notes_passed:
        issues.append("fact_sources_insufficient")
    if evidence_required and not report_path.is_file():
        issues.append("research_evidence_report_missing")
    elif report_path.is_file() and report.get("policy_id") != standard.get("policy_id"):
        issues.append("research_evidence_policy_mismatch")
    elif report_path.is_file() and report.get("passed") is not True:
        issues.append("research_evidence_report_failed")
    elif report_path.is_file() and not evidence_current:
        issues.append("research_evidence_report_stale")
    return {
        "passed": notes_passed and evidence_passed,
        "path": str(path),
        "sha256": file_sha256(path) if path.is_file() else "",
        "source_url_count": count,
        "evidence_required": evidence_required,
        "evidence_passed": evidence_passed,
        "evidence_report": str(report_path),
        "evidence_report_sha256": file_sha256(report_path) if report_path.is_file() else "",
        "evidence_report_id": str(report.get("report_id", "")),
        "claim_count": record_count(report.get("claim_count")),
        "source_domain_count": record_count(report.get("source_domain_count")),
        "issues": issues,
    }


def copy_gate(pack: Path) -> dict[str, Any]:
    path = pack / "content-compliance-report.json"
    payload = read_json(path)
    expected = plan_platform_ids(pack)
    coverage = (exact_platform_coverage(payload.get("items", []), expected)
                and record_count(payload.get("platform_count")) == len(expected))
    compliance_passed = payload.get("passed") is True and coverage and all(item.get("passed") is True for item in payload.get("items", []))
    quality_path = pack / "content-quality-report.json"
    quality = read_json(quality_path)
    quality_standard = load_content_quality_standard()
    quality_required = (pack_date_prefix(pack) or "") >= str(quality_standard.get("effective_from", ""))
    quality_current = (
        quality_path.is_file() and content_quality_report_is_current(quality, pack)
        and exact_platform_coverage(quality.get("items", []), quality_platform_ids(pack))
    )
    quality_valid = (
        quality_path.is_file()
        and quality.get("policy_id") == quality_standard.get("policy_id")
        and quality.get("passed") is True
        and quality_current
    )
    quality_passed = quality_valid or (not quality_required and not quality_path.is_file())
    passed = compliance_passed and quality_passed
    issues: list[str] = []
    if not path.is_file():
        issues.append("compliance_report_missing")
    if path.is_file() and (payload.get("passed") is not True or (coverage and not compliance_passed)):
        issues.append("compliance_report_failed")
    if path.is_file() and not coverage:
        issues.append("compliance_platform_coverage_incomplete")
    if quality_required and not quality_path.is_file():
        issues.append("content_quality_report_missing")
    elif quality_path.is_file() and quality.get("policy_id") != quality_standard.get("policy_id"):
        issues.append("content_quality_policy_mismatch")
    elif quality_path.is_file() and not quality_current:
        issues.append("content_quality_report_stale")
    elif quality_path.is_file() and quality.get("passed") is not True:
        issues.append("content_quality_report_failed")
    return {
        "passed": passed,
        "path": str(path),
        "sha256": file_sha256(path) if path.is_file() else "",
        "policy_id": str(payload.get("policy_id", "")),
        "platform_count": record_count(payload.get("platform_count")),
        "blocking_count": record_count(payload.get("blocking_count")),
        "quality_required": quality_required,
        "quality_passed": quality_passed,
        "quality_report": str(quality_path),
        "quality_report_sha256": file_sha256(quality_path) if quality_path.is_file() else "",
        "quality_score": quality.get("pack_score") if quality_current else None,
        "quality_input_fingerprint": str(quality.get("input_fingerprint", "")),
        "issues": issues,
    }


def asset_gate(pack: Path) -> dict[str, Any]:
    from image_quality_gate import gate as image_quality_gate

    visual = image_quality_gate(pack)
    image_checks: list[dict[str, Any]] = []
    hashes: set[str] = set()
    for index, path in enumerate(five_image_paths(pack), start=1):
        probe = image_file_probe(path)
        expected = (900, 383) if index == 1 else (1080, 720)
        passed = (
            probe.get("decoded") is True
            and probe.get("format") == "PNG"
            and (probe.get("width"), probe.get("height")) == expected
            and bool(probe.get("sha256"))
        )
        if probe.get("sha256"):
            hashes.add(str(probe["sha256"]))
        image_checks.append({"path": str(path), "expected": list(expected), "passed": passed, "probe": probe})

    images_passed = len(image_checks) >= 5 and all(item["passed"] for item in image_checks) and len(hashes) == len(image_checks)
    if not requires_video(pack):
        from requested_topic import inline_video_gate
        inline = inline_video_gate(pack)
        return {
            "passed": images_passed and visual["passed"] and inline["passed"],
            "images_passed": images_passed,
            "unique_image_count": len(hashes),
            "image_checks": image_checks,
            "video_required": False,
            "video_status": "not_required",
            "voice_profile_required": False,
            "image_quality": visual,
            "inline_videos": inline,
            "issues": ([] if images_passed else ["five_image_quality_failed"]) + [item["code"] for item in visual["issues"]] + inline["issues"],
        }
    video_path = pack / "video-quality-report.json"
    video = read_json(video_path)
    raw_video = Path(str(video.get("video", ""))) if video.get("video") else None
    current_video_sha = file_sha256(raw_video) if raw_video and raw_video.is_file() else ""
    raw_cover = Path(str(video.get("cover", ""))) if video.get("cover") else None
    cover_current = bool(raw_cover and raw_cover.is_file()) and file_sha256(raw_cover) == video.get("cover_sha256")
    video_current = bool(current_video_sha) and current_video_sha == video.get("video_sha256") and cover_current
    video_passed = video.get("passed") is True and video_current

    voice_path = pack / "video-audio-profile.json"
    voice_required = (pack_date_prefix(pack) or "") >= VOICE_PROFILE_REQUIRED_FROM
    voice_profile = read_json(voice_path)
    voice_policy = load_voice_policy()
    voice_present_and_valid = voice_path.is_file() and not validate_voice_profile(voice_profile, voice_policy)
    voice_passed = voice_present_and_valid or (not voice_required and not voice_path.is_file())
    images_passed = len(image_checks) >= 5 and all(item["passed"] for item in image_checks) and len(hashes) == len(image_checks)
    issues: list[str] = []
    if not images_passed:
        issues.append("five_image_quality_failed")
    if not video_path.is_file():
        issues.append("video_quality_report_missing")
    elif not video_current:
        issues.append("video_quality_report_stale")
    elif video.get("passed") is not True:
        issues.append("video_quality_report_failed")
    if voice_required and not voice_path.is_file():
        issues.append("video_audio_profile_missing")
    elif voice_path.is_file() and not voice_present_and_valid:
        issues.append("video_audio_profile_invalid")
    return {
        "passed": images_passed and video_passed and voice_passed and visual["passed"],
        "images_passed": images_passed,
        "unique_image_count": len(hashes),
        "image_checks": image_checks,
        "video_required": True,
        "video_passed": video_passed,
        "video_report": str(video_path),
        "video_report_sha256": file_sha256(video_path) if video_path.is_file() else "",
        "video_report_id": str(video.get("report_id", "")),
        "voice_profile_required": voice_required,
        "voice_profile_passed": voice_passed,
        "voice_profile": str(voice_path),
        "voice_profile_sha256": file_sha256(voice_path) if voice_path.is_file() else "",
        "image_quality": visual,
        "issues": issues + [item["code"] for item in visual["issues"]],
    }


def preflight_gate(pack: Path) -> dict[str, Any]:
    from social_draft_router import plan_identity, result_identity, workflow_bundles

    plan_path = pack / "distribution-plan.json"
    dry_path = pack / "distribution-dry-run-results.json"
    plan = read_json(plan_path)
    dry = read_json(dry_path)
    items = dry.get("items") if isinstance(dry.get("items"), list) else []
    inputs = dry.get("inputs") if isinstance(dry.get("inputs"), dict) else {}
    plan_hash = file_sha256(plan_path) if plan_path.is_file() else ""
    expected = plan_platform_ids(pack)
    coverage = exact_platform_coverage(items, expected)
    keys = {"content_bundle": "content", "image_bundle": "images",
            "video_asset_bundle": "video_assets", "quality_gate_bundle": "quality_gates"}
    bundle_status = {key: False for key in keys}
    try:
        # Derive scope from the plan and policy, never from a self-reported file list.
        bundles = workflow_bundles(pack, plan)
        for key, name in keys.items():
            current = bundles[name]
            empty_allowed = key == "video_asset_bundle" and not requires_video(pack)
            bundle_status[key] = ((current.get("valid") is True or (empty_allowed and current.get("files") == []))
                                  and (bool(current.get("files")) or empty_allowed)
                                  and inputs.get(key) == current)
    except (OSError, ValueError, TypeError, AttributeError, KeyError):
        pass
    plan_valid = bool(plan.get("plan_id")) and plan.get("plan_id") == plan_identity(plan)
    dry_valid = bool(dry.get("result_id")) and dry.get("result_id") == result_identity(dry)
    passed = (
        plan_path.is_file()
        and dry_path.is_file()
        and dry.get("dry_run") is True
        and plan_valid and dry_valid
        and coverage
        and all(isinstance(item, dict) and item.get("status") == "validated" for item in items)
        and inputs.get("plan_id") == plan.get("plan_id")
        and inputs.get("plan_file_sha256") == plan_hash
        and all(bundle_status.values())
    )
    issues: list[str] = []
    if not plan_path.is_file():
        issues.append("distribution_plan_missing")
    if not dry_path.is_file():
        issues.append("distribution_dry_run_missing")
    if not coverage:
        issues.append("dry_run_platform_coverage_mismatch")
    if not plan.get("plan_id") or not dry.get("result_id"):
        issues.append("workflow_identity_missing")
    if not plan_valid:
        issues.append("distribution_plan_identity_invalid")
    if not dry_valid:
        issues.append("dry_run_identity_invalid")
    if dry.get("dry_run") is not True:
        issues.append("dry_run_marker_invalid")
    if inputs.get("plan_id") != plan.get("plan_id"):
        issues.append("dry_run_plan_id_mismatch")
    if any(not isinstance(item, dict) or item.get("status") != "validated" for item in items):
        issues.append("dry_run_contains_non_validated_items")
    if inputs.get("plan_file_sha256") != plan_hash:
        issues.append("dry_run_plan_hash_stale")
    for key, current in bundle_status.items():
        if not current:
            issues.append(f"dry_run_{key}_stale_or_missing")
    return {
        "passed": passed,
        "plan_id": str(plan.get("plan_id", "")),
        "dry_run_id": str(dry.get("result_id", "")),
        "plan_sha256": plan_hash,
        "dry_run_sha256": file_sha256(dry_path) if dry_path.is_file() else "",
        "platform_count": len(items),
        "bundle_status": bundle_status,
        "issues": issues,
    }


def review_gate(pack: Path) -> dict[str, Any]:
    path = pack / "content-approval.json"
    payload = read_json(path)
    decision = "policy_auto" if not path.exists() else payload.get("decision")
    valid = isinstance(decision, str) and decision in {"policy_auto", "approved", "hold", "rejected"}
    issues = [] if valid else ["manual_review_invalid"]
    if decision in ("hold", "rejected"):
        issues.append(f"manual_review_{decision}")
    if path.exists() and (type(payload.get("schema_version")) is not int or payload.get("schema_version") != 1):
        issues.append("manual_review_invalid")
    if "approval_id" in payload and payload["approval_id"] != canonical_sha256({k: v for k, v in payload.items() if k != "approval_id"}):
        issues.append("manual_review_identity_invalid")
    passed = not issues
    return {
        "passed": passed,
        "decision": decision if isinstance(decision, str) else "invalid",
        "reviewer": str(payload.get("reviewer", "")),
        "reviewed_at": str(payload.get("reviewed_at", "")),
        "note": str(payload.get("note", "")),
        "path": str(path),
        "sha256": file_sha256(path) if path.is_file() else "",
        "issues": sorted(set(issues)),
    }


def result_gate(pack: Path) -> dict[str, Any]:
    path = pack / "distribution-audit.json"
    audit = read_json(path)
    summary = audit.get("summary") if isinstance(audit.get("summary"), dict) else {}
    complete = int(summary.get("complete") or 0)
    expected = plan_platform_ids(pack)
    passed = (audit.get("complete") is True
              and int(summary.get("total") or 0) == len(expected)
              and complete == len(expected)
              and exact_platform_coverage(audit.get("items", []), expected))
    return {
        "passed": passed,
        "complete_platforms": complete,
        "total_platforms": int(summary.get("total") or 0),
        "audit_id": str(audit.get("audit_id", "")),
        "path": str(path),
        "sha256": file_sha256(path) if path.is_file() else "",
        "issues": [] if passed else ["draft_verification_incomplete"],
    }


def evaluate_pack(pack: Path) -> dict[str, Any]:
    gates = {
        "research": topic_gate(pack),
        "growth": growth_gate(pack),
        "facts": fact_gate(pack),
        "review": review_gate(pack),
        "copy": copy_gate(pack),
        "assets": asset_gate(pack),
        "preflight": preflight_gate(pack),
        "drafts": result_gate(pack),
    }
    staged = read_json(pack / "social-auto-upload-jobs.json")
    stage_inputs = staged.get("inputs", {})
    stage_current = (
        bool(staged.get("jobs"))
        and stage_inputs.get("plan_id") == gates["preflight"]["plan_id"]
        and stage_inputs.get("dry_run_id") == gates["preflight"]["dry_run_id"]
        and stage_inputs.get("plan_file_sha256") == gates["preflight"]["plan_sha256"]
        and stage_inputs.get("dry_run_file_sha256") == gates["preflight"]["dry_run_sha256"]
        and all(job.get("platform_id") in STAGE_PLATFORMS for job in staged.get("jobs", []))
        and all(job.get("status") != "blocked" for job in staged.get("jobs", []))
    )
    stage_checks = {
        "research_complete": gates["research"]["passed"] and gates["growth"]["passed"] and gates["facts"]["passed"],
        "topic_selected": gates["research"]["passed"] and gates["growth"]["passed"] and gates["review"]["passed"],
        "copy_reviewed": gates["copy"]["passed"] and gates["review"]["passed"],
        "assets_verified": gates["assets"]["passed"],
        "preflight_passed": gates["preflight"]["passed"],
        "staged": stage_current,
        "draft_verified": gates["drafts"]["passed"],
    }
    highest = "not_started"
    for stage in STAGES:
        if stage_checks[stage]:
            highest = stage
        else:
            break
    issues = sorted({issue for gate in gates.values() for issue in gate.get("issues", [])})
    payload = {
        "schema_version": 1,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "pack": str(pack.resolve()),
        "highest_contiguous_stage": highest,
        "ready_for_draft_actions": all(stage_checks[stage] for stage in STAGES[:5]),
        "stage_checks": stage_checks,
        "gates": gates,
        "issues": issues,
    }
    payload["workflow_id"] = canonical_sha256({
        "pack": payload["pack"],
        "highest_contiguous_stage": payload["highest_contiguous_stage"],
        "ready_for_draft_actions": payload["ready_for_draft_actions"],
        "stage_checks": payload["stage_checks"],
        "gates": payload["gates"],
        "issues": payload["issues"],
    })
    return payload


def write_review(pack: Path, decision: str, reviewer: str, note: str) -> dict[str, Any]:
    if decision not in {"approved", "hold", "rejected", "policy_auto"}:
        raise ValueError("manual_review_invalid")
    payload = {
        "schema_version": 1,
        "decision": decision,
        "reviewer": reviewer,
        "reviewed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "note": note,
    }
    payload["approval_id"] = canonical_sha256(payload)
    create_pack_backup(pack, "content-review", ["content-approval.json"], keep=10)
    atomic_write_json(pack / "content-approval.json", payload)
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Huazi content workflow gate")
    sub = parser.add_subparsers(dest="command", required=True)
    evaluate = sub.add_parser("evaluate")
    evaluate.add_argument("--pack", required=True)
    evaluate.add_argument("--output", default="")
    review = sub.add_parser("review")
    review.add_argument("--pack", required=True)
    review.add_argument("--decision", required=True, choices=["approved", "hold", "rejected", "policy_auto"])
    review.add_argument("--reviewer", default="huazi-workflow")
    review.add_argument("--note", default="")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    pack = Path(args.pack).resolve()
    if not pack.is_dir():
        raise SystemExit(f"pack not found: {pack}")
    if args.command == "review":
        with pack_lock(pack, "content-review"):
            payload = write_review(pack, args.decision, args.reviewer, args.note)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    with pack_lock(pack, "content-workflow"):
        payload = evaluate_pack(pack)
        output = Path(args.output).resolve() if args.output else pack / "content-workflow.json"
        atomic_write_json(output, payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    print(f"Wrote workflow state: {output}")
    return 0 if payload["ready_for_draft_actions"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
