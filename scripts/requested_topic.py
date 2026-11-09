"""Validate explicit, pack-scoped user exceptions without changing daily policy."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from distribution_io import canonical_sha256, file_sha256, pack_date_prefix

ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / "config" / "requested-topics.json"


def request_for_pack(pack: Path) -> dict | None:
    if pack.resolve().parent != (ROOT / "outputs").resolve() or not POLICY.exists():
        return None
    policy = json.loads(POLICY.read_text(encoding="utf-8"))
    if policy.get("schema_version") != 1 or not isinstance(policy.get("packs"), dict):
        raise ValueError("REQUESTED_TOPIC_POLICY_INVALID")
    entry = policy["packs"].get(pack.name)
    if entry is None:
        return None
    if (not isinstance(entry, dict) or entry.get("date") != pack_date_prefix(pack)
            or entry.get("waivers") != ["news_freshness", "candidate_competition"]
            or not isinstance(entry.get("user_confirmations"), list)
            or not entry["user_confirmations"]):
        raise ValueError("REQUESTED_TOPIC_AUTHORIZATION_INVALID")
    recorded = json.loads((pack / "topic-request.json").read_text(encoding="utf-8"))
    if recorded != entry:
        raise ValueError("REQUESTED_TOPIC_AUTHORIZATION_CHANGED")
    if file_sha256(pack / "wechat-article.md") != entry.get("article_sha256"):
        raise ValueError("REQUESTED_TOPIC_ARTICLE_CHANGED")
    if entry.get("browser_video_draft") is False:
        if entry.get("kind") != "wechat_graphic" or entry.get("videos") not in (None, []):
            raise ValueError("REQUESTED_TOPIC_AUTHORIZATION_INVALID")
        return {**entry, "request_id": canonical_sha256(entry)}
    if entry.get("browser_video_draft") is not True or len(entry["user_confirmations"]) != 2:
        raise ValueError("REQUESTED_TOPIC_AUTHORIZATION_INVALID")
    videos = entry.get("videos")
    if not isinstance(videos, list) or len(videos) != 2:
        raise ValueError("REQUESTED_TOPIC_VIDEO_COUNT_INVALID")
    seen = set()
    for video in videos:
        relative = Path(video.get("path", ""))
        target = pack / relative
        if (len(relative.parts) != 2 or relative.parts[0] != "media" or relative.suffix != ".mp4"
                or target.is_symlink() or not target.resolve().is_relative_to(pack.resolve())
                or not target.is_file() or file_sha256(target) != video.get("sha256")
                or video["sha256"] in seen):
            raise ValueError("REQUESTED_TOPIC_VIDEO_CHANGED")
        seen.add(video["sha256"])
    return {**entry, "request_id": canonical_sha256(entry)}


def inline_video_gate(pack: Path) -> dict:
    request = request_for_pack(pack)
    if not request or request.get("browser_video_draft") is not True:
        return {"required": False, "passed": True, "issues": []}
    issues = []
    try:
        review = json.loads((pack / "inline-video-review.json").read_text(encoding="utf-8"))
        stamp = datetime.fromisoformat(review["reviewed_at"])
        now = datetime.now(timezone.utc)
        if stamp.tzinfo is None or not now - timedelta(hours=24) <= stamp <= now + timedelta(minutes=5):
            issues.append("INLINE_VIDEO_REVIEW_TIME_INVALID")
        if (review.get("schema_version") != 1 or review.get("request_id") != request["request_id"]
                or review.get("article_sha256") != request["article_sha256"]
                or review.get("authorization_basis") != "user_attested"):
            issues.append("INLINE_VIDEO_REVIEW_BINDING_INVALID")
        rows = review.get("items", [])
        if not isinstance(rows, list) or len(rows) != len(request["videos"]):
            issues.append("INLINE_VIDEO_REVIEW_COUNT_INVALID")
        else:
            for expected, row in zip(request["videos"], rows):
                if (row.get("path") != expected["path"] or row.get("sha256") != expected["sha256"]
                        or row.get("has_audio") is not expected["has_audio"]
                        or row.get("full_decode_passed") is not True
                        or row.get("sampled_visual_review") is not True
                        or row.get("original_post_verified") is not True
                        or not row.get("observations") or not row.get("original_post")):
                    issues.append("INLINE_VIDEO_REVIEW_INCOMPLETE")
    except (OSError, ValueError, TypeError, KeyError):
        issues.append("INLINE_VIDEO_REVIEW_INVALID")
    return {"required": True, "passed": not issues, "issues": sorted(set(issues)),
            "scope": "technical_and_sampled_review_not_full_watch_or_audio_listening"}
