# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch


try:
    from core import registration_service as service
except ModuleNotFoundError as exc:  # 本机未安装浏览器依赖时保持测试集可运行
    service = None
    _IMPORT_ERROR = exc
else:
    _IMPORT_ERROR = None


@unittest.skipIf(service is None, f"registration_service dependencies unavailable: {_IMPORT_ERROR}")
class RegistrationRetryInfoBatchTests(unittest.TestCase):
    def test_batch_uses_single_account_snapshot_and_job_context(self):
        jobs = [
            {"id": 1, "status": "failed", "email": "a@example.com"},
            {"id": 2, "status": "success", "root_job_id": 1, "email": "a@example.com"},
            {"id": 3, "status": "failed", "account_id": 9, "email": "b@example.com"},
        ]
        accounts = [{"id": 9, "email": "b@example.com", "codex_status": "failed"}]
        with patch.object(service.db, "list_accounts", return_value=accounts) as list_accounts, \
             patch.object(service.db, "list_jobs", side_effect=AssertionError("不应重复读取任务文件")), \
             patch.object(service.db, "get_successful_retry_for_job", side_effect=AssertionError("不应逐条读取任务文件")), \
             patch.object(service.db, "get_account", side_effect=AssertionError("不应逐条读取账号文件")), \
             patch.object(service.db, "get_account_by_email", side_effect=AssertionError("不应逐条读取账号文件")):
            results = service.get_retry_infos([jobs[0], jobs[2]], all_jobs=jobs)

        list_accounts.assert_called_once_with(limit=1_000_000, archived="all")
        self.assertFalse(results[0]["retryable"])
        self.assertEqual(results[0]["successful_retry_job_id"], 2)
        self.assertTrue(results[1]["retryable"])
        self.assertEqual(results[1]["retry_action"], "codex")
        self.assertEqual(results[1]["display_status"], "partial_success")


if __name__ == "__main__":
    unittest.main()
