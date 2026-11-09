import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import requested_topic as target
from distribution_io import file_sha256
from topic_candidate_ranker import rubric_for_pack


class RequestedTopicTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.pack = self.root / "outputs/2026-10-29-test"
        (self.pack / "media").mkdir(parents=True)
        (self.pack / "wechat-article.md").write_text("article", encoding="utf-8")
        videos = []
        for name in ("one", "two"):
            p = self.pack / f"media/{name}.mp4"
            p.write_bytes(name.encode())
            videos.append({"path": f"media/{name}.mp4", "sha256": file_sha256(p), "has_audio": False})
        self.entry = {"date": "2026-10-29", "waivers": ["news_freshness", "candidate_competition"], "browser_video_draft": True,
                      "user_confirmations": [{"answer": "confirmed"}, {"answer": "confirmed"}],
                      "article_sha256": file_sha256(self.pack / "wechat-article.md"), "videos": videos}
        self.policy = self.root / "policy.json"
        self.policy.write_text(json.dumps({"schema_version": 1, "packs": {self.pack.name: self.entry}}), encoding="utf-8")
        (self.pack / "topic-request.json").write_text(json.dumps(self.entry), encoding="utf-8")
        self.patches = [patch.object(target, "ROOT", self.root), patch.object(target, "POLICY", self.policy)]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def test_scoped_and_bound(self):
        self.assertTrue(target.request_for_pack(self.pack)["request_id"])
        self.assertIsNone(target.request_for_pack(self.root / "outputs/other"))
        self.assertIsNone(target.request_for_pack(self.root / "work" / self.pack.name))
        (self.pack / "wechat-article.md").write_text("changed", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "ARTICLE_CHANGED"):
            target.request_for_pack(self.pack)

    def test_changed_video_and_missing_review_block(self):
        self.assertFalse(target.inline_video_gate(self.pack)["passed"])
        (self.pack / "media/one.mp4").write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "VIDEO_CHANGED"):
            target.request_for_pack(self.pack)

    def test_only_candidate_and_time_requirements_change(self):
        base = json.loads((ROOT / "config/content-workflow-rubric.json").read_text(encoding="utf-8"))
        adapted = rubric_for_pack(self.pack, base)
        self.assertEqual(adapted["dimensions"], base["dimensions"])
        self.assertEqual(adapted["hard_requirements"], base["hard_requirements"])
        self.assertEqual(adapted["knowledge_layer"], base["knowledge_layer"])
        self.assertEqual(adapted["minimum_score"], 85)
        self.assertEqual(base["minimum_candidate_count"], 5)
        self.assertTrue(base["news_priority"]["enabled"])

    def test_changed_authorization_blocks(self):
        (self.pack / "topic-request.json").write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "AUTHORIZATION_CHANGED"):
            target.request_for_pack(self.pack)

    def test_graphic_request_is_article_bound_and_has_no_video_gate(self):
        self.entry.update(kind="wechat_graphic", browser_video_draft=False,
                          user_confirmations=[{"answer": "confirmed"}])
        self.entry.pop("videos")
        self.policy.write_text(json.dumps({"schema_version": 1, "packs": {self.pack.name: self.entry}}), encoding="utf-8")
        (self.pack / "topic-request.json").write_text(json.dumps(self.entry), encoding="utf-8")
        self.assertTrue(target.request_for_pack(self.pack)["request_id"])
        self.assertFalse(target.inline_video_gate(self.pack)["required"])
        (self.pack / "wechat-article.md").write_text("changed", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "ARTICLE_CHANGED"):
            target.request_for_pack(self.pack)
