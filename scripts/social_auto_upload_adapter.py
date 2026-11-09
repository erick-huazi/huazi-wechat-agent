#!/usr/bin/env python3
"""Stage Huazi image and video jobs without executing publish actions."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


SCRIPT_ROOT = Path(__file__).resolve().parent
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

from distribution_io import (  # noqa: E402
    LEGACY_FIVE_IMAGE_NAMES,
    atomic_write_json,
    file_sha256,
    five_image_paths,
    image_file_probe,
    pack_date_prefix,
    pack_lock,
    video_file_probe,
)
from content_workflow_gate import evaluate_pack as evaluate_content_workflow  # noqa: E402


PROJECT_ROOT = SCRIPT_ROOT.parent
DEFAULT_REPO = PROJECT_ROOT / "research" / "social-auto-upload"
# Historical compatibility for undated packs and older test fixtures only.
CANONICAL_IMAGES = list(LEGACY_FIVE_IMAGE_NAMES)
TARGETS = {
    "xiaohongshu": {"upstream": "xiaohongshu", "media_type": "images", "content": "xiaohongshu.md", "max_chars": 1000},
    "douyin-video": {"upstream": "douyin", "media_type": "video", "content": "douyin-video.md", "max_chars": 1000},
    "video-channel": {"upstream": "tencent", "media_type": "video", "content": "video-channel.md", "max_chars": 1000, "draft_flag": True},
    "kuaishou-video": {"upstream": "kuaishou", "media_type": "video", "content": "kuaishou-video.md", "max_chars": 500},
    # Historical compatibility only. New packs do not stage these targets.
    "douyin-image": {"upstream": "douyin", "media_type": "images", "content": "douyin.md", "max_chars": 1000, "legacy": True},
    "kuaishou-image": {"upstream": "kuaishou", "media_type": "images", "content": "kuaishou.md", "max_chars": 500, "legacy": True},
}
from workflow_policy import STAGE_PLATFORMS, require_active

DEFAULT_TARGETS = STAGE_PLATFORMS


def first_heading(path: Path) -> str:
    if not path.is_file():
        return ""
    match = re.search(r"^#\s+(.+)$", path.read_text(encoding="utf-8"), flags=re.M)
    return match.group(1).strip() if match else ""


def markdown_note(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    text = re.sub(r"^#\s+.+\r?\n", "", text, count=1)
    text = re.sub(r"!\[[^\]]*\]\([^)]+\)", "", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def extract_tags(text: str) -> list[str]:
    tags: list[str] = []
    for tag in re.findall(r"(?<!\w)#([^#\s]{1,24})", text):
        cleaned = tag.strip("，。！？；：,.!?;:")
        if cleaned and cleaned not in tags:
            tags.append(cleaned)
    return tags[:10]


def repo_commit(repo: Path) -> str:
    if not (repo / ".git").exists():
        return ""
    completed = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
        check=False,
    )
    return completed.stdout.strip() if completed.returncode == 0 else ""


def canonical_image_paths(pack: Path) -> list[Path]:
    return five_image_paths(pack)


def video_paths(pack: Path) -> tuple[Path, Path]:
    date = pack_date_prefix(pack)
    prefix = date or "short"
    return pack / "video" / f"{prefix}-short-video.mp4", pack / "video" / f"{prefix}-video-cover.png"


def adapter_status(repo: Path) -> dict[str, Any]:
    cli = repo / "sau_cli.py"
    return {
        "adapter": "social-auto-upload",
        "repo": str(repo),
        "installed": cli.is_file(),
        "commit": repo_commit(repo),
        "license": "MIT",
        "supported_platforms": list(DEFAULT_TARGETS),
        "legacy_platforms": [key for key, value in TARGETS.items() if value.get("legacy")],
        "mode": "stage_only",
        "execution_enabled": False,
        "reason": "image draft staging only; no upstream direct-publish execution; retired targets remain available for historical inspection only",
    }


def build_job(pack: Path, platform_id: str, repo: Path) -> dict[str, Any]:
    target = TARGETS[platform_id]
    content = pack / target["content"]
    problems: list[str] = []
    if not content.is_file():
        problems.append(f"missing_content:{content.name}")
    title = first_heading(content) if content.is_file() else ""
    note = markdown_note(content) if content.is_file() else ""
    if len(note) > target["max_chars"]:
        problems.append(f"content_too_long:{len(note)}>{target['max_chars']}")
    images: list[Path] = []
    missing_images: list[str] = []
    video: Path | None = None
    cover: Path | None = None
    probe: dict[str, Any] = {}

    if target["media_type"] == "images":
        images = canonical_image_paths(pack)
        missing_images = [path.name for path in images if not path.is_file()]
        if missing_images:
            problems.append("missing_images:" + ",".join(missing_images))
        invalid_dimensions = []
        image_hashes: dict[str, list[str]] = {}
        for index, path in enumerate(images, start=1):
            expected = (900, 383) if index == 1 else (1080, 720)
            image_probe = image_file_probe(path)
            actual = (image_probe["width"], image_probe["height"]) if image_probe["decoded"] else None
            if path.is_file() and (image_probe["format"] != "PNG" or actual != expected):
                invalid_dimensions.append(f"{path.name}!={expected[0]}x{expected[1]}")
            if image_probe["sha256"]:
                image_hashes.setdefault(image_probe["sha256"], []).append(path.name)
        if invalid_dimensions:
            problems.append("invalid_image_dimensions:" + ",".join(invalid_dimensions))
        duplicates = [names for names in image_hashes.values() if len(names) > 1]
        if duplicates:
            problems.append("duplicate_images:" + ";".join(",".join(names) for names in duplicates))
        preview = [
            str(repo / "sau_cli.py"), target["upstream"], "upload-note",
            "--account", "<account>", "--images", *[str(path) for path in images],
            "--title", title, "--note", "<content from staged file>",
        ]
    else:
        video, cover = video_paths(pack)
        probe = video_file_probe(video)
        if not probe.get("decoded") and probe.get("error") == "ffprobe_unavailable":
            report_path = pack / "video-quality-report.json"
            try:
                report = json.loads(report_path.read_text(encoding="utf-8")) if report_path.is_file() else {}
            except json.JSONDecodeError:
                report = {}
            quality_probe = report.get("technical_probe", {})
            current_video_sha = file_sha256(video) if video.is_file() else ""
            current_cover_sha = file_sha256(cover) if cover.is_file() else ""
            if (
                report.get("passed") is True
                and report.get("video_sha256") == current_video_sha
                and report.get("cover_sha256") == current_cover_sha
                and quality_probe.get("decoded")
                and quality_probe.get("sha256") == current_video_sha
            ):
                probe = {**quality_probe, "source": "current_video_quality_report"}
        if not video.is_file():
            problems.append(f"missing_video:{video.name}")
        elif not probe.get("decoded"):
            problems.append(f"invalid_video:{probe.get('error') or 'decode_failed'}")
        else:
            if (probe.get("width"), probe.get("height")) != (1080, 1920):
                problems.append(f"invalid_video_dimensions:{probe.get('width')}x{probe.get('height')}!=1080x1920")
            if not probe.get("has_audio"):
                problems.append("video_audio_missing")
            duration = float(probe.get("duration") or 0)
            if duration < 15 or duration > 180:
                problems.append(f"video_duration_out_of_range:{duration:.1f}")
        cover_probe = image_file_probe(cover)
        if not cover.is_file():
            problems.append(f"missing_cover:{cover.name}")
        elif not cover_probe.get("decoded") or cover_probe.get("format") != "PNG" or (
            cover_probe.get("width"), cover_probe.get("height")
        ) != (1080, 1440):
            problems.append("invalid_video_cover:expected_1080x1440_png")
        preview = [
            str(repo / "sau_cli.py"), target["upstream"], "upload-video",
            "--account", "<account>", "--file", str(video),
            "--title", title, "--desc", "<content from staged file>",
            "--thumbnail", str(cover),
        ]
        if target.get("draft_flag"):
            preview.append("--draft")
    return {
        "platform_id": platform_id,
        "upstream_platform": target["upstream"],
        "media_type": target["media_type"],
        "status": "ready_for_manual_adapter_review" if not problems else "blocked",
        "title": title,
        "content_file": str(content),
        "content_chars": len(note),
        "tags": extract_tags(note),
        "images": [str(path) for path in images],
        "image_count": len(images) - len(missing_images),
        "video": str(video) if video else "",
        "video_count": int(bool(video and video.is_file())),
        "video_probe": probe,
        "cover": str(cover) if cover else "",
        "command_preview": preview,
        "execution_enabled": False,
        "problems": problems,
    }


def stage(pack: Path, repo: Path, platforms: list[str]) -> dict[str, Any]:
    require_active(platforms)
    unknown = [platform for platform in platforms if platform not in TARGETS]
    if unknown:
        raise ValueError("unsupported social-auto-upload platform: " + ",".join(unknown))
    jobs = [build_job(pack, platform, repo) for platform in platforms]
    plan_path = pack / "distribution-plan.json"
    dry_run_path = pack / "distribution-dry-run-results.json"
    plan = json.loads(plan_path.read_text(encoding="utf-8")) if plan_path.is_file() else {}
    dry_run = json.loads(dry_run_path.read_text(encoding="utf-8")) if dry_run_path.is_file() else {}
    workflow = evaluate_content_workflow(pack)
    planned = {item.get("platform_id") for item in plan.get("items", [])}
    for job in jobs:
        if job["platform_id"] not in planned:
            job["status"] = "blocked"
            job.setdefault("problems", []).append("stage_platform_not_in_plan")
    if not workflow.get("ready_for_draft_actions"):
        for job in jobs:
            job["status"] = "blocked"
            job.setdefault("problems", []).append("content_workflow_gate_not_ready")
    results_path = pack / "distribution-results.json"
    if results_path.is_file():
        from social_draft_router import resume_context, resume_report

        try:
            resume_plan, _, results = resume_context(pack)
            decisions = {row["platform_id"]: row for row in resume_report(pack, resume_plan, results)["items"]}
        except (ValueError, KeyError, TypeError):
            decisions = {}
        for job in jobs:
            decision = decisions.get(job["platform_id"], {"action": "wait_for_input", "code": "RESUME_RESULTS_INVALID"})
            job["resume"] = decision
            if decision["action"] != "dispatch":
                job["command_preview"] = []
                if job["status"] != "blocked":
                    job["status"] = decision["action"]
    return {
        "schema_version": 2,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "pack": str(pack),
        "inputs": {
            "plan_id": plan.get("plan_id", ""),
            "plan_file_sha256": file_sha256(plan_path) if plan_path.is_file() else "",
            "dry_run_id": dry_run.get("result_id", ""),
            "dry_run_file_sha256": file_sha256(dry_run_path) if dry_run_path.is_file() else "",
            "workflow_id": workflow.get("workflow_id", ""),
            "results_file_sha256": file_sha256(results_path) if results_path.is_file() else "",
        },
        "workflow": workflow,
        "adapter": adapter_status(repo),
        "execution_policy": "preview_only_never_publish",
        "jobs": jobs,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Safe social-auto-upload staging adapter")
    parser.add_argument("--repo", default=str(DEFAULT_REPO))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    stage_parser = sub.add_parser("stage")
    stage_parser.add_argument("--pack", required=True)
    stage_parser.add_argument("--platforms", default=",".join(DEFAULT_TARGETS))
    stage_parser.add_argument("--output", default="")
    return parser


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = build_parser().parse_args()
    repo = Path(args.repo).resolve()
    if args.command == "status":
        print(json.dumps(adapter_status(repo), ensure_ascii=False, indent=2))
        return 0
    pack = Path(args.pack).resolve()
    if not pack.is_dir():
        raise SystemExit(f"pack not found: {pack}")
    platforms = [item.strip() for item in args.platforms.split(",") if item.strip()]
    with pack_lock(pack, "stage"):
        payload = stage(pack, repo, platforms)
        output = Path(args.output).resolve() if args.output else pack / "social-auto-upload-jobs.json"
        atomic_write_json(output, payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    print(f"Wrote staged jobs: {output}")
    return 1 if any(job["status"] == "blocked" for job in payload["jobs"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
