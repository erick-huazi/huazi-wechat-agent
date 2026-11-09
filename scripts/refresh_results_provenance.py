#!/usr/bin/env python3
"""Refresh result provenance when a stable plan is revalidated without re-pushing."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from distribution_io import atomic_write_json, create_pack_backup, file_sha256, pack_lock
from social_draft_router import result_identity


def main() -> None:
    parser = argparse.ArgumentParser(description="Refresh distribution result provenance")
    parser.add_argument("--pack", required=True)
    args = parser.parse_args()

    pack = Path(args.pack).resolve()
    plan_path = pack / "distribution-plan.json"
    dry_path = pack / "distribution-dry-run-results.json"
    results_path = pack / "distribution-results.json"
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    dry = json.loads(dry_path.read_text(encoding="utf-8"))
    results = json.loads(results_path.read_text(encoding="utf-8"))

    plan_id = str(plan.get("plan_id", ""))
    if not plan_id or dry.get("inputs", {}).get("plan_id") != plan_id or results.get("inputs", {}).get("plan_id") != plan_id:
        raise SystemExit("Plan IDs do not match; rerun the full workflow")
    for bundle_name in ("content_bundle", "image_bundle"):
        dry_fingerprint = dry.get("inputs", {}).get(bundle_name, {}).get("fingerprint")
        result_fingerprint = results.get("inputs", {}).get(bundle_name, {}).get("fingerprint")
        if not dry_fingerprint or dry_fingerprint != result_fingerprint:
            raise SystemExit(f"{bundle_name} changed; formal push provenance cannot be refreshed")

    with pack_lock(pack, "refresh-results-provenance"):
        create_pack_backup(pack, "refresh-results-provenance", [results_path.name], keep=10)
        results["inputs"]["plan_file_sha256"] = file_sha256(plan_path)
        results["result_id"] = result_identity(results)
        atomic_write_json(results_path, results)

    print(json.dumps({
        "plan_id": plan_id,
        "plan_file_sha256": results["inputs"]["plan_file_sha256"],
        "result_id": results["result_id"],
        "content_fingerprint": results["inputs"]["content_bundle"]["fingerprint"],
        "image_fingerprint": results["inputs"]["image_bundle"]["fingerprint"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
