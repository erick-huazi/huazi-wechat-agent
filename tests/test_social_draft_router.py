from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest
from datetime import datetime
from functools import lru_cache
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("social_draft_router", ROOT / "scripts" / "social_draft_router.py")
ROUTER = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(ROUTER)


@lru_cache(maxsize=32)
def png_header(width: int = 1080, height: int = 720, marker: int = 0) -> bytes:
    stream = BytesIO()
    Image.new("RGB", (width, height), ((marker * 43) % 255, (marker * 79) % 255, (marker * 127) % 255)).save(stream, format="PNG")
    return stream.getvalue()


def write_five_images(pack: Path) -> None:
    (pack / "images").mkdir()
    for index, name in enumerate(ROUTER.CANONICAL_IMAGE_NAMES, start=1):
        size = (900, 383) if name == "00-cover.png" else (1080, 720)
        (pack / "images" / name).write_bytes(png_header(*size, index))


def write_dated_five_images(pack: Path, image_date: str) -> list[str]:
    (pack / "images").mkdir()
    names = [f"{image_date}-{index:02d}.png" for index in range(1, 6)]
    for index, name in enumerate(names):
        size = (900, 383) if index == 0 else (1080, 720)
        (pack / "images" / name).write_bytes(png_header(*size, index + 10))
    return names


def write_video_assets(pack: Path, image_date: str) -> None:
    video_root = pack / "video"
    video_root.mkdir()
    (video_root / f"{image_date}-short-video.mp4").write_bytes(b"test-video")
    (video_root / f"{image_date}-video-cover.png").write_bytes(png_header(1080, 1440, 88))


def write_video_quality_report(pack: Path, image_date: str) -> None:
    video = pack / "video" / f"{image_date}-short-video.mp4"
    cover = pack / "video" / f"{image_date}-video-cover.png"
    (pack / "video-quality-report.json").write_text(
        json.dumps({
            "schema_version": 1,
            "report_id": "video-qc-test",
            "passed": True,
            "video_sha256": ROUTER.file_sha256(video),
            "cover_sha256": ROUTER.file_sha256(cover),
            "checks": {"no_blank_sample_frames": True},
            "errors": [],
        }),
        encoding="utf-8",
    )


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


class SocialDraftRouterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.registry = ROUTER.load_registry(ROOT / "config" / "social-platforms.json")

    def test_alias_resolution_supports_chinese_comma_and_deduplicates(self) -> None:
        platforms = ROUTER.resolve_platforms("公众号，微信,toutiao,知乎", self.registry)
        self.assertEqual([item["id"] for item in platforms], ["wechat", "toutiao", "zhihu"])

    def test_unknown_platform_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ROUTER.resolve_platforms("不存在的平台", self.registry)

    def test_wechat_credentials_can_be_hydrated_from_secure_store(self) -> None:
        saved = {name: os.environ.pop(name, None) for name in ROUTER.WECHAT_CREDENTIAL_NAMES}

        def hydrate() -> str:
            os.environ["WECHAT_APP_ID"] = "secure-id"
            os.environ["WECHAT_APP_SECRET"] = "secure-secret"
            return "windows_credential_manager"

        try:
            with mock.patch.object(ROUTER, "hydrate_wechat_environment", side_effect=hydrate):
                ready, missing, source = ROUTER.configured(list(ROUTER.WECHAT_CREDENTIAL_NAMES))
        finally:
            for name in ROUTER.WECHAT_CREDENTIAL_NAMES:
                os.environ.pop(name, None)
                if saved[name] is not None:
                    os.environ[name] = saved[name]

        self.assertTrue(ready)
        self.assertEqual(missing, [])
        self.assertEqual(source, "windows_credential_manager")

    def test_wechat_plan_checks_real_five_images_cover_and_order(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            pack = Path(temp)
            write_five_images(pack)
            refs = "\n".join(f"![图{i}](images/{name})" for i, name in enumerate(ROUTER.CANONICAL_IMAGE_NAMES))
            (pack / "wechat-article.md").write_text("# 一个足够清楚的公众号标题\n" + refs + "\n" + "正文" * 600, encoding="utf-8")
            platform = ROUTER.resolve_platforms("wechat", self.registry)[0]
            item = ROUTER.plan_item(pack, "发布包标题", platform)
            self.assertEqual(item["problems"], [])
            self.assertEqual(item["title"], "一个足够清楚的公众号标题")

    def test_requested_graphic_uses_official_api_without_inline_video(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            pack = Path(temp)
            write_five_images(pack)
            refs = "\n".join(f"![图{i}](images/{name})" for i, name in enumerate(ROUTER.CANONICAL_IMAGE_NAMES))
            (pack / "wechat-article.md").write_text("# 一个足够清楚的公众号标题\n" + refs + "\n" + "正文" * 600, encoding="utf-8")
            platform = ROUTER.resolve_platforms("wechat", self.registry)[0]
            request = {"kind": "wechat_graphic", "browser_video_draft": False, "request_id": "graphic-request"}
            with mock.patch("requested_topic.request_for_pack", return_value=request), mock.patch.object(ROUTER, "configured", return_value=(True, [], "test")):
                item = ROUTER.plan_item(pack, "发布包标题", platform)
            self.assertEqual(item["delivery"], "official_draft_api")
            self.assertEqual(item["request_id"], "graphic-request")
            self.assertNotIn("inline_videos", item)

    def test_wechat_plan_accepts_dated_five_image_names(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            pack = Path(temp) / "2026-07-22-test-huazi"
            pack.mkdir()
            names = write_dated_five_images(pack, "2026-07-22")
            refs = "\n".join(f"![图{i}](images/{name})" for i, name in enumerate(names))
            (pack / "wechat-article.md").write_text(
                "# 一个足够清楚的公众号标题\n" + refs + "\n" + "正文" * 600,
                encoding="utf-8",
            )
            platform = ROUTER.resolve_platforms("wechat", self.registry)[0]
            item = ROUTER.plan_item(pack, "发布包标题", platform)
            self.assertEqual(item["problems"], [])
            self.assertTrue(item["cover"].endswith("2026-07-22-01.png"))

    def test_plan_and_bundle_fingerprints_track_content_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            pack = Path(temp)
            write_five_images(pack)
            content = pack / "xiaohongshu.md"
            content.write_text("# 指纹测试\n\n" + "正文" * 100, encoding="utf-8")
            platform = ROUTER.resolve_platforms("xiaohongshu", self.registry)
            first_plan = ROUTER.build_plan(pack, "指纹测试", platform, self.registry)
            second_plan = ROUTER.build_plan(pack, "指纹测试", platform, self.registry)
            self.assertEqual(first_plan["plan_id"], second_plan["plan_id"])
            first_bundle = ROUTER.workflow_bundles(pack, first_plan)
            content.write_text("# 指纹测试\n\n" + "已修改" * 100, encoding="utf-8")
            second_bundle = ROUTER.workflow_bundles(pack, first_plan)
            self.assertNotEqual(
                first_bundle["content"]["fingerprint"],
                second_bundle["content"]["fingerprint"],
            )

    def test_push_reuses_matching_plan_without_rewriting_it(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            pack = Path(temp) / "2026-07-23-test-huazi"
            pack.mkdir()
            write_dated_five_images(pack, "2026-07-23")
            (pack / "xiaohongshu.md").write_text(
                "# Stable plan\n\n" + "Content" * 100,
                encoding="utf-8",
            )
            platforms = ROUTER.resolve_platforms("xiaohongshu", self.registry)
            plan = ROUTER.build_plan(pack, "Stable plan", platforms, self.registry, True)
            plan["generated_at"] = "2000-01-01T00:00:00+08:00"
            plan_path = pack / "distribution-plan.json"
            ROUTER.atomic_write_json(plan_path, plan)
            before = plan_path.read_bytes()
            args = SimpleNamespace(
                pack=str(pack),
                platforms="xiaohongshu",
                title="Stable plan",
                output="",
                author="Huazi",
                digest="",
                dry_run=True,
                confirm_drafts=False,
            )

            with mock.patch("builtins.print"):
                self.assertEqual(ROUTER.command_push(args, self.registry), 0)

            self.assertEqual(plan_path.read_bytes(), before)

    def test_result_identity_ignores_runtime_timestamps(self) -> None:
        first = {
            "schema_version": 2,
            "pack": "C:/packs/example",
            "title": "Stable result",
            "dry_run": False,
            "inputs": {"plan_id": "plan-1"},
            "items": [{
                "platform_id": "toutiao",
                "status": "success",
                "started_at": "2026-07-22T08:00:00+08:00",
                "finished_at": "2026-07-22T08:01:00+08:00",
                "evidence": {
                    "kind": "browser_visual",
                    "verified_at": "2026-07-22T08:01:00+08:00",
                    "summary": "Draft visible",
                    "reference": "evidence/toutiao.png",
                },
            }],
        }
        second = json.loads(json.dumps(first))
        second["items"][0]["started_at"] = "2026-07-22T09:00:00+08:00"
        second["items"][0]["finished_at"] = "2026-07-22T09:01:00+08:00"
        second["items"][0]["evidence"]["verified_at"] = "2026-07-22T09:01:00+08:00"
        first["inputs"]["plan_file_sha256"] = "old-plan-file"
        second["inputs"]["plan_file_sha256"] = "new-plan-file"
        self.assertEqual(ROUTER.result_identity(first), ROUTER.result_identity(second))

        second["items"][0]["status"] = "blocked"
        self.assertNotEqual(ROUTER.result_identity(first), ROUTER.result_identity(second))

    def test_all_five_image_platforms_reject_wrong_body_dimensions(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            pack = Path(temp) / "2026-07-22-test-huazi"
            pack.mkdir()
            names = write_dated_five_images(pack, "2026-07-22")
            (pack / "images" / names[3]).write_bytes(png_header(1080, 1080, 99))
            (pack / "xiaohongshu.md").write_text("# 五图尺寸测试\n\n" + "正文" * 100, encoding="utf-8")
            platform = ROUTER.resolve_platforms("xiaohongshu", self.registry)[0]
            problems = ROUTER.plan_item(pack, "五图尺寸测试", platform)["problems"]
            self.assertTrue(any(item.startswith("invalid_canonical_image_dimensions:") for item in problems))

    def test_wechat_rejects_missing_markdown_image_even_when_pack_has_five(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            pack = Path(temp)
            write_five_images(pack)
            refs = [f"![图{i}](images/{name})" for i, name in enumerate(ROUTER.CANONICAL_IMAGE_NAMES)]
            refs[2] = "![缺图](images/not-found.png)"
            (pack / "wechat-article.md").write_text(
                "# 一个足够清楚的公众号标题\n" + "\n".join(refs) + "\n" + "正文" * 600,
                encoding="utf-8",
            )
            platform = ROUTER.resolve_platforms("wechat", self.registry)[0]
            problems = ROUTER.plan_item(pack, "发布包标题", platform)["problems"]
            self.assertIn("wechat_contains_missing_or_remote_images", problems)
            self.assertIn("wechat_images_must_follow_canonical_01_to_05_order", problems)

    def test_registry_is_valid_json(self) -> None:
        raw = json.loads((ROOT / "config" / "social-platforms.json").read_text(encoding="utf-8"))
        self.assertEqual(raw["schema_version"], 3)
        self.assertGreaterEqual(len(raw["platforms"]), 10)

    def test_default_platform_is_wechat_only(self) -> None:
        platforms = ROUTER.resolve_platforms(ROUTER.DEFAULT_PLATFORMS, self.registry)
        self.assertEqual(
            [item["id"] for item in platforms],
            ["wechat"],
        )
        self.assertEqual(ROUTER.resolve_platforms("抖音,快手,视频号", self.registry)[0]["id"], "douyin-video")

    def test_oschina_prefers_dedicated_technical_copy(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            pack = Path(temp)
            (pack / "images").mkdir()
            (pack / "images" / "00-cover.png").write_bytes(b"image")
            (pack / "oschina.md").write_text("# OSCHINA technical copy\n", encoding="utf-8")
            (pack / "bilibili.md").write_text("# Bilibili fallback\n", encoding="utf-8")
            platform = ROUTER.resolve_platforms("oschina", self.registry)[0]
            item = ROUTER.plan_item(pack, "Technical title", platform)
            self.assertEqual(Path(item["content"]).name, "oschina.md")
            self.assertEqual(item["delivery"], "browser_draft")
            self.assertEqual(item["problems"], [])

    def test_short_form_platforms_require_dedicated_copy(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            pack = Path(temp)
            write_five_images(pack)
            (pack / "xiaohongshu.md").write_text("# 小红书标题\n\n" + "正文" * 100, encoding="utf-8")
            for platform in ROUTER.resolve_platforms("抖音,快手", self.registry):
                item = ROUTER.plan_item(pack, "发布包标题", platform)
                self.assertIn(f"missing_dedicated_content:{platform['content_files'][0]}", item["problems"])

    def test_kuaishou_rejects_description_over_500_chars(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            pack = Path(temp) / "2026-08-06-video-huazi"
            pack.mkdir()
            write_video_assets(pack, "2026-08-06")
            (pack / "kuaishou-video.md").write_text("# 快手视频标题\n\n" + "字" * 501, encoding="utf-8")
            platform = ROUTER.resolve_platforms("快手", self.registry)[0]
            with mock.patch.object(ROUTER, "video_file_probe", return_value=VALID_VIDEO_PROBE):
                problems = ROUTER.plan_item(pack, "发布包标题", platform)["problems"]
            self.assertIn("content_too_long:501>500", problems)

    def test_video_plan_requires_audio_and_accepts_shared_vertical_asset(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            pack = Path(temp) / "2026-08-06-video-huazi"
            pack.mkdir()
            write_video_assets(pack, "2026-08-06")
            write_video_quality_report(pack, "2026-08-06")
            (pack / "douyin-video.md").write_text("# 抖音视频测试标题\n\n" + "正文" * 40, encoding="utf-8")
            platform = ROUTER.resolve_platforms("抖音", self.registry)[0]
            with mock.patch.object(ROUTER, "video_file_probe", return_value=VALID_VIDEO_PROBE):
                item = ROUTER.plan_item(pack, "发布包标题", platform)
            self.assertEqual(item["problems"], [])
            self.assertEqual(item["video_count"], 1)
            self.assertEqual(item["status"], "manual_browser")

            no_audio = {**VALID_VIDEO_PROBE, "has_audio": False, "audio_codec": ""}
            with mock.patch.object(ROUTER, "video_file_probe", return_value=no_audio):
                problems = ROUTER.plan_item(pack, "发布包标题", platform)["problems"]
            self.assertIn("video_audio_missing", problems)

            (pack / "video" / "2026-08-06-short-video.mp4").write_bytes(b"changed-video")
            with mock.patch.object(ROUTER, "video_file_probe", return_value=VALID_VIDEO_PROBE):
                problems = ROUTER.plan_item(pack, "Publish title", platform)["problems"]
            self.assertIn("video_quality_report_stale", problems)

    def test_video_audit_requires_video_audio_cover_and_draft_verification(self) -> None:
        plan = {"items": [{
            "platform_id": "douyin-video",
            "platform": "抖音视频",
            "delivery": "browser_draft",
            "required_video_count": 1,
            "cover": "video/cover.png",
            "video_probe": {"has_audio": True},
        }]}
        results = {"items": [{
            "platform_id": "douyin-video",
            "status": "local_draft_created",
            "video_count": 1,
            "video_verified": True,
            "audio_verified": True,
            "cover_verified": True,
            "draft_verified": True,
            "proof": "重开草稿后成片、音轨、封面和文案可见",
            "evidence": {
                "kind": "browser_visual",
                "verified_at": datetime.now().astimezone().isoformat(),
                "summary": "视频草稿已重开核验",
                "reference": "draft-123",
            },
        }]}
        audit = ROUTER.audit_distribution(ROOT, plan, results)
        self.assertTrue(audit["complete"])
        results["items"][0]["audio_verified"] = False
        audit = ROUTER.audit_distribution(ROOT, plan, results)
        self.assertIn("audio_not_verified", audit["items"][0]["issues"])

    def test_plan_blocks_engagement_inducement_for_every_platform(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            pack = Path(temp) / "2026-08-01-compliance-huazi"
            pack.mkdir()
            write_dated_five_images(pack, "2026-08-01")
            for platform in ROUTER.resolve_platforms(ROUTER.DEFAULT_PLATFORMS, self.registry):
                filename = platform["content_files"][0]
                (pack / filename).write_text(
                    "# 合规门禁测试标题\n\n需要完整模板，留言关键词领取。\n" + "正文" * 600,
                    encoding="utf-8",
                )
                item = ROUTER.plan_item(pack, "合规门禁测试标题", platform)
                self.assertTrue(
                    any(problem.startswith("content_compliance:") for problem in item["problems"]),
                    platform["id"],
                )
                self.assertFalse(item["compliance"]["passed"])

    def test_plan_records_clean_compliance_result(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            pack = Path(temp) / "2026-08-01-clean-huazi"
            pack.mkdir()
            write_dated_five_images(pack, "2026-08-01")
            (pack / "xiaohongshu.md").write_text(
                "# 合规内容测试\n\n" + "今天先把一项重复工作写成输入、步骤和验收标准。" * 20,
                encoding="utf-8",
            )
            platform = ROUTER.resolve_platforms("xiaohongshu", self.registry)[0]
            item = ROUTER.plan_item(pack, "合规内容测试", platform)
            self.assertTrue(item["compliance"]["passed"])
            self.assertFalse(any(problem.startswith("content_compliance:") for problem in item["problems"]))

    def test_audit_rejects_complete_browser_result_without_proof(self) -> None:
        plan = {
            "items": [{
                "platform_id": "xiaohongshu",
                "platform": "小红书",
                "delivery": "browser_draft",
                "required_image_count": 5,
            }]
        }
        results = {"items": [{"platform_id": "xiaohongshu", "status": "local_draft_created", "image_count": 5}]}
        audit = ROUTER.audit_distribution(ROOT, plan, results)
        self.assertFalse(audit["complete"])
        self.assertEqual(audit["items"][0]["quality"], "invalid")
        self.assertIn("missing_browser_draft_proof", audit["items"][0]["issues"])

    def test_audit_requires_visual_image_cover_and_draft_verification(self) -> None:
        plan = {
            "items": [{
                "platform_id": "xiaohongshu",
                "platform": "小红书",
                "delivery": "browser_draft",
                "required_image_count": 5,
                "cover": "images/2026-07-22-01.png",
            }]
        }
        results = {"items": [{
            "platform_id": "xiaohongshu",
            "status": "local_draft_created",
            "image_count": 5,
            "image_verified": True,
            "cover_verified": True,
            "draft_verified": True,
            "proof": "5 张图均有 naturalWidth，封面预览可见，暂存后草稿仍存在",
            "evidence": {
                "kind": "browser_visual",
                "verified_at": datetime.now().astimezone().isoformat(),
                "summary": "五张图片、封面与草稿均在编辑器中可见",
                "reference": "123456",
            },
        }]}
        audit = ROUTER.audit_distribution(ROOT, plan, results)
        self.assertTrue(audit["complete"])
        self.assertEqual(audit["items"][0]["quality"], "complete")

        results["items"][0]["image_verified"] = False
        audit = ROUTER.audit_distribution(ROOT, plan, results)
        self.assertFalse(audit["complete"])
        self.assertIn("images_not_visually_verified", audit["items"][0]["issues"])

    def test_audit_validates_local_evidence_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            pack = Path(temp)
            plan = {"items": [{
                "platform_id": "xiaohongshu", "platform": "小红书", "delivery": "browser_draft",
                "required_image_count": 5, "cover": "images/cover.png",
            }]}
            result = {
                "platform_id": "xiaohongshu", "status": "local_draft_created", "image_count": 5,
                "image_verified": True, "cover_verified": True, "draft_verified": True,
                "proof": "草稿已留存",
                "evidence": {
                    "kind": "browser_visual", "verified_at": datetime.now().astimezone().isoformat(),
                    "summary": "五图和草稿均可见", "reference": "evidence/xiaohongshu.png",
                },
            }
            audit = ROUTER.audit_distribution(pack, plan, {"items": [result]})
            self.assertEqual(audit["items"][0]["quality"], "invalid")
            self.assertIn("missing_or_external_evidence_artifact", audit["items"][0]["issues"])
            evidence_path = pack / "evidence" / "xiaohongshu.png"
            evidence_path.parent.mkdir()
            evidence_path.write_bytes(png_header(320, 180, 7))
            result["evidence"]["sha256"] = ROUTER.file_sha256(evidence_path)
            audit = ROUTER.audit_distribution(pack, plan, {"items": [result]})
            self.assertTrue(audit["complete"])
            self.assertEqual(len(audit["items"][0]["evidence"]["artifact"]["sha256"]), 64)

    def test_audit_rejects_complete_result_without_structured_evidence(self) -> None:
        plan = {
            "items": [{
                "platform_id": "xiaohongshu",
                "platform": "小红书",
                "delivery": "browser_draft",
                "required_image_count": 5,
                "cover": "images/2026-07-22-01.png",
            }]
        }
        results = {"items": [{
            "platform_id": "xiaohongshu",
            "status": "local_draft_created",
            "image_count": 5,
            "image_verified": True,
            "cover_verified": True,
            "draft_verified": True,
            "proof": "旧式文字说明仍保留，但不再单独算作结构化凭证",
        }]}
        audit = ROUTER.audit_distribution(ROOT, plan, results)
        self.assertFalse(audit["complete"])
        self.assertEqual(audit["items"][0]["quality"], "invalid")
        self.assertIn("missing_structured_evidence", audit["items"][0]["issues"])
        self.assertEqual(audit["schema_version"], 2)

    def test_audit_rejects_complete_result_without_evidence_reference(self) -> None:
        plan = {"items": [{
            "platform_id": "xiaohongshu", "platform": "小红书", "delivery": "browser_draft",
            "required_image_count": 5, "cover": "images/2026-07-22-01.png",
        }]}
        results = {"items": [{
            "platform_id": "xiaohongshu", "status": "local_draft_created", "image_count": 5,
            "image_verified": True, "cover_verified": True, "draft_verified": True,
            "proof": "草稿已留存",
            "evidence": {
                "kind": "browser_visual", "verified_at": "2026-07-21T20:00:00+08:00",
                "summary": "五图和草稿均可见", "reference": "",
            },
        }]}
        audit = ROUTER.audit_distribution(ROOT, plan, results)
        self.assertEqual(audit["items"][0]["quality"], "invalid")
        self.assertIn("missing_evidence_reference", audit["items"][0]["issues"])

    def test_evidence_ledger_is_append_only_and_deduplicated(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            pack = Path(temp)
            results = {"items": [{
                "platform_id": "wechat", "status": "draft_created",
                "image_verified": True, "cover_verified": True, "draft_verified": True,
                "evidence": {
                    "kind": "api_response", "verified_at": "2026-07-21T20:00:00+08:00",
                    "summary": "已返回 media_id", "reference": "media-id-12345678901234567890",
                },
            }]}
            audit = {"items": [{"platform_id": "wechat", "quality": "complete"}]}
            self.assertEqual(ROUTER.append_evidence_ledger(pack, results, audit), 1)
            self.assertEqual(ROUTER.append_evidence_ledger(pack, results, audit), 0)
            lines = (pack / "distribution-evidence.jsonl").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 1)
            row = json.loads(lines[0])
            self.assertEqual(row["platform_id"], "wechat")
            self.assertEqual(row["chain_version"], 1)
            self.assertEqual(len(row["record_hash"]), 64)
            anchor = json.loads((pack / "distribution-evidence-chain.json").read_text(encoding="utf-8"))
            self.assertEqual(anchor["entries"], 1)
            self.assertEqual(anchor["head_hash"], row["record_hash"])

    def test_evidence_ledger_refuses_broken_hash_chain(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            pack = Path(temp)
            results = {"items": [{
                "platform_id": "wechat", "status": "draft_created",
                "image_verified": True, "cover_verified": True, "draft_verified": True,
                "evidence": {
                    "kind": "api_response", "verified_at": "2026-07-21T20:00:00+08:00",
                    "summary": "已返回 media_id", "reference": "media-id-12345678901234567890",
                },
            }]}
            audit = {"items": [{"platform_id": "wechat", "quality": "complete"}]}
            ROUTER.append_evidence_ledger(pack, results, audit)
            path = pack / "distribution-evidence.jsonl"
            row = json.loads(path.read_text(encoding="utf-8"))
            row["status"] = "tampered"
            path.write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                ROUTER.append_evidence_ledger(pack, results, audit)

    def test_evidence_ledger_anchor_detects_tail_truncation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            pack = Path(temp)
            results = {"items": [
                {
                    "platform_id": "wechat", "status": "draft_created",
                    "image_verified": True, "cover_verified": True, "draft_verified": True,
                    "evidence": {
                        "kind": "api_response", "verified_at": "2026-07-21T20:00:00+08:00",
                        "summary": "已返回 media_id", "reference": "media-id-12345678901234567890",
                    },
                },
                {
                    "platform_id": "toutiao", "status": "unverified",
                    "image_verified": False, "cover_verified": True, "draft_verified": False,
                    "evidence": {
                        "kind": "browser_state", "verified_at": "2026-07-21T20:01:00+08:00",
                        "summary": "编辑器待核验", "reference": "https://example.com/editor",
                    },
                },
            ]}
            audit = {"items": [
                {"platform_id": "wechat", "quality": "complete"},
                {"platform_id": "toutiao", "quality": "unverified"},
            ]}
            ROUTER.append_evidence_ledger(pack, results, audit)
            path = pack / "distribution-evidence.jsonl"
            lines = path.read_text(encoding="utf-8").splitlines()
            path.write_text(lines[0] + "\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                ROUTER.append_evidence_ledger(pack, results, audit)


class StrictDatedImageNamingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.registry = ROUTER.load_registry(ROOT / "config" / "social-platforms.json")

    def test_dated_pack_rejects_legacy_image_names(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            pack = Path(temp) / "2026-07-27-test-huazi"
            pack.mkdir()
            write_five_images(pack)
            refs = "\n".join(
                f"![image-{index}](images/{name})"
                for index, name in enumerate(ROUTER.CANONICAL_IMAGE_NAMES, start=1)
            )
            (pack / "wechat-article.md").write_text(
                "# Dated pack naming guard\n" + refs + "\n" + "body" * 600,
                encoding="utf-8",
            )
            platform = ROUTER.resolve_platforms("wechat", self.registry)[0]
            item = ROUTER.plan_item(pack, "Dated pack naming guard", platform)
            self.assertTrue(any(
                problem.startswith("missing_canonical_images:2026-07-27-01.png")
                for problem in item["problems"]
            ))
            self.assertEqual(item["cover"], "")

    def test_registry_uses_dated_cover_template(self) -> None:
        five_image_platforms = [
            platform
            for platform in self.registry["platforms"]
            if platform.get("required_image_count") == 5
        ]
        self.assertTrue(five_image_platforms)
        self.assertTrue(all(
            platform.get("cover") == "images/{pack_date}-01.png"
            for platform in five_image_platforms
        ))


if __name__ == "__main__":
    unittest.main()
