# -*- coding: utf-8 -*-
import json
import unittest
from unittest.mock import patch

from core import codex_oauth


class CpaOAuthTests(unittest.TestCase):
    def test_wait_cpa_auth_completion_waits_until_file_is_visible(self):
        responses = [
            {"status": "wait"},
            {"status": "ok"},
        ]
        meta = {"name": "codex-user@example.com-plus.json", "email": "user@example.com", "type": "codex"}

        with patch.object(codex_oauth._cfg, "CPA_AUTH_COMPLETION_TIMEOUT", 30), \
             patch.object(codex_oauth._cfg, "CPA_AUTH_COMPLETION_POLL_INTERVAL", 0.2), \
             patch.object(codex_oauth, "_cpa_request_json", side_effect=responses) as request, \
             patch.object(codex_oauth, "find_cpa_codex_auth_file", return_value=meta), \
             patch.object(codex_oauth.time, "sleep"):
            result = codex_oauth._wait_cpa_auth_completion("state-123", email="user@example.com")

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["cpa_file_name"], meta["name"])
        self.assertEqual(request.call_count, 2)

    def test_wait_cpa_auth_completion_raises_exchange_error(self):
        with patch.object(codex_oauth, "_cpa_request_json", return_value={
            "status": "error",
            "error": "Failed to exchange authorization code for tokens: unsupported_country_region_territory",
        }):
            with self.assertRaisesRegex(RuntimeError, "unsupported_country_region_territory"):
                codex_oauth._wait_cpa_auth_completion("state-123", email="user@example.com")

    def test_download_cpa_credential_normalizes_for_codex2api(self):
        raw = json.dumps({
            "type": "codex",
            "email": "user@example.com",
            "refreshToken": "rt-test",
            "accessToken": "at-test",
            "idToken": "id-test",
            "accountId": "account-test",
        })

        with patch.object(codex_oauth, "_cpa_request_raw", return_value=raw):
            text, name, _meta = codex_oauth.download_cpa_codex_auth_text(
                cpa_name="codex-user@example.com-plus.json",
                email="user@example.com",
            )

        result = json.loads(text)
        self.assertEqual(name, "codex-user@example.com-plus.json")
        self.assertEqual(result["refresh_token"], "rt-test")
        self.assertEqual(result["access_token"], "at-test")
        self.assertEqual(result["id_token"], "id-test")
        self.assertEqual(result["account_id"], "account-test")

    def test_download_cpa_rejects_callback_receipt_without_tokens(self):
        raw = json.dumps({
            "type": "codex_cpa_callback",
            "email": "user@example.com",
            "callback_url": "http://localhost:1455/auth/callback?code=redacted",
        })

        with patch.object(codex_oauth, "_cpa_request_raw", return_value=raw):
            with self.assertRaisesRegex(RuntimeError, "无法导入 Codex2API"):
                codex_oauth.download_cpa_codex_auth_text(
                    cpa_name="codex-user@example.com-cpa-callback.json",
                    email="user@example.com",
                )


if __name__ == "__main__":
    unittest.main()
