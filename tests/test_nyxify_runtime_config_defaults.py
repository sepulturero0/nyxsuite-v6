import tempfile
from pathlib import Path
from unittest import mock

from core import nyxify_runtime_config as nrc


def test_nyxify_defaults_keep_tags_blank_and_disabled():
    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp)
        config_path = data_dir / "nyxify_config.json"

        with mock.patch.object(nrc, "DATA_DIR", data_dir), \
                mock.patch.object(nrc, "CONFIG_PATH", config_path):
            config = nrc.load_nyxify_config()

    assert config["tag_one"] == ""
    assert config["tag_two"] == ""
    assert config["adspower_tags_enabled"] is False
    # Extension turn-off during account creation is OFF by default now.
    assert config["disable_extensions_enabled"] is False
    assert config["keep_profile_open_after_signup"] is False
    assert config["proxy_priority_enabled"] is False
    assert config["proxy_priority_patterns"] == []


def test_disable_extensions_flag_round_trips_through_save():
    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp)
        config_path = data_dir / "nyxify_config.json"

        with mock.patch.object(nrc, "DATA_DIR", data_dir), \
                mock.patch.object(nrc, "CONFIG_PATH", config_path):
            nrc.save_nyxify_config({"disable_extensions_enabled": True})
            reloaded = nrc.load_nyxify_config()
            assert reloaded["disable_extensions_enabled"] is True

            nrc.save_nyxify_config({"disable_extensions_enabled": False})
            reloaded = nrc.load_nyxify_config()
            assert reloaded["disable_extensions_enabled"] is False


def test_keep_profile_open_after_signup_flag_round_trips_through_save():
    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp)
        config_path = data_dir / "nyxify_config.json"

        with mock.patch.object(nrc, "DATA_DIR", data_dir), \
                mock.patch.object(nrc, "CONFIG_PATH", config_path):
            nrc.save_nyxify_config({"keep_profile_open_after_signup": True})
            reloaded = nrc.load_nyxify_config()
            assert reloaded["keep_profile_open_after_signup"] is True

            nrc.save_nyxify_config({"keep_profile_open_after_signup": False})
            reloaded = nrc.load_nyxify_config()
            assert reloaded["keep_profile_open_after_signup"] is False


def test_verification_priority_defaults_to_auto_and_round_trips():
    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp)
        config_path = data_dir / "nyxify_config.json"

        with mock.patch.object(nrc, "DATA_DIR", data_dir), \
                mock.patch.object(nrc, "CONFIG_PATH", config_path):
            assert nrc.load_nyxify_config()["verification_priority"] == "auto"

            nrc.save_nyxify_config({"verification_priority": "PHONE"})
            assert nrc.load_nyxify_config()["verification_priority"] == "phone"

            nrc.save_nyxify_config({"verification_priority": "not-a-mode"})
            assert nrc.load_nyxify_config()["verification_priority"] == "phone"


def test_proxy_priority_round_trips_enabled_and_patterns():
    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp)
        config_path = data_dir / "nyxify_config.json"

        with mock.patch.object(nrc, "DATA_DIR", data_dir), \
                mock.patch.object(nrc, "CONFIG_PATH", config_path):
            nrc.save_nyxify_config({
                "proxy_priority_enabled": True,
                "proxy_priority_patterns": "23\n\n23.54\n 130.24 ",
            })
            reloaded = nrc.load_nyxify_config()

    assert reloaded["proxy_priority_enabled"] is True
    assert reloaded["proxy_priority_patterns"] == ["23", "23.54", "130.24"]
