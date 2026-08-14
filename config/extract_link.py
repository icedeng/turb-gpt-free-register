# -*- coding: utf-8 -*-
"""Plus 试用提链服务配置。"""
from config.env_loader import apply_env_overrides

# 提链服务地址
EXTRACT_LINK_API_BASE: str = ""

# 提链 CDK；创建任务和监听事件都需要。
EXTRACT_LINK_CDK: str = ""

# 提链类型：pix / upi / kakao_pay / ideal / paypal_zero
EXTRACT_LINK_TYPE: str = "pix"

# PayPal 0 元提链服务（vendor/link-pp）与专用巴西出口代理池。
PAYPAL_ZERO_API_BASE: str = "http://127.0.0.1:5572"
PAYPAL_ZERO_PROXY_POOL: list = []
PAYPAL_ZERO_PROXY_SCHEME: str = "http"
PAYPAL_ZERO_PROXY_COUNTRY: str = "BR"
PAYPAL_ZERO_CHECKOUT_ATTEMPTS: int = 5
PAYPAL_ZERO_PROVIDER_ATTEMPTS: int = 10

# 后台提链并发与超时
EXTRACT_LINK_WORKERS: int = 3
EXTRACT_LINK_QUEUE_LIMIT: int = 500
EXTRACT_LINK_REQUEST_TIMEOUT: int = 30
EXTRACT_LINK_EVENT_TIMEOUT: int = 180

apply_env_overrides(globals(), {
    'EXTRACT_LINK_API_BASE': 'str',
    'EXTRACT_LINK_CDK': 'str',
    'EXTRACT_LINK_TYPE': 'str',
    'PAYPAL_ZERO_API_BASE': 'str',
    'PAYPAL_ZERO_PROXY_POOL': 'list_str_multiline',
    'PAYPAL_ZERO_PROXY_SCHEME': 'str',
    'PAYPAL_ZERO_PROXY_COUNTRY': 'str',
    'PAYPAL_ZERO_CHECKOUT_ATTEMPTS': 'int',
    'PAYPAL_ZERO_PROVIDER_ATTEMPTS': 'int',
    'EXTRACT_LINK_WORKERS': 'int',
    'EXTRACT_LINK_QUEUE_LIMIT': 'int',
    'EXTRACT_LINK_REQUEST_TIMEOUT': 'int',
    'EXTRACT_LINK_EVENT_TIMEOUT': 'int',
})
