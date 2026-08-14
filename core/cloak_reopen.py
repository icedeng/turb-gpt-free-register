# -*- coding: utf-8 -*-
"""在账号页打开/关闭 CloakBrowser，并恢复已保存的 ChatGPT 登录态。"""
from __future__ import annotations

import logging
import threading
from datetime import datetime
from pathlib import Path

from core.cloak_account_session import _restore_saved_state, _saved_auth_state
from core.cloakbrowser_driver import build_cloak_driver

logger = logging.getLogger(__name__)
_LOG_DIR = Path(__file__).resolve().parent.parent / "注册日志"
_LOCK = threading.RLock()
_RUNNING: set[str] = set()
_OPEN: dict[str, object] = {}


def _key(email: str) -> str:
    return str(email or "").strip().lower()


def log_path(email: str) -> Path:
    safe = str(email or "").replace("/", "_").replace("\\", "_").replace(":", "_")
    return _LOG_DIR / f"cloak-reopen-{safe}.log"


def is_reopening(email: str) -> bool:
    with _LOCK:
        return _key(email) in _RUNNING


def is_open(email: str) -> bool:
    with _LOCK:
        return _key(email) in _OPEN


def _begin_log(email: str):
    key = _key(email)
    if not key:
        raise RuntimeError("账号邮箱为空，无法打开 CloakBrowser")
    with _LOCK:
        if key in _RUNNING:
            raise RuntimeError("该账号正在打开 CloakBrowser")
        _RUNNING.add(key)
    path = log_path(email)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")
    handler = logging.FileHandler(str(path), encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"))
    handler.addFilter(lambda record: record.threadName == threading.current_thread().name)
    logging.getLogger().addHandler(handler)
    return key, handler


def _finish_log(key: str, handler) -> None:
    try:
        logging.getLogger().removeHandler(handler)
        handler.close()
    finally:
        with _LOCK:
            _RUNNING.discard(key)


def reopen_account_in_cloak(account: dict) -> dict:
    """打开 CloakBrowser，Cookie 无效时重新登录；窗口保留到用户手动关闭。"""
    email = str(account.get("email") or "").strip()
    key, handler = _begin_log(email)
    driver = None
    try:
        with _LOCK:
            previous = _OPEN.pop(key, None)
        if previous is not None:
            try:
                previous.quit()
            except Exception:
                pass

        logger.info("[Cloak重新打开] 开始：账号=%s", email)
        driver, opened = build_cloak_driver(proxy=None)
        state = _saved_auth_state(account)
        session_info = None
        restored = 0
        if state.get("cookies"):
            try:
                from core.roxy_registration import _read_chatgpt_session_once
                from core.roxy_reopen import _chatgpt_session_is_usable, _clear_stale_chatgpt_auth_state

                restored = _restore_saved_state(driver, state)
                driver.get("https://chatgpt.com/")
                driver.refresh()
                session_info = _read_chatgpt_session_once(driver)
                if session_info:
                    usable, reason = _chatgpt_session_is_usable(driver, session_info, email)
                    if not usable:
                        logger.info("[Cloak重新打开] Cookie Session 不可用，转登录：%s", reason)
                        _clear_stale_chatgpt_auth_state(driver)
                        session_info = None
            except Exception as exc:
                logger.info("[Cloak重新打开] Cookie 恢复失败，转登录：%s", exc)
                session_info = None

        if session_info:
            auth_method = "cookie"
        else:
            from core.roxy_reopen import _login_account_in_roxy

            login_result = _login_account_in_roxy(driver, account)
            session_info = login_result.get("session") or {}
            auth_method = str(login_result.get("method") or "login")

        from core.roxy_reopen import capture_auth_state

        auth_state = capture_auth_state(driver)
        with _LOCK:
            _OPEN[key] = driver
        logger.info("[Cloak重新打开] 完成：账号=%s auth_method=%s cookies=%s", email, auth_method, restored)
        return {
            "ok": True,
            "profile_id": f"cloak:{key}",
            "restored_cookies": restored,
            "auth_method": auth_method,
            "opened_at": datetime.now().isoformat(timespec="seconds"),
            "_access_token": session_info.get("accessToken") if isinstance(session_info, dict) else None,
            "_auth_state": auth_state,
        }
    except Exception:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass
        logger.exception("[Cloak重新打开] 失败：账号=%s", email)
        raise
    finally:
        _finish_log(key, handler)


def close_account_cloak(account: dict) -> dict:
    email = str(account.get("email") or "").strip()
    key = _key(email)
    with _LOCK:
        driver = _OPEN.pop(key, None)
    if driver is None:
        return {"ok": True, "closed": False, "already_closed": True, "profile_id": f"cloak:{key}"}
    driver.quit()
    logger.info("[Cloak重新打开] 已手动关闭：账号=%s", email)
    return {"ok": True, "closed": True, "already_closed": False, "profile_id": f"cloak:{key}"}
