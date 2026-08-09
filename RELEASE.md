# Nyx Suite v6 Release Guide

Nyx Suite v6 uses a single public GitHub repository for source and update
assets: `jaymaroldan026/nyxsuite-v6`.

The dashboard updater reads `update_config.json`, calls GitHub Releases for that
repo, and downloads the newest non-draft release asset matching
`NyxSuite-v*.zip`.

## Latest Release Notes

### NyxSuite v6.5.6

- Nyxify now offers Scan Banned Rows, Remove Banned, and Warm Up Banned
  controls in both the extension popup and the web dashboard, backed by new
  local API endpoints (`/replace_banned/scan`, `/replace_banned/remove`,
  `/replace_banned/warmup`).
- Remove Banned clears a banned row's SnapBoard AdsPower ID, forces a proxy
  rotation (the rotation request now carries an explicit `force` flag so the
  proxy actually changes), and removes the row from the local store. Warm Up
  Banned pushes the SnapBoard status to "Warm Up" first. Both actions continue
  per row instead of aborting the whole batch when a single row fails.
- The SnapBoard OTP/SMS fetch window is now one minute (was 30 seconds), so
  SnapBoard has more time to detect a code before the fresh email/number
  recovery path triggers.
- When verification fetches an OTP, Nyxify now always clicks the SnapBoard
  Check Code / Check SMS button and waits for a fresh code instead of trusting
  a stale row value, and fails fast with an explicit "button not found" error
  when the check button cannot be clicked.

### NyxSuite v6.5.5

- Restored Nyxify wrong-code recovery for email and SMS verification: when
  Snapchat shows "That's not the right code!", the verification phase now backs
  up to the email/phone entry card, gets a fresh email or number, and submits a
  replacement OTP on the same account.
- The recovery helper now checks whether the email/phone textbox is already
  visible before clicking Back, avoiding repeated back clicks that can return to
  the full signup form.
- The change is scoped to the post-"Agree and Continue" verification phase.

### NyxSuite v6.5.4

- Fixed Nyxify phone verification after the `v6.5.3` rollback: after entering a
  phone number, the runner now sends that same phone value with the SnapBoard
  SMS bridge request, so the extension can click Check SMS instead of rejecting
  the request and rotating numbers.
- The signup verification phase remains on the restored `v6.2.0` flow; this
  change only restores the required phone context at the runner-to-SnapBoard
  bridge boundary.

### NyxSuite v6.5.3

- Nyxify verification after a successful "Agree and Continue" handoff has been
  restored to the stable `v6.2.0` behavior.
- Email verification again uses the `v6.2.0` four-attempt email order budget,
  while phone verification uses the `v6.2.0` two-number SMS path.
- OTP and SMS provider calls use the older no-argument contract, matching the
  stable verification phase and avoiding the newer SMS bridge pickup behavior
  observed in the stuck run.

### NyxSuite v6.5.0

- Nyxify phone/SMS verification now accepts already-entered or formatted phone
  values before clicking the SMS submit button.
- The phone verification step now dispatches input/change/blur events after the
  phone value is present and can click visible Continue/Next/Send/SMS submit
  variants when Snapchat does not expose a normal submit button.
- The Nyxify popup now shows yellow AM/G5 and SP/TV segmented provider-lock
  controls, backed by the existing `lockG5` and `lockTV` SnapBoard lock keys.
- Added regression coverage for formatted phone fields, alternate phone-step
  submit buttons, and the segmented provider-lock popup contract.

### NyxSuite v6.4.3

- SnapBoard AdsPower-name sync no longer writes the full
  `Snapchat: <username>` AdsPower profile name into the visible SnapBoard
  username/name field.
- Nyxify now targets only explicit AdsPower-name SnapBoard fields when syncing
  the profile name, avoiding generic `name` fields on SnapBoard layouts.
- Added regression coverage to prevent AdsPower-name sync from touching
  SnapBoard username/name fields.
- Expanded the release guide with hard-stop rules, exact version checks, and a
  committed-`HEAD` ZIP build flow to prevent dirty or wrong-version releases.

### NyxSuite v6.4.2

- Nyxify now releases its Playwright/CDP browser connection before the final
  Continuous Mode AdsPower profile rename, reducing rename misses when the
  freshly finished account is handed to Nyx immediately.
- SnapBoard AdsPower-name sync now sends the full profile name
  `Snapchat: <username>` instead of only the bare Snapchat username.
- Signup username retry now stops typing if Snapchat has already moved to
  email, phone, OTP, or welcome verification.
- Added regression coverage for final profile rename ordering, SnapBoard
  AdsPower-name payloads, and stale signup retry transitions.

### NyxSuite v6.4.1

- Fixed a Nyxify delay on Snapchat Step 2 where the page already showed
  "Use Email Instead" but the runner could still be retrying the old Step 1
  "Agree and Continue" path.
- Nyxify now rechecks the live handoff state before retrying after a stale
  unable-to-process signal, so phone/email verification controls take priority.
- Fast signup-submit retries now use bounded enabled/click waits instead of
  falling into Playwright's long default click timeout.
- Added regression tests for the stale handoff retry and bounded click waits.

### NyxSuite v6.4.0

- Nyx and Nyxify extension popups no longer render their queue tables or selected-row queue actions, reducing popup work while keeping the runner controls and status counters available.
- Dashboard queue views, backend queues, and local queue APIs remain unchanged; queue inspection and row-level actions stay in the dashboard.
- Nyxify now has its own Setup & Install button and bundled setup helper, matching the Nyx popup setup flow.

### NyxSuite v6.3.10

- Nyxify now detects local SnapBoard bridge pickup failures quickly for email,
  phone, and SMS requests, avoiding long per-attempt waits on affected devices.
- Local API SnapBoard requests refresh stale bridge tokens after HTTP 401 and
  retry once, reducing device-specific token/session drift.
- Signup progress now treats visible email, phone, OTP, and welcome handoff
  states as authoritative over stale username retry UI, so rows stop reporting
  `retrying_signup_username` once verification has started.
- "Use email instead" is clicked once per signup wait and then Nyxify waits for
  the verification controls.
- Added regression tests for bridge timing, stale-token recovery, pending
  SnapBoard status metadata, and stale username retry handoff.

### NyxSuite v6.3.9

- Continuous Mode now prunes stale browser tabs after Nyx attaches and before
  opening Bitmoji, preserving one AdsPower start tab.
- Nyxify-continuous handoffs close leftover Snapchat signup, 403, Bitmoji
  login, and Bitmoji create tabs so Nyx starts from a single fresh Bitmoji
  flow.
- Manual and non-continuous Nyx runs keep the existing less-aggressive tab
  behavior.
- Added regression coverage for the continuous-only pre-Bitmoji tab cleanup.

### NyxSuite v6.3.5

- Continuous Mode now releases Nyxify's Playwright/CDP browser connection before
  queueing the profile into Nyx, so Nyx attaches with its own fresh controller.
- Nyxify no longer closes the completed Snapchat signup tab before handoff; the
  AdsPower browser stays open and Nyx takes over through the normal profile
  attach path.
- Added regression coverage for the handoff contract: signup tab remains open,
  Nyxify is disconnected, and the profile is still queued to Nyx.

### NyxSuite v6.3.4

- Continuous Mode now closes the Snapchat signup tab before handing the profile
  to Nyx, so the Bitmoji run starts from a cleaner browser tab set.
- The Automation Speed setting now applies only while Nyx is actively editing
  the Bitmoji avatar, leaving OAuth/login/navigation waits on their fixed
  reliability timings.
- Added regression coverage for continuous handoff tab cleanup and editor-only
  speed scaling.

### NyxSuite v6.3.3

- Added a Settings toggle for making the macOS menu-bar tray icon transparent.
- Transparent mode keeps the same clickable tray target and menu actions while
  hiding the visible status dot.
- The bridge saves the preference locally and preserves it across updates.

### NyxSuite v6.3.2

- Fixed a Continuous Mode stall where AdsPower's CDP endpoint was visible but
  Playwright could not complete `connect_over_cdp`, leaving the Nyx row parked
  at `running_bitmoji_flow`.
- Nyx now times out CDP attach faster, records `recovering_cdp_attach`, closes
  the AdsPower profile, and retries the Bitmoji run from a fresh browser
  session.
- Runner startup now requeues orphaned `RUNNING` Nyx rows so restarting Nyx can
  recover active continuous handoffs instead of leaving them stuck.

### NyxSuite v6.3.1

- Fixed a Continuous Mode queue stall where a new Nyxify handoff could remain
  pending because an older normal Nyx row was parked at `need_login` and held
  the only Nyx slot.
- High-priority `nyxify_continuous` tasks can now temporarily borrow an extra
  slot from non-continuous manual-login waits, so the immediate Bitmoji run
  starts without waiting for manual login cleanup.
- Active editor/Bitmoji work is not interrupted; the temporary slot only opens
  for the non-continuous `need_login` blocker case.

### NyxSuite v6.3.0

- Tightened OAuth Continue detection so regular Snapchat welcome/account pages
  do not count as OAuth consent buttons.
- This prevents the OAuth-cleared check from sticking after the Continue click
  when a welcome tab remains open.
- Includes the v6.2.9 Continuous Mode OAuth tab priority and AdsPower GUI rename
  recovery, with a reminder to restart the bridge/runners after updating.

### NyxSuite v6.2.9

- Continuous Mode now prioritizes an active Snapchat `Continue to Bitmoji?`
  OAuth consent tab over older login tabs, so immediate Nyxify handoffs proceed
  into Bitmoji automation instead of parking at `need_login`.
- Nyx scans every CDP browser context for OAuth/editor progress states before
  falling back to Snapchat login detection.
- AdsPower GUI rename now retries by reapplying Nyxify's remembered temp-name
  filter when the profile row is not visible under the current AdsPower search.

### NyxSuite v6.2.8

- Fixed the Continuous Mode Bitmoji auth stall shown as `need_login` while the
  browser is already on Snapchat's `Continue to Bitmoji?` OAuth consent page.
- Snapchat OAuth consent pages now take priority over login-page detection, even
  when the page contains hidden username/password fields.
- Nyx now routes that screen straight into the OAuth Continue click path so the
  Bitmoji editor automation can proceed without manual intervention.

### NyxSuite v6.2.7

- Continuous Mode Nyx handoff no longer gets stuck at `need_login` when the
  Snapchat login page advances into Bitmoji OAuth before the login form appears.
- Nyx now recognizes OAuth/Bitmoji handoff states while waiting for Snapchat
  auto-login fields, so it proceeds to Continue/Gender/Editor instead of
  falling into manual-login wait.
- `need_login` is now recorded only after automatic Snapchat login has actually
  failed and Nyx is entering manual-login wait.

### NyxSuite v6.2.6

- Continuous Mode now runs as a one-account pipeline: Nyxify creates the
  Snapchat account, renames the AdsPower profile, hands it to Nyx immediately,
  then waits for that continuous Nyx work before starting the next signup.
- Nyx queue handoff now uses a high-priority `run_now` path so continuous
  Bitmoji tasks are selected ahead of normal pending rows as soon as a Nyx slot
  is available.
- Nyxify now shows `waiting_for_continuous_nyx` while a continuous Nyx handoff
  is pending or running, reducing accidental overlap without blocking manual
  Stop/Restart controls.

### NyxSuite v6.2.5

- AdsPower profile close now first targets the profile's own Chromium CDP
  endpoint and closes every tab, avoiding another AdsPower GUI search when the
  browser can close itself.
- The CDP close path bypasses page "Leave site?" prompts by closing tabs without
  running before-unload handlers.
- Existing AdsPower API and GUI close paths remain as fallbacks when a profile
  does not have a live CDP endpoint.

### NyxSuite v6.2.4

- Windows launcher now starts from its own install folder, preserves failures,
  and pauses when setup fails so the operator can read the error.
- Windows PowerShell launch now quotes the bridge entry-script path, fixing
  installs whose folder path contains spaces.
- The source updater now skips empty staged source directories, preventing an
  empty release folder from wiping installed bridge/native-host files.
- Proxy Ranking's "Ban all red proxies" now refreshes live ranking rows before
  posting the ban, and explicit ban actions turn Proxy Blocker enforcement on.

### NyxSuite v6.2.3

- Nyxify now detects Snapchat wrong-code verification errors and recovers by
  going back, requesting a fresh SnapBoard email or phone number, and retrying
  the new code on the same signup.
- The SnapBoard banned-row scan controls no longer show the initial
  "Scan SnapBoard for banned rows." helper text in the dashboard or popup.
- Proxy Ranking now surfaces the worst subnets first, adds Good/Watch/Red
  summary chips, highlights red rows, and adds a bulk "Ban all red proxies"
  action.

### NyxSuite v6.2.2

- Nyxify signup recovery now refreshes faster: no-captcha/signup stalls after
  100 seconds and hard stuck signup pages after 200 seconds.
- Nyxify now refreshes and re-enters signup details when the expected signup
  form or verification handoff page is not detected for the stall window.
- Added regression coverage for the new signup refresh timings and missing-page
  recovery path.

### NyxSuite v6.2.1

- AdsPower GUI recovery now hard-refreshes the desktop app when the dashboard
  becomes unresponsive: Shift-Command-R on macOS and Control-Shift-R on Windows,
  after foregrounding AdsPower.
- Nyxify signup refresh/retry coverage now explicitly protects the filled-form
  no-captcha stall path, along with the existing reCAPTCHA unreachable and
  blank-form refill recovery paths.
- Nyx now keeps a private copy of the SnapBoard Snapchat username/password on
  the Nyx queue row, so Bitmoji auto-login still uses the real account password
  even after the Nyxify row is pruned, replaced, or no longer matches.
- Nyx and Nyxify handoff paths now carry SnapBoard credentials through the
  extension, local API, direct fallback queue write, and continuous-mode handoff.

### NyxSuite v6.2.0

- Dashboard command areas now use consistent zones for Nyx and Nyxify: runner
  controls, product tools, queue actions, selected-row actions, search, and
  Nyxify-only banned-row utilities.
- Queue buttons and row buttons keep the same placement across both dashboard
  tabs, with responsive wrapping that avoids horizontal overflow.
- Nyxify extension popup now places Start/Stop and Pause/Resume at the top of
  the runner card, above the counters, with a compact runner-state pill.
- Pause starts disabled and only becomes available when the Nyxify runner is
  active.
- Nyx and Nyxify popup headers are smaller to free up control space.
- Nyxify popup hides Push AdsPower ID and Apply AdsPower tags, moves Auto-Fill
  Row/target to the top of the toggle panel, and keeps the hidden settings wired
  through backend/options config.
- Both extension popups now auto-save dashboard settings as fields are typed or
  changed, so the manual Save Dashboard Settings button is gone.

## Create a New Release

Follow this checklist exactly. It is written to prevent dirty, stale, or
wrong-version release assets even when the release is done by a basic agent.

### Hard Stop Rules

- Do not build a release ZIP directly from the live working tree.
- Do not use `git add -A` when unrelated files are dirty.
- Do not publish a ZIP built before the final release commit.
- Do not publish when `VERSION`, `core/version.py`, both extension manifests,
  the Git tag, and the ZIP filename disagree.
- Do not reuse an existing version unless the task explicitly says to rebuild
  that same version.
- Do not upload a release asset until GitHub `master` and the release tag point
  to the intended commit.

### 1. Confirm Release Scope

Check the worktree and decide exactly which files belong in the release:

```bash
git status -sb
git diff --name-only
```

If unrelated files are listed, leave them alone. Do not commit yet; first finish
the version and test gates below.

When it is time to commit in step 4, stage only the intended files explicitly.
Never use `git add -A` unless the full worktree is intentionally part of the
release.

### 2. Set and Sync the Version

For a new version, update `core/version.py` first. Then run:

```bash
python scripts/sync_version.py
```

Manually update root `VERSION` if needed. Confirm all code-side declarations
match:

```bash
VERSION_EXPECTED=<version>
test "$(tr -d '\r\n' < VERSION)" = "$VERSION_EXPECTED"
grep -q "NYX_VERSION = \"$VERSION_EXPECTED\"" core/version.py
grep -q "NYXIFY_VERSION = \"$VERSION_EXPECTED\"" core/version.py
grep -q "\"version\": \"$VERSION_EXPECTED\"" nyx_extension/manifest.json
grep -q "\"version\": \"$VERSION_EXPECTED\"" nyxify_extension/manifest.json
```

### 3. Run Verification Before Committing

Run at least the focused release checks plus the full test suite:

```bash
.venv/bin/python -m pytest tests/test_nyxify_continuous_mode.py tests/test_signup_blockers.py tests/test_release_packaging.py tests/test_release_updater_sync.py -q
.venv/bin/python -m pytest tests -q
```

If `.venv/bin/python` is unavailable, use the project Python that has
dependencies installed. Do not claim the release is validated from a Python that
cannot collect the suite.

### 4. Commit and Push the Exact Release Commit

Stage only the intended release files, commit them, push the branch, and verify
that GitHub `master` matches local `HEAD`:

```bash
git add <file-1> <file-2> <file-3>
git diff --cached --stat
git diff --cached --check
git commit -m "<short release/fix message>"
git push origin master
LOCAL_HEAD="$(git rev-parse HEAD)"
REMOTE_MASTER="$(git ls-remote origin refs/heads/master | awk '{print $1}')"
test "$LOCAL_HEAD" = "$REMOTE_MASTER"
```

After committing, `git status -sb` may still show unrelated local files. That is
allowed only because the release ZIP is built from the committed `HEAD` archive
in step 5, not from the live working tree.

### 5. Build the ZIP From Committed `HEAD`, Not the Dirty Tree

Always build from `git archive HEAD`. This prevents local databases, edited
username lists, machine-specific manifests, or half-finished files from leaking
into the release ZIP.

macOS/Linux:

```bash
VERSION_EXPECTED=<version>
BUILD_ROOT="$(mktemp -d /tmp/nyxsuite-release-build.XXXXXX)"
git archive HEAD | tar -x -C "$BUILD_ROOT"
bash "$BUILD_ROOT/packaging/create_release_zip.sh" \
  --version "$VERSION_EXPECTED" \
  --output-dir "$PWD/dist"
```

Windows PowerShell:

```powershell
$VersionExpected = "<version>"
$BuildRoot = Join-Path $env:TEMP ("nyxsuite-release-build-" + [guid]::NewGuid())
New-Item -ItemType Directory -Force -Path $BuildRoot | Out-Null
git archive HEAD | tar -x -C $BuildRoot
powershell -ExecutionPolicy Bypass -File "$BuildRoot\packaging\create_release_zip.ps1" `
  -Version $VersionExpected `
  -OutputDir "$PWD\dist"
```

### 6. Verify the ZIP Before Uploading

Confirm structure, version, manifest safety, and checksum:

```bash
ZIP="dist/NyxSuite-v<version>.zip"
unzip -l "$ZIP" | awk 'NR > 3 {print $4}' | awk -F/ 'NF && $1 != "" {print $1}' | sort -u
unzip -p "$ZIP" "NyxSuite-v<version>/VERSION"
unzip -p "$ZIP" "NyxSuite-v<version>/agent_host/com.nyxsuite.agent.json" | grep -q '"path": "agent_host/host_main.py"'
shasum -a 256 "$ZIP"
```

The top-level-folder command must print exactly:

```text
NyxSuite-v<version>
```

If the ZIP contains `.env`, `*.db`, logs, local update backups, `.obsidian`,
license/signing secrets, or a machine-specific native-host path, stop and fix
the packaging before uploading.

### 7. Create or Replace the GitHub Release

For a new version:

```bash
git tag v<version> HEAD
git push origin refs/tags/v<version>
gh release create v<version> dist/NyxSuite-v<version>.zip \
  --repo jaymaroldan026/nyxsuite-v6 \
  --title "NyxSuite v<version>" \
  --notes "Describe the user-facing changes."
```

For an explicitly authorized same-version rebuild:

```bash
git tag -f v<version> HEAD
git push origin refs/tags/v<version> --force
gh release upload v<version> dist/NyxSuite-v<version>.zip \
  --repo jaymaroldan026/nyxsuite-v6 \
  --clobber
```

### 8. Verify GitHub After Upload

Check that remote `master`, the tag, and the release asset all match the
intended release:

```bash
git ls-remote origin refs/heads/master
git ls-remote --tags origin "v<version>"
gh release view v<version> --repo jaymaroldan026/nyxsuite-v6 \
  --json tagName,targetCommitish,assets,url
```

The release asset digest from GitHub must match the local `shasum -a 256`
checksum. If it does not match, upload the correct ZIP with `--clobber` before
announcing the release.

### 9. Verify Update From an Older Install

- Windows: Dashboard -> Settings -> Check for Update -> Apply Update.
- macOS: run `run_nyx_suite.command`, then Dashboard -> Settings -> Check for Update -> Apply Update.
- Confirm the installed `VERSION` file, dashboard version text, native messaging,
  preserved runtime data, and bridge restart behavior.

## Update Package Rules

- Keep `update_config.json` pointed at `jaymaroldan026/nyxsuite-v6`.
- Keep `asset_pattern` as `NyxSuite-v*.zip`.
- Do not ship runtime databases, local `.env`, logs, local update backups, or license/signing secrets.
- The release ZIP preserves runtime DB/config/log paths during update.
- The native-messaging manifest in the ZIP must use `agent_host/host_main.py`, not a machine-specific absolute path.
- Build release ZIPs from committed `HEAD` using `git archive`, never from the
  live dirty working tree.

## SnapBoard Password Behavior

Nyxify reads the SnapBoard Password column from each row and uses it when filling
the Snapchat signup form. If the row password is blank, signup falls back to the
legacy default password `ABC123wgmi*`.
