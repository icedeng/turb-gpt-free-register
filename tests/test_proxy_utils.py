import unittest

from core.proxy_utils import normalize_proxy_url
from core.roxybrowser_client import _proxy_url_to_roxy_info


class ProxyUtilsTests(unittest.TestCase):
    def test_normalize_proxy_url_supports_pool_formats(self):
        cases = [
            (
                "user:pass@proxy.example:1080",
                "socks5://user:pass@proxy.example:1080",
            ),
            (
                "proxy.example:1080:user:pass",
                "socks5://user:pass@proxy.example:1080",
            ),
            (
                "user:pass:proxy.example:1080",
                "socks5://user:pass@proxy.example:1080",
            ),
            (
                "proxy.example:1080@user:pass",
                "socks5://user:pass@proxy.example:1080",
            ),
            (
                "http://user:pass@proxy.example:8080",
                "http://user:pass@proxy.example:8080",
            ),
            (
                "http://proxy.example:8080:user:pass",
                "http://user:pass@proxy.example:8080",
            ),
            (
                "socks5://user:pass:proxy.example:1080",
                "socks5://user:pass@proxy.example:1080",
            ),
            (
                "socks5h://proxy.example:1080",
                "socks5h://proxy.example:1080",
            ),
        ]
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(normalize_proxy_url(raw), expected)

    def test_normalize_proxy_url_keeps_empty_proxy_empty(self):
        self.assertEqual(normalize_proxy_url("   "), "")

    def test_normalize_proxy_url_rejects_ambiguous_host_first_value(self):
        with self.assertRaisesRegex(ValueError, "代理格式无法识别"):
            normalize_proxy_url("proxy.example:not-a-port:user:pass")

    def test_roxy_proxy_info_accepts_username_first_shorthand(self):
        info = _proxy_url_to_roxy_info("user:pass@proxy.example:1080")
        self.assertEqual(info["protocol"], "SOCKS5")
        self.assertEqual(info["host"], "proxy.example")
        self.assertEqual(info["port"], "1080")
        self.assertEqual(info["proxyUserName"], "user")
        self.assertEqual(info["proxyPassword"], "pass")


if __name__ == "__main__":
    unittest.main()
