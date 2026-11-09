#!/usr/bin/env python3
"""Append topic candidates for Huazi social agent."""

from __future__ import annotations

import argparse
import hashlib
import json
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path


def now_date() -> str:
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")


def candidate_id(source: str, title: str, url: str = "") -> str:
    normalized_title = title.strip().lower().replace(" ", "")
    url_path = url.split("?")[0].rstrip("/") if url else ""
    raw = f"{source.split(':')[0]}|{normalized_title}|{url_path}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def append_candidate(out: Path, title: str, source: str, summary: str, url: str = "", tier: str = "tier3") -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    old = out.read_text(encoding="utf-8") if out.exists() else "# 华子社长候选选题池\n\n## 候选项\n"
    cid = candidate_id(source, title, url)
    if cid in old:
        print(f"skip duplicate: {title}")
        return
    entry = f"""
### [{tier}] {title}

- id: {cid}
- source: {source}
- url: {url}
- snapshot_at: {now_date()}
- topic_score:
- content_score:
- status: idea
- prediction_file:
- retro_file:
- note: {summary}

> {summary}
"""
    out.write_text(old.rstrip() + "\n" + entry + "\n", encoding="utf-8")
    print(f"added: {title}")


def cmd_manual(args: argparse.Namespace) -> None:
    append_candidate(Path(args.out), args.title, args.source, args.summary, args.url, args.tier)


def cmd_github(args: argparse.Namespace) -> None:
    query = urllib.parse.urlencode({
        "q": args.query,
        "sort": args.sort,
        "order": "desc",
        "per_page": args.limit,
    })
    url = f"https://api.github.com/search/repositories?{query}"
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "huazi-social-agent",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    for item in data.get("items", [])[: args.limit]:
        title = f"GitHub 项目：{item.get('full_name')}"
        desc = item.get("description") or "暂无描述"
        stars = item.get("stargazers_count", 0)
        html = item.get("html_url", "")
        summary = f"{desc}（stars: {stars}）。可评估是否适合做 AI 工具/Agent/Skill 拆解。"
        append_candidate(Path(args.out), title, f"github:{args.query}", summary, html, "tier3")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Huazi topic source collector")
    sub = parser.add_subparsers(dest="cmd", required=True)

    manual = sub.add_parser("manual")
    manual.add_argument("--title", required=True)
    manual.add_argument("--source", default="manual")
    manual.add_argument("--summary", required=True)
    manual.add_argument("--url", default="")
    manual.add_argument("--tier", default="tier3")
    manual.add_argument("--out", default="outputs/huazi-social-agent/candidates.md")
    manual.set_defaults(func=cmd_manual)

    github = sub.add_parser("github")
    github.add_argument("--query", required=True)
    github.add_argument("--sort", default="updated")
    github.add_argument("--limit", type=int, default=5)
    github.add_argument("--out", default="outputs/huazi-social-agent/candidates.md")
    github.set_defaults(func=cmd_github)

    return parser


def main() -> None:
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
