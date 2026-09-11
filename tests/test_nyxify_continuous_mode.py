import asyncio
import sys
import tempfile
import time
import types
import unittest
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
from core.task_store import TaskStore
from core.nyxify_task_store import NyxifyTaskStore


class _FakeLogger:
    def info(self, *_args, **_kwargs):
        pass

    def warning(self, *_args, **_kwargs):
        pass

    def error(self, *_args, **_kwargs):
        pass


class _FakeStore:
    def __init__(self):
        self.updates = []
        self.usernames = []
        self.proxy_updates = []

    def update_task_state(self, task_id, **updates):
        self.updates.append((task_id, dict(updates)))

    def update_task_username(self, row_key, username):
        self.usernames.append((row_key, username))

    def update_task_proxy(self, task_id, proxy_address):
        self.proxy_updates.append((task_id, proxy_address))

    def clear_otp_request(self, _row_key):
        pass

    def request_otp_for_row(self, _row_key):
        pass

    def consume_otp_code(self, _row_key):
        return ""


class _FakeAdsPower:
    def __init__(self, rename_error=None, rotate_during_create=False, events=None):
        self.closed = []
        self.deleted = []
        self.renamed = []
        self.create_calls = []
        self.rotated_proxy = ""
        self.rename_error = rename_error
        self.rotate_during_create = rotate_during_create
        self.events = events if events is not None else []

    def create_profile(self, **kwargs):
        self.create_calls.append(dict(kwargs))
        if self.rotate_during_create:
            self.rotated_proxy = kwargs["proxy_rotator"](
                current_proxy=kwargs["proxy_value"],
                attempt=1,
                reason="gui_proxy_check_failed",
            )
        return {"profile_id": "k1new", "name": "Snapchat:"}

    def close_profile(self, profile_id):
        self.closed.append(profile_id)
        return {"code": 0}

    def delete_profile(self, profile_id):
        self.deleted.append(profile_id)
        return {"code": 0}

    def rename_profile(self, profile_id, new_name):
        if self.rename_error:
            raise self.rename_error
        self.events.append(f"rename:{profile_id}:{new_name}")
        self.renamed.append((profile_id, new_name))
        return {"profile_id": profile_id, "name": new_name}


class _FakePage:
    def __init__(self, url="https://accounts.snapchat.com/v2/signup"):
        self.url = url
        self.closed = False
        self.handlers = {}

    def is_closed(self):
        return self.closed

    async def close(self):
        self.closed = True
        callback = self.handlers.get("close")
        if callback:
            callback()

    def on(self, *_args, **_kwargs):
        if len(_args) >= 2:
            self.handlers[_args[0]] = _args[1]


class _FakeContext:
    def __init__(self):
        self.signup_page = _FakePage()
        self.start_page = _FakePage("https://start.adspower.net/?id=k1new")

    @property
    def pages(self):
        return [self.start_page, self.signup_page]


class _FakePlaywright:
    def __init__(self, events=None, stop_delay_seconds=0):
        self.stopped = False
        self.events = events if events is not None else []
        self.stop_delay_seconds = stop_delay_seconds

    async def stop(self):
        self.events.append("playwright_stop")
        if self.stop_delay_seconds:
            await asyncio.sleep(self.stop_delay_seconds)
        self.stopped = True


async def _fake_rotate_proxy(**kwargs):
    return kwargs["proxy_value"], {"proxy": None}


async def _fake_warmup(*_args, **_kwargs):
    return {"enabled": True, "visited": ["https://wikipedia.org/"]}


async def _fake_signup(**kwargs):
    await kwargs["username_detected_callback"]("cleepink")
    return {"final_username": "cleepink", "otp_entered": True}


async def _fake_signup_without_welcome_username(**kwargs):
    return {
        "final_username": "",
        "otp_entered": True,
        "reached_verification": True,
        "error": "",
    }


async def _fake_snapboard_wait(*_args, **_kwargs):
    return True


class NyxifyContinuousModeTests(unittest.IsolatedAsyncioTestCase):
    async def _run_task(
        self,
        continuous_mode,
        rename_error=None,
        signup_side_effect=_fake_signup,
        adspower_id_update=None,
        rotate_during_create=False,
        snapboard_rotation="",
        keep_profile_open_after_signup=False,
        playwright_stop_delay_seconds=0,
        capture_handoff_state=False,
        capture_events=False,
        snapboard_wait_side_effect=None,
    ):
        store = _FakeStore()
        events = []
        adspower = _FakeAdsPower(
            rename_error=rename_error,
            rotate_during_create=rotate_during_create,
            events=events,
        )
        context = _FakeContext()
        playwright = _FakePlaywright(
            events=events,
            stop_delay_seconds=playwright_stop_delay_seconds,
        )
        handoffs = []
        handoff_observations = []
        adspower_id_update = adspower_id_update or (lambda *_args, **_kwargs: True)
        config = {
            "blocked_proxies": [],
            "proxy_blocker_enabled": False,
            "proxy_checker_enabled": False,
            "adspower_tags_enabled": False,
            "temporary_profile_name": "Snapchat:",
            "adspower_group": "Snapchat",
            "extension_category": "Snap",
            "push_adspower_id_enabled": True,
            "full_auto_mode_enabled": False,
            "continuous_mode_enabled": continuous_mode,
            "keep_profile_open_after_signup": keep_profile_open_after_signup,
            "names_dir": "",
        }
        task = {
            "id": 123,
            "row_key": "snapboard:123",
            "username": "seeduser",
            "model": "Clea",
            "proxy_address": "1.2.3.4:5555:user:pass",
            "email": "person@example.com",
        }

        def fake_handoff(profile_id, model, logger=None, username="", password=""):
            events.append(f"handoff:{profile_id}")
            handoff_observations.append({
                "signup_closed": context.signup_page.closed,
                "playwright_stopped": playwright.stopped,
            })
            handoffs.append((profile_id, model, username, password))
            return {"ok": True, "method": "api"}

        def record_adspower_name_update(_row_key, adspower_name):
            events.append(f"snapboard_name:{adspower_name}")
            return True

        async def fake_snapboard_rotation(*_args, **_kwargs):
            return snapboard_rotation

        async def fake_disable_extensions(*_args, **_kwargs):
            return {
                "playwright_instance": playwright,
                "signup_page": None,
                "context": context,
                "signup_url": "",
            }

        async def fake_warmup(*_args, **_kwargs):
            events.append("cookie_warmup")
            return await _fake_warmup(*_args, **_kwargs)

        async def fake_clear_tabs(*_args, **_kwargs):
            events.append("clear_tabs")
            return {"closed": 0, "kept": 1}

        async def fake_open_signup(*_args, **_kwargs):
            events.append("open_signup")
            return {
                "page": context.signup_page,
                "url": "https://accounts.snapchat.com/v2/signup",
                "method": "new_tab",
            }

        with mock.patch.object(nyxify_runner, "logger", _FakeLogger()), \
                mock.patch.object(nyxify_runner, "load_nyxify_config", return_value=config), \
                mock.patch.object(nyxify_runner, "_rotate_proxy_until_usable", side_effect=_fake_rotate_proxy), \
                mock.patch.object(nyxify_runner, "disable_profile_extensions", side_effect=fake_disable_extensions), \
                mock.patch.object(nyxify_runner, "warm_ads_profile_cookies", side_effect=fake_warmup), \
                mock.patch.object(nyxify_runner, "clear_unrelated_tabs_except_adspower", side_effect=fake_clear_tabs, create=True), \
                mock.patch.object(nyxify_runner, "open_snapchat_signup", side_effect=fake_open_signup), \
                mock.patch.object(nyxify_runner, "perform_snapchat_signup", side_effect=signup_side_effect), \
                mock.patch.object(
                    nyxify_runner,
                    "_wait_for_snapboard_update",
                    side_effect=snapboard_wait_side_effect or _fake_snapboard_wait,
                ), \
                mock.patch.object(nyxify_runner, "_request_snapboard_username_update", return_value=True), \
                mock.patch.object(nyxify_runner, "_request_snapboard_adspower_id_update", side_effect=adspower_id_update), \
                mock.patch.object(nyxify_runner, "_request_snapboard_adspower_name_update", side_effect=record_adspower_name_update), \
                mock.patch.object(nyxify_runner, "_request_snapboard_rotation_sync", return_value=snapboard_rotation, create=True), \
                mock.patch.object(nyxify_runner, "_request_snapboard_rotation", side_effect=fake_snapboard_rotation), \
                mock.patch.object(nyxify_runner, "_play_completion_sound"), \
                mock.patch.object(nyxify_runner, "enqueue_profile_for_nyx", side_effect=fake_handoff):
            await nyxify_runner.process_task(task, store, adspower)

        if capture_events:
            return store, adspower, handoffs, events
        if capture_handoff_state:
            return store, adspower, handoffs, handoff_observations, context, playwright
        return store, adspower, handoffs

    async def test_cookie_warmup_is_visible_step_before_signup_handoff(self):
        store, _adspower, _handoffs = await self._run_task(True)

        steps = [update.get("last_step") for _task_id, update in store.updates if update.get("last_step")]

        # The browser-prep step is "extension_disable_skipped" by default now
        # (extension turn-off is opt-in), or "extensions_disabled" when enabled.
        prep_step = "extension_disable_skipped" if "extension_disable_skipped" in steps else "extensions_disabled"
        self.assertLess(steps.index(prep_step), steps.index("cookie_warmup"))
        self.assertLess(steps.index("cookie_warmup"), steps.index("signup_handoff"))
        self.assertLess(steps.index("signup_handoff"), steps.index("signup_opened"))

    async def test_unrelated_tabs_are_cleared_after_cookie_warmup_before_signup(self):
        _store, _adspower, _handoffs, events = await self._run_task(
            True,
            capture_events=True,
        )

        self.assertLess(events.index("cookie_warmup"), events.index("clear_tabs"))
        self.assertLess(events.index("clear_tabs"), events.index("open_signup"))

    async def test_sms_fetcher_reuses_last_phone_when_called_without_argument(self):
        sms_requests = []

        async def signup_side_effect(**kwargs):
            phone = await kwargs["phone_fetcher"]()
            self.assertEqual(phone, "+15551234567")
            sms_code = await kwargs["sms_fetcher"]()
            self.assertEqual(sms_code, "222222")
            await kwargs["username_detected_callback"]("cleepink")
            return {"final_username": "cleepink", "otp_entered": True}

        async def request_phone(*_args, **_kwargs):
            return "+15551234567"

        async def request_sms(_row_key, timeout_seconds=90, expected_phone=""):
            sms_requests.append({
                "timeout_seconds": timeout_seconds,
                "expected_phone": expected_phone,
            })
            return "222222"

        with mock.patch.object(nyxify_runner, "_request_snapboard_phone", side_effect=request_phone), \
                mock.patch.object(nyxify_runner, "_request_snapboard_sms", side_effect=request_sms):
            await self._run_task(True, signup_side_effect=signup_side_effect)

        self.assertEqual(sms_requests, [{
            "timeout_seconds": nyxify_runner.SNAPBOARD_VERIFICATION_CODE_TIMEOUT_SECONDS,
            "expected_phone": "+15551234567",
        }])

    async def test_continuous_mode_renames_queues_nyx_and_does_not_close(self):
        store, adspower, handoffs = await self._run_task(True)

        self.assertEqual(adspower.renamed, [("k1new", "Snapchat: cleepink")])
        self.assertEqual(adspower.closed, [])
        self.assertEqual(handoffs, [("k1new", "Clea", "cleepink", "")])
        self.assertTrue(any(update.get("last_step") == "queued_for_nyx" for _task_id, update in store.updates))

    async def test_continuous_mode_releases_playwright_before_nyx_handoff_without_closing_signup_tab(self):
        _store, _adspower, handoffs, observations, context, playwright = await self._run_task(
            True,
            capture_handoff_state=True,
        )

        self.assertEqual(handoffs, [("k1new", "Clea", "cleepink", "")])
        self.assertEqual(observations, [{"signup_closed": False, "playwright_stopped": True}])
        self.assertFalse(context.signup_page.closed)
        self.assertFalse(context.start_page.closed)
        self.assertTrue(playwright.stopped)

    async def test_continuous_mode_releases_playwright_before_rename_and_handoff(self):
        _store, adspower, handoffs, events = await self._run_task(
            True,
            capture_events=True,
        )

        self.assertEqual(adspower.renamed, [("k1new", "Snapchat: cleepink")])
        self.assertEqual(handoffs, [("k1new", "Clea", "cleepink", "")])
        self.assertLess(
            events.index("playwright_stop"),
            events.index("rename:k1new:Snapchat: cleepink"),
        )
        self.assertLess(
            events.index("rename:k1new:Snapchat: cleepink"),
            events.index("handoff:k1new"),
        )

    async def test_continuous_mode_does_not_wait_on_slow_playwright_release_before_rename(self):
        started_at = time.monotonic()
        with mock.patch.object(nyxify_runner, "PLAYWRIGHT_RELEASE_TIMEOUT_SECONDS", 0.01):
            _store, adspower, handoffs, events = await self._run_task(
                True,
                playwright_stop_delay_seconds=0.2,
                capture_events=True,
            )
        elapsed = time.monotonic() - started_at

        self.assertEqual(adspower.renamed, [("k1new", "Snapchat: cleepink")])
        self.assertEqual(handoffs, [("k1new", "Clea", "cleepink", "")])
        self.assertLess(elapsed, 0.15)
        self.assertLess(
            events.index("playwright_stop"),
            events.index("rename:k1new:Snapchat: cleepink"),
        )

    async def test_final_username_syncs_full_adspower_name_to_snapboard(self):
        _store, _adspower, _handoffs, events = await self._run_task(
            True,
            capture_events=True,
        )

        self.assertIn("snapboard_name:Snapchat: cleepink", events)
        self.assertNotIn("snapboard_name:cleepink", events)

    async def test_continuous_handoff_does_not_wait_for_adspower_name_confirmation(self):
        waits = []

        async def wait_for_update(_path, _row_key, label, timeout_seconds=30):
            waits.append((label, timeout_seconds))
            return label != "AdsPower name"

        _store, _adspower, handoffs = await self._run_task(
            True,
            snapboard_wait_side_effect=wait_for_update,
        )

        self.assertEqual(handoffs, [("k1new", "Clea", "cleepink", "")])
        self.assertNotIn("AdsPower name", [label for label, _timeout in waits])

    async def test_toggle_off_keeps_existing_close_after_signup_behavior(self):
        _store, adspower, handoffs = await self._run_task(False)

        self.assertEqual(handoffs, [])
        self.assertEqual(adspower.closed, ["k1new"])
        self.assertEqual(adspower.renamed, [("k1new", "Snapchat: cleepink")])

    async def test_keep_profile_open_renames_without_closing_completed_signup(self):
        store, adspower, handoffs = await self._run_task(
            False,
            keep_profile_open_after_signup=True,
        )

        steps = [update.get("last_step") for _task_id, update in store.updates if update.get("last_step")]
        done_updates = [update for _task_id, update in store.updates if update.get("status") == "DONE"]

        self.assertEqual(handoffs, [])
        self.assertEqual(adspower.closed, [])
        self.assertEqual(adspower.renamed, [("k1new", "Snapchat: cleepink")])
        self.assertNotIn("closing_profile", steps)
        self.assertNotIn("profile_closed", steps)
        self.assertTrue(done_updates)
        self.assertEqual(done_updates[-1].get("last_step"), "signup_complete")

    async def test_continuous_mode_rename_failure_still_hands_off_to_nyx(self):
        # The rename is AdsPower bookkeeping only. Once the Snapchat account is
        # real (welcome page confirmed the username), a rename hiccup must not
        # block the Nyx handoff — Bitmoji creation continues, the rename problem
        # stays visible on the row's error text.
        store, adspower, handoffs = await self._run_task(True, rename_error=RuntimeError("rename unavailable"))

        self.assertEqual(adspower.closed, [])
        self.assertEqual(adspower.renamed, [])
        self.assertEqual(handoffs, [("k1new", "Clea", "cleepink", "")])
        self.assertTrue(any(update.get("last_step") == "profile_rename_failed" for _task_id, update in store.updates))
        self.assertTrue(any("rename unavailable" in update.get("error", "") for _task_id, update in store.updates))
        self.assertTrue(any(update.get("last_step") == "queued_for_nyx" for _task_id, update in store.updates))
        self.assertTrue(any(update.get("status") == "DONE" for _task_id, update in store.updates))

    async def test_continuous_wait_guard_detects_active_nyx_handoff(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "nyx_tasks.db"
            store = TaskStore(db_path=str(db_path))
            task_id, _action = store.upsert_task(
                profile_id="k1nyx",
                model="Clea",
                source="nyxify_continuous",
                priority=100,
            )

            self.assertTrue(nyxify_runner._continuous_nyx_handoff_active(db_path=str(db_path)))

            store.update_status(task_id, "DONE", "completed")

            self.assertFalse(nyxify_runner._continuous_nyx_handoff_active(db_path=str(db_path)))

    async def test_continuous_wait_guard_marks_ready_pending_nyxify_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = NyxifyTaskStore(db_path=str(Path(tmp) / "nyxify_tasks.db"))
            store.upsert_task(
                row_key="snapboard:ready",
                model="Clea",
                ip_address="1.2.3.4:5555",
                proxy_address="1.2.3.4:5555:user:pass",
                username="readyuser",
                email="person@example.com",
            )

            count = nyxify_runner._mark_pending_tasks_waiting_for_continuous_nyx(store)

            self.assertEqual(count, 1)
            row = store.list_tasks(limit=1)[0]
            self.assertEqual(row["last_step"], "waiting_for_continuous_nyx")

    async def test_close_path_publishes_closing_profile_before_profile_closed(self):
        # Non-continuous completion must not publish the ready step while the
        # close+rename bookkeeping is still running — the Nyx guard holds on
        # "closing_profile" so Nyx never opens the profile mid-close.
        store, _adspower, _handoffs = await self._run_task(False)

        steps = [update.get("last_step") for _task_id, update in store.updates if update.get("last_step")]
        self.assertIn("closing_profile", steps)
        self.assertIn("profile_closed", steps)
        self.assertLess(steps.index("closing_profile"), steps.index("profile_closed"))
        self.assertNotIn("signup_complete", steps)

        done_updates = [update for _task_id, update in store.updates if update.get("status") == "DONE"]
        self.assertTrue(done_updates)
        self.assertEqual(done_updates[-1].get("last_step"), "closing_profile")

    async def test_otp_without_welcome_username_does_not_complete_or_handoff(self):
        store, adspower, handoffs = await self._run_task(
            True,
            signup_side_effect=_fake_signup_without_welcome_username,
        )

        self.assertEqual(adspower.closed, ["k1new"])
        self.assertEqual(adspower.deleted, ["k1new"])
        self.assertEqual(adspower.renamed, [])
        self.assertEqual(handoffs, [])
        self.assertFalse(any(update.get("status") == "DONE" for _task_id, update in store.updates))
        self.assertTrue(any(
            update.get("status") == "PENDING"
            and str(update.get("last_step") or "").startswith("retry_pending_after_")
            for _task_id, update in store.updates
        ))

    async def test_incomplete_signup_does_not_push_adspower_id_to_snapboard(self):
        adspower_id_updates = []

        def record_adspower_id_update(row_key, adspower_id):
            adspower_id_updates.append((row_key, adspower_id))
            return True

        await self._run_task(
            True,
            signup_side_effect=_fake_signup_without_welcome_username,
            adspower_id_update=record_adspower_id_update,
        )

        self.assertEqual(
            adspower_id_updates,
            [("snapboard:123", "k1new"), ("snapboard:123", "")],
            "Incomplete signup cleanup should clear the deleted AdsPower id",
        )

    async def test_gui_proxy_check_failure_rotator_updates_task_proxy(self):
        store, adspower, _handoffs = await self._run_task(
            False,
            rotate_during_create=True,
            snapboard_rotation="9.9.9.9:9999:user:pass",
        )

        self.assertEqual(adspower.rotated_proxy, "9.9.9.9:9999:user:pass")
        self.assertEqual(store.proxy_updates, [(123, "9.9.9.9:9999:user:pass")])
        self.assertTrue(callable(adspower.create_calls[0]["proxy_rotator"]))
        self.assertTrue(any(
            update.get("last_step") == "refreshing_proxy_after_gui_proxy_check_failed"
            for _task_id, update in store.updates
        ))


if __name__ == "__main__":
    unittest.main()
