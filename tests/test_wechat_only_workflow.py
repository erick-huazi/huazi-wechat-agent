from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import workflow_policy as policy
import social_draft_router as router
import social_auto_upload_adapter as adapter
import content_quality_gate as quality
import image_quality_gate as images
import research_evidence_gate as research
import content_visual_brief as brief
import huazi_content_ops as ops


class WeChatOnlyWorkflowTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.pack = self.root / '2026-10-20-wechat-test-huazi'
        self.pack.mkdir()

    def plan(self, ids, pack=None):
        path = (pack or self.pack) / 'distribution-plan.json'
        path.write_text(json.dumps({'items': [{'platform_id': pid} for pid in ids]}), encoding='utf-8')

    def test_defaults_and_pause_are_reversible_without_automatic_expiry(self):
        self.assertEqual(policy.ACTIVE_PLATFORMS, ('wechat',))
        self.assertEqual(router.DEFAULT_PLATFORMS, 'wechat')
        self.assertEqual(adapter.DEFAULT_TARGETS, ())
        self.assertEqual(policy.PAUSED_PLATFORMS, {'toutiao', 'zhihu', 'xiaohongshu', 'weibo'})
        self.assertEqual(policy.POLICY['resume_policy'], 'explicit_user_request_only_no_automatic_expiry')

    def test_new_scope_checks_only_wechat_and_storyboard(self):
        self.plan(['wechat'])
        self.assertEqual(quality.quality_scope(self.pack), (['wechat'], False, []))
        self.assertEqual(images.layout_platform_ids(self.pack), ['wechat'])
        self.assertTrue(images.plan_scope_valid(self.pack))
        self.assertEqual(research.required_article_files(self.pack, research.load_standard()),
                         ['wechat-article.md', 'image-storyboard.json'])
        cards, _ = brief.platform_contexts(self.pack, brief.read_object(brief.POLICY))
        self.assertEqual([card['platform'] for card in cards], ['wechat'])

    def test_missing_empty_duplicate_or_paused_plan_is_blocked(self):
        self.assertIn('CONTENT_PLAN_SCOPE_INVALID', quality.quality_scope(self.pack)[2])
        for ids in ([], ['wechat', 'wechat'], ['toutiao'], list(policy.GRAPHIC_PLATFORMS)):
            with self.subTest(ids=ids):
                self.plan(ids)
                self.assertIn('CONTENT_PLAN_SCOPE_INVALID', quality.quality_scope(self.pack)[2])
                self.assertFalse(images.plan_scope_valid(self.pack))
                self.assertEqual(images.layout_platform_ids(self.pack), ['wechat'])

    def test_transition_accepts_exact_old_or_new_scope_not_arbitrary_subset(self):
        pack = self.root / '2026-10-19-transition-huazi'
        pack.mkdir()
        for ids in (['wechat'], list(policy.GRAPHIC_PLATFORMS)):
            self.plan(ids, pack)
            self.assertTrue(images.plan_scope_valid(pack))
            self.assertEqual(images.layout_platform_ids(pack), ids)
        self.plan(['wechat', 'toutiao'], pack)
        self.assertFalse(images.plan_scope_valid(pack))

    def test_old_five_platform_checks_remain_complete_and_read_only(self):
        pack = self.root / '2026-10-18-historical-huazi'
        pack.mkdir()
        self.plan(list(policy.GRAPHIC_PLATFORMS), pack)
        before = (pack / 'distribution-plan.json').read_bytes()
        self.assertTrue(images.plan_scope_valid(pack))
        self.assertEqual(len(quality.quality_scope(pack)[0]), 6)
        self.assertIn('weibo.md', research.required_article_files(pack, research.load_standard()))
        self.assertEqual(before, (pack / 'distribution-plan.json').read_bytes())

    def test_paused_dispatch_and_browser_begin_stop_before_any_writes(self):
        registry = router.load_registry(router.DEFAULT_REGISTRY)
        for pid in policy.PAUSED_PLATFORMS:
            with self.subTest(platform=pid), mock.patch.object(router, 'dispatch_item') as remote:
                args = router.build_parser().parse_args(['push', '--pack', str(self.pack), '--platforms', pid, '--confirm-drafts'])
                with self.assertRaisesRegex(ValueError, 'PLATFORM_DISABLED_BY_WORKFLOW'):
                    router.command_resume_push(args, registry)
                args = router.build_parser().parse_args(['browser-begin', '--pack', str(self.pack), '--platform', pid])
                with self.assertRaisesRegex(ValueError, 'PLATFORM_DISABLED_BY_WORKFLOW'):
                    router.command_browser_begin(args)
                with self.assertRaisesRegex(ValueError, 'PLATFORM_DISABLED_BY_WORKFLOW'):
                    adapter.stage(self.pack, self.root, [pid])
                remote.assert_not_called()
        self.assertEqual(list(self.pack.iterdir()), [])

    def test_skeleton_creates_no_paused_copy_or_results(self):
        args = ops.build_parser().parse_args(['pack', '--out', str(self.pack), '--root', str(self.root / 'ops'),
            '--title', 'Synthetic current-scope test', *[arg for key in ops.WEIGHTS for arg in ('--score', f'{key}=3')]])
        with contextlib.redirect_stdout(io.StringIO()):
            ops.cmd_pack(args)
        self.assertTrue((self.pack / 'wechat-article.md').is_file())
        self.assertTrue((self.pack / 'image-storyboard.json').is_file())
        for pid in policy.PAUSED_PLATFORMS:
            self.assertFalse((self.pack / policy.ARTICLE_FILES[pid]).exists())
            self.assertFalse((self.pack / (pid + '-publish-result.md')).exists())
        self.assertFalse((self.pack / 'weibo.md').exists())
        self.assertFalse((self.pack / 'video').exists())

    def test_existing_results_forbid_reducing_historical_plan(self):
        self.plan(list(policy.GRAPHIC_PLATFORMS))
        (self.pack / 'distribution-results.json').write_text('{"historical":true}')
        before = {p.name:p.read_bytes() for p in self.pack.iterdir()}
        with self.assertRaisesRegex(ValueError, 'PLAN_SCOPE_CHANGE_REQUIRES_RECONCILIATION'):
            router.protect_plan_scope(self.pack, [{'id':'wechat'}])
        self.assertEqual(before, {p.name:p.read_bytes() for p in self.pack.iterdir()})


if __name__ == '__main__':
    unittest.main()
