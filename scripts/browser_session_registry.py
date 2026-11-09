#!/usr/bin/env python3
"""Track non-secret creator-session health without storing browser credentials."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


SCRIPT_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_ROOT.parent
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

from distribution_io import atomic_write_json  # noqa: E402


DEFAULT_PATH = PROJECT_ROOT / "config" / "browser-session-state.json"
AUTH_STATUSES = {"auth_ok", "auth_required", "unknown"}
CONTROL_STATUSES = {"controlled", "unavailable", "unknown"}
DRAFT_RESUME_STATUSES = {"available", "not_checked", "unavailable"}


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def load_state(path: Path) -> dict:
    if not path.is_file():
        return {"schema_version": 1, "updated_at": "", "platforms": {}}
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema_version") != 1 or not isinstance(data.get("platforms"), dict):
        raise ValueError("browser session state must use schema_version=1")
    return data


def safe_editor_url(value: str) -> str:
    if not value:
        return ""
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"}:
        raise ValueError("editor URL must be http or https")
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def parse_time(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def issue_code(entry: dict, *, max_age_hours: float = 168) -> str:
    verified_at = parse_time(str(entry.get("last_verified_at", "")))
    if verified_at is None or datetime.now().astimezone() - verified_at > timedelta(hours=max_age_hours):
        return "SESSION_STALE"
    if entry.get("auth_status") == "auth_required":
        return "AUTH_REQUIRED"
    if entry.get("auth_status") != "auth_ok":
        return "AUTH_UNKNOWN"
    if entry.get("control_status") != "controlled":
        return "AUTH_OK_CONTROL_UNAVAILABLE"
    if entry.get("draft_resume_status") == "available":
        return "DRAFT_RESUME_AVAILABLE"
    return "SESSION_READY"


def record(args: argparse.Namespace) -> int:
    path = Path(args.state).resolve()
    state = load_state(path)
    verified_at = args.verified_at or now_iso()
    state["platforms"][args.platform] = {
        "browser_surface": args.browser_surface,
        "editor_url": safe_editor_url(args.editor_url),
        "auth_status": args.auth_status,
        "control_status": args.control_status,
        "draft_resume_status": args.draft_resume_status,
        "last_verified_at": verified_at,
        "reference": args.reference,
    }
    state["updated_at"] = now_iso()
    atomic_write_json(path, state)
    entry = state["platforms"][args.platform]
    print(json.dumps({
        "platform": args.platform,
        "issue_code": issue_code(entry),
        **entry,
    }, ensure_ascii=False, indent=2))
    return 0


def show(args: argparse.Namespace) -> int:
    state = load_state(Path(args.state).resolve())
    rows = []
    for platform, entry in sorted(state["platforms"].items()):
        rows.append({"platform": platform, "issue_code": issue_code(entry, max_age_hours=args.max_age_hours), **entry})
    print(json.dumps({"schema_version": 1, "platforms": rows}, ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", default=str(DEFAULT_PATH))
    commands = parser.add_subparsers(dest="command", required=True)

    record_parser = commands.add_parser("record")
    record_parser.add_argument("--platform", required=True)
    record_parser.add_argument("--browser-surface", default="iab")
    record_parser.add_argument("--editor-url", required=True)
    record_parser.add_argument("--auth-status", choices=sorted(AUTH_STATUSES), required=True)
    record_parser.add_argument("--control-status", choices=sorted(CONTROL_STATUSES), required=True)
    record_parser.add_argument("--draft-resume-status", choices=sorted(DRAFT_RESUME_STATUSES), required=True)
    record_parser.add_argument("--verified-at", default="")
    record_parser.add_argument("--reference", default="")
    record_parser.set_defaults(func=record)

    show_parser = commands.add_parser("show")
    show_parser.add_argument("--max-age-hours", type=float, default=168)
    show_parser.set_defaults(func=show)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
