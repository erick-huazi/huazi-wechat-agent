#!/usr/bin/env python3
"""Create a deterministic technical and visual quality report for a vertical short video."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont, ImageStat


SCRIPT_ROOT = Path(__file__).resolve().parent
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

from distribution_io import (  # noqa: E402
    atomic_write_json,
    canonical_sha256,
    file_sha256,
    image_file_probe,
    video_file_probe,
)


SAMPLE_POSITIONS = (0.05, 0.16, 0.32, 0.50, 0.68, 0.84, 0.95)


def ffmpeg_path() -> str:
    path = os.environ.get("FFMPEG_PATH") or shutil.which("ffmpeg")
    if not path:
        raise RuntimeError("ffmpeg_unavailable")
    return path


def extract_frame(video: Path, timestamp: float, output: Path) -> None:
    completed = subprocess.run(
        [
            ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-ss", f"{timestamp:.3f}",
            "-i", str(video), "-frames:v", "1", "-vf", "scale=360:-2", "-y", str(output),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=45,
        check=False,
    )
    if completed.returncode != 0 or not output.is_file():
        raise RuntimeError(completed.stderr.strip() or "frame_extract_failed")


def frame_metrics(path: Path) -> dict[str, Any]:
    with Image.open(path) as image:
        rgb = image.convert("RGB")
        gray = rgb.convert("L")
        stat = ImageStat.Stat(gray)
        mean = float(stat.mean[0])
        variance = float(stat.var[0])
        extrema = gray.getextrema()
        fingerprint = hashlib.sha256(gray.resize((32, 32)).tobytes()).hexdigest()[:16]
    blank = mean <= 4.0 or mean >= 251.0 or variance < 2.0 or (extrema[1] - extrema[0]) < 5
    return {
        "mean_luma": round(mean, 2),
        "variance": round(variance, 2),
        "luma_range": int(extrema[1] - extrema[0]),
        "visual_fingerprint": fingerprint,
        "blank_or_static": blank,
    }


def audio_metrics(video: Path) -> dict[str, Any]:
    completed = subprocess.run(
        [
            ffmpeg_path(), "-hide_banner", "-nostats", "-i", str(video),
            "-af", "volumedetect", "-f", "null", "-",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=False,
    )
    text = completed.stderr
    mean_match = re.search(r"mean_volume:\s*(-?\d+(?:\.\d+)?)\s*dB", text)
    max_match = re.search(r"max_volume:\s*(-?\d+(?:\.\d+)?)\s*dB", text)
    mean_db = float(mean_match.group(1)) if mean_match else None
    max_db = float(max_match.group(1)) if max_match else None
    return {
        "analyzed": completed.returncode == 0 and max_db is not None,
        "mean_volume_db": mean_db,
        "max_volume_db": max_db,
        "audible": max_db is not None and max_db > -40.0,
    }


def contact_sheet(frames: list[dict[str, Any]], output: Path) -> None:
    tile_width, tile_height = 360, 640
    label_height = 42
    columns = 4
    rows = math.ceil(len(frames) / columns)
    canvas = Image.new("RGB", (columns * tile_width, rows * (tile_height + label_height)), "white")
    draw = ImageDraw.Draw(canvas)
    font = ImageFont.load_default()
    for index, frame in enumerate(frames):
        with Image.open(frame["path"]) as image:
            tile = image.convert("RGB").resize((tile_width, tile_height))
        x = (index % columns) * tile_width
        y = (index // columns) * (tile_height + label_height)
        canvas.paste(tile, (x, y))
        label = f"{frame['timestamp_seconds']:.1f}s  var={frame['variance']:.1f}"
        draw.text((x + 8, y + tile_height + 12), label, fill="black", font=font)
    output.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(output, format="JPEG", quality=90, optimize=True)


def build_report(video: Path, cover: Path, output: Path) -> dict[str, Any]:
    probe = video_file_probe(video)
    cover_probe = image_file_probe(cover)
    frames: list[dict[str, Any]] = []
    errors: list[str] = []
    duration = float(probe.get("duration") or 0)
    evidence_dir = output.parent / "evidence"
    contact_path = evidence_dir / f"{video.stem}-qc-contact-sheet.jpg"

    if probe.get("decoded") and duration > 0:
        with tempfile.TemporaryDirectory(prefix="huazi-video-qc-") as temp:
            temp_root = Path(temp)
            for index, position in enumerate(SAMPLE_POSITIONS, start=1):
                frame_path = temp_root / f"frame-{index:02d}.jpg"
                timestamp = min(max(duration * position, 0), max(duration - 0.05, 0))
                try:
                    extract_frame(video, timestamp, frame_path)
                    metrics = frame_metrics(frame_path)
                    frames.append({
                        "index": index,
                        "position": position,
                        "timestamp_seconds": round(timestamp, 3),
                        "path": str(frame_path),
                        **metrics,
                    })
                except RuntimeError as exc:
                    errors.append(f"frame_{index}_failed:{exc}")
            if frames:
                contact_sheet(frames, contact_path)
    else:
        errors.append(f"video_not_decodable:{probe.get('error') or 'unknown'}")

    for frame in frames:
        frame.pop("path", None)
    audio = audio_metrics(video) if probe.get("has_audio") else {
        "analyzed": False,
        "mean_volume_db": None,
        "max_volume_db": None,
        "audible": False,
    }
    blank_count = sum(1 for frame in frames if frame["blank_or_static"])
    unique_frame_count = len({frame["visual_fingerprint"] for frame in frames})
    checks = {
        "video_decoded": probe.get("decoded") is True,
        "vertical_1080x1920": (probe.get("width"), probe.get("height")) == (1080, 1920),
        "h264_video": str(probe.get("video_codec", "")).lower() in {"h264", "avc1"},
        "duration_15_to_180_seconds": 15 <= duration <= 180,
        "audio_track_present": probe.get("has_audio") is True,
        "audio_is_audible": audio.get("audible") is True,
        "seven_frames_extracted": len(frames) == len(SAMPLE_POSITIONS),
        "no_blank_sample_frames": blank_count == 0,
        "sample_frames_show_progression": unique_frame_count >= min(4, len(frames)),
        "cover_is_1080x1440_png": cover_probe.get("decoded") is True
        and cover_probe.get("format") == "PNG"
        and (cover_probe.get("width"), cover_probe.get("height")) == (1080, 1440),
    }
    passed = all(checks.values()) and not errors
    generated_at = datetime.now().astimezone().isoformat(timespec="seconds")
    report = {
        "schema_version": 1,
        "generated_at": generated_at,
        "video": str(video.resolve()),
        "video_sha256": file_sha256(video) if video.is_file() else "",
        "cover": str(cover.resolve()),
        "cover_sha256": file_sha256(cover) if cover.is_file() else "",
        "technical_probe": probe,
        "audio": audio,
        "sample_frames": frames,
        "blank_sample_count": blank_count,
        "unique_sample_frame_count": unique_frame_count,
        "contact_sheet": str(contact_path.resolve()) if contact_path.is_file() else "",
        "contact_sheet_sha256": file_sha256(contact_path) if contact_path.is_file() else "",
        "checks": checks,
        "errors": errors,
        "passed": passed,
    }
    report["report_id"] = canonical_sha256({
        "video_sha256": report["video_sha256"],
        "cover_sha256": report["cover_sha256"],
        "technical_probe": report["technical_probe"],
        "audio": report["audio"],
        "sample_frames": report["sample_frames"],
        "checks": report["checks"],
        "errors": report["errors"],
        "passed": report["passed"],
    })
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Huazi vertical video quality gate")
    parser.add_argument("--video", required=True)
    parser.add_argument("--cover", required=True)
    parser.add_argument("--output", required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    video = Path(args.video).resolve()
    cover = Path(args.cover).resolve()
    output = Path(args.output).resolve()
    report = build_report(video, cover, output)
    atomic_write_json(output, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"Wrote video QC: {output}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
