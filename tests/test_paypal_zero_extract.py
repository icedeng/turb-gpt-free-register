# -*- coding: utf-8 -*-
import tempfile
import unittest
from pathlib import Path
from unittest.mock import call, patch

from core import db, extract_link_service as service


class _QueueSlots:
    def __init__(self):
        self.released = 0

    def acquire(self, blocking=False):
        return True

    def release(self):
        self.released += 1


class _Executor:
    def __init__(self):
        self.kwargs = None

    def submit(self, _fn, **kwargs):
        self.kwargs = kwargs
        return object()


class PaypalZeroExtractTests(unittest.TestCase):
    def test_link_type_supports_paypal_zero(self):
        self.assertEqual(service._link_type("paypal_zero"), "paypal_zero")

    def test_empty_proxy_pool_fails_clearly(self):
        with patch.object(service, "_runtime_setting", return_value=[]):
            with self.assertRaisesRegex(ValueError, "需要巴西出口代理"):
                service._paypal_zero_proxy_pool()

    def test_create_job_builds_link_pp_payload(self):
        settings = {
            "PAYPAL_ZERO_PROXY_POOL": ["proxy.example:8080:user:pass"],
            "PAYPAL_ZERO_PROXY_COUNTRY": "br",
            "PAYPAL_ZERO_PROXY_SCHEME": "http",
            "PAYPAL_ZERO_CHECKOUT_ATTEMPTS": 4,
            "PAYPAL_ZERO_PROVIDER_ATTEMPTS": 7,
            "PAYPAL_ZERO_API_BASE": "http://127.0.0.1:5572",
            "EXTRACT_LINK_REQUEST_TIMEOUT": 30,
        }
        with patch.object(service, "_runtime_setting", side_effect=lambda name, default=None: settings.get(name, default)), \
             patch.object(service, "_request_json", return_value={"job_id": "job-1"}) as request_json:
            result = service._create_paypal_zero_job(token="access-token")

        self.assertEqual(result["job_id"], "job-1")
        request_json.assert_called_once_with(
            "POST",
            "http://127.0.0.1:5572/api/jobs",
            payload={
                "access_token": "access-token",
                "country": "BR",
                "proxies": "proxy.example:8080:user:pass",
                "proxy_scheme": "http",
                "checkout_attempts": 4,
                "provider_attempts": 7,
            },
            timeout=30,
        )

    def test_paypal_result_maps_to_existing_link_fields(self):
        result = service._paypal_zero_result({
            "paypal_approve_url": "https://www.paypal.com/agreements/approve?ba_token=BA-1",
            "ba_token": "BA-1",
            "session_id": "oaics_1",
        })
        self.assertEqual(result["long_url"], result["paypal_approve_url"])
        self.assertEqual(result["copy_paste"], result["paypal_approve_url"])
        self.assertEqual(result["payment_method"], "paypal")
        self.assertEqual(result["payment_link_type"], "paypal_zero")

    def test_paypal_zero_enqueue_does_not_require_cdk(self):
        slots = _QueueSlots()
        executor = _Executor()
        with patch.object(service, "_QUEUE_SLOTS", slots), \
             patch.object(service, "_EXECUTOR", executor), \
             patch.object(service.db, "claim_account_extract", return_value=True), \
             patch.object(service, "_cdk", side_effect=AssertionError("不应读取 CDK")):
            result = service.enqueue_account_extract(
                account_id=7,
                email="user@example.com",
                access_token="token",
                link_type="paypal_zero",
            )

        self.assertTrue(result["accepted"])
        self.assertEqual(executor.kwargs["cdk"], "")
        self.assertEqual(slots.released, 0)

    def test_run_paypal_job_maps_success(self):
        updates = []
        responses = [
            {"job_id": "job-2"},
            {"status": "running"},
            {
                "status": "success",
                "result": {
                    "paypal_approve_url": "https://www.paypal.com/agreements/approve?ba_token=BA-2",
                    "provider_redirect_url": "https://paypal.example/redirect",
                    "checkout_url": "https://chatgpt.com/checkout",
                    "ba_token": "BA-2",
                    "session_id": "oaics_2",
                },
            },
        ]
        with patch.object(service.db, "mark_account_extract_running", return_value=True), \
             patch.object(service.db, "update_account_extract", side_effect=lambda _id, value: updates.append(value)), \
             patch.object(service, "_request_json", side_effect=responses), \
             patch.object(service, "_paypal_zero_proxy_pool", return_value=["http://proxy.example:8080"]), \
             patch.object(service.time, "sleep"):
            result = service._run_paypal_zero_extract(
                account_id=8,
                email="user@example.com",
                access_token="token",
                trigger="manual",
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["result"]["ba_token"], "BA-2")
        self.assertEqual(updates[-1]["status"], "success")

    def test_failed_link_pp_job_uses_failure_reason(self):
        slots = _QueueSlots()
        responses = [
            {"job_id": "job-3"},
            {"status": "failed", "failure_reason": "未开放 PayPal"},
        ]
        with patch.object(service, "_QUEUE_SLOTS", slots), \
             patch.object(service.db, "mark_account_extract_running", return_value=True), \
             patch.object(service.db, "update_account_extract") as update, \
             patch.object(service, "_request_json", side_effect=responses), \
             patch.object(service, "_paypal_zero_proxy_pool", return_value=["http://proxy.example:8080"]):
            result = service._run_extract(
                account_id=9,
                email="user@example.com",
                access_token="token",
                link_type="paypal_zero",
                cdk="",
                trigger="manual",
            )

        self.assertFalse(result["ok"])
        self.assertIn("未开放 PayPal", result["error"])
        self.assertEqual(slots.released, 1)
        self.assertEqual(update.call_args_list[-1], call(9, result))

    def test_db_persists_paypal_specific_fields(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            accounts_path = root / "accounts.json"
            accounts_path.write_text('[{"id":1,"email":"a@test.com"}]', encoding="utf-8")
            with patch.object(db, "_ACCOUNTS_JSON", accounts_path), \
                 patch.object(db, "_LEGACY_ACCOUNTS_JSON", root / "legacy.json"), \
                 patch.object(db, "_ACCOUNTS_TXT", root / "accounts.txt"), \
                 patch.object(db, "_TOKENS_TXT", root / "tokens.txt"), \
                 patch.object(db, "_VIEWER_HTML", root / "viewer.html"):
                db.update_account_extract(1, {
                    "ok": True,
                    "status": "success",
                    "result": {
                        "paypal_approve_url": "https://paypal.example/approve",
                        "provider_redirect_url": "https://paypal.example/redirect",
                        "checkout_url": "https://chatgpt.com/checkout",
                        "ba_token": "BA-DB",
                        "session_id": "oaics_db",
                    },
                })
                account = db.get_account(1)

        self.assertEqual(account["extract_link_paypal_approve_url"], "https://paypal.example/approve")
        self.assertEqual(account["extract_link_ba_token"], "BA-DB")
        self.assertEqual(account["extract_link_checkout_session_id"], "oaics_db")


if __name__ == "__main__":
    unittest.main()
