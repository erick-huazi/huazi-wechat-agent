from __future__ import annotations

import importlib.util
import copy
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "content_quality_gate", ROOT / "scripts" / "content_quality_gate.py"
)
QUALITY = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(QUALITY)
from workflow_policy import GRAPHIC_PLATFORMS


def long_copy(title: str, seed: str, minimum: int) -> str:
    opening = "最怕的不是 AI 不聪明，而是团队把一段没有交付标准的活直接丢给它。"
    scene = f"当你每天处理{seed}时，输入、权限和结果格式只要缺一项，流程就会卡在返工。"
    risk = "风险边界要提前写清：限定权限，敏感动作人工确认，关键结果再次复核。"
    paragraph = f"{seed}要先从具体场景开始，再把输入、步骤、停止条件和验收结果逐项写清。"
    blocks = [opening, scene, risk]
    while sum(len(item) for item in blocks) < minimum:
        blocks.append(paragraph)
    return f"# {title}\n\n" + "\n\n".join(blocks) + "\n"


def write_valid_pack(pack: Path) -> None:
    wechat = long_copy("AI同事不是聊天框，而是可验收岗位", "客户资料整理", 1650)
    wechat += "\n## 场景\n\n## 结构\n\n## 流程\n\n1. 写输入\n2. 写步骤\n3. 写验收\n\n## 边界\n\n"
    wechat += "\n".join(f"![图{index}](images/2026-08-22-{index:02d}.png)" for index in range(1, 6))
    wechat += "\n\n今天先画出输入、步骤和验收三栏，再决定是否交给 AI。\n"
    (pack / "wechat-article.md").write_text(wechat, encoding="utf-8")
    (pack / "toutiao-article.md").write_text(
        long_copy("AI干活最怕少了验收这一步", "门店报表", 520), encoding="utf-8"
    )
    (pack / "zhihu-article.md").write_text(
        long_copy("为什么AI演示很聪明，落地却总返工", "研究资料", 650), encoding="utf-8"
    )
    weibo_article = (
        "# AI自动化真正的门槛是交付标准\n\n"
        "真正拖慢自动化的，不是模型速度，而是任务到了最后没人知道什么叫完成。\n\n"
        "当你每天协调活动执行时，策划、物料、审批和结果分散在不同位置，任何一处遗漏都会造成返工。\n\n"
        "微博长文更适合先给判断，再用一条事件链解释：需求进入后要有负责人，执行中要有状态，结束时要有可检查的文件和页面记录。\n\n"
        "风险边界要同时出现。权限只开放到必要范围，敏感动作人工确认，关键结果再次复核，异常时立即停止。\n\n"
        + "活动协作不是把同一句提示词重复交给模型，而是把责任人、截止时间、交付格式和异常处理写进任务单。\n\n" * 8
    )
    (pack / "weibo-article.md").write_text(weibo_article, encoding="utf-8")
    (pack / "xiaohongshu.md").write_text(
        "# AI干活前先写这3项\n\n别先问工具，先写岗位卡。\n\n1. 输入是什么\n2. 步骤怎么走\n3. 结果如何验收\n\n"
        + "\n\n".join(["把权限、停止条件和人工确认一起写清，流程才不会在最后返工。"] * 8),
        encoding="utf-8",
    )
    (pack / "weibo.md").write_text(
        "AI 自动化最怕的不是模型不聪明，而是没有验收标准。先写输入、步骤和结果，再开放权限；敏感动作保留人工确认，最后只按文件、页面状态和操作记录验收，异常时立即停下。",
        encoding="utf-8",
    )
    for filename, title, body in [
        ("douyin-video.md", "AI干活前先写验收", "别先给权限。输入、步骤和结果没有写清，Agent 只会更快返工。" * 2),
        ("video-channel.md", "AI同事先写岗位卡", "先把输入、步骤和结果写成岗位卡，再决定开放哪些操作权限。" * 2),
        ("kuaishou-video.md", "自动化别急着开权限", "真正该先做的是写清交付物、停止条件和人工确认。" * 2),
    ]:
        (pack / filename).write_text(f"# {title}\n\n{body}\n", encoding="utf-8")
    narration = (
        "最怕的不是 AI 不聪明，而是它说完成了，你却找不到交付物。"
        "先把输入、步骤和结果写成岗位卡。输入写清资料从哪里来，步骤写清哪些动作可以做，"
        "结果写清文件、页面状态和记录如何验收。再补上权限边界、停止条件和人工确认。"
        "这样交出去的不是一个问题，而是一段能被检查的活。执行时只开放必要站点，敏感动作停下来找人，"
        "交付后再核对文件名称、页面状态和操作记录。今天先选一项重复工作，把这三栏写出来。"
    )
    scenes = "\n".join(f"{index}. 镜头 {index}" for index in range(1, 7))
    (pack / "visual-video-pack.md").write_text(
        f"# 视频脚本\n\n## 旁白\n\n{narration}\n\n## 镜头\n\n{scenes}\n",
        encoding="utf-8",
    )


class ContentQualityGateTests(unittest.TestCase):
    def test_new_policy_blocks_repeated_filler(self):
        with tempfile.TemporaryDirectory() as temp:
            pack = Path(temp) / "2026-10-05-quality-huazi"
            pack.mkdir()
            write_valid_pack(pack)
            report = QUALITY.build_report(pack)
            self.assertFalse(report["passed"])
            self.assertTrue(any("CONTENT_REPEATED_PARAGRAPHS" in item["blocking"] for item in report["items"]))

    def test_new_policy_allows_fact_led_opening_without_forced_tension(self):
        with tempfile.TemporaryDirectory() as temp:
            pack = Path(temp) / "2026-10-05-quality-huazi"
            pack.mkdir()
            path = pack / "weibo-article.md"
            path.write_text("# A factual announcement\n\nThe provider released a new model and documented its available regions.\n\n", encoding="utf-8")
            policy = QUALITY.load_standard()
            item = QUALITY.platform_item(pack, "weibo_article", policy["platforms"]["weibo_article"], policy)
            self.assertNotIn("opening_has_no_reader_tension", item["blocking"])
            self.assertIn("opening_review_news_relevance", item["warnings"])

    def test_new_report_policy_changes_make_report_stale(self):
        with tempfile.TemporaryDirectory() as temp:
            pack = Path(temp) / "2026-10-05-quality-huazi"
            pack.mkdir()
            write_valid_pack(pack)
            report = QUALITY.build_report(pack)
            self.assertTrue(QUALITY.report_is_current(report))
            report["standard_sha256"] = "invalid"
            self.assertFalse(QUALITY.report_is_current(report))

    def test_valid_pack_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            pack = Path(temp) / "2026-08-22-quality-huazi"
            pack.mkdir()
            write_valid_pack(pack)
            report = QUALITY.build_report(pack)
            self.assertTrue(report["passed"], report)
            self.assertGreaterEqual(report["pack_score"], 85)

    def test_soft_opener_is_visible_as_warning(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            pack = Path(temp) / "2026-08-22-quality-huazi"
            pack.mkdir()
            write_valid_pack(pack)
            path = pack / "wechat-article.md"
            text = path.read_text(encoding="utf-8").replace(
                "最怕的不是 AI 不聪明", "很多人看到这里，最怕的不是 AI 不聪明", 1
            )
            path.write_text(text, encoding="utf-8")
            report = QUALITY.build_report(pack)
            wechat = next(item for item in report["items"] if item["platform_id"] == "wechat")
            self.assertIn("soft_opener:很多人看到这里", wechat["warnings"])

    def test_weak_weibo_opening_blocks_report(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            pack = Path(temp) / "2026-08-22-quality-huazi"
            pack.mkdir()
            write_valid_pack(pack)
            path = pack / "weibo-article.md"
            text = path.read_text(encoding="utf-8")
            text = text.replace(
                "真正拖慢自动化的，不是模型速度，而是任务到了最后没人知道什么叫完成。",
                "某公司发布了三项新的产品功能，并公布了对应的技术文档。公告介绍了接口名称、可用地区、支持版本和基础参数。技术文档进一步列出了调用方式、返回字段、兼容环境以及常见示例。这些信息构成了本次更新的主要内容。",
                1,
            )
            path.write_text(text, encoding="utf-8")
            report = QUALITY.build_report(pack)
            self.assertFalse(report["passed"])
            item = next(item for item in report["items"] if item["platform_id"] == "weibo_article")
            self.assertIn("opening_has_no_reader_tension", item["blocking"])

    def test_report_becomes_stale_after_copy_change(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            pack = Path(temp) / "2026-08-22-quality-huazi"
            pack.mkdir()
            write_valid_pack(pack)
            report = QUALITY.build_report(pack)
            self.assertTrue(QUALITY.report_is_current(report))
            with (pack / "weibo.md").open("a", encoding="utf-8") as handle:
                handle.write("新增一句。")
            self.assertFalse(QUALITY.report_is_current(report))


class ContentInputBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.pack = Path(self.temp.name) / "2026-10-07-content-input-huazi"
        self.pack.mkdir()
        write_valid_pack(self.pack)
        self.plan = self.pack / "distribution-plan.json"
        self.set_plan([{"platform_id": pid} for pid in GRAPHIC_PLATFORMS])

    def set_plan(self, items):
        self.plan.write_text(json.dumps({"items": items}), encoding="utf-8")

    def assert_blocked(self, report, code):
        self.assertFalse(report["passed"])
        self.assertIn(code, {issue["code"] for issue in report["issues"]})

    def test_new_subset_plan_cannot_skip_other_copy_or_require_video(self):
        self.set_plan([{"platform_id": "xiaohongshu"}])
        report = QUALITY.build_report(self.pack)
        self.assert_blocked(report, "CONTENT_PLAN_SCOPE_INVALID")
        self.assertEqual(set(report["platform_ids"]), {"wechat", "toutiao", "zhihu", "xiaohongshu", "weibo_article", "weibo"})
        self.assertFalse(report["video_script"]["required"])

    def test_new_missing_or_malformed_plan_still_checks_full_copy_set(self):
        for value in (None, "{", "[]", '{"items":[{"platform_id":["wechat"]}]}'):
            with self.subTest(value=value):
                if value is None:
                    self.plan.unlink()
                else:
                    self.plan.write_text(value, encoding="utf-8")
                report = QUALITY.build_report(self.pack)
                self.assert_blocked(report, "CONTENT_PLAN_SCOPE_INVALID")
                self.assertEqual(len(report["items"]), 6)

    def test_disabled_or_duplicate_platforms_do_not_reactivate_video_reads(self):
        original = QUALITY.local_bytes
        seen = []
        def track(pack, name):
            seen.append(name)
            return original(pack, name)
        for extra in ("douyin-video", "wechat", "unknown"):
            self.set_plan([{"platform_id": pid} for pid in (*GRAPHIC_PLATFORMS, extra)])
            with mock.patch.object(QUALITY, "local_bytes", side_effect=track):
                report = QUALITY.build_report(self.pack)
            self.assert_blocked(report, "CONTENT_PLAN_SCOPE_INVALID")
        self.assertFalse(any("video" in name for name in seen))

    def test_legacy_plan_scope_is_preserved(self):
        legacy = Path(self.temp.name) / "2026-09-28-historical-huazi"
        legacy.mkdir()
        write_valid_pack(legacy)
        (legacy / "distribution-plan.json").write_text('{"items":[{"platform_id":"douyin-video"}]}', encoding="utf-8")
        report = QUALITY.build_report(legacy)
        self.assertEqual(report["platform_ids"], ["douyin_video"])
        self.assertIn("source", report["video_script"])
        self.assertFalse(report["issues"])

    def test_unsafe_spec_is_rejected_before_reading_outside_file(self):
        outside = Path(self.temp.name) / "outside.md"
        outside.write_text("SYNTHETIC_EXTERNAL_CONTENT_MUST_NOT_APPEAR", encoding="utf-8")
        policy = copy.deepcopy(QUALITY.load_standard())
        policy["platforms"]["wechat"]["file"] = "../outside.md"
        original = Path.read_bytes
        def guarded(path):
            if path.resolve() == outside.resolve():
                raise AssertionError("outside file was read")
            return original(path)
        with mock.patch.object(Path, "read_bytes", autospec=True, side_effect=guarded):
            report = QUALITY.build_report(self.pack, policy)
        self.assert_blocked(report, "CONTENT_INPUT_OUTSIDE_PACK")
        self.assertNotIn("SYNTHETIC_EXTERNAL_CONTENT_MUST_NOT_APPEAR", json.dumps(report))

    def test_plan_or_copy_resolving_outside_pack_is_not_read(self):
        original_resolve = Path.resolve
        original_read = Path.read_bytes
        outside = Path(self.temp.name) / "synthetic-outside.md"
        for name in ("distribution-plan.json", "wechat-article.md"):
            target = self.pack / name
            def resolve(path, *args, **kwargs):
                return outside if path == target else original_resolve(path, *args, **kwargs)
            def read(path):
                if path == outside or path == target:
                    raise AssertionError("unsafe resolved input was read")
                return original_read(path)
            with mock.patch.object(Path, "resolve", autospec=True, side_effect=resolve), \
                    mock.patch.object(Path, "read_bytes", autospec=True, side_effect=read):
                report = QUALITY.build_report(self.pack)
            self.assert_blocked(report, "CONTENT_INPUT_OUTSIDE_PACK")

    def test_invalid_utf8_and_directory_input_fail_with_stable_code(self):
        path = self.pack / "wechat-article.md"
        path.write_bytes(b"\xff\xfe")
        self.assert_blocked(QUALITY.build_report(self.pack), "CONTENT_INPUT_UNREADABLE")
        path.unlink()
        path.mkdir()
        self.assert_blocked(QUALITY.build_report(self.pack), "CONTENT_INPUT_UNREADABLE")

    def test_same_size_timestamp_edit_during_check_cannot_leave_passing_metrics(self):
        path = self.pack / "wechat-article.md"
        before_stat = path.stat()
        original = QUALITY.platform_item
        def change_after_read(pack, platform, spec, policy, **kwargs):
            item = original(pack, platform, spec, policy, **kwargs)
            if platform == "wechat":
                raw = path.read_bytes()
                path.write_bytes(raw.replace("最怕".encode(), "真怕".encode(), 1))
                os.utime(path, ns=(before_stat.st_atime_ns, before_stat.st_mtime_ns))
            return item
        with mock.patch.object(QUALITY, "platform_item", side_effect=change_after_read):
            report = QUALITY.build_report(self.pack)
        self.assert_blocked(report, "CONTENT_INPUT_CHANGED_DURING_CHECK")
        self.assertEqual(report["pack_score"], 0)
        self.assertEqual(report["items"], [])
        self.assertEqual(path.stat().st_size, before_stat.st_size)
        self.assertEqual(path.stat().st_mtime_ns, before_stat.st_mtime_ns)

    def test_analyzed_text_hash_must_match_the_inspection_snapshot(self):
        original = QUALITY.local_text
        def transient_text(pack, name):
            text, digest = original(pack, name)
            if Path(name).name == "wechat-article.md":
                text += "temporary synthetic edit"
                digest = hashlib.sha256(text.encode()).hexdigest()
            return text, digest
        with mock.patch.object(QUALITY, "local_text", side_effect=transient_text):
            report = QUALITY.build_report(self.pack)
        self.assert_blocked(report, "CONTENT_INPUT_CHANGED_DURING_CHECK")

    def test_source_hash_is_for_the_same_bytes_that_were_analyzed(self):
        policy = QUALITY.load_standard()
        with mock.patch.object(QUALITY, "file_sha256", side_effect=AssertionError("must not reread to hash")):
            row = QUALITY.platform_item(self.pack, "wechat", policy["platforms"]["wechat"], policy)
        self.assertEqual(row["source_sha256"], hashlib.sha256((self.pack / "wechat-article.md").read_bytes()).hexdigest())

    def test_scores_and_similarity_decode_each_copy_only_once(self):
        with mock.patch.object(QUALITY, "local_text", wraps=QUALITY.local_text) as decode:
            report = QUALITY.build_report(self.pack)
        self.assertEqual(decode.call_count, 6)
        self.assertEqual(len(report["platform_similarity"]), 6)
        self.assertEqual(len({call.args[1] for call in decode.call_args_list}), 6)

    def test_plan_change_invalidates_new_saved_report_even_with_same_platforms(self):
        report = QUALITY.build_report(self.pack)
        self.assertTrue(QUALITY.report_is_current(report))
        value = json.loads(self.plan.read_text(encoding="utf-8"))
        value["title"] = "Changed plan title"
        self.plan.write_text(json.dumps(value), encoding="utf-8")
        self.assertFalse(QUALITY.report_is_current(report))


if __name__ == "__main__":
    unittest.main()
