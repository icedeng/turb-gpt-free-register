# -*- coding: utf-8 -*-
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from config import roxybrowser

try:
    from core import account_liveness, live_check_service
    _ACCOUNT_LIVENESS_IMPORT_ERROR = ""
except ModuleNotFoundError as exc:
    # 仅在未安装项目运行依赖的本地最小环境中跳过；完整 requirements 测试会执行这些用例。
    account_liveness = None
    live_check_service = None
    _ACCOUNT_LIVENESS_IMPORT_ERROR = str(exc)


@unittest.skipIf(account_liveness is None, f"account_liveness dependencies unavailable: {_ACCOUNT_LIVENESS_IMPORT_ERROR}")
class AccountLivenessDriverTests(unittest.TestCase):
    def test_same_as_registration_resolves_to_registration_driver(self):
        with patch.object(roxybrowser, "ACCOUNT_LIVENESS_DRIVER", "same_as_registration"), \
             patch.object(roxybrowser, "REGISTRATION_DRIVER", "roxy"):
            self.assertEqual(account_liveness._account_liveness_driver(), "roxy")

        with patch.object(roxybrowser, "ACCOUNT_LIVENESS_DRIVER", "same-as-registration"), \
             patch.object(roxybrowser, "REGISTRATION_DRIVER", "protocol"):
            self.assertEqual(account_liveness._account_liveness_driver(), "protocol")

    def test_roxy_driver_receives_complete_account_snapshot(self):
        account = {
            "id": 12,
            "email": "user@example.com",
            "extra_json": '{"registration_password":"secret"}',
        }
        result = {"ok": True, "status": "live", "access_token": "token", "auth_driver": "roxy"}
        with tempfile.TemporaryDirectory() as tmp:
            log_file = Path(tmp) / "live.log"
            with patch.object(account_liveness, "log_path", return_value=log_file), \
                 patch.object(account_liveness, "_account_liveness_driver", return_value="roxy"), \
                 patch("core.roxy_account_session.check_account_in_roxy", return_value=result) as check:
                output = account_liveness.check_account_liveness(
                    account["email"], proxy="socks5://127.0.0.1:1000", account=account,
                )
            self.assertTrue(log_file.exists())

        check.assert_called_once_with(account, proxy="socks5://127.0.0.1:1000")
        self.assertTrue(output["ok"])
        self.assertEqual(output["auth_driver"], "roxy")
        self.assertEqual(output["proxy_used"], "socks5://127.0.0.1:1000")

    def test_unknown_driver_is_reported_as_failed_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_file = Path(tmp) / "live.log"
            with patch.object(account_liveness, "log_path", return_value=log_file), \
                 patch.object(account_liveness, "_account_liveness_driver", return_value="unknown"):
                output = account_liveness.check_account_liveness("user@example.com")

        self.assertFalse(output["ok"])
        self.assertEqual(output["status"], "failed")
        self.assertEqual(output["auth_driver"], "unknown")
        self.assertIn("不支持的 ACCOUNT_LIVENESS_DRIVER", output["error"])

    def test_background_service_passes_complete_account_to_roxy_entry(self):
        account = {"id": 21, "email": "user@example.com", "extra_json": '{"roxy_auth_state":{}}'}

        class Slot:
            released = 0

            def release(self):
                self.released += 1

        slot = Slot()
        route = {
            "proxy": "socks5://127.0.0.1:1000",
            "proxy_mode": "request",
            "network_route": "proxy",
            "proxy_used": "socks5://127.0.0.1:1000",
            "proxy_fallback_reason": None,
        }
        live_result = {"ok": True, "status": "live", "access_token": "token"}
        with patch.object(live_check_service.db, "mark_account_live_check_running", return_value=True), \
             patch.object(live_check_service.db, "get_account", return_value=account), \
             patch.object(live_check_service.db, "update_account_liveness") as update, \
             patch.object(live_check_service, "resolve_plan_check_route", return_value=route), \
             patch.object(live_check_service, "check_account_liveness", return_value=live_result) as check, \
             patch.object(live_check_service, "_append_log"), \
             patch.object(live_check_service, "_QUEUE_SLOTS", slot):
            output = live_check_service._run_live_check(
                account_id=21,
                email=account["email"],
                proxy=None,
                trigger="manual",
            )

        self.assertEqual(output, live_result)
        check.assert_called_once_with(
            account["email"],
            proxy=route["proxy"],
            clear_log=False,
            account=account,
        )
        update.assert_called_once_with(21, live_result)
        self.assertEqual(slot.released, 1)


if __name__ == "__main__":
    unittest.main()
