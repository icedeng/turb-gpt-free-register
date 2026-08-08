import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from core.proxy_utils import normalize_proxy_url
from core import roxybrowser_client
from core import roxy_reopen
from core import db
from core.roxybrowser_client import RoxyBrowserClient, _proxy_url_to_roxy_info


class ProxyUtilsTests(unittest.TestCase):
    def test_normalize_proxy_url_supports_pool_formats(self):
        cases = [
            (
                "user:pass@proxy.example:1080",
                "socks5://user:pass@proxy.example:1080",
            ),
            (
                "proxy.example:1080:user:pass",
                "socks5://user:pass@proxy.example:1080",
            ),
            (
                "user:pass:proxy.example:1080",
                "socks5://user:pass@proxy.example:1080",
            ),
            (
                "proxy.example:1080@user:pass",
                "socks5://user:pass@proxy.example:1080",
            ),
            (
                "http://user:pass@proxy.example:8080",
                "http://user:pass@proxy.example:8080",
            ),
            (
                "http://proxy.example:8080:user:pass",
                "http://user:pass@proxy.example:8080",
            ),
            (
                "socks5://user:pass:proxy.example:1080",
                "socks5://user:pass@proxy.example:1080",
            ),
            (
                "socks5h://proxy.example:1080",
                "socks5h://proxy.example:1080",
            ),
        ]
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(normalize_proxy_url(raw), expected)

    def test_normalize_proxy_url_keeps_empty_proxy_empty(self):
        self.assertEqual(normalize_proxy_url("   "), "")

    def test_normalize_proxy_url_rejects_ambiguous_host_first_value(self):
        with self.assertRaisesRegex(ValueError, "代理格式无法识别"):
            normalize_proxy_url("proxy.example:not-a-port:user:pass")

    def test_roxy_proxy_info_accepts_username_first_shorthand(self):
        info = _proxy_url_to_roxy_info("user:pass@proxy.example:1080")
        self.assertEqual(info["protocol"], "SOCKS5")
        self.assertEqual(info["host"], "proxy.example")
        self.assertEqual(info["port"], "1080")
        self.assertEqual(info["proxyUserName"], "user")
        self.assertEqual(info["proxyPassword"], "pass")

    def test_roxy_create_uses_dedicated_pool_and_overrides_snapshot_proxy(self):
        client = RoxyBrowserClient()
        captured = {}

        def fake_request(method, path, *, params=None, json_body=None):
            captured["body"] = json_body
            return {"data": {"dirId": "new-profile"}}

        with patch.object(roxybrowser_client._cfg, "ROXY_WORKSPACE_ID", "123"), \
             patch.object(roxybrowser_client._cfg, "ROXY_PROJECT_ID", "456"), \
             patch.object(roxybrowser_client._cfg, "ROXY_CREATE_USE_PROXY_POOL", True), \
             patch.object(roxybrowser_client._cfg, "ROXY_PROXY_POOL", ["http://user:pass@new.proxy:8080"]), \
             patch.object(client, "request", side_effect=fake_request):
            profile_id = client.create_profile(payload={
                "proxyInfo": {"host": "snapshot.proxy", "port": "1080"},
            })

        self.assertEqual(profile_id, "new-profile")
        self.assertEqual(captured["body"]["proxyInfo"]["host"], "new.proxy")
        self.assertEqual(captured["body"]["proxyInfo"]["protocol"], "HTTP")

    def test_roxy_create_fails_when_dedicated_pool_is_enabled_but_empty(self):
        client = RoxyBrowserClient()
        with patch.object(roxybrowser_client._cfg, "ROXY_WORKSPACE_ID", "123"), \
             patch.object(roxybrowser_client._cfg, "ROXY_CREATE_USE_PROXY_POOL", True), \
             patch.object(roxybrowser_client._cfg, "ROXY_PROXY_POOL", []):
            with self.assertRaisesRegex(RuntimeError, "ROXY_PROXY_POOL 为空"):
                client.create_profile()

    def test_reopen_cleanup_keeps_profile_and_window_open(self):
        client = MagicMock()
        driver = MagicMock()
        opened = SimpleNamespace(profile_id="temporary-profile")

        roxy_reopen._cleanup_reopen_resources(client, opened, driver)

        driver.quit.assert_not_called()
        client.close_profile.assert_not_called()
        client.delete_profile.assert_not_called()

    def test_close_and_release_account_roxy_closes_and_deletes_recorded_profile(self):
        account = {
            "email": "user@example.com",
            "extra_json": '{"roxy_auth_state":{"reopen":{"profile_id":"temporary-profile"}}}',
        }
        client = MagicMock()
        client.close_profile.return_value = True
        client.delete_profile.return_value = True

        with patch.object(roxy_reopen, "RoxyBrowserClient", return_value=client):
            result = roxy_reopen.close_and_release_account_roxy(account)

        client.close_profile.assert_called_once_with("temporary-profile")
        client.delete_profile.assert_called_once_with("temporary-profile", strict=True)
        self.assertTrue(result["deleted"])

    def test_close_and_release_account_roxy_is_idempotent_without_profile(self):
        result = roxy_reopen.close_and_release_account_roxy({"email": "user@example.com"})

        self.assertTrue(result["already_released"])

    def test_close_and_release_account_roxy_clears_stale_missing_profile(self):
        account = {
            "email": "user@example.com",
            "extra_json": '{"roxy_auth_state":{"reopen":{"profile_id":"stale-profile"}}}',
        }
        client = MagicMock()
        client.close_profile.return_value = False
        client.delete_profile.side_effect = RuntimeError("Roxy API 返回失败: 环境不存在")

        with patch.object(roxy_reopen, "RoxyBrowserClient", return_value=client):
            result = roxy_reopen.close_and_release_account_roxy(account)

        self.assertTrue(result["already_released"])
        self.assertTrue(result["deleted"])

    def test_account_trial_filter_distinguishes_eligible_ineligible_and_unknown(self):
        self.assertTrue(db._account_matches_trial_filter({"plus_trial_eligible": True}, "eligible"))
        self.assertFalse(db._account_matches_trial_filter({"plus_trial_eligible": False}, "eligible"))
        self.assertTrue(db._account_matches_trial_filter({"plus_trial_eligible": False}, "ineligible"))
        self.assertTrue(db._account_matches_trial_filter({}, "unknown"))
        self.assertFalse(db._account_matches_trial_filter({"plus_trial_eligible": "yes"}, "unknown"))


if __name__ == "__main__":
    unittest.main()
