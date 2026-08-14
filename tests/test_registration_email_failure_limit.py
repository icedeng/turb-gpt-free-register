import unittest
from unittest.mock import patch

from core import db, registration_service


class RegistrationEmailFailureLimitTests(unittest.TestCase):
    def test_startup_recovery_reconciles_active_registration_jobs(self):
        jobs = [
            {"id": 10, "status": "running", "job_type": "registration", "email": "done@example.com"},
            {"id": 11, "status": "pending", "job_type": "registration", "email": "unused@example.com"},
            {"id": 12, "status": "stopping", "job_type": "codex_retry", "email": "codex@example.com"},
            {"id": 13, "status": "success", "job_type": "registration", "email": "old@example.com"},
        ]

        def account_for_job(job):
            return {"id": 99, "email": "done@example.com"} if job["id"] == 10 else None

        with patch.object(db, "list_jobs", return_value=jobs), \
             patch.object(registration_service, "_account_for_job", side_effect=account_for_job), \
             patch.object(db, "update_job") as update, \
             patch.object(registration_service, "_release_unconsumed_job_email", return_value=True) as release:
            result = registration_service.recover_interrupted_jobs()

        self.assertEqual(result, {"total": 3, "success": 1, "stopped": 2, "released": 1})
        update.assert_any_call(
            10,
            status="success",
            email="done@example.com",
            account_id=99,
            completed_at=unittest.mock.ANY,
            clear_error=True,
        )
        update.assert_any_call(
            11,
            status="stopped",
            error="WebUI 重启导致任务中断，请重试",
            completed_at=unittest.mock.ANY,
        )
        update.assert_any_call(
            12,
            status="stopped",
            error="WebUI 重启导致任务中断，请重试",
            completed_at=unittest.mock.ANY,
        )
        release.assert_called_once_with("unused@example.com", "WebUI 重启导致注册任务中断")

    def test_count_only_full_registration_failures_without_account(self):
        rows = [
            {"email": "user@example.com", "status": "failed", "job_type": "registration", "account_id": None},
            {"email": "USER@example.com", "status": "failed", "job_type": "registration", "account_id": ""},
            {"email": "user@example.com", "status": "stopped", "job_type": "registration", "account_id": None},
            {"email": "user@example.com", "status": "failed", "job_type": "registration", "account_id": None, "error_message": "Roxy API 返回失败：窗口单日创建次数已经超出"},
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

    def test_roxy_quota_failure_is_released_without_counting(self):
        with patch.object(registration_service, "_release_unconsumed_job_email", return_value=True) as release, \
             patch.object(db, "count_registration_failures") as count, \
             patch.object(registration_service, "_disable_job_email") as disable:
            registration_service._handle_failed_registration_email(
                "user@example.com", "RuntimeError: Roxy API 返回失败 POST /browser/create: 窗口单日创建次数已经超出",
            )

        release.assert_called_once()
        count.assert_not_called()
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
