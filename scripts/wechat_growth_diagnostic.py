#!/usr/bin/env python3
"""Inspect manually supplied aggregate WeChat article metrics without side effects."""
from __future__ import annotations

import argparse
import json
import statistics
from datetime import datetime, timedelta
from pathlib import Path


def timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError('TIMESTAMP_REQUIRED')
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError as exc:
        raise ValueError('TIMESTAMP_INVALID') from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError('TIMEZONE_REQUIRED')
    return result


def count(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError('COUNT_INVALID')
    return value


def validate_record(record: object, as_of: datetime) -> dict:
    if not isinstance(record, dict):
        raise ValueError('RECORD_INVALID')
    if not isinstance(record.get('id'), str) or not record['id'].strip():
        raise ValueError('ID_REQUIRED')
    published = timestamp(record.get('published_at'))
    observed = timestamp(record.get('observed_at'))
    window = record.get('window_hours')
    if isinstance(window, bool) or not isinstance(window, int) or not 1 <= window <= 720:
        raise ValueError('WINDOW_INVALID')
    if observed > as_of or published > as_of:
        raise ValueError('FUTURE_TIMESTAMP')
    if observed < published + timedelta(hours=window):
        raise ValueError('WINDOW_INCOMPLETE')
    if record.get('window_confirmed') is not True:
        raise ValueError('WINDOW_NOT_CONFIRMED')
    if not isinstance(record.get('metric'), str) or not record['metric'].strip():
        raise ValueError('METRIC_REQUIRED')
    if not isinstance(record.get('source'), str) or not record['source'].strip():
        raise ValueError('SOURCE_REQUIRED')
    if not isinstance(record.get('cohort'), str) or not record['cohort'].strip():
        raise ValueError('COHORT_REQUIRED')
    return {'id': record['id'], 'published': published, 'observed': observed,
            'window': window, 'metric': record['metric'].strip(),
            'source': record['source'].strip(), 'cohort': record['cohort'].strip(),
            'reads': count(record.get('reads'))}


def diagnose(payload: dict) -> dict:
    if not isinstance(payload, dict) or payload.get('schema_version') != 1:
        raise ValueError('SCHEMA_INVALID')
    as_of = timestamp(payload.get('as_of'))
    target = validate_record(payload.get('target'), as_of)
    raw = payload.get('history')
    if not isinstance(raw, list):
        raise ValueError('HISTORY_INVALID')
    history = [validate_record(item, as_of) for item in raw]
    ids = [target['id'], *(r['id'] for r in history)]
    if len(ids) != len(set(ids)):
        raise ValueError('DUPLICATE_ID')
    matching = [r for r in history if r['published'] < target['published']
                and (r['window'], r['metric'], r['source'], r['cohort']) ==
                (target['window'], target['metric'], target['source'], target['cohort'])]
    matching.sort(key=lambda r: r['published'], reverse=True)
    recent = matching[:10]
    history_complete = payload.get('history_complete') is True
    median = statistics.median(r['reads'] for r in recent) if len(recent) == 10 and history_complete else None
    ratio = target['reads'] / median if median is not None and median > 0 else None
    issues = []
    if len(recent) < 10:
        issues.append('BASELINE_INSUFFICIENT')
    if not history_complete:
        issues.append('HISTORY_COMPLETENESS_UNKNOWN')
    if median == 0:
        issues.append('ZERO_BASELINE_RATIO_UNDEFINED')
    excluded = len(history) - len(matching)
    if excluded:
        issues.append('NONCOMPARABLE_HISTORY_EXCLUDED')
    return {
        'schema_version': 1, 'authority': 'manual_aggregate_observation_only',
        'target_reads': target['reads'], 'metric': target['metric'],
        'window_hours': target['window'], 'comparable_history_count': len(matching),
        'baseline_count': len(recent), 'baseline_median': median,
        'descriptive_multiple': ratio,
        'three_x_performance_marker': ratio >= 3 if ratio is not None else None,
        'experiment_result': 'inconclusive', 'issues': issues,
        'next_checks': [
            '核对曝光、阅读人数或阅读次数是否属于同一平台、同一窗口和同一人群；缺失就保留未知。',
            '核对曝光入口、标题与封面、发布时间和选题相关性；低阅读数不能单独定位原因。',
            '先记录一个可观察问题和下一篇唯一变量；跨篇数据不是随机分流，不能归因。',
        ],
        'limitations': [
            '数据由用户提供，工具不连接后台，不验证来源或采集数据。',
            '不计算曝光点击率或完读率：计数定义、人群和分母未获核实。',
            '3倍仅为既有描述性表现标记，不证明实验胜利或因果。',
            '若后台无法提供精确观察窗口，不能把当前累计数填作指定窗口数。',
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', required=True, help='Local aggregate JSON; never account credentials')
    args = parser.parse_args()
    try:
        payload = json.loads(Path(args.input).read_text(encoding='utf-8'))
        result = diagnose(payload)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise SystemExit(f'Invalid input: {exc}') from None
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
