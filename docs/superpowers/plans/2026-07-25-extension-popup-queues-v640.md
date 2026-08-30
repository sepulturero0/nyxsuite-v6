# Extension Popup Queue Removal v6.4.0 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship v6.4.0 with Nyx and Nyxify extension popups no longer rendering queue tables, while adding Nyxify `Setup & Install`.

**Architecture:** Keep backend queues and dashboard queue screens untouched. Remove popup-only queue DOM and JavaScript row rendering/selection paths. Copy the existing Nyx setup helper into Nyxify and wire Nyxify's popup button to the same bridge setup behavior.

**Tech Stack:** Chrome extension HTML/CSS/JavaScript, Python pytest static regression tests, existing `scripts/sync_version.py`, existing release ZIP packaging.

---

### Task 1: Popup Regression Tests

**Files:**
- Create: `tests/test_extension_popup_v640.py`

- [ ] **Step 1: Write the failing test**

Create static tests that assert removed popup queue controls are absent and Nyxify setup controls are present:

```python
from pathlib import Path


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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_extension_popup_v640.py -q`

Expected: FAIL because the popup queue markup and scripts still exist and Nyxify setup files do not yet exist.

### Task 2: Remove Popup Queue UI and Wiring

**Files:**
- Modify: `nyx_extension/popup.html`
- Modify: `nyx_extension/popup.js`
- Modify: `nyxify_extension/popup.html`
- Modify: `nyxify_extension/popup.js`

- [ ] **Step 1: Remove Nyx popup queue HTML**

Delete the `Nyx Queue` card from `nyx_extension/popup.html`. Do not remove `Daily Update`, `Nyx Scrape`, `Local Sync`, or `Setup & Install`.

- [ ] **Step 2: Remove Nyx popup queue JavaScript**

Delete queue table globals, queue filtering/selection/render helpers, row action helpers, queue table event listeners, and selected-row queue button listeners from `nyx_extension/popup.js`. Leave runner status counters, runner control actions, daily update, scrape, and setup logic in place.

- [ ] **Step 3: Remove Nyxify popup queue HTML**

Delete the `Nyxify Queue` card from `nyxify_extension/popup.html`. Keep the runner card, counters, control panel, banned proxies, last detected, local sync, and username scrape tab.

- [ ] **Step 4: Remove Nyxify popup queue JavaScript**

Delete queue table globals, queue signature/render/selection helpers, `getSelectedRow`, selected-row queue button listeners, and `sheetQueue` click wiring from `nyxify_extension/popup.js`. Keep status counters and runner actions.

- [ ] **Step 5: Run the popup tests**

Run: `.venv/bin/python -m pytest tests/test_extension_popup_v640.py -q`

Expected: FAIL only on missing Nyxify setup install behavior until Task 3 is complete.

### Task 3: Add Nyxify Setup & Install

**Files:**
- Modify: `nyxify_extension/popup.html`
- Modify: `nyxify_extension/popup.js`
- Create: `nyxify_extension/setup.html`
- Create: `nyxify_extension/setup.js`

- [ ] **Step 1: Add Nyxify popup button**

Add a bottom popup row matching Nyx:

```html
<div class="button-row button-row-compact button-row-single setup-install-row">
  <button id="setupInstallButton" class="button button-secondary button-small" type="button" title="Set up & install Nyx Suite (opens the install web UI)">Setup &amp; Install</button>
</div>
```

- [ ] **Step 2: Add Nyxify setup helper files**

Copy the existing setup helper behavior from the Nyx extension into `nyxify_extension/setup.html` and `nyxify_extension/setup.js`. Use neutral Nyx Suite wording so the setup page applies to both extensions.

- [ ] **Step 3: Wire Nyxify setup behavior**

Add `openSetupInstall()` to `nyxify_extension/popup.js`: when the bridge is running, call `focusOrCreateDashboard(DASHBOARD_URL + "#setup", true)`; when offline, open `chrome.runtime.getURL("setup.html")`. Use this helper for missing-native-host and bridge-timeout statuses.

- [ ] **Step 4: Run the popup tests**

Run: `.venv/bin/python -m pytest tests/test_extension_popup_v640.py -q`

Expected: PASS.

### Task 4: Version and Release Notes

**Files:**
- Modify: `core/version.py`
- Modify: `VERSION`
- Modify: `nyx_extension/manifest.json`
- Modify: `nyxify_extension/manifest.json`
- Modify: `CHANGELOG.md`
- Modify: `RELEASE.md`

- [ ] **Step 1: Bump source version**

Set both `NYX_VERSION` and `NYXIFY_VERSION` in `core/version.py` to `"6.4.0"`, then run `python scripts/sync_version.py`.

- [ ] **Step 2: Document v6.4.0**

Add changelog and release guide notes explaining popup queue removal, Nyxify setup/install parity, and the unchanged dashboard/backend queues.

- [ ] **Step 3: Run version tests**

Run: `.venv/bin/python -m pytest tests/test_extension_popup_v640.py tests/test_release_packaging.py tests/test_rollback_versions.py -q`

Expected: PASS.

### Task 5: Release Build and Publish

**Files:**
- Generated: `dist/NyxSuite-v6.4.0.zip`

- [ ] **Step 1: Build release ZIP**

Run: `bash packaging/create_release_zip.sh --version 6.4.0`

Expected: `dist/NyxSuite-v6.4.0.zip` exists and contains one top-level `NyxSuite-v6.4.0/` folder.

- [ ] **Step 2: Check git scope**

Run: `git status -sb` and `git diff --stat`.

Expected: release files are modified, and pre-existing unrelated data/full-auto username edits remain unstaged.

- [ ] **Step 3: Commit and push master**

Stage only files from this plan plus `dist/NyxSuite-v6.4.0.zip`, commit with `Release v6.4.0 popup performance`, and push `master`.

- [ ] **Step 4: Create GitHub release**

Run:

```bash
gh release create v6.4.0 dist/NyxSuite-v6.4.0.zip \
  --repo sepulturero0/nyxsuite-v6 \
  --title "NyxSuite v6.4.0" \
  --notes-file /tmp/nyxsuite-v6.4.0-notes.md
```

Expected: GitHub release `v6.4.0` is published with the ZIP asset.
