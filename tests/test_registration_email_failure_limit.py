import unittest
from unittest.mock import patch

from core import db, registration_service


class RegistrationEmailFailureLimitTests(unittest.TestCase):
    def test_count_only_full_registration_failures_without_account(self):
        rows = [
            {"email": "user@example.com", "status": "failed", "job_type": "registration", "account_id": None},
            {"email": "USER@example.com", "status": "failed", "job_type": "registration", "account_id": ""},
            {"email": "user@example.com", "status": "stopped", "job_type": "registration", "account_id": None},
            {"email": "user@example.com", "status": "failed", "job_type": "codex_retry", "account_id": 1},
            {"email": "user@example.com", "status": "failed", "job_type": "registration", "account_id": 1},
        ]
        with patch.object(db, "_load_jobs", return_value=rows):
            self.assertEqual(db.count_registration_failures("user@example.com"), 2)

    def test_failure_below_limit_releases_email(self):
        with patch.object(db, "count_registration_failures", return_value=2), \
             patch.object(registration_service, "_registration_email_max_failures", return_value=3), \
             patch.object(registration_service, "_release_unconsumed_job_email") as release, \
             patch.object(registration_service, "_disable_job_email") as disable:
            registration_service._handle_failed_registration_email("user@example.com", "普通失败")

        release.assert_called_once()
        disable.assert_not_called()

    def test_failure_at_limit_disables_email(self):
        with patch.object(db, "count_registration_failures", return_value=3), \
             patch.object(registration_service, "_registration_email_max_failures", return_value=3), \
             patch.object(registration_service, "_release_unconsumed_job_email") as release, \
             patch.object(registration_service, "_disable_job_email") as disable:
            registration_service._handle_failed_registration_email("user@example.com", "普通失败")

        disable.assert_called_once()
        release.assert_not_called()

    def test_high_risk_failure_disables_immediately(self):
        reason = "等待 /api/auth/session accessToken 超时 WARNING_BANNER '_http_status': 200"
        with patch.object(db, "count_registration_failures") as count, \
             patch.object(registration_service, "_disable_job_email") as disable:
            registration_service._handle_failed_registration_email("user@example.com", reason)

        disable.assert_called_once()
        count.assert_not_called()


if __name__ == "__main__":
    unittest.main()
