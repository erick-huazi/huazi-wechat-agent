from __future__ import annotations

import importlib.util
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "browser_session_registry",
    ROOT / "scripts" / "browser_session_registry.py",
)
REGISTRY = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(REGISTRY)


class BrowserSessionRegistryTests(unittest.TestCase):
    def test_safe_editor_url_removes_query_and_fragment(self) -> None:
        self.assertEqual(
            REGISTRY.safe_editor_url("https://example.com/editor?token=secret#/draft/123"),
            "https://example.com/editor",
        )

    def test_control_unavailable_is_not_treated_as_logged_out(self) -> None:
        entry = {
            "auth_status": "auth_ok",
            "control_status": "unavailable",
            "draft_resume_status": "not_checked",
            "last_verified_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        self.assertEqual(REGISTRY.issue_code(entry), "AUTH_OK_CONTROL_UNAVAILABLE")

    def test_stale_session_wins_over_old_auth_ok(self) -> None:
        entry = {
            "auth_status": "auth_ok",
            "control_status": "controlled",
            "draft_resume_status": "available",
            "last_verified_at": (datetime.now().astimezone() - timedelta(days=8)).isoformat(timespec="seconds"),
        }
        self.assertEqual(REGISTRY.issue_code(entry), "SESSION_STALE")

    def test_empty_state_has_no_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = REGISTRY.load_state(Path(temp) / "missing.json")
        self.assertEqual(state["platforms"], {})
        self.assertNotIn("cookie", state)


if __name__ == "__main__":
    unittest.main()
