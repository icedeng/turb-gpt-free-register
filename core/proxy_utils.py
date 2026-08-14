# -*- coding: utf-8 -*-
"""代理字符串规范化工具。"""
from __future__ import annotations

import ipaddress


_SUPPORTED_SCHEMES = {"http", "https", "socks5", "socks5h"}


def _is_port(value: str) -> bool:
    if not value.isdigit():
        return False
    port = int(value)
    return 1 <= port <= 65535


def _looks_like_host(value: str) -> bool:
    value = value.strip("[]")
    if value.lower() == "localhost" or "." in value:
        return True
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True


def normalize_proxy_url(proxy_url: str, *, default_scheme: str = "http") -> str:
    """把代理池简写统一转换成标准代理 URL。

    支持标准 URL、``username:password@hostname:port``、
    ``username:password:hostname:port``、``hostname:port:username:password``
    和 ``hostname:port@username:password``。
    无协议格式默认补充 HTTP；空值保持为空。
    """
    text = str(proxy_url or "").strip()
    if not text:
        return text
    if "://" in text:
        prefix, payload = text.split("://", 1)
        if prefix.lower() not in _SUPPORTED_SCHEMES:
            return text
        default_scheme = prefix.lower()
        text = payload.strip()
        if not text:
            return ""
    if text.startswith("//"):
        return f"{default_scheme}:{text}"

    if "@" in text:
        first, second = text.split("@", 1)
        first_parts = first.rsplit(":", 1)
        second_parts = second.rsplit(":", 1)
        first_is_host_port = len(first_parts) == 2 and _is_port(first_parts[1])
        second_is_host_port = len(second_parts) == 2 and _is_port(second_parts[1])
        if second_is_host_port and ":" in first and not first_is_host_port:
            # username:password@hostname:port
            return f"{default_scheme}://{text}"
        if first_is_host_port and ":" in second and not second_is_host_port:
            # hostname:port@username:password
            return f"{default_scheme}://{second}@{first}"
        if first_is_host_port and second_is_host_port:
            # 密码可能恰好是纯数字时两种 @ 格式会出现语法歧义，用主机特征消歧。
            first_host = first_parts[0]
            second_host = second_parts[0]
            if _looks_like_host(first_host) and not _looks_like_host(second_host):
                return f"{default_scheme}://{second}@{first}"
            if _looks_like_host(second_host) and not _looks_like_host(first_host):
                return f"{default_scheme}://{text}"
        raise ValueError(f"代理格式无法识别: {text}")

    parts = text.split(":", 3)
    if len(parts) == 4:
        host_first = _is_port(parts[1]) and parts[2] and parts[3]
        user_first = _is_port(parts[3]) and parts[0] and parts[1]
        if host_first and not user_first:
            # hostname:port:username:password
            return f"{default_scheme}://{parts[2]}:{parts[3]}@{parts[0]}:{parts[1]}"
        if user_first and not host_first:
            # username:password:hostname:port
            return f"{default_scheme}://{parts[0]}:{parts[1]}@{parts[2]}:{parts[3]}"
        if host_first and user_first:
            # 两个端口字段都为数字时，优先按更常见的 hostname:port:username:password。
            if _looks_like_host(parts[2]) and not _looks_like_host(parts[0]):
                return f"{default_scheme}://{parts[0]}:{parts[1]}@{parts[2]}:{parts[3]}"
            return f"{default_scheme}://{parts[2]}:{parts[3]}@{parts[0]}:{parts[1]}"
    if text.count(":") == 1:
        # hostname:port（无认证）
        return f"{default_scheme}://{text}"
    raise ValueError(f"代理格式无法识别: {text}")
