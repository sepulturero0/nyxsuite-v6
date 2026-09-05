"""SMS verification repair: tolerant, row-scoped Check SMS detection in the
SnapBoard content script and verified OTP entry into Snapchat before submit.
"""
from pathlib import Path
import unittest
from unittest import mock

from core import signup_flow


ROOT = Path(__file__).resolve().parents[1]


class SmsCheckDetectionSourceTests(unittest.TestCase):
    def test_content_check_sms_is_tolerant_and_row_scoped(self):
        content = (ROOT / "nyxify_extension" / "content.js").read_text(encoding="utf-8")

        # Row-scoped detection helpers that accept button / link / role=button /
        # text / aria-label / title, case-insensitively.
        self.assertIn("function _checkCodeCandidateMatches", content)
        self.assertIn("function _authCheckCandidates", content)
        self.assertIn("function _findAuthCheckButton", content)
        self.assertIn("function _authCheckState", content)
        self.assertIn("waitForExpectedRowValue", content)
        self.assertIn("check_attempts=", content)
        self.assertIn("|| null;", content[content.index("function getRowEl"):content.index("function _isClickableControl")])
        self.assertIn("function _isClickableControl", content)
        # Page-world click with scroll/focus + mousedown/mouseup/click sequence.
        self.assertIn("function clickAuthElement", content)
        self.assertIn("scrollIntoView", content)
        self.assertIn('new MouseEvent("mousedown"', content)
        self.assertIn('new MouseEvent("click"', content)
        # Tolerant attribute / text variants for the SMS control.
        self.assertIn('"data-check-sms"', content)
        self.assertIn('"check sms"', content)
        self.assertIn("aria-label", content.lower())
        self.assertIn("title", content)
        # Exact submitted-phone matching is preserved.
        self.assertIn("function rowMatchesExpectedPhone", content)
        self.assertIn("expected.slice(-10)", content)

    def test_signup_flow_gates_submit_on_typed_otp(self):
        flow = (ROOT / "core" / "signup_flow.py").read_text(encoding="utf-8")

        # Every OTP type is captured and the submit is gated on the verified
        # result, so a code that wasn't actually typed is never submitted.
        self.assertGreaterEqual(flow.count("await _type_otp_code("), 4)
        self.assertGreaterEqual(flow.count("otp_typed = await _type_otp_code("), 4)
        self.assertGreaterEqual(
            flow.count("otp_typed and await _click_visible_verification_submit"), 4
        )

    def test_email_switch_not_clickable_falls_back_to_phone(self):
        flow = (ROOT / "core" / "signup_flow.py").read_text(encoding="utf-8")

        # email -> otp -> phone -> otp: a phone step that appears during the
        # email/OTP path with "Use email instead" is clicked; if that switch is
        # present but never clickable, we fall back to phone -> SMS OTP instead
        # of looping forever.
        self.assertIn("EMAIL_SWITCH_MAX_ATTEMPTS", flow)
        self.assertIn("async def try_email_switch_step", flow)
        self.assertIn('return "phone"', flow)
        self.assertIn("falling back to phone verification", flow)
        self.assertIn('if stage == "phone":', flow)
        self.assertIn(
            "return await _handle_optional_phone_sms_verification(", flow
        )

    def test_email_verify_failure_switches_to_phone(self):
        flow = (ROOT / "core" / "signup_flow.py").read_text(encoding="utf-8")

        # After email fails to verify N times (wrong code / already-verified),
        # click "Use Phone Number Instead" and run the phone -> SMS OTP path.
        self.assertIn("EMAIL_VERIFY_MAX_ATTEMPTS = int(os.getenv", flow)
        self.assertIn("async def _click_use_phone_instead", flow)
        self.assertIn("Use Phone Number Instead", flow)
        self.assertIn("async def switch_to_phone", flow)
        self.assertIn("email_verify_failures = 0", flow)
        self.assertIn(
            "return await switch_to_phone(", flow
        )
        self.assertGreaterEqual(flow.count("return await switch_to_phone("), 2)


class VerifyPhoneIsAPrimaryPathTests(unittest.IsolatedAsyncioTestCase):
    """Phone is NOT a fallback — Snapchat can show it without any "Use email
    instead" switch, or as a second verification after a successful email OTP."""

    def _detect(self, *, otp=False, switch=False, email=False, phone=True):
        page = mock.Mock()
        patchers = [
            mock.patch.object(signup_flow, "_read_success_username", mock.AsyncMock(return_value="")),
            mock.patch.object(signup_flow, "_visible_any", mock.AsyncMock(return_value=True if otp else "")),
            mock.patch.object(signup_flow, "_is_use_email_switch_visible", mock.AsyncMock(return_value=switch)),
            mock.patch.object(signup_flow, "_is_email_verification_step", mock.AsyncMock(return_value=email)),
            mock.patch.object(signup_flow, "_is_phone_verification_step", mock.AsyncMock(return_value=phone)),
        ]
        for p in patchers:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in reversed(patchers)])
        return page

    async def test_phone_without_use_email_instead_is_detected_as_phone(self):
        # The phone screen appears, but there is no "Use email instead" button.
        # This must be treated as a phone path (phone -> SMS OTP), not an
        # email/OTP path.
        page = self._detect(otp=False, switch=False, email=False, phone=True)
        stage = await signup_flow._detect_signup_handoff_stage(page, None, "1")
        self.assertEqual(stage, "phone")

    async def test_phone_with_use_email_instead_is_detected_as_switch(self):
        # Same phone screen but with the "Use email instead" link -> switch to
        # the email/OTP route.
        page = self._detect(otp=False, switch=True, email=False, phone=True)
        stage = await signup_flow._detect_signup_handoff_stage(page, None, "1")
        self.assertEqual(stage, "email_switch")


class PhoneRoutingSourceTests(unittest.TestCase):
    def test_phone_is_routed_to_phone_handler_in_all_contexts(self):
        flow = (ROOT / "core" / "signup_flow.py").read_text(encoding="utf-8")

        # Phone is a primary verification path (not a fallback). It is routed to
        # the phone -> SMS OTP handler in all three contexts:
        #   1) phone shown initially,
        #   2) phone shown during/after the email path (email-OTP not taken),
        #   3) phone shown as a SECOND verification after a successful email OTP.
        self.assertGreaterEqual(
            flow.count("return await _handle_optional_phone_sms_verification("), 3
        )


class TypeOtpVerificationTests(unittest.IsolatedAsyncioTestCase):
    def _page(self, input_values):
        page = mock.Mock()
        loc = mock.Mock()
        loc.is_visible = mock.AsyncMock(return_value=True)
        loc.click = mock.AsyncMock()
        loc.fill = mock.AsyncMock()
        loc.type = mock.AsyncMock()
        loc.input_value = mock.AsyncMock(side_effect=list(input_values))
        page.locator.return_value.first = loc
        return page

    async def _run(self, page, code):
        with mock.patch.object(signup_flow, "_human_pause", mock.AsyncMock()), \
             mock.patch.object(signup_flow, "_random_delay_ms", lambda *_: 1), \
             mock.patch.object(signup_flow, "_humanized_type_only", mock.AsyncMock(return_value=False)):
            return await signup_flow._type_otp_code(
                page, ["input[name='code']"], code, None, "1"
            )

    async def test_mismatched_typed_code_is_not_accepted(self):
        # Each otpDigits input reads back "0" — not the expected code.
        ok = await self._run(self._page("0" * 6), "123456")
        self.assertFalse(ok)

    async def test_verified_code_is_accepted(self):
        # Each otpDigits input reads back the exact expected digits.
        ok = await self._run(self._page("123456"), "123456")
        self.assertTrue(ok)


if __name__ == "__main__":
    unittest.main()
