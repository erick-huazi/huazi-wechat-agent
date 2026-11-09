from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from io import BytesIO
from pathlib import Path

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("distribution_io", ROOT / "scripts" / "distribution_io.py")
IO = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(IO)


class DistributionIOTests(unittest.TestCase):
    def test_dated_pack_image_names_are_strict(self) -> None:
        names = IO.five_image_names(Path("2026-07-27-topic-huazi"))
        self.assertEqual(names, [f"2026-07-27-{index:02d}.png" for index in range(1, 6)])

    def test_undated_history_keeps_legacy_image_names(self) -> None:
        self.assertEqual(
            IO.five_image_names(Path("historical-pack")),
            list(IO.LEGACY_FIVE_IMAGE_NAMES),
        )

    def test_atomic_write_json_replaces_complete_document(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "state.json"
            path.write_text("{broken", encoding="utf-8")
            IO.atomic_write_json(path, {"status": "ok", "items": [1, 2, 3]})
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["status"], "ok")
            self.assertEqual(list(path.parent.glob(".*.tmp")), [])

    def test_pack_lock_rejects_concurrent_writer(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            pack = Path(temp)
            with IO.pack_lock(pack, "audit"):
                with self.assertRaises(IO.PackLockError):
                    with IO.pack_lock(pack, "plan"):
                        pass
            self.assertFalse((pack / IO.PACK_LOCK_NAME).exists())

    def test_pack_lock_recovers_stale_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            pack = Path(temp)
            old = (datetime.now().astimezone() - timedelta(hours=2)).isoformat(timespec="seconds")
            (pack / IO.PACK_LOCK_NAME).write_text(json.dumps({
                "token": "old", "pid": 1, "action": "plan", "started_at": old,
            }), encoding="utf-8")
            with IO.pack_lock(pack, "audit", stale_seconds=60) as lock:
                self.assertEqual(lock["action"], "audit")
            self.assertTrue(any(pack.glob(".distribution-action.stale-*.json")))

    def test_ledger_hash_chain_detects_tampering(self) -> None:
        records = IO.chain_ledger_records([
            {"platform_id": "wechat", "status": "draft_created"},
            {"platform_id": "toutiao", "status": "unverified"},
        ])
        self.assertTrue(IO.validate_ledger_chain(records)["valid"])
        records[0]["status"] = "changed"
        status = IO.validate_ledger_chain(records)
        self.assertFalse(status["valid"])
        self.assertEqual(status["broken_at"], 1)

    def test_backup_retention_and_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            pack = Path(temp)
            (pack / "distribution-plan.json").write_text("{}", encoding="utf-8")
            backup = IO.create_pack_backup(pack, "plan", ["distribution-plan.json"], keep=2)
            manifest = json.loads((backup / "backup-manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["files"][0]["name"], "distribution-plan.json")
            listed = IO.list_pack_backups(pack, verify=True)[0]
            self.assertEqual(listed["action"], "plan")
            self.assertTrue(listed["verification"]["valid"])
            self.assertEqual(listed["comparison"]["unchanged"], 1)
            (pack / "distribution-plan.json").write_text('{"changed": true}', encoding="utf-8")
            comparison = IO.compare_pack_backup(pack, backup)
            self.assertEqual(comparison["changed"], 1)
            (backup / "distribution-plan.json").write_text("changed", encoding="utf-8")
            self.assertFalse(IO.verify_pack_backup(backup)["valid"])

    def test_backup_preserves_nested_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            pack = Path(temp)
            nested = pack / "images" / "2026-08-06-01.png"
            nested.parent.mkdir(parents=True)
            nested.write_bytes(b"image-bytes")
            backup = IO.create_pack_backup(pack, "assets", ["images/2026-08-06-01.png"])
            copied = backup / "images" / "2026-08-06-01.png"
            self.assertEqual(copied.read_bytes(), b"image-bytes")
            self.assertTrue(IO.verify_pack_backup(backup)["valid"])

    def test_image_probe_requires_a_decodable_image(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            broken = root / "broken.png"
            broken.write_bytes(b"\x89PNG\r\n\x1a\n" + b"broken")
            self.assertFalse(IO.image_file_probe(broken)["decoded"])
            valid = root / "valid.png"
            stream = BytesIO()
            Image.new("RGB", (64, 32), (10, 20, 30)).save(stream, format="PNG")
            valid.write_bytes(stream.getvalue())
            probe = IO.image_file_probe(valid)
            self.assertTrue(probe["decoded"])
            self.assertEqual((probe["width"], probe["height"]), (64, 32))

    def test_file_bundle_snapshot_detects_changes_and_external_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            article = root / "article.md"
            article.write_text("version one", encoding="utf-8")
            first = IO.file_bundle_snapshot(root, [article])
            self.assertTrue(first["valid"])
            article.write_text("version two", encoding="utf-8")
            second = IO.file_bundle_snapshot(root, [article])
            self.assertNotEqual(first["fingerprint"], second["fingerprint"])
            external = IO.file_bundle_snapshot(root, [root.parent / "outside.md"])
            self.assertFalse(external["valid"])
            self.assertFalse(external["files"][0]["allowed"])

    def test_chained_action_log_detects_anchor_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            log = root / "actions.jsonl"
            anchor = root / "actions-chain.json"
            IO.append_chained_jsonl(log, anchor, [{"action": "audit", "status": "ok"}])
            self.assertTrue(IO.inspect_chained_jsonl(log, anchor)["valid"])
            anchor_payload = json.loads(anchor.read_text(encoding="utf-8"))
            anchor_payload["entries"] = 99
            anchor.write_text(json.dumps(anchor_payload), encoding="utf-8")
            self.assertFalse(IO.inspect_chained_jsonl(log, anchor)["valid"])


if __name__ == "__main__":
    unittest.main()
