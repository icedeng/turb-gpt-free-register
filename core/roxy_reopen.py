# -*- coding: utf-8 -*-
"""使用已保存的 ChatGPT 浏览器状态重新打开 RoxyBrowser。"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from core.roxybrowser_client import RoxyBrowserClient, RoxyOpenResult

logger = logging.getLogger(__name__)

_CHATGPT_URL = "https://chatgpt.com/"
_LOG_DIR = Path(__file__).resolve().parent.parent / "注册日志"
_RUNNING: set[str] = set()
_RUNNING_LOCK = threading.Lock()

# /browser/detail 返回的字段中，以下字段可直接用于 /browser/create。
# 其余字段是只读状态（dirId、时间、打开状态等），不能回传创建接口。
_PROFILE_CREATE_FIELDS = (
    "windowName", "coreVersion", "coreType", "os", "osVersion", "userAgent",
    "cookie", "searchEngine", "labelIds", "windowPlatformList", "defaultOpenUrl",
    "windowRemark", "projectId", "proxyInfo", "fingerInfo",
    "position", "openWidth", "openHeight", "openBookmarks", "positionSwitch",
    "windowRatioPosition", "isDisplayName", "syncBookmark", "syncHistory", "syncTab",
    "syncCookie", "syncExtensions", "syncPassword", "syncIndexedDb", "syncLocalStorage",
    "clearCacheFile", "clearCookie", "clearLocalStorage", "randomFingerprint",
    "forbidSavePassword", "stopOpenNet", "stopOpenIP", "stopOpenPosition", "openWorkbench",
    "resolutionType", "resolutionX", "resolutionY", "fontType", "webRTC", "webGL",
    "webGLInfo", "webGLManufacturer", "webGLRender", "webGpu", "canvas", "audioContext",
    "speechVoices", "doNotTrack", "clientRects", "deviceInfo", "deviceNameSwitch", "macInfo",
    "hardwareConcurrent", "deviceMemory", "disableSsl", "disableSslList", "portScanProtect",
    "portScanList", "useGpu", "sandboxPermission",
)
_PROXY_CREATE_FIELDS = (
    "moduleId", "proxyMethod", "proxyCategory", "ipType", "protocol", "host", "port",
    "proxyUserName", "proxyPassword", "refreshUrl", "checkChannel",
)


def _email_key(email: str) -> str:
    return str(email or "").strip().lower()


def log_path(email: str) -> Path:
    safe = str(email or "").replace("/", "_").replace("\\", "_").replace(":", "_")
    return _LOG_DIR / f"roxy-reopen-{safe}.log"


def is_reopening(email: str) -> bool:
    with _RUNNING_LOCK:
        return _email_key(email) in _RUNNING


def _cleanup_reopen_resources(client: RoxyBrowserClient, opened: RoxyOpenResult | None, driver) -> None:
    """重新打开流程结束后保留 Roxy 窗口和 Profile，由用户手动关闭。"""
    # 这里的 driver 只是自动化连接；调用 quit() 会连带关闭用户刚打开的
    # Roxy 窗口，因此必须保持连接对象和 Profile 不动，交给用户手动关闭。
    if opened is not None and opened.profile_id:
        logger.info(
            "[Roxy重新打开] 已保留环境：profile=%s，窗口不会自动关闭，请在 RoxyBrowser 中手动关闭",
            opened.profile_id,
        )


def _begin_reopen_log(email: str) -> tuple[str, logging.FileHandler | None]:
    key = _email_key(email)
    if not key:
        raise RuntimeError("账号邮箱为空，无法创建 Roxy 运行日志")
    with _RUNNING_LOCK:
        if key in _RUNNING:
            raise RuntimeError("该账号正在重新打开 RoxyBrowser，请先查看当前运行日志")
        _RUNNING.add(key)
    handler: logging.FileHandler | None = None
    try:
        path = log_path(email)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
        handler = logging.FileHandler(str(path), encoding="utf-8")
        handler.setLevel(logging.DEBUG)
        handler.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(message)s",
            datefmt="%H:%M:%S",
        ))
        thread_name = threading.current_thread().name
        handler.addFilter(lambda record: record.threadName == thread_name)
        logging.getLogger().addHandler(handler)
    except Exception:
        # 日志文件失败不应阻断 Roxy 操作；系统日志仍会保留同一条记录。
        logging.getLogger(__name__).exception("创建 Roxy 重新打开日志失败：email=%s", email)
    return key, handler


def _finish_reopen_log(key: str, handler: logging.FileHandler | None) -> None:
    if handler is not None:
        try:
            logging.getLogger().removeHandler(handler)
            handler.close()
        except Exception:
            pass
    with _RUNNING_LOCK:
        _RUNNING.discard(key)


def capture_auth_state(driver) -> dict:
    """从注册成功的 Selenium 会话中提取可恢复登录态的 Cookie 和 Web Storage。"""
    cookies: list[dict] = []
    try:
        # CDP 能拿到当前 profile 的全部域 Cookie；比 get_cookies() 只返回当前域更完整。
        payload = driver.execute_cdp_cmd("Network.getAllCookies", {})
        raw_cookies = payload.get("cookies") if isinstance(payload, dict) else None
        if isinstance(raw_cookies, list):
            cookies = [x for x in raw_cookies if isinstance(x, dict)]
    except Exception:
        pass
    if not cookies:
        try:
            raw_cookies = driver.get_cookies() or []
            cookies = [x for x in raw_cookies if isinstance(x, dict)]
        except Exception:
            cookies = []

    storage: dict[str, dict[str, str]] = {"local": {}, "session": {}}
    storage_origin = ""
    try:
        current_url = str(getattr(driver, "current_url", "") or "")
        parsed = urlparse(current_url)
        if parsed.scheme and parsed.netloc:
            storage_origin = f"{parsed.scheme}://{parsed.netloc}"
    except Exception:
        pass
    try:
        raw_storage = driver.execute_script(
            """
            const dump = (s) => {
              const out = {};
              for (let i = 0; i < s.length; i++) {
                const k = s.key(i);
                if (k !== null) out[k] = String(s.getItem(k) ?? '');
              }
              return out;
            };
            return {local: dump(window.localStorage), session: dump(window.sessionStorage)};
            """
        )
        if isinstance(raw_storage, dict):
            for kind in ("local", "session"):
                values = raw_storage.get(kind)
                if isinstance(values, dict):
                    # 避免把异常的大型站点缓存写入账号文件；登录相关值通常远小于此限制。
                    storage[kind] = {
                        str(k): str(v)[:200_000]
                        for k, v in values.items()
                        if str(k) and len(str(v)) <= 200_000
                    }
    except Exception:
        pass

    return {
        "captured_at": datetime.now().isoformat(timespec="seconds"),
        "cookies": cookies,
        "storage": storage,
        "storage_origin": storage_origin,
    }


def auth_state_from_account(account: dict) -> dict:
    """读取账号 extra_json 中保存的 Roxy 恢复信息。"""
    raw = account.get("extra_json")
    if not raw:
        return {}
    try:
        extra = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return {}
    if not isinstance(extra, dict):
        return {}
    state = extra.get("roxy_auth_state")
    return state if isinstance(state, dict) else {}


def _extra_from_account(account: dict) -> dict:
    raw = account.get("extra_json")
    try:
        extra = json.loads(raw) if isinstance(raw, str) and raw else (raw if isinstance(raw, dict) else {})
    except Exception:
        extra = {}
    return extra if isinstance(extra, dict) else {}


def _profile_detail_from_account(account: dict) -> dict:
    extra = _extra_from_account(account)
    roxy = extra.get("roxybrowser")
    if not isinstance(roxy, dict):
        return {}
    detail = roxy.get("profile_detail")
    return detail if isinstance(detail, dict) else {}


def _profile_recreate_payload(account: dict) -> dict:
    """把 /browser/detail 快照裁剪成 /browser/create 可接受的请求体。"""
    detail = _profile_detail_from_account(account)
    if not detail:
        return {}
    payload = {key: detail[key] for key in _PROFILE_CREATE_FIELDS if key in detail}
    if detail.get("windowName"):
        # 兼容旧版客户端使用的 name 字段，同时提交官方 windowName。
        payload.setdefault("name", detail["windowName"])
    proxy = detail.get("proxyInfo")
    if isinstance(proxy, dict):
        payload["proxyInfo"] = {key: proxy[key] for key in _PROXY_CREATE_FIELDS if key in proxy}
    try:
        from config import roxybrowser as roxy_cfg
        payload["workspaceId"] = int(roxy_cfg.ROXY_WORKSPACE_ID) if str(roxy_cfg.ROXY_WORKSPACE_ID).isdigit() else roxy_cfg.ROXY_WORKSPACE_ID
        project_id = str(getattr(roxy_cfg, "ROXY_PROJECT_ID", "") or "").strip()
        if project_id:
            payload["projectId"] = int(project_id) if project_id.isdigit() else project_id
    except Exception:
        pass
    return payload


def _cookie_host(cookie: dict) -> str:
    domain = str(cookie.get("domain") or "chatgpt.com").lstrip(".").strip()
    return domain or "chatgpt.com"


def _cookie_for_selenium(cookie: dict) -> dict:
    allowed = {"name", "value", "path", "domain", "secure", "httpOnly", "expiry", "sameSite"}
    out = {k: cookie[k] for k in allowed if k in cookie and cookie[k] is not None}
    if "expiry" not in out and cookie.get("expires") not in (None, 0, -1):
        out["expiry"] = cookie.get("expires")
    out["name"] = str(out.get("name") or "")
    out["value"] = str(out.get("value") or "")
    if not out["name"]:
        raise ValueError("Cookie 缺少名称")
    if "expiry" in out:
        try:
            out["expiry"] = int(float(out["expiry"]))
        except (TypeError, ValueError):
            out.pop("expiry", None)
    same_site = str(out.get("sameSite") or "").capitalize()
    if same_site in {"Lax", "Strict", "None"}:
        out["sameSite"] = same_site
    else:
        out.pop("sameSite", None)
    return out


def _restore_storage(driver, storage: dict) -> None:
    for kind, script in (
        ("local", "window.localStorage.setItem(arguments[0], arguments[1]);"),
        ("session", "window.sessionStorage.setItem(arguments[0], arguments[1]);"),
    ):
        values = storage.get(kind) if isinstance(storage, dict) else None
        if not isinstance(values, dict):
            continue
        for key, value in values.items():
            try:
                driver.execute_script(script, str(key), str(value))
            except Exception as exc:
                logger.debug("恢复 %sStorage 失败 key=%s: %s", kind, key, exc)


def _restore_cookies(driver, cookies: list[dict]) -> int:
    restored = 0
    by_host: dict[str, list[dict]] = {}
    for raw in cookies:
        if not isinstance(raw, dict):
            continue
        try:
            cookie = _cookie_for_selenium(raw)
        except ValueError:
            continue
        by_host.setdefault(_cookie_host(cookie), []).append(cookie)

    for host, host_cookies in by_host.items():
        # Selenium 只能向当前域写入 Cookie；先访问该域再写入。
        scheme = "https" if host not in {"localhost", "127.0.0.1"} else "http"
        driver.get(f"{scheme}://{host}/")
        for cookie in host_cookies:
            try:
                driver.add_cookie(cookie)
                restored += 1
            except Exception as exc:
                logger.debug("恢复 Cookie 失败 domain=%s name=%s: %s", host, cookie.get("name"), exc)
    return restored


def _fill_login_password(driver, password: str) -> None:
    """填写 auth.openai.com 登录密码页，不依赖按钮文字。"""
    from core.roxy_registration import _human_click, _human_type_text, _password_page_state

    result = driver.execute_script(
        """
        const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
          && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none'
          && !el.disabled && !el.readOnly;
        const input = [...document.querySelectorAll('input[type=password],input[name*=password i]')].find(visible);
        if (!input) return {ok:false, reason:'missing_password_input'};
        const form = input.closest('form');
        const scope = form || document;
        const button = [...scope.querySelectorAll('button,input[type=submit]')].find(visible);
        if (!button) return {ok:false, reason:'missing_password_submit'};
        return {ok:true, input, button};
        """
    ) or {}
    if not result.get("ok"):
        raise RuntimeError(f"登录密码页处理失败：{result} state={_password_page_state(driver)}")
    _human_type_text(driver, result["input"], password, clear=True)
    _human_click(driver, result["button"], label="login_password_submit")


def _wait_login_next_state(driver, timeout: int = 30) -> str:
    from core.roxy_registration import (
        _has_access_token,
        _is_email_verification_page,
        _is_login_password_page,
    )

    end = time.time() + timeout
    last = "unknown"
    while time.time() < end:
        if _has_access_token(driver):
            return "logged_in"
        if _is_email_verification_page(driver):
            return "otp"
        if _is_login_password_page(driver):
            last = "login_password"
        else:
            last = "unknown"
        time.sleep(0.5)
    return last


def _login_account_in_roxy(driver, account: dict) -> dict:
    """Cookie 失效时，在当前 Roxy 窗口执行已注册账号登录。"""
    from core.email_provider import wait_for_otp
    from core.roxy_registration import (
        _click_continue,
        _click_passwordless_signup_if_present,
        _click_resend_email_otp,
        _clear_otp_inputs,
        _fetch_chatgpt_session,
        _maybe_accept,
        _safe_get,
        _submit_email_step,
        _type_email_address,
        _type_otp,
        _wait_after_email_otp_submit,
        _wait_email_submit_next_state,
    )

    email = str(account.get("email") or "").strip()
    if not email:
        raise RuntimeError("账号缺少邮箱，无法重新登录")
    extra = _extra_from_account(account)
    password = str(extra.get("registration_password") or "").strip()
    login_method = "email_otp"
    otp_after_ts = time.time()
    _safe_get(
        driver,
        "https://chatgpt.com/auth/login",
        timeout=45,
        attempts=2,
        accept_hosts=("chatgpt.com", "auth.openai.com"),
    )
    _maybe_accept(driver)
    _type_email_address(driver, email, timeout=25)
    _submit_email_step(driver, email)
    state = _wait_email_submit_next_state(driver, email, timeout=30)
    if state == "logged_in":
        session = _fetch_chatgpt_session(driver, timeout=30, auto_jump_wait=5)
        return {"session": session, "method": "email_cookie_or_existing"}

    # 优先使用注册时保存的密码；没有密码或密码页没有可用密码时再尝试一次性验证码。
    if state in {"login_password", "password"} and password:
        _fill_login_password(driver, password)
        login_method = "password"
        state = _wait_login_next_state(driver, timeout=30)
        if state == "login_password":
            passwordless = _click_passwordless_signup_if_present(driver)
            if passwordless.get("ok"):
                login_method = "email_otp"
                state = _wait_login_next_state(driver, timeout=30)
            else:
                raise RuntimeError("保存的登录密码未能完成登录，且页面没有一次性验证码入口")
    elif state in {"login_password", "password"}:
        passwordless = _click_passwordless_signup_if_present(driver)
        if passwordless.get("ok"):
            state = _wait_login_next_state(driver, timeout=30)
        else:
            raise RuntimeError("账号进入登录密码页，但没有保存密码，也没有找到一次性验证码入口")

    if state == "otp":
        for attempt in range(1, 4):
            try:
                code = wait_for_otp(email, after_ts=otp_after_ts)
                _clear_otp_inputs(driver)
                _type_otp(driver, code)
                try:
                    _click_continue(driver)
                except Exception:
                    pass
                outcome = _wait_after_email_otp_submit(driver, timeout=15)
                if outcome == "accepted":
                    break
            except Exception:
                if attempt >= 3:
                    raise
            if attempt < 3:
                otp_after_ts = time.time()
                _click_resend_email_otp(driver, timeout=25)
        else:
            raise RuntimeError("邮箱验证码连续失败，无法重新登录")

    if state not in {"logged_in", "otp"}:
        # 密码提交后可能没有及时被状态轮询捕获，交给 session 检查做最后确认。
        logger.info("Roxy 登录状态未明确收敛，继续读取 ChatGPT session：state=%s", state)
    session = _fetch_chatgpt_session(driver, timeout=60, auto_jump_wait=8)
    return {"session": session, "method": login_method}


def reopen_account_in_roxy(account: dict) -> dict:
    """创建/复用 Roxy 环境并恢复登录态，完成后保留窗口供用户使用。"""
    email = str(account.get("email") or "").strip()
    log_key, log_handler = _begin_reopen_log(email)
    logger.info("[Roxy重新打开] 开始：账号=%s", email)
    state = auth_state_from_account(account)
    cookies = state.get("cookies") if isinstance(state, dict) else None
    if not isinstance(cookies, list):
        cookies = []

    from core.roxy_registration import _build_driver, _center_browser_window

    client = RoxyBrowserClient()
    reopen = state.get("reopen") if isinstance(state.get("reopen"), dict) else {}
    previous_profile_id = str(reopen.get("profile_id") or "").strip()
    opened: RoxyOpenResult | None = None
    driver = None
    created = False
    try:
        if previous_profile_id:
            try:
                opened = client.open_profile(previous_profile_id, reuse_existing=True)
            except Exception as exc:
                logger.info("旧 Roxy 环境不可复用，将创建新环境 profile=%s: %s", previous_profile_id, exc)
                opened = None
        if opened is None:
            recreate_payload = _profile_recreate_payload(account)
            if recreate_payload:
                logger.info("[Roxy重新打开] 原 Profile 不可复用，按环境快照重建：fields=%s", len(recreate_payload))
            else:
                logger.info("[Roxy重新打开] 没有可用环境快照，使用默认配置创建新 Profile")
            opened = client.open_profile(create_payload=recreate_payload or None)
            created = bool(opened.created_by_run)
        logger.info("[Roxy重新打开] Roxy 环境已打开：profile=%s created=%s", opened.profile_id, created)
        driver = _build_driver(opened)
        _center_browser_window(driver)
        driver.set_page_load_timeout(45)
        auth_method = "cookie"
        restored = 0
        cookie_session = None
        if cookies:
            try:
                restored = _restore_cookies(driver, cookies)
                driver.get(_CHATGPT_URL)
                storage_origin = str(state.get("storage_origin") or "").strip().rstrip("/")
                if storage_origin and "chatgpt.com" not in storage_origin:
                    driver.get(storage_origin + "/")
                    _restore_storage(driver, state.get("storage") or {})
                    driver.get(_CHATGPT_URL)
                else:
                    _restore_storage(driver, state.get("storage") or {})
                driver.refresh()
                time.sleep(1)
                current_url = getattr(driver, "current_url", "") or _CHATGPT_URL
                if "/auth/login" not in str(current_url).lower() and "/log-in" not in str(current_url).lower():
                    from core.roxy_registration import _read_chatgpt_session_once
                    cookie_session = _read_chatgpt_session_once(driver)
                if not cookie_session:
                    logger.info("[Roxy重新打开] Cookie 未恢复有效 session，将重新走登录流程：%s", account.get("email"))
            except Exception as exc:
                logger.warning("[Roxy重新打开] Cookie 恢复失败，将重新走登录流程：%s", exc)
        else:
            logger.info("[Roxy重新打开] 未找到 Cookie 快照，将直接走账号登录流程：%s", email)
        if not cookie_session:
            logger.info("[Roxy重新打开] 开始账号登录流程：%s", email)
            login_result = _login_account_in_roxy(driver, account)
            auth_method = str(login_result.get("method") or "login")
            active_session = login_result.get("session") or {}
            current_url = getattr(driver, "current_url", "") or _CHATGPT_URL
        else:
            active_session = cookie_session
        refreshed_state = capture_auth_state(driver)
        profile_detail = {}
        try:
            profile_detail = client.get_profile_detail(opened.profile_id)
            logger.info("[Roxy重新打开] 已刷新当前 Profile 环境快照：fields=%s", len(profile_detail))
        except Exception as exc:
            logger.warning("[Roxy重新打开] 获取当前 Profile 环境快照失败，保留旧快照：%s", exc)
        logger.info("[Roxy重新打开] 账号=%s profile=%s cookies=%s", account.get("email"), opened.profile_id, restored)
        logger.info("[Roxy重新打开] 完成：账号=%s auth_method=%s url=%s", email, auth_method, current_url)
        return {
            "ok": True,
            "profile_id": opened.profile_id,
            "restored_cookies": restored,
            "url": current_url,
            "created": created,
            "auth_method": auth_method,
            "_access_token": active_session.get("accessToken") if isinstance(active_session, dict) else None,
            "_auth_state": refreshed_state if refreshed_state.get("cookies") else state,
            "_profile_detail": profile_detail,
        }
    except Exception as exc:
        logger.exception("[Roxy重新打开] 失败：账号=%s error=%s", email, exc)
        raise
    finally:
        _cleanup_reopen_resources(client, opened, driver)
        _finish_reopen_log(log_key, log_handler)
