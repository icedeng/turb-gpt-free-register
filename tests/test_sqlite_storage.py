# -*- coding: utf-8 -*-
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from core import db


class SqliteStorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.paths = {
            "accounts": self.root / "accounts.json",
            "jobs": self.root / "jobs.json",
            "outlook": self.root / "outlook.json",
            "generic": self.root / "generic.json",
            "domain": self.root / "domain.json",
            "sqlite": self.root / "storage.sqlite3",
        }
        self.paths["accounts"].write_text(json.dumps([
            {"id": 1, "email": "a@example.com", "access_token": "token-a", "archived": False},
            {"id": 2, "email": "b@example.com", "access_token": "token-b", "archived": True},
        ]), encoding="utf-8")
        self.paths["jobs"].write_text(json.dumps([
            {"id": 1, "email": "a@example.com", "status": "failed"},
        ]), encoding="utf-8")
        self.paths["outlook"].write_text(json.dumps([
            {"id": 1, "email": "o@example.com", "status": "available"},
        ]), encoding="utf-8")
        self.paths["generic"].write_text(json.dumps([
            {"id": 1, "email": "g@example.com", "status": "used", "code_url": "https://mail.test"},
        ]), encoding="utf-8")
        self.paths["domain"].write_text("[]", encoding="utf-8")
        table_sources = {
            "accounts": (self.paths["accounts"],),
            "registration_jobs": (self.paths["jobs"],),
            "outlook_pool": (self.paths["outlook"],),
            "generic_email_pool": (self.paths["generic"],),
            "domain_email_pool": (self.paths["domain"],),
        }
        self.patchers = [
            patch.object(db, "_SQLITE_PATH", self.paths["sqlite"]),
            patch.object(db, "_SQLITE_TABLES", table_sources),
            patch.object(db, "_FORCE_SQLITE_FOR_TESTS", True),
        ]
        for item in self.patchers:
            item.start()
        db._SQLITE_READY_PATH = None
        db._SQLITE_ROW_CACHE.clear()

    def tearDown(self):
        db._SQLITE_READY_PATH = None
        db._SQLITE_ROW_CACHE.clear()
        for item in reversed(self.patchers):
            item.stop()
        self.tmp.cleanup()

    def test_imports_json_and_reports_integrity(self):
        status = db.sqlite_storage_status()
        self.assertEqual(status["quick_check"], "ok")
        self.assertEqual(status["journal_mode"], "wal")
        self.assertEqual(status["counts"]["accounts"], 2)
        self.assertEqual(status["counts"]["registration_jobs"], 1)
        self.assertEqual(db.count_accounts(), 2)
        self.assertEqual(db.get_account_by_email("A@EXAMPLE.COM")["access_token"], "token-a")

    def test_updates_one_account_without_rewriting_json_source(self):
        db.sqlite_storage_status()
        original_json = self.paths["accounts"].read_text(encoding="utf-8")
        self.assertTrue(db.update_account_note(1, "SQLite note"))
        self.assertEqual(db.get_account(1)["note"], "SQLite note")
        self.assertEqual(self.paths["accounts"].read_text(encoding="utf-8"), original_json)

        # 清缓存后重新从 SQLite 读取，确认不是仅更新内存。
        db._SQLITE_ROW_CACHE.clear()
        self.assertEqual(db.get_account(1)["note"], "SQLite note")

    def test_delete_and_insert_persist_across_cache_reload(self):
        db.sqlite_storage_status()
        self.assertTrue(db.delete_account(2))
        db.insert_account(email="c@example.com", access_token="token-c")
        db._SQLITE_ROW_CACHE.clear()
        self.assertIsNone(db.get_account_by_email("b@example.com"))
        self.assertEqual(db.get_account_by_email("c@example.com")["access_token"], "token-c")
        self.assertEqual(db.count_accounts(), 2)

    def test_new_job_updates_persist_without_cache_reload(self):
        db.sqlite_storage_status()
        job = db.create_job("generic_api")
        db.update_job(
            job["id"],
            status="running",
            email="new@example.com",
            started_at="2026-08-14T01:00:00",
        )
        db.update_job(
            job["id"],
            status="success",
            account_id=99,
            completed_at="2026-08-14T01:02:00",
        )

        db._SQLITE_ROW_CACHE.clear()
        saved = db.get_job(job["id"])
        self.assertEqual(saved["status"], "success")
        self.assertEqual(saved["email"], "new@example.com")
        self.assertEqual(saved["account_id"], 99)
        self.assertEqual(saved["started_at"], "2026-08-14T01:00:00")
        self.assertEqual(saved["completed_at"], "2026-08-14T01:02:00")

    def test_new_account_and_pool_rows_remain_tracked(self):
        db.sqlite_storage_status()

        account_id = db.insert_account(email="new-account@example.com", access_token="token")
        self.assertTrue(db.update_account_note(account_id, "persisted note"))

        inserted, skipped = db.import_generic_api_emails([
            {"email": "new-pool@example.com", "code_url": "https://mail.test/new"},
        ])
        self.assertEqual((inserted, skipped), (1, 0))
        db.release_generic_api_email("new-pool@example.com", status="disabled", note="persisted status")

        db.claim_next_domain_email("new-domain@example.com")
        db.release_domain_email("new-domain@example.com", status="failed", note="persisted domain status")

        db._SQLITE_ROW_CACHE.clear()
        self.assertEqual(db.get_account(account_id)["note"], "persisted note")
        self.assertEqual(db.get_generic_api_email_by_email("new-pool@example.com")["status"], "disabled")
        self.assertEqual(db.get_generic_api_email_by_email("new-pool@example.com")["note"], "persisted status")
        self.assertEqual(db.get_domain_email_by_email("new-domain@example.com")["status"], "failed")
        self.assertEqual(db.get_domain_email_by_email("new-domain@example.com")["note"], "persisted domain status")

    def test_pool_summaries_use_sqlite(self):
        self.assertEqual(db.outlook_pool_summary()["available"], 1)
        self.assertEqual(db.generic_api_email_pool_summary()["used"], 1)
        self.assertEqual(db.list_jobs(limit=10)[0]["status"], "failed")

    def test_migration_is_idempotent(self):
        first = db.sqlite_storage_status()
        db._SQLITE_READY_PATH = None
        db._SQLITE_ROW_CACHE.clear()
        second = db.sqlite_storage_status()
        self.assertEqual(first["counts"], second["counts"])
        self.assertEqual(second["quick_check"], "ok")

    def test_concurrent_account_updates_remain_consistent(self):
        db.sqlite_storage_status()
        errors = []

        def update(account_id, prefix):
            try:
                for index in range(20):
                    db.update_account_note(account_id, f"{prefix}-{index}")
            except Exception as exc:  # pragma: no cover - 断言收集线程异常
                errors.append(exc)

        threads = [
            threading.Thread(target=update, args=(1, "a")),
            threading.Thread(target=update, args=(2, "b")),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        db._SQLITE_ROW_CACHE.clear()
        self.assertEqual(db.get_account(1)["note"], "a-19")
        self.assertEqual(db.get_account(2)["note"], "b-19")
        self.assertEqual(db.sqlite_storage_status()["quick_check"], "ok")


if __name__ == "__main__":
    unittest.main()
