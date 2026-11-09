import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from wechat_growth_diagnostic import diagnose


def record(i, reads=10):
    return {'id': str(i), 'published_at': f'2026-10-{i:02}T08:00:00+08:00',
            'observed_at': '2026-11-06T08:00:00+08:00', 'window_hours': 72,
            'window_confirmed': True, 'metric': '阅读人数', 'source': '后台汇总', 'cohort': '同一账号与内容范围', 'reads': reads}


def sample():
    return {'schema_version': 1, 'as_of': '2026-11-07T08:00:00+08:00', 'history_complete': True,
            'target': record(22, 30), 'history': [record(i, i) for i in range(1, 12)]}


class GrowthDiagnosticTests(unittest.TestCase):
    def test_recent_ten_and_observational_only(self):
        result = diagnose(sample())
        self.assertEqual(result['baseline_count'], 10)
        self.assertEqual(result['baseline_median'], 6.5)
        self.assertTrue(result['three_x_performance_marker'])
        self.assertEqual(result['experiment_result'], 'inconclusive')

    def test_short_baseline_is_unknown_not_zero(self):
        data = sample()
        data['history'] = data['history'][:9]
        result = diagnose(data)
        self.assertIsNone(result['baseline_median'])
        self.assertIsNone(result['three_x_performance_marker'])
        self.assertIn('BASELINE_INSUFFICIENT', result['issues'])

    def test_zero_baseline_no_division(self):
        data = sample()
        for row in data['history']:
            row['reads'] = 0
        result = diagnose(data)
        self.assertIsNone(result['descriptive_multiple'])
        self.assertIn('ZERO_BASELINE_RATIO_UNDEFINED', result['issues'])

    def test_incomplete_history_cannot_claim_recent_ten(self):
        data = sample()
        data['history_complete'] = False
        result = diagnose(data)
        self.assertIsNone(result['baseline_median'])
        self.assertIn('HISTORY_COMPLETENESS_UNKNOWN', result['issues'])

    def test_incompatible_window_metric_source_and_future_article_excluded(self):
        data = sample()
        data['history'][0]['window_hours'] = 48
        data['history'][1]['metric'] = '阅读次数'
        data['history'][2]['source'] = '其他账号'
        data['history'][3]['published_at'] = '2026-10-23T08:00:00+08:00'
        result = diagnose(data)
        self.assertEqual(result['comparable_history_count'], 7)
        self.assertIn('NONCOMPARABLE_HISTORY_EXCLUDED', result['issues'])

    def test_invalid_or_unfinished_windows_block(self):
        for change in ({'window_confirmed': False}, {'window_hours': 0},
                       {'observed_at': '2026-10-23T08:00:00+08:00'},
                       {'observed_at': '2026-11-08T08:00:00+08:00'},
                       {'published_at': '2026-10-22T08:00:00'}):
            data = sample()
            data['target'].update(change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                diagnose(data)

    def test_invalid_counts_and_duplicate_ids_block(self):
        for value in (-1, True, 2.5, None):
            data = sample()
            data['target']['reads'] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                diagnose(data)
        data = sample()
        data['history'][0]['id'] = data['target']['id']
        with self.assertRaisesRegex(ValueError, 'DUPLICATE_ID'):
            diagnose(data)

    def test_no_personal_ids_or_source_echo_in_output(self):
        data = sample()
        data['target']['id'] = 'private_identifier'
        data['target']['source'] = 'private_source'
        self.assertNotIn('private_identifier', str(diagnose(copy.deepcopy(data))))
        self.assertNotIn('private_source', str(diagnose(copy.deepcopy(data))))


if __name__ == '__main__':
    unittest.main()
