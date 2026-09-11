# Bridge OTP, AdsPower Rename, and Update Repair Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Find the root causes and fix three reported v6.7.2 regressions: missed SnapBoard Check Code/Check SMS actions, macOS AdsPower profile rename failures after update, and Windows extension Start Bridge failures after update or bridge restart.

**Architecture:** Treat this as three boundary failures, not one broad patch. First add evidence at each handoff: runner to local API, local API to extension/background/content script, AdsPower manager to GUI backend, and extension native host to launcher/bridge. Then implement the smallest confirmed fixes and keep platform-specific behavior covered by focused tests.

**Tech Stack:** Python async runner, SQLite task store/local HTTP APIs, Chrome Manifest V3 extension JavaScript, macOS AXUI AdsPower backend, Windows PowerShell/native messaging launcher, pytest/unittest.

---

## Root-Cause Rules

- No production fix is allowed until the task's investigation step identifies which boundary is failing.
- Each issue must produce one failing automated test or a live diagnostic log proving the broken boundary.
- If a live-only condition cannot be reproduced locally, implement diagnostics first, ship no behavioral change for that issue, and record the exact evidence still needed.

---

### Task 1: Baseline Evidence and Current Version

**Files:**
- Read: `VERSION`
- Read: `core/version.py`
- Read: `nyxify_extension/manifest.json`
- Read: `nyx_extension/manifest.json`
- Read: `RELEASE.md`
- Test: existing focused suites listed below

- [x] Record the current implementation version from `VERSION` and `core/version.py`.

Expected current value:

```text
6.7.2
```

- [x] Run baseline focused tests before changes.

Run:

```bash
python3 -m unittest tests.test_nyxify_bridge_waits tests.test_sms_to_snapchat_entry tests.test_signup_sms_recovery tests.test_adspower_open_batch tests.test_adspower_macos_backend tests.test_adspower_cdp_fallback tests.test_windows_launcher tests.test_release_updater_sync -v
```

Expected before fixes:

```text
Existing tests may pass. Passing means current coverage misses the reported regressions; it does not disprove the user reports.
```

- [x] Inspect recent release notes around verification and rename.

Known relevant code areas:

```text
nyxify_runner.py
core/signup_flow.py
core/nyxify_task_store.py
core/nyxify_local_api.py
nyxify_extension/content.js
nyxify_extension/background.js
core/adspower.py
core/adspower_ui.py
core/adspower_ui_backend_macos.py
scripts/macos_relocate.py
agent_host/host_main.py
agent_host/install_host.py
portable_launch_nyx.ps1
packaging/updater.py
```

---

### Task 2: Prove Where OTP/SMS Fetching Stops

**Files:**
- Modify: `tests/test_sms_to_snapchat_entry.py`
- Modify: `tests/test_nyxify_bridge_waits.py`
- Read: `nyxify_extension/content.js`
- Read: `nyxify_extension/background.js`
- Read: `core/nyxify_task_store.py`
- Read: `core/nyxify_local_api.py`

- [x] Add a source-level regression test proving the content script has a bounded reclick cadence while fetching verification codes.

Add to `tests/test_sms_to_snapchat_entry.py`:

```python
def test_check_code_and_sms_use_bounded_twenty_second_reclicks(self):
    content = (ROOT / "nyxify_extension" / "content.js").read_text(encoding="utf-8")

    self.assertIn("VERIFICATION_RECLICK_INTERVAL_MS = 10000", content)
    self.assertIn("VERIFICATION_RECLICK_LIMIT = 3", content)
    self.assertIn("nextAllowedClickAt", content)
    self.assertIn("successfulClicks < VERIFICATION_RECLICK_LIMIT", content)
    self.assertIn("Date.now() >= nextAllowedClickAt", content)
```

- [x] Add a bridge-store regression test proving SMS failed dispatches become eligible again after the configured retry delay, up to the code-fetch deadline.

Add to `tests/test_nyxify_bridge_waits.py` near the existing local API bridge tests:

```python
def test_sms_pending_requeues_after_failed_empty_result(self):
    from core.nyxify_local_api import _SmsFetchStore

    store = _SmsFetchStore()
    store.request("snapboard:42", phone="+15551234567")

    first = store.pop_pending()
    self.assertEqual(first["row_key"], "snapboard:42")

    store.store_result("snapboard:42", code="", error="SnapBoard SMS fetch failed.")
    second = store.pop_pending()

    self.assertEqual(second["row_key"], "snapboard:42")
    self.assertEqual(second["phone"], "+15551234567")
```

- [x] Add a task-store regression test proving email OTP requests expose dispatch diagnostics or retry eligibility, because `get_pending_otp_request()` currently returns only the oldest `PENDING` row.

Add to `tests/test_nyxify_bridge_waits.py`:

```python
def test_otp_pending_request_includes_dispatch_diagnostics(self):
    from core.nyxify_task_store import NyxifyTaskStore

    with tempfile.TemporaryDirectory() as tmp:
        store = NyxifyTaskStore(Path(tmp) / "tasks.db")
        store.upsert_task(
            "snapboard:42",
            model="Olivia",
            ip_address="1.2.3.4",
            username="readyuser",
            email="submitted@example.com",
        )
        store.request_otp_for_row("snapboard:42", email="submitted@example.com")

        pending = store.get_pending_otp_request()

    self.assertEqual(pending["row_key"], "snapboard:42")
    self.assertEqual(pending["email"], "submitted@example.com")
    self.assertIn("dispatch_count", pending)
    self.assertIn("age_seconds", pending)
```

- [x] Run the new tests and confirm at least the new diagnostics/cadence tests fail before implementation.

Run:

```bash
python3 -m unittest tests.test_sms_to_snapchat_entry tests.test_nyxify_bridge_waits -v
```

Expected:

```text
FAIL before implementation on missing bounded reclick constants and missing OTP dispatch diagnostics.
```

---

### Task 3: Implement Bounded Check Code / Check SMS Reclicks

**Files:**
- Modify: `nyxify_extension/content.js`
- Modify: `nyxify_extension/background.js` only if Task 2 proves the background bridge is the stalled layer
- Modify: `core/nyxify_task_store.py` only if Task 2 proves email OTP requests need dispatch aging
- Modify: `core/nyxify_local_api.py` only if Task 2 proves SMS dispatch aging is insufficient
- Test: `tests/test_sms_to_snapchat_entry.py`
- Test: `tests/test_nyxify_bridge_waits.py`

- [x] In `nyxify_extension/content.js`, add constants near the existing OTP constants.

```javascript
var VERIFICATION_RECLICK_INTERVAL_MS = 10000;
var VERIFICATION_RECLICK_LIMIT = 3;
```

- [x] In `clickAuthCodeUntilFound`, replace unbounded repeated click behavior with initial click plus up to two more clicks spaced by 10 seconds, while continuing to wait for code text between clicks.

Implementation shape:

```javascript
var nextAllowedClickAt = startedAt;

while (Date.now() < deadline) {
  var now = Date.now();
  var shouldClick = successfulClicks < VERIFICATION_RECLICK_LIMIT
    && now >= nextAllowedClickAt;

  if (shouldClick) {
    var clickResult = sms ? clickCheckSms(rowId) : clickCheckCode(rowId);
    var clicked = !!(clickResult && clickResult.clicked);
    lastClickState = (clickResult && clickResult.state) || lastClickState;
    clickAttempts += 1;
    if (clicked) {
      successfulClicks += 1;
      nextAllowedClickAt = Date.now() + VERIFICATION_RECLICK_INTERVAL_MS;
      observeCountdown(true);
      diagTiming(sms ? "check_sms.click" : "check_code.click", diagStart);
    }
  }

  var latestCode = await (sms ? waitForSmsCode : waitForOtpCode)(
    rowId,
    Math.min(OTP_CLICK_RETRY_INTERVAL_MS, Math.max(500, deadline - Date.now())),
    popupSnapshot,
    previousCode
  );
  if (latestCode) {
    diagTiming(sms ? "sms.code_retrieval" : "otp.code_retrieval", diagStart);
    return { ok: true, code: latestCode };
  }

  if (hasNoPendingOrderToast(sms ? "phone" : "email")) {
    return {
      ok: false,
      terminal: true,
      error: sms
        ? "No pending phone order for this account. Request a number first."
        : "No pending email order for this account. Get email first.",
    };
  }

  observeCountdown(false);
  await sleep(300);
}
```

- [x] Preserve disabled-control handling: when candidates exist but clickable count is zero, wait for code text and do not call `HTMLElement.click()`.

- [x] If Task 2 shows email OTP dispatch diagnostics are missing, add `otp_dispatch_count` and `otp_dispatched_at` columns to `core/nyxify_task_store.py`, and make `get_pending_otp_request()` return age/dispatch info without losing compatibility.

Migration shape:

```python
try:
    conn.execute("ALTER TABLE tasks ADD COLUMN otp_dispatch_count INTEGER NOT NULL DEFAULT 0")
except Exception:
    pass
try:
    conn.execute("ALTER TABLE tasks ADD COLUMN otp_dispatched_at REAL NOT NULL DEFAULT 0")
except Exception:
    pass
```

- [x] Run focused verification.

Run:

```bash
python3 -m unittest tests.test_sms_to_snapchat_entry tests.test_nyxify_bridge_waits tests.test_signup_sms_recovery -v
```

Expected:

```text
OK
```

---

### Task 3A: Preserve Proxy Priority Across Failed Profile Retries

**Files:**
- Modify: `nyxify_runner.py`
- Test: `tests/test_nyxify_continuous_mode.py` or a focused retry test module

- [x] Add a regression test proving cleanup after a failed profile creation passes the configured proxy-priority and blocked-proxy patterns into the SnapBoard rotation request.

- [x] Keep same-profile Agree and Continue retries on the current proxy; rotate only when the failed profile is deleted and requeued.

- [x] When proxy priority is enabled, make the cleanup rotation use the same priority-aware selection rules as the normal pre-create rotation, without exposing proxy credentials in logs.

- [x] Run the focused continuous-mode and proxy tests.

---

### Task 4: Prove macOS AdsPower Rename Failure Boundary

**Files:**
- Modify: `tests/test_adspower_macos_backend.py`
- Modify: `tests/test_adspower_open_batch.py`
- Read: `core/adspower.py`
- Read: `core/adspower_ui.py`
- Read: `core/adspower_ui_backend_macos.py`
- Read: `scripts/macos_relocate.py`
- Optional live tool: `tools/test_adspower_ui_profile.py`

- [x] Add a test proving the macOS backend invalidates cached app/window references when the AdsPower app process changes after update/restart.

Add to `tests/test_adspower_macos_backend.py`:

```python
def test_cached_window_is_not_reused_when_process_identifier_changes(self):
    from core.adspower_ui_backend_macos import MacOSAdsPowerBackend, Rect

    backend = MacOSAdsPowerBackend.__new__(MacOSAdsPowerBackend)
    backend._app = mock.Mock()
    backend._app.processIdentifier.return_value = 111
    backend._app_ref = object()
    backend._window = object()
    backend._app_pid = 222
    backend.attr = mock.Mock(return_value=False)
    backend.element_rect = mock.Mock(return_value=Rect(0, 0, 1200, 800))

    self.assertFalse(backend._cached_window_is_usable())
```

- [x] Add a test proving failed rename confirmation refreshes the AdsPower dashboard once before returning failure.

Add to `tests/test_adspower_open_batch.py`:

```python
def test_rename_refreshes_dashboard_before_final_failure(self):
    from core.adspower_ui import AdsPowerUIController, AdsPowerUIError

    ctrl = AdsPowerUIController.__new__(AdsPowerUIController)
    ctrl._connect = mock.Mock()
    ctrl._ensure_row_visible = mock.Mock(return_value=True)
    ctrl._recover_presearch_row_for_rename = mock.Mock(return_value=False)
    ctrl._open_rename_dialog = mock.Mock(return_value=True)
    ctrl._rect = mock.Mock(return_value=None)
    ctrl._fill_name = mock.Mock()
    ctrl._click_ok = mock.Mock()
    ctrl._rename_confirmed_or_absent = mock.Mock(return_value=False)
    ctrl._refresh_window = mock.Mock(return_value=True)

    with self.assertRaises(AdsPowerUIError):
        ctrl.rename_profile_by_id("k1target", "Snapchat: fixeduser")

    ctrl._refresh_window.assert_called()
```

- [x] Run tests and confirm they fail before implementation.

Run:

```bash
python3 -m unittest tests.test_adspower_macos_backend tests.test_adspower_open_batch -v
```

Expected:

```text
FAIL on missing process-id invalidation or missing refresh-before-final-failure behavior.
```

- [ ] If a macOS machine with AdsPower is available, run one live rename after an update before changing code.

Run:

```bash
python3 tools/test_adspower_ui_profile.py --rename k1PROFILEID:"Snapchat: livecheck"
```

Capture:

```text
Whether API or GUI mode was used, visible row name before/after rename, AdsPower frontmost app name, AX window title/size, and any Accessibility error.
```

---

### Task 5: Implement macOS Rename Recovery

**Files:**
- Modify: `core/adspower_ui_backend_macos.py`
- Modify: `core/adspower_ui.py`
- Modify: `core/adspower.py` only if Task 4 proves manager routing is wrong
- Modify: `scripts/macos_relocate.py` only if Task 4 proves the safe installed copy stays stale after update
- Test: `tests/test_adspower_macos_backend.py`
- Test: `tests/test_adspower_open_batch.py`
- Test: `tests/test_adspower_cdp_fallback.py`

- [x] In `MacOSAdsPowerBackend.connect`, store the app process identifier whenever a window is selected.

Implementation shape:

```python
self._app_pid = int(app.processIdentifier())
```

- [x] In `_cached_window_is_usable`, reject the cached window if the current app PID differs from the stored PID.

Implementation shape:

```python
try:
    current_pid = int(self._app.processIdentifier())
except Exception:
    return False
if getattr(self, "_app_pid", None) not in (None, current_pid):
    return False
```

- [x] In `AdsPowerUIController`, add a small wrapper that calls the backend dashboard refresh only when available.

Implementation shape:

```python
def _refresh_window(self) -> bool:
    backend = getattr(self, "_backend", None)
    refresh = getattr(backend, "refresh_window", None)
    if callable(refresh):
        return bool(refresh())
    return False
```

- [x] In `rename_profile_by_id`, after a failed edit-form confirmation, refresh the window, reconnect, re-ensure the row is visible, and retry the dialog once before raising.

- [x] Do not make rename failure block Nyx handoff for completed signups; keep the existing behavior where `profile_rename_failed` is surfaced but handoff can continue.

- [x] Run focused verification.

Run:

```bash
python3 -m unittest tests.test_adspower_macos_backend tests.test_adspower_open_batch tests.test_adspower_cdp_fallback tests.test_nyxify_continuous_mode -v
```

Expected:

```text
OK
```

---

### Task 6: Prove Windows Start Bridge Failure Boundary

**Files:**
- Modify: `tests/test_windows_launcher.py`
- Modify: `tests/test_release_updater_sync.py`
- Read: `agent_host/host_main.py`
- Read: `agent_host/host_main.bat`
- Read: `agent_host/install_host.py`
- Read: `portable_launch_nyx.ps1`
- Read: `packaging/updater.py`

- [x] Add a unit/source test proving the native host starts the bridge through the Windows portable launcher in source installs, instead of bypassing setup by running `bridge_app.py` directly.

Add to `tests/test_windows_launcher.py`:

```python
def test_windows_native_host_start_agent_uses_portable_launcher_for_source_installs():
    host = (ROOT / "agent_host" / "host_main.py").read_text(encoding="utf-8")

    assert "portable_launch_nyx.ps1" in host
    assert "-EntryScript" in host
    assert "bridge_app.py" in host
    assert "sys.platform == \"win32\"" in host
```

- [x] Add a test proving the updater refreshes native messaging registration after swapping files on Windows.

Add to `tests/test_release_updater_sync.py`:

```python
def test_updater_refreshes_native_host_registration_after_update():
    updater = (ROOT / "packaging" / "updater.py").read_text(encoding="utf-8")

    assert "install_host" in updater
    assert "register()" in updater
    assert "native messaging" in updater.lower()
```

- [x] Run the new tests and confirm they fail before implementation.

Run:

```bash
python3 -m unittest tests.test_windows_launcher tests.test_release_updater_sync -v
```

Expected:

```text
FAIL before implementation on direct bridge launch and/or missing post-update native host refresh.
```

- [ ] On an affected Windows machine, collect non-secret diagnostics from these locations before changing code.

Commands:

```powershell
Get-ItemProperty 'HKCU:\Software\Google\Chrome\NativeMessagingHosts\com.nyxsuite.agent'
Get-Content "$env:LOCALAPPDATA\NyxSuite\bootstrap\portable_launch.log" -Tail 80
Test-NetConnection 127.0.0.1 -Port 8870
Test-NetConnection 127.0.0.1 -Port 8869
```

Expected diagnostic result:

```text
Registry default value should point to the current install's agent_host\com.nyxsuite.agent.json.
If it points to an old/deleted release folder, Start Bridge cannot work until registration is refreshed.
If ports are down and the log shows import/dependency failures, the native host must use the launcher path.
```

---

### Task 7: Implement Windows Start Bridge and Update Repair

**Files:**
- Modify: `agent_host/host_main.py`
- Modify: `packaging/updater.py`
- Modify: `portable_launch_nyx.ps1` only if Task 6 proves setup state is stale after dependency changes
- Test: `tests/test_windows_launcher.py`
- Test: `tests/test_release_updater_sync.py`
- Test: `tests/test_bridge_duplicate_open.py`

- [x] In `agent_host/host_main.py`, route Windows source-install `start_agent` through `portable_launch_nyx.ps1` so setup/repair can run when the bridge is started from the extension.

Implementation shape:

```python
if sys.platform == "win32" and not getattr(sys, "frozen", False):
    powershell = os.environ.get("SystemRoot", "")
    powershell_exe = str(Path(powershell) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe") if powershell else "powershell.exe"
    cmd = [
        powershell_exe,
        "-NoLogo",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(root / "portable_launch_nyx.ps1"),
        "-EntryScript",
        "bridge_app.py",
        "-Quiet",
    ]
```

- [x] Keep frozen behavior unchanged: frozen native host should still find and launch the bundled `bridge_app.exe`.

- [x] In `packaging/updater.py`, after file swap/version write and before relaunch, call native host registration best-effort from the updated install root.

Implementation shape:

```python
def _refresh_native_host_registration(install_root: Path) -> None:
    try:
        sys.path.insert(0, str(install_root))
        from agent_host.install_host import register

        ok = register()
        print(f"[updater] native messaging registration refreshed={bool(ok)}")
    except Exception as exc:
        print(f"[updater] native messaging registration refresh skipped: {exc}")
```

- [x] Call `_refresh_native_host_registration(install_root)` after `_write_version(install_root, staged_version)`.

- [x] Run focused verification.

Run:

```bash
python3 -m unittest tests.test_windows_launcher tests.test_release_updater_sync tests.test_bridge_duplicate_open -v
```

Expected:

```text
OK
```

---

### Task 7A: Keep the SnapBoard Bridge Alive Without Manual Page or Popup Opening

**Files:**
- Modify: `nyxify_extension/background.js`
- Modify: `nyxify_extension/content.js` only if reconnect signaling requires a content-side change
- Test: focused extension source/behavior tests

- [x] Add a regression test proving bridge actions do not silently stop just because `snapboardPorts` is empty while a SnapBoard tab exists.

- [x] Add a background keepalive/recovery path for alarms, startup, and pending work: find an existing SnapBoard tab, ping its content script, and reload it once when the port/content script is stale.

- [x] Make `sendMessageToSnapboardTab()` fall back to a discovered SnapBoard tab when no connected port is registered, while retaining a clear `Waiting for SnapBoard tab` diagnostic when no tab is available.

- [x] Keep Full Auto and other DOM-dependent actions queued/retryable until the SnapBoard content script reconnects; opening the extension popup must not be required to wake the workflow.

- [x] Do not open an unexpected visible tab unless the existing product flow already permits it; prefer an existing SnapBoard tab and report the waiting state when none exists.

- [x] Run the extension-focused regression tests and inspect the actual Chrome/SnapBoard tab when available.

---

### Task 8: End-to-End Verification and Release Notes

**Files:**
- Modify: `RELEASE.md`
- Optional modify: `CHANGELOG.md`
- Test: focused suites from Tasks 3, 5, and 7

- [x] Run all focused suites together.

Run:

```bash
python3 -m unittest tests.test_nyxify_bridge_waits tests.test_sms_to_snapchat_entry tests.test_signup_sms_recovery tests.test_adspower_open_batch tests.test_adspower_macos_backend tests.test_adspower_cdp_fallback tests.test_nyxify_continuous_mode tests.test_windows_launcher tests.test_release_updater_sync tests.test_bridge_duplicate_open -v
```

Expected:

```text
OK
```

- [x] Add a concise `RELEASE.md` entry under the current unreleased/top section.

Entry shape:

```markdown
- SnapBoard Check Code / Check SMS fetching now uses bounded 10-second reclicks while waiting for OTP/SMS results, with diagnostics that show click attempts and row-control state.
- macOS AdsPower GUI rename now invalidates stale AX window references after AdsPower restarts and refreshes the dashboard once before declaring a rename failure.
- Windows native Start Bridge now routes through the portable launcher repair path, and updates refresh native messaging registration so the extension does not fall back to setup after bridge restart.
- Failed profile retries preserve proxy-priority selection, and SnapBoard bridge actions recover when the page or extension popup has not been opened manually.
```

- [ ] If live machines are available, run platform checks.

macOS:

```bash
python3 tools/test_adspower_ui_profile.py --rename k1PROFILEID:"Snapchat: livecheck"
```

Windows:

```powershell
.\portable_launch_nyx.ps1 -EntryScript bridge_app.py -SetupOnly
```

Then stop the bridge, open the extension setup page, click `Start the bridge`, and confirm `http://127.0.0.1:8870/` responds.

- [ ] Do not store logs, tokens, account data, OTP codes, emails, or phone numbers in Agent Memory. Store only stable verified facts if the user approves a memory update after the work is complete.
