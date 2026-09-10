"""Tests for Nyxify's proxy blocker matching (nyxify_runner._is_proxy_banned)."""

import unittest

import nyxify_runner


class IsProxyBannedTests(unittest.TestCase):
    def test_subnet_prefix_match(self):
        self.assertTrue(nyxify_runner._is_proxy_banned("130.24.5.7:8080", ["130.24"]))

    def test_first_octet_prefix_blocks_whole_range(self):
        self.assertTrue(nyxify_runner._is_proxy_banned("130.99.1.1:9000", ["130"]))

    def test_full_proxy_string_match(self):
        self.assertTrue(
            nyxify_runner._is_proxy_banned("109.176.200.39:57813", ["109.176.200.39:57813"])
        )

    def test_substring_match(self):
        self.assertTrue(nyxify_runner._is_proxy_banned("user@82.26.10.5:3128", ["82.26"]))

    def test_no_match(self):
        self.assertFalse(nyxify_runner._is_proxy_banned("45.10.1.1:80", ["130", "82.26"]))

    def test_empty_proxy_never_banned(self):
        self.assertFalse(nyxify_runner._is_proxy_banned("", ["130"]))

    def test_empty_banlist_never_bans(self):
        self.assertFalse(nyxify_runner._is_proxy_banned("130.24.5.7:8080", []))

    def test_blank_ban_patterns_ignored(self):
        self.assertFalse(nyxify_runner._is_proxy_banned("130.24.5.7:8080", ["", "   "]))

    def test_case_insensitive_hostname(self):
        self.assertTrue(
            nyxify_runner._is_proxy_banned("Proxy.Example.COM:9000", ["proxy.example.com"])
        )


class ProxyPriorityTests(unittest.TestCase):
    def test_priority_matches_first_octet_prefix(self):
        self.assertTrue(nyxify_runner._is_proxy_priority_match("23.10.1.5:9000:user:pass", ["23"]))

    def test_priority_matches_dotted_prefix(self):
        self.assertTrue(nyxify_runner._is_proxy_priority_match("23.54.8.9:9000:user:pass", ["23.54"]))

    def test_priority_does_not_match_later_digits(self):
        self.assertFalse(nyxify_runner._is_proxy_priority_match("123.54.8.9:9000:user:pass", ["23.54"]))

    def test_single_octet_priority_does_not_match_longer_octet(self):
        self.assertFalse(nyxify_runner._is_proxy_priority_match("95.134.176.130:49108:u:p", ["9"]))

    def test_priority_accepts_any_pattern(self):
        self.assertTrue(nyxify_runner._is_proxy_priority_match("130.24.5.7:8080", ["23", "130.24"]))

    def test_blank_priority_patterns_are_ignored(self):
        self.assertFalse(nyxify_runner._is_proxy_priority_match("23.10.1.5:9000", ["", "   "]))


if __name__ == "__main__":
    unittest.main()
