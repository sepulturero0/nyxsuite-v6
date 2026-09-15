"""Nyxify account creation must be able to skip the extension turn-off step.

Users asked to stop disabling the profile's Chrome extensions while the Snapchat
account is being created. With ``disable_extensions=False`` the helper must NOT
visit chrome://extensions/ or toggle anything, but still open the profile,
attach the context, and (optionally) open the signup page.
"""

import asyncio
import sys
import types
import unittest
from unittest import mock

# Stub playwright before importing the module under test.
if "playwright" not in sys.modules:
    _pw = types.ModuleType("playwright")
    _pw_async = types.ModuleType("playwright.async_api")

    async def _fake_async_playwright_start():
        raise AssertionError("async_playwright().start should be patched in tests")

    _pw_async.async_playwright = lambda: types.SimpleNamespace(start=_fake_async_playwright_start)
    _pw_async.TimeoutError = TimeoutError
    sys.modules["playwright"] = _pw
    sys.modules["playwright.async_api"] = _pw_async

from core import adspower_extension_cleanup as cleanup


class _FakePage:
    def __init__(self):
        self.goto_calls = []

    async def goto(self, url, **_kwargs):
        self.goto_calls.append(url)

    async def evaluate(self, *_a, **_k):
        return {}

    async def wait_for_timeout(self, *_a, **_k):
        return None

    def is_closed(self):
        return False

    async def close(self, **_kwargs):
        return None

    def on(self, *_a, **_k):
        return None


class _FakeLocator:
    def __init__(self, visible=False):
        self._visible = visible
        self.first = self

    async def is_visible(self):
        return bool(self._visible)


class _BlankSignupPage(_FakePage):
    url = "https://accounts.snapchat.com/v2/signup"

    def __init__(self):
        super().__init__()
        self.reload_calls = 0
        self.waits = []

    def locator(self, selector):
        form_visible = self.reload_calls > 0 and (
            "#firstname" in selector
            or "InitialSignupForm" in selector
        )
        return _FakeLocator(form_visible)

    def get_by_role(self, *_a, **_k):
        return _FakeLocator(False)

    async def evaluate(self, *_a, **_k):
        return self.reload_calls == 0

    async def reload(self, **_kwargs):
        self.reload_calls += 1

    async def wait_for_timeout(self, ms):
        self.waits.append(ms)


class _UndetectableLogoShellPage(_BlankSignupPage):
    """Models a logo-only shell whose DOM does not match the detector."""

    async def evaluate(self, script, *_a, **_k):
        if "const controls" in script:
            return False
        # The DOM is ready even though the logo markup is not recognizable.
        # This is the condition under which the handoff fallback may refresh.
        return True


class _LoadingSignupPage(_BlankSignupPage):
    async def evaluate(self, *_a, **_k):
        return False


class _FakeContext:
    def __init__(self):
        self.new_pages = []

    async def new_page(self):
        page = _FakePage()
        self.new_pages.append(page)
        return page

    @property
    def pages(self):
        return list(self.new_pages)


class _FakeBrowser:
    def __init__(self, context):
        self.contexts = [context]

    async def close(self):
        return None


class _FakePlaywright:
    def __init__(self, browser):
        self._browser = browser
        self.stopped = False
        self.chromium = types.SimpleNamespace(
            connect_over_cdp=self._connect
        )

    async def _connect(self, _endpoint):
        return self._browser

    async def stop(self):
        self.stopped = True


class ExtensionDisableSkipTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, disable_extensions):
        context = _FakeContext()
        browser = _FakeBrowser(context)
        playwright = _FakePlaywright(browser)

        adspower = mock.Mock()
        adspower.open_profile.return_value = "ws://fake"

        open_signup_calls = []

        async def fake_open_signup(_context, _logger, _pid):
            open_signup_calls.append(_pid)
            return {"url": "https://accounts.snapchat.com/v2/signup", "method": "new_tab", "page": object()}

        with mock.patch.object(cleanup, "async_playwright", return_value=types.SimpleNamespace(
                    start=mock.AsyncMock(return_value=playwright))), \
                mock.patch.object(cleanup, "maximize_browser_window", mock.AsyncMock()), \
                mock.patch.object(cleanup, "apply_dark_mode_preferences", mock.AsyncMock()), \
                mock.patch.object(cleanup, "apply_dark_mode_to_page", mock.AsyncMock()), \
                mock.patch.object(cleanup, "open_snapchat_signup", side_effect=fake_open_signup):
            result = await cleanup.disable_profile_extensions(
                adspower, "k1abc", logger=None,
                keep_open=True, keep_playwright=True, open_signup=True,
                disable_extensions=disable_extensions,
            )
        return result, context, open_signup_calls

    async def test_skip_does_not_visit_extensions_page(self):
        result, context, open_signup_calls = await self._run(disable_extensions=False)

        # No page navigated to chrome://extensions/.
        visited = [url for page in context.new_pages for url in page.goto_calls]
        self.assertNotIn("chrome://extensions/", visited)
        self.assertTrue(result["success"])
        self.assertTrue(result.get("skipped"))
        self.assertEqual(result["disabled_now"], [])
        # Signup still opened, context + playwright still handed back.
        self.assertEqual(open_signup_calls, ["k1abc"])
        self.assertIs(result["context"], context)
        self.assertIsNotNone(result["playwright_instance"])

    async def test_enabled_path_visits_extensions_page(self):
        result, context, _calls = await self._run(disable_extensions=True)

        visited = [url for page in context.new_pages for url in page.goto_calls]
        self.assertIn("chrome://extensions/", visited)

    async def test_blank_signup_shell_refreshes_during_initial_handoff(self):
        page = _BlankSignupPage()
        context = types.SimpleNamespace(pages=[page])

        result = await cleanup._wait_for_usable_signup_page(
            context,
            page,
            logger=None,
            profile_id="k1blank",
            deadline=asyncio.get_running_loop().time() + 5,
        )

        self.assertIs(result, page)
        self.assertEqual(page.reload_calls, 1)
        self.assertIn(1500, page.waits)

    async def test_logo_only_signup_shell_detector_accepts_initial_shell(self):
        page = _BlankSignupPage()

        self.assertTrue(await cleanup._is_blank_snapchat_signup_shell(page))

    async def test_unusable_signup_handoff_refreshes_without_logo_dom_match(self):
        page = _UndetectableLogoShellPage()
        context = types.SimpleNamespace(pages=[page])

        result = await cleanup._wait_for_usable_signup_page(
            context,
            page,
            logger=None,
            profile_id="k1undetectable",
            deadline=asyncio.get_running_loop().time() + 5,
        )

        self.assertIs(result, page)
        self.assertEqual(page.reload_calls, 1)
        self.assertIn(1500, page.waits)

    async def test_loading_signup_handoff_does_not_refresh(self):
        page = _LoadingSignupPage()
        context = types.SimpleNamespace(pages=[page])

        with self.assertRaises(RuntimeError):
            await cleanup._wait_for_usable_signup_page(
                context,
                page,
                logger=None,
                profile_id="k1stillloading",
                deadline=asyncio.get_running_loop().time() + 0.1,
            )

        self.assertEqual(page.reload_calls, 0)


if __name__ == "__main__":
    unittest.main()
