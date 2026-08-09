# -*- coding: utf-8 -*-
import json
import sys
import types
import unittest
from unittest.mock import patch

from core import db, roxy_account_session
import core.roxy_reopen  # noqa: F401
from core.roxybrowser_client import RoxyOpenResult


class _FakeDriver:
    current_url = "https://chatgpt.com/"

    def __init__(self):
        self.quit_called = False
        self.visited = []

    def set_page_load_timeout(self, _timeout):
        return None

    def set_script_timeout(self, _timeout):
        return None

    def get(self, url):
        self.visited.append(url)
        self.current_url = url

    def refresh(self):
        return None

    def quit(self):
        self.quit_called = True


class _StorageDriver:
    current_url = "https://chatgpt.com/"

    def __init__(self):
        self.visited = []

    def get(self, url):
        self.visited.append(url)
        self.current_url = url


class _FakeClient:
    def __init__(self):
        self.created = []
        self.opened = []
        self.closed = []
        self.deleted = []

    def create_profile(self, payload=None):
        self.created.append(payload)
        return "temporary-profile"

    def open_profile(self, profile_id, **kwargs):
        self.opened.append((profile_id, kwargs))
        return RoxyOpenResult(
            profile_id,
            {"ok": True},
            debugger_address="127.0.0.1:9222",
        )

    def close_profile(self, profile_id):
        self.closed.append(profile_id)

    def delete_profile(self, profile_id):
        self.deleted.append(profile_id)


class RoxyAccountSessionTests(unittest.TestCase):
    @staticmethod
    def _fake_roxy_registration(driver, session):
        module = types.ModuleType("core.roxy_registration")
        module._build_driver = lambda _opened: driver
        module._read_chatgpt_session_once = lambda _driver: session
        return module

    def test_cookie_session_is_reused_and_profile_is_cleaned(self):
        client = _FakeClient()
        driver = _FakeDriver()
        account = {
            "email": "user@example.com",
            "extra_json": json.dumps({
                "roxy_auth_state": {
                    "cookies": [{"name": "session", "value": "saved"}],
                    "storage": {"local": {"k": "v"}},
                },
            }),
        }
        session = {
            "accessToken": "new-token",
            "user": {"email": "user@example.com"},
            "account": {"planType": "free"},
        }
        fake_registration = self._fake_roxy_registration(driver, session)
        with patch.dict(sys.modules, {"core.roxy_registration": fake_registration}), \
             patch.object(roxy_account_session, "RoxyBrowserClient", return_value=client), \
             patch("core.roxy_reopen._chatgpt_session_is_usable", return_value=(True, "session_ok")), \
             patch("core.roxy_reopen.capture_auth_state", return_value={"cookies": [{"name": "fresh"}]}), \
             patch.object(roxy_account_session, "_restore_saved_state", return_value=1):
            result = roxy_account_session.check_account_in_roxy(account, headless=True)

        self.assertTrue(result["ok"])
        self.assertEqual(result["auth_method"], "cookie")
        self.assertEqual(result["access_token"], "new-token")
        self.assertEqual(client.created, [None])
        self.assertEqual(client.opened[0][0], "temporary-profile")
        self.assertTrue(client.opened[0][1]["headless"])
        self.assertEqual(client.closed, ["temporary-profile"])
        self.assertEqual(client.deleted, ["temporary-profile"])
        self.assertTrue(driver.quit_called)

    def test_liveness_persists_roxy_state_without_overwriting_manual_reopen(self):
        row = {
            "id": 7,
            "email": "user@example.com",
            "access_token": "old-token",
            "extra_json": json.dumps({
                "roxy_auth_state": {
                    "cookies": [{"name": "old"}],
                    "reopen": {"profile_id": "manual-profile", "status": "open"},
                },
            }),
        }
        saved = []
        result = {
            "ok": True,
            "status": "live",
            "checked_at": "2026-08-09T20:00:00",
            "access_token": "fresh-token",
            "session": {"user": {"id": "user-1", "name": "User"}},
            "auth_driver": "roxy",
            "auth_method": "cookie",
            "auth_profile_id": "temporary-profile",
            "auth_state": {"cookies": [{"name": "fresh"}]},
            "restored_cookies": 1,
        }
        with patch.object(db, "_load_accounts", return_value=[row]), \
             patch.object(db, "_save_accounts", side_effect=lambda rows: saved.extend(rows)):
            self.assertTrue(db.update_account_liveness(7, result))

        self.assertEqual(row["access_token"], "fresh-token")
        self.assertEqual(row["live_check_driver"], "roxy")
        self.assertEqual(row["live_check_method"], "cookie")
        extra = json.loads(row["extra_json"])
        self.assertEqual(extra["roxy_auth_state"]["cookies"], [{"name": "fresh"}])
        self.assertEqual(extra["roxy_auth_state"]["reopen"]["profile_id"], "manual-profile")
        self.assertEqual(extra["roxy_live_check"]["profile_id"], "temporary-profile")
        self.assertEqual(saved[0]["id"], 7)

    def test_empty_capture_does_not_discard_previous_roxy_cookies(self):
        row = {
            "id": 8,
            "email": "user@example.com",
            "extra_json": json.dumps({
                "roxy_auth_state": {
                    "cookies": [{"name": "saved", "value": "cookie"}],
                    "storage": {"local": {"key": "saved"}},
                    "storage_origin": "https://chatgpt.com",
                },
            }),
        }
        result = {
            "ok": True,
            "status": "live",
            "access_token": "fresh-token",
            "auth_driver": "roxy",
            "auth_method": "password",
            "auth_state": {"cookies": [], "storage": {}, "storage_origin": ""},
        }
        with patch.object(db, "_load_accounts", return_value=[row]), \
             patch.object(db, "_save_accounts"):
            self.assertTrue(db.update_account_liveness(8, result))

        state = json.loads(row["extra_json"])["roxy_auth_state"]
        self.assertEqual(state["cookies"], [{"name": "saved", "value": "cookie"}])
        self.assertEqual(state["storage"]["local"]["key"], "saved")
        self.assertEqual(result["auth_state"]["cookies"], [])

    def test_invalid_cookie_falls_back_to_account_login(self):
        client = _FakeClient()
        driver = _FakeDriver()
        account = {
            "email": "user@example.com",
            "extra_json": json.dumps({
                "roxy_auth_state": {"cookies": [{"name": "expired", "value": "1"}]},
            }),
        }
        session = {
            "accessToken": "login-token",
            "user": {"email": "user@example.com"},
        }
        fake_registration = self._fake_roxy_registration(driver, None)
        with patch.dict(sys.modules, {"core.roxy_registration": fake_registration}), \
             patch.object(roxy_account_session, "RoxyBrowserClient", return_value=client), \
             patch("core.roxy_reopen._login_account_in_roxy", return_value={"session": session, "method": "email_otp"}), \
             patch("core.roxy_reopen._chatgpt_session_is_usable", return_value=(True, "session_ok")), \
             patch("core.roxy_reopen.capture_auth_state", return_value={"cookies": [{"name": "fresh"}]}), \
             patch.object(roxy_account_session, "_restore_saved_state", return_value=1), \
             patch.object(roxy_account_session, "_read_restored_session", return_value=None):
            result = roxy_account_session.check_account_in_roxy(account)

        self.assertTrue(result["ok"])
        self.assertEqual(result["auth_method"], "email_otp")
        self.assertEqual(result["access_token"], "login-token")
        self.assertEqual(client.closed, ["temporary-profile"])
        self.assertEqual(client.deleted, ["temporary-profile"])

    def test_cleanup_can_keep_profile_for_debugging(self):
        client = _FakeClient()
        driver = _FakeDriver()
        account = {"email": "user@example.com", "extra_json": "{}"}
        session = {"accessToken": "token", "user": {"email": "user@example.com"}}
        fake_registration = self._fake_roxy_registration(driver, session)
        with patch.dict(sys.modules, {"core.roxy_registration": fake_registration}), \
             patch.object(roxy_account_session, "RoxyBrowserClient", return_value=client), \
             patch.object(roxy_account_session._cfg, "ROXY_LIVE_CHECK_DELETE_PROFILE", False), \
             patch("core.roxy_reopen._login_account_in_roxy", return_value={"session": session, "method": "password"}), \
             patch("core.roxy_reopen._chatgpt_session_is_usable", return_value=(True, "session_ok")), \
             patch("core.roxy_reopen.capture_auth_state", return_value={"cookies": []}):
            result = roxy_account_session.check_account_in_roxy(account)

        self.assertTrue(result["ok"])
        self.assertEqual(client.closed, ["temporary-profile"])
        self.assertEqual(client.deleted, [])
        self.assertTrue(driver.quit_called)

    def test_login_error_still_closes_and_deletes_profile(self):
        client = _FakeClient()
        driver = _FakeDriver()
        account = {"email": "user@example.com", "extra_json": "{}"}
        fake_registration = self._fake_roxy_registration(driver, None)
        with patch.dict(sys.modules, {"core.roxy_registration": fake_registration}), \
             patch.object(roxy_account_session, "RoxyBrowserClient", return_value=client), \
             patch("core.roxy_reopen._login_account_in_roxy", side_effect=RuntimeError("login failed")), \
             patch.object(roxy_account_session, "_restore_saved_state", return_value=0):
            with self.assertRaisesRegex(RuntimeError, "login failed"):
                roxy_account_session.check_account_in_roxy(account)

        self.assertTrue(driver.quit_called)
        self.assertEqual(client.closed, ["temporary-profile"])
        self.assertEqual(client.deleted, ["temporary-profile"])

    def test_storage_is_restored_on_saved_origin_before_returning_to_chatgpt(self):
        driver = _StorageDriver()
        storage = {"local": {"key": "value"}, "session": {}}
        with patch("core.roxy_reopen._restore_cookies", return_value=2) as restore_cookies, \
             patch("core.roxy_reopen._restore_storage") as restore_storage:
            restored = roxy_account_session._restore_saved_state(
                driver,
                {"cookies": [{"name": "a"}], "storage": storage, "storage_origin": "https://auth.openai.com"},
            )

        self.assertEqual(restored, 2)
        restore_cookies.assert_called_once()
        restore_storage.assert_called_once_with(driver, storage)
        self.assertEqual(driver.visited, ["https://auth.openai.com/", "https://chatgpt.com/"])


if __name__ == "__main__":
    unittest.main()
