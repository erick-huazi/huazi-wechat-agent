#!/usr/bin/env python3
"""Atomically copy prepared content files into a locked Huazi publish pack."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from distribution_io import atomic_write_text, create_pack_backup, pack_lock, validate_image_count
from content_revision_review import SUFFIXES as ALLOWED_SUFFIXES, list_staged_files, prepare_revision, revision_is_current, strict_json


def source_files(source: Path) -> list[Path]:
    return list_staged_files(source)


def main() -> None:
    parser = argparse.ArgumentParser(description="Write staged content into a publish pack")
    parser.add_argument("--pack", required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("--action", default="content-write")
    parser.add_argument("--review-only", action="store_true", help="Print changes without backups or content writes")
    parser.add_argument("--expect-review-id", help="Stop if staged files or bound pack inputs changed since review")
    args = parser.parse_args()

    pack = Path(args.pack).resolve()
    source = Path(args.source).resolve()
    if not pack.is_dir():
        raise SystemExit(f"Pack does not exist: {pack}")
    if not source.is_dir():
        raise SystemExit(f"Staging directory does not exist: {source}")

    with pack_lock(pack, args.action):
        review, staged = prepare_revision(pack, source)
        if "image-count.json" in staged:
            try:
                validate_image_count(pack, strict_json(staged["image-count.json"]))
            except ValueError as exc:
                raise SystemExit(f"IMAGE_COUNT_INVALID: {exc}") from exc
            current = pack / "image-count.json"
            changed = not current.is_file() or current.read_text(encoding="utf-8") != staged["image-count.json"]
            if changed and (pack / "distribution-results.json").exists():
                raise SystemExit("IMAGE_COUNT_RESULTS_EXIST: preserve the original plan and verify existing drafts")
        if args.expect_review_id and args.expect_review_id != review["review_id"]:
            raise SystemExit("REV_REVIEW_ID_MISMATCH: inspect changes again; no content written")
        if not revision_is_current(pack, source, review):
            raise SystemExit("REV_INPUT_CHANGED: no content written")
        if args.review_only:
            print(json.dumps(review, ensure_ascii=False, indent=2))
            return
        relative_names = list(staged)
        backup = create_pack_backup(pack, args.action, relative_names, keep=10)
        if not revision_is_current(pack, source, review):
            raise SystemExit("REV_INPUT_CHANGED: recovery point retained; no content written")
        for relative_name, content in staged.items():
            destination = (pack / relative_name).resolve()
            if pack not in destination.parents:
                raise SystemExit(f"Destination escapes pack: {relative_name}")
            atomic_write_text(destination, content)

    print(f"Wrote {len(staged)} files using per-file atomic replacement (not a multi-file transaction)")
    print(f"Backup: {backup}")
    print(f"Input binding: {review['review_id']} (not content approval)")


if __name__ == "__main__":
    main()
