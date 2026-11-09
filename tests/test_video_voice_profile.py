from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "video_voice_profile", ROOT / "scripts" / "video_voice_profile.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class VideoVoiceProfileTests(unittest.TestCase):
    def test_default_is_huazi_president(self) -> None:
        policy = MODULE.load_voice_policy()
        profile = MODULE.build_voice_profile(policy)
        self.assertEqual(profile["preset_id"], "huazi-president")
        self.assertEqual(profile["voice_id"], "zh-CN-YunyangNeural")
        self.assertEqual(profile["rate"], "+5%")

    def test_explicit_breakout_preset(self) -> None:
        policy = MODULE.load_voice_policy()
        profile = MODULE.build_voice_profile(policy, "huazi-breakout", "Strong point of view")
        self.assertEqual(profile["voice_id"], "zh-CN-YunjianNeural")
        self.assertEqual(profile["selection_reason"], "Strong point of view")

    def test_built_profile_validates_against_policy(self) -> None:
        policy = MODULE.load_voice_policy()
        profile = MODULE.build_voice_profile(policy)
        self.assertEqual(MODULE.validate_voice_profile(profile, policy), [])

    def test_modified_rate_is_rejected(self) -> None:
        policy = MODULE.load_voice_policy()
        profile = MODULE.build_voice_profile(policy)
        profile["rate"] = "+20%"
        self.assertIn("voice_profile_mismatch:rate", MODULE.validate_voice_profile(profile, policy))

    def test_unknown_preset_is_rejected(self) -> None:
        policy = MODULE.load_voice_policy()
        with self.assertRaises(ValueError):
            MODULE.build_voice_profile(policy, "random-voice")

    def test_policy_requires_valid_default(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "policy.json"
            path.write_text(
                json.dumps({"default_preset": "missing", "presets": {"valid": {}}}),
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                MODULE.load_voice_policy(path)


if __name__ == "__main__":
    unittest.main()
