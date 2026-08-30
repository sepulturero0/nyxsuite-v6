# NyxSuite v6 Release and SnapBoard Password Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Publish the current NyxSuite feature set as v6 in `sepulturero0/nyxsuite-v6` and use SnapBoard row passwords during signup.

**Architecture:** Extend the existing SnapBoard row pipeline with a `password` field from browser content script through local API, SQLite, runner, and signup form. Rebrand visible/package/update metadata to v6 and point the source-based updater at the same public GitHub repository that hosts source and releases.

**Tech Stack:** Python 3.9+, SQLite, Playwright-driven signup flow, Chrome MV3 extensions, GitHub CLI, shell and PowerShell release packaging.

---

### Task 1: Add Password Persistence Tests

**Files:**
- Modify: `tests/test_nyxify_snapboard_bridge.py`
- Test: `tests/test_nyxify_snapboard_bridge.py`

- [ ] Add tests that verify `NyxifyTaskStore.upsert_task(..., password="...")` stores `password`, updates it on re-sync, and returns it from `list_tasks()` / `claim_pending_tasks()`.
- [ ] Run `python -m pytest tests/test_nyxify_snapboard_bridge.py -q` and verify the new tests fail because `password` is not implemented.

### Task 2: Persist Password Through Nyxify Queue

**Files:**
- Modify: `core/nyxify_task_store.py`
- Modify: `core/nyxify_local_api.py`
- Modify: `nyxify_extension/content.js`
- Modify: `nyxify_extension/background.js`
- Test: `tests/test_nyxify_snapboard_bridge.py`

- [ ] Add a `password TEXT NOT NULL DEFAULT ''` task column and migration.
- [ ] Include `password` in selects, row dictionaries, `upsert_task`, and queue upsert parsing.
- [ ] Extract the SnapBoard Password column using aliases `password`, `pass`, `snap password`, `snapchat password`, `account password`.
- [ ] Preserve `password` in background sanitize/merge/flush equality checks.
- [ ] Run `python -m pytest tests/test_nyxify_snapboard_bridge.py -q` and verify it passes.

### Task 3: Use Row Password During Signup

**Files:**
- Modify: `tests/test_signup_blockers.py`
- Modify: `core/signup_flow.py`
- Modify: `nyxify_runner.py`
- Test: `tests/test_signup_blockers.py`

- [ ] Add a failing async signup test that patches `_humanized_type` and asserts `#password` receives a task-specific password.
- [ ] Add `password` parameter support to `_fill_signup_form`, `_reload_and_refill_signup`, `_replace_signup_username`, and `perform_snapchat_signup`.
- [ ] Pass `task["password"]` from `nyxify_runner.py`; fall back to the default constant when blank.
- [ ] Keep logs redacted by reporting whether password is set, not the raw value.
- [ ] Run `python -m pytest tests/test_signup_blockers.py -q` and verify it passes.

### Task 4: Rebrand and Configure v6 Releases

**Files:**
- Modify: `VERSION`
- Modify: `core/version.py`
- Modify: `README.md`
- Modify: `CHANGELOG.md`
- Modify: `update_config.json`
- Modify: `packaging/create_release_zip.sh`
- Modify: `packaging/create_release_zip.ps1`
- Modify: `packaging/build_bridge.ps1`
- Modify: `packaging/build_updater.ps1`
- Modify: `packaging/README.md`
- Modify: `packaging/V4_RELEASE.md`
- Modify: `packaging/update_config.template.json`
- Modify: `nyx_extension/manifest.json`
- Modify: `nyx_extension/popup.html`
- Modify: `nyx_extension/options.html`
- Modify: `nyx_extension/README.md`
- Modify: `nyxify_extension/manifest.json`
- Modify: `nyxify_extension/popup.html`
- Modify: `nyxify_extension/options.html`
- Modify: `nyxify_extension/README.md`
- Modify: `agent_host/com.nyxsuite.agent.json`
- Test: `tests/test_release_packaging.py`

- [ ] Add failing release tests that assert generated ZIP `update_config.json` points at `sepulturero0/nyxsuite-v6`.
- [ ] Replace user-facing v4/v5 branding with v6 where it affects users, builders, extension popups, or release instructions.
- [ ] Set version files/manifests to `6.0.0`.
- [ ] Reset checked-in native host manifest path to `agent_host/host_main.py`.
- [ ] Run release packaging tests and targeted text scans for stale visible v5/v4 labels.

### Task 5: Document Release Procedure

**Files:**
- Create: `RELEASE.md`
- Modify: `README.md`

- [ ] Document the v6 release flow: bump `core/version.py`, run `scripts/sync_version.py`, build ZIP with shell or PowerShell, create GitHub release in `sepulturero0/nyxsuite-v6`, upload `NyxSuite-v<version>.zip`, verify dashboard update on Windows and macOS.
- [ ] Include what update assets must contain and which runtime files are preserved.

### Task 6: Verify, Commit, Create Repo, Push

**Files:**
- All intended source files and existing working tree features.

- [ ] Run focused tests: `python -m pytest tests/test_nyxify_snapboard_bridge.py tests/test_signup_blockers.py tests/test_release_packaging.py -q`.
- [ ] Run broader tests if time permits: `python -m pytest -q`.
- [ ] Create public repo `sepulturero0/nyxsuite-v6` if missing.
- [ ] Set `origin` to the v6 repo.
- [ ] Stage the full intended working tree so current v6 features are included, while respecting `.gitignore` secrets.
- [ ] Commit with a v6 release message.
- [ ] Push the default branch to GitHub.
