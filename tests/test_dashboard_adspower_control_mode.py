from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_dashboard_exposes_adspower_control_mode_buttons():
    html = (ROOT / "webui" / "index.html").read_text(encoding="utf-8")
    js = (ROOT / "webui" / "dashboard.js").read_text(encoding="utf-8")

    assert "adspower-mode-auto" in html
    assert "adspower-mode-api" in html
    assert "adspower-mode-gui" in html
    assert "adspower_control_mode" in js
    assert "cfg-adspower_control_mode" in js


def test_dashboard_runner_actions_use_bot_routes():
    js = (ROOT / "webui" / "dashboard.js").read_text(encoding="utf-8")

    assert 'callAction(p, "/bot/" + action, {})' in js
    assert 'callAction(p, action, {})' not in js


def test_dashboard_update_actions_are_single_flight_with_loading_states():
    js = (ROOT / "webui" / "dashboard.js").read_text(encoding="utf-8")
    css = (ROOT / "webui" / "dashboard.css").read_text(encoding="utf-8")

    assert "let updateCheckInFlight = false;" in js
    assert "let updateApplyInFlight = false;" in js
    assert "function renderUpdateActionButtons()" in js
    assert "if (updateCheckInFlight || updateApplyInFlight) return;" in js
    assert "button.btn.is-loading" in css
    assert "dashboardButtonSpin" in css


def test_developer_device_fleet_dashboard_is_minimal():
    js = (ROOT / "webui" / "dashboard.js").read_text(encoding="utf-8")
    css = (ROOT / "webui" / "dashboard.css").read_text(encoding="utf-8")

    assert "Device Fleet" in js
    assert "Enable Device Heartbeat" in js
    assert "<th>Status</th><th>Device Name</th><th>OS</th><th>Version</th><th>Last Seen</th>" in js
    assert "device_fleet_status" in js
    assert "device_fleet_ping" in js
    assert "fleet-dot-active" in css
    assert "fleet-dot-inactive" in css
    assert "<th>Nyx" not in js
    assert "<th>Nyxify" not in js
