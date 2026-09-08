import unittest
from unittest import mock

from core import signup_flow


def _page():
    page = mock.Mock()
    page.wait_for_timeout = mock.AsyncMock()
    return page


class VerificationPriorityTests(unittest.IsolatedAsyncioTestCase):
    def test_retry_budgets_match_current_verification_rules(self):
        self.assertEqual(signup_flow.PHONE_VERIFICATION_MAX_ATTEMPTS, 3)
        self.assertEqual(signup_flow.EMAIL_SWITCH_MAX_ATTEMPTS, 1)
        self.assertEqual(signup_flow.VERIFICATION_AVAILABLE_METHOD_MAX_ATTEMPTS, 5)

    async def test_phone_priority_switches_from_email_card(self):
        page = _page()
        result = {
            "reached_verification": True,
            "otp_entered": False,
            "phone_entered": True,
            "sms_otp_entered": True,
            "final_username": "",
            "email": "old@example.com",
        }
        click_phone = mock.AsyncMock(return_value=True)

        with mock.patch.object(signup_flow, "_resolve_active_signup_page", mock.AsyncMock(return_value=page)), \
                mock.patch.object(signup_flow, "_wait_for_signup_progress", mock.AsyncMock(return_value="email")), \
                mock.patch.object(signup_flow, "_click_use_phone_instead", click_phone), \
                mock.patch.object(signup_flow, "_handle_optional_phone_sms_verification", mock.AsyncMock(return_value=result)) as handle_phone:
            output = await signup_flow._handle_verification(
                page,
                "old@example.com",
                mock.AsyncMock(),
                None,
                "1",
                phone_fetcher=mock.Mock(),
                sms_fetcher=mock.Mock(),
                verification_priority="phone",
            )

        click_phone.assert_awaited_once_with(page, None, "1")
        handle_phone.assert_awaited_once()
        self.assertTrue(output["phone_entered"])

    async def test_phone_priority_clicks_phone_switch_once_before_running_phone(self):
        page = _page()
        result = {
            "reached_verification": True,
            "otp_entered": False,
            "phone_entered": True,
            "sms_otp_entered": True,
            "final_username": "phoneswitch",
            "email": "old@example.com",
        }
        click_phone = mock.AsyncMock(return_value=True)

        with mock.patch.object(signup_flow, "_resolve_active_signup_page", mock.AsyncMock(return_value=page)), \
                mock.patch.object(signup_flow, "_wait_for_signup_progress", mock.AsyncMock(return_value="email")), \
                mock.patch.object(signup_flow, "_click_use_phone_instead", click_phone), \
                mock.patch.object(signup_flow, "_handle_optional_phone_sms_verification", mock.AsyncMock(return_value=result)) as handle_phone:
            output = await signup_flow._handle_verification(
                page,
                "old@example.com",
                mock.AsyncMock(),
                None,
                "1",
                phone_fetcher=mock.Mock(),
                sms_fetcher=mock.Mock(),
                verification_priority="phone",
            )

        self.assertEqual(click_phone.await_count, 1)
        handle_phone.assert_awaited_once()
        self.assertEqual(output["final_username"], "phoneswitch")

    async def test_email_priority_switches_from_phone_card(self):
        page = _page()
        click_email = mock.AsyncMock(return_value=True)

        with mock.patch.object(signup_flow, "_resolve_active_signup_page", mock.AsyncMock(return_value=page)), \
                mock.patch.object(signup_flow, "_wait_for_signup_progress", mock.AsyncMock(side_effect=["phone", "welcome"])), \
                mock.patch.object(signup_flow, "_click_use_email_instead", click_email), \
                mock.patch.object(signup_flow, "_read_success_username", mock.AsyncMock(return_value="preferredemail")):
            output = await signup_flow._handle_verification(
                page,
                "old@example.com",
                mock.AsyncMock(),
                None,
                "1",
                verification_priority="email",
            )

        click_email.assert_awaited_once_with(page, None, "1")
        self.assertEqual(output["final_username"], "preferredemail")

    async def test_email_priority_clicks_email_switch_once_before_rewaiting(self):
        page = _page()
        click_email = mock.AsyncMock(return_value=True)

        with mock.patch.object(signup_flow, "_resolve_active_signup_page", mock.AsyncMock(return_value=page)), \
                mock.patch.object(signup_flow, "_wait_for_signup_progress", mock.AsyncMock(side_effect=["phone", "welcome"])), \
                mock.patch.object(signup_flow, "_click_use_email_instead", click_email), \
                mock.patch.object(signup_flow, "_read_success_username", mock.AsyncMock(return_value="emailswitch")):
            output = await signup_flow._handle_verification(
                page,
                "old@example.com",
                mock.AsyncMock(),
                None,
                "1",
                verification_priority="email",
            )

        self.assertEqual(click_email.await_count, 1)
        self.assertEqual(output["final_username"], "emailswitch")

    async def test_phone_priority_without_switch_tries_available_email_five_times(self):
        page = _page()
        page.bring_to_front = mock.AsyncMock()
        otp_fetcher = mock.AsyncMock(side_effect=["100001", "100002", "100003", "100004", "100005"])
        email_fetcher = mock.AsyncMock(side_effect=[
            "fresh1@example.com",
            "fresh2@example.com",
            "fresh3@example.com",
            "fresh4@example.com",
        ])
        click_phone = mock.AsyncMock(return_value=False)

        with mock.patch.object(signup_flow, "_resolve_active_signup_page", mock.AsyncMock(return_value=page)), \
                mock.patch.object(signup_flow, "_wait_for_signup_progress", mock.AsyncMock(side_effect=["email", "otp", "otp", "otp", "otp", "otp"])), \
                mock.patch.object(signup_flow, "_click_use_phone_instead", click_phone), \
                mock.patch.object(signup_flow, "_fill_and_submit_verification_email", mock.AsyncMock(return_value=True)), \
                mock.patch.object(signup_flow, "_is_email_already_verified_error_visible", mock.AsyncMock(return_value=False)), \
                mock.patch.object(signup_flow, "_type_otp_code", mock.AsyncMock(return_value=True)), \
                mock.patch.object(signup_flow, "_click_visible_verification_submit", mock.AsyncMock(return_value=True)), \
                mock.patch.object(signup_flow, "_is_wrong_verification_code_error_visible", mock.AsyncMock(return_value=True)), \
                mock.patch.object(signup_flow, "_is_email_verification_step", mock.AsyncMock(return_value=True)), \
                mock.patch.object(signup_flow, "_emit_signup_progress", mock.AsyncMock()):
            output = await signup_flow._handle_verification(
                page,
                "old@example.com",
                otp_fetcher,
                None,
                "1",
                email_fetcher=email_fetcher,
                phone_fetcher=mock.Mock(),
                sms_fetcher=mock.Mock(),
                verification_priority="phone",
            )

        self.assertEqual(click_phone.await_count, 1)
        self.assertEqual(otp_fetcher.await_count, 5)
        self.assertEqual(email_fetcher.await_count, 4)
        self.assertFalse(output["otp_entered"])

    async def test_email_priority_without_switch_tries_available_phone_five_times(self):
        page = _page()
        phone_fetcher = mock.AsyncMock(side_effect=[
            "+15550000001",
            "+15550000002",
            "+15550000003",
            "+15550000004",
            "+15550000005",
        ])
        click_email = mock.AsyncMock(return_value=False)

        with mock.patch.object(signup_flow, "_resolve_active_signup_page", mock.AsyncMock(return_value=page)), \
                mock.patch.object(signup_flow, "_wait_for_signup_progress", mock.AsyncMock(return_value="phone")), \
                mock.patch.object(signup_flow, "_click_use_email_instead", click_email), \
                mock.patch.object(signup_flow, "_fill_and_submit_phone_number", mock.AsyncMock(return_value=True)):
            with self.assertRaisesRegex(RuntimeError, "5 phone number attempt"):
                await signup_flow._handle_verification(
                    page,
                    "old@example.com",
                    mock.AsyncMock(),
                    None,
                    "1",
                    phone_fetcher=phone_fetcher,
                    sms_fetcher=mock.AsyncMock(return_value="222222"),
                    verification_priority="email",
                )

        self.assertEqual(click_email.await_count, 1)
        self.assertEqual(
            phone_fetcher.await_args_list,
            [
                mock.call(force_new=False),
                mock.call(force_new=True),
                mock.call(force_new=True),
                mock.call(force_new=True),
                mock.call(force_new=True),
            ],
        )

    async def test_auto_and_phone_priorities_use_phone_switch_card_as_is(self):
        page = _page()
        for priority in ("auto", "phone"):
            with self.subTest(priority=priority), \
                    mock.patch.object(signup_flow, "_resolve_active_signup_page", mock.AsyncMock(return_value=page)), \
                    mock.patch.object(signup_flow, "_is_account_creation_blocked_visible", mock.AsyncMock(return_value=False)), \
                    mock.patch.object(signup_flow, "_is_recaptcha_connect_error_visible", mock.AsyncMock(return_value=False)), \
                    mock.patch.object(signup_flow, "_read_input_value", mock.AsyncMock(return_value="")), \
                    mock.patch.object(signup_flow, "_detect_signup_handoff_stage", mock.AsyncMock(return_value="email_switch")), \
                    mock.patch.object(signup_flow, "_click_use_email_instead", mock.AsyncMock()) as click_email:
                stage = await signup_flow._wait_for_signup_progress(
                    page,
                    timeout_ms=1000,
                    verification_priority=priority,
                )

            self.assertEqual(stage, "phone")
            click_email.assert_not_awaited()

    async def test_verification_phase_lock_blocks_signup_refresh(self):
        page = _page()
        resubmit = mock.AsyncMock(return_value=True)
        stall_state = {
            "verification_phase": True,
            "refresh_attempts": 0,
            "form_since": None,
            "blank_refill_attempts": 0,
            "page_issue_since": 1.0,
        }

        with mock.patch.object(signup_flow.time, "monotonic", mock.Mock(return_value=999.0)), \
                mock.patch.object(signup_flow, "SIGNUP_STALL_SECONDS", 10), \
                mock.patch.object(signup_flow, "_resolve_active_signup_page", mock.AsyncMock(return_value=page)), \
                mock.patch.object(signup_flow, "_is_account_creation_blocked_visible", mock.AsyncMock(return_value=False)), \
                mock.patch.object(signup_flow, "_is_recaptcha_connect_error_visible", mock.AsyncMock(return_value=False)), \
                mock.patch.object(signup_flow, "_read_input_value", mock.AsyncMock(return_value="")), \
                mock.patch.object(signup_flow, "_detect_signup_handoff_stage", mock.AsyncMock(return_value="")), \
                mock.patch.object(signup_flow, "_is_username_taken_error_visible", mock.AsyncMock(return_value=False)), \
                mock.patch.object(signup_flow, "_is_unable_to_process_error_visible", mock.AsyncMock(return_value=False)), \
                mock.patch.object(signup_flow, "_visible_any", mock.AsyncMock(return_value="")), \
                mock.patch.object(signup_flow, "_is_blank_signup_shell", mock.AsyncMock(return_value=False)):
            stage = await signup_flow._wait_for_signup_progress(
                page,
                timeout_ms=1000,
                resubmit_callback=resubmit,
                stall_state=stall_state,
            )

        self.assertEqual(stage, "verification_unresponsive")
        resubmit.assert_not_awaited()

    async def test_phone_priority_falls_back_to_email_after_phone_failure(self):
        page = _page()
        failed_phone = {
            "reached_verification": True,
            "otp_entered": False,
            "phone_entered": False,
            "sms_otp_entered": False,
            "final_username": "",
            "email": "old@example.com",
        }
        click_phone = mock.AsyncMock(return_value=True)
        click_email = mock.AsyncMock(return_value=True)

        with mock.patch.object(signup_flow, "_resolve_active_signup_page", mock.AsyncMock(return_value=page)), \
                mock.patch.object(signup_flow, "_wait_for_signup_progress", mock.AsyncMock(side_effect=["email", "welcome"])), \
                mock.patch.object(signup_flow, "_click_use_phone_instead", click_phone), \
                mock.patch.object(signup_flow, "_click_use_email_instead", click_email), \
                mock.patch.object(signup_flow, "_handle_optional_phone_sms_verification", mock.AsyncMock(return_value=failed_phone)), \
                mock.patch.object(signup_flow, "_read_success_username", mock.AsyncMock(return_value="emailfallback")):
            output = await signup_flow._handle_verification(
                page,
                "old@example.com",
                mock.AsyncMock(),
                None,
                "1",
                email_fetcher=mock.Mock(),
                phone_fetcher=mock.Mock(),
                sms_fetcher=mock.Mock(),
                verification_priority="phone",
            )

        click_phone.assert_awaited_once_with(page, None, "1")
        click_email.assert_awaited_once_with(page, None, "1")
        self.assertEqual(output["final_username"], "emailfallback")

    async def test_auto_uses_first_phone_card_then_falls_back_to_email(self):
        page = _page()
        failed_phone = {
            "reached_verification": True,
            "otp_entered": False,
            "phone_entered": False,
            "sms_otp_entered": False,
            "final_username": "",
            "email": "old@example.com",
        }
        click_email = mock.AsyncMock(return_value=True)

        with mock.patch.object(signup_flow, "_resolve_active_signup_page", mock.AsyncMock(return_value=page)), \
                mock.patch.object(signup_flow, "_wait_for_signup_progress", mock.AsyncMock(side_effect=["phone", "welcome"])), \
                mock.patch.object(signup_flow, "_click_use_email_instead", click_email), \
                mock.patch.object(signup_flow, "_handle_optional_phone_sms_verification", mock.AsyncMock(return_value=failed_phone)), \
                mock.patch.object(signup_flow, "_read_success_username", mock.AsyncMock(return_value="autofallback")):
            output = await signup_flow._handle_verification(
                page,
                "old@example.com",
                mock.AsyncMock(),
                None,
                "1",
                email_fetcher=mock.Mock(),
                phone_fetcher=mock.Mock(),
                sms_fetcher=mock.Mock(),
                verification_priority="auto",
            )

        click_email.assert_awaited_once_with(page, None, "1")
        self.assertEqual(output["final_username"], "autofallback")

    async def test_email_priority_falls_back_to_phone_after_email_failure(self):
        page = _page()
        successful_phone = {
            "reached_verification": True,
            "otp_entered": False,
            "phone_entered": True,
            "sms_otp_entered": True,
            "final_username": "phonefallback",
            "email": "old@example.com",
        }
        click_phone = mock.AsyncMock(return_value=True)

        with mock.patch.object(signup_flow, "_resolve_active_signup_page", mock.AsyncMock(return_value=page)), \
                mock.patch.object(signup_flow, "_wait_for_signup_progress", mock.AsyncMock(return_value="email")), \
                mock.patch.object(signup_flow, "_fill_and_submit_verification_email", mock.AsyncMock(return_value=True)), \
                mock.patch.object(signup_flow, "_is_email_already_verified_error_visible", mock.AsyncMock(return_value=True)), \
                mock.patch.object(signup_flow, "EMAIL_VERIFY_MAX_ATTEMPTS", 1), \
                mock.patch.object(signup_flow, "_click_use_phone_instead", click_phone), \
                mock.patch.object(signup_flow, "_handle_optional_phone_sms_verification", mock.AsyncMock(return_value=successful_phone)):
            output = await signup_flow._handle_verification(
                page,
                "old@example.com",
                mock.AsyncMock(),
                None,
                "1",
                verification_priority="email",
                phone_fetcher=mock.Mock(),
                sms_fetcher=mock.Mock(),
            )

        click_phone.assert_awaited_once_with(page, None, "1")
        self.assertEqual(output["final_username"], "phonefallback")


if __name__ == "__main__":
    unittest.main()
