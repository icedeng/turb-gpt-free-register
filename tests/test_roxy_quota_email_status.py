# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

try:
    from core import roxy_registration
    _IMPORT_ERROR = ""
except ModuleNotFoundError as exc:
    roxy_registration = None
    _IMPORT_ERROR = str(exc)


@unittest.skipIf(roxy_registration is None, f"roxy registration dependencies unavailable: {_IMPORT_ERROR}")
class RoxyQuotaEmailStatusTests(unittest.TestCase):
    def test_profile_quota_error_is_logged_and_email_remains_available(self):
        error = RuntimeError("Roxy API 返回失败 POST /browser/create: 窗口单日创建次数已经超出")
        with patch.object(roxy_registration.RoxyBrowserClient, "open_profile", side_effect=error), \
             patch("core.email_provider.release_email") as release, \
             self.assertLogs(roxy_registration.logger, level="WARNING") as logs:
            result = roxy_registration.run_roxy_registration(
                "quota@example.com", "Test User", "1995-01-01",
            )

        self.assertFalse(result["success"])
        self.assertIn("窗口单日创建次数已经超出", result["error"])
        release.assert_called_once()
        self.assertEqual(release.call_args.kwargs["status"], "available")
        self.assertIn("Roxy额度超限，邮箱未失败", release.call_args.kwargs["note"])
        self.assertIn("邮箱保持 available", "\n".join(logs.output))


if __name__ == "__main__":
    unittest.main()
