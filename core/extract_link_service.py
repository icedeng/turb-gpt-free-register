# -*- coding: utf-8 -*-
"""Plus 试用提链后台队列。"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import ProxyHandler, Request, build_opener, urlopen

try:
    from curl_cffi import requests as curl_requests
except Exception:  # WebUI 环境未装 curl_cffi 时使用标准库兜底
    curl_requests = None

from config import extract_link as cfg
from core import db

logger = logging.getLogger(__name__)


def _runtime_setting(name: str, default=None):
    """
    提链配置多数保存在 .env。服务模块会在 WebUI 启动时较早 import，
    因此每次实际读取时都重新加载 .env，避免“页面已保存但当前进程仍读到空值”。
    """
    try:
        from config.env_loader import load_env
        load_env(override=True)
    except Exception:
        pass
    raw = os.getenv(name)
    if raw is not None and str(raw).strip() != "":
        return str(raw).strip()
    return getattr(cfg, name, default)


def _int_setting(name: str, default: int, lower: int, upper: int) -> int:
    try:
        value = int(_runtime_setting(name, default) or default)
    except (TypeError, ValueError):
        value = default
    return max(lower, min(upper, value))


SUPPORTED_LINK_TYPES = {"pix", "upi", "kakao_pay", "ideal", "paypal_zero"}


def _link_type(value: str | None = None) -> str:
    t = str(value or _runtime_setting("EXTRACT_LINK_TYPE", "pix") or "pix").strip().lower()
    if t not in SUPPORTED_LINK_TYPES:
        raise ValueError("提链类型无效，仅支持 pix / upi / kakao_pay / ideal / paypal_zero")
    return t


def _api_base() -> str:
    base = str(_runtime_setting("EXTRACT_LINK_API_BASE", "") or "").strip().rstrip("/")
    if not base:
        raise ValueError("EXTRACT_LINK_API_BASE 为空")
    return base


def _cdk(value: str | None = None) -> str:
    cdk = str(value or _runtime_setting("EXTRACT_LINK_CDK", "") or "").strip()
    if not cdk:
        raise ValueError("EXTRACT_LINK_CDK/CDK 为空")
    return cdk


def _paypal_zero_api_base() -> str:
    base = str(_runtime_setting("PAYPAL_ZERO_API_BASE", "http://127.0.0.1:5572") or "").strip().rstrip("/")
    if not base:
        raise ValueError("PAYPAL_ZERO_API_BASE 为空")
    return base


def _paypal_zero_proxy_pool() -> list[str]:
    raw = _runtime_setting("PAYPAL_ZERO_PROXY_POOL", [])
    if isinstance(raw, str):
        rows = raw.splitlines()
    else:
        rows = list(raw or [])
    proxies = [str(item or "").strip() for item in rows if str(item or "").strip() and not str(item).strip().startswith("#")]
    if not proxies:
        raise ValueError("PAYPAL_ZERO_PROXY_POOL 为空；PayPal 0 元提链需要巴西出口代理")
    return proxies


def _request_json(method: str, url: str, *, payload: dict | None = None, timeout: int = 30) -> dict:
    """直连提链服务并返回 JSON，不继承宿主机的 HTTP 代理。"""
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = Request(
        url,
        data=body,
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        method=method,
    )
    try:
        with build_opener(ProxyHandler({})).open(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except HTTPError as exc:
        try:
            data = json.loads(exc.read().decode("utf-8", "replace") or "{}")
        except Exception:
            data = {}
        raise RuntimeError(_extract_error_message(data) or f"HTTP {exc.code}") from exc
    try:
        data = json.loads(raw or "{}")
    except Exception as exc:
        raise RuntimeError(f"提链服务响应不是有效 JSON: {raw[:300]}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"提链服务响应不是 JSON 对象: {type(data).__name__}")
    return data


def _create_paypal_zero_job(*, token: str) -> dict:
    payload = {
        "access_token": token,
        "country": str(_runtime_setting("PAYPAL_ZERO_PROXY_COUNTRY", "BR") or "BR").strip().upper(),
        "proxies": "\n".join(_paypal_zero_proxy_pool()),
        "proxy_scheme": str(_runtime_setting("PAYPAL_ZERO_PROXY_SCHEME", "http") or "http").strip().lower(),
        "checkout_attempts": _int_setting("PAYPAL_ZERO_CHECKOUT_ATTEMPTS", 5, 1, 50),
        "provider_attempts": _int_setting("PAYPAL_ZERO_PROVIDER_ATTEMPTS", 10, 1, 100),
    }
    data = _request_json(
        "POST",
        f"{_paypal_zero_api_base()}/api/jobs",
        payload=payload,
        timeout=_int_setting("EXTRACT_LINK_REQUEST_TIMEOUT", 30, 5, 300),
    )
    if not data.get("job_id"):
        raise RuntimeError(f"link-pp 未返回 job_id: {data}")
    return data


def _paypal_zero_result(payload: dict) -> dict:
    """把 link-pp 结果映射为现有提链存储/前端字段。"""
    result = dict(payload or {})
    approve_url = str(result.get("paypal_approve_url") or result.get("provider_redirect_url") or result.get("checkout_url") or "")
    result.update({
        "long_url": approve_url,
        "copy_paste": approve_url,
        "payment_method": "paypal",
        "payment_link_type": "paypal_zero",
    })
    return result


def _cancel_paypal_zero_job(job_id: str) -> None:
    """父任务异常结束时停止 link-pp 子任务，避免继续占用代理。"""
    try:
        _request_json(
            "POST",
            f"{_paypal_zero_api_base()}/api/jobs/{quote(job_id, safe='')}/cancel",
            payload={},
            timeout=_int_setting("EXTRACT_LINK_REQUEST_TIMEOUT", 30, 5, 300),
        )
    except Exception:
        logger.warning("[提链] 取消 link-pp 任务失败: job=%s", job_id, exc_info=True)


def _run_paypal_zero_extract(*, account_id: int, email: str, access_token: str, trigger: str) -> dict:
    if not db.mark_account_extract_running(account_id):
        return {"ok": False, "error": "账号已删除或提链状态已被重置"}
    created = _create_paypal_zero_job(token=access_token)
    job_id = str(created.get("job_id") or "")
    db.update_account_extract(account_id, {
        "ok": False,
        "status": "running",
        "job_id": job_id,
        "link_type": "paypal_zero",
        "message": "PayPal 0 元提链任务已创建，等待结果",
    })
    timeout = _int_setting("EXTRACT_LINK_EVENT_TIMEOUT", 180, 30, 1800)
    deadline = time.monotonic() + timeout
    last_status = ""
    try:
        while time.monotonic() < deadline:
            snapshot = _request_json(
                "GET",
                f"{_paypal_zero_api_base()}/api/jobs/{quote(job_id, safe='')}",
                timeout=_int_setting("EXTRACT_LINK_REQUEST_TIMEOUT", 30, 5, 300),
            )
            status = str(snapshot.get("status") or "")
            if status != last_status:
                last_status = status
                db.update_account_extract(account_id, {
                    "ok": False,
                    "status": "running",
                    "job_id": job_id,
                    "link_type": "paypal_zero",
                    "message": f"PayPal 0 元提链：{status or 'running'}",
                })
            if status == "success":
                result = _paypal_zero_result(snapshot.get("result") if isinstance(snapshot.get("result"), dict) else {})
                if not result.get("long_url"):
                    raise RuntimeError("link-pp 成功但未返回 PayPal 链接")
                final = {"ok": True, "status": "success", "job_id": job_id, "link_type": "paypal_zero", "result": result}
                db.update_account_extract(account_id, final)
                logger.info("[提链] PayPal 0 元提链成功: %s job=%s", email, job_id)
                return final
            if status in {"failed", "cancelled"}:
                raise RuntimeError(str(snapshot.get("failure_reason") or snapshot.get("error") or f"link-pp 任务 {status}"))
            time.sleep(2)
        raise TimeoutError(f"等待 link-pp 任务结果超时（{timeout}s）")
    except Exception:
        if last_status not in {"failed", "cancelled"}:
            _cancel_paypal_zero_job(job_id)
        raise


_WORKERS = _int_setting("EXTRACT_LINK_WORKERS", 3, 1, 16)
_QUEUE_LIMIT = _int_setting("EXTRACT_LINK_QUEUE_LIMIT", 500, _WORKERS, 5000)
_EXECUTOR = ThreadPoolExecutor(max_workers=_WORKERS, thread_name_prefix="extract-link")
_QUEUE_SLOTS = threading.BoundedSemaphore(_QUEUE_LIMIT)


def queue_settings() -> dict:
    return {"workers": _WORKERS, "queue_limit": _QUEUE_LIMIT}


def _session():
    if curl_requests is None:
        return None
    return curl_requests.Session()


def query_cdk(*, cdk: str | None = None) -> dict:
    base = _api_base()
    code = _cdk(cdk)
    timeout = _int_setting("EXTRACT_LINK_REQUEST_TIMEOUT", 30, 5, 300)
    s = _session()
    try:
        if s is None:
            req = Request(f"{base}/api/cdk?{urlencode({'code': code})}", headers={"Accept": "application/json"})
            with urlopen(req, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8", "replace") or "{}")
            return payload if isinstance(payload, dict) else {}
        resp = s.get(f"{base}/api/cdk?{urlencode({'code': code})}", timeout=timeout)
        try:
            payload = resp.json()
        except Exception:
            payload = {"error": (resp.text or "")[:300]}
        if resp.status_code < 200 or resp.status_code >= 300:
            raise RuntimeError(payload.get("error") or f"HTTP {resp.status_code}")
        return payload if isinstance(payload, dict) else {}
    finally:
        try:
            s.close()
        except Exception:
            pass


def _create_extract_job(*, token: str, link_type: str, cdk: str) -> dict:
    base = _api_base()
    timeout = _int_setting("EXTRACT_LINK_REQUEST_TIMEOUT", 30, 5, 300)
    payload = {"link_type": _link_type(link_type), "cdk": _cdk(cdk), "token": token}
    s = _session()
    try:
        if s is None:
            body = json.dumps(payload).encode("utf-8")
            req = Request(
                f"{base}/api/extract",
                data=body,
                headers={"Accept": "application/json", "Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace") or "{}")
            if not isinstance(data, dict) or not data.get("job_id"):
                raise RuntimeError(f"提链服务未返回 job_id: {data}")
            return data
        resp = s.post(f"{base}/api/extract", json=payload, timeout=timeout)
        try:
            data = resp.json()
        except Exception:
            data = {"error": (resp.text or "")[:300]}
        if resp.status_code < 200 or resp.status_code >= 300:
            raise RuntimeError(data.get("error") or f"HTTP {resp.status_code}")
        if not isinstance(data, dict) or not data.get("job_id"):
            raise RuntimeError(f"提链服务未返回 job_id: {data}")
        return data
    finally:
        try:
            s.close()
        except Exception:
            pass


def _iter_sse_events(*, job_id: str, cdk: str):
    base = _api_base()
    timeout = _int_setting("EXTRACT_LINK_EVENT_TIMEOUT", 180, 30, 900)
    url = f"{base}/api/jobs/{quote(job_id, safe='')}/events?{urlencode({'cdk': _cdk(cdk)})}"
    s = _session()
    try:
        if s is None:
            req = Request(url, headers={"Accept": "text/event-stream"})
            with urlopen(req, timeout=timeout) as resp:
                event = "message"
                data_lines: list[str] = []
                for raw in resp:
                    line = raw.decode("utf-8", "replace").rstrip("\r\n")
                    if line == "":
                        if data_lines:
                            text = "\n".join(data_lines)
                            try:
                                data = json.loads(text)
                            except Exception:
                                data = {"raw": text}
                            yield event, data
                        event = "message"
                        data_lines = []
                        continue
                    if line.startswith(":"):
                        continue
                    if line.startswith("event:"):
                        event = line.split(":", 1)[1].strip() or "message"
                    elif line.startswith("data:"):
                        data_lines.append(line.split(":", 1)[1].lstrip())
                if data_lines:
                    text = "\n".join(data_lines)
                    try:
                        data = json.loads(text)
                    except Exception:
                        data = {"raw": text}
                    yield event, data
            return
        resp = s.get(url, timeout=timeout, stream=True)
        if resp.status_code < 200 or resp.status_code >= 300:
            raise RuntimeError(f"监听提链事件失败 HTTP {resp.status_code}: {(resp.text or '')[:300]}")
        event = "message"
        data_lines: list[str] = []
        for raw in resp.iter_lines():
            if raw is None:
                continue
            if isinstance(raw, bytes):
                line = raw.decode("utf-8", "replace")
            else:
                line = str(raw)
            line = line.rstrip("\r")
            if line == "":
                if data_lines:
                    text = "\n".join(data_lines)
                    try:
                        data = json.loads(text)
                    except Exception:
                        data = {"raw": text}
                    yield event, data
                event = "message"
                data_lines = []
                continue
            if line.startswith(":"):
                continue
            if line.startswith("event:"):
                event = line.split(":", 1)[1].strip() or "message"
            elif line.startswith("data:"):
                data_lines.append(line.split(":", 1)[1].lstrip())
        if data_lines:
            text = "\n".join(data_lines)
            try:
                data = json.loads(text)
            except Exception:
                data = {"raw": text}
            yield event, data
    finally:
        try:
            s.close()
        except Exception:
            pass


def _extract_error_message(data) -> str:
    """尽量从提链服务返回的任意错误结构中提取用户可读原因。"""
    if data is None:
        return ""
    if isinstance(data, str):
        return data.strip()
    if not isinstance(data, dict):
        return str(data)
    err = data.get("error")
    if isinstance(err, dict):
        for key in ("message", "detail", "reason", "error", "msg", "description"):
            value = err.get(key)
            if value:
                return str(value).strip()
        return json.dumps(err, ensure_ascii=False)[:500]
    if err:
        return str(err).strip()
    for key in ("message", "detail", "reason", "msg", "description", "raw"):
        value = data.get(key)
        if value:
            return str(value).strip()
    return json.dumps(data, ensure_ascii=False)[:500]


def _format_failure_reason(exc: Exception, logs: list[str] | None = None, last_event: dict | None = None) -> str:
    reason = f"{type(exc).__name__}: {str(exc)}".strip()
    if (not str(exc).strip()) and logs:
        reason = str(logs[-1])
    if last_event and "提链事件流结束但未返回 result" in reason:
        extracted = _extract_error_message(last_event.get("data"))
        if extracted:
            reason = f"提链事件流结束但未返回 result；最后事件 {last_event.get('event')}: {extracted}"
    return reason[:500]


def _run_extract(*, account_id: int, email: str, access_token: str, link_type: str, cdk: str, trigger: str) -> dict:
    logs: list[str] = []
    last_event = None
    try:
        if link_type == "paypal_zero":
            return _run_paypal_zero_extract(
                account_id=account_id,
                email=email,
                access_token=access_token,
                trigger=trigger,
            )
        if not db.mark_account_extract_running(account_id):
            return {"ok": False, "error": "账号已删除或提链状态已被重置"}
        job = _create_extract_job(token=access_token, link_type=link_type, cdk=cdk)
        job_id = str(job.get("job_id") or "")
        db.update_account_extract(account_id, {
            "ok": False,
            "status": "running",
            "job_id": job_id,
            "link_type": link_type,
            "message": "提链任务已创建，等待结果",
            "cdk_remaining": job.get("cdk_remaining"),
        })
        for event, data in _iter_sse_events(job_id=job_id, cdk=cdk):
            last_event = {"event": event, "data": data}
            if event == "log":
                msg = str((data or {}).get("message") or "")[:300]
                if msg:
                    logs.append(msg)
                    db.update_account_extract(account_id, {
                        "ok": False,
                        "status": "running",
                        "job_id": job_id,
                        "link_type": link_type,
                        "message": msg,
                    })
            elif event == "result":
                result = (data or {}).get("result") if isinstance(data, dict) else None
                if not isinstance(result, dict):
                    result = {}
                final = {"ok": True, "status": "success", "job_id": job_id, "link_type": link_type, "result": result, "logs": logs}
                db.update_account_extract(account_id, final)
                logger.info("[提链] 成功: %s type=%s job=%s", email, link_type, job_id)
                return final
            elif event == "error":
                msg = _extract_error_message(data)
                raise RuntimeError(msg or "提链任务失败")
            elif event == "done":
                break
        raise RuntimeError(f"提链事件流结束但未返回 result: {last_event}")
    except Exception as exc:
        reason = _format_failure_reason(exc, logs=logs, last_event=last_event)
        result = {
            "ok": False,
            "status": "failed",
            "checked_at": datetime.now().isoformat(timespec="seconds"),
            "error": reason,
            "message": reason,
        }
        try:
            db.update_account_extract(account_id, result)
        except Exception:
            logger.exception("[提链] 写入失败状态异常: account_id=%s", account_id)
        logger.exception("[提链] 失败: %s", email)
        return result
    finally:
        _QUEUE_SLOTS.release()


def enqueue_account_extract(*, account_id: int, email: str, access_token: str, trigger: str = "manual", link_type: str | None = None, cdk: str | None = None) -> dict:
    if not _QUEUE_SLOTS.acquire(blocking=False):
        return {"accepted": False, "busy": False, "error": "提链队列已满"}
    try:
        lt = _link_type(link_type)
        code = "" if lt == "paypal_zero" else _cdk(cdk)
        if not db.claim_account_extract(account_id, trigger=trigger, link_type=lt):
            _QUEUE_SLOTS.release()
            return {"accepted": False, "busy": True, "error": "该账号正在提链中"}
        fut = _EXECUTOR.submit(_run_extract, account_id=account_id, email=email, access_token=access_token, link_type=lt, cdk=code, trigger=trigger)
        return {"accepted": True, "busy": False, "future": fut, "link_type": lt}
    except Exception:
        _QUEUE_SLOTS.release()
        raise
