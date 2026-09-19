import tempfile
import json
from pathlib import Path
from unittest import mock
from unittest.mock import patch

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
    assert config["auto_fill_row"] is False
    assert config["top_rows_to_detect"] == 20
    assert config["proxy_priority_enabled"] is False
    assert config["proxy_priority_patterns"] == []
    assert config["proxy_type"] == "off"
    assert config["adaptive_email_provider_enabled"] is False
    assert config["adaptive_phone_provider_enabled"] is False


def test_adaptive_provider_flags_round_trip_through_save():
    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp)
        config_path = data_dir / "nyxify_config.json"

        with mock.patch.object(nrc, "DATA_DIR", data_dir), \
                mock.patch.object(nrc, "CONFIG_PATH", config_path):
            nrc.save_nyxify_config({
                "adaptive_email_provider_enabled": True,
                "adaptive_phone_provider_enabled": True,
            })
            reloaded = nrc.load_nyxify_config()
            assert reloaded["adaptive_email_provider_enabled"] is True
            assert reloaded["adaptive_phone_provider_enabled"] is True

            nrc.save_nyxify_config({
                "adaptive_email_provider_enabled": False,
                "adaptive_phone_provider_enabled": False,
            })
            reloaded = nrc.load_nyxify_config()
            assert reloaded["adaptive_email_provider_enabled"] is False
            assert reloaded["adaptive_phone_provider_enabled"] is False


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


def test_boolean_strings_are_normalized_for_extension_toggle():
    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp)
        config_path = data_dir / "nyxify_config.json"

        with mock.patch.object(nrc, "DATA_DIR", data_dir), \
                mock.patch.object(nrc, "CONFIG_PATH", config_path):
            for raw_value, expected in (
                ("false", False),
                ("0", False),
                ("off", False),
                ("true", True),
                ("1", True),
                ("on", True),
            ):
                config_path.write_text(
                    json.dumps({"disable_extensions_enabled": raw_value}),
                    encoding="utf-8",
                )
                assert nrc.load_nyxify_config()["disable_extensions_enabled"] is expected


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


def test_auto_fill_row_flag_round_trips_through_save():
    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp)
        config_path = data_dir / "nyxify_config.json"

        with mock.patch.object(nrc, "DATA_DIR", data_dir), \
                mock.patch.object(nrc, "CONFIG_PATH", config_path):
            nrc.save_nyxify_config({"auto_fill_row": True})
            reloaded = nrc.load_nyxify_config()
            assert reloaded["auto_fill_row"] is True

            nrc.save_nyxify_config({"auto_fill_row": False})
            reloaded = nrc.load_nyxify_config()
            assert reloaded["auto_fill_row"] is False


def test_top_rows_to_detect_round_trips_through_save():
    with tempfile.TemporaryDirectory() as tmp:
        with patch.object(nrc, "DATA_DIR", Path(tmp)), patch.object(
            nrc, "CONFIG_PATH", Path(tmp) / "nyxify_config.json"
        ):
            nrc.save_nyxify_config({"top_rows_to_detect": 42})
            assert nrc.load_nyxify_config()["top_rows_to_detect"] == 42


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


def test_proxy_type_accepts_only_off_socks5_or_http():
    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp)
        config_path = data_dir / "nyxify_config.json"

        with mock.patch.object(nrc, "DATA_DIR", data_dir), \
                mock.patch.object(nrc, "CONFIG_PATH", config_path):
            nrc.save_nyxify_config({"proxy_type": "SOCKS5"})
            assert nrc.load_nyxify_config()["proxy_type"] == "socks5"

            nrc.save_nyxify_config({"proxy_type": "not-a-proxy"})
            assert nrc.load_nyxify_config()["proxy_type"] == "socks5"
