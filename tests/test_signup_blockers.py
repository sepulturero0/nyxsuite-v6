"""Tests for the Nyxify signup-blocker handling added in Phase 1.

Covers:
  * the runner classifier/cleanup wiring for the new blocker error classes
    (reCAPTCHA stall, non-English page, "mobile app" block, email order
    unavailable), and
  * the signup_flow page detectors that raise those errors.
"""

import unittest
import sys
import types
from pathlib import Path
from unittest import mock

class _RequestsResponse:
    status_code = 200
    text = "{}"

    def json(self):
        return {}

    def raise_for_status(self):
        return None


class _RequestsSession:
    def __init__(self):
        self.trust_env = False

    def get(self, *_args, **_kwargs):
        return _RequestsResponse()

    def post(self, *_args, **_kwargs):
        return _RequestsResponse()


_requests_stub = types.ModuleType("requests")
_requests_stub.Session = _RequestsSession
_requests_stub.get = lambda *_args, **_kwargs: _RequestsResponse()
_requests_stub.post = lambda *_args, **_kwargs: _RequestsResponse()
_requests_stub.exceptions = types.SimpleNamespace(
    ConnectionError=ConnectionError,
    Timeout=TimeoutError,
    RequestException=Exception,
)
sys.modules.setdefault("requests", _requests_stub)
sys.modules.setdefault("dotenv", types.SimpleNamespace(load_dotenv=lambda *_args, **_kwargs: None))
_playwright_pkg = types.ModuleType("playwright")
_playwright_async_api = types.ModuleType("playwright.async_api")
_playwright_async_api.async_playwright = lambda: None
_playwright_async_api.TimeoutError = TimeoutError
sys.modules.setdefault("playwright", _playwright_pkg)
sys.modules.setdefault("playwright.async_api", _playwright_async_api)

import nyxify_runner
from core import signup_flow


class ClassifyBlockerErrorsTests(unittest.TestCase):
    """Each blocker error must classify to its own step AND be cleanup-eligible
    so the runner deletes the profile, rotates the proxy, and requeues PENDING.
    """

    CASES = {
        "account_creation_blocked: please try again on our mobile app.": "account_creation_blocked",
        "signup_non_english_page: page rendered in a language the bot cannot drive.": "signup_non_english_page",
        "signup_stuck_retry_exhausted: still stuck after 3 refresh attempts.": "signup_stuck_retry_exhausted",
        "email_order_unavailable: SnapBoard had no verification email.": "email_order_unavailable",
        "phone_verification_rejected: Snapchat rejected all requested phone numbers.": "phone_verification_rejected",
    }

    def test_blocker_errors_classify_to_their_step(self):
        for error_message, expected_step in self.CASES.items():
            with self.subTest(error=error_message):
                step = nyxify_runner._classify_failure_last_step(
                    {"profile_id": "k1abc"},
                    "running_signup",
                    error_message,
                )
                self.assertEqual(step, expected_step)

    def test_blocker_steps_trigger_cleanup_retry(self):
        for expected_step in self.CASES.values():
            with self.subTest(step=expected_step):
                self.assertTrue(
                    nyxify_runner._should_cleanup_failed_created_profile(expected_step, ""),
                    f"{expected_step} should be cleanup-eligible",
                )


class FakePage:
    """Minimal stand-in for a Playwright page for the signup_flow detectors.

    ``evaluate`` distinguishes the two call shapes the detectors use:
      * ``evaluate(js, needle_list)`` — visible-text scan (returns bool)
      * ``evaluate(js)`` — language probe (returns the configured dict) or the
        reCAPTCHA-widget probe (returns the configured bool).
    """

    def __init__(self, text="", lang_info=None, recaptcha=True, generic_form_error=False, signup_form_visible=True):
        self._text = str(text or "").lower()
        self._lang_info = dict(lang_info or {})
        self._recaptcha = recaptcha
        self._generic_form_error = generic_form_error
        self._signup_form_visible = signup_form_visible

    async def evaluate(self, js, arg=None):
        js_text = str(js)
        if "GenericFormLevelErrorMessage" in js_text and "#firstname" in js_text:
            return self._generic_form_error and self._signup_form_visible
        if isinstance(arg, list):
            return any(str(needle).lower() in self._text for needle in arg)
        if "htmlLang" in js_text:
            return dict(self._lang_info)
        if "grecaptcha-badge" in js_text:
            return self._recaptcha
        return False


class SignupDetectorTests(unittest.IsolatedAsyncioTestCase):
    async def test_recaptcha_connect_error_detected(self):
        page = FakePage(
            text="Could not connect to the reCAPTCHA service. Please check your internet connection "
            "and reload to get a reCAPTCHA challenge."
        )
        self.assertTrue(await signup_flow._is_recaptcha_connect_error_visible(page))

    async def test_recaptcha_connect_error_absent(self):
        page = FakePage(text="Sign Up — Step 1 of 3")
        self.assertFalse(await signup_flow._is_recaptcha_connect_error_visible(page))

    async def test_recaptcha_widget_absent_when_only_global_loader_exists(self):
        class GlobalOnlyRecaptchaPage:
            async def evaluate(self, js):
                return "window.grecaptcha" in str(js)

        self.assertFalse(await signup_flow._recaptcha_widget_present(GlobalOnlyRecaptchaPage()))

    async def test_account_creation_blocked_detected(self):
        page = FakePage(
            text="Account creation could not be completed at this time. Please try again on our mobile app."
        )
        self.assertTrue(await signup_flow._is_account_creation_blocked_visible(page))

    async def test_polish_unable_to_process_error_detected_on_signup_form(self):
        page = FakePage(
            text="Przepraszamy, ale nie udało nam się przetworzyć Twojego polecenia.",
            generic_form_error=True,
            signup_form_visible=True,
        )
        self.assertTrue(await signup_flow._is_unable_to_process_error_visible(page))

    async def test_english_unable_to_process_error_detected_from_visible_text(self):
        page = FakePage(
            text="Sign Up Step 1 of 3 We are sorry, we were unable to process your request.",
            generic_form_error=False,
            signup_form_visible=True,
        )

        with mock.patch.object(signup_flow, "_visible_any", mock.AsyncMock(return_value="#username")):
            self.assertTrue(await signup_flow._is_unable_to_process_error_visible(page))

    async def test_arabic_page_is_non_english(self):
        # Mirrors the Arabic signup screenshot: lang=ar, all non-Latin script.
        page = FakePage(lang_info={"lang": "ar", "nonLatin": 25, "latin": 0})
        self.assertTrue(await signup_flow._is_non_english_signup_page(page))

    async def test_chinese_page_is_non_english(self):
        page = FakePage(lang_info={"lang": "zh-cn", "nonLatin": 18, "latin": 2})
        self.assertTrue(await signup_flow._is_non_english_signup_page(page))

    async def test_english_page_is_not_flagged(self):
        page = FakePage(lang_info={"lang": "en-US", "nonLatin": 0, "latin": 42})
        self.assertFalse(await signup_flow._is_non_english_signup_page(page))

    async def test_english_lang_with_stray_glyphs_not_flagged(self):
        # A mostly-English page with a couple of emoji/accents must NOT churn.
        page = FakePage(lang_info={"lang": "en", "nonLatin": 2, "latin": 40})
        self.assertFalse(await signup_flow._is_non_english_signup_page(page))

    async def test_phone_verification_step_detected_from_phone_inputs(self):
        class FakeLocator:
            def __init__(self, visible):
                self._visible = visible

            @property
            def first(self):
                return self

            async def is_visible(self):
                return self._visible

        class FakePhonePage(FakePage):
            def locator(self, selector):
                return FakeLocator(selector == "#phoneNumber")

        page = FakePhonePage(text="Sign Up Step 2 of 3 Country Phone Number Next")
        self.assertTrue(await signup_flow._is_phone_verification_step(page))

    def test_phone_number_split_preserves_country_code(self):
        self.assertEqual(
            signup_flow._split_phone_number("+1 (555) 123-4567"),
            ("+1", "5551234567"),
        )

    async def test_phone_verification_fills_only_local_number_when_country_code_present(self):
        class FakeLocator:
            def __init__(self, visible):
                self._visible = visible

            @property
            def first(self):
                return self

            async def is_visible(self):
                return self._visible

        class FakePhoneEntryPage:
            def locator(self, selector):
                return FakeLocator(selector in {"#countryCode", "#phoneNumber"})

            async def wait_for_timeout(self, _ms):
                return None

        page = FakePhoneEntryPage()
        typed_values = []

        async def record_typed_value(_page, selector, value, *_args):
            typed_values.append((selector, value))
            return True

        with mock.patch.object(signup_flow, "_humanized_type_only", mock.AsyncMock(side_effect=record_typed_value)), \
            mock.patch.object(signup_flow, "_wait_enabled", mock.AsyncMock(return_value=True)), \
            mock.patch.object(signup_flow, "_human_pause", mock.AsyncMock(return_value=None)), \
            mock.patch.object(signup_flow, "_js_click", mock.AsyncMock(return_value=True)):
            self.assertTrue(await signup_flow._fill_and_submit_phone_number(page, "+15551234567", None, "172"))

        self.assertEqual(typed_values, [("#phoneNumber", "5551234567")])

    async def test_phone_verification_clicks_when_phone_field_is_already_formatted(self):
        class FakeLocator:
            @property
            def first(self):
                return self

            async def is_visible(self):
                return True

            async def input_value(self):
                return "(555) 123-4567"

            async def dispatch_event(self, _event):
                return None

        class FakePhoneEntryPage:
            def locator(self, _selector):
                return FakeLocator()

            async def wait_for_timeout(self, _ms):
                return None

        with mock.patch.object(signup_flow, "_humanized_type_only", mock.AsyncMock(return_value=False)), \
            mock.patch.object(signup_flow, "_wait_enabled", mock.AsyncMock(return_value=True)), \
            mock.patch.object(signup_flow, "_human_pause", mock.AsyncMock(return_value=None)), \
            mock.patch.object(signup_flow, "_js_click", mock.AsyncMock(return_value=True)) as js_click:
            self.assertTrue(await signup_flow._fill_and_submit_phone_number(
                FakePhoneEntryPage(),
                "+15551234567",
                None,
                "172",
            ))

        js_click.assert_awaited_once_with(mock.ANY, "button[type='submit']")

    async def test_phone_verification_clicks_continue_when_submit_selector_stays_disabled(self):
        class FakeLocator:
            def __init__(self, selector):
                self.selector = selector

            @property
            def first(self):
                return self

            async def is_visible(self):
                return self.selector == "#phoneNumber" or "Continue" in self.selector

            async def dispatch_event(self, _event):
                return None

        class FakePhoneEntryPage:
            def locator(self, selector):
                return FakeLocator(selector)

            async def wait_for_timeout(self, _ms):
                return None

        async def enabled_for_continue(_page, selector, *_args, **_kwargs):
            return "Continue" in selector

        with mock.patch.object(signup_flow, "_humanized_type_only", mock.AsyncMock(return_value=True)), \
            mock.patch.object(signup_flow, "_wait_enabled", mock.AsyncMock(side_effect=enabled_for_continue)), \
            mock.patch.object(signup_flow, "_human_pause", mock.AsyncMock(return_value=None)), \
            mock.patch.object(signup_flow, "_js_click", mock.AsyncMock(return_value=True)) as js_click:
            self.assertTrue(await signup_flow._fill_and_submit_phone_number(
                FakePhoneEntryPage(),
                "+15551234567",
                None,
                "172",
            ))

        js_click.assert_awaited_once_with(mock.ANY, "button:has-text('Continue')")

    async def test_phone_verification_submit_fails_when_click_fails(self):
        class FakeLocator:
            @property
            def first(self):
                return self

            async def is_visible(self):
                return True

        class FakePhonePage:
            def locator(self, selector):
                return FakeLocator()

            async def wait_for_timeout(self, _ms):
                return None

        with mock.patch.object(signup_flow, "_humanized_type_only", mock.AsyncMock(return_value=True)), \
            mock.patch.object(signup_flow, "_wait_enabled", mock.AsyncMock(return_value=True)), \
            mock.patch.object(signup_flow, "_human_pause", mock.AsyncMock(return_value=None)), \
            mock.patch.object(signup_flow, "_js_click", mock.AsyncMock(return_value=False)):
            submitted = await signup_flow._fill_and_submit_phone_number(
                FakePhonePage(),
                "+15551234567",
                None,
                "172",
            )

        self.assertFalse(submitted)

    async def test_optional_sms_verification_runs_after_email_otp(self):
        class FakeButton:
            async def is_visible(self):
                return True

            async def click(self):
                return None

        class FakeLocator:
            @property
            def first(self):
                return self

            async def is_visible(self):
                return True

            async def click(self):
                return None

        class FakeVerificationPage:
            def locator(self, _selector):
                return FakeLocator()

            async def wait_for_timeout(self, _ms):
                return None

            async def bring_to_front(self):
                return None

        stages = iter(["otp", "phone", "otp", "welcome"])
        page = FakeVerificationPage()
        otp_fetcher = mock.AsyncMock(return_value="111111")
        phone_fetcher = mock.AsyncMock(return_value="+15551234567")
        sms_fetcher = mock.AsyncMock(return_value="222222")

        with mock.patch.object(signup_flow, "_resolve_active_signup_page", mock.AsyncMock(return_value=page)), \
            mock.patch.object(signup_flow, "_wait_for_signup_progress", mock.AsyncMock(side_effect=lambda *a, **k: next(stages))), \
            mock.patch.object(signup_flow, "_type_otp_code", mock.AsyncMock(return_value=True)) as type_otp, \
            mock.patch.object(signup_flow, "_wait_enabled", mock.AsyncMock(return_value=True)), \
            mock.patch.object(signup_flow, "_fill_and_submit_phone_number", mock.AsyncMock(return_value=True)) as fill_phone, \
            mock.patch.object(signup_flow, "_read_success_username", mock.AsyncMock(return_value="cleaopala")):
            result = await signup_flow._handle_verification(
                page,
                "kellyfrench8406123880@gmail.com",
                otp_fetcher,
                None,
                "172",
                phone_fetcher=phone_fetcher,
                sms_fetcher=sms_fetcher,
            )

        self.assertTrue(result["otp_entered"])
        self.assertTrue(result["phone_entered"])
        self.assertTrue(result["sms_otp_entered"])
        self.assertEqual(result["final_username"], "cleaopala")
        otp_fetcher.assert_awaited_once()
        phone_fetcher.assert_awaited_once()
        sms_fetcher.assert_awaited_once()
        fill_phone.assert_awaited_once_with(page, "+15551234567", None, "172")
        self.assertEqual(type_otp.await_count, 2)

    async def test_phone_verification_retries_with_new_number_when_first_stays_on_phone_step(self):
        class FakeLocator:
            @property
            def first(self):
                return self

            async def is_visible(self):
                return True

            async def click(self):
                return None

        class FakeVerificationPage:
            def locator(self, _selector):
                return FakeLocator()

            async def wait_for_timeout(self, _ms):
                return None

            async def bring_to_front(self):
                return None

        page = FakeVerificationPage()
        phone_fetcher = mock.AsyncMock(side_effect=["+15550000000", "+15551234567"])
        sms_fetcher = mock.AsyncMock(return_value="222222")

        with mock.patch.object(signup_flow, "_resolve_active_signup_page", mock.AsyncMock(return_value=page)), \
            mock.patch.object(signup_flow, "_wait_for_signup_progress", mock.AsyncMock(side_effect=["phone", "otp"])), \
            mock.patch.object(signup_flow, "_fill_and_submit_phone_number", mock.AsyncMock(return_value=True)) as fill_phone, \
            mock.patch.object(signup_flow, "_type_otp_code", mock.AsyncMock(return_value=True)) as type_otp, \
            mock.patch.object(signup_flow, "_wait_enabled", mock.AsyncMock(return_value=True)), \
            mock.patch.object(signup_flow, "_wait_for_stage_after_otp", mock.AsyncMock(return_value="welcome")), \
            mock.patch.object(signup_flow, "_read_success_username", mock.AsyncMock(return_value="cleaopala")):
            result = await signup_flow._handle_optional_phone_sms_verification(
                page,
                phone_fetcher,
                sms_fetcher,
                {
                    "reached_verification": True,
                    "otp_entered": True,
                    "phone_entered": False,
                    "sms_otp_entered": False,
                    "final_username": "",
                    "email": "kellyfrench8406123880@gmail.com",
                },
                None,
                "172",
            )

        self.assertTrue(result["phone_entered"])
        self.assertTrue(result["sms_otp_entered"])
        self.assertEqual(result["final_username"], "cleaopala")
        self.assertEqual(
            phone_fetcher.await_args_list,
            [mock.call(force_new=False), mock.call(force_new=True)],
        )
        self.assertEqual(fill_phone.await_count, 2)
        fill_phone.assert_has_awaits([
            mock.call(page, "+15550000000", None, "172"),
            mock.call(page, "+15551234567", None, "172"),
        ])
        sms_fetcher.assert_awaited_once()
        type_otp.assert_awaited_once()

    async def test_phone_verification_rejected_after_replacement_exhausted(self):
        class FakeVerificationPage:
            async def wait_for_timeout(self, _ms):
                return None

        page = FakeVerificationPage()
        phone_fetcher = mock.AsyncMock(side_effect=["+15550000000", "+15551234567", "+15557654321"])

        with mock.patch.object(signup_flow, "_resolve_active_signup_page", mock.AsyncMock(return_value=page)), \
            mock.patch.object(signup_flow, "_wait_for_signup_progress", mock.AsyncMock(side_effect=["phone", "phone", "phone"])), \
            mock.patch.object(signup_flow, "_fill_and_submit_phone_number", mock.AsyncMock(return_value=True)):
            with self.assertRaisesRegex(RuntimeError, "phone_verification_rejected"):
                await signup_flow._handle_optional_phone_sms_verification(
                    page,
                    phone_fetcher,
                    mock.AsyncMock(return_value="222222"),
                    {
                        "reached_verification": True,
                        "otp_entered": True,
                        "phone_entered": False,
                        "sms_otp_entered": False,
                        "final_username": "",
                        "email": "kellyfrench8406123880@gmail.com",
                    },
                    None,
                    "172",
                )

        self.assertEqual(
            phone_fetcher.await_args_list,
            [
                mock.call(force_new=False),
                mock.call(force_new=True),
                mock.call(force_new=True),
            ],
        )

    async def test_perform_signup_continues_on_non_latin_signup_page(self):
        page = FakePage(lang_info={"lang": "ar", "nonLatin": 25, "latin": 0})
        fill_form = mock.AsyncMock(return_value={"submitted": True, "username": "wilzxcute"})
        handle_verification = mock.AsyncMock(
            return_value={
                "reached_verification": False,
                "otp_entered": False,
                "final_username": "",
                "email": "",
            }
        )

        with mock.patch.object(signup_flow, "_keep_signup_page_clear", mock.AsyncMock(return_value=False)), \
            mock.patch.object(signup_flow, "_wait_visible", mock.AsyncMock(return_value=True)), \
            mock.patch.object(signup_flow, "get_random_name", return_value="Wilz"), \
            mock.patch.object(signup_flow, "generate_birthday", return_value={"month": 7, "day": 6, "year": "2004"}), \
            mock.patch.object(signup_flow, "_fill_signup_form", fill_form), \
            mock.patch.object(signup_flow, "_handle_verification", handle_verification):
            result = await signup_flow.perform_snapchat_signup(
                page,
                model="Willow",
                username="wilzxcute",
                email="",
                names_dir=Path("."),
                logger=None,
                profile_id="k1pl",
                otp_fetcher=mock.AsyncMock(return_value="123456"),
            )

        self.assertEqual(result["error"], "")
        fill_form.assert_awaited_once()
        handle_verification.assert_awaited_once()


class SignupUsernameRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_fill_signup_form_uses_snapboard_row_password(self):
        class FakeSignupPage:
            async def bring_to_front(self):
                return None

            async def evaluate(self, _script, selector=None):
                if selector == "#password":
                    return "set"
                return "ok"

        page = FakeSignupPage()
        typed = []

        async def record_type(_page, selector, value, _logger=None, _label=""):
            typed.append((selector, value))
            return True

        with mock.patch.object(signup_flow, "_human_pause", mock.AsyncMock()), \
            mock.patch.object(signup_flow, "_keep_signup_page_clear", mock.AsyncMock(return_value=False)), \
            mock.patch.object(signup_flow, "_js_select_month", mock.AsyncMock(return_value=True)), \
            mock.patch.object(signup_flow, "_humanized_type", mock.AsyncMock(side_effect=record_type)), \
            mock.patch.object(signup_flow, "_click_signup_submit", mock.AsyncMock(return_value=True)):
            result = await signup_flow._fill_signup_form(
                page,
                snap_name="Clea",
                birthday={"month": 7, "day": 6, "year": "2004"},
                username="cleaopala",
                password="KyotoRiver%12",
                logger=None,
                profile_id="505811",
            )

        self.assertTrue(result["submitted"])
        self.assertIn(("#password", "KyotoRiver%12"), typed)

    async def test_click_signup_submit_prefers_agree_continue_button(self):
        page = FakePage(text="Sign Up Step 1 of 3")

        with mock.patch.object(signup_flow, "_visible_any", mock.AsyncMock(side_effect=lambda _page, selectors: selectors[0])), \
            mock.patch.object(signup_flow, "_keep_signup_page_clear", mock.AsyncMock(return_value=False)), \
            mock.patch.object(signup_flow, "_wait_enabled", mock.AsyncMock(return_value=True)), \
            mock.patch.object(signup_flow, "_human_pause", mock.AsyncMock()), \
            mock.patch.object(signup_flow, "_js_click", mock.AsyncMock(return_value=True)) as js_click:
            self.assertTrue(await signup_flow._click_signup_submit(page))

        js_click.assert_awaited_once_with(
            page,
            "button:has-text('Agree and Continue')",
            timeout_ms=1500,
        )

    async def test_click_signup_submit_keeps_faster_clear_and_human_pause_windows(self):
        page = FakePage(text="Sign Up Step 1 of 3")
        clear_windows = []
        pause_windows = []

        async def record_clear(_page, _logger, _profile_id, duration_ms):
            clear_windows.append(duration_ms)
            return False

        async def record_pause(_page, min_ms, max_ms):
            pause_windows.append((min_ms, max_ms))

        with mock.patch.object(signup_flow, "_visible_any", mock.AsyncMock(side_effect=lambda _page, selectors: selectors[0])), \
            mock.patch.object(signup_flow, "_keep_signup_page_clear", mock.AsyncMock(side_effect=record_clear)), \
            mock.patch.object(signup_flow, "_wait_enabled", mock.AsyncMock(return_value=True)), \
            mock.patch.object(signup_flow, "_human_pause", mock.AsyncMock(side_effect=record_pause)), \
            mock.patch.object(signup_flow, "_js_click", mock.AsyncMock(return_value=True)):
            self.assertTrue(await signup_flow._click_signup_submit(page))

        self.assertEqual(clear_windows, [800, 500])
        self.assertEqual(pause_windows, [(350, 900)])

    async def test_click_signup_submit_fast_mode_uses_tighter_safe_windows(self):
        page = FakePage(text="Sign Up Step 1 of 3")
        clear_windows = []
        pause_windows = []

        async def record_clear(_page, _logger, _profile_id, duration_ms):
            clear_windows.append(duration_ms)
            return False

        async def record_pause(_page, min_ms, max_ms):
            pause_windows.append((min_ms, max_ms))

        with mock.patch.object(signup_flow, "_visible_any", mock.AsyncMock(side_effect=lambda _page, selectors: selectors[0])), \
            mock.patch.object(signup_flow, "_keep_signup_page_clear", mock.AsyncMock(side_effect=record_clear)), \
            mock.patch.object(signup_flow, "_wait_enabled", mock.AsyncMock(return_value=True)), \
            mock.patch.object(signup_flow, "_human_pause", mock.AsyncMock(side_effect=record_pause)), \
            mock.patch.object(signup_flow, "_js_click", mock.AsyncMock(return_value=True)):
            self.assertTrue(await signup_flow._click_signup_submit(page, fast=True))

        self.assertEqual(clear_windows, [250, 150])
        self.assertEqual(pause_windows, [(90, 220)])

    async def test_click_signup_submit_fast_mode_uses_bounded_waits(self):
        page = FakePage(text="Sign Up Step 1 of 3")

        with mock.patch.object(signup_flow, "_visible_any", mock.AsyncMock(side_effect=lambda _page, selectors: selectors[0])), \
            mock.patch.object(signup_flow, "_keep_signup_page_clear", mock.AsyncMock(return_value=False)), \
            mock.patch.object(signup_flow, "_wait_enabled", mock.AsyncMock(return_value=False)) as wait_enabled, \
            mock.patch.object(signup_flow, "_human_pause", mock.AsyncMock()), \
            mock.patch.object(signup_flow, "_js_click", mock.AsyncMock(return_value=False)) as js_click:
            self.assertFalse(await signup_flow._click_signup_submit(page, fast=True))

        wait_enabled.assert_awaited_once_with(
            page,
            "button:has-text('Agree and Continue')",
            timeout_ms=1200,
        )
        js_click.assert_awaited_once_with(
            page,
            "button:has-text('Agree and Continue')",
            timeout_ms=1000,
        )

    async def test_click_signup_submit_fast_refreshes_when_captcha_missing_and_submit_disabled(self):
        page = FakePage(text="Sign Up Step 1 of 3", recaptcha=False)

        with mock.patch.object(signup_flow, "_visible_any", mock.AsyncMock(side_effect=lambda _page, selectors: selectors[0])), \
            mock.patch.object(signup_flow, "_keep_signup_page_clear", mock.AsyncMock(return_value=False)), \
            mock.patch.object(signup_flow, "_recaptcha_widget_present", mock.AsyncMock(return_value=False)), \
            mock.patch.object(signup_flow, "_wait_enabled", mock.AsyncMock(return_value=False)) as wait_enabled, \
            mock.patch.object(signup_flow, "_human_pause", mock.AsyncMock()), \
            mock.patch.object(signup_flow, "_js_click", mock.AsyncMock(return_value=True)) as js_click:
            self.assertFalse(await signup_flow._click_signup_submit(page, fast=True))

        wait_enabled.assert_awaited_once_with(
            page,
            "button:has-text('Agree and Continue')",
            timeout_ms=1000,
        )
        js_click.assert_not_awaited()

    async def test_js_click_uses_bounded_locator_fallback_timeout(self):
        class FakeLocator:
            def __init__(self, page):
                self._page = page

            @property
            def first(self):
                return self

            async def click(self, **kwargs):
                self._page.click_kwargs = dict(kwargs)

        class FakeClickPage:
            def __init__(self):
                self.click_kwargs = None

            async def evaluate(self, _script, _selector):
                return False

            def locator(self, _selector):
                return FakeLocator(self)

        page = FakeClickPage()

        self.assertTrue(await signup_flow._js_click(page, "button[type='submit']", timeout_ms=700))
        self.assertEqual(page.click_kwargs, {"force": True, "timeout": 700})

    async def test_username_taken_detector_accepts_shorter_copy(self):
        class FakeUsernameTakenPage:
            async def evaluate(self, _script, markers=None):
                text = "sorry, this username is taken. please try another one."
                return any(marker in text for marker in (markers or []))

        with mock.patch.object(signup_flow, "_visible_any", mock.AsyncMock(return_value="#username")):
            self.assertTrue(
                await signup_flow._is_username_taken_error_visible(FakeUsernameTakenPage())
            )

    async def test_username_retry_detector_accepts_invalid_username_copy(self):
        class FakeInvalidUsernamePage:
            async def evaluate(self, _script, markers=None):
                text = (
                    "invalid username. letters and numbers with an optional hyphen, "
                    "underscore, or period in between please!"
                )
                return any(marker in text for marker in (markers or []))

        with mock.patch.object(signup_flow, "_visible_any", mock.AsyncMock(return_value="#username")):
            self.assertTrue(
                await signup_flow._is_username_taken_error_visible(FakeInvalidUsernamePage())
            )

    async def test_username_taken_retry_wins_over_generic_form_error(self):
        class FakeProgressPage:
            url = "https://accounts.snapchat.com/v2/signup"

            def __init__(self):
                self.waits = []

            async def wait_for_timeout(self, ms):
                self.waits.append(ms)

        page = FakeProgressPage()
        username_state = {"value": "milyaure", "manual_override": False}
        unable_detector = mock.AsyncMock(return_value=True)
        retry_done = False

        async def detect_handoff(_page, *_args, **_kwargs):
            return "email" if retry_done else ""

        async def retry_username(*_args, **_kwargs):
            nonlocal retry_done
            retry_done = True
            return "freshmily"

        with mock.patch.object(signup_flow, "_resolve_active_signup_page", mock.AsyncMock(return_value=page)), \
            mock.patch.object(signup_flow, "_is_account_creation_blocked_visible", mock.AsyncMock(return_value=False)), \
            mock.patch.object(signup_flow, "_is_recaptcha_connect_error_visible", mock.AsyncMock(return_value=False)), \
            mock.patch.object(signup_flow, "_detect_signup_handoff_stage", mock.AsyncMock(side_effect=detect_handoff)), \
            mock.patch.object(signup_flow, "_read_input_value", mock.AsyncMock(side_effect=["milyaure", "freshmily"])), \
            mock.patch.object(signup_flow, "_is_username_taken_error_visible", mock.AsyncMock(side_effect=[True, False])), \
            mock.patch.object(signup_flow, "_is_use_email_switch_visible", mock.AsyncMock(return_value=False)), \
            mock.patch.object(signup_flow, "_visible_any", mock.AsyncMock(return_value="")), \
            mock.patch.object(signup_flow, "_retry_taken_username", mock.AsyncMock(side_effect=retry_username)) as retry_taken, \
            mock.patch.object(signup_flow, "_is_unable_to_process_error_visible", unable_detector), \
            mock.patch.object(signup_flow, "_click_signup_submit", mock.AsyncMock(return_value=True)):
            stage = await signup_flow._wait_for_signup_progress(
                page,
                logger=None,
                profile_id="194",
                timeout_ms=1000,
                username_retry_provider=mock.AsyncMock(return_value="freshmily"),
                username_state=username_state,
            )

        self.assertEqual(stage, "email")
        self.assertEqual(username_state["value"], "freshmily")
        retry_taken.assert_awaited_once()
        unable_detector.assert_not_awaited()
        self.assertEqual(page.waits[0], 700)

    async def test_email_handoff_wins_over_stale_username_taken_error(self):
        class FakeProgressPage:
            url = "https://accounts.snapchat.com/v2/signup"

            def __init__(self):
                self.waits = []

            async def wait_for_timeout(self, ms):
                self.waits.append(ms)

        page = FakeProgressPage()
        username_state = {"value": "milyaure", "manual_override": False}
        retry_taken = mock.AsyncMock(return_value="")
        steps = []

        with mock.patch.object(signup_flow, "_resolve_active_signup_page", mock.AsyncMock(return_value=page)), \
            mock.patch.object(signup_flow, "_is_account_creation_blocked_visible", mock.AsyncMock(return_value=False)), \
            mock.patch.object(signup_flow, "_is_recaptcha_connect_error_visible", mock.AsyncMock(return_value=False)), \
            mock.patch.object(signup_flow, "_detect_signup_handoff_stage", mock.AsyncMock(return_value="email")), \
            mock.patch.object(signup_flow, "_read_input_value", mock.AsyncMock(return_value="milyaure")), \
            mock.patch.object(signup_flow, "_is_username_taken_error_visible", mock.AsyncMock(return_value=True)), \
            mock.patch.object(signup_flow, "_is_use_email_switch_visible", mock.AsyncMock(return_value=False)), \
            mock.patch.object(signup_flow, "_visible_any", mock.AsyncMock(return_value="")), \
            mock.patch.object(signup_flow, "_retry_taken_username", retry_taken), \
            mock.patch.object(signup_flow, "_is_unable_to_process_error_visible", mock.AsyncMock(return_value=True)):
            stage = await signup_flow._wait_for_signup_progress(
                page,
                logger=None,
                profile_id="194",
                timeout_ms=1000,
                username_retry_provider=mock.AsyncMock(return_value="freshmily"),
                username_state=username_state,
                progress_callback=lambda step: steps.append(step),
            )

        self.assertEqual(stage, "email")
        retry_taken.assert_not_awaited()
        self.assertEqual(steps, ["awaiting_email_verification"])
        self.assertEqual(page.waits, [])

    async def test_auto_phone_card_wins_over_stale_username_taken_error(self):
        class FakeProgressPage:
            url = "https://accounts.snapchat.com/v2/signup"

            def __init__(self):
                self.waits = []

            async def wait_for_timeout(self, ms):
                self.waits.append(ms)

        page = FakeProgressPage()
        steps = []

        with mock.patch.object(signup_flow, "_resolve_active_signup_page", mock.AsyncMock(return_value=page)), \
            mock.patch.object(signup_flow, "_is_account_creation_blocked_visible", mock.AsyncMock(return_value=False)), \
            mock.patch.object(signup_flow, "_is_recaptcha_connect_error_visible", mock.AsyncMock(return_value=False)), \
            mock.patch.object(signup_flow, "_detect_signup_handoff_stage", mock.AsyncMock(return_value="")), \
            mock.patch.object(signup_flow, "_read_input_value", mock.AsyncMock(return_value="milyaure")), \
            mock.patch.object(signup_flow, "_is_username_taken_error_visible", mock.AsyncMock(return_value=True)), \
            mock.patch.object(signup_flow, "_is_use_email_switch_visible", mock.AsyncMock(return_value=True)), \
             mock.patch.object(signup_flow, "_click_use_email_instead", mock.AsyncMock(return_value=True)) as click_email, \
            mock.patch.object(signup_flow, "_visible_any", mock.AsyncMock(return_value="")), \
            mock.patch.object(signup_flow, "_retry_taken_username", mock.AsyncMock(return_value="")) as retry_taken, \
            mock.patch.object(signup_flow, "_is_unable_to_process_error_visible", mock.AsyncMock(return_value=False)):
            stage = await signup_flow._wait_for_signup_progress(
                page,
                logger=None,
                profile_id="194",
                timeout_ms=1000,
                username_retry_provider=mock.AsyncMock(return_value="freshmily"),
                username_state={"value": "milyaure", "manual_override": False},
                progress_callback=lambda step: steps.append(step),
            )

        self.assertEqual(stage, "phone")
        click_email.assert_not_awaited()
        retry_taken.assert_not_awaited()
        self.assertEqual(steps, ["awaiting_phone_verification"])

    async def test_unable_to_process_retry_uses_fast_submit_and_short_settle(self):
        class FakeProgressPage:
            url = "https://accounts.snapchat.com/v2/signup"

            def __init__(self):
                self.waits = []

            async def wait_for_timeout(self, ms):
                self.waits.append(ms)

        page = FakeProgressPage()

        with mock.patch.object(signup_flow, "_resolve_active_signup_page", mock.AsyncMock(return_value=page)), \
            mock.patch.object(signup_flow, "_is_account_creation_blocked_visible", mock.AsyncMock(return_value=False)), \
            mock.patch.object(signup_flow, "_is_recaptcha_connect_error_visible", mock.AsyncMock(return_value=False)), \
            mock.patch.object(signup_flow, "_read_input_value", mock.AsyncMock(return_value="")), \
            mock.patch.object(signup_flow, "_is_username_taken_error_visible", mock.AsyncMock(return_value=False)), \
            mock.patch.object(signup_flow, "_detect_signup_handoff_stage", mock.AsyncMock(return_value="")), \
            mock.patch.object(signup_flow, "_is_unable_to_process_error_visible", mock.AsyncMock(side_effect=[True, False])), \
            mock.patch.object(signup_flow, "_visible_any", mock.AsyncMock(return_value="")), \
            mock.patch.object(signup_flow, "_click_signup_submit", mock.AsyncMock(return_value=True)) as click_submit:
            stage = await signup_flow._wait_for_signup_progress(
                page,
                logger=None,
                profile_id="194",
                timeout_ms=1000,
            )

        self.assertEqual(stage, "")
        click_submit.assert_awaited_once_with(page, None, "194", fast=True)
        self.assertEqual(page.waits[0], 300)

    async def test_unable_to_process_rechecks_handoff_before_retry_submit(self):
        class FakeProgressPage:
            url = "https://accounts.snapchat.com/v2/signup"

            def __init__(self):
                self.waits = []

            async def wait_for_timeout(self, ms):
                self.waits.append(ms)

        page = FakeProgressPage()
        steps = []
        detect_calls = 0

        async def detect_handoff(*_args, **_kwargs):
            nonlocal detect_calls
            detect_calls += 1
            if detect_calls == 2:
                return "email_switch"
            return ""

        with mock.patch.object(signup_flow, "_resolve_active_signup_page", mock.AsyncMock(return_value=page)), \
            mock.patch.object(signup_flow, "_is_account_creation_blocked_visible", mock.AsyncMock(return_value=False)), \
            mock.patch.object(signup_flow, "_is_recaptcha_connect_error_visible", mock.AsyncMock(return_value=False)), \
            mock.patch.object(signup_flow, "_read_input_value", mock.AsyncMock(return_value="")), \
            mock.patch.object(signup_flow, "_is_username_taken_error_visible", mock.AsyncMock(return_value=False)), \
            mock.patch.object(signup_flow, "_detect_signup_handoff_stage", mock.AsyncMock(side_effect=detect_handoff)), \
            mock.patch.object(signup_flow, "_is_unable_to_process_error_visible", mock.AsyncMock(side_effect=[True, False])), \
            mock.patch.object(signup_flow, "_click_use_email_instead", mock.AsyncMock(return_value=True)) as click_email, \
            mock.patch.object(signup_flow, "_visible_any", mock.AsyncMock(return_value="")), \
            mock.patch.object(signup_flow, "_click_signup_submit", mock.AsyncMock(return_value=True)) as click_submit:
            stage = await signup_flow._wait_for_signup_progress(
                page,
                logger=None,
                profile_id="194",
                timeout_ms=1000,
                progress_callback=lambda step: steps.append(step),
            )

        self.assertEqual(stage, "phone")
        click_email.assert_not_awaited()
        click_submit.assert_not_awaited()
        self.assertEqual(steps, ["awaiting_phone_verification"])


if __name__ == "__main__":
    unittest.main()
