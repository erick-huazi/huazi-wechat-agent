#!/usr/bin/env python3
"""Atomically copy staged binary assets into a locked publish pack."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path

from distribution_io import create_pack_backup, pack_lock
from content_revision_review import checked_path
from image_asset_review import prepare_assets, assets_are_current


def atomic_write_bytes(content: bytes, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, destination)
    finally:
        temporary_path.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Write staged image assets into a publish pack")
    parser.add_argument("--pack", required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("--subdir", default="images")
    parser.add_argument("--action", default="asset-write")
    parser.add_argument("--mapping", help="JSON mapping of five source basenames to exact images/<dated-name>.png targets")
    parser.add_argument("--review-only", action="store_true", help="Inspect mapping and pixel changes without backups or image writes")
    parser.add_argument("--expect-review-id", help="Stop if captured images, mapping or bound pack inputs changed")
    args = parser.parse_args()

    pack = Path(args.pack).absolute()
    source = Path(args.source).absolute()
    mapping = Path(args.mapping).absolute() if args.mapping else None
    checked_path(pack, ".")
    if not pack.is_dir() or not source.is_dir():
        raise SystemExit("Pack or staged PNG assets are missing")
    with pack_lock(pack, args.action):
        try:
            review, staged = prepare_assets(pack, source, args.subdir, mapping)
            if args.expect_review_id and args.expect_review_id != review["review_id"]:
                raise SystemExit("ASSET_REVIEW_ID_MISMATCH: inspect again; no images written")
            if not assets_are_current(pack, source, args.subdir, mapping, review):
                raise SystemExit("ASSET_INPUT_CHANGED: no images written")
            if args.review_only:
                print(json.dumps(review, ensure_ascii=False, indent=2))
                if review["blockers"]:
                    raise SystemExit(2)
                return
            if review["blockers"]:
                raise SystemExit("ASSET_PREFLIGHT_BLOCKED: " + json.dumps(review["blockers"], ensure_ascii=False))
            changed = {r["target"]: staged[r["target"]] for r in review["files"] if r["changed"]}
            if not changed:
                print("No image changes; no backup or write performed. This is not visual approval.")
                return
            backup = create_pack_backup(pack, args.action, list(changed), keep=10)
            if not assets_are_current(pack, source, args.subdir, mapping, review):
                raise SystemExit("ASSET_INPUT_CHANGED: recovery point retained; no images written")
            for name, raw in changed.items():
                atomic_write_bytes(raw, checked_path(pack, name))
        except (ValueError, OSError) as exc:
            raise SystemExit(str(exc)) from exc

    print(f"Wrote {len(changed)} PNGs using per-file atomic replacement (not a multi-file transaction)")
    print("Names: " + ", ".join(changed))
    print(f"Backup: {backup}")
    print(f"Input binding: {review['review_id']} (not visual approval)")


if __name__ == "__main__":
    main()
