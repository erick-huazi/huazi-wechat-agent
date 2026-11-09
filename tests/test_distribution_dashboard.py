from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from functools import lru_cache
from io import BytesIO
from pathlib import Path
from unittest import mock

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "distribution_dashboard", ROOT / "scripts" / "distribution_dashboard.py"
)
DASHBOARD = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(DASHBOARD)


@lru_cache(maxsize=32)
def png_bytes(width: int, height: int, marker: int = 0) -> bytes:
    stream = BytesIO()
    Image.new("RGB", (width, height), ((marker * 47) % 255, (marker * 83) % 255, (marker * 131) % 255)).save(stream, format="PNG")
    return stream.getvalue()


class DistributionDashboardTests(unittest.TestCase):
    def test_result_and_audit_ids_ignore_runtime_timestamps(self) -> None:
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
        second["items"][0]["evidence"]["verified_at"] = "2026-07-22T09:01:00+08:00"
        first["inputs"]["plan_file_sha256"] = "old-plan-file"
        second["inputs"]["plan_file_sha256"] = "new-plan-file"
        self.assertEqual(DASHBOARD.result_identity(first), DASHBOARD.result_identity(second))

        audit_a = {"pack": first["pack"], "summary": {"complete": 1}, "items": first["items"]}
        audit_b = {"pack": second["pack"], "summary": {"complete": 1}, "items": second["items"]}
        self.assertEqual(
            DASHBOARD.audit_identity(audit_a, "plan-1", "result-1"),
            DASHBOARD.audit_identity(audit_b, "plan-1", "result-1"),
        )

    def test_empty_dashboard_is_a_valid_fresh_cycle(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            payload = DASHBOARD.dashboard_payload(Path(temp))
            self.assertEqual(payload["packs"], [])
            self.assertIsNone(payload["selected"])
            self.assertEqual(payload["platform_matrix"], {"columns": [], "rows": []})
            self.assertEqual(payload["system_health"]["status"], "online")
            self.assertIsNone(payload["system_health"]["pack_freshness_minutes"])
            self.assertTrue(payload["draft_only"])

    def test_pack_path_cannot_escape_outputs_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "valid").mkdir()
            self.assertEqual(DASHBOARD.ensure_pack(root, "valid"), (root / "valid").resolve())
            with self.assertRaises(ValueError):
                DASHBOARD.ensure_pack(root, "../outside")

    def test_dashboard_payload_merges_plan_results_and_audit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            pack = root / "2026-07-20-test-huazi"
            pack.mkdir()
            (pack / "README.md").write_text("# 测试发布包\n", encoding="utf-8")
            (pack / "distribution-plan.json").write_text(json.dumps({
                "items": [{
                    "platform_id": "wechat",
                    "platform": "微信公众号",
                    "title": "测试标题",
                    "content": str(pack / "wechat-article.md"),
                    "delivery": "official_draft_api",
                    "image_count": 5,
                    "content_chars": 1200,
                }]
            }, ensure_ascii=False), encoding="utf-8")
            (pack / "distribution-results.json").write_text(json.dumps({
                "items": [{
                    "platform_id": "wechat",
                    "status": "draft_created",
                    "image_count": 5,
                    "image_verified": True,
                    "cover_verified": True,
                    "draft_verified": True,
                }]
            }), encoding="utf-8")
            (pack / "distribution-audit.json").write_text(json.dumps({
                "summary": {"total": 1, "complete": 1},
                "items": [{"platform_id": "wechat", "quality": "complete", "issues": []}],
            }), encoding="utf-8")
            payload = DASHBOARD.dashboard_payload(root)
            self.assertTrue(payload["draft_only"])
            self.assertEqual(payload["selected"]["title"], "测试发布包")
            self.assertEqual(payload["selected"]["platforms"][0]["quality"], "complete")
            self.assertEqual(payload["selected"]["insights"]["completion_rate"], 100)
            self.assertEqual(payload["selected"]["quality_gates"]["total"], 11)
            self.assertEqual(payload["selected"]["insights"]["funnel"][-1]["value"], 1)
            self.assertEqual(payload["history"][0]["completion_rate"], 100)
            self.assertIsNone(payload["history"][0]["completion_delta"])
            wechat_row = next(row for row in payload["platform_matrix"]["rows"] if row["platform_id"] == "wechat")
            self.assertEqual(wechat_row["complete_count"], 1)
            self.assertEqual(wechat_row["cells"][0]["quality"], "complete")

    def test_issue_queue_prioritizes_blocked_platforms(self) -> None:
        platforms = [
            {"id": "zhihu", "name": "知乎", "quality": "partial", "raw_status": "text_draft_created_cover_pending", "image_count": 0, "editor_url": ""},
            {"id": "douyin-image", "name": "抖音图文", "quality": "blocked", "raw_status": "blocked_by_previous_draft", "image_count": 0, "editor_url": ""},
            {"id": "wechat", "name": "微信公众号", "quality": "complete", "raw_status": "draft_created", "image_count": 5, "editor_url": ""},
        ]
        with tempfile.TemporaryDirectory() as temp:
            pack = Path(temp)
            (pack / "distribution-audit.json").write_text(json.dumps({
                "summary": {"total": 3, "complete": 1, "partial": 1, "blocked": 1},
                "items": [],
            }), encoding="utf-8")
            insights = DASHBOARD.insight_payload(pack, platforms)
            self.assertEqual([item["platform_id"] for item in insights["issue_queue"]], ["douyin-image", "zhihu"])
            self.assertEqual(insights["issue_queue"][0]["action"], "先处理旧草稿")

    def test_funnel_uses_verified_images_instead_of_uploaded_count(self) -> None:
        platforms = [
            {
                "id": "toutiao", "name": "今日头条", "quality": "unverified",
                "raw_status": "unverified_editor_timeout", "image_count": 5,
                "image_verified": False, "cover_verified": True, "draft_verified": False,
                "verification_recorded": True, "verification_passed": False,
                "verification_missing": ["图片", "草稿"], "editor_url": "",
            },
            {
                "id": "wechat", "name": "微信公众号", "quality": "complete",
                "raw_status": "draft_created", "image_count": 5,
                "image_verified": True, "cover_verified": True, "draft_verified": True,
                "verification_recorded": True, "verification_passed": True,
                "verification_missing": [], "editor_url": "",
            },
        ]
        with tempfile.TemporaryDirectory() as temp:
            pack = Path(temp)
            (pack / "distribution-audit.json").write_text(json.dumps({
                "summary": {"total": 2, "complete": 1, "unverified": 1},
                "items": [],
            }), encoding="utf-8")
            insights = DASHBOARD.insight_payload(pack, platforms)
            funnel = {item["key"]: item["value"] for item in insights["funnel"]}
            self.assertEqual(funnel["images"], 1)
            self.assertEqual(funnel["draft"], 1)
            self.assertEqual(funnel["complete"], 1)
            self.assertEqual(insights["verification"]["recorded"], 2)
            self.assertEqual(insights["issue_queue"][0]["verification_missing"], ["图片", "草稿"])

    def test_unverified_result_without_boolean_fields_requests_verification_record(self) -> None:
        platforms = [{
            "id": "zhihu", "name": "知乎", "quality": "unverified",
            "raw_status": "unverified_editor_timeout", "image_count": 0,
            "verification_recorded": False, "verification_missing": ["图片", "封面", "草稿"],
            "editor_url": "",
        }]
        with tempfile.TemporaryDirectory() as temp:
            pack = Path(temp)
            (pack / "distribution-audit.json").write_text(json.dumps({
                "summary": {"total": 1, "unverified": 1}, "items": [],
            }), encoding="utf-8")
            item = DASHBOARD.insight_payload(pack, platforms)["issue_queue"][0]
            self.assertEqual(item["action"], "补录验收")
            self.assertIn("缺少统一验收记录", item["message"])

    def test_evidence_fields_tracks_freshness_and_reference(self) -> None:
        old_time = (datetime.now().astimezone() - timedelta(hours=25)).isoformat(timespec="seconds")
        evidence = DASHBOARD.evidence_fields({
            "evidence": {
                "kind": "browser_visual",
                "verified_at": old_time,
                "summary": "五张图片、封面与草稿均在编辑器内可见",
                "reference": "work/evidence/toutiao.png",
            }
        })
        self.assertTrue(evidence["recorded"])
        self.assertTrue(evidence["stale"])
        self.assertGreater(evidence["age_minutes"], 1440)
        self.assertEqual(evidence["reference"], "work/evidence/toutiao.png")
        self.assertEqual(evidence["reference_type"], "artifact")
        self.assertEqual(evidence["reliability"], "strong")
        self.assertFalse(evidence["trusted"])

    def test_evidence_without_reference_is_recorded_but_not_trusted(self) -> None:
        evidence = DASHBOARD.evidence_fields({
            "evidence": {
                "kind": "browser_visual",
                "verified_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "summary": "页面中可以看到五图与草稿状态",
                "reference": "",
            }
        })
        self.assertTrue(evidence["recorded"])
        self.assertFalse(evidence["reference_recorded"])
        self.assertEqual(evidence["reliability"], "weak")
        self.assertFalse(evidence["trusted"])

    def test_retry_guidance_separates_retry_manual_and_start(self) -> None:
        self.assertEqual(DASHBOARD.retry_guidance("unverified_editor_timeout", "unverified")["mode"], "retry")
        self.assertEqual(DASHBOARD.retry_guidance("needs_credentials", "blocked")["mode"], "manual")
        self.assertEqual(DASHBOARD.retry_guidance("not_attempted", "not_attempted")["mode"], "start")
        self.assertEqual(DASHBOARD.retry_guidance("draft_created", "complete")["mode"], "none")

    def test_pack_evidence_rate_uses_structured_records_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            pack = Path(temp)
            now = datetime.now().astimezone().isoformat(timespec="seconds")
            (pack / "distribution-plan.json").write_text(json.dumps({
                "items": [{"platform_id": "wechat"}, {"platform_id": "toutiao"}],
            }), encoding="utf-8")
            (pack / "distribution-results.json").write_text(json.dumps({
                "items": [
                    {"platform_id": "wechat", "evidence": {
                        "kind": "api_response", "verified_at": now, "summary": "已返回 media_id",
                    }},
                    {"platform_id": "toutiao", "proof": "只有旧式文字说明"},
                ],
            }), encoding="utf-8")
            self.assertEqual(DASHBOARD.pack_evidence_rate(pack), 50)

    def test_fresh_evidence_rate_excludes_stale_records(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            pack = Path(temp)
            now = datetime.now().astimezone().isoformat(timespec="seconds")
            old = (datetime.now().astimezone() - timedelta(hours=25)).isoformat(timespec="seconds")
            (pack / "distribution-plan.json").write_text(json.dumps({
                "items": [{"platform_id": "wechat"}, {"platform_id": "toutiao"}],
            }), encoding="utf-8")
            (pack / "distribution-results.json").write_text(json.dumps({
                "items": [
                    {"platform_id": "wechat", "evidence": {
                        "kind": "api_response", "verified_at": now, "summary": "返回 media_id", "reference": "a" * 24,
                    }},
                    {"platform_id": "toutiao", "evidence": {
                        "kind": "browser_visual", "verified_at": old, "summary": "编辑器可见", "reference": "https://example.com/editor",
                    }},
                ],
            }), encoding="utf-8")
            self.assertEqual(DASHBOARD.pack_fresh_evidence_rate(pack), 50)

    def test_data_health_surfaces_invalid_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            pack = Path(temp)
            (pack / "distribution-plan.json").write_text(json.dumps({"items": []}), encoding="utf-8")
            (pack / "distribution-results.json").write_text("{broken", encoding="utf-8")
            health = DASHBOARD.pack_data_health(pack)
            self.assertEqual(health["status"], "error")
            self.assertTrue(any("distribution-results.json" in item["message"] for item in health["issues"]))

    def test_audit_report_exports_status_and_draft_only_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            pack = Path(temp) / "2026-07-21-report-huazi"
            pack.mkdir()
            now = datetime.now().astimezone().isoformat(timespec="seconds")
            (pack / "README.md").write_text("# 验收报告测试\n", encoding="utf-8")
            (pack / "distribution-plan.json").write_text(json.dumps({
                "items": [{
                    "platform_id": "wechat", "platform": "微信公众号", "title": "测试标题",
                    "delivery": "official_draft_api", "required_image_count": 5,
                }],
            }, ensure_ascii=False), encoding="utf-8")
            (pack / "distribution-results.json").write_text(json.dumps({
                "items": [{
                    "platform_id": "wechat", "status": "draft_created", "image_count": 5,
                    "image_verified": True, "cover_verified": True, "draft_verified": True,
                    "media_id": "media-123", "evidence": {
                        "kind": "api_response", "verified_at": now,
                        "summary": "官方草稿接口已返回 media_id", "reference": "media-123",
                    },
                }],
            }, ensure_ascii=False), encoding="utf-8")
            (pack / "distribution-audit.json").write_text(json.dumps({
                "summary": {"total": 1, "complete": 1},
                "items": [{"platform_id": "wechat", "quality": "complete", "issues": []}],
            }), encoding="utf-8")
            report = DASHBOARD.audit_report_markdown(pack)
            self.assertIn("微信公众号", report)
            self.assertIn("官方草稿接口已返回 media_id", report)
            self.assertIn("只保存草稿，不直接发布", report)

    def test_history_summary_supports_legacy_draft_status(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            pack = Path(temp)
            (pack / "distribution-plan.json").write_text(json.dumps({
                "items": [{"platform_id": "wechat"}, {"platform_id": "toutiao"}],
            }), encoding="utf-8")
            (pack / "distribution-results.json").write_text(json.dumps({
                "items": [
                    {"platform_id": "wechat", "status": "drafted"},
                    {"platform_id": "toutiao", "status": "blocked_ip_whitelist"},
                ],
            }), encoding="utf-8")
            summary = DASHBOARD.history_summary(pack)
            self.assertEqual(summary["complete"], 1)
            self.assertEqual(summary["blocked"], 1)
            self.assertEqual(DASHBOARD.completion_rate(summary), 50)

    def test_platform_history_marks_missing_platform_as_not_available(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            pack = root / "2026-07-20-test"
            pack.mkdir()
            (pack / "distribution-plan.json").write_text(json.dumps({
                "items": [{"platform_id": "wechat", "platform": "微信公众号"}],
            }), encoding="utf-8")
            (pack / "distribution-results.json").write_text(json.dumps({
                "items": [{"platform_id": "wechat", "status": "draft_created"}],
            }), encoding="utf-8")
            history = [{"name": pack.name, "date": "2026-07-20", "completion_rate": 100, "image_rate": 0, "selected": True}]
            matrix = DASHBOARD.platform_history_matrix(root, history)
            toutiao = next(row for row in matrix["rows"] if row["platform_id"] == "toutiao")
            self.assertEqual(toutiao["cells"][0]["quality"], "not_available")
            self.assertEqual(toutiao["observed_count"], 0)

    def test_quality_gates_require_exact_cover_and_all_platform_copy(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            pack = Path(temp)
            (pack / "fact-check-notes.md").write_text("事实核查" * 20, encoding="utf-8")
            (pack / "prediction.md").write_text("发布前预测" * 20, encoding="utf-8")
            (pack / "wechat-article.md").write_text("公众号正文" * 20, encoding="utf-8")
            (pack / "distribution-plan.json").write_text(json.dumps({
                "items": [{"platform_id": "wechat", "content": str(pack / "wechat-article.md")}],
            }), encoding="utf-8")
            gates = DASHBOARD.quality_gates(pack)
            index = {item["key"]: item for item in gates["items"]}
            self.assertTrue(index["facts"]["passed"])
            self.assertTrue(index["prediction"]["passed"])
            self.assertTrue(index["copies"]["passed"])
            self.assertFalse(index["images"]["passed"])
            self.assertFalse(gates["ready"])

    def test_quality_gates_accept_dated_five_image_names(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            pack = Path(temp) / "2026-07-22-test-huazi"
            pack.mkdir()
            (pack / "images").mkdir()
            for index in range(1, 6):
                width, height = ((900, 383) if index == 1 else (1080, 720))
                (pack / "images" / f"2026-07-22-{index:02d}.png").write_bytes(png_bytes(width, height, index))
            gates = DASHBOARD.quality_gates(pack)
            index = {item["key"]: item for item in gates["items"]}
            self.assertTrue(index["images"]["passed"])
            groups = {item["key"]: item for item in gates["groups"]}
            self.assertEqual(groups["preparation"]["total"], 8)
            self.assertNotIn("video_quality", index)
            self.assertEqual(groups["records"]["total"], 3)

    def test_quality_gates_reject_wrong_body_image_dimensions(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            pack = Path(temp) / "2026-07-22-test-huazi"
            pack.mkdir()
            (pack / "images").mkdir()
            for index in range(1, 6):
                width, height = ((900, 383) if index == 1 else (1080, 720))
                if index == 4:
                    width, height = (1080, 1080)
                (pack / "images" / f"2026-07-22-{index:02d}.png").write_bytes(png_bytes(width, height, index + 10))
            gates = DASHBOARD.quality_gates(pack)
            image_gate = next(item for item in gates["items"] if item["key"] == "images")
            self.assertFalse(image_gate["passed"])
            self.assertEqual(image_gate["detail"], "4 / 5 张尺寸通过")

    def test_data_health_warns_when_audit_predates_results(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            pack = Path(temp)
            (pack / "distribution-plan.json").write_text(
                json.dumps({"schema_version": 2, "items": []}), encoding="utf-8"
            )
            (pack / "distribution-dry-run-results.json").write_text(
                json.dumps({"schema_version": 2, "dry_run": True, "items": []}), encoding="utf-8"
            )
            (pack / "distribution-audit.json").write_text(
                json.dumps({"schema_version": 2, "summary": {}, "items": []}), encoding="utf-8"
            )
            (pack / "distribution-results.json").write_text(json.dumps({"schema_version": 2, "items": []}), encoding="utf-8")
            audit_path = pack / "distribution-audit.json"
            old = datetime.now().timestamp() - 60
            os.utime(audit_path, (old, old))
            health = DASHBOARD.pack_data_health(pack)
            self.assertEqual(health["status"], "warning")
            self.assertTrue(any("结果审计早于" in item["message"] for item in health["issues"]))

    def test_evidence_manifest_includes_integrity_and_platform_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            pack = Path(temp)
            now = datetime.now().astimezone().isoformat(timespec="seconds")
            (pack / "README.md").write_text("# 证据清单测试\n", encoding="utf-8")
            (pack / "distribution-plan.json").write_text(json.dumps({"items": [{
                "platform_id": "wechat", "platform": "微信公众号", "delivery": "official_draft_api",
            }]}), encoding="utf-8")
            (pack / "distribution-results.json").write_text(json.dumps({"schema_version": 2, "items": [{
                "platform_id": "wechat", "status": "draft_created", "image_verified": True,
                "cover_verified": True, "draft_verified": True, "evidence": {
                    "kind": "api_response", "verified_at": now, "summary": "已返回", "reference": "m" * 24,
                },
            }]}), encoding="utf-8")
            manifest = DASHBOARD.evidence_manifest(pack)
            self.assertEqual(manifest["policy"], "draft_only_never_publish")
            self.assertEqual(manifest["platforms"][0]["platform_id"], "wechat")
            self.assertIn("ledger_chain", manifest["data_health"])
            self.assertEqual(len(manifest["manifest_id"]), 64)
            self.assertTrue(all(len(item["sha256"]) == 64 for item in manifest["artifacts"]))

    def test_delivery_flow_reports_real_file_progress(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            pack = Path(temp)
            (pack / "distribution-plan.json").write_text(json.dumps({
                "items": [
                    {"platform_id": "wechat"},
                    {"platform_id": "xiaohongshu"},
                ],
            }), encoding="utf-8")
            (pack / "distribution-dry-run-results.json").write_text(json.dumps({
                "items": [
                    {"platform_id": "wechat", "status": "validated"},
                    {"platform_id": "xiaohongshu", "status": "validated"},
                ],
            }), encoding="utf-8")
            (pack / "social-auto-upload-jobs.json").write_text(json.dumps({
                "jobs": [{"platform_id": "xiaohongshu"}],
            }), encoding="utf-8")
            (pack / "distribution-audit.json").write_text(json.dumps({
                "items": [{"platform_id": "wechat"}, {"platform_id": "xiaohongshu"}],
            }), encoding="utf-8")
            gates = {"ready": False, "passed": 5, "total": 7}
            platforms = [
                {"id": "wechat", "quality": "complete", "draft_verified": True, "verification_passed": True},
                {"id": "xiaohongshu", "quality": "partial", "draft_verified": True, "verification_passed": True},
            ]
            flow = {item["key"]: item for item in DASHBOARD.delivery_flow(pack, gates, platforms)}
            self.assertEqual(flow["quality"]["state"], "attention")
            self.assertEqual(flow["plan"]["state"], "complete")
            self.assertEqual(flow["dry_run"]["state"], "complete")
            self.assertEqual(flow["stage"]["state"], "complete")
            self.assertEqual(flow["draft"]["state"], "complete")
            self.assertEqual(flow["audit"]["state"], "complete")

    def test_channel_coverage_adds_manual_browser_lane(self) -> None:
        adapters = [{
            "id": "wechat-official",
            "name": "微信官方草稿 API",
            "mode": "draft",
            "platforms": ["wechat"],
            "installed": True,
        }]
        platforms = [
            {"id": "wechat", "name": "微信公众号", "quality": "complete"},
            {"id": "weibo", "name": "微博", "quality": "not_attempted"},
        ]
        rows = DASHBOARD.channel_coverage(adapters, platforms)
        self.assertEqual(rows[0]["complete"], 1)
        self.assertEqual(rows[1]["id"], "browser-draft")
        self.assertEqual(rows[1]["platforms"], ["微博"])

    def test_local_origin_policy_blocks_external_mutations(self) -> None:
        self.assertTrue(DASHBOARD.origin_allowed("", "127.0.0.1:8765"))
        self.assertTrue(DASHBOARD.origin_allowed("http://127.0.0.1:8765", "127.0.0.1:8765"))
        self.assertFalse(DASHBOARD.origin_allowed("https://example.com", "127.0.0.1:8765"))
        self.assertFalse(DASHBOARD.origin_allowed("http://localhost:8765", "127.0.0.1:8765"))

    def test_host_policy_blocks_dns_rebinding_hosts(self) -> None:
        self.assertTrue(DASHBOARD.host_allowed("127.0.0.1:8765"))
        self.assertTrue(DASHBOARD.host_allowed("localhost:8765"))
        self.assertTrue(DASHBOARD.host_allowed("[::1]:8765"))
        self.assertFalse(DASHBOARD.host_allowed("example.com:8765"))
        self.assertFalse(DASHBOARD.host_allowed("127.0.0.1.example.com:8765"))

    def test_stale_running_action_is_reconciled_as_interrupted(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            pack = Path(temp)
            (pack / "distribution-action-state.json").write_text(
                json.dumps({"status": "running", "action": "audit"}), encoding="utf-8"
            )
            state = DASHBOARD.effective_action_state(pack, {"active": False, "stale": False})
            self.assertEqual(state["status"], "interrupted")
            self.assertTrue(state["reconciled"])

    def test_canonical_image_validation_rejects_exact_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            pack = Path(temp)
            (pack / "images").mkdir()
            (pack / "images" / "00-cover.png").write_bytes(png_bytes(900, 383, 1))
            duplicate = png_bytes(1080, 720, 2)
            for name in DASHBOARD.CANONICAL_IMAGES[1:]:
                (pack / "images" / name).write_bytes(duplicate)
            validation = DASHBOARD.canonical_image_validation(pack)
            self.assertFalse(validation["ready"])
            self.assertEqual(validation["decoded_count"], 5)
            self.assertEqual(validation["unique_count"], 2)
            self.assertTrue(validation["duplicate_groups"])

    def test_core_document_shape_errors_are_reported(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            pack = Path(temp)
            (pack / "distribution-plan.json").write_text(
                json.dumps({"schema_version": 2, "items": [{"title": "missing platform"}]}),
                encoding="utf-8",
            )
            (pack / "distribution-dry-run-results.json").write_text(
                json.dumps({"schema_version": 2, "dry_run": False, "items": []}), encoding="utf-8"
            )
            health = DASHBOARD.pack_data_health(pack)
            self.assertEqual(health["status"], "error")
            messages = " ".join(item["message"] for item in health["issues"])
            self.assertIn("platform_id", messages)
            self.assertIn("dry_run", messages)

    def test_automation_readiness_requires_every_gate(self) -> None:
        ready = DASHBOARD.automation_readiness(
            {"groups": [{"key": "preparation", "ready": True}]},
            {"status": "healthy"},
            {"active": False},
            {"ready": True},
            True,
        )
        blocked = DASHBOARD.automation_readiness(
            {"groups": [{"key": "preparation", "ready": False}]},
            {"status": "error"},
            {"active": True},
            {"ready": False},
            False,
        )
        self.assertTrue(ready["ready"])
        self.assertFalse(blocked["ready"])
        self.assertEqual(len(blocked["blockers"]), 5)

    def test_safe_output_redacts_common_credentials(self) -> None:
        output = DASHBOARD.safe_output(
            "token=abc access_token:xyz AppSecret=qwerty Authorization=Bearer-secret "
            "Cookie=session API_KEY=123 Bearer visible-token"
        )
        for secret in ("abc", "xyz", "qwerty", "Bearer-secret", "session", "123", "visible-token"):
            self.assertNotIn(secret, output)

    def test_workflow_fingerprints_detect_content_changed_after_dry_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            pack = Path(temp)
            (pack / "images").mkdir()
            for index, name in enumerate(DASHBOARD.CANONICAL_IMAGES, start=1):
                size = (900, 383) if index == 1 else (1080, 720)
                (pack / "images" / name).write_bytes(png_bytes(*size, index + 20))
            content = pack / "wechat-article.md"
            content.write_text("# 指纹健康检查\n\n正文", encoding="utf-8")
            plan = {
                "schema_version": 2,
                "registry_verified_at": "2026-07-22",
                "pack": str(pack),
                "title": "指纹健康检查",
                "draft_only_policy": True,
                "items": [{"platform_id": "wechat", "platform": "微信公众号", "content": str(content)}],
            }
            plan["plan_id"] = DASHBOARD.plan_identity(plan)
            DASHBOARD.atomic_write_json(pack / "distribution-plan.json", plan)
            bundles = DASHBOARD.workflow_bundles(pack, plan)
            dry_run = {
                "schema_version": 2,
                "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "pack": str(pack),
                "title": plan["title"],
                "dry_run": True,
                "inputs": {
                    "plan_id": plan["plan_id"],
                    "plan_file_sha256": DASHBOARD.file_sha256(pack / "distribution-plan.json"),
                    "content_bundle": bundles["content"],
                    "image_bundle": bundles["images"],
                },
                "items": [{"platform_id": "wechat", "status": "validated"}],
            }
            dry_run["result_id"] = DASHBOARD.result_identity(dry_run)
            DASHBOARD.atomic_write_json(pack / "distribution-dry-run-results.json", dry_run)
            results = {
                "schema_version": 2,
                "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "pack": str(pack),
                "title": plan["title"],
                "dry_run": False,
                "items": [{
                    "platform_id": "wechat", "status": "unverified",
                    "image_verified": False, "cover_verified": False, "draft_verified": False,
                    "evidence": {
                        "kind": "browser_state",
                        "verified_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                        "summary": "等待核验",
                        "reference": "https://example.com/editor",
                    },
                }],
            }
            results["result_id"] = DASHBOARD.result_identity(results)
            DASHBOARD.atomic_write_json(pack / "distribution-results.json", results)
            audit = {
                "schema_version": 2,
                "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "pack": str(pack),
                "complete": False,
                "summary": {"total": 1, "complete": 0, "unverified": 1},
                "items": [{"platform_id": "wechat", "quality": "unverified"}],
            }
            audit["audit_id"] = DASHBOARD.audit_identity(audit, plan["plan_id"], results["result_id"])
            audit["inputs"] = {
                "plan_id": plan["plan_id"],
                "plan_file_sha256": DASHBOARD.file_sha256(pack / "distribution-plan.json"),
                "dry_run_id": dry_run["result_id"],
                "dry_run_file_sha256": DASHBOARD.file_sha256(pack / "distribution-dry-run-results.json"),
                "result_id": results["result_id"],
                "results_file_sha256": DASHBOARD.file_sha256(pack / "distribution-results.json"),
            }
            DASHBOARD.atomic_write_json(pack / "distribution-audit.json", audit)
            self.assertEqual(DASHBOARD.pack_data_health(pack)["status"], "healthy")
            content.write_text("# 指纹健康检查\n\n正文已修改", encoding="utf-8")
            health = DASHBOARD.pack_data_health(pack)
            self.assertEqual(health["status"], "warning")
            self.assertIn("provenance.content_bundle_stale", {item["code"] for item in health["issues"]})

    def test_dashboard_revision_changes_with_pack_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            pack = root / "2026-07-22-test-huazi"
            pack.mkdir()
            path = pack / "distribution-plan.json"
            path.write_text("{}", encoding="utf-8")
            first = DASHBOARD.dashboard_revision(root, pack.name)
            path.write_text('{"changed":true}', encoding="utf-8")
            second = DASHBOARD.dashboard_revision(root, pack.name)
            self.assertNotEqual(first, second)

    def test_server_falls_back_when_preferred_port_is_occupied(self) -> None:
        fallback_server = object()
        with mock.patch.object(DASHBOARD, "port_is_listening", side_effect=[True, False]), mock.patch.object(
            DASHBOARD, "ThreadingHTTPServer", return_value=fallback_server
        ) as constructor:
            server = DASHBOARD.create_server("127.0.0.1", 8765, attempts=2)
        self.assertIs(server, fallback_server)
        self.assertEqual(constructor.call_args.args[0], ("127.0.0.1", 8766))


if __name__ == "__main__":
    unittest.main()
