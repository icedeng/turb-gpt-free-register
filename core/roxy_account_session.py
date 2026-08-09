# -*- coding: utf-8 -*-
"""使用临时 Roxy Profile 执行已注册 ChatGPT 账号认证。

该模块与 :mod:`core.roxy_reopen` 共用页面登录、Cookie 恢复和 Session 校验逻辑，
但专门面向后台查活：每次创建独立 Profile，认证完成后关闭并删除，绝不占用用户
在账号页手动打开的 Roxy 环境。
"""
from __future__ import annotations

import logging
import time
from typing import Any

from config import roxybrowser as _cfg
from core.roxybrowser_client import RoxyBrowserClient, RoxyOpenResult, _proxy_url_to_roxy_info

logger = logging.getLogger(__name__)


def _auth_state(account: dict) -> dict:
    from core.roxy_reopen import auth_state_from_account

    state = auth_state_from_account(account)
    return state if isinstance(state, dict) else {}


def _recreate_payload(account: dict, proxy: str | None) -> dict:
    """按账号上次 Roxy 快照创建临时环境，并可覆盖本次网络代理。"""
    from core.roxy_reopen import _profile_recreate_payload

    payload = dict(_profile_recreate_payload(account) or {})
    if proxy is None:
        return payload
    proxy_text = str(proxy or "").strip()
    if proxy_text:
        payload["proxyInfo"] = _proxy_url_to_roxy_info(proxy_text)
    else:
        # 显式空字符串代表直连；不要把账号旧快照里的代理带入本次查活。
        payload.pop("proxyInfo", None)
    return payload


def _restore_saved_state(driver: Any, state: dict) -> int:
    """恢复保存的 Cookie/Storage，返回成功写入的 Cookie 数量。"""
    from core.roxy_reopen import _restore_cookies, _restore_storage

    cookies = state.get("cookies") if isinstance(state, dict) else None
    restored = _restore_cookies(driver, cookies if isinstance(cookies, list) else [])
    storage = state.get("storage") if isinstance(state, dict) else {}
    storage_origin = str(state.get("storage_origin") or "").strip().rstrip("/") if isinstance(state, dict) else ""
    # _restore_cookies 会依次访问 Cookie 所属域，当前页不一定仍是 ChatGPT。
    # Web Storage 必须写入保存时的 origin，否则下次查活无法恢复对应的登录态。
    target_origin = storage_origin or "https://chatgpt.com"
    try:
        driver.get(target_origin + "/")
        _restore_storage(driver, storage if isinstance(storage, dict) else {})
    finally:
        # 后续 /api/auth/session 和页面状态检查必须在 ChatGPT 域执行。
        if "chatgpt.com" not in target_origin:
            driver.get("https://chatgpt.com/")
    return restored


def _cleanup_temp_profile(
    client: RoxyBrowserClient,
    profile_id: str,
    driver: Any,
    *,
    delete_profile: bool = True,
) -> None:
    """尽力关闭并删除临时 Profile；清理失败只记录日志，不覆盖认证结果。"""
    if driver is not None:
        try:
            driver.quit()
        except Exception as exc:
            logger.warning("[Roxy查活] Selenium 连接关闭失败：%s", exc)
    if not profile_id:
        return
    try:
        client.close_profile(profile_id)
    except Exception as exc:
        logger.warning("[Roxy查活] 关闭临时 Profile 失败 profile=%s：%s", profile_id, exc)
    if delete_profile:
        try:
            client.delete_profile(profile_id)
        except Exception as exc:
            logger.warning("[Roxy查活] 删除临时 Profile 失败 profile=%s：%s", profile_id, exc)


def _read_restored_session(driver: Any, read_once, *, timeout: float = 8.0) -> dict | None:
    """Cookie 恢复后短暂等待 ChatGPT 写入 Session，避免因页面初始化延迟误走 OTP。"""
    end = time.time() + max(0.5, float(timeout))
    last_exc: Exception | None = None
    while time.time() < end:
        try:
            session = read_once(driver)
            if isinstance(session, dict) and session.get("accessToken"):
                return session
        except Exception as exc:
            last_exc = exc
        time.sleep(0.5)
    if last_exc is not None:
        logger.debug("[Roxy查活] 等待 Cookie Session 时最后一次读取失败：%s", last_exc)
    return None


def check_account_in_roxy(
    account: dict,
    *,
    proxy: str | None = None,
    headless: bool | None = None,
) -> dict:
    """在临时 Roxy Profile 中登录账号并返回最新 ChatGPT Session。"""
    email = str(account.get("email") or "").strip()
    if not email:
        raise ValueError("账号缺少邮箱，无法使用 Roxy 查活")

    from core.roxy_registration import _build_driver, _read_chatgpt_session_once
    from core.roxy_reopen import (
        _chatgpt_session_is_usable,
        _clear_stale_chatgpt_auth_state,
        _login_account_in_roxy,
        capture_auth_state,
    )

    client = RoxyBrowserClient()
    opened: RoxyOpenResult | None = None
    driver = None
    profile_id = ""
    try:
        payload = _recreate_payload(account, proxy)
        # 后台查活必须创建新环境，不能复用用户手动打开的 Profile，也不能被固定
        # ROXY_PROFILE_ID 影响。先显式 create，再以 reuse_existing 打开。
        profile_id = str(client.create_profile(payload=payload or None) or "").strip()
        if not profile_id:
            raise RuntimeError("Roxy 创建查活 Profile 未返回 profile_id")
        opened = client.open_profile(
            profile_id,
            reuse_existing=True,
            headless=(bool(getattr(_cfg, "ROXY_LIVE_CHECK_HEADLESS", True)) if headless is None else bool(headless)),
        )
        opened.created_by_run = True
        driver = _build_driver(opened)
        try:
            driver.set_page_load_timeout(45)
        except Exception:
            pass
        try:
            driver.set_script_timeout(20)
        except Exception:
            pass

        state = _auth_state(account)
        cookie_session = None
        restored = 0
        cookies = state.get("cookies") if isinstance(state, dict) else None
        if isinstance(cookies, list) and cookies:
            try:
                restored = _restore_saved_state(driver, state)
                driver.get("https://chatgpt.com/")
                driver.refresh()
                cookie_session = _read_restored_session(driver, _read_chatgpt_session_once)
                if cookie_session:
                    usable, reason = _chatgpt_session_is_usable(driver, cookie_session, email)
                    if not usable:
                        logger.info("[Roxy查活] Cookie Session 不可用，转登录：%s", reason)
                        _clear_stale_chatgpt_auth_state(driver)
                        cookie_session = None
            except Exception as exc:
                logger.info("[Roxy查活] Cookie 恢复失败，转登录：%s", exc)
                cookie_session = None

        if cookie_session:
            session_info = cookie_session
            auth_method = "cookie"
        else:
            login_result = _login_account_in_roxy(driver, account)
            session_info = login_result.get("session") or {}
            auth_method = str(login_result.get("method") or "login")

        access_token = str(session_info.get("accessToken") or "").strip()
        if not access_token:
            raise RuntimeError("Roxy 登录成功但 /api/auth/session 缺少 accessToken")
        usable, reason = _chatgpt_session_is_usable(driver, session_info, email)
        if not usable:
            raise RuntimeError(f"Roxy 登录后 ChatGPT Session 不可用: {reason}")

        refreshed_state = capture_auth_state(driver)
        logger.info(
            "[Roxy查活] 正常：email=%s profile=%s auth_method=%s restored_cookies=%s",
            email,
            profile_id,
            auth_method,
            restored,
        )
        return {
            "ok": True,
            "status": "live",
            "access_token": access_token,
            "session": session_info,
            "auth_driver": "roxy",
            "auth_method": auth_method,
            "auth_state": refreshed_state,
            "auth_profile_id": profile_id,
            "proxy_used": proxy or None,
            "restored_cookies": restored,
        }
    finally:
        _cleanup_temp_profile(
            client,
            profile_id,
            driver,
            delete_profile=bool(getattr(_cfg, "ROXY_LIVE_CHECK_DELETE_PROFILE", True)),
        )
