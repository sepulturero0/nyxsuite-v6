from pathlib import Path
import json
import re


ROOT = Path(__file__).resolve().parents[1]


def read(path):
    return (ROOT / path).read_text(encoding="utf-8")


def test_nyx_popup_queue_is_removed_from_extension_only():
    popup_html = read("nyx_extension/popup.html")
    popup_js = read("nyx_extension/popup.js")

    for removed in [
        "queueSectionToggle",
        "queueSearchInput",
        "queueTable",
        "markDoneQueueProfileButton",
        "relaunchQueueProfileButton",
        "closeQueueProfileButton",
        "removeQueueProfileButton",
        "Nyx Queue",
    ]:
        assert removed not in popup_html

    for removed in [
        "renderQueueTable",
        "getFilteredQueueRows",
        "getSelectedQueueProfileIds",
        "markDoneSelectedQueueProfiles",
        "closeQueueProfile",
        "queueSearchInput",
        "queueTable",
        "NYX_MARK_DONE_PROFILE",
        "NYX_RELAUNCH_QUEUE_PROFILE",
        "NYX_REMOVE_QUEUE_PROFILE",
    ]:
        assert removed not in popup_js

    assert "Daily Update" in popup_html
    assert "Nyx Scrape" in popup_html
    assert "setupInstallButton" in popup_html


def test_nyxify_popup_queue_is_removed_and_setup_install_is_available():
    popup_html = read("nyxify_extension/popup.html")
    popup_js = read("nyxify_extension/popup.js")

    for removed in [
        "sheetQueue",
        "banProxyButton",
        "removeQueueRowButton",
        "Nyxify Queue",
    ]:
        assert removed not in popup_html

    for removed in [
        "renderSheetQueue",
        "getQueueSignature",
        "syncSelectedRowClass",
        "getSelectedRow",
        "sheetQueue",
        "banProxyButton",
        "removeQueueRowButton",
        "NYXIFY_BAN_PROXY",
        "NYXIFY_REMOVE_QUEUE_ROW",
    ]:
        assert removed not in popup_js

    assert "setupInstallButton" in popup_html
    assert (ROOT / "nyxify_extension" / "setup.html").exists()
    assert (ROOT / "nyxify_extension" / "setup.js").exists()


def test_nyxify_popup_provider_locks_use_yellow_segmented_controls():
    popup_html = read("nyxify_extension/popup.html")
    popup_js = read("nyxify_extension/popup.js")
    popup_css = read("nyxify_extension/styles.css")

    assert 'data-provider-lock="g5"' in popup_html
    assert 'data-provider-lock="tv"' in popup_html
    assert 'data-config-key="lockG5"' in popup_html
    assert 'data-config-key="lockTV"' in popup_html
    assert re.search(r'<button[^>]*data-value="false"[^>]*>AM</button>', popup_html)
    assert re.search(r'<button[^>]*data-value="true"[^>]*>G5</button>', popup_html)
    assert re.search(r'<button[^>]*data-value="false"[^>]*>SP</button>', popup_html)
    assert re.search(r'<button[^>]*data-value="true"[^>]*>TV</button>', popup_html)

    assert "popupLockG5Toggle" not in popup_html
    assert "popupLockTVToggle" not in popup_html
    assert "popupLockG5Toggle" not in popup_js
    assert "popupLockTVToggle" not in popup_js

    assert ".provider-lock-segmented" in popup_css
    assert ".provider-lock-option-active" in popup_css
    assert "#fffb00" in popup_css.lower()
    assert "lockG5" in popup_js
    assert "lockTV" in popup_js


def version_decl(name, text):
    match = re.search(rf'^{name}\s*=\s*"([^"]+)"', text, re.MULTILINE)
    assert match, f"{name} declaration not found"
    return match.group(1)


def test_extension_version_metadata_is_synced():
    expected_version = "6.5.6"
    version_py = read("core/version.py")
    nyx_manifest = json.loads(read("nyx_extension/manifest.json"))
    nyxify_manifest = json.loads(read("nyxify_extension/manifest.json"))
    nyx_version = version_decl("NYX_VERSION", version_py)
    nyxify_version = version_decl("NYXIFY_VERSION", version_py)

    assert nyx_version == expected_version
    assert read("VERSION").strip() == nyx_version
    assert nyxify_version == nyx_version
    assert nyx_manifest["version"] == nyx_version
    assert nyx_manifest["version_name"] == nyx_version
    assert nyxify_manifest["version"] == nyxify_version
    assert nyxify_manifest["version_name"] == nyxify_version
