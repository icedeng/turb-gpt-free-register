#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""初始化、校验 SQLite 主存储，并按需导出旧版兼容文件。"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import db  # noqa: E402


def _backup_sources() -> Path:
    backup = ROOT / "data-backups" / f"pre-sqlite-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    backup.mkdir(parents=True, exist_ok=False)
    paths = db.storage_paths()
    for key in ("accounts_json", "outlook_json", "jobs_json"):
        source = Path(paths[key])
        if source.exists():
            shutil.copy2(source, backup / source.name)
    for name in ("用于注册的API邮箱.json", "用于注册的域名邮箱.json"):
        source = ROOT / name
        if source.exists():
            shutil.copy2(source, backup / source.name)
    return backup


def main() -> int:
    parser = argparse.ArgumentParser(description="迁移并检查 turb-gpt SQLite 主存储")
    parser.add_argument("--backup", action="store_true", help="初始化前备份原 JSON 数据")
    parser.add_argument("--export", action="store_true", help="从 SQLite 重新生成兼容 JSON/TXT/HTML")
    args = parser.parse_args()

    sqlite_path = Path(db.storage_paths()["sqlite"])
    backup = _backup_sources() if args.backup and not sqlite_path.exists() else None
    status = db.sqlite_storage_status()
    # 以 root 手动迁移时，让数据库继承账号 JSON 的服务用户所有权。
    owner_source = Path(db.storage_paths()["accounts_json"])
    if owner_source.exists() and sqlite_path.exists():
        source_stat = owner_source.stat()
        for path in (sqlite_path, Path(str(sqlite_path) + "-wal"), Path(str(sqlite_path) + "-shm")):
            if path.exists():
                try:
                    os.chown(path, source_stat.st_uid, source_stat.st_gid)
                except PermissionError:
                    pass
    result = {"backup": str(backup) if backup else None, "status": status}
    if args.export:
        result["export"] = db.export_compatibility_files(include_viewer=True)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if status.get("quick_check") == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
