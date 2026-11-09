#!/usr/bin/env python3
"""Explainable, deterministic compliance checks for social-media copy."""

from __future__ import annotations

import argparse
import json
import re
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_RULES = PROJECT_ROOT / "config" / "content-compliance-rules.json"
ZERO_WIDTH = re.compile(r"[\u200b-\u200f\u2060\ufeff]")
MARKDOWN_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
MARKDOWN_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
FENCED_BLOCK = re.compile(r"```.*?```", flags=re.S)


def load_rules(path: Path = DEFAULT_RULES) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data.get("rules"), list):
        raise ValueError("content compliance config must contain a rules list")
    return data


def normalize_line(line: str) -> str:
    line = unicodedata.normalize("NFKC", line)
    line = ZERO_WIDTH.sub("", line)
    line = MARKDOWN_IMAGE.sub("", line)
    line = MARKDOWN_LINK.sub(r"\1", line)
    return line.strip()


def scan_text(
    text: str,
    *,
    source: str = "",
    platform_id: str = "",
    rules: dict[str, Any] | None = None,
) -> dict[str, Any]:
    policy = rules or load_rules()
    scrubbed = FENCED_BLOCK.sub("", text)
    findings: list[dict[str, Any]] = []
    for line_number, raw_line in enumerate(scrubbed.splitlines(), start=1):
        line = normalize_line(raw_line)
        if not line:
            continue
        for rule in policy["rules"]:
            match = re.search(str(rule["pattern"]), line, flags=re.I)
            if not match:
                continue
            start = max(match.start() - 18, 0)
            end = min(match.end() + 18, len(line))
            findings.append({
                "rule_id": str(rule["id"]),
                "category": str(rule.get("category", "内容合规")),
                "severity": str(rule.get("severity", "block")),
                "line": line_number,
                "match": match.group(0),
                "excerpt": line[start:end],
                "message": str(rule.get("message", "命中合规规则")),
                "suggestion": str(rule.get("suggestion", "删除或改写该表达")),
            })
    blocking = [item for item in findings if item["severity"] == "block"]
    return {
        "schema_version": 1,
        "policy_id": str(policy.get("policy_id", "")),
        "effective_from": str(policy.get("effective_from", "")),
        "platform_id": platform_id,
        "source": source,
        "passed": not blocking,
        "finding_count": len(findings),
        "blocking_count": len(blocking),
        "findings": findings,
    }


def scan_path(
    path: Path,
    *,
    platform_id: str = "",
    rules: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not path.is_file():
        return {
            "schema_version": 1,
            "policy_id": str((rules or load_rules()).get("policy_id", "")),
            "platform_id": platform_id,
            "source": str(path),
            "passed": False,
            "finding_count": 1,
            "blocking_count": 1,
            "findings": [{
                "rule_id": "content.missing",
                "category": "文件",
                "severity": "block",
                "line": 0,
                "match": "",
                "excerpt": "",
                "message": "待扫描稿件不存在。",
                "suggestion": "先生成平台专属稿件。",
            }],
        }
    return scan_text(
        path.read_text(encoding="utf-8"),
        source=str(path),
        platform_id=platform_id,
        rules=rules,
    )


def build_report(pack: Path, platform_files: list[tuple[str, Path]]) -> dict[str, Any]:
    rules = load_rules()
    items = [scan_path(path, platform_id=platform_id, rules=rules) for platform_id, path in platform_files]
    blocking = sum(item["blocking_count"] for item in items)
    return {
        "schema_version": 1,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "policy_id": rules.get("policy_id", ""),
        "pack": str(pack),
        "passed": blocking == 0 and bool(items),
        "platform_count": len(items),
        "blocking_count": blocking,
        "items": items,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Scan social copy for engagement-inducement risks")
    parser.add_argument("paths", nargs="+", help="Markdown files to scan")
    parser.add_argument("--platform", default="")
    parser.add_argument("--output", default="")
    args = parser.parse_args()
    items = [scan_path(Path(raw).resolve(), platform_id=args.platform) for raw in args.paths]
    payload = {
        "schema_version": 1,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "passed": bool(items) and all(item["passed"] for item in items),
        "items": items,
    }
    rendered = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        Path(args.output).resolve().write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if payload["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
