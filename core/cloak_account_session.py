# -*- coding: utf-8 -*-
"""使用临时 CloakBrowser 环境执行已注册 ChatGPT 账号认证。"""
from __future__ import annotations

import logging
import time

from core.cloakbrowser_driver import build_cloak_driver

logger = logging.getLogger(__name__)


def _saved_auth_state(account: dict) -> dict:
    from core.roxy_reopen import auth_state_from_account

    state = auth_state_from_account(account)
    return state if isinstance(state, dict) else {}


def _restore_saved_state(driver, state: dict) -> int:
    from core.roxy_reopen import _restore_storage

    cookies = state.get("cookies") if isinstance(state, dict) else []
    restored = 0
    if isinstance(cookies, list) and driver.context is not None:
        usable = []
        for raw in cookies:
            if not isinstance(raw, dict) or not raw.get("name"):
                continue
            allowed = {"name", "value", "url", "domain", "path", "expires", "httpOnly", "secure", "sameSite"}
            item = {key: value for key, value in dict(raw).items() if key in allowed}
            same_site = str(item.get("sameSite") or "").strip().lower()
            if same_site:
                normalized = {"lax": "Lax", "strict": "Strict", "none": "None", "no_restriction": "None"}.get(same_site)
                if normalized:
                    item["sameSite"] = normalized
                else:
                    item.pop("sameSite", None)
            usable.append(item)
        if usable:
            driver.context.add_cookies(usable)
            restored = len(usable)
    origin = str(state.get("storage_origin") or "https://chatgpt.com").rstrip("/")
    driver.get(origin + "/")
    _restore_storage(driver, state.get("storage") if isinstance(state.get("storage"), dict) else {})
    if "chatgpt.com" not in origin:
        driver.get("https://chatgpt.com/")
    return restored


def _read_session(driver, read_once, timeout: float = 8.0) -> dict | None:
    end = time.time() + max(0.5, timeout)
    while time.time() < end:
        session = read_once(driver)
        if isinstance(session, dict) and session.get("accessToken"):
            return session
        time.sleep(0.5)
    return None


def check_account_in_cloak(account: dict, *, proxy: str | None = None) -> dict:
    """在临时 CloakBrowser 中恢复登录态，失效时自动走密码/邮箱 OTP。"""
    email = str(account.get("email") or "").strip()
    if not email:
        raise ValueError("账号缺少邮箱，无法使用 CloakBrowser 查活")

    from core.roxy_registration import _read_chatgpt_session_once
    from core.roxy_reopen import (
        _chatgpt_session_is_usable,
        _clear_stale_chatgpt_auth_state,
        _login_account_in_roxy,
        capture_auth_state,
    )

    driver = None
    try:
        driver, opened = build_cloak_driver(proxy=proxy)
        state = _saved_auth_state(account)
        session_info = None
        restored = 0
        if state.get("cookies"):
            try:
                restored = _restore_saved_state(driver, state)
                driver.get("https://chatgpt.com/")
                driver.refresh()
                session_info = _read_session(driver, _read_chatgpt_session_once)
                if session_info:
                    usable, reason = _chatgpt_session_is_usable(driver, session_info, email)
                    if not usable:
                        logger.info("[Cloak查活] Cookie Session 不可用，转登录：%s", reason)
                        _clear_stale_chatgpt_auth_state(driver)
                        session_info = None
            except Exception as exc:
                logger.info("[Cloak查活] Cookie 恢复失败，转登录：%s", exc)
                session_info = None

        if session_info:
            auth_method = "cookie"
        else:
            login_result = _login_account_in_roxy(driver, account)
            session_info = login_result.get("session") or {}
            auth_method = str(login_result.get("method") or "login")

        access_token = str(session_info.get("accessToken") or "").strip()
        if not access_token:
            raise RuntimeError("CloakBrowser 登录成功但 /api/auth/session 缺少 accessToken")
        usable, reason = _chatgpt_session_is_usable(driver, session_info, email)
        if not usable:
            raise RuntimeError(f"CloakBrowser 登录后 ChatGPT Session 不可用: {reason}")
        return {
            "ok": True,
            "status": "live",
            "access_token": access_token,
            "session": session_info,
            "auth_driver": "cloak",
            "auth_method": auth_method,
            "auth_state": capture_auth_state(driver),
            "auth_profile_id": str(getattr(opened, "profile_id", "cloakbrowser") or "cloakbrowser"),
            "proxy_used": ((opened.raw or {}).get("proxy") if opened else None) or (proxy or None),
            "restored_cookies": restored,
        }
    finally:
        if driver is not None:
            driver.quit()
