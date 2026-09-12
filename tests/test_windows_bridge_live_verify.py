from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_windows_bridge_live_verify_collects_bridge_startup_evidence():
    script = (ROOT / "tools" / "windows_bridge_live_verify.ps1").read_text(encoding="ascii")

    assert "param(" in script
    assert "portable_launch_nyx.ps1" in script
    assert "Invoke-BridgeCycle" in script
    assert "Invoke-EndpointTimings" in script
    assert "Invoke-ProcessSnapshot" in script
    assert "Invoke-PortableLogAnalysis" in script
    assert "Get-BridgeReady" in script
    assert "Request-TokenFromLocalApi" in script
    assert "Compress-Archive" in script
    assert "http://127.0.0.1:8870/" in script
    assert "http://127.0.0.1:8865/token" in script
    assert "http://127.0.0.1:8866/token" in script
    assert "http://127.0.0.1:8870/bridge/status" in script
    assert "http://127.0.0.1:8865/status" in script
    assert "http://127.0.0.1:8866/status" in script
    assert "Preparing Snap Bitmoji Bot" in script
    assert "Installing the Playwright Chromium runtime" in script
    assert "bridge-on-ms" in script
    assert "bridge-off-port-down-ms" in script
