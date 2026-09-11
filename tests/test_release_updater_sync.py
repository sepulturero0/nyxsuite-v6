from pathlib import Path

from core import release_updater


ROOT = Path(__file__).resolve().parents[1]


def test_empty_staged_agent_host_does_not_wipe_installed_bridge_files(tmp_path):
    staging = tmp_path / "staging"
    install = tmp_path / "install"

    (staging / "agent_host").mkdir(parents=True)
    (install / "agent_host").mkdir(parents=True)
    installed_host = install / "agent_host" / "host_main.py"
    installed_host.write_text("installed bridge host\n", encoding="utf-8")

    synced = release_updater.sync_source_dirs(staging, install)

    assert synced == 0
    assert installed_host.read_text(encoding="utf-8") == "installed bridge host\n"


def test_updater_refreshes_native_host_registration_after_update():
    updater = (ROOT / "packaging" / "updater.py").read_text(encoding="utf-8")

    assert "install_host" in updater
    assert "register()" in updater
    assert "native messaging" in updater.lower()
