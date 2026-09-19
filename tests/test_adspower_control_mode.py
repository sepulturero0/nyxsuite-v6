import tempfile
from pathlib import Path
from unittest import mock

from core import nyx_runtime_config as nrc
from core.adspower import AdsPowerManager


class _FakeResponse:
    status_code = 200
    content = b"{}"

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def test_nyx_config_persists_adspower_control_mode():
    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp)
        config_path = data_dir / "nyx_config.json"

        with mock.patch.object(nrc, "DATA_DIR", data_dir), \
                mock.patch.object(nrc, "CONFIG_PATH", config_path):
            assert nrc.load_nyx_config()["adspower_control_mode"] == "auto"

            saved = nrc.save_nyx_config({"adspower_control_mode": "GUI"})
            assert saved["adspower_control_mode"] == "gui"
            assert nrc.load_nyx_config()["adspower_control_mode"] == "gui"

            saved = nrc.save_nyx_config({"adspower_control_mode": "not-real"})
            assert saved["adspower_control_mode"] == "gui"


def test_api_mode_disables_ads_power_gui_and_cdp_fallbacks():
    with mock.patch(
        "core.nyx_runtime_config.load_nyx_config",
        return_value={"adspower_control_mode": "api"},
    ):
        manager = AdsPowerManager()

    assert manager.control_mode == "api"
    assert manager.ui_fallback_enabled is False
    assert manager.cdp_fallback_enabled is False


def test_gui_mode_create_profile_skips_api_and_uses_gui():
    manager = AdsPowerManager()
    manager.control_mode = "gui"
    manager.ui_fallback_enabled = True
    manager._create_profile_via_api = mock.Mock(side_effect=AssertionError("API should be skipped"))

    fake_ui = mock.Mock()
    fake_ui.create_profile.return_value = {"profile_id": "k1gui", "name": "Snapchat: Pending"}
    manager._ui_controller = mock.Mock(return_value=fake_ui)

    result = manager.create_profile(
        name="Snapchat: Pending",
        proxy_value="1.2.3.4:9999:user:pass",
        group_reference="Snapchat",
    )

    manager._create_profile_via_api.assert_not_called()
    fake_ui.create_profile.assert_called_once()
    assert result["profile_id"] == "k1gui"
    assert "k1gui" in manager._cdp_fallback_profiles


def test_gui_mode_open_profile_skips_api_and_uses_gui():
    manager = AdsPowerManager()
    manager.control_mode = "gui"
    manager.ui_fallback_enabled = True
    manager.cdp_fallback_enabled = True
    manager._open_profile_via_api = mock.Mock(side_effect=AssertionError("API should be skipped"))

    fake_ui = mock.Mock()
    fake_ui.open_profile_by_id.return_value = "ws://127.0.0.1:9999/devtools/browser/gui"
    manager._ui_controller = mock.Mock(return_value=fake_ui)

    with mock.patch("core.adspower_cdp.find_open_profile_cdp_endpoint", return_value=""):
        endpoint = manager.open_profile("k1gui")

    manager._open_profile_via_api.assert_not_called()
    fake_ui.open_profile_by_id.assert_called_once_with("k1gui")
    assert endpoint == "ws://127.0.0.1:9999/devtools/browser/gui"
    assert "k1gui" in manager._cdp_fallback_profiles


def test_gui_mode_preflight_allows_permission_gated_status():
    with mock.patch(
        "core.nyx_runtime_config.load_nyx_config",
        return_value={"adspower_control_mode": "gui"},
    ):
        manager = AdsPowerManager()
    manager.session.get = mock.Mock(
        return_value=_FakeResponse({"code": 9110, "msg": "No local API permission"})
    )
    fake_ui = mock.Mock()
    fake_ui.preflight_check.return_value = True
    manager._ui_controller = mock.Mock(return_value=fake_ui)

    result = manager.preflight_check()

    assert result["ok"] is True
    assert result["code"] == "ok"
    assert "Accessibility" in result["message"]
    manager.session.get.assert_not_called()
