from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("video_quality_gate", ROOT / "scripts" / "video_quality_gate.py")
VIDEO_QC = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(VIDEO_QC)


class VideoQualityGateTests(unittest.TestCase):
    def test_blank_frame_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "blank.png"
            Image.new("RGB", (360, 640), "black").save(path)
            self.assertTrue(VIDEO_QC.frame_metrics(path)["blank_or_static"])

    def test_visually_varied_frame_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "varied.png"
            image = Image.new("RGB", (360, 640), "white")
            for x in range(180):
                for y in range(640):
                    image.putpixel((x, y), (20, 80, 180))
            image.save(path)
            metrics = VIDEO_QC.frame_metrics(path)
            self.assertFalse(metrics["blank_or_static"])
            self.assertGreater(metrics["variance"], 2)


if __name__ == "__main__":
    unittest.main()
