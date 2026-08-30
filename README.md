# Nyx Suite v6

Current release line (6.0.1) of the **no-API** Nyx Suite — it drives the AdsPower desktop
app directly when the Local API is permission-gated, so it works on AdsPower
Employee/sub-accounts. This is an **open build with no license/activation**.

Automation suite with a local **bridge** (system-tray agent + web dashboard) that
controls two runners:

- **Nyx** — Bitmoji creation runner (`main.py`)
- **Nyxify** — AdsPower profile-creation runner (`nyxify_runner.py`)

The bridge (`bridge_app.py`) serves a real-time dashboard at
`http://127.0.0.1:8870` and supervises the per-product local APIs. Browser
automation runs through AdsPower + Playwright; the MV3 extensions assist on-page.
When AdsPower's Local API is gated, profile create/open/rename/close/delete fall
back to GUI automation (Windows) — see **No-API mode** below.

## What is in this repo

- `bridge_app.py` — tray agent + dashboard host (the app you launch)
- `main.py` — Nyx queue runner (spawned by the bridge)
- `nyxify_runner.py` — Nyxify queue runner (spawned by the bridge)
- `core/` — shared automation, AdsPower (API + no-API GUI), queue, runtime config, updater
- `webui/` — dashboard single-page app served by the bridge
- `agent_host/` — native-messaging host linking the browser extensions to the agent
- `nyx_extension/`, `nyxify_extension/` — MV3 browser extension sources
- `snap_selectors/` — model selector definitions
- `data/signup_names/`, `data/full_auto_usernames/` — committed lists the flows require
- `packaging/` — Windows and source-ZIP release scripts

## Run locally (Windows, macOS, Linux)

First run creates the virtualenv, installs dependencies, and downloads the
Playwright Chromium runtime automatically. All launchers default to starting
`bridge_app.py`; the dashboard opens in your browser.

**Windows** — double-click `run_nyx_suite.bat`, or:

```
powershell -ExecutionPolicy Bypass -File .\portable_launch_nyx.ps1
```

**macOS** — double-click `run_nyx_suite.command` (first time: right-click → **Open**
to clear Gatekeeper), or:

```
bash ./portable_launch_nyx.sh
```

> **macOS note:** if the folder is in Documents/Desktop/Downloads/iCloud, the
> launcher auto-installs the app to `~/Library/Application Support/NyxSuite/app`
> and runs from there. macOS (TCC) blocks browsers from launching the
> native-messaging host out of those protected folders, which otherwise makes
> the extension's **Connect** fail with *"Native host has exited."* Runs on the
> Python bundled with macOS (3.9+) — no Homebrew Python required.

**Linux**:

```
bash ./run_nyx_suite.sh
```

## Environment

Copy `.env.example` to `.env` and fill in your AdsPower values:

- `ADSP_HOST`
- `ADSP_PORT`
- `ADSP_API_KEY`

## No-API mode (AdsPower GUI automation)

On AdsPower **Employee/sub-accounts** the Local API is permission-gated:
`/browser/start` and profile create return `9110 No local API permission`. When
that happens the suite automatically drives the **AdsPower desktop app** instead
of the API — no API key or admin permission required:

- **Create** a profile through the GUI: New Profile → name → group → paste the
  proxy into the Host field (AdsPower auto-parses `host:port:user:pass`) → Check
  Proxy → OK. (`core/adspower_ui.py`)
- **Find** the new profile's id from the Profiles list, dup-safe under many
  concurrent profile creators (serial watermark + `Name contains` filter).
- **Open** it via the search bar (clear search → `Profile ID is <id>` → row
  **Open**), then attach Playwright over CDP (`core/adspower_cdp.py`). The
  Bitmoji/signup flow then runs unchanged over the CDP endpoint. When more than
  one profile is opened (or closed) at once, they are **coalesced into one bulk
  search** (`Profile ID is <id1> <id2> …`, space-separated) and each row's
  **Open**/**Close** is clicked from that single result — one search instead of
  one per profile (`core/adspower_ui.py` `_GuiBatcher` / `_search_by_ids`).
- **Rename** after a successful signup (Name edit-pencil → type → OK), the same
  step the API path did via `/user/update`.
- **Close** when a run finishes (the row's **Close** button) so browser windows
  don't pile up — both the Bitmoji runner and Nyxify completion close their
  profile. Concurrent closes share one bulk search the same way opens do.
- **Delete + re-create** on the signup retry path: a failed account's profile is
  closed, deleted (select row → trash → confirm), and the proxy is rotated via
  SnapBoard (unchanged) before the row retries.

The proxy pre-check also degrades gracefully: when AdsPower's proxy-check API is
permission-gated it falls through to a socket reachability test (AdsPower's own
"Check Proxy" still validates for real during GUI create), so the rotation loop
never stalls. Every GUI operation is serialized by a **cross-process** lock (a
Windows named mutex) — Nyx and Nyxify run as separate processes but share one real
mouse, so their GUI touchpoints run one at a time. The Playwright work after an
open still runs in parallel, and an open only holds the lock for the click (not the
browser-launch wait). For best throughput, run Nyx and Nyxify at different times;
running them together is safe but their GUI steps will queue behind each other.
Tags and extension-category are not set on GUI-created profiles — they are
organizational metadata only and neither runner depends on them.

This path is **resolution-, DPI- and window-position-independent**: it locates
controls by platform accessibility APIs (Windows UI Automation on Windows,
AXUIElement on macOS) and clicks their real on-screen rectangles
(`core/win_focus.py` forces the AdsPower window foreground first on Windows;
the macOS backend raises the AdsPower Global window through AppKit). A cross-
platform OpenCV template-matching fallback (`core/ui_vision.py`) covers the rare
case accessibility can't see a control; templates auto-capture into
`ui_templates/adspower/elements/`.

Controls:

- Dashboard mode selector: Settings -> AdsPower Control -> `Auto`, `API`, or `GUI`.
  `Auto` keeps the smart API-first fallback behavior, `API` uses the Local API
  only, and `GUI` drives the AdsPower desktop app first for no-API devices.
- `ADSPOWER_UI_FALLBACK=0` (or `adspower_ui_fallback=false` in settings) disables it.
- Smoke test: `python tools/test_adspower_ui_profile.py`
  (create + find + open + Playwright attach, fully no-API).
- Full lifecycle test: `python tools/test_adspower_ui_profile.py --lifecycle`
  (create → open → rename → close → delete). Single ops: `--rename ID:Name`,
  `--close ID`, `--delete ID`.

## Keyboard shortcuts — stop / start

Each product has its own dedicated global hotkey (they work even while the
AdsPower window is focused):

- **Ctrl+F8** — stop/start **Nyx** (Bitmoji runner)
- **Ctrl+F7** — stop/start **Nyxify** (profile-creation runner)

A key always controls its own product: if that runner is active it performs the
same **full Stop** as the dashboard Stop button (the whole runner process tree
is killed, force-killed if it survives); if it is stopped, the key starts it. A
distinct built-in tone plays per action (low descending double-beep = stopped,
higher rising double-beep = started) so you know the key was caught.

The listener runs in the **bridge** process (`core/hotkeys.py`, started by
`bridge_app.py`), which is what lets a hotkey also *start* a runner that has no
process yet.

> **macOS:** global hotkeys require the host app (Terminal, or the bundled Nyx
> Suite app) to have **Accessibility** permission — grant it under *System
> Settings → Privacy & Security → Accessibility*. Without it the suite still
> runs; only the Ctrl+F7/F8 shortcuts are inactive (the dashboard/tray controls
> work regardless).

## Browser extension host (optional)

To let the extensions talk to the agent, register the native-messaging host:

```
python agent_host/install_host.py --register
```

This writes the host manifest to the correct per-OS location (Windows registry /
macOS `~/Library/Application Support/.../NativeMessagingHosts` / Linux `~/.config/...`).

## Notes

- AdsPower must be installed and running on the target machine.
- **No license/activation** — this open build runs unconditionally.
- **Cross-OS:** the suite installs and runs on Windows, macOS and Linux (Local API
  + Playwright). The **no-API GUI fallback works on Windows and macOS** (Windows
  UI Automation on Windows, AXUIElement Accessibility on macOS); Linux continues
  to use the AdsPower Local API path.
- **Run on login** is cross-platform (Settings → Start on Login): Windows `Run`
  key, macOS LaunchAgent, Linux XDG autostart.
- `data/signup_names/` and `data/full_auto_usernames/` are intentionally versioned —
  the signup and Full Auto flows depend on them.
- Runtime databases, config, and logs are created locally and kept out of Git via `.gitignore`.

## Release updates

Nyx Suite v6 uses one public GitHub repo for source and update releases:
`sepulturero0/nyxsuite-v6`. The dashboard updater checks that repo's GitHub
Releases for an asset matching `NyxSuite-v*.zip`.

Release checklist for future agents:

1. Update `core/version.py`.
2. Run `python scripts/sync_version.py` so both extension manifests match.
3. Build a source release ZIP:
   - macOS/Linux: `bash packaging/create_release_zip.sh --version <version>`
   - Windows: `powershell -ExecutionPolicy Bypass -File .\packaging\create_release_zip.ps1 -Version <version>`
4. Create/publish a GitHub release in `sepulturero0/nyxsuite-v6` with tag `v<version>`.
5. Upload `dist/NyxSuite-v<version>.zip` to that release.
6. From an older install on Windows and macOS, open Dashboard -> Settings -> Check for Update -> Apply Update.
