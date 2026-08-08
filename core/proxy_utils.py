# -*- coding: utf-8 -*-
"""代理字符串规范化工具。"""
from __future__ import annotations


def _is_port(value: str) -> bool:
    if not value.isdigit():
        return False
    port = int(value)
    return 1 <= port <= 65535


def normalize_proxy_url(proxy_url: str, *, default_scheme: str = "socks5") -> str:
    """把代理池简写统一转换成标准代理 URL。

    支持标准 URL、``username:password@hostname:port``、
    ``hostname:port:username:password`` 和 ``hostname:port@username:password``。
    无协议格式默认补充 SOCKS5；空值保持为空。
    """
    text = str(proxy_url or "").strip()
    if not text or "://" in text:
        return text
    if text.startswith("//"):
        return f"{default_scheme}:{text}"

    if "@" in text:
        first, second = text.split("@", 1)
        first_parts = first.rsplit(":", 1)
        second_parts = second.rsplit(":", 1)
        first_is_host_port = len(first_parts) == 2 and _is_port(first_parts[1])
        second_is_host_port = len(second_parts) == 2 and _is_port(second_parts[1])
        if second_is_host_port and ":" in first:
            # username:password@hostname:port
            return f"{default_scheme}://{text}"
        if first_is_host_port and ":" in second:
            # hostname:port@username:password
            return f"{default_scheme}://{second}@{first}"
        raise ValueError(f"代理格式无法识别: {text}")

    parts = text.split(":", 3)
    if len(parts) == 4 and _is_port(parts[1]) and parts[2] and parts[3]:
        # hostname:port:username:password
        return f"{default_scheme}://{parts[2]}:{parts[3]}@{parts[0]}:{parts[1]}"
    if text.count(":") == 1:
        # hostname:port（无认证）
        return f"{default_scheme}://{text}"
    raise ValueError(f"代理格式无法识别: {text}")
