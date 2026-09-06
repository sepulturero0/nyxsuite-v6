import json
import tempfile
from pathlib import Path
from unittest import mock

import bridge_app
from core import bridge_runtime_config as brc


def test_bridge_config_defaults_tray_icon_and_alarm_off():
    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp)
        config_path = data_dir / "bridge_config.json"

        with mock.patch.object(brc, "DATA_DIR", data_dir), \
                mock.patch.object(brc, "CONFIG_PATH", config_path):
            config = brc.load_bridge_config()

    assert config == {
        "transparent_tray_icon": False,
        "nyxify_failure_alarm_enabled": False,
    }


def test_bridge_config_saves_and_reloads_transparent_tray_icon():
    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp)
        config_path = data_dir / "bridge_config.json"

        with mock.patch.object(brc, "DATA_DIR", data_dir), \
                mock.patch.object(brc, "CONFIG_PATH", config_path):
            saved = brc.save_bridge_config({"transparent_tray_icon": True})
            reloaded = brc.load_bridge_config()

    assert saved["transparent_tray_icon"] is True
    assert reloaded["transparent_tray_icon"] is True


def test_bridge_config_saves_and_reloads_nyxify_failure_alarm():
    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp)
        config_path = data_dir / "bridge_config.json"

        with mock.patch.object(brc, "DATA_DIR", data_dir), \
                mock.patch.object(brc, "CONFIG_PATH", config_path):
            saved = brc.save_bridge_config({"nyxify_failure_alarm_enabled": False})
            reloaded = brc.load_bridge_config()

    assert saved["nyxify_failure_alarm_enabled"] is False
    assert reloaded["nyxify_failure_alarm_enabled"] is False


def test_bridge_config_invalid_json_falls_back_to_default():
    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp)
        data_dir.mkdir(parents=True, exist_ok=True)
        config_path = data_dir / "bridge_config.json"
        config_path.write_text("{not json", encoding="utf-8")

        with mock.patch.object(brc, "DATA_DIR", data_dir), \
                mock.patch.object(brc, "CONFIG_PATH", config_path):
            config = brc.load_bridge_config()

    assert config["transparent_tray_icon"] is False


def test_bridge_config_write_uses_json_file():
    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp)
        config_path = data_dir / "bridge_config.json"

        with mock.patch.object(brc, "DATA_DIR", data_dir), \
                mock.patch.object(brc, "CONFIG_PATH", config_path):
            brc.save_bridge_config({"transparent_tray_icon": True})
            raw = json.loads(config_path.read_text(encoding="utf-8"))

    assert raw == {
        "transparent_tray_icon": True,
        "nyxify_failure_alarm_enabled": False,
    }


def test_bridge_alarm_action_persists_and_clears_active_incidents():
    app = bridge_app.BridgeApp()
    app._nyxify_alarm_tracker = mock.Mock()
    with mock.patch.object(
        bridge_app,
        "save_bridge_config",
        return_value={"nyxify_failure_alarm_enabled": False},
    ) as save:
        result = app._action_set_nyxify_failure_alarm({"enabled": False})

    assert result["enabled"] is False
    assert app._nyxify_failure_alarm_enabled is False
    save.assert_called_once_with({"nyxify_failure_alarm_enabled": False})
    app._nyxify_alarm_tracker.clear.assert_called_once_with()
