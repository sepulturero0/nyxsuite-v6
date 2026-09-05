import sys
import types
import unittest
from urllib.parse import urlparse
from unittest import mock


class _RequestsResponse:
    status_code = 200
    ok = True
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


class Response:
    def __init__(self, payload=None, ok=True, status_code=200):
        self._payload = payload or {}
        self.ok = ok
        self.status_code = status_code

    def json(self):
        return dict(self._payload)


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    async def sleep(self, seconds):
        self.now += float(seconds)


class BridgeValueWaitTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._old_token = nyxify_runner.NYXIFY_LOCAL_API_TOKEN
        self._old_cached = nyxify_runner._LOCAL_API_TOKEN_CACHED
        nyxify_runner.NYXIFY_LOCAL_API_TOKEN = "token"
        nyxify_runner._LOCAL_API_TOKEN_CACHED = True
        self.addCleanup(self._restore_token)

    def _restore_token(self):
        nyxify_runner.NYXIFY_LOCAL_API_TOKEN = self._old_token
        nyxify_runner._LOCAL_API_TOKEN_CACHED = self._old_cached

    async def test_email_terminal_bridge_error_returns_after_first_status_result(self):
        clock = FakeClock()
        status_calls = []

        def fake_get(url, **_kwargs):
            status_calls.append(url)
            return Response({"ok": True, "done": True, "email": "", "error": "No pending email order."})

        with mock.patch.object(nyxify_runner._requests, "post", return_value=Response({"ok": True})), \
            mock.patch.object(nyxify_runner._requests, "get", side_effect=fake_get), \
            mock.patch.object(nyxify_runner.time, "monotonic", side_effect=clock.monotonic), \
            mock.patch.object(nyxify_runner.asyncio, "sleep", side_effect=clock.sleep):
            email = await nyxify_runner._request_snapboard_email("snapboard:1", timeout_seconds=30)

        self.assertEqual(email, "")
        self.assertEqual(len(status_calls), 1)
        self.assertLess(clock.now, 2)

    async def test_email_refreshes_snapboard_and_retries_when_bridge_never_dispatches_request(self):
        clock = FakeClock()
        post_paths = []
        email_requests = 0
        email_status_calls = 0
        refresh_status_calls = 0

        def fake_post(url, **_kwargs):
            nonlocal email_requests
            path = urlparse(url).path
            post_paths.append(path)
            if path == "/email/request":
                email_requests += 1
            return Response({"ok": True, "request_id": "refresh:1"})

        def fake_get(url, **_kwargs):
            nonlocal email_status_calls, refresh_status_calls
            path = urlparse(url).path
            if path == "/snapboard_refresh/status":
                refresh_status_calls += 1
                return Response({"ok": True, "done": True, "success": True})
            if path == "/email/status":
                email_status_calls += 1
                if email_requests == 1:
                    return Response({
                        "ok": True,
                        "done": False,
                        "requested": True,
                        "dispatched": False,
                        "age_seconds": 12,
                    })
                return Response({"ok": True, "done": True, "email": "fresh@example.com"})
            return Response({"ok": False}, ok=False, status_code=404)

        with mock.patch.object(nyxify_runner._requests, "post", side_effect=fake_post), \
            mock.patch.object(nyxify_runner._requests, "get", side_effect=fake_get), \
            mock.patch.object(nyxify_runner.time, "monotonic", side_effect=clock.monotonic), \
            mock.patch.object(nyxify_runner.asyncio, "sleep", side_effect=clock.sleep):
            email = await nyxify_runner._request_snapboard_email("snapboard:1", timeout_seconds=120)

        self.assertEqual(email, "fresh@example.com")
        self.assertEqual(post_paths, ["/email/request", "/snapboard_refresh/request", "/email/request"])
        self.assertEqual(email_status_calls, 2)
        self.assertEqual(refresh_status_calls, 1)

    async def test_email_refresh_retry_preserves_original_deadline(self):
        clock = FakeClock()
        email_requests = 0

        def fake_post(url, **_kwargs):
            nonlocal email_requests
            path = urlparse(url).path
            if path == "/email/request":
                email_requests += 1
            return Response({"ok": True, "request_id": "refresh:1"})

        async def fake_refresh(*_args, **_kwargs):
            await clock.sleep(2)
            return True

        def fake_get(url, **_kwargs):
            path = urlparse(url).path
            if path == "/email/status":
                if email_requests == 1:
                    return Response({
                        "ok": True,
                        "done": False,
                        "requested": True,
                        "dispatched": False,
                        "age_seconds": 8,
                    })
                if clock.now >= 20:
                    return Response({"ok": True, "done": True, "email": "late@example.com"})
                return Response({"ok": True, "done": False, "requested": True, "dispatched": True})
            return Response({"ok": False}, ok=False, status_code=404)

        with mock.patch.object(nyxify_runner._requests, "post", side_effect=fake_post), \
            mock.patch.object(nyxify_runner._requests, "get", side_effect=fake_get), \
            mock.patch.object(nyxify_runner, "_request_snapboard_refresh", new=mock.AsyncMock(side_effect=fake_refresh)), \
            mock.patch.object(nyxify_runner.time, "monotonic", side_effect=clock.monotonic), \
            mock.patch.object(nyxify_runner.asyncio, "sleep", side_effect=clock.sleep):
            email = await nyxify_runner._request_snapboard_email("snapboard:1", timeout_seconds=12)

        self.assertEqual(email, "")
        self.assertLess(clock.now, 15)

    async def test_phone_returns_empty_when_snapboard_refresh_fails(self):
        clock = FakeClock()
        post_paths = []

        def fake_post(url, **_kwargs):
            path = urlparse(url).path
            post_paths.append(path)
            return Response({"ok": True, "request_id": "refresh:1"})

        def fake_get(url, **_kwargs):
            path = urlparse(url).path
            if path == "/snapboard_refresh/status":
                return Response({"ok": True, "done": True, "success": False, "error": "No SnapBoard tab."})
            return Response({
                "ok": True,
                "done": False,
                "requested": True,
                "dispatched": False,
                "age_seconds": 12,
            })

        with mock.patch.object(nyxify_runner._requests, "post", side_effect=fake_post), \
            mock.patch.object(nyxify_runner._requests, "get", side_effect=fake_get), \
            mock.patch.object(nyxify_runner.time, "monotonic", side_effect=clock.monotonic), \
            mock.patch.object(nyxify_runner.asyncio, "sleep", side_effect=clock.sleep):
            phone = await nyxify_runner._request_snapboard_phone("snapboard:1", timeout_seconds=120)

        self.assertEqual(phone, "")
        self.assertEqual(post_paths, ["/phone/request", "/snapboard_refresh/request"])

    async def test_email_request_refreshes_stale_token_after_unauthorized_response(self):
        post_headers = []

        def fake_post(_url, json=None, headers=None, **_kwargs):
            post_headers.append(dict(headers or {}))
            token = (headers or {}).get("X-Nyxify-Token")
            if token == "stale":
                return Response({"ok": False, "error": "Unauthorized request."}, ok=False, status_code=401)
            self.assertEqual(json.get("token"), "fresh")
            return Response({"ok": True})

        def fake_get(url, **_kwargs):
            if url.endswith("/token"):
                return Response({"ok": True, "token": "fresh"})
            return Response({"ok": True, "done": True, "email": "fresh@example.com"})

        nyxify_runner.NYXIFY_LOCAL_API_TOKEN = "stale"
        nyxify_runner._LOCAL_API_TOKEN_CACHED = True

        with mock.patch.object(nyxify_runner._requests, "post", side_effect=fake_post), \
            mock.patch.object(nyxify_runner._requests, "get", side_effect=fake_get), \
            mock.patch.object(nyxify_runner.asyncio, "sleep", new=mock.AsyncMock()):
            email = await nyxify_runner._request_snapboard_email("snapboard:1", timeout_seconds=30)

        self.assertEqual(email, "fresh@example.com")
        self.assertEqual(
            [headers.get("X-Nyxify-Token") for headers in post_headers],
            ["stale", "fresh"],
        )

    async def test_otp_refreshes_snapboard_and_retries_after_timeout(self):
        class FakeOtpStore:
            def __init__(self):
                self.requests = 0
                self.clears = 0

            def request_otp_for_row(self, row_key):
                self.requests += 1
                self.row_key = row_key

            def consume_otp_code(self, _row_key):
                return "123456" if self.requests >= 2 else ""

            def clear_otp_request(self, _row_key):
                self.clears += 1

        clock = FakeClock()
        store = FakeOtpStore()
        refresh_mock = mock.AsyncMock(return_value=True)

        with mock.patch.object(nyxify_runner, "_request_snapboard_refresh", new=refresh_mock), \
            mock.patch.object(nyxify_runner.time, "monotonic", side_effect=clock.monotonic), \
            mock.patch.object(nyxify_runner.asyncio, "sleep", side_effect=clock.sleep):
            code = await nyxify_runner._request_snapboard_otp_from_store(
                store,
                "snapboard:1",
                task_id=42,
                timeout_seconds=2,
                poll_seconds=1,
                retry_timeout_seconds=2,
            )

        self.assertEqual(code, "123456")
        self.assertEqual(store.requests, 2)
        self.assertEqual(store.clears, 1)
        refresh_mock.assert_awaited_once()

    async def test_otp_request_uses_exact_submitted_email(self):
        class FakeOtpStore:
            def __init__(self):
                self.requested = []

            def request_otp_for_row(self, row_key, email=""):
                self.requested.append((row_key, email))

            def consume_otp_code(self, _row_key):
                return "123456"

            def clear_otp_request(self, _row_key):
                pass

        store = FakeOtpStore()

        with mock.patch.object(nyxify_runner.asyncio, "sleep", new=mock.AsyncMock()):
            code = await nyxify_runner._request_snapboard_otp_from_store(
                store,
                "snapboard:1",
                task_id=42,
                expected_email="submitted@example.com",
            )

        self.assertEqual(code, "123456")
        self.assertEqual(store.requested, [("snapboard:1", "submitted@example.com")])

    async def test_otp_no_pending_order_is_terminal_for_current_attempt(self):
        class FakeOtpStore:
            def __init__(self):
                self.requests = 0
                self.clears = 0

            def request_otp_for_row(self, _row_key, email=""):
                self.requests += 1

            def consume_otp_result(self, _row_key):
                return {
                    "code": "",
                    "error": "No pending email order for this account. Get email first.",
                }

            def clear_otp_request(self, _row_key):
                self.clears += 1

        store = FakeOtpStore()
        refresh_mock = mock.AsyncMock(return_value=True)

        with mock.patch.object(nyxify_runner, "_request_snapboard_refresh", new=refresh_mock), \
            mock.patch.object(nyxify_runner.asyncio, "sleep", new=mock.AsyncMock()):
            code = await nyxify_runner._request_snapboard_otp_from_store(
                store,
                "snapboard:1",
                task_id=42,
                expected_email="submitted@example.com",
            )

        self.assertEqual(code, "")
        self.assertEqual(store.requests, 1)
        self.assertEqual(store.clears, 1)
        refresh_mock.assert_not_awaited()

    async def test_otp_missing_expected_email_does_not_refresh_retry(self):
        class FakeOtpStore:
            def __init__(self):
                self.requests = 0
                self.clears = 0

            def request_otp_for_row(self, _row_key, email=""):
                self.requests += 1

            def consume_otp_result(self, _row_key):
                return {"code": "", "error": "Missing expected email for OTP check."}

            def clear_otp_request(self, _row_key):
                self.clears += 1

        store = FakeOtpStore()
        refresh_mock = mock.AsyncMock(return_value=True)

        with mock.patch.object(nyxify_runner, "_request_snapboard_refresh", new=refresh_mock), \
            mock.patch.object(nyxify_runner.asyncio, "sleep", new=mock.AsyncMock()):
            code = await nyxify_runner._request_snapboard_otp_from_store(
                store,
                "snapboard:1",
                task_id=42,
            )

        self.assertEqual(code, "")
        self.assertEqual(store.requests, 1)
        self.assertEqual(store.clears, 1)
        refresh_mock.assert_not_awaited()

    async def test_otp_refresh_retry_does_not_extend_original_deadline(self):
        class FakeOtpStore:
            def __init__(self, clock):
                self.clock = clock
                self.requests = 0
                self.clears = 0

            def request_otp_for_row(self, _row_key, email=""):
                self.requests += 1

            def consume_otp_code(self, _row_key):
                if self.requests >= 2 and self.clock.now >= 7:
                    return "123456"
                return ""

            def clear_otp_request(self, _row_key):
                self.clears += 1

        clock = FakeClock()
        store = FakeOtpStore(clock)

        async def fake_refresh(*_args, **_kwargs):
            await clock.sleep(1)
            return True

        with mock.patch.object(nyxify_runner, "_request_snapboard_refresh", new=mock.AsyncMock(side_effect=fake_refresh)), \
            mock.patch.object(nyxify_runner.time, "monotonic", side_effect=clock.monotonic), \
            mock.patch.object(nyxify_runner.asyncio, "sleep", side_effect=clock.sleep):
            code = await nyxify_runner._request_snapboard_otp_from_store(
                store,
                "snapboard:1",
                task_id=42,
                timeout_seconds=4,
                poll_seconds=1,
            )

        self.assertEqual(code, "")
        self.assertLess(clock.now, 7)


if __name__ == "__main__":
    unittest.main()
