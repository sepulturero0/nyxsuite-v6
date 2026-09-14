import json
import socket
import tempfile
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

from core.nyxify_local_api import NyxifyLocalApiServer, _StatusUpdateStore
from core.nyxify_task_store import NyxifyTaskStore


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class StatusUpdateStoreTests(unittest.TestCase):
    def test_request_pop_result_roundtrip(self):
        store = _StatusUpdateStore()
        store.request("snapboard:42", "Banned")

        popped = store.pop_pending()
        self.assertEqual(popped, {"row_key": "snapboard:42", "status": "Banned"})
        # Already dispatched within the debounce window -> not handed out again.
        self.assertIsNone(store.pop_pending())

        store.store_result("snapboard:42", True)
        result = store.get_result("snapboard:42")
        self.assertTrue(result["success"])

    def test_failed_result_is_redispatched(self):
        store = _StatusUpdateStore()
        store.request("snapboard:7", "Banned")
        self.assertIsNotNone(store.pop_pending())

        store.store_result("snapboard:7", False, error="no select")
        # A failure clears the dispatched flag so the poller retries it.
        self.assertEqual(store.pop_pending(), {"row_key": "snapboard:7", "status": "Banned"})


class StatusUpdateApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db_path = Path(self.tmp.name) / "nyxify_tasks.db"
        self.store = NyxifyTaskStore(db_path=str(db_path))
        self.port = _free_port()
        self.server = NyxifyLocalApiServer(
            self.store, host="127.0.0.1", port=self.port, token="testtoken"
        )
        self.server.start()
        self.base = f"http://127.0.0.1:{self.port}"

    def tearDown(self):
        self.server.stop()
        self.tmp.cleanup()

    def _post(self, path, payload):
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.base + path,
            data=data,
            headers={"Content-Type": "application/json", "X-Nyxify-Token": "testtoken"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _get(self, path):
        req = urllib.request.Request(
            self.base + path, headers={"X-Nyxify-Token": "testtoken"}, method="GET"
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def test_status_update_request_pending_result_flow(self):
        # Bitmoji bot requests a Banned status for a SnapBoard row.
        resp = self._post("/status_update/request", {"row_key": "snapboard:99", "status": "Banned"})
        self.assertTrue(resp["ok"])

        # Content script polls and receives it.
        pending = self._get("/status_update/pending")
        self.assertEqual(pending["request"], {"row_key": "snapboard:99", "status": "Banned"})

        # Content script reports success; the bot confirms via /status.
        self._post("/status_update/result", {"row_key": "snapboard:99", "success": True})
        status = self._get("/status_update/status?row_key=snapboard:99")
        self.assertTrue(status["done"])
        self.assertTrue(status["success"])

    def test_status_update_requires_row_and_status(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post("/status_update/request", {"row_key": "snapboard:1"})
        self.assertEqual(ctx.exception.code, 400)

    def test_phone_fetch_request_pending_result_flow(self):
        resp = self._post("/phone/request", {"row_key": "snapboard:99"})
        self.assertTrue(resp["ok"])

        pending = self._get("/phone/pending")
        self.assertEqual(pending["request"], {"row_key": "snapboard:99", "force_new": False})

        self._post("/phone/result", {"row_key": "snapboard:99", "phone": "+15551234567"})
        status = self._get("/phone/status?row_key=snapboard:99")
        self.assertTrue(status["done"])
        self.assertEqual(status["phone"], "+15551234567")

    def test_email_status_reports_undispatched_pending_request(self):
        resp = self._post("/email/request", {"row_key": "snapboard:99"})
        self.assertTrue(resp["ok"])

        status = self._get("/email/status?row_key=snapboard:99")
        self.assertFalse(status["done"])
        self.assertTrue(status["requested"])
        self.assertFalse(status["dispatched"])
        self.assertGreaterEqual(status["age_seconds"], 0)

    def test_sms_request_pending_result_flow(self):
        resp = self._post("/sms/request", {"row_key": "snapboard:99", "phone": "+15551234567"})
        self.assertTrue(resp["ok"])

        pending = self._get("/sms/pending")
        self.assertEqual(pending["request"]["row_key"], "snapboard:99")
        self.assertEqual(pending["request"]["phone"], "+15551234567")
        self.assertEqual(pending["request"]["dispatch_count"], 1)
        self.assertIn("dispatched_at", pending["request"])

        self._post("/sms/result", {"row_key": "snapboard:99", "code": "654321"})
        status = self._get("/sms/status?row_key=snapboard:99")
        self.assertTrue(status["done"])
        self.assertEqual(status["code"], "654321")

    def test_snapboard_refresh_request_pending_result_flow(self):
        resp = self._post("/snapboard_refresh/request", {"reason": "email_fetch_not_dispatched"})
        self.assertTrue(resp["ok"])
        request_id = resp["request_id"]

        pending = self._get("/snapboard_refresh/pending")
        self.assertEqual(pending["request"]["request_id"], request_id)
        self.assertEqual(pending["request"]["reason"], "email_fetch_not_dispatched")

        status = self._get(f"/snapboard_refresh/status?request_id={request_id}")
        self.assertFalse(status["done"])
        self.assertTrue(status["requested"])
        self.assertTrue(status["dispatched"])

        self._post("/snapboard_refresh/result", {"request_id": request_id, "success": True})
        status = self._get(f"/snapboard_refresh/status?request_id={request_id}")
        self.assertTrue(status["done"])
        self.assertTrue(status["success"])

    def test_config_accepts_extension_category_setting(self):
        captured = {}

        def fake_save(updates):
            captured.update(updates)
            return dict(updates)

        with mock.patch("core.nyxify_local_api.save_nyxify_config", side_effect=fake_save):
            resp = self._post("/config", {
                "temporary_profile_name": "Snapchat: from-settings",
                "adspower_group": "",
                "extension_category": "Snapchat Extensions",
                "continuous_mode_enabled": True,
                "adaptive_email_provider_enabled": True,
                "adaptive_phone_provider_enabled": True,
                "keep_profile_open_after_signup": True,
            })

        self.assertTrue(resp["ok"])
        self.assertEqual(captured["temporary_profile_name"], "Snapchat: from-settings")
        self.assertEqual(captured["adspower_group"], "")
        self.assertEqual(captured["extension_category"], "Snapchat Extensions")
        self.assertTrue(captured["continuous_mode_enabled"])
        self.assertTrue(captured["adaptive_email_provider_enabled"])
        self.assertTrue(captured["adaptive_phone_provider_enabled"])
        self.assertTrue(captured["keep_profile_open_after_signup"])


if __name__ == "__main__":
    unittest.main()
