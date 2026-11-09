#!/usr/bin/env python3
"""Atomically merge one verified browser/API outcome into distribution results."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path


SCRIPT_ROOT = Path(__file__).resolve().parent
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

from distribution_io import atomic_write_json, create_pack_backup, file_sha256, pack_lock  # noqa: E402
from social_draft_router import (  # noqa: E402
    COMPLETE_RESULT_STATUSES, PARTIAL_RESULT_STATUSES, UNVERIFIED_RESULT_STATUSES,
    EVIDENCE_KINDS, result_identity, remote_outcome_possible, resume_context,
)


def observation_time(value: str) -> str:
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("EVIDENCE_TIME_INVALID: supply an ISO8601 observation time") from exc
    if timestamp.utcoffset() is None:
        raise ValueError("EVIDENCE_TIME_INVALID: include a timezone")
    if timestamp - datetime.now().astimezone() > timedelta(minutes=5):
        raise ValueError("EVIDENCE_TIME_INVALID: observation is in the future")
    return timestamp.isoformat(timespec="seconds")


def validate_transition(previous: dict, status: str, dispatch_id: str) -> None:
    if dispatch_id and dispatch_id != previous.get("dispatch_id"):
        raise ValueError("RESULT_DISPATCH_MISMATCH: observe the current attempt before recording")
    if previous.get("dispatch_mode") == "browser" and not dispatch_id:
        raise ValueError("RESULT_DISPATCH_ID_REQUIRED: pass the browser-begin dispatch_id")
    if remote_outcome_possible(previous) and status not in (
        COMPLETE_RESULT_STATUSES | PARTIAL_RESULT_STATUSES | UNVERIFIED_RESULT_STATUSES
    ):
        raise ValueError("RESULT_REMOTE_OUTCOME_UNRESOLVED: record partial/unverified; do not reset remote intent")


def read_json(path: Path, default: object) -> object:
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"RESULT_FILE_INVALID: repair {path.name} before writing") from exc


def parse_bool(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes"}:
        return True
    if normalized in {"0", "false", "no"}:
        return False
    raise argparse.ArgumentTypeError("expected true or false")


def bootstrap_results_from_dry_run(pack: Path, current: dict) -> dict:
    """Recover a full formal-result shell after a subset push changed provenance."""
    current_plan_id = current.get("inputs", {}).get("plan_id")
    plan = read_json(pack / "distribution-plan.json", {})
    dry = read_json(pack / "distribution-dry-run-results.json", {})
    plan_id = plan.get("plan_id")
    if current.get("schema_version") == 2 and not plan_id:
        return current
    if not plan_id or dry.get("inputs", {}).get("plan_id") != plan_id:
        raise SystemExit("Formal results do not match the active plan and dry-run cannot repair them")

    if current.get("schema_version") == 2 and current_plan_id == plan_id:
        return current

    if any(remote_outcome_possible(row) or row.get("status") not in {"not_attempted", "validated", "not_executed"}
           for row in current.get("items", [])):
        raise ValueError("RESULT_PLAN_CHANGED: reconcile existing draft evidence; do not reset results")

    generated_at = datetime.now().astimezone().isoformat(timespec="seconds")
    items = []
    for item in dry.get("items", []):
        items.append({
            **item,
            "status": "not_attempted",
            "image_verified": False,
            "video_verified": False,
            "audio_verified": False,
            "cover_verified": False,
            "draft_verified": False,
            "reason": "Formal draft result has not been recorded for this plan",
        })
    repaired = {
        "schema_version": 2,
        "generated_at": generated_at,
        "pack": str(pack),
        "title": plan.get("title", dry.get("title", pack.name)),
        "dry_run": False,
        "inputs": dry.get("inputs", {}),
        "items": items,
        "recovered_from_dry_run": True,
    }
    repaired["result_id"] = result_identity(repaired)
    return repaired


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pack", required=True)
    parser.add_argument("--platform", required=True)
    parser.add_argument("--status", required=True)
    parser.add_argument("--image-verified", type=parse_bool, default=False)
    parser.add_argument("--video-verified", type=parse_bool, default=False)
    parser.add_argument("--audio-verified", type=parse_bool, default=False)
    parser.add_argument("--cover-verified", type=parse_bool, required=True)
    parser.add_argument("--draft-verified", type=parse_bool, required=True)
    parser.add_argument("--image-count", type=int, default=0)
    parser.add_argument("--video-count", type=int, default=0)
    parser.add_argument("--evidence-kind", choices=sorted(EVIDENCE_KINDS), required=True)
    parser.add_argument("--evidence-summary", required=True)
    parser.add_argument("--evidence-reference", required=True)
    parser.add_argument("--draft-url")
    parser.add_argument("--media-id")
    parser.add_argument("--dispatch-id", default="", help="Attempt ID returned by browser-begin")
    parser.add_argument("--verified-at", default="", help="Actual observation time including timezone; does not refresh old evidence")
    parser.add_argument("--delivery-used", default="")
    parser.add_argument("--proof")
    parser.add_argument("--reason", default="")
    args = parser.parse_args()

    pack = Path(args.pack).resolve()
    results_path = pack / "distribution-results.json"
    with pack_lock(pack, "record-result"):
        results = read_json(results_path, {})
        if results.get("result_id") and results["result_id"] != result_identity(results):
            raise ValueError("RESULT_ID_INVALID: inspect existing results before recording")
        results = bootstrap_results_from_dry_run(pack, results)
        if results.get("schema_version") != 2:
            raise SystemExit("distribution-results.json must use schema_version=2")
        items = results.get("items", [])
        if len({item.get("platform_id") for item in items}) != len(items):
            raise ValueError("RESULT_DUPLICATE_PLATFORM: reconcile duplicates before recording")
        index = next((i for i, item in enumerate(items) if item.get("platform_id") == args.platform), None)
        if index is None:
            raise SystemExit(f"platform not found in results: {args.platform}")

        previous = items[index]
        validate_transition(previous, args.status, args.dispatch_id)
        if previous.get("dispatch_mode") == "browser":
            if not args.verified_at:
                raise ValueError("RESULT_OBSERVATION_TIME_REQUIRED: supply the actual browser observation time")
            # Bind observations to the same full preflight, not just an old plan label.
            resume_context(pack)
        for key, value in (("media_id", args.media_id), ("draft_url", args.draft_url)):
            if previous.get(key) and value is not None and value != previous[key]:
                raise ValueError("RESULT_REMOTE_ID_CHANGED: preserve the existing draft identity for reconciliation")

        reference = args.evidence_reference.replace("\\", "/")
        local_reference = not reference.startswith(("https://", "http://")) and Path(reference).suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".json", ".md"}
        evidence_hash = ""
        if local_reference:
            artifact = (pack / reference).resolve()
            artifact.relative_to(pack)
            if not artifact.is_file():
                raise SystemExit(f"evidence file does not exist: {reference}")
            evidence_hash = file_sha256(artifact)

        now = datetime.now().astimezone().isoformat(timespec="seconds")
        verified_at = observation_time(args.verified_at or now)
        if args.status in COMPLETE_RESULT_STATUSES and datetime.now().astimezone() - datetime.fromisoformat(verified_at) > timedelta(hours=24):
            raise ValueError("EVIDENCE_STALE: reopen the draft or record an unverified observation")
        updated = {
            **previous,
            "status": args.status,
            "finished_at": now,
            "image_count": args.image_count,
            "image_verified": args.image_verified,
            "video_count": args.video_count,
            "video_verified": args.video_verified,
            "audio_verified": args.audio_verified,
            "cover_verified": args.cover_verified,
            "draft_verified": args.draft_verified,
            "draft_url": args.draft_url if args.draft_url is not None else previous.get("draft_url", ""),
            "media_id": args.media_id if args.media_id is not None else previous.get("media_id", ""),
            "reason": args.reason,
            "proof": args.proof if args.proof is not None else previous.get("proof", ""),
            "evidence": {
                "kind": args.evidence_kind,
                "verified_at": verified_at,
                "summary": args.evidence_summary,
                "reference": reference,
                "sha256": evidence_hash,
            },
        }
        if args.delivery_used:
            updated["delivery_used"] = args.delivery_used
        if args.proof:
            updated["proof"] = args.proof
        updated["dispatch_state"] = "recorded"
        if args.status in COMPLETE_RESULT_STATUSES:
            for key in ("missing_credentials", "issue_code", "problems"):
                updated.pop(key, None)
        items[index] = updated
        results["items"] = items
        results["dry_run"] = False
        results["updated_at"] = now
        results["result_id"] = result_identity(results)
        create_pack_backup(pack, "record-result", ["distribution-results.json"], keep=10)
        atomic_write_json(results_path, results)

    print(f"platform={args.platform}")
    print(f"status={args.status}")
    print(f"result_id={results['result_id']}")
    if local_reference:
        print(f"evidence_sha256={evidence_hash}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
