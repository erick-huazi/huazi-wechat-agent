from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "wechat_credentials", ROOT / "scripts" / "wechat_credentials.py"
)
CREDENTIALS = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(CREDENTIALS)


class WeChatCredentialTests(unittest.TestCase):
    def test_setup_routes_to_masked_form_without_reading_stdin(self) -> None:
        with patch.object(CREDENTIALS, "setup_windows_credentials", return_value=0) as setup:
            with patch.object(CREDENTIALS.sys, "argv", ["wechat_credentials.py", "setup", "--app-id", "wx-test"]):
                with patch.object(CREDENTIALS.sys.stdin, "readline", side_effect=AssertionError("Unexpected stdin")):
                    self.assertEqual(CREDENTIALS.main(), 0)
        setup.assert_called_once_with("wx-test")

    def test_setup_cancellation_is_not_reported_as_saved(self) -> None:
        with patch.object(CREDENTIALS, "setup_windows_credentials", return_value=1):
            with patch.object(CREDENTIALS.sys, "argv", ["wechat_credentials.py", "setup"]):
                self.assertEqual(CREDENTIALS.main(), 1)

    def test_environment_has_priority(self) -> None:
        credentials = CREDENTIALS.resolve_wechat_credentials(
            {"WECHAT_APP_ID": "env-id", "WECHAT_APP_SECRET": "env-secret"},
            lambda: ("stored-id", "stored-secret"),
        )
        self.assertEqual(credentials.app_id, "env-id")
        self.assertEqual(credentials.source, "environment")

    def test_credential_manager_is_used_when_environment_is_missing(self) -> None:
        credentials = CREDENTIALS.resolve_wechat_credentials(
            {}, lambda: ("stored-id", "stored-secret")
        )
        self.assertEqual(credentials.app_secret, "stored-secret")
        self.assertEqual(credentials.source, "windows_credential_manager")

    def test_incomplete_sources_are_not_treated_as_configured(self) -> None:
        credentials = CREDENTIALS.resolve_wechat_credentials(
            {"WECHAT_APP_ID": "partial"}, lambda: None
        )
        self.assertIsNone(credentials)

    def test_hydrate_sets_process_values_without_overwriting(self) -> None:
        original_reader = CREDENTIALS.read_windows_credential
        try:
            CREDENTIALS.read_windows_credential = lambda: ("stored-id", "stored-secret")
            environment = {}
            source = CREDENTIALS.hydrate_wechat_environment(environment)
        finally:
            CREDENTIALS.read_windows_credential = original_reader
        self.assertEqual(source, "windows_credential_manager")
        self.assertEqual(environment["WECHAT_APP_ID"], "stored-id")


if __name__ == "__main__":
    unittest.main()
