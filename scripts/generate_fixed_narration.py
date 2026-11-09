#!/usr/bin/env python3
"""Generate narration with the voice profile pinned to a Huazi publish pack."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any


SCRIPT_ROOT = Path(__file__).resolve().parent
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

from distribution_io import atomic_write_json, file_sha256, pack_lock  # noqa: E402
from video_voice_profile import load_voice_policy, validate_voice_profile  # noqa: E402


def read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid voice profile: {path}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"Invalid voice profile: {path}")
    return payload


def edge_tts_command(
    executable: str,
    text_path: Path,
    media_path: Path,
    profile: dict[str, Any],
    subtitle_path: Path | None = None,
) -> list[str]:
    command = [
        executable,
        "--file", str(text_path),
        "--voice", str(profile["voice_id"]),
        f"--rate={profile['rate']}",
        f"--pitch={profile['pitch']}",
        f"--volume={profile['volume']}",
        "--write-media", str(media_path),
    ]
    if subtitle_path is not None:
        command.extend(["--write-subtitles", str(subtitle_path)])
    return command


def ensure_inside(pack: Path, path: Path, label: str) -> None:
    try:
        path.relative_to(pack)
    except ValueError as exc:
        raise ValueError(f"{label} must stay inside the publish pack: {path}") from exc


def generate_narration(
    pack: Path,
    text_path: Path,
    output_path: Path,
    subtitle_path: Path | None = None,
) -> dict[str, Any]:
    pack = pack.resolve()
    text_path = text_path.resolve()
    output_path = output_path.resolve()
    subtitle_path = subtitle_path.resolve() if subtitle_path else None
    if not pack.is_dir():
        raise ValueError(f"Publish pack not found: {pack}")
    if not text_path.is_file() or not text_path.read_text(encoding="utf-8").strip():
        raise ValueError(f"Narration text is missing or empty: {text_path}")
    ensure_inside(pack, text_path, "Narration text")
    ensure_inside(pack, output_path, "Narration output")
    if subtitle_path:
        ensure_inside(pack, subtitle_path, "Subtitle output")

    profile_path = pack / "video-audio-profile.json"
    profile = read_json(profile_path)
    policy = load_voice_policy()
    issues = validate_voice_profile(profile, policy)
    if issues:
        raise ValueError("Voice profile failed validation: " + ", ".join(issues))

    executable = shutil.which("edge-tts")
    if not executable:
        raise ValueError("edge-tts executable is unavailable")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if subtitle_path:
        subtitle_path.parent.mkdir(parents=True, exist_ok=True)

    with pack_lock(pack, "fixed-narration"):
        with tempfile.TemporaryDirectory(prefix="huazi-narration-", dir=output_path.parent) as temp:
            temp_root = Path(temp)
            temp_media = temp_root / "narration.mp3"
            temp_subtitles = temp_root / "narration.vtt" if subtitle_path else None
            completed = subprocess.run(
                edge_tts_command(executable, text_path, temp_media, profile, temp_subtitles),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=180,
                check=False,
            )
            if completed.returncode != 0 or not temp_media.is_file() or temp_media.stat().st_size == 0:
                detail = completed.stderr.strip() or completed.stdout.strip() or "edge_tts_failed"
                raise RuntimeError(detail)
            os.replace(temp_media, output_path)
            if subtitle_path and temp_subtitles and temp_subtitles.is_file():
                os.replace(temp_subtitles, subtitle_path)

        result = {
            "schema_version": 1,
            "status": "success",
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "profile": str(profile_path),
            "profile_sha256": file_sha256(profile_path),
            "preset_id": profile["preset_id"],
            "provider": profile["provider"],
            "voice_id": profile["voice_id"],
            "rate": profile["rate"],
            "pitch": profile["pitch"],
            "volume": profile["volume"],
            "text": str(text_path),
            "text_sha256": file_sha256(text_path),
            "audio": str(output_path),
            "audio_sha256": file_sha256(output_path),
            "subtitles": str(subtitle_path) if subtitle_path and subtitle_path.is_file() else "",
            "subtitles_sha256": file_sha256(subtitle_path) if subtitle_path and subtitle_path.is_file() else "",
        }
        atomic_write_json(pack / "video-narration-result.json", result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate Huazi narration from a fixed voice profile")
    parser.add_argument("--pack", required=True)
    parser.add_argument("--text", required=True)
    parser.add_argument("--output")
    parser.add_argument("--subtitles")
    args = parser.parse_args()

    pack = Path(args.pack).resolve()
    output = Path(args.output).resolve() if args.output else pack / "video" / "narration.mp3"
    subtitles = Path(args.subtitles).resolve() if args.subtitles else pack / "video" / "narration.vtt"
    try:
        result = generate_narration(pack, Path(args.text), output, subtitles)
    except (ValueError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(
        f"Generated narration: {result['voice_id']} at {result['rate']} "
        f"({result['audio']})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
