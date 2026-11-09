#!/usr/bin/env python3
"""Verify a WeChat draft through draft/get and update local distribution evidence."""

from __future__ import annotations

import argparse
import json
import os
import re
from datetime import datetime
from pathlib import Path

from distribution_io import atomic_write_json, atomic_write_text, create_pack_backup, pack_lock
from social_draft_router import result_identity
from wechat_credentials import hydrate_wechat_environment
from wechat_draft_publisher import API, request_json, token


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify a WeChat official-account draft")
    parser.add_argument("--pack", required=True)
    parser.add_argument("--media-id", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--expected-images", type=int, default=5)
    args = parser.parse_args()

    source = hydrate_wechat_environment()
    app_id = os.environ.get("WECHAT_APP_ID", "")
    app_secret = os.environ.get("WECHAT_APP_SECRET", "")
    if not app_id or not app_secret:
        raise SystemExit("WeChat credentials are not configured")

    access_token = token(app_id, app_secret)
    result = request_json(
        f"{API}/draft/get?access_token={access_token}",
        data=json.dumps({"media_id": args.media_id}, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    items = result.get("news_item") if isinstance(result.get("news_item"), list) else []
    article = items[0] if items else {}
    content = str(article.get("content", ""))
    image_count = len(re.findall(r"<img\b", content, flags=re.I))
    title_ok = str(article.get("title", "")).strip() == args.title.strip()
    author_ok = str(article.get("author", "")).strip() == "华子社长"
    cover_ok = bool(str(article.get("thumb_media_id", "")).strip())
    valid = title_ok and author_ok and cover_ok and image_count == args.expected_images
    verified_at = datetime.now().astimezone().isoformat(timespec="seconds")

    pack = Path(args.pack).resolve()
    results_path = pack / "distribution-results.json"
    publish_result_path = pack / "wechat-publish-result.md"
    with pack_lock(pack, "wechat-draft-verify"):
        create_pack_backup(
            pack,
            "wechat-draft-verify",
            [results_path.name, publish_result_path.name],
            keep=10,
        )
        results = json.loads(results_path.read_text(encoding="utf-8"))
        for item in results.get("items", []):
            if item.get("platform_id") != "wechat":
                continue
            item.update({
                "status": "success" if valid else "unverified",
                "finished_at": verified_at,
                "image_count": image_count,
                "image_verified": image_count == args.expected_images,
                "cover_verified": cover_ok,
                "draft_verified": valid,
                "media_id": args.media_id,
                "delivery_used": "official_draft_api",
                "proof": f"draft_get_verified={str(valid).lower()};figures={image_count};thumb_media_id={'present' if cover_ok else 'missing'};credential_source={source}",
                "evidence": {
                    "kind": "api_response",
                    "verified_at": verified_at,
                    "summary": f"官方 draft/get 回查：标题与作者匹配，正文 {image_count} 张图，封面素材{'存在' if cover_ok else '缺失'}。",
                    "reference": args.media_id,
                },
            })
            if valid:
                item["reason"] = ""
            break
        results["result_id"] = result_identity(results)
        atomic_write_json(results_path, results)
        atomic_write_text(
            publish_result_path,
            "# 微信公众号草稿推送结果\n\n"
            f"- status: {'success' if valid else 'unverified'}\n"
            f"- verified_at: {verified_at}\n"
            f"- media_id: {args.media_id}\n"
            f"- image_count: {image_count}\n"
            f"- image_verified: {str(image_count == args.expected_images).lower()}\n"
            f"- cover_verified: {str(cover_ok).lower()}\n"
            f"- draft_verified: {str(valid).lower()}\n"
            f"- evidence_kind: api_response\n"
            f"- evidence_reference: {args.media_id}\n"
            f"- credential_source: {source}\n",
        )

    print(json.dumps({
        "verified": valid,
        "title_verified": title_ok,
        "author_verified": author_ok,
        "image_count": image_count,
        "cover_verified": cover_ok,
        "media_id": args.media_id,
        "result_id": results.get("result_id", ""),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
