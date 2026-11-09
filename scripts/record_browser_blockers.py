#!/usr/bin/env python3
"""Record browser-session blockers without misreporting draft success."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

from distribution_io import atomic_write_json, atomic_write_text, create_pack_backup, pack_lock
from social_draft_router import result_identity


RESULT_FILES = {
    "toutiao": "toutiao-publish-result.md",
    "zhihu": "zhihu-publish-result.md",
    "xiaohongshu": "xiaohongshu-publish-result.md",
    "douyin-image": "douyin-publish-result.md",
    "kuaishou-image": "kuaishou-publish-result.md",
    "douyin-video": "douyin-publish-result.md",
    "video-channel": "video-channel-publish-result.md",
    "kuaishou-video": "kuaishou-publish-result.md",
    "weibo": "weibo-publish-result.md",
    "bilibili-column": "bilibili-publish-result.md",
}


def main() -> None:
    parser = argparse.ArgumentParser(description="Record platform browser blockers")
    parser.add_argument("--pack", required=True)
    parser.add_argument("--platforms", required=True)
    parser.add_argument("--evidence", required=True)
    parser.add_argument(
        "--reason",
        default="browser control unavailable; authentication was not evaluated",
    )
    parser.add_argument(
        "--issue-code",
        choices=["AUTH_REQUIRED", "AUTH_UNKNOWN_CONTROL_UNAVAILABLE", "AUTH_OK_CONTROL_UNAVAILABLE"],
        default="AUTH_UNKNOWN_CONTROL_UNAVAILABLE",
    )
    args = parser.parse_args()

    pack = Path(args.pack).resolve()
    evidence_path = (pack / args.evidence).resolve()
    if not evidence_path.is_file() or pack not in evidence_path.parents:
        raise SystemExit("Evidence file must exist inside the publish pack")

    platform_ids = [value.strip() for value in args.platforms.split(",") if value.strip()]
    unknown = [value for value in platform_ids if value not in RESULT_FILES]
    if unknown:
        raise SystemExit(f"Unsupported platform: {unknown[0]}")

    results_path = pack / "distribution-results.json"
    result_names = [RESULT_FILES[platform_id] for platform_id in platform_ids]
    verified_at = datetime.now().astimezone().isoformat(timespec="seconds")
    with pack_lock(pack, "record-browser-blockers"):
        create_pack_backup(pack, "record-browser-blockers", [results_path.name, *result_names], keep=10)
        results = json.loads(results_path.read_text(encoding="utf-8"))
        index = {str(item.get("platform_id")): item for item in results.get("items", [])}
        for platform_id in platform_ids:
            item = index.get(platform_id)
            if item is None:
                continue
            auth_required = args.issue_code == "AUTH_REQUIRED"
            item.update({
                "status": "blocked" if auth_required else "partial",
                "finished_at": verified_at,
                "reason": args.reason,
                "issue_code": args.issue_code,
                "image_count": 0,
                "image_verified": False,
                "cover_verified": False,
                "draft_verified": False,
                "draft_url": "",
                "media_id": "",
                "evidence": {
                    "kind": "account_state",
                    "verified_at": verified_at,
                    "summary": (
                        "创作页明确要求重新登录；未上传内容，未形成草稿。"
                        if auth_required
                        else "本次没有可控制的创作标签页；登录状态未被判定为失效，未上传内容，未形成草稿。"
                    ),
                    "reference": args.evidence.replace("\\", "/"),
                },
            })
            atomic_write_text(
                pack / RESULT_FILES[platform_id],
                f"# {item.get('platform', platform_id)}草稿结果\n\n"
                f"- status: {'blocked' if auth_required else 'partial'}\n"
                f"- verified_at: {verified_at}\n"
                f"- issue_code: {args.issue_code}\n"
                "- image_count: 0\n"
                "- image_verified: false\n"
                "- cover_verified: false\n"
                "- draft_verified: false\n"
                "- evidence_kind: account_state\n"
                f"- evidence_reference: {args.evidence.replace(chr(92), '/')}\n"
                f"- reason: {args.reason}\n",
            )
        results["result_id"] = result_identity(results)
        atomic_write_json(results_path, results)

    print(json.dumps({
        "updated": platform_ids,
        "verified_at": verified_at,
        "result_id": results.get("result_id", ""),
        "evidence": args.evidence.replace("\\", "/"),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
