from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest import mock

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "pack_asset_writer", ROOT / "scripts" / "pack_asset_writer.py"
)
WRITER = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(WRITER)


def png_bytes(marker: int) -> bytes:
    stream = BytesIO()
    Image.new("RGB", (64, 64), (marker, marker * 2, marker * 3)).save(
        stream,
        format="PNG",
    )
    return stream.getvalue()


class PackAssetWriterTests(unittest.TestCase):
    def test_dated_pack_renames_staged_images_to_date_sequence(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source"
            pack = root / "2026-07-27-writer-test-huazi"
            source.mkdir()
            (pack / "images").mkdir(parents=True)
            for index in range(1, 6):
                (source / f"arbitrary-{index}.png").write_bytes(png_bytes(index))

            argv = [
                "pack_asset_writer.py",
                "--pack",
                str(pack),
                "--source",
                str(source),
            ]
            with mock.patch.object(sys, "argv", argv), mock.patch("builtins.print"):
                WRITER.main()

            self.assertEqual(
                sorted(path.name for path in (pack / "images").iterdir()),
                [f"2026-07-27-{index:02d}.png" for index in range(1, 6)],
            )

    def test_image_write_requires_exactly_five_png_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source"
            pack = root / "2026-07-27-writer-test-huazi"
            source.mkdir()
            (pack / "images").mkdir(parents=True)
            (source / "only-one.png").write_bytes(png_bytes(1))

            argv = [
                "pack_asset_writer.py",
                "--pack",
                str(pack),
                "--source",
                str(source),
            ]
            with mock.patch.object(sys, "argv", argv), self.assertRaises(SystemExit):
                WRITER.main()


if __name__ == "__main__":
    unittest.main()
