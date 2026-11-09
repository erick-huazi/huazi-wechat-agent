#!/usr/bin/env python3
"""Atomically copy one dated video and cover into a locked publish pack."""

from __future__ import annotations

import argparse
import os
import shutil
import tempfile
from pathlib import Path

from distribution_io import create_pack_backup, pack_lock


def atomic_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    os.close(descriptor)
    temporary_path = Path(temporary)
    try:
        shutil.copyfile(source, temporary_path)
        os.replace(temporary_path, destination)
    finally:
        temporary_path.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Write a staged Huazi video and cover into a publish pack"
    )
    parser.add_argument("--pack", required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("--action", default="video-write")
    args = parser.parse_args()

    pack = Path(args.pack).resolve()
    source = Path(args.source).resolve()
    if not pack.is_dir() or not source.is_dir():
        raise SystemExit("Pack or staged video directory is missing")

    prefix = pack.name[:10]
    source_video = source / f"{prefix}-short-video.mp4"
    source_cover = source / f"{prefix}-video-cover.png"
    if not source_video.is_file() or not source_cover.is_file():
        raise SystemExit(
            f"Expected {source_video.name} and {source_cover.name} in {source}"
        )

    target_dir = (pack / "video").resolve()
    if pack not in target_dir.parents:
        raise SystemExit("Target directory escapes pack")
    destinations = [target_dir / source_video.name, target_dir / source_cover.name]
    sources = [source_video, source_cover]
    existing = [
        str(path.relative_to(pack)) for path in destinations if path.is_file()
    ]

    with pack_lock(pack, args.action):
        backup = create_pack_backup(pack, args.action, existing, keep=10)
        for source_path, destination in zip(sources, destinations):
            atomic_copy(source_path, destination)

    print(f"Wrote video: {destinations[0].name}")
    print(f"Wrote cover: {destinations[1].name}")
    print(f"Backup: {backup}")


if __name__ == "__main__":
    main()
