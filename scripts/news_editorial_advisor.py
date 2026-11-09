#!/usr/bin/env python3
"""Read-only editorial triage; signals and reader value still require human review."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from urllib.parse import urlparse

DEMAND_KINDS = {"search_trend", "community_discussion", "github_trending"}
WEAK_KINDS = {"trusted_media", "product_status"}


def host(url: object) -> str:
    if not isinstance(url, str):
        return ""
    try:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            return ""
        return parsed.hostname.lower().removeprefix("www.")
    except ValueError:
        return ""


def related(a: str, b: str) -> bool:
    return bool(a and b and (a == b or a.endswith("." + b) or b.endswith("." + a)))


def diagnose(candidate: dict) -> dict:
    sources = candidate.get("source_urls", [])
    sources = [sources] if isinstance(sources, str) else sources
    sources = sources if isinstance(sources, list) else []
    primary_hosts = [host(u) for u in sources if host(u)]
    demand, weak, same_source, incomplete = [], [], [], []
    signals = candidate.get("heat_signals", [])
    for signal in signals if isinstance(signals, list) else []:
        if not isinstance(signal, dict):
            incomplete.append("invalid_signal")
            continue
        kind, ref = signal.get("kind"), signal.get("reference")
        if kind not in DEMAND_KINDS | WEAK_KINDS:
            continue
        domain = host(ref)
        if not domain or not str(signal.get("summary") or "").strip():
            incomplete.append(str(kind))
        elif any(related(domain, p) for p in primary_hosts):
            same_source.append(str(kind))
        elif kind in DEMAND_KINDS:
            demand.append({"kind": kind, "reference": ref, "summary": signal["summary"]})
        else:
            weak.append({"kind": kind, "reference": ref, "summary": signal["summary"]})
    notes = []
    if not demand:
        notes.append("NEWS_DEMAND_NOT_EVIDENCED")
    if weak:
        notes.append("COVERAGE_NOT_DEMAND")
    if same_source:
        notes.append("SAME_SOURCE_SIGNAL")
    if incomplete:
        notes.append("SIGNAL_INCOMPLETE")
    if candidate.get("topic_type") == "open_source_release":
        notes.append("TOOL_DETAIL_REQUIRES_BROAD_RELEVANCE_REVIEW")
    return {
        "candidate_id": candidate.get("candidate_id"), "title": candidate.get("title"),
        "reader_demand_references": demand, "coverage_references": weak,
        "same_source_kinds": same_source, "incomplete_signal_kinds": incomplete,
        "issues": notes,
        "manual_checks": [
            "原始来源的变化、时间和实际开放范围是否准确？",
            "独立链接是否真实显示本次事件的讨论或搜索需求，而非仅收录？",
            "读者为什么现在需要知道，是否能说明具体人群和任务？",
            "版本更新是主要新闻还是次要例子？是否能找到更有公共意义的角度？",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, help="Existing topic-candidates.json")
    args = parser.parse_args()
    data = json.loads(Path(args.input).read_text(encoding="utf-8"))
    candidates = data.get("candidates")
    if not isinstance(candidates, list) or not candidates or any(not isinstance(c, dict) for c in candidates):
        raise SystemExit("Invalid candidate list")
    print(json.dumps({
        "schema_version": 1,
        "authority": "editorial_advice_only",
        "limitations": [
            "Kinds and links are author declarations, not verified demand or popularity.",
            "Different hostnames do not prove independent sources.",
            "Does not change scores, rank, eligibility, source evidence or approval.",
            "Does not estimate retention, predict traffic, or perform platform actions.",
        ],
        "candidates": [diagnose(c) for c in candidates],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
