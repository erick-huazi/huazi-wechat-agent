"""Daily destinations, with historical plan scope preserved for validation."""

from __future__ import annotations

import json
from pathlib import Path
from distribution_io import pack_date_prefix

POLICY_PATH = Path(__file__).resolve().parents[1] / "config" / "draft-workflow.json"
POLICY = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
ACTIVE_PLATFORMS = tuple(POLICY["active_platforms"])
STAGE_PLATFORMS = tuple(POLICY["stage_platforms"])
PAUSED_PLATFORMS = frozenset(POLICY.get("paused_platforms", []))
DISABLED_PLATFORMS = frozenset(POLICY["disabled_platforms"]) | PAUSED_PLATFORMS
VIDEO_PLATFORMS = frozenset({"douyin-video", "video-channel", "kuaishou-video"})
GRAPHIC_PLATFORMS = ("wechat", "toutiao", "zhihu", "xiaohongshu", "weibo")
ARTICLE_FILES = dict(zip(GRAPHIC_PLATFORMS, ("wechat-article.md", "toutiao-article.md", "zhihu-article.md", "xiaohongshu.md", "weibo-article.md")))


def require_active(platforms) -> None:
    disabled = sorted(set(platforms) - set(ACTIVE_PLATFORMS))
    if disabled:
        raise ValueError("PLATFORM_DISABLED_BY_WORKFLOW: " + ",".join(disabled))


def daily_platform_scopes(pack: Path) -> list[list[str]]:
    """Exact scopes for dated QA; the change day preserves pre-existing plans."""
    date = pack_date_prefix(pack) or ""
    if date < POLICY["effective_from"]:
        return []
    changed = POLICY.get("scope_changed_on", POLICY["effective_from"])
    previous = list(POLICY.get("previous_active_platforms", ACTIVE_PLATFORMS))
    if date < changed:
        return [previous]
    if date == changed and previous != list(ACTIVE_PLATFORMS):
        return [list(ACTIVE_PLATFORMS), previous]
    return [list(ACTIVE_PLATFORMS)]


def scope_matches_daily_policy(pack: Path, ids: list[str]) -> bool:
    scopes = daily_platform_scopes(pack)
    return not scopes or any(sorted(ids) == sorted(scope) for scope in scopes)


def inspection_platform_ids(pack: Path) -> list[str]:
    ids = plan_platform_ids(pack)
    scopes = daily_platform_scopes(pack)
    if scopes and (not (pack / "distribution-plan.json").is_file() or not scope_matches_daily_policy(pack, ids)):
        return list(dict.fromkeys(pid for scope in scopes for pid in scope))
    return ids


def plan_platform_ids(pack: Path) -> list[str]:
    path = pack / "distribution-plan.json"
    if not path.is_file():
        date = pack_date_prefix(pack) or ""
        return list(POLICY.get("previous_active_platforms", ACTIVE_PLATFORMS)
                    if date and date < POLICY.get("scope_changed_on", POLICY["effective_from"])
                    else ACTIVE_PLATFORMS)
    try:
        plan = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(plan, dict) or not isinstance(plan.get("items"), list):
        return []
    return [str(item.get("platform_id", "")) if isinstance(item, dict) else "" for item in plan["items"]]


def requires_video(pack: Path) -> bool:
    return bool(set(plan_platform_ids(pack)) & VIDEO_PLATFORMS)


def exact_platform_coverage(items: list[dict], expected: list[str]) -> bool:
    if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
        return False
    ids = [item.get("platform_id", "") for item in items]
    if any(not isinstance(pid, str) or not pid for pid in ids):
        return False
    return bool(expected) and bool(all(expected)) and len(set(expected)) == len(expected) and sorted(ids) == sorted(expected)


def quality_platform_ids(pack: Path) -> list[str]:
    ids = []
    for platform in plan_platform_ids(pack):
        if platform == "weibo":
            ids.extend(("weibo_article", "weibo"))
        else:
            ids.append(platform.replace("-", "_"))
    return ids
