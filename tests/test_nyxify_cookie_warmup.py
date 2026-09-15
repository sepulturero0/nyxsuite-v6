import asyncio
import unittest
import sys
import types
from unittest import mock

_playwright_pkg = types.ModuleType("playwright")
_playwright_async_api = types.ModuleType("playwright.async_api")
_playwright_async_api.async_playwright = lambda: None
_playwright_async_api.TimeoutError = TimeoutError
sys.modules.setdefault("playwright", _playwright_pkg)
sys.modules.setdefault("playwright.async_api", _playwright_async_api)

from core import adspower_extension_cleanup


class _WarmupPage:
    def __init__(self, url="about:blank"):
        self.url = url
        self.closed = False

    def is_closed(self):
        return self.closed

    async def close(self):
        self.closed = True


class _WarmupContext:
    def __init__(self, baseline_pages=None):
        self.pages = list(baseline_pages or [])

    async def new_page(self):
        page = _WarmupPage()
        self.pages.append(page)
        return page


class CookieWarmupOrderingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.config_patch = mock.patch(
            "core.nyxify_runtime_config.load_nyxify_config",
            return_value={
                "cookie_warmup_enabled": True,
                "cookie_warmup_sites": [],
            },
        )
        self.config_patch.start()
        self.addCleanup(self.config_patch.stop)

    async def test_open_signup_helper_does_not_run_cookie_warmup(self):
        calls = []
        context = object()

        async def fake_warmup(_context, _logger, _profile_id):
            calls.append("warmup")
            return {"enabled": True, "visited": ["https://wikipedia.org/"]}

        async def fake_open(_context, _logger, _profile_id):
            calls.append("open_signup")
            return {
                "url": "https://accounts.snapchat.com/v2/signup",
                "method": "new_tab",
                "page": object(),
            }

        with mock.patch.object(
            adspower_extension_cleanup,
            "_warm_ads_profile_cookies",
            mock.AsyncMock(side_effect=fake_warmup),
        ), mock.patch.object(
            adspower_extension_cleanup,
            "_open_snapchat_signup_with_timeout",
            mock.AsyncMock(side_effect=fake_open),
        ):
            result = await adspower_extension_cleanup.open_snapchat_signup(
                context,
                logger=None,
                profile_id="k1abc",
            )

        self.assertEqual(calls, ["open_signup"])
        self.assertEqual(result["url"], "https://accounts.snapchat.com/v2/signup")

    async def test_cookie_warmup_clicks_consent_prompt_when_present(self):
        page = mock.AsyncMock()
        page.evaluate.return_value = True

        clicked = await adspower_extension_cleanup.accept_cookie_consent_if_present(
            page,
            logger=None,
            profile_id="k1abc",
            site_url="https://example.com/",
        )

        self.assertTrue(clicked)
        page.evaluate.assert_awaited_once()

    async def test_cookie_warmup_runs_sites_with_concurrency_cap(self):
        active = 0
        max_active = 0
        started = []

        async def fake_warm_site(_context, url, _duration, _logger, _profile_id):
            nonlocal active, max_active
            started.append(url)
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.01)
            active -= 1
            return True

        with mock.patch.object(adspower_extension_cleanup, "COOKIE_WARMUP_MIN_SITES", 6), \
                mock.patch.object(adspower_extension_cleanup, "COOKIE_WARMUP_MAX_SITES", 6), \
                mock.patch.object(adspower_extension_cleanup, "COOKIE_WARMUP_MIN_SECONDS", 12), \
                mock.patch.object(adspower_extension_cleanup, "COOKIE_WARMUP_MAX_SECONDS", 12), \
                mock.patch.object(adspower_extension_cleanup, "COOKIE_WARMUP_MAX_CONCURRENT_TABS", 4), \
                mock.patch.object(adspower_extension_cleanup, "_warm_one_cookie_site", side_effect=fake_warm_site):
            result = await adspower_extension_cleanup.warm_ads_profile_cookies(
                _WarmupContext(),
                logger=None,
                profile_id="k1abc",
            )

        self.assertEqual(len(started), 6)
        self.assertEqual(len(result["visited"]), 6)
        self.assertLessEqual(max_active, 4)
        self.assertGreater(max_active, 1)

    async def test_cookie_warmup_closes_pages_created_after_baseline(self):
        baseline_page = _WarmupPage("https://start.adspower.net/")
        context = _WarmupContext([baseline_page])

        async def fake_warm_site(context, url, _duration, _logger, _profile_id):
            page = await context.new_page()
            page.url = url
            return True

        with mock.patch.object(adspower_extension_cleanup, "COOKIE_WARMUP_MIN_SITES", 3), \
                mock.patch.object(adspower_extension_cleanup, "COOKIE_WARMUP_MAX_SITES", 3), \
                mock.patch.object(adspower_extension_cleanup, "COOKIE_WARMUP_MAX_CONCURRENT_TABS", 3), \
                mock.patch.object(adspower_extension_cleanup, "_warm_one_cookie_site", side_effect=fake_warm_site):
            result = await adspower_extension_cleanup.warm_ads_profile_cookies(context, None, "k1abc")

        self.assertEqual(len(result["visited"]), 3)
        self.assertFalse(baseline_page.closed)
        warmup_pages = [page for page in context.pages if page is not baseline_page]
        self.assertEqual(len(warmup_pages), 3)
        self.assertTrue(all(page.closed for page in warmup_pages))

    async def test_post_warmup_cleanup_keeps_adspower_and_closes_unrelated_tabs(self):
        adspower_page = _WarmupPage("https://start.adspower.net/?id=k1abc")
        old_tab = _WarmupPage("https://example.com/")
        extension_tab = _WarmupPage("chrome://extensions/")
        context = _WarmupContext([old_tab, adspower_page, extension_tab])

        result = await adspower_extension_cleanup.clear_unrelated_tabs_except_adspower(
            context,
            logger=None,
            profile_id="k1abc",
        )

        self.assertFalse(adspower_page.closed)
        self.assertTrue(old_tab.closed)
        self.assertTrue(extension_tab.closed)
        self.assertEqual(result["closed"], 2)
        self.assertEqual(result["kept"], 1)

    async def test_cookie_warmup_worker_failure_does_not_stop_other_sites(self):
        calls = []

        async def fake_warm_site(_context, url, _duration, _logger, _profile_id):
            calls.append(url)
            if len(calls) == 2:
                raise RuntimeError("site exploded")
            return True

        with mock.patch.object(adspower_extension_cleanup, "COOKIE_WARMUP_MIN_SITES", 4), \
                mock.patch.object(adspower_extension_cleanup, "COOKIE_WARMUP_MAX_SITES", 4), \
                mock.patch.object(adspower_extension_cleanup, "COOKIE_WARMUP_MAX_CONCURRENT_TABS", 4), \
                mock.patch.object(adspower_extension_cleanup, "_warm_one_cookie_site", side_effect=fake_warm_site):
            result = await adspower_extension_cleanup.warm_ads_profile_cookies(_WarmupContext(), None, "k1abc")

        self.assertEqual(len(calls), 4)
        self.assertEqual(len(result["visited"]), 3)

    async def test_hung_site_is_skipped_by_per_site_hard_timeout(self):
        # A site that never returns must not hang the whole warm-up; the hard
        # per-site cap force-skips it and the others still succeed.
        async def fake_warm_site(_context, url, _duration, _logger, _profile_id):
            if url.endswith("hang/"):
                await asyncio.sleep(100)
            return True

        sites = ("https://a.example/", "https://hang/", "https://b.example/")
        with mock.patch.object(adspower_extension_cleanup, "COOKIE_WARMUP_GOOD_WEBSITES", sites), \
                mock.patch.object(adspower_extension_cleanup, "COOKIE_WARMUP_MIN_SITES", 3), \
                mock.patch.object(adspower_extension_cleanup, "COOKIE_WARMUP_MAX_SITES", 3), \
                mock.patch.object(adspower_extension_cleanup, "COOKIE_WARMUP_MAX_CONCURRENT_TABS", 3), \
                mock.patch.object(adspower_extension_cleanup, "COOKIE_WARMUP_PER_SITE_HARD_TIMEOUT", 0.2), \
                mock.patch.object(adspower_extension_cleanup, "COOKIE_WARMUP_TOTAL_HARD_TIMEOUT", 5), \
                mock.patch.object(adspower_extension_cleanup, "_warm_one_cookie_site", side_effect=fake_warm_site):
            result = await adspower_extension_cleanup.warm_ads_profile_cookies(_WarmupContext(), None, "k1abc")

        # The two healthy sites succeed; the hung one is dropped.
        self.assertEqual(len(result["visited"]), 2)
        self.assertNotIn("https://hang/", result["visited"])

    async def test_warmup_page_registers_dialog_auto_dismiss(self):
        # Each warm-up page must install a dialog handler so a beforeunload /
        # confirm from a stray navigation click can't block the tab (or its
        # close) — the reported Windows hang.
        page = mock.AsyncMock()
        page.is_closed = mock.Mock(return_value=False)
        registered = []
        page.on = mock.Mock(side_effect=lambda event, _cb: registered.append(event))
        page.goto.side_effect = RuntimeError("stop early after dialog wiring")

        context = mock.AsyncMock()
        context.new_page.return_value = page

        await adspower_extension_cleanup._warm_one_cookie_site(
            context, "https://example.com/", 1, None, "k1abc"
        )

        self.assertIn("dialog", registered)
        self.assertIn("popup", registered)
        page.add_init_script.assert_awaited_once()
        self.assertIn("window.open = () => null", page.add_init_script.await_args.args[0])
        self.assertIn('closest("a, area, form")', page.add_init_script.await_args.args[0])
        self.assertIn('target !== "_self"', page.add_init_script.await_args.args[0])


if __name__ == "__main__":
    unittest.main()
