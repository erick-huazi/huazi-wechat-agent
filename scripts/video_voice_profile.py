#!/usr/bin/env python3
"""Create a deterministic Huazi narration voice profile for a publish pack."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


SCRIPT_ROOT = Path(__file__).resolve().parent
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

from distribution_io import atomic_write_text, pack_lock


PROJECT_ROOT = SCRIPT_ROOT.parent
DEFAULT_POLICY_PATH = PROJECT_ROOT / "config" / "video-voice-presets.json"


def now_iso() -> str:
    return datetime.now(timezone(timedelta(hours=8))).replace(microsecond=0).isoformat()


def load_voice_policy(path: Path | None = None) -> dict[str, Any]:
    policy_path = (path or DEFAULT_POLICY_PATH).resolve()
    if not policy_path.is_file():
        raise ValueError(f"Voice policy not found: {policy_path}")
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    presets = policy.get("presets")
    default_preset = policy.get("default_preset")
    if not isinstance(presets, dict) or not presets:
        raise ValueError("Voice policy has no presets")
    if default_preset not in presets:
        raise ValueError("Voice policy default_preset is invalid")
    return policy


def build_voice_profile(
    policy: dict[str, Any],
    preset_id: str | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    selected_id = preset_id or str(policy["default_preset"])
    presets = policy["presets"]
    if selected_id not in presets:
        choices = ", ".join(sorted(presets))
        raise ValueError(f"Unknown voice preset '{selected_id}'. Expected one of: {choices}")
    preset = presets[selected_id]
    return {
        "schema_version": 1,
        "policy_id": str(policy.get("policy_id", "huazi-video-voice-v1")),
        "selected_at": now_iso(),
        "preset_id": selected_id,
        "selection_reason": reason or (
            "Project default" if selected_id == policy["default_preset"] else "Explicit content-mode override"
        ),
        "provider": str(policy.get("provider", "edge-tts")),
        "language": str(policy.get("language", "zh-CN")),
        "voice_id": str(preset["voice_id"]),
        "display_name": str(preset["display_name"]),
        "gender": str(preset["gender"]),
        "tone": str(preset["tone"]),
        "rate": str(preset["rate"]),
        "pitch": str(preset["pitch"]),
        "volume": str(preset["volume"]),
        "use_when": str(preset["use_when"]),
        "mix": policy.get("mix", {}),
    }


def validate_voice_profile(profile: dict[str, Any], policy: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    presets = policy.get("presets") if isinstance(policy.get("presets"), dict) else {}
    preset = presets.get(profile.get("preset_id"), {})
    expected = {
        "policy_id": policy.get("policy_id"),
        "provider": policy.get("provider"),
        "language": policy.get("language"),
        "voice_id": preset.get("voice_id"),
        "rate": preset.get("rate"),
        "pitch": preset.get("pitch"),
        "volume": preset.get("volume"),
    }
    if not preset:
        issues.append("voice_preset_not_allowed")
    for field, value in expected.items():
        if profile.get(field) != value:
            issues.append(f"voice_profile_mismatch:{field}")
    return sorted(set(issues))


def main() -> None:
    parser = argparse.ArgumentParser(description="Write Huazi video-audio-profile.json")
    parser.add_argument("--pack", required=True)
    parser.add_argument("--preset")
    parser.add_argument("--reason")
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY_PATH)
    args = parser.parse_args()

    pack = Path(args.pack).resolve()
    if not pack.is_dir():
        raise SystemExit(f"Publish pack not found: {pack}")
    try:
        policy = load_voice_policy(args.policy)
        profile = build_voice_profile(policy, args.preset, args.reason)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc

    output = pack / "video-audio-profile.json"
    with pack_lock(pack, "video-voice-profile"):
        atomic_write_text(output, json.dumps(profile, ensure_ascii=False, indent=2) + "\n")
    print(f"Wrote voice profile: {output}")
    print(f"Preset: {profile['preset_id']} ({profile['voice_id']}, rate {profile['rate']})")


if __name__ == "__main__":
    main()
