"""Manual signup success recovery.

When the operator finishes OTP/SMS/email verification by hand, Snapchat can
land the browser on a confirmed post-signup account page (/v2/welcome,
/accounts, or /accounts/*). Nyxify must treat that as a normal success — but
only when a username is actually readable on the page, never from the URL
alone.
"""
import unittest
from unittest import mock

from core import signup_flow


class _FakeLocator:
    def __init__(self, text=""):
        self._text = str(text or "")

    @property
    def first(self):
        return self

    async def count(self):
        return 1 if self._text else 0

    async def text_content(self):
        return self._text


class _FakePage:
    def __init__(self, url="", username=""):
        self.url = url
        self._username = str(username or "")
        self.context = None
        self.front_calls = 0
        self.wait_timeouts = 0

    def is_closed(self):
        return False

    def locator(self, selector):
        if self._username and "username" in selector:
            return _FakeLocator(self._username)
        return _FakeLocator("")

    async def evaluate(self, _script):
        return ""

    async def bring_to_front(self):
        self.front_calls += 1

    async def wait_for_timeout(self, _ms):
        self.wait_timeouts += 1


class _FakeContext:
    def __init__(self, pages):
        self.pages = list(pages or [])


def _page_with_context(url, username=""):
    page = _FakePage(url=url, username=username)
    page.context = _FakeContext([page])
    return page


class ConfirmedPostSignupUrlTests(unittest.TestCase):
    def test_welcome_and_account_pages_are_confirmed(self):
        confirmed = [
            "https://accounts.snapchat.com/v2/welcome",
            "https://accounts.snapchat.com/v2/welcome/",
            "https://accounts.snapchat.com/v2/welcome?locale=en",
            "https://accounts.snapchat.com/accounts",
            "https://accounts.snapchat.com/accounts/",
            "https://accounts.snapchat.com/accounts/settings",
        ]
        for url in confirmed:
            with self.subTest(url=url):
                self.assertTrue(signup_flow._is_confirmed_post_signup_url(url))

    def test_signup_login_verification_and_error_pages_are_not_confirmed(self):
        rejected = [
            "https://accounts.snapchat.com/v2/signup",
            "https://accounts.snapchat.com/accounts/v2/signup",
            "https://accounts.snapchat.com/v2/login",
            "https://accounts.snapchat.com/accounts/v2/login",
            "https://accounts.snapchat.com/accounts/verify?continue=1",
            "https://accounts.snapchat.com/v2/tiv",
            "https://accounts.snapchat.com/accounts/oauth2/auth",
            "https://accounts.snapchat.com/accounts/v2/403",
            "https://accounts.snapchat.com/accounts/v2/server-error",
            "https://example.com/accounts",
            "",
            "not a url",
        ]
        for url in rejected:
            with self.subTest(url=url):
                self.assertFalse(signup_flow._is_confirmed_post_signup_url(url))


class SuccessUsernameTests(unittest.IsolatedAsyncioTestCase):
    async def test_welcome_page_with_username_returns_username(self):
        page = _page_with_context("https://accounts.snapchat.com/v2/welcome", "welcomename")
        self.assertEqual(await signup_flow._read_success_username(page), "welcomename")

    async def test_accounts_pages_with_username_return_username(self):
        urls = [
            "https://accounts.snapchat.com/accounts",
            "https://accounts.snapchat.com/accounts/",
            "https://accounts.snapchat.com/accounts/settings",
        ]
        for url in urls:
            with self.subTest(url=url):
                page = _page_with_context(url, "accountname")
                self.assertEqual(await signup_flow._read_success_username(page), "accountname")

    async def test_accounts_page_without_readable_username_is_not_success(self):
        page = _page_with_context("https://accounts.snapchat.com/accounts")
        self.assertEqual(await signup_flow._read_success_username(page), "")

    async def test_non_success_pages_do_not_return_a_username(self):
        urls = [
            "https://accounts.snapchat.com/v2/signup",
            "https://accounts.snapchat.com/accounts/v2/login",
            "https://accounts.snapchat.com/accounts/verify",
            "https://accounts.snapchat.com/accounts/v2/403",
        ]
        for url in urls:
            with self.subTest(url=url):
                page = _page_with_context(url, "staleusername")
                self.assertEqual(await signup_flow._read_success_username(page), "")

    async def test_other_context_page_username_surfaces_for_manual_recovery(self):
        signup_page = _FakePage(url="https://accounts.snapchat.com/v2/signup")
        account_page = _FakePage(url="https://accounts.snapchat.com/accounts", username="manualname")
        context = _FakeContext([signup_page, account_page])
        signup_page.context = context
        account_page.context = context

        self.assertEqual(await signup_flow._read_success_username(signup_page), "manualname")


class WaitForFinalSuccessUsernameTests(unittest.IsolatedAsyncioTestCase):
    async def test_returns_username_after_manual_navigation_to_accounts(self):
        page = _FakePage(url="https://accounts.snapchat.com/v2/signup")
        context = _FakeContext([page])
        page.context = context

        calls = {"count": 0}

        async def wait_for_timeout(_ms):
            page.wait_timeouts += 1
            calls["count"] += 1
            if calls["count"] >= 1:
                page.url = "https://accounts.snapchat.com/accounts"
                page._username = "latemanual"

        page.wait_for_timeout = wait_for_timeout

        username = await signup_flow._wait_for_final_success_username(page, timeout_ms=5000)
        self.assertEqual(username, "latemanual")

    async def test_account_page_without_username_times_out_empty(self):
        page = _page_with_context("https://accounts.snapchat.com/accounts")
        username = await signup_flow._wait_for_final_success_username(page, timeout_ms=1000)
        self.assertEqual(username, "")

    async def test_brings_confirmed_account_page_to_front(self):
        signup_page = _FakePage(url="https://accounts.snapchat.com/v2/signup")
        account_page = _FakePage(url="https://accounts.snapchat.com/accounts")
        context = _FakeContext([signup_page, account_page])
        signup_page.context = context
        account_page.context = context

        username = await signup_flow._wait_for_final_success_username(signup_page, timeout_ms=1000)

        self.assertEqual(username, "")
        self.assertGreaterEqual(account_page.front_calls, 1)


class SignupResultWiringTests(unittest.IsolatedAsyncioTestCase):
    async def test_welcome_stage_username_reaches_result_and_callback(self):
        page = mock.Mock()
        page.wait_for_timeout = mock.AsyncMock()
        callback = mock.AsyncMock()

        with mock.patch.object(signup_flow, "_resolve_active_signup_page", mock.AsyncMock(return_value=page)), \
                mock.patch.object(signup_flow, "_wait_for_signup_progress", mock.AsyncMock(return_value="welcome")), \
                mock.patch.object(signup_flow, "_read_success_username", mock.AsyncMock(return_value="manualqueen")):
            result = await signup_flow._handle_verification(
                page,
                "person@example.com",
                mock.AsyncMock(),
                None,
                "1",
                username_detected_callback=callback,
            )

        self.assertEqual(result["final_username"], "manualqueen")
        self.assertTrue(result["reached_verification"])
        callback.assert_awaited_once_with("manualqueen")

    async def test_fetching_email_sees_manual_welcome_before_failure_cleanup(self):
        page = _page_with_context("https://accounts.snapchat.com/accounts/verify")
        callback = mock.AsyncMock()
        steps = []

        async def fetch_email(*_args, **_kwargs):
            page.url = "https://accounts.snapchat.com/v2/welcome"
            page._username = "emailmanual"
            return ""

        with mock.patch.object(signup_flow, "_resolve_active_signup_page", mock.AsyncMock(return_value=page)), \
                mock.patch.object(signup_flow, "_wait_for_signup_progress", mock.AsyncMock(return_value="email")), \
                mock.patch.object(signup_flow, "_fetch_email_from_provider", fetch_email):
            result = await signup_flow._handle_verification(
                page,
                "",
                mock.AsyncMock(),
                None,
                "1",
                username_detected_callback=callback,
                email_fetcher=mock.AsyncMock(),
                progress_callback=lambda step: steps.append(step),
            )

        self.assertEqual(result["final_username"], "emailmanual")
        self.assertTrue(result["reached_verification"])
        self.assertIn("fetching_email", steps)
        self.assertIn("signup_complete", steps)
        callback.assert_awaited_once_with("emailmanual")

    async def test_fetching_replacement_email_sees_manual_welcome_before_retry_failure(self):
        page = _page_with_context("https://accounts.snapchat.com/accounts/verify")
        callback = mock.AsyncMock()
        steps = []

        async def fetch_replacement(*_args, **_kwargs):
            page.url = "https://accounts.snapchat.com/accounts"
            page._username = "replacementmanual"
            return ""

        with mock.patch.object(signup_flow, "_resolve_active_signup_page", mock.AsyncMock(return_value=page)), \
                mock.patch.object(signup_flow, "_wait_for_signup_progress", mock.AsyncMock(return_value="email")), \
                mock.patch.object(signup_flow, "_fill_and_submit_verification_email", mock.AsyncMock(return_value=True)), \
                mock.patch.object(signup_flow, "_is_email_already_verified_error_visible", mock.AsyncMock(return_value=True)), \
                mock.patch.object(signup_flow, "_fetch_email_from_provider", fetch_replacement):
            result = await signup_flow._handle_verification(
                page,
                "old@example.com",
                mock.AsyncMock(),
                None,
                "1",
                username_detected_callback=callback,
                email_fetcher=mock.AsyncMock(),
                progress_callback=lambda step: steps.append(step),
            )

        self.assertEqual(result["final_username"], "replacementmanual")
        self.assertTrue(result["reached_verification"])
        self.assertIn("fetching_replacement_email", steps)
        self.assertIn("signup_complete", steps)
        callback.assert_awaited_once_with("replacementmanual")

    async def test_switching_to_phone_sees_manual_welcome_when_switch_fails(self):
        page = _page_with_context("https://accounts.snapchat.com/accounts/verify")
        callback = mock.AsyncMock()
        steps = []

        async def click_phone(*_args, **_kwargs):
            page.url = "https://accounts.snapchat.com/v2/welcome"
            page._username = "switchmanual"
            return False

        with mock.patch.object(signup_flow, "_resolve_active_signup_page", mock.AsyncMock(return_value=page)), \
                mock.patch.object(signup_flow, "_wait_for_signup_progress", mock.AsyncMock(return_value="email")), \
                mock.patch.object(signup_flow, "_click_use_phone_instead", click_phone):
            result = await signup_flow._handle_verification(
                page,
                "old@example.com",
                mock.AsyncMock(),
                None,
                "1",
                username_detected_callback=callback,
                progress_callback=lambda step: steps.append(step),
                phone_fetcher=mock.Mock(),
                sms_fetcher=mock.Mock(),
                verification_priority="phone",
            )

        self.assertEqual(result["final_username"], "switchmanual")
        self.assertTrue(result["reached_verification"])
        self.assertIn("switching_to_phone", steps)
        self.assertIn("signup_complete", steps)
        callback.assert_awaited_once_with("switchmanual")


if __name__ == "__main__":
    unittest.main()
