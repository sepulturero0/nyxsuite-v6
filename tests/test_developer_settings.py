import tempfile
import unittest
from pathlib import Path
from unittest import mock

import bridge_app
from core import device_fleet
from core import developer_settings as ds


class DeveloperSettingsTests(unittest.TestCase):
    def test_settings_are_private_and_slots_are_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            with mock.patch.object(ds, "DATA_DIR", data_dir), \
                    mock.patch.object(ds, "CONFIG_PATH", data_dir / "developer_settings.json"):
                self.assertTrue(ds.load_developer_settings()["device_heartbeat_enabled"])
                self.assertEqual(
                    ds.load_developer_settings()["fleet_api_url"],
                    "https://bhdovyegahohiwsmqdgr.supabase.co/functions/v1/nyxsuite-devices",
                )
                saved = ds.save_developer_settings({
                    "parallel_continuous_pipeline_enabled": True,
                    "parallel_continuous_slots": 99,
                })
                self.assertTrue(saved["parallel_continuous_pipeline_enabled"])
                self.assertEqual(saved["parallel_continuous_slots"], 5)
                self.assertEqual(ds.save_developer_settings({"parallel_continuous_slots": 1})[
                    "parallel_continuous_slots"
                ], 2)

    def test_device_fleet_settings_mask_token_for_dashboard(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            with mock.patch.object(ds, "DATA_DIR", data_dir), \
                    mock.patch.object(ds, "CONFIG_PATH", data_dir / "developer_settings.json"):
                saved = ds.save_developer_settings({
                    "device_heartbeat_enabled": True,
                    "fleet_api_url": " https://example.supabase.co/functions/v1/nyxsuite-devices ",
                    "fleet_token": "secret-token",
                    "device_name": "Operator Mac",
                })
                public = ds.public_developer_settings(saved)

        self.assertTrue(public["device_heartbeat_enabled"])
        self.assertEqual(public["fleet_api_url"], "https://example.supabase.co/functions/v1/nyxsuite-devices")
        self.assertEqual(public["device_name"], "Operator Mac")
        self.assertTrue(public["fleet_token_configured"])
        self.assertNotIn("fleet_token", public)

    def test_device_fleet_payload_is_minimal(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            with mock.patch.object(device_fleet, "DATA_DIR", data_dir), \
                    mock.patch.object(device_fleet, "DEVICE_ID_PATH", data_dir / "device_fleet_id.txt"), \
                    mock.patch.object(device_fleet, "default_device_name", return_value="Default Device"), \
                    mock.patch.object(device_fleet.sys, "platform", "darwin"):
                payload = device_fleet.build_heartbeat_payload({
                    "device_name": "Mac Mini",
                    "nyx_state": "RUNNING",
                    "nyxify_state": "RUNNING",
                }, version="6.7.13")

        self.assertEqual(set(payload), {"device_id", "device_name", "os", "app_version"})
        self.assertEqual(payload["device_name"], "Mac Mini")
        self.assertEqual(payload["os"], "macos")
        self.assertNotIn("nyx_state", payload)
        self.assertNotIn("nyxify_state", payload)

    def test_device_fleet_heartbeat_is_low_frequency(self):
        self.assertEqual(device_fleet.HEARTBEAT_INTERVAL_SECONDS, 300.0)
        self.assertEqual(device_fleet.ACTIVE_WINDOW_SECONDS, 660.0)

    def test_device_fleet_enrollment_does_not_need_an_existing_token(self):
        with mock.patch.object(device_fleet, "_request_json", return_value={
                    "ok": True,
                    "device_token": "device-only-token",
                }) as request:
            result = device_fleet.enroll_device({"fleet_api_url": "https://fleet.example"}, version="6.7.16")

        self.assertTrue(result["ok"])
        self.assertEqual(result["device_token"], "device-only-token")
        self.assertEqual(request.call_args.kwargs["require_token"], False)

    def test_bridge_stores_auto_enrolled_device_token(self):
        app = bridge_app.BridgeApp()
        configured = {"device_heartbeat_enabled": True, "fleet_token": "device-only-token"}
        with mock.patch.object(bridge_app, "load_developer_settings", return_value={
                    "device_heartbeat_enabled": True,
                    "fleet_token": "",
                }), \
                mock.patch.object(bridge_app, "enroll_device", return_value={
                    "ok": True,
                    "device_token": "device-only-token",
                }), \
                mock.patch.object(bridge_app, "save_developer_settings", return_value=configured) as save:
            settings, error = app._fleet_settings_with_enrollment(version="6.7.16")

        self.assertIsNone(error)
        self.assertEqual(settings["fleet_token"], "device-only-token")
        save.assert_called_once_with({"fleet_token": "device-only-token"})

    def test_settings_require_fixed_pin_session_before_read_or_save(self):
        app = bridge_app.BridgeApp()
        self.assertTrue(app._action_developer_settings({})["locked"])
        self.assertFalse(app._action_developer_unlock({"pin": "000000"})["ok"])
        app._developer_unlock_not_before = 0.0

        with mock.patch.object(bridge_app, "load_developer_settings", return_value={
                    "parallel_continuous_slots": 2,
                    "fleet_token": "secret-token",
                }), \
                mock.patch.object(bridge_app, "save_developer_settings", return_value={
                    "parallel_continuous_slots": 3,
                    "fleet_token": "secret-token",
                }) as save:
            unlocked = app._action_developer_unlock({"pin": "093180"})
            self.assertTrue(unlocked["ok"])
            self.assertNotIn("fleet_token", unlocked["settings"])
            self.assertTrue(unlocked["settings"]["fleet_token_configured"])
            session = unlocked["developer_session"]
            self.assertTrue(app._action_developer_settings({"developer_session": session})["ok"])
            saved = app._action_save_developer_settings({
                "developer_session": session,
                "parallel_continuous_slots": 3,
            })
            self.assertTrue(saved["ok"])
            self.assertNotIn("fleet_token", saved["settings"])
            save.assert_called_once()

    def test_device_fleet_actions_require_developer_session(self):
        app = bridge_app.BridgeApp()
        self.assertTrue(app._action_device_fleet_status({})["locked"])
        self.assertTrue(app._action_device_fleet_ping({})["locked"])
