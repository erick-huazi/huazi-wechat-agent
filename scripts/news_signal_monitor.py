#!/usr/bin/env python3
"""Inspect supplied public-signal observations without fetching or scoring them."""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from math import isfinite
from pathlib import Path
from urllib.parse import urlparse


def instant(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else None
    except (ValueError, OverflowError):
        return None


def public_url(value):
    if not isinstance(value, str):
        return False
    try:
        p = urlparse(value)
        return bool(p.scheme in ("http", "https") and p.hostname and not p.username and not p.password)
    except ValueError:
        return False


def evaluate(payload, now, max_age_hours=24):
    if not isinstance(payload, dict) or not isinstance(payload.get("event_id"), str) or not payload["event_id"].strip():
        raise ValueError("event_id required")
    records = payload.get("observations")
    if not isinstance(records, list) or any(not isinstance(r, dict) for r in records):
        raise ValueError("observations must be a list of objects")
    if (now.tzinfo is None or isinstance(max_age_hours, bool)
            or not isinstance(max_age_hours, (int, float))
            or not isfinite(max_age_hours) or max_age_hours <= 0):
        raise ValueError("timezone-aware now and positive freshness required")
    if not isinstance(payload.get("comparisons", []), list):
        raise ValueError("comparisons must be a list")
    now = now.astimezone(timezone.utc)
    results = []
    ids = set()
    for r in records:
        issues = []
        rid = r.get("id")
        if not isinstance(rid, str) or not rid.strip() or rid in ids:
            issues.append("OBSERVATION_ID_INVALID")
        else:
            ids.add(rid)
        status = r.get("status")
        if status not in {"observed", "not_observed", "unavailable", "error"}:
            issues.append("STATUS_INVALID")
        when = instant(r.get("observed_at"))
        if when is None:
            issues.append("TIME_MISSING_OR_NAIVE")
        elif when > now + timedelta(minutes=5):
            issues.append("TIME_IN_FUTURE")
        elif now - when > timedelta(hours=max_age_hours):
            issues.append("OBSERVATION_STALE")
        if not public_url(r.get("source_url")):
            issues.append("SOURCE_URL_INVALID")
        for key in ("provider", "region", "query", "window", "signal_type"):
            if not isinstance(r.get(key), str) or not r[key].strip():
                issues.append("MISSING_" + key.upper())
        if r.get("event_id") != payload["event_id"]:
            issues.append("EVENT_MISMATCH")
        if status in {"observed", "not_observed"} and not str(r.get("evidence_note") or "").strip():
            issues.append("EVIDENCE_NOTE_MISSING")
        results.append({"id": rid, "status": status, "issues": issues,
                        "usable_for_manual_review": not issues and status in {"observed", "not_observed"}})
    comparisons = []
    by_id = {r.get("id"): r for r in records if isinstance(r.get("id"), str)}
    by_result = {r["id"]: r for r in results if isinstance(r["id"], str)}
    if len(by_id) != len(records) or any("OBSERVATION_ID_INVALID" in r["issues"] for r in results):
        by_id, by_result = {}, {}
    for pair in payload.get("comparisons", []):
        if not isinstance(pair, dict):
            comparisons.append({"issues": ["COMPARISON_INVALID"], "difference": None})
            continue
        before, after = by_id.get(pair.get("baseline_id")), by_id.get(pair.get("current_id"))
        issues = []
        if not before or not after:
            issues.append("COMPARISON_OBSERVATION_MISSING")
        else:
            if not by_result[before["id"]]["usable_for_manual_review"] or not by_result[after["id"]]["usable_for_manual_review"]:
                issues.append("COMPARISON_INPUT_UNUSABLE")
            if before.get("status") != "observed" or after.get("status") != "observed":
                issues.append("COMPARISON_NOT_OBSERVED")
            for key in ("provider", "region", "query", "window", "signal_type", "metric", "unit", "scale"):
                if not isinstance(before.get(key), str) or not before[key].strip() or before.get(key) != after.get(key):
                    issues.append("COMPARISON_SCOPE_MISMATCH")
                    break
            bt, at = instant(before.get("observed_at")), instant(after.get("observed_at"))
            if bt is None or at is None or bt >= at:
                issues.append("COMPARISON_ORDER_INVALID")
            for item in (before, after):
                value = item.get("value")
                try:
                    valid_value = not isinstance(value, bool) and isinstance(value, (int, float)) and isfinite(value)
                except OverflowError:
                    valid_value = False
                if not valid_value:
                    issues.append("COMPARISON_VALUE_INVALID")
                    break
        difference = None if issues else after["value"] - before["value"]
        if difference is not None and not isfinite(difference):
            issues.append("COMPARISON_VALUE_INVALID")
            difference = None
        comparisons.append({"baseline_id": pair.get("baseline_id"), "current_id": pair.get("current_id"),
                            "issues": issues, "difference": difference})
    return {"schema_version": 1, "event_id": payload["event_id"], "checked_at": now.isoformat(),
            "observations": results, "comparisons": comparisons,
            "limitations": ["Inputs are unverified declarations; check original sources and source independence.",
                            "Unavailable or stale does not mean zero demand; not_observed only describes that query and window.",
                            "Numeric differences are descriptive, not growth rates, causal estimates, news scores or predictions.",
                            "No network requests, public-data collection, publishing or changes to the original ranking."]}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input", required=True)
    p.add_argument("--max-age-hours", type=float, default=24)
    args = p.parse_args()
    try:
        result = evaluate(json.loads(Path(args.input).read_text(encoding="utf-8")),
                          datetime.now(timezone.utc), args.max_age_hours)
    except (OSError, ValueError, TypeError, OverflowError) as exc:
        raise SystemExit(f"Invalid input: {type(exc).__name__}") from None
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
