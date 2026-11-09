from __future__ import annotations

from contextlib import redirect_stdout
from io import BytesIO, StringIO
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from PIL import Image, ImageDraw, PngImagePlugin

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import image_asset_review as review
import pack_asset_writer as writer
from distribution_io import file_sha256, five_image_names, pack_lock, PackLockError


def fixture_png(index=1, size=(1080, 720), note=None, transparent=False, flat=False):
    image = Image.new("RGBA", size, (255, 255, 255, 0 if transparent else 255))
    if not flat:
        ImageDraw.Draw(image).rectangle((min(index * 12, size[0] // 4), min(40, size[1] // 4), size[0] // 2, size[1] // 2),
                                        fill=(index * 30 % 256, 80, 170, 0 if transparent else 255))
    stream = BytesIO()
    info = PngImagePlugin.PngInfo()
    if note:
        info.add_text("note", note)
    image.save(stream, "PNG", pnginfo=info)
    return stream.getvalue()


class ImageAssetReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.pack = self.root / "2026-10-09-fixture-huazi"
        self.source = self.root / "source"
        self.pack.mkdir()
        self.source.mkdir()
        self.names = five_image_names(self.pack)
        for i, name in enumerate(self.names):
            (self.source / name).write_bytes(fixture_png(i + 1, (900, 383) if i == 0 else (1080, 720)))

    def prepare(self, **kwargs):
        return review.prepare_assets(self.pack, self.source, **kwargs)[0]

    def run_writer(self, *args):
        output = StringIO()
        with mock.patch.object(sys, "argv", ["writer", "--pack", str(self.pack), "--source", str(self.source), *args]), redirect_stdout(output):
            writer.main()
        return output.getvalue()

    def install_baseline(self):
        target = self.pack / "images"
        target.mkdir(exist_ok=True)
        for path in self.source.iterdir():
            (target / path.name).write_bytes(path.read_bytes())

    def test_review_only_maps_roles_and_does_not_write(self):
        result = json.loads(self.run_writer("--review-only"))
        self.assertTrue(result["technical_preflight_passed"])
        self.assertEqual([r["expected_role"] for r in result["files"]],
                         ["cover", "news_fact", "mechanism", "scenario", "action_boundary"])
        self.assertFalse(result["visual_review_performed"])
        self.assertFalse(result["fact_verification_performed"])
        self.assertFalse(result["draft_actions_performed"])
        self.assertEqual(list(self.pack.iterdir()), [])

    def test_explicit_mapping_does_not_depend_on_lexical_order(self):
        rows = []
        for i, name in enumerate(self.names):
            random_name = f"random-{9-i}.png"
            (self.source / name).rename(self.source / random_name)
            rows.append({"source": random_name, "target": f"images/{name}"})
        mapping = self.root / "mapping.json"
        mapping.write_text(json.dumps({"images": list(reversed(rows))}), encoding="utf-8")
        result = self.prepare(mapping=mapping)
        self.assertEqual([r["source"] for r in result["files"]], [r["source"] for r in rows])
        self.assertTrue(result["technical_preflight_passed"])
        self.run_writer("--mapping", str(mapping), "--expect-review-id", result["review_id"])
        for row in rows:
            self.assertEqual((self.pack / row["target"]).read_bytes(), (self.source / row["source"]).read_bytes())

    def test_new_pack_rejects_ambiguous_order_before_backup(self):
        (self.source / self.names[0]).rename(self.source / "cover.png")
        with self.assertRaisesRegex(SystemExit, "ASSET_ORDER_REQUIRED"):
            self.run_writer()
        self.assertFalse((self.pack / ".backups").exists())

    def test_mapping_requires_exact_bijection(self):
        mapping = self.root / "mapping.json"
        rows = [{"source": self.names[0], "target": f"images/{n}"} for n in self.names]
        mapping.write_text(json.dumps({"images": rows}), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "BIJECTIVE"):
            self.prepare(mapping=mapping)

    def test_mapping_rejects_duplicate_json_keys_and_invalid_rows(self):
        path = self.root / "mapping.json"
        for value in ('{"images":[],"images":[]}', '{"images":[null,null,null,null,null]}'):
            with self.subTest(value=value):
                path.write_text(value, encoding="utf-8")
                with self.assertRaises(ValueError):
                    self.prepare(mapping=path)

    def test_pixel_duplicate_catches_different_metadata(self):
        (self.source / self.names[1]).write_bytes(fixture_png(2, note="a"))
        (self.source / self.names[2]).write_bytes(fixture_png(2, note="b"))
        result = self.prepare()
        self.assertIn("ASSET_PIXEL_DUPLICATE", [b["code"] for b in result["blockers"]])
        self.assertNotEqual(result["files"][1]["staged_sha256"], result["files"][2]["staged_sha256"])

    def test_unchanged_inputs_have_stable_id_and_no_write(self):
        self.install_baseline()
        first = self.prepare()
        self.assertEqual(first["review_id"], self.prepare()["review_id"])
        self.assertFalse(first["requires_visual_review"])
        self.assertTrue(all(r["comparison"]["kind"] == "identical_file" for r in first["files"]))
        self.assertIn("No image changes", self.run_writer())
        self.assertFalse((self.pack / ".backups").exists())

    def test_metadata_only_is_not_new_visual_information(self):
        self.install_baseline()
        (self.source / self.names[1]).write_bytes(fixture_png(2, note="changed"))
        result = self.prepare()
        self.assertEqual(result["files"][1]["comparison"]["kind"], "encoded_bytes_only_changed")
        self.assertTrue(result["requires_visual_review"])

    def test_exact_pixel_diff_detects_alpha_and_small_changes(self):
        before = Image.new("RGBA", (8, 8), (255, 0, 0, 255))
        after = before.copy()
        after.putpixel((2, 3), (255, 0, 0, 254))
        result = review.pixel_change(before, after)
        self.assertEqual(result["different_rgba_pixels"], 1)
        self.assertEqual(result["bounding_box"], [2, 3, 3, 4])

    def test_wrong_dimensions_corruption_jpeg_flat_transparent(self):
        jpg = BytesIO()
        Image.new("RGB", (1080, 720)).save(jpg, "JPEG")
        for raw, code in [(fixture_png(1, (40, 40)), "ASSET_WRONG_DIMENSIONS"),
                          (b"broken", "ASSET_DECODE_FAILED"), (jpg.getvalue(), "ASSET_NOT_PNG"),
                          (fixture_png(1, flat=True), "ASSET_BLANK_OR_FLAT"),
                          (fixture_png(1, transparent=True), "ASSET_BLANK_OR_FLAT")]:
            with self.subTest(code=code, size=len(raw)):
                (self.source / self.names[1]).write_bytes(raw)
                self.assertIn(code, [b["code"] for b in self.prepare()["blockers"]])

    def test_apng_is_rejected_even_when_first_frame_is_valid(self):
        stream = BytesIO()
        Image.new("RGB", (1080, 720), "red").save(stream, "PNG", save_all=True,
            append_images=[Image.new("RGB", (1080, 720), "blue")], duration=100, loop=0)
        (self.source / self.names[2]).write_bytes(stream.getvalue())
        self.assertIn("ASSET_ANIMATED_PNG", [b["code"] for b in self.prepare()["blockers"]])

    def test_decode_is_full_not_just_header(self):
        (self.source / self.names[3]).write_bytes((self.source / self.names[3]).read_bytes()[:-40])
        self.assertIn("ASSET_DECODE_FAILED", [b["code"] for b in self.prepare()["blockers"]])

    def test_pixel_limit_and_byte_limit(self):
        with self.assertRaisesRegex(ValueError, "PIXEL_LIMIT"):
            review.decode_png(fixture_png(1, (2100, 2000)))
        with mock.patch.object(review, "MAX_BATCH_BYTES", 1), self.assertRaisesRegex(ValueError, "BATCH_TOO_LARGE"):
            self.prepare()
        with self.assertRaisesRegex(ValueError, "FILE_TOO_LARGE"):
            review.captured(self.source / self.names[0], 2)

    def test_all_five_preflight_before_backup_or_write(self):
        self.install_baseline()
        before = {p.name: file_sha256(p) for p in (self.pack / "images").iterdir()}
        (self.source / self.names[4]).write_bytes(b"invalid")
        with self.assertRaisesRegex(SystemExit, "PREFLIGHT_BLOCKED"):
            self.run_writer()
        self.assertEqual(before, {p.name: file_sha256(p) for p in (self.pack / "images").iterdir()})
        self.assertFalse((self.pack / ".backups").exists())

    def test_blocked_review_prints_json_and_exits_two(self):
        (self.source / self.names[4]).write_bytes(b"bad")
        output = StringIO()
        with mock.patch.object(sys, "argv", ["writer", "--pack", str(self.pack), "--source", str(self.source), "--review-only"]), redirect_stdout(output), self.assertRaises(SystemExit) as exc:
            writer.main()
        self.assertEqual(exc.exception.code, 2)
        self.assertFalse(json.loads(output.getvalue())["technical_preflight_passed"])

    def test_stale_id_blocks_changes_in_images_context_and_mapping(self):
        result = self.prepare()
        (self.pack / "wechat-article.md").write_text("changed scope", encoding="utf-8")
        with self.assertRaisesRegex(SystemExit, "REVIEW_ID_MISMATCH"):
            self.run_writer("--expect-review-id", result["review_id"])
        self.assertFalse((self.pack / ".backups").exists())
        result = self.prepare()
        (self.source / self.names[4]).write_bytes(fixture_png(8))
        self.assertFalse(review.assets_are_current(self.pack, self.source, "images", None, result))

    def test_context_plan_approval_and_policy_hashes_are_bound(self):
        original = self.prepare()["review_id"]
        for name in ("content-approval.json", "distribution-plan.json", "claim-verification.json"):
            (self.pack / name).write_text('{"status":"hold"}', encoding="utf-8")
            current = self.prepare()["review_id"]
            self.assertNotEqual(current, original)
            original = current
        self.assertFalse(self.prepare()["draft_actions_performed"])

    def test_board_intent_is_not_visual_observation(self):
        board = {"items": [{"image": f"images/{self.names[0]}", "role": "cover", "message": "Author intent", "on_image_text": "Label"}]}
        (self.pack / "image-storyboard.json").write_text(json.dumps(board), encoding="utf-8")
        row = self.prepare()["files"][0]
        self.assertEqual(row["author_intent_not_verified"]["on_image_text"], "Label")
        self.assertEqual(row["actual_visual_review"], "not_performed")

    def test_unreadable_old_image_can_be_repaired(self):
        self.install_baseline()
        (self.pack / "images" / self.names[2]).write_bytes(b"old broken")
        result = self.prepare()
        self.assertTrue(result["technical_preflight_passed"])
        self.assertEqual(result["files"][2]["comparison"]["kind"], "old_image_unreadable")

    def test_saved_bytes_and_backups(self):
        self.install_baseline()
        old = (self.pack / "images" / self.names[3]).read_bytes()
        new = fixture_png(7)
        (self.source / self.names[3]).write_bytes(new)
        result = self.prepare()
        self.run_writer("--expect-review-id", result["review_id"])
        self.assertEqual((self.pack / "images" / self.names[3]).read_bytes(), new)
        copies = list((self.pack / ".backups").rglob(self.names[3]))
        self.assertTrue(any(p.read_bytes() == old for p in copies))

    def test_source_changed_before_backup_does_not_write(self):
        with mock.patch.object(writer, "assets_are_current", return_value=False), self.assertRaisesRegex(SystemExit, "INPUT_CHANGED"):
            self.run_writer()
        self.assertFalse((self.pack / ".backups").exists())

    def test_source_changed_after_backup_does_not_write(self):
        with mock.patch.object(writer, "assets_are_current", side_effect=[True, False]), self.assertRaisesRegex(SystemExit, "INPUT_CHANGED"):
            self.run_writer()
        self.assertFalse((self.pack / "images").exists())
        self.assertTrue((self.pack / ".backups").exists())

    def test_writer_uses_captured_bytes_not_later_reads(self):
        expected = (self.source / self.names[0]).read_bytes()
        real = writer.atomic_write_bytes
        first = True
        def mutate(raw, destination):
            nonlocal first
            if first:
                first = False
                (self.source / self.names[0]).write_bytes(b"changed after last check")
            return real(raw, destination)
        with mock.patch.object(writer, "atomic_write_bytes", side_effect=mutate):
            self.run_writer()
        self.assertEqual((self.pack / "images" / self.names[0]).read_bytes(), expected)

    def test_inventory_extra_file_blocks_without_deleting_it(self):
        self.install_baseline()
        extra = self.pack / "images/old.png"
        extra.write_bytes(fixture_png(9))
        self.assertIn("ASSET_TARGET_EXTRA_FILES", [b["code"] for b in self.prepare()["blockers"]])
        self.assertTrue(extra.exists())

    def test_source_overlap_escape_and_hidden_files(self):
        with self.assertRaisesRegex(ValueError, "DISJOINT"):
            review.prepare_assets(self.pack, self.pack)
        with self.assertRaisesRegex(ValueError, "PATH_ESCAPE"):
            self.prepare(subdir="../outside")
        (self.source / ".hidden.png").write_bytes(b"x")
        with self.assertRaisesRegex(ValueError, "UNEXPECTED"):
            self.prepare()

    def test_normalized_image_subdir_does_not_bypass_strict_policy(self):
        (self.source / self.names[1]).write_bytes(fixture_png(2, (64, 64)))
        result = self.prepare(subdir="images/../images")
        self.assertTrue(result["strict_five_image_policy"])
        self.assertIn("ASSET_WRONG_DIMENSIONS", [b["code"] for b in result["blockers"]])

    def test_symlink_guard(self):
        original = Path.is_symlink
        with mock.patch.object(Path, "is_symlink", lambda p: p == self.source / self.names[2] or original(p)), self.assertRaisesRegex(ValueError, "LINK_NOT_ALLOWED"):
            self.prepare()

    def test_nonimage_directory_still_uses_captured_pngs(self):
        result = self.prepare(subdir="samples")
        self.assertFalse(result["strict_five_image_policy"])
        self.run_writer("--subdir", "samples", "--expect-review-id", result["review_id"])
        self.assertEqual(len(list((self.pack / "samples").glob("*.png"))), 5)

    def test_pack_lock_conflict_never_writes(self):
        with pack_lock(self.pack, "test"), self.assertRaises(PackLockError):
            self.run_writer()

    def test_mapping_bytes_and_policy_changes_invalidate_id(self):
        mapping = self.root / "mapping.json"
        rows = [{"source": n, "target": f"images/{n}"} for n in self.names]
        mapping.write_text(json.dumps({"images": rows}), encoding="utf-8")
        original = self.prepare(mapping=mapping)["review_id"]
        rows[1]["source"], rows[2]["source"] = rows[2]["source"], rows[1]["source"]
        mapping.write_text(json.dumps({"images": rows}), encoding="utf-8")
        self.assertNotEqual(self.prepare(mapping=mapping)["review_id"], original)
        policy = json.loads(review.POLICY.read_text(encoding="utf-8"))
        policy["minimum_image_stddev"] = 2.5
        policy_path = self.root / "policy.json"
        policy_path.write_text(json.dumps(policy), encoding="utf-8")
        original = self.prepare()["review_id"]
        with mock.patch.object(review, "POLICY", policy_path):
            self.assertNotEqual(self.prepare()["review_id"], original)

    def test_failed_atomic_replace_keeps_old_file_and_cleans_temp(self):
        destination = self.root / "sample.png"
        destination.write_bytes(b"old")
        with mock.patch.object(writer.os, "replace", side_effect=OSError("disk failure")), self.assertRaises(OSError):
            writer.atomic_write_bytes(b"new", destination)
        self.assertEqual(destination.read_bytes(), b"old")
        self.assertEqual(list(self.root.glob(".*.tmp")), [])

    def test_mid_batch_failure_is_not_reported_as_full_transaction(self):
        self.install_baseline()
        old_second = (self.pack / "images" / self.names[1]).read_bytes()
        for index, name in enumerate(self.names):
            (self.source / name).write_bytes(fixture_png(index + 8, (900, 383) if index == 0 else (1080, 720)))
        real = writer.atomic_write_bytes
        def fail_second(raw, target):
            if target.name == self.names[1]:
                raise OSError("disk failure")
            real(raw, target)
        with mock.patch.object(writer, "atomic_write_bytes", side_effect=fail_second), self.assertRaisesRegex(SystemExit, "disk failure"):
            self.run_writer()
        self.assertEqual((self.pack / "images" / self.names[0]).read_bytes(), (self.source / self.names[0]).read_bytes())
        self.assertEqual((self.pack / "images" / self.names[1]).read_bytes(), old_second)
        self.assertTrue((self.pack / ".backups").exists())

    def test_research_scores_and_license_provenance(self):
        value = json.loads((ROOT / "research/github-image-assets-review-2026-10-09.json").read_text(encoding="utf-8"))
        self.assertEqual(sum(value["rubric"].values()), 100)
        self.assertEqual(len(value["candidates"]), 5)
        for row in value["candidates"]:
            with self.subTest(repo=row["repo"]):
                self.assertEqual(sum(row["scores"].values()), row["total"])
                self.assertTrue(all(0 <= score <= value["rubric"][key] for key, score in row["scores"].items()))
                self.assertEqual(len(row["sha"]), 40)
                self.assertEqual(len(row["license_blob_sha"]), 40)
                self.assertIn(row["sha"], row["license_verified_from"])
                self.assertNotEqual(row["license"], "NOASSERTION")


if __name__ == "__main__":
    unittest.main()
