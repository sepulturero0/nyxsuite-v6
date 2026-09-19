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


def test_current_release_notes_reads_matching_changelog_entry(tmp_path, monkeypatch):
    (tmp_path / "CHANGELOG.md").write_text(
        "# Changelog\n\n"
        "## 2.0.0 - Dashboard polish\n\n"
        "- Added the What's New dialog.\n"
        "- Added version-aware first-open behavior.\n\n"
        "## 1.9.0 - Older release\n\n"
        "- Older item.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(release_updater, "_install_root", lambda: tmp_path)

    result = release_updater.get_current_release_notes("2.0.0")

    assert result["version"] == "2.0.0"
    assert result["title"] == "Dashboard polish"
    assert result["bullets"] == [
        "Added the What's New dialog.",
        "Added version-aware first-open behavior.",
    ]
    assert result["source"] == "CHANGELOG.md"
