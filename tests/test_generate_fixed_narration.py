from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "generate_fixed_narration", ROOT / "scripts" / "generate_fixed_narration.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class GenerateFixedNarrationTests(unittest.TestCase):
    def test_command_uses_every_pinned_audio_parameter(self) -> None:
        profile = {
            "voice_id": "zh-CN-YunyangNeural",
            "rate": "+5%",
            "pitch": "+0Hz",
            "volume": "+0%",
        }
        command = MODULE.edge_tts_command(
            "edge-tts",
            Path("narration.txt"),
            Path("narration.mp3"),
            profile,
            Path("narration.vtt"),
        )
        self.assertEqual(command[command.index("--voice") + 1], "zh-CN-YunyangNeural")
        self.assertIn("--rate=+5%", command)
        self.assertIn("--pitch=+0Hz", command)
        self.assertIn("--volume=+0%", command)
        self.assertIn("--write-subtitles", command)


if __name__ == "__main__":
    unittest.main()
