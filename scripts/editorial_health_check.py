#!/usr/bin/env python3
"""Read-only cross-stage diagnosis. Never authorizes or performs a platform action."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from content_workflow_gate import evaluate_pack
from distribution_io import (PackLockError, compare_pack_backup, inspect_chained_jsonl,
                             list_pack_backups, pack_lock, verify_pack_backup)
from social_draft_router import resume_context, resume_report


def delivery_state(workflow: dict, resume: dict) -> dict:
    """Separate preparation from delivery; never infer a remote save from readiness."""
    if workflow.get("status") != "evaluated" or resume.get("status") != "evaluated":
        return {"status": "unknown", "issues": ["DELIVERY_INPUT_UNKNOWN"]}
    rows = resume.get("items")
    if not isinstance(rows, list) or not rows or any(not isinstance(r, dict) for r in rows):
        return {"status": "unknown", "issues": ["DELIVERY_ROWS_INVALID"]}
    ids = [r.get("platform_id") for r in rows]
    if any(not isinstance(i, str) or not i for i in ids) or len(set(ids)) != len(ids):
        return {"status": "unknown", "issues": ["DELIVERY_PLATFORM_INVALID"]}
    actions = [r.get("action") for r in rows]
    if any(a not in {"dispatch", "skip_verified", "verify_existing", "wait_for_input"} for a in actions):
        return {"status": "unknown", "issues": ["DELIVERY_ACTION_INVALID"]}
    checks = workflow.get("stage_checks")
    if not isinstance(checks, dict) or type(checks.get("draft_verified")) is not bool:
        return {"status": "unknown", "issues": ["DELIVERY_AUDIT_UNKNOWN"]}
    verified = checks["draft_verified"]
    all_verified = all(a == "skip_verified" for a in actions)
    if verified != all_verified:
        if verified or all_verified:
            return {"status": "inconsistent", "issues": ["DELIVERY_AUDIT_RESUME_MISMATCH"]}
    if all_verified:
        return {"status": "verified", "issues": []}
    if "verify_existing" in actions or "wait_for_input" in actions:
        return {"status": "needs_review", "issues": ["DELIVERY_REMOTE_OR_BLOCKER_REVIEW"]}
    return {"status": "pending", "issues": ["DELIVERY_NOT_YET_VERIFIED"]}


def inspect_pack(pack: Path) -> dict:
    pack = pack.resolve()
    if not pack.is_dir():
        raise ValueError("pack directory does not exist")
    result = {
        "schema_version": 1, "pack": str(pack.resolve()), "authority": "read_only_diagnostic",
        "workflow": {"status": "unknown"}, "resume": {"status": "unknown"},
        "chains": {}, "latest_backup": {"status": "unknown"},
        "limitations": [
            "A diagnostic is not approval, dispatch authorization or evidence of public publication.",
            "Prior platform status may be stale; an uncertain remote write must be verified, never recreated.",
            "Source integrity and declared visual checks do not prove factual truth or aesthetic quality.",
            "Only cooperative pack writers are serialized; do not edit this pack concurrently outside its lock.",
        ],
    }
    with pack_lock(pack, "editorial-health-check"):
        for name in ("distribution-evidence", "distribution-dashboard-actions"):
            info = inspect_chained_jsonl(pack / (name + ".jsonl"), pack / (name + "-chain.json"))
            result["chains"][name] = {"valid": info.get("valid") is True, "issue_code": info.get("issue_code")}
        backups = list_pack_backups(pack, limit=1)
        if backups:
            path = Path(backups[0]["path"])
            verified = verify_pack_backup(path)
            result["latest_backup"] = {"status": "valid" if verified.get("valid") is True else "invalid",
                                       "name": path.name}
            if verified.get("valid") is True:
                comparison = compare_pack_backup(pack, path)
                result["latest_backup"]["comparison_counts"] = {
                    k: len(comparison[k]) if isinstance(comparison.get(k), list) else comparison.get(k)
                    for k in ("unchanged", "changed", "missing", "added")
                }
        else:
            result["latest_backup"] = {"status": "missing"}
        try:
            workflow = evaluate_pack(pack)
            result["workflow"] = {"status": "evaluated", "ready_for_draft_actions": workflow.get("ready_for_draft_actions") is True,
                                  "highest_contiguous_stage": workflow.get("highest_contiguous_stage"),
                                  "stage_checks": workflow.get("stage_checks"), "issues": workflow.get("issues"),
                                  "workflow_id": workflow.get("workflow_id")}
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            result["workflow"] = {"status": "invalid_or_unreadable"}
        try:
            plan, _, records = resume_context(pack)
            resume = resume_report(pack, plan, records)
            result["resume"] = {"status": "evaluated", "plan_id": resume.get("plan_id"),
                                "items": resume.get("items", [])}
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            result["resume"] = {"status": "preflight_invalid_missing_or_changed",
                                "action": "inspect_existing_before_any_remote_write"}
    result["delivery"] = delivery_state(result["workflow"], result["resume"])
    result["requires_attention"] = (
        result["workflow"].get("ready_for_draft_actions") is not True
        or result["resume"].get("status") != "evaluated"
        or any(c["valid"] is not True for c in result["chains"].values())
        or result["latest_backup"].get("status") != "valid"
        or result["delivery"]["status"] in {"unknown", "inconsistent", "needs_review"}
        or any(i.get("action") in {"verify_existing", "wait_for_input"} for i in result["resume"].get("items", []))
    )
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pack", required=True)
    args = parser.parse_args()
    try:
        report = inspect_pack(Path(args.pack))
    except (OSError, ValueError, PackLockError):
        raise SystemExit("PACK_UNAVAILABLE_OR_BUSY: inspect the pack; no action was attempted") from None
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
