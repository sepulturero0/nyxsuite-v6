import json
import subprocess
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_shell_release_zip_uses_portable_native_host_manifest(tmp_path):
    output_dir = tmp_path / "release"
    secret_file = ROOT / "core" / "license_runtime_secret.py"
    secret_file.write_text('SECRET = "do-not-ship"\n', encoding="utf-8")
    try:
        subprocess.run(
            [
                "bash",
                str(ROOT / "packaging" / "create_release_zip.sh"),
                "--version",
                "9.9.9-test",
                "--output-dir",
                str(output_dir),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    finally:
        secret_file.unlink(missing_ok=True)

    zip_path = output_dir / "NyxSuite-v9.9.9-test.zip"
    with zipfile.ZipFile(zip_path) as archive:
        names = set(archive.namelist())
        manifest = json.loads(
            archive.read("NyxSuite-v9.9.9-test/agent_host/com.nyxsuite.agent.json")
        )
        update_config = json.loads(
            archive.read("NyxSuite-v9.9.9-test/update_config.json")
        )

    assert manifest["path"] == "agent_host/host_main.py"
    assert "NyxSuite-v9.9.9-test/core/license_runtime_secret.py" not in names
    assert "NyxSuite-v9.9.9-test/ui_templates/adspower/elements/new_profile_btn.png" in names
    assert update_config["repo"] == "jaymaroldan026/nyxsuite-v6"
    assert update_config["asset_pattern"] == "NyxSuite-v*.zip"
    assert "data/bridge_config.json" in update_config["data_preserve_paths"]


def test_shell_release_zip_accepts_relative_output_dir(tmp_path):
    relative_output = "relative-release"
    subprocess.run(
        [
            "bash",
            str(ROOT / "packaging" / "create_release_zip.sh"),
            "--version",
            "9.9.8-test",
            "--output-dir",
            relative_output,
        ],
        check=True,
        capture_output=True,
        text=True,
        cwd=tmp_path,
    )

    assert (tmp_path / relative_output / "NyxSuite-v9.9.8-test.zip").exists()


# Every user-edited data file must survive an update. The generated catalog
# (bitmoji_catalog.json) is intentionally NOT preserved — releases may replace it.
USER_EDITED_DATA_FILES = [
    "data/bridge_config.json",
    "data/nyx_config.json",
    "data/nyxify_config.json",
    "data/bitmoji_models.json",
    "data/bitmoji_outfits.json",
    "data/full_auto_usernames/",
    "data/signup_names/",
    "data/logs/",
]


def test_all_data_preserve_lists_include_user_edited_files():
    locations = [
        ROOT / "update_config.json",
        ROOT / "core" / "release_updater.py",
        ROOT / "packaging" / "create_release_zip.sh",
        ROOT / "packaging" / "create_release_zip.ps1",
        ROOT / "packaging" / "update_config.template.json",
        ROOT / "packaging" / "updater.py",
    ]
    for location in locations:
        text = location.read_text(encoding="utf-8")
        for entry in USER_EDITED_DATA_FILES:
            assert entry in text, f"{location.name} is missing preserve entry {entry}"


def test_generated_catalog_is_explicitly_replaceable():
    # The (huge, generated) Bitmoji catalog must NOT be in the preserve list —
    # a release is allowed to replace it when the editor catalog changes.
    for location in (
        ROOT / "update_config.json",
        ROOT / "core" / "release_updater.py",
        ROOT / "packaging" / "updater.py",
    ):
        text = location.read_text(encoding="utf-8")
        assert "data/bitmoji_catalog.json" not in text, (
            f"{location.name} must not preserve the generated Bitmoji catalog"
        )
