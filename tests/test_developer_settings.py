import tempfile
import unittest
from pathlib import Path
from unittest import mock

import bridge_app
from core import developer_settings as ds


class DeveloperSettingsTests(unittest.TestCase):
    def test_settings_are_private_and_slots_are_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            with mock.patch.object(ds, "DATA_DIR", data_dir), \
                    mock.patch.object(ds, "CONFIG_PATH", data_dir / "developer_settings.json"):
                saved = ds.save_developer_settings({
                    "parallel_continuous_pipeline_enabled": True,
                    "parallel_continuous_slots": 99,
                })
                self.assertEqual(saved, {
                    "parallel_continuous_pipeline_enabled": True,
                    "parallel_continuous_slots": 5,
                })
                self.assertEqual(ds.save_developer_settings({"parallel_continuous_slots": 1})[
                    "parallel_continuous_slots"
                ], 2)

    def test_settings_require_fixed_pin_session_before_read_or_save(self):
        app = bridge_app.BridgeApp()
        self.assertTrue(app._action_developer_settings({})["locked"])
        self.assertFalse(app._action_developer_unlock({"pin": "000000"})["ok"])
        app._developer_unlock_not_before = 0.0

        with mock.patch.object(bridge_app, "load_developer_settings", return_value={"parallel_continuous_slots": 2}), \
                mock.patch.object(bridge_app, "save_developer_settings", return_value={"parallel_continuous_slots": 3}) as save:
            unlocked = app._action_developer_unlock({"pin": "093180"})
            self.assertTrue(unlocked["ok"])
            session = unlocked["developer_session"]
            self.assertTrue(app._action_developer_settings({"developer_session": session})["ok"])
            saved = app._action_save_developer_settings({
                "developer_session": session,
                "parallel_continuous_slots": 3,
            })
            self.assertTrue(saved["ok"])
            save.assert_called_once()
