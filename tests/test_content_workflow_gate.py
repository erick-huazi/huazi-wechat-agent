from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from io import BytesIO
from pathlib import Path

from PIL import Image
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import social_draft_router as router


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("content_workflow_gate", ROOT / "scripts" / "content_workflow_gate.py")
GATE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(GATE)

def png_bytes(width: int, height: int, marker: int) -> bytes:
    stream = BytesIO()
    Image.new("RGB", (width, height), (marker * 20, marker * 25, marker * 30)).save(stream, format="PNG")
    return stream.getvalue()


def write_ready_pack(pack: Path, video_required: bool = True) -> None:
    platforms = ["wechat", "toutiao", "zhihu", "xiaohongshu", "weibo"]
    if video_required:
        platforms += ["douyin-video", "video-channel", "kuaishou-video"]
    images = pack / "images"
    images.mkdir(parents=True)
    for index in range(1, 6):
        size = (900, 383) if index == 1 else (1080, 720)
        (images / f"2026-08-06-{index:02d}.png").write_bytes(png_bytes(*size, index))
    video_dir = pack / "video"
    video = video_dir / "2026-08-06-short-video.mp4"
    cover = video_dir / "2026-08-06-video-cover.png"
    if video_required:
        video_dir.mkdir()
        video.write_bytes(b"video")
        cover.write_bytes(png_bytes(1080, 1440, 8))
        (pack / "video-quality-report.json").write_text(json.dumps({
            "report_id": "qc-1", "passed": True,
            "video": str(video), "video_sha256": GATE.file_sha256(video),
            "cover": str(cover), "cover_sha256": GATE.file_sha256(cover),
        }), encoding="utf-8")
    candidates = [{
        "candidate_id": "selected" if index == 0 else f"other-{index}",
        "verified": True,
    } for index in range(5)]
    (pack / "topic-ranking.json").write_text(json.dumps({
        "passed": True,
        "ranking_id": "ranking-1",
        "candidate_count": 5,
        "selected_candidate_id": "selected",
        "selected_title": "Ready topic",
        "selected_score": 92,
        "candidates": candidates,
    }), encoding="utf-8")
    (pack / "growth-experiment.json").write_text(json.dumps({
        "schema_version": 1,
        "experiment_id": "experiment-1",
        "strategy_id": "huazi-survival-extreme-v1",
        "mode": "survival",
        "status": "planned",
        "ranking_id": "ranking-1",
        "selected_candidate_id": "selected",
        "variable": "opening_hook",
        "hypothesis": "Conflict-first opening improves qualified reading.",
        "control": "Descriptive opening.",
        "signature_move": "A counter-intuitive judgment backed by a workflow.",
        "breakout_reason": "The topic resolves a live practical conflict.",
        "audience_signal": "Completion and qualified sharing.",
        "kill_condition": "Stop when completion falls below baseline.",
    }), encoding="utf-8")
    (pack / "fact-check-notes.md").write_text(
        "# Sources\n\nhttps://github.com/example/a\n\nhttps://github.com/example/b\n" + "facts" * 40,
        encoding="utf-8",
    )
    (pack / "content-compliance-report.json").write_text(json.dumps({
        "passed": True,
        "platform_count": len(platforms),
        "items": [{"platform_id": key, "passed": True} for key in platforms],
        "blocking_count": 0,
        "policy_id": "policy-1",
    }), encoding="utf-8")
    plan_items = []
    for key in platforms:
        content = pack / f"fixture-{key}.md"
        content.write_text("Synthetic test content, not a platform article.", encoding="utf-8")
        item = {"platform_id": key, "content": str(content)}
        if key in {"douyin-video", "video-channel", "kuaishou-video"}:
            item.update(video=str(video), cover=str(cover), required_video_count=1)
        plan_items.append(item)
    plan = {"schema_version": 2, "pack": str(pack), "title": "Fixture", "draft_only_policy": True, "items": plan_items}
    plan["plan_id"] = router.plan_identity(plan)
    (pack / "distribution-plan.json").write_text(json.dumps(plan), encoding="utf-8")
    plan_hash = GATE.file_sha256(pack / "distribution-plan.json")
    bundles = router.workflow_bundles(pack, plan)
    dry = {
        "schema_version": 2,
        "dry_run": True,
        "inputs": {
            "plan_id": plan["plan_id"],
            "plan_file_sha256": plan_hash,
            "content_bundle": bundles["content"],
            "image_bundle": bundles["images"],
            "video_asset_bundle": bundles["video_assets"],
            "quality_gate_bundle": bundles["quality_gates"],
        },
        "items": [{"platform_id": key, "status": "validated"} for key in platforms],
    }
    dry["result_id"] = router.result_identity(dry)
    (pack / "distribution-dry-run-results.json").write_text(json.dumps(dry), encoding="utf-8")


class ContentWorkflowGateTests(unittest.TestCase):
    def test_ready_pack_reaches_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            pack = Path(temp) / "2026-08-06-ready-huazi"
            pack.mkdir()
            write_ready_pack(pack)
            result = GATE.evaluate_pack(pack)
            self.assertTrue(result["ready_for_draft_actions"])
            self.assertEqual(result["highest_contiguous_stage"], "preflight_passed")

    def test_manual_hold_blocks_topic_and_copy_review(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            pack = Path(temp) / "2026-08-06-hold-huazi"
            pack.mkdir()
            write_ready_pack(pack)
            GATE.write_review(pack, "hold", "tester", "needs revision")
            result = GATE.evaluate_pack(pack)
            self.assertFalse(result["ready_for_draft_actions"])
            self.assertIn("manual_review_hold", result["issues"])

    def test_missing_growth_experiment_blocks_research(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            pack = Path(temp) / "2026-08-06-no-experiment-huazi"
            pack.mkdir()
            write_ready_pack(pack)
            (pack / "growth-experiment.json").unlink()
            result = GATE.evaluate_pack(pack)
            self.assertFalse(result["ready_for_draft_actions"])
            self.assertIn("growth_experiment_missing", result["issues"])

    def test_new_pack_requires_valid_voice_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            pack = Path(temp) / "2026-08-22-voice-huazi"
            pack.mkdir()
            write_ready_pack(pack)

            missing = GATE.asset_gate(pack)
            self.assertFalse(missing["voice_profile_passed"])
            self.assertIn("video_audio_profile_missing", missing["issues"])

            policy = GATE.load_voice_policy()
            preset = policy["presets"][policy["default_preset"]]
            (pack / "video-audio-profile.json").write_text(json.dumps({
                "policy_id": policy["policy_id"],
                "preset_id": policy["default_preset"],
                "provider": policy["provider"],
                "language": policy["language"],
                "voice_id": preset["voice_id"],
                "rate": preset["rate"],
                "pitch": preset["pitch"],
                "volume": preset["volume"],
            }), encoding="utf-8")

            present = GATE.asset_gate(pack)
            self.assertTrue(present["voice_profile_passed"])
            self.assertNotIn("video_audio_profile_missing", present["issues"])
            self.assertNotIn("video_audio_profile_invalid", present["issues"])

    def test_new_pack_requires_content_quality_report(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            pack = Path(temp) / "2026-08-22-quality-huazi"
            pack.mkdir()
            write_ready_pack(pack)
            result = GATE.copy_gate(pack)
            self.assertFalse(result["quality_passed"])
            self.assertIn("content_quality_report_missing", result["issues"])

    def test_new_pack_requires_research_evidence_report(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            pack = Path(temp) / "2026-08-23-research-huazi"
            pack.mkdir()
            (pack / "fact-check-notes.md").write_text(
                "# Sources\n\nhttps://example.com/a\n\nhttps://example.net/b\n" + "facts" * 40,
                encoding="utf-8",
            )
            result = GATE.fact_gate(pack)
            self.assertFalse(result["evidence_passed"])
            self.assertIn("research_evidence_report_missing", result["issues"])


if __name__ == "__main__":
    unittest.main()
