from __future__ import annotations

import importlib.util
import tempfile
import unittest
from functools import lru_cache
from io import BytesIO
from pathlib import Path
from unittest import mock

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "social_auto_upload_adapter", ROOT / "scripts" / "social_auto_upload_adapter.py"
)
ADAPTER = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(ADAPTER)


@lru_cache(maxsize=32)
def png_bytes(width: int, height: int, marker: int) -> bytes:
    stream = BytesIO()
    Image.new("RGB", (width, height), ((marker * 41) % 255, (marker * 73) % 255, (marker * 109) % 255)).save(stream, format="PNG")
    return stream.getvalue()


def write_images(pack: Path, dated: bool = False) -> None:
    (pack / "images").mkdir()
    names = [f"2026-07-22-{index:02d}.png" for index in range(1, 6)] if dated else ADAPTER.CANONICAL_IMAGES
    for index, name in enumerate(names, start=1):
        size = (900, 383) if index == 1 else (1080, 720)
        (pack / "images" / name).write_bytes(png_bytes(*size, index))


def write_video_assets(pack: Path, image_date: str = "2026-07-22") -> None:
    video_root = pack / "video"
    video_root.mkdir(exist_ok=True)
    (video_root / f"{image_date}-short-video.mp4").write_bytes(b"test-video")
    (video_root / f"{image_date}-video-cover.png").write_bytes(png_bytes(1080, 1440, 44))


VALID_VIDEO_PROBE = {
    "exists": True,
    "decoded": True,
    "format": "mov,mp4,m4a,3gp,3g2,mj2",
    "width": 1080,
    "height": 1920,
    "duration": 50.0,
    "fps": 30.0,
    "video_codec": "h264",
    "audio_codec": "aac",
    "has_audio": True,
    "bytes": 10,
    "sha256": "abc",
    "error": "",
}


class SocialAutoUploadAdapterTests(unittest.TestCase):
    def test_stage_builds_preview_jobs_without_enabling_execution(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            pack = Path(temp) / "2026-07-22-video-huazi"
            pack.mkdir()
            write_images(pack, dated=True)
            write_video_assets(pack)
            for filename in ["xiaohongshu.md", "douyin-video.md", "video-channel.md", "kuaishou-video.md"]:
                (pack / filename).write_text("# 平台标题\n\n正文内容 #AI工作流", encoding="utf-8")
            with mock.patch.object(ADAPTER, "video_file_probe", return_value=VALID_VIDEO_PROBE):
                payload = ADAPTER.stage(pack, ROOT / "research" / "social-auto-upload", list(ADAPTER.DEFAULT_TARGETS))
            self.assertEqual(payload["execution_policy"], "preview_only_never_publish")
            self.assertEqual(len(payload["jobs"]), 0)
            self.assertTrue(all(job["execution_enabled"] is False for job in payload["jobs"]))

    def test_retired_platform_staging_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            for platform in ("douyin-video", "video-channel", "kuaishou-video", "douyin-image", "kuaishou-image"):
                with self.subTest(platform=platform), self.assertRaisesRegex(ValueError, "PLATFORM_DISABLED_BY_WORKFLOW"):
                    ADAPTER.stage(Path(temp), Path(temp), [platform])

    def test_stage_blocks_missing_dedicated_content(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            pack = Path(temp)
            write_images(pack)
            job = ADAPTER.build_job(pack, "douyin-image", ROOT / "research" / "social-auto-upload")
            self.assertEqual(job["status"], "blocked")
            self.assertIn("missing_content:douyin.md", job["problems"])

    def test_stage_accepts_dated_five_image_names(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            pack = Path(temp) / "2026-07-22-test-huazi"
            pack.mkdir()
            write_images(pack, dated=True)
            (pack / "xiaohongshu.md").write_text("# 平台标题\n\n正文内容 #AI工作流", encoding="utf-8")
            job = ADAPTER.build_job(pack, "xiaohongshu", ROOT / "research" / "social-auto-upload")
            self.assertEqual(job["image_count"], 5)
            self.assertTrue(job["images"][0].endswith("2026-07-22-01.png"))
            self.assertEqual(job["status"], "ready_for_manual_adapter_review")


class StrictDatedImageNamingTests(unittest.TestCase):
    def test_dated_pack_rejects_legacy_image_names(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            pack = Path(temp) / "2026-07-27-test-huazi"
            pack.mkdir()
            write_images(pack)
            (pack / "xiaohongshu.md").write_text(
                "# Naming guard\n\nBody #AI",
                encoding="utf-8",
            )
            job = ADAPTER.build_job(
                pack,
                "xiaohongshu",
                ROOT / "research" / "social-auto-upload",
            )
            self.assertEqual(job["status"], "blocked")
            self.assertTrue(any(
                problem.startswith("missing_images:2026-07-27-01.png")
                for problem in job["problems"]
            ))


if __name__ == "__main__":
    unittest.main()
