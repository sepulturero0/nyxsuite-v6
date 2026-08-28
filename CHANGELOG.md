# Changelog

## 6.6.1 - Runner and update control fixes

### Dashboard controls
- Fixed Nyx and Nyxify Start/Stop actions using malformed local API URLs.
- Check for Update and Apply Update now prevent repeated clicks with visible
  loading/waiting states and recover cleanly after failures or restart timeouts.

### Nyx extension popup
- Fixed scrolling flicker caused by a live-status persistence feedback loop.
- The first-paint runner cache now stores only the small rendered status payload.

## 6.6.0 - Performance, SMS verification, Nyxmoji accuracy, and update safety

### Runner controls & timing (Phases 1-2)
- Redacted timing diagnostics (opt-in via `NYXSUITE_TIMING=1`) for bridge startup,
  dashboard status, and Start/Stop actions.
- Start/Stop now acknowledge immediately and the SSE watcher confirms the final
  state; cached runner/process state, explicit STARTING/STOPPING states, and
  per-product action locks prevent duplicate clicks.
- All PowerShell/tasklist calls are time-bounded; a timed-out probe fails closed.
- Fast action acks no longer build the full 500-row status snapshot.

### Dashboard performance (Phase 3)
- Status card/tiles and the table now render independently, so bot-only SSE
  updates don't rebuild the whole table.
- Duplicate Start/Stop clicks are guarded and the requested transition is shown
  immediately.
- An aggregate `/bridge/status` cache reduces repeated full-snapshot builds.

### Extension popup performance (Phase 4)
- Popups render the last-known status on open, then refresh in the background.
- Local-API requests are timeout-bounded, config reads are cached, and status
  JSON signatures are compacted; the popup `/status` payload is much smaller.

### RAM/CPU reduction (Phase 5)
- The large Bitmoji catalog and outfit config are cached by modification time and
  no longer re-parsed on every garment/colour operation.
- SnapBoard polling pauses when the tab is hidden; the mutation observer is
  scoped to the row table area.

### Windows startup & hang recovery (Phase 6)
- All v6 paths are unified under `NyxSuite`; the native host supports the
  machine-local v6 venv; PowerShell/tasklist calls are bounded; actionable
  failure logs for Python/ports/permissions/native-messaging/AdsPower.

### SMS verification repair (Phase 7)
- Check SMS/OTP detection is row-scoped and tolerant of button/link/role/text/
  aria-label/title/case variants, with a robust page-world click.
- SMS/OTP are served ahead of long email batches; the retrieved code is verified
  as actually typed into Snapchat before it is submitted.

### Nyxmoji accuracy (Phase 8)
- The selected custom preset is the one the runner uses; custom garment IDs and
  colours are preserved when the live catalog is stale; config preservation is
  separated from runtime validity.
- The configured garment colour is applied consistently (closest swatch) and the
  colour picker waits for rendering.

### Update data preservation (Phase 9)
- Updates preserve `data/*.db`, `bridge_config.json`, `nyx_config.json`,
  `nyxify_config.json`, `bitmoji_models.json`, `bitmoji_outfits.json`, usernames,
  signup names, and logs; generated catalogs may be replaced by releases.

## 6.5.8 - Web dashboard redesign

- The web dashboard was redesigned with a new topbar and tabbed navigation
  (Nyx / Nyxify / Bitmoji / Settings) across the whole UI.
- Each product/panel now uses a modern design-token theme (background, panel,
  accent, glow, card-shadow, borders) with updated runner status strips, docks,
  controls, tiles, and command action stacks.
- Status pills show the app version, connection state, and available-update
  badge at the top bar; the layout is restructured for clearer command and
  queue controls.
- Pure UI change: existing local API and `/bridge/*` endpoints are unchanged.

## 6.5.7 - Nyxmoji outfit system and tuck-in

### Nyxmoji: outfits applied reliably with tuck-in
- Nyx now selects a full outfit (tops, bottoms, footwear, and colors) from the
  dashboard-configured presets and applies it after the bottom is selected, so
  tuck-in works correctly on every run.
- The dashboard Nyxmoji panel gained a tuck-in toggle at the bottom of the
  Bitmoji preview; each preset remembers whether the outfit should be tucked
  in, and the live editor tucks the shirt in when the preset says so.
- The Bitmoji preview now reflects the tuck-in state (is_tucked render
  parameter), so the preview matches the final avatar.

### Nyx popup: outfit style sync
- The Nyx popup now shows an Outfit style selector (Default / Mix / Casual /
  Sexy / Custom) that syncs with the runner, and the runner accepts the new
  mix/custom style values alongside the legacy mixed style.
- Custom outfits are edited per user in the dashboard Nyxmoji panel; the popup
  selector stays empty by design.

## 6.5.6 - Nyxify banned-row workflow and OTP pickup hardening

### Nyxify: scan, remove, and warm-up banned rows directly
- The Nyxify extension popup and web dashboard now expose Scan Banned Rows,
  Remove Banned, and Warm Up Banned controls backed by the new
  `/replace_banned/scan`, `/replace_banned/remove`, and `/replace_banned/warmup`
  local API endpoints.
- Remove Banned clears the row's SnapBoard Ads Power ID, rotates the proxy with
  a forced change, and removes the row from the local store; Warm Up first
  pushes the SnapBoard status to "Warm Up", and both actions report partial
  failures per row instead of aborting the batch.
- Proxy rotation requests accept an explicit `force` flag so replace-banned
  recovery always gets a different proxy instead of reusing the previous value.

### Nyxify: verification OTP pickup hardening
- The SnapBoard OTP fetch window is now one minute (was 30 seconds) for both
  email and SMS codes, giving SnapBoard more time to detect the code before the
  recovery path (fresh email/number + resubmit) is triggered.
- When the verification phase fetches an OTP, Nyxify now always clicks the
  SnapBoard Check Code / Check SMS button first and waits for a fresh code
  instead of trusting a stale row value.
- If the Check Code / Check SMS button cannot be found or clicked, the bridge
  now fails fast with an explicit error instead of spending the whole window
  waiting and reporting a generic "code not found".

## 6.5.5 - Nyxify wrong-code verification recovery

### Nyxify: recover rejected email and SMS OTP codes
- Restored wrong-code detection for email and SMS verification after the
  `v6.5.3` verification rollback.
- When Snapchat shows a wrong-code error, Nyxify now returns only to the
  verification email/phone entry step, requests a fresh email or number, and
  submits a replacement code on the same account.
- Recovery now checks whether the email/phone textbox is already visible before
  clicking the verification back button, preventing repeated back clicks from
  overshooting into the full signup form.

## 6.5.4 - Nyxify SMS phone-context fix

### Nyxify: click SnapBoard Check SMS after phone entry
- Nyxify now carries the last fetched verification phone into the SMS bridge
  request even when the restored verification flow calls `sms_fetcher()` without
  arguments.
- This preserves the `v6.2.0` verification phase shape while still giving the
  SnapBoard content script the expected phone value it requires before clicking
  Check SMS.

## 6.5.3 - Nyxify verification phase rollback

### Nyxify: restore v6.2.0 email/phone verification behavior
- Reverted only the post-"Agree and Continue" verification helper phase to the
  `v6.2.0` flow.
- Restored the `v6.2.0` email/phone retry budgets and OTP/SMS provider-call
  contract so failed SMS bridge pickup follows the older bounded recovery path.
- Added regression coverage for the restored two-attempt phone verification
  behavior and no-argument OTP/SMS fetcher calls.

## 6.5.0 - Verification click recovery and provider lock popup

### Nyxify: phone/SMS verification click recovery
- Phone verification now accepts already-entered or formatted local phone
  values before clicking the SMS submit button.
- The phone step now dispatches input/change/blur events after the phone value
  is present and can click visible Continue/Next/Send/SMS submit variants.
- Added regression coverage for formatted phone fields and alternate phone-step
  submit buttons.

### Nyxify: provider lock segmented popup controls
- Replaced the popup's old Lock in G5 / Lock in TV checkbox rows with compact
  yellow AM/G5 and SP/TV segmented controls.
- AM/SP keep the default provider behavior, while G5/TV save the existing
  `lockG5` and `lockTV` config keys that drive SnapBoard provider locking.

## 6.4.1 - Nyxify phone-step handoff speed

### Nyxify: skip stale signup-submit retries once Step 2 is visible
- Nyxify now rechecks the live signup handoff state before retrying the Step 1
  "Agree and Continue" button after a stale unable-to-process signal.
- When Snapchat has already advanced to phone verification and shows
  "Use Email Instead", Nyxify clicks the email switch immediately instead of
  waiting on the old Step 1 submit button path.
- Fast submit retries now use bounded enabled/click waits, preventing
  Playwright's default 30-second click fallback from delaying the phone/email
  verification handoff.
- Added regression coverage for stale unable-to-process handoff rechecks and
  bounded signup-submit click waits.

## 6.4.0 - Extension popup performance

### Extensions: queue tables removed from popups
- Removed the Nyx Queue table and selected-row actions from the Nyx extension popup, reducing popup DOM work while keeping runner controls, Daily Update, Nyx Scrape, and Setup & Install available.
- Removed the Nyxify Queue table and selected-row actions from the Nyxify extension popup, keeping runner controls, counters, banned proxies, Last detected, Local Sync, and Username Scrape available.
- Dashboard queue views, backend queues, and local queue APIs are unchanged; queue inspection and row-level actions now stay in the dashboard.

### Nyxify: Setup & Install parity
- Added Setup & Install directly to the Nyxify popup.
- Nyxify now opens the install web UI when the shared bridge is running, or its bundled setup helper when the bridge/native host still needs first-run setup.

## 6.3.10 - SnapBoard bridge timing recovery

### Nyxify: fail fast when a local SnapBoard bridge is not picking up requests
- Email, phone, and SMS fetches now poll the local bridge every 0.5 seconds and
  stop quickly when the pending request is not dispatched by the SnapBoard
  bridge, instead of waiting through long 120-165 second timeouts per attempt.
- Local API requests now refresh a stale bridge token after an HTTP 401 and
  retry once, which fixes device-specific token/session drift without requiring
  a full app restart.
- SnapBoard status endpoints now expose pending request metadata so the runner
  can tell the difference between "bridge has not picked this up" and
  "SnapBoard is still working."

### Nyxify: verification phase wins over stale username retry UI
- If Snapchat has already moved to the email, phone, or OTP verification phase,
  Nyxify no longer leaves the row on `retrying_signup_username` because of a
  stale username-taken element.
- "Use email instead" is clicked once per signup wait, then Nyxify waits for the
  verification controls instead of repeatedly clicking the switch.
- Added regression coverage for bridge timeout/token recovery and stale signup
  username retry detection.

## 6.3.9 - Continuous Mode pre-Bitmoji tab cleanup

### Nyx: stale signup and Bitmoji tabs are pruned before editor work
- Continuous Mode now closes leftover Snapchat signup, 403, Bitmoji login, and
  Bitmoji create tabs after Nyx attaches and before it opens the fresh Bitmoji
  flow.
- Nyx preserves one `https://start.adspower.net/` tab so the AdsPower browser
  stays alive while removing old pages that can confuse state detection.
- Manual and non-continuous Nyx runs keep the existing tab behavior.
- Added regression coverage for continuous-only pre-Bitmoji tab cleanup and
  queue routing.

## 6.3.8 - Signup handoff priority

### Nyxify: stale username errors no longer delay email handoff
- Signup progress now checks for email, OTP, phone, and welcome handoff stages
  before handling username-taken retry UI.
- This prevents stale username-error elements from keeping a row stuck at
  `retrying_signup_username` after Snapchat has already advanced to email
  verification or welcome.
- Added regression coverage for the email handoff winning over a stale
  username-taken state.

## 6.3.7 - Blank signup shell refresh

### Nyxify: recover the empty Snapchat signup shell
- Detects the blank Snapchat signup shell where `/v2/signup` loads with
  `#__next` and `__NEXT_DATA__` but no rendered signup form or verification
  controls.
- Refreshes and re-enters the saved signup details immediately for that shell,
  instead of waiting for the normal missing-page stall window.
- Preserves the new `refreshing_signup_blank_shell` progress state so failures
  during this refresh path clean up and requeue like the other signup recovery
  states.

## 6.3.6 - Signup retry recovery

### Nyxify: refresh stuck signup submits and speed retry checks
- If Agree and Continue does not click after the initial signup fill, Nyxify now
  refreshes the signup page, re-enters the saved form details, and retries
  within the existing bounded refresh budget.
- Signup refresh/refill progress states now preserve their failure step so the
  runner can clean up the half-created AdsPower profile, rotate/requeue, and
  avoid stranding the SnapBoard row.
- Username-taken retry settling is now 700ms, and unable-to-process retry
  settling is now 300ms, while keeping the same button-enabled and blocker
  checker logic.

## 6.3.5 - Continuous Mode handoff disconnect

### Nyxify: disconnect before Nyx handoff without closing signup tab
- Continuous Mode now stops Nyxify's Playwright/CDP connection after the rename
  phase and before queueing the AdsPower profile into Nyx.
- The completed Snapchat signup tab is left open instead of being closed by
  Nyxify, preventing stale page-control handles while keeping the browser state
  intact for Nyx's normal attach flow.
- Regression coverage now verifies that the signup tab remains open and the
  Nyxify Playwright connection is stopped before the Nyx queue call.

## 6.3.4 - Continuous Mode editor speed scope

### Nyxify: close signup tab before Nyx handoff
- Continuous Mode now closes the Snapchat signup/account-creation tab after a
  confirmed signup and before queueing the AdsPower profile into Nyx.
- The AdsPower profile itself stays open for the immediate Bitmoji handoff;
  only the stale signup tab is removed.

### Nyx: Automation Speed only affects Bitmoji editor actions
- The Automation Speed slider now affects the face, outfit, and save editor
  phase only.
- OAuth, login, redirect, and editor-load waits keep their fixed timing so the
  speed setting does not make authentication less reliable.

## 6.3.3 - Transparent macOS tray icon

### Bridge: optional invisible menu-bar icon on macOS
- Added a Settings toggle for a transparent macOS menu-bar tray icon.
- When enabled, the tray keeps its clickable menu-bar target and the same
  Open/Start/Stop/Restart/Update actions, but the status dot is invisible.
- The preference is saved in bridge runtime config and preserved across future
  updater installs.

## 6.3.2 - CDP attach recovery

### Nyx: recover stuck AdsPower browser attach during Continuous Mode
- Added bounded Playwright CDP attach attempts so an AdsPower browser that is
  half-responsive cannot park a continuous handoff for long 180-second retry
  cycles.
- Nyx now records `recovering_cdp_attach`, closes the AdsPower profile, and
  retries the Bitmoji flow when CDP attach times out.
- Runner startup now requeues orphaned `RUNNING` Nyx rows, so a restart after a
  stuck attach does not leave Continuous Mode waiting forever.

## 6.3.1 - Continuous Mode queue-slot recovery

### Nyx: continuous handoffs no longer wait behind manual-login rows
- Fixed a Continuous Mode stall where a freshly queued Nyxify handoff could stay
  pending while an older non-continuous Nyx row sat `RUNNING` at `need_login`
  and occupied the only available Nyx slot.
- Nyx can now temporarily borrow one slot for high-priority
  `nyxify_continuous` tasks when the only blocker is a non-continuous manual
  login wait.
- Active Bitmoji/editor work is still allowed to finish normally; the extra
  slot only applies to the manual-login wait case.

## 6.3.0 - Continuous Mode OAuth cleanup

### Nyx: Snapchat account pages no longer look like OAuth consent
- Tightened OAuth Continue fallback detection so regular Snapchat account and
  welcome pages are not mistaken for the `Continue to Bitmoji?` consent screen.
- This prevents the OAuth-cleared check from sticking after the Continue click
  when a non-consent Snapchat tab remains open.
- Includes the v6.2.9 Continuous Mode OAuth tab priority and AdsPower GUI rename
  recovery; restart the bridge/runners after updating so the loaded process uses
  the current code.

## 6.2.9 - Continuous Mode OAuth tab and GUI rename recovery

### Nyx: Continuous Mode OAuth tab wins over stale login tabs
- Fixed a Continuous Mode stall where a newly handed-off profile could remain
  `RUNNING` at `need_login` while another browser tab was already showing
  Snapchat's `Continue to Bitmoji?` OAuth consent page.
- Nyx now scans all visible CDP browser contexts and prioritizes OAuth/editor
  progress states before older Snapchat login tabs, then continues into the
  OAuth Continue click path.
- Auto-login now checks for handoff progress before locking onto a login form,
  preventing stale login tabs from interrupting immediate Nyxify-to-Nyx runs.

### Nyxify: AdsPower GUI rename retries the temp-name view
- GUI rename now remembers the Nyxify temporary AdsPower profile name and, if
  the row is not visible, reapplies the `Name contains <temp>` filter before
  opening the rename dialog.
- This fixes rename failures where AdsPower was left on another search filter,
  causing `Could not open the rename dialog` even though the temp-named profile
  existed.

## 6.2.8 - OAuth Continue screen classification fix

### Nyx: OAuth consent no longer appears as need_login
- Fixed the Continuous Mode stall where the dashboard showed `need_login` while
  the AdsPower browser was already on Snapchat's `Continue to Bitmoji?` OAuth
  consent page.
- OAuth consent screens are now identified before Snapchat login screens, so
  hidden account fields on the consent page cannot misroute the flow into
  manual-login wait.
- Nyx now proceeds directly to the OAuth Continue click path and then into the
  Bitmoji editor automation.

## 6.2.7 - Continuous Mode Bitmoji auth handoff fix

### Nyx: continue past transient Snapchat login during Continuous Mode
- Fixed a Continuous Mode handoff case where Nyx could mark the task
  `need_login` and wait manually even though the newly created Snapchat session
  was already advancing into Bitmoji OAuth.
- While auto-login is waiting for a Snapchat login form, Nyx now checks whether
  the browser has moved to OAuth Continue, gender selection, the editor, account
  home, or the Bitmoji login redirect recovery path, then continues from that
  state.
- `need_login` is now emitted only after automatic login cannot continue and Nyx
  is actually entering manual-login wait.

## 6.2.6 - Continuous Mode immediate Nyx handoff

### Nyxify: one-account Continuous Mode pipeline
- Continuous Mode now creates one Snapchat account, renames the AdsPower
  profile, immediately hands that same profile to Nyx, and waits before
  launching the next Nyxify signup.
- Rename happens before the Nyx handoff. If GUI/API rename fails, Nyxify records
  `profile_rename_failed` on the row but still starts Nyx because the account
  already exists.
- Continuous Mode now uses effective Nyxify launch concurrency `1` and marks
  ready rows as `waiting_for_continuous_nyx` while a continuous Nyx task is
  pending or running.

### Nyx: priority run-now queue
- Added a `/queue/run_now` local API handoff that writes high-priority
  `nyxify_continuous` tasks with carried username/password credentials.
- Pending task selection now claims high-priority continuous tasks before older
  normal pending rows, while preserving existing in-progress Nyx work.
- Normal queue upserts no longer downgrade an active continuous handoff's
  source, priority, or credentials.

## 6.2.0 - Dashboard and extension popup control cleanup

### Dashboard: consistent Nyx/Nyxify control placement
- Reorganized the Nyx and Nyxify dashboard command areas into matching zones:
  runner controls, product tools, queue actions, selected-row actions, search,
  and Nyxify-only banned-row utilities.
- Queue and selected-row buttons now keep the same placement across Nyx and
  Nyxify, reducing the drift that made the right-side toolbar hard to scan.
- Responsive wrapping now keeps the control rows inside the viewport without
  horizontal overflow.

### Nyxify extension popup: runner controls promoted to the top
- Moved Nyxify Start/Stop and Pause/Resume into the top runner card above the
  queue counters, matching the Nyx popup workflow.
- Added a compact runner-state pill and disabled Pause by default until the
  runner is active.
- Shrank the Nyx/Nyxify popup brand headers so controls have more room in the
  extension popup viewport.
- Removed the Nyxify popup rows for Push AdsPower ID to SnapBoard and Apply
  AdsPower tags while keeping those backend/options settings intact.
- Moved Auto-Fill Row and Auto-fill target to the top of the Nyxify toggle
  panel.
- Removed the manual Save Dashboard Settings button from both extension popups;
  editable dashboard settings now auto-save as values are changed or typed.

## 6.1.14 - Retry hardening for AdsPower open failures and account creation

### Nyx: whole-profile retries now catch transient open exceptions
- AdsPower/CDP launch failures such as delayed DevTools endpoints, target-closed
  races, and Bitmoji editor load exceptions now flow through the same
  whole-profile retry policy as normal failed task results.
- Missing profile/config failures still stop immediately, so Nyx does not waste
  retries on permanent setup problems.

### Nyxify: account creation cleanup/retry covers OTP and SnapBoard stalls
- Signup runs that reach OTP, SnapBoard handoff, or submitted signup states
  without a final Snapchat username now use the existing cleanup path: close and
  delete the created AdsPower profile, clear the SnapBoard AdsPower ID, rotate
  the proxy when configured, and requeue the row as pending.
- This prevents half-created accounts from being marked failed without the
  retry/cleanup behavior already used for earlier signup blockers.

## 6.1.9 — AdsPower new ID-column UI compatibility

### AdsPower GUI control: new table layout support
- Updated the no-API AdsPower GUI automation on macOS and Windows for the new
  Profiles table that separates profile `ID` from optional order columns.
- Row actions now anchor on the visible AdsPower `ID` plus the needed action
  controls (`Open`, `Close`, rename/menu, delete/select), while ignoring
  optional/reordered columns such as `#`, dates, platform, and tags.
- If the AdsPower `ID` column is hidden, NyxSuite now fails clearly and asks the
  operator to enable the `ID` column instead of guessing from row order.
- Verified live against AdsPower with `Snapchat: xyz`: create, open, close,
  rename, and delete all complete through the desktop GUI.

## 6.1.8 — Dashboard runner controls pinned upper-left

### Dashboard: Start/Stop and Pause/Resume stay put
- Moved the Nyx and Nyxify runner controls into the left status block so
  **Start/Stop** and **Pause/Resume** always appear in the upper-left of the
  dashboard instead of drifting into the right-side toolbar.
- Kept the compact command layout by sharing the runner controls and live status
  on one row, with queue/action tools still organized on the right.

## 6.1.7 — Replace banned, faster dashboard command layout

### Nyxify: Replace banned accounts from popup or dashboard
- Added **Replace banned** to the Nyxify popup and web dashboard. Scan SnapBoard
  banned rows, review the count, then replace them row by row.
- Replacement now immediately reserves a fresh Full Auto username, updates the
  SnapBoard username, requests a fresh email, rotates the proxy, deletes the old
  AdsPower profile through the current API/GUI control mode, clears the
  SnapBoard AdsPower ID, sets the row back to **Warm Up**, and resets the row in
  Nyxify as **PENDING**.
- The existing Nyx SnapBoard **Replace profile** action now uses the same
  replacement flow for one row and no longer asks for a confirmation popup.

### Nyx: quicker manual queue handoff
- The SnapBoard status-dot menu now includes **Add to Nyx pending**, which sends
  that row/profile directly to Nyx pending without a confirmation popup.
- The Nyx dashboard no longer shows **Clear Queue**, reducing the chance of
  wiping active queue data from the dashboard.

### Dashboard: compact command header
- Nyx and Nyxify controls were reorganized into a compact command header with
  status/count tiles on the left and action/search controls on the right, so the
  queue table starts higher and gets more usable space.

## 6.1.6 — Nyxmoji editor overhaul: every outfit colour, working Shuffle, preset gallery

### Nyxmoji: full outfit colour palette (all colours)
- Every outfit slot (**Tops, Outfits, Bottoms, Dresses, Footwear, Outerwear**) now
  offers a **complete full-spectrum colour palette** — neutrals, every hue family,
  and earthy browns — instead of the old 14-swatch strip. Pick one colour in
  **Fixed**, or tick a whole **colour pool** in **Random** (with **All / Clear**).
- The live editor already snaps each choice to the nearest real Bitmoji swatch and
  the preview renders the exact tint, so any colour applies cleanly and never fails
  a profile — an unmatched colour just falls back to a random valid pick.

### Nyxmoji: Shuffle preview actually shuffles
- **Shuffle preview** now rolls a brand-new complete look on every press — like
  generating a random Bitmoji. It honours your **Fixed** pins, draws **Random**
  features from their pool, and fills every **Preset/unconfigured** feature from the
  full catalogue, so the preview visibly changes each time. The stage falls back to
  the last good / base look if a rare combination can't render, so it never blanks.

### Nyxmoji: Preset shows the whole gallery
- A feature left on **Preset** now displays a **read-only gallery of every option's
  PNG** (and the full colour palette for outfits), with the model's built-in choice
  highlighted — filling the space that used to be an empty panel. **Click any option
  to pin it as Fixed** without hunting for the mode tab.

### Nyxmoji: a roomier, faster editor
- The option grid now **fills the panel width and viewport height** (denser packing =
  far more options visible at once), the **avatar stage stays pinned** while you
  scroll, and a new **filter box** narrows big lists (e.g. 291 tops) by id.
- **Random** pools gain **Select all / Clear / Invert** (filter-aware), so building a
  pool is a couple of clicks instead of tapping dozens of thumbnails.

## 6.1.5 — Banned proxies persist (no more clearing on save/restart)

### Nyxify: banning a proxy now sticks
- Banning a subnet from **Proxy Ranking** (or a proxy from the popup) is saved
  immediately and **survives restarts and updates**. Previously an unrelated
  config save — even just flipping a toggle — could push a stale/empty banned
  list that overwrote the stored one, so bans quietly disappeared.
- The banned list is now owned by the runner: the "Ban" buttons **add** to it,
  and only a deliberate edit of the **Banned proxies** text box replaces it.
  Ordinary config saves no longer touch it, so nothing can wipe your bans by
  accident. (The banned list was already kept across updates; this closes the
  path that cleared it on the next save/restart.)

## 6.1.4 — Tray sync fix, OTP/phone retry hardening, SnapBoard re-login

### Tray: Start/Stop now tracks the real state (macOS + Windows)
- The menu-bar/tray menu could show an enabled **Start** while a product was
  already running (or the reverse) when it had been started/stopped from the
  dashboard or a hotkey. The tray now **rebuilds its menu whenever the runner
  state changes**, so Start/Stop and the status lines always match reality.

### Nyxify: email/phone re-order respects the 60-second cooldown
- SnapBoard's "get new email / number" buttons enforce a ~60s cooldown, during
  which they are disabled and a click does nothing. Requesting a replacement now
  **waits the cooldown out and then re-orders**, instead of silently clicking a
  disabled button — the old behaviour looked like "the number never changed" and
  failed the account.

### Nyxify: SMS code missing → rotate the number instead of failing
- When a phone is accepted but the **SMS code never arrives**, Nyxify now goes
  back, orders a **fresh number**, resubmits it, and refetches the code **on the
  same account** — rather than failing the profile and recreating it from
  scratch. Only a persistently rejected number still routes to the recreate path.

### Nyxify: recover an unresponsive / logged-out SnapBoard
- Email, phone and OTP/SMS fetches now **recover a signed-out board by logging
  back in** before retrying. Email/phone additionally fall back to a full board
  refresh (a stale board is a common "no pending order" cause). OTP/SMS use a
  lighter re-login-only retry so a code that simply "hasn't landed yet" never
  triggers a board reload that would disrupt other in-flight accounts.

### SnapBoard: auto sign-in now types stored credentials
- Auto sign-in no longer depends on Chrome autofill (which usually won't fill
  SnapBoard's login form, and can hide the password from the page). Enter your
  SnapBoard **Name + Password** in the **Nyxify extension Options** — they're
  stored locally (kept out of the synced runner config) and typed into the login
  form **only when a field is blank on a signed-out board**, then submitted once
  both fields are populated (so it never posts an empty login).

## 6.1.3 — Cookie warm-up sites are prefilled and editable

### Nyxify: the built-in warm-up site list shows in the dashboard
- The **Cookie warm-up sites** editor now comes pre-filled with the full built-in
  list, so every site is visible and can be **edited or removed** individually
  instead of being a hidden fallback. Clearing the box entirely restores the
  built-in list on the next open. The list now lives in one place shared by the
  editor and the runner.

## 6.1.2 — SnapBoard auto-login, Lock in TV phone provider

### SnapBoard: auto-login when logged out
- When a SnapBoard tab shows the sign-in screen (session expired / logged out),
  Nyxify now **auto-clicks "Sign In"** so the dashboard comes back on its own.
  It does **not** type any credentials — it relies on Chrome's saved-password
  autofill for that profile, and only submits once the name field is populated,
  so it never posts an empty login. Attempts are spaced out and capped per
  sign-in-screen appearance, and reset automatically once the dashboard loads.

### Nyxify: Lock in TV (phone provider)
- New **Lock in TV** toggle (popup + options), mirroring **Lock in G5**: it keeps
  the SnapBoard **phone/SMS provider pinned to TextVerified (TV)** after a
  refresh, re-selecting it whenever the toggle drifts back to SMSPool. Lock in G5
  (email → G5) and Lock in TV (phone → TV) work independently and can both be on.

## 6.1.1 — whox.com trust-score gate, editable cookie warm-up

### Nyxify: whox.com deep-scan trust gate before signup
- After an AdsPower profile opens (before cookie warm-up), Nyxify now opens
  **whox.com**, runs its **Deep Scan**, and reads the resulting Deep Trust Score.
  If the score is **at or above the threshold** the profile continues to warm-up
  and signup; if it's **below**, the profile is closed + deleted, its SnapBoard
  AdsPower id is cleared, the proxy is refreshed, and the row is requeued to be
  created from scratch — the same cleanup+retry path a failed signup uses.
- The gate reads the **real** settled score, not the count-up animation whox
  plays after the scan completes: it waits for the score to hold steady before
  deciding, and likewise waits for the Fast score to settle before triggering the
  Deep Scan (clicking too early yields a degenerate result).
- **Fail-open by design:** if whox can't be reached, the page never settles, or
  the scan times out, the profile is **kept** (never wrongly deleted). Set
  `NYXIFY_WHOX_FAIL_OPEN=0` to fail-closed instead.
- New dashboard controls (Nyxify config): **whox Trust Check** on/off toggle
  (on by default), **whox min trust score** (1–100, default 70), and **whox URL**.

### Nyxify: cookie warm-up is now configurable from the dashboard
- **Cookie Warm-up** on/off toggle and an editable **warm-up sites** list (one
  URL per line; leave blank to use the built-in curated pool). A short custom
  list is handled safely — the sample size clamps to the number of sites given.

### Nyxify: config-save fixes
- The `/config` endpoint now persists **Disable extensions on create** and the
  names directory, which were previously dropped on save, alongside the new
  whox / cookie-warmup settings.

## 6.0.9 — Nyxmoji preview + colours + Recommend, proxy ranking chart, compact dashboard

### Nyxmoji: the avatar preview always renders (and reflects the randomizer)
- Opening the editor or switching model now auto-shuffles a real sample from each
  Random pool, so the preview shows an actual random draw instead of the first
  pool item. When a rare parameter combination can't render, the preview falls
  back to the model's base look (with a small caption) instead of blanking out to
  "Preview unavailable".

### Nyxmoji: Outfits vs Tops de-duplicated
- The catalog had captured "Outfits" as a byte-identical copy of "Tops" (same
  items, same top slot) with an empty colour list, and its preview used a
  parameter the avatar endpoint ignores — so it rendered blank. Outfits is now
  relabelled **"Outfits (Tops slot)"**, previews correctly, and inherits Tops'
  colour swatches. A hint in the editor makes the shared slot explicit.

### Nyxmoji: random/fixed clothing colours are applied to the created avatar
- Colours chosen in the editor (a fixed colour, or a random pick from a colour
  pool) were saved and previewed but never applied during creation. The bot now
  clicks the matching colour swatch for each outfit piece. It stays fully
  backward compatible: unconfigured models keep the existing random colour, and a
  colour that can't be matched never fails the profile. New **All** / **Clear**
  buttons build the colour pool in one click.

### Nyxmoji: one-click "Recommend" + per-feature Reset
- A new **✨ Recommend** button fills coherent random pools (tops, bottoms,
  footwear, colours, plus some hair variety) as a starting point, and each
  feature gets a **Reset** control that returns it to the model preset.

### Nyxify: Proxy Ranking now has a snapshot bar chart
- The proxy ranking panel draws a compact bar chart of each subnet's score
  (lower is better), coloured good/amber/bad to match the table, above the list.

### Dashboard: more compact and keyboard-accessible
- Tighter spacing, smaller tiles, and denser tables reclaim vertical space while
  keeping the same theme and features. Status tiles are now real buttons with
  ARIA state, every control shows a keyboard focus ring, and low-contrast text
  was lightened.

## 6.0.8 — Bitmoji login-redirect recovery, real site theme, proxy rotation + ranking, macOS dashboard auto-refresh

### Nyx: Bitmoji no longer gets stuck on the login page
- After a Snapchat OAuth redirect, Bitmoji sometimes re-renders its own
  `bitmoji.com/login` page (the `?code=` exchange didn't complete). That page
  carries a "Log In with Snapchat" button, so it was mis-classified as a Snapchat
  login and the flow ran the credential auto-login — which has no form to fill
  there — looping "no login form context … falling back to manual login" until it
  timed out. The callback is now detected as a distinct `LOGIN_REDIRECT` state and
  recovered by reloading (to finish the code exchange) and, if still stuck,
  re-clicking "Log In with Snapchat" to re-run OAuth against the existing session.

### Nyx: automation keeps the site's real theme (no more white editor)
- The Bitmoji editor is dark only via `@media(prefers-color-scheme: dark)`.
  Automation used to leave the page in light, so it rendered **white** during a
  run and snapped back to dark afterwards. The forced-dark override (injected CSS,
  `matchMedia` patch, MutationObserver) is gone; automated pages now simply mirror
  the host OS appearance, so the site's own theme renders exactly as it does for a
  human — including the editor tab that previously flashed white.

### Nyxify: more reliable proxy blocker + GUI proxy-checker rotation
- The runner-driven SnapBoard rotation now uses the same robust multi-click path
  as the manual rotate (clicks up to N times, waits ~22s for the proxy cell to
  actually change) instead of a single click with a 16s window that reported a
  slow-but-successful rotation as "did not change".
- Blocked/failed rotations are tagged by reason (`blocked` vs `check_failed`),
  back off between failed rotation requests, and surface why they failed.
- The in-form "Check Proxy" wait was widened (a failing connection test can take
  longer than a few seconds to say so) and now logs at WARNING when it proceeds
  with no verdict, so a proxy that was never rotated is visible.

### Nyxify: Proxy Ranking dashboard (new)
- New **Proxy Ranking** panel (button next to Full Auto Editor) ranks proxies by
  **subnet**, good → bad, from how often each needed a retry, failed a creation,
  or hit a ban — updated every time a proxy is used. Each row has a one-click
  **Ban** that adds the subnet straight to the Proxy Blocker.

### macOS: auto-refresh an unresponsive AdsPower dashboard
- When the AdsPower dashboard hangs during GUI automation (New Profile button or
  Profiles search bar never resolves), Nyx now triggers **Window ▸ Refresh**
  (Shift-Cmd-R) automatically and retries, instead of failing the profile — no
  manual menu click needed.

## 6.0.7 — Bitmoji outfit fallback, roll back to any version, Windows Chrome-kill fix, SnapBoard auto-refresh

### Bitmoji: stop "scrolling forever" when an item leaves the catalog
- Bitmoji rotates its clothing catalog, so configured outfit item ids (e.g.
  `bottom=788`, and every currently-configured footwear id) periodically stop
  existing. The exact-match scan then never found them, scrolled the panel to the
  bottom, and failed the whole profile — the "scroll forever" symptom. Outfit
  selection now still **prefers** the configured item, but when its id is gone it
  falls back to any available item of the same category so the avatar still gets
  dressed (deterministic per profile, skips blocked ids). Toggle with
  `NYX_OUTFIT_FALLBACK_ANY=0` for strict exact-item behaviour.
- Also bounded the Bitmoji panel scroll calls (were able to hang ~30s each) so a
  slow-rendering category can't stall the run.

### Updates: roll back to any published version
- The rollback picker now lists **every** published release, not just local
  snapshots — pick any older version in Dashboard → Settings and it downloads and
  applies that release if no local backup exists. Local backups kept bumped from
  2 to 20 (env `NYX_KEEP_BACKUPS`).

### Windows: running the GUI no longer kills Chrome
- Fixed a PID-reuse bug where a recycled process id that now belonged to
  `chrome.exe` could be trusted or force-killed as if it were our runner. The
  runner now verifies a pid actually belongs to our process (image/cmdline)
  before trusting or terminating it.

### Nyxify: SnapBoard auto-refresh + email/phone OTP retry parity
- When an email or phone OTP fetch comes back "no pending order for this account"
  (often just a stale SnapBoard tab), Nyxify now refreshes the SnapBoard tab
  (with a cooldown) and retries. Phone OTP got the same no-pending retry and
  back-button recovery that email already had.

### Snapchat signup: login-phase stall fix
- Bounded the `scroll_into_view` on the Snapchat username/login field (was able
  to block ~30s and stall the signup) to a short, non-fatal timeout.

### Nyx extension: daily-update AdsPower id now auto-saves
- The Start-ID field in the extension popup now debounced-auto-saves as you type
  (and on blur). Previously the 15s panel refresh wiped the typed id because it
  was never persisted.

## 6.0.6 — macOS tray color fix, tiny dot, restart controls

- Fixes the macOS menu-bar dot rendering monochrome/black: the icon is now
  installed as a non-template NSImage and repainted on the main thread (AppKit
  UI mutations from the background poll thread were silently ignored, so the
  color never showed). The dot is now a small color indicator.
- Recolors the indicator: **blue** while Nyx is running, **gray** while Nyxify
  is running, a split blue/gray dot when both run, a faint ring when idle.
- Makes the dot **tiny** (small padded glyph) instead of filling the menu bar.
- Adds **Restart Nyx** and **Restart Nyxify** to the tray menu alongside
  Start/Stop.

## 6.0.5 — Auto-retry transient Bitmoji fails, macOS tray redesign, parallel-create dedup

### Bitmoji: auto-retry the whole profile before failing
- A generic Bitmoji step failure (a category/trait panel that didn't render in
  time) now re-runs the whole profile automatically before the row is marked
  FAILED — the same thing users did by hand with "reset failed → rerun", which
  is why those reruns succeeded. Terminal results (banned / dead proxy / browser
  closed / already-has-bitmoji) are never retried. Tunable via
  `NYX_BITMOJI_PROFILE_RETRIES` (default 2 extra attempts) and
  `NYX_BITMOJI_RETRY_BACKOFF_SECONDS`. Cross-platform (Windows + macOS).

### Nyxify: two parallel creates can no longer merge onto one profile
- Fixed the race where two parallel profile creations resolved to the **same**
  AdsPower id — so two signups ran on one profile and the same id was written
  into two SnapBoard rows. Profile-id discovery now keys off the set of ids
  present *before* the create and a process-wide assigned-id registry, so it
  always returns the genuinely-new id and never one already handed to another
  create. Robust against transient accessibility mis-reads of the serial
  watermark that used to make it return the newest (other task's) row.

### macOS/Windows tray redesign
- The menu-bar item is now a **color status dot**, not the app icon: violet when
  Nyx is running, cyan when Nyxify is running, a split dot when both run, and a
  faint hollow ring (near-invisible but still clickable) when idle. It repaints
  live as runners start and stop.
- The dropdown is flat: **Start/Stop for both Nyx and Nyxify are shown directly**
  (no submenu to hover into), each with a live "running / stopped" status line,
  and Start/Stop enable based on the current state. The Dock icon stays hidden.

## 6.0.4 — Bitmoji completion, warm-up unblock, deleted-profile handling, SnapBoard sign-in

### Bitmoji: complete every profile (no random stops)
- Trait, paired-earring and outfit steps now retry as a whole unit: a step that
  can't land re-opens its category/subcategory panel, waits for the items to
  actually render, then clicks the *same* intended item again. The random
  "stops at paired earrings / outfit" failures were a click firing into a panel
  that had switched category but not yet painted — never a wrong selector, so
  the fix only makes the correct choice land, it never bypasses or substitutes.
- Tunable via `BITMOJI_STEP_CLICK_RETRIES`, `BITMOJI_STEP_UNIT_RETRIES`,
  `BITMOJI_PANEL_ITEMS_TIMEOUT`.

### Cookie warm-up no longer blocks signup (Windows hang)
- Warm-up pages now auto-dismiss any `beforeunload`/alert/confirm dialog a stray
  navigation click raises — an unanswered dialog used to freeze the tab and its
  close, so the site never closed and signup never started (manually closing the
  tab was the known workaround). Page close is also timeout-guarded and skips
  the beforeunload handler.
- Hard per-site and whole-phase caps (`NYXIFY_COOKIE_WARMUP_PER_SITE_HARD_TIMEOUT`,
  `NYXIFY_COOKIE_WARMUP_TOTAL_HARD_TIMEOUT`) guarantee warm-up always yields to
  the signup even if a site wedges.

### Deleted Nyxify profiles no longer run under Nyx
- When Nyxify deletes a profile (failed-signup cleanup, stale-pending cleanup,
  or a Replace action) its Nyx queue row is removed and the id archived, so Nyx
  never opens a deleted profile just to fail with `profile_missing`, and an
  extension/SnapBoard re-sync can't re-queue it.
- A run-time `profile_missing` also archives the id for the same reason.

### Nyx auto sign-in uses the SnapBoard password
- When a profile's Snapchat session has dropped and the **sign-in** page shows
  (the account exists but is logged out), Nyx now signs back in with the
  SnapBoard row's Password and the username Nyxify confirmed — not a fixed
  default. The sign-in walk is more resilient (waits for the form, retries both
  steps, ignores the un-renamed temp profile name as a username) and can recover
  a session that drops mid-flow instead of timing out.

### Nyxify: extension turn-off during account creation is now opt-in
- The Chrome-extension disabling step during signup is OFF by default (new
  "Disable extensions on create" toggle in Nyxify settings). The browser open,
  cookie warm-up and signup are unchanged; extensions are simply left as
  configured while the account is created.

### Editable accounts-per-hour (Nyx extension)
- The Daily Report's expected-hours rate is now an editable "Accounts / hour"
  field (persisted) instead of a fixed 7.

## 6.0.3 — Per-product hotkeys, full stop, and Bitmoji queue unblocking

### Hotkeys: Ctrl+F8 = Nyx, Ctrl+F7 = Nyxify
- Each product now has its own dedicated global stop/start hotkey: **Ctrl+F8**
  always controls Nyx and **Ctrl+F7** always controls Nyxify. The old shared
  Ctrl+F8 acted on whichever dashboard tab was viewed last (default Nyx), so
  with Nyxify running it could fail to stop it — or start Nyx instead.
- A hotkey stop is the same **full stop** as the dashboard Stop button, and the
  supervisor now confirms the runner actually died: a runner that survives the
  normal kill (e.g. wedged in a blocking call) is force-killed within ~3s.

### Nyx profiles stuck "waiting for Nyxify" (Bitmoji not proceeding)
- With continuous mode on, any queued Nyxify work used to hold **every** Nyx
  profile that had no matching Nyxify row — old, fully signed-up profiles never
  started their Bitmoji run. The guard now only holds while a signup is
  actively RUNNING, and only for a bounded id-sync window (15 min, tunable via
  `NYXIFY_PROFILE_SYNC_HOLD_MAX_SECONDS`).
- A Nyxify row that failed **after** the Snapchat account was real (rename /
  close / handoff bookkeeping hiccups) no longer parks its Nyx row forever —
  the Bitmoji run proceeds and the bookkeeping problem stays visible on the
  Nyxify row.
- A Nyxify row that failed **before** an account existed now fails the Nyx row
  visibly as `nyxify_signup_failed` instead of re-holding it every poll.
- Every "waiting for Nyxify" hold is now bounded: a crashed/stale Nyxify can
  no longer park Nyx rows indefinitely (10-min staleness valve, tunable via
  `NYXIFY_BOOKKEEPING_STALE_SECONDS`).

### Continuous mode
- Bitmoji creation now reliably continues once the account is real: an
  AdsPower rename failure no longer blocks the SnapBoard id push or the Nyx
  handoff (the rename problem is still recorded on the row).
- Nyx can no longer interrupt account creation: the completed-signup row is
  published as `closing_profile` while Nyxify is still closing/renaming the
  profile, and only becomes ready (`profile_closed` / `profile_close_failed`)
  when Nyxify is done with it. Previously Nyx could open the profile mid-close
  and the fresh Bitmoji run died with `manual_terminate`.
- The last handoff of a batch no longer strands below the start threshold:
  a held handoff re-arms the flush latch so it is retried on the next poll.

## 6.0.2 — macOS search-click fix + signup stall recovery

- Fixes the Nyxify temp-name search on macOS ("Name contains &lt;temp&gt;") clicking beside the suggestion instead of on it. The dropdown row is now grouped by vertical-overlap ratio and the click lands on the field label itself, so it stays accurate across AdsPower window zoom levels and screen resolutions. The Nyx Profile-ID search is unchanged.
- Nyxify signup: when the page is stuck for a very long time and "Agree and Continue" never becomes clickable — even with a captcha present — the form is reloaded and re-filled from the saved credentials as a last resort.
- Nyxify signup: when the form is detected blank (for example after a manual page refresh), the saved credentials are re-entered immediately instead of waiting out the stall timer.

## 6.0.1 — AdsPower control mode selector

- Adds a dashboard Settings control for AdsPower mode: Auto, API, or GUI.
- GUI mode skips Local API create/open/close/delete/rename attempts and drives the AdsPower desktop app first, reducing cold-start delay on no-API accounts.
- API mode disables GUI/CDP fallback for devices where Local API-only behavior is preferred.
- Ships the AdsPower GUI template assets in the release ZIP for safer first-run setup on new devices.

## 6.0.0 — Public v6 source and release line

- Rebrands source-visible older line labels to v6.
- Uses the public `jaymaroldan026/nyxsuite-v6` repo for both source and dashboard update releases.
- Nyxify signup now uses each SnapBoard row's Password column, with the old default password retained only as a blank-row fallback.
- Keeps the no-API AdsPower GUI automation path for Windows and macOS.

## 5.0.0 — First no-API build

First public no-API release. Runs without the AdsPower Local API and
without any license/activation.

### No-API AdsPower automation (Windows)
When the AdsPower Local API is permission-gated (`9110 No local API permission`,
common on Employee/sub-accounts), the suite drives the AdsPower **desktop app**
directly instead of failing:

- **Create** profiles via the New-Profile form (name, group, proxy → Check Proxy → OK).
- **Open** profiles via the search bar, then attach Playwright over CDP.
- **Rename** profiles (Name edit-pencil) after a successful signup.
- **Close** profiles (row Close button) when a run finishes.
- **Delete** profiles (select → trash → confirm) on the signup retry path.
- Proxy pre-check falls back to a socket test when AdsPower's checker is gated.

The GUI automation is resolution-/DPI-/window-position-independent (Windows UI
Automation), with an OpenCV template-matching fallback. All GUI operations are
serialized across the Nyx and Nyxify processes by a cross-process lock.

### Other
- License/activation removed — runners start unconditionally.
- Cross-OS: installs and runs on Windows, macOS and Linux (Local API + Playwright);
  the no-API GUI fallback works on Windows and macOS.
- The Bitmoji and Snapchat-signup automation (Playwright) is unchanged.
