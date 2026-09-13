# NyxSuite Performance Root Cause Investigation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Find which NyxSuite component consumes the most CPU/RAM during use, and identify the real root cause of slow Nyx/Nyxify popup opening and delayed NyxSuite bridge startup/toggle behavior.

**Architecture:** Treat this as a root-cause investigation, not a fix-first task. Capture synchronized evidence across operating-system processes, bridge timing logs, local HTTP endpoints, extension popup behavior, and live browser interaction, then form one hypothesis at a time from the data.

**Tech Stack:** Python stdlib, zsh, macOS process tools (`ps`, `lsof`, `top` if useful), existing NyxSuite timing diagnostics (`NYXSUITE_TIMING=1`), local HTTP ports `8865`, `8866`, `8869`, `8870`, Chrome/extension live run, pytest.

---

## Investigation Rules

- Do not propose or implement performance fixes until Task 1 through Task 5 produce evidence.
- Do not log secrets, tokens, account data, emails, phone numbers, OTPs, proxies, cookies, or SnapBoard account rows.
- Use `/private/tmp/nyxsuite-perf-*` for generated diagnostic artifacts unless the user explicitly wants repo files.
- If a code change is needed for diagnostics, keep it small, temporary, and clearly marked as instrumentation.
- Preserve unrelated worktree changes.
- Record exact timestamps for every measurement so process samples, browser actions, and bridge logs can be correlated.

## Files and Components to Inspect

- `bridge_app.py`: bridge startup, local API startup, tray/menu, bridge actions.
- `core/webui_server.py`: dashboard `/bridge/status`, `/bridge/events`, action dispatch, SSE watcher.
- `core/timing_diag.py`: existing opt-in timing log support via `NYXSUITE_TIMING=1`.
- `core/runner_supervisor.py`: Nyx/Nyxify process start/stop, orphan process scan, PID identity checks.
- `core/process_utils.py`: subprocess/timeouts, process lookup, runner process launch.
- `core/nyx_controller.py`: Nyx `status_snapshot()`, `light_status()`, start action.
- `core/nyxify_controller.py`: Nyxify `status_snapshot()`, `light_status()`, start action.
- `core/adspower_live.py`: live AdsPower/SunBrowser CDP endpoint scan and row annotation.
- `nyx_extension/popup.js`: Nyx extension popup rendering, cached first-paint, bridge toggle.
- `nyxify_extension/popup.js`: Nyxify extension popup rendering, status refresh, bridge toggle.
- `nyx_extension/background.js`: Nyx extension local API calls and storage work.
- `nyxify_extension/background.js`: Nyxify extension local API calls, SnapBoard bridge, storage work.
- Existing tests to run after any diagnostic-safe code changes:
  - `tests/test_popup_first_paint.py`
  - `tests/test_controller_fast_ack.py`
  - `tests/test_runner_fast_state.py`
  - `tests/test_process_utils_timeouts.py`
  - `tests/test_bridge_duplicate_open.py`
  - `tests/test_nyxify_bridge_waits.py`

---

### Task 1: Baseline The Workspace And Running Processes

**Files:**
- Read: `Agent Memory/00 Dashboard.md`
- Read: `Agent Memory/Agent Memory.md`
- Read: `Agent Memory/00 Home/Memory Hub.md`
- Read: `Agent Memory/01 Current/Current State.md`
- Read: `Agent Memory/01 Current/Active Work.md`
- Read: `bridge_app.py`
- Read: `core/webui_server.py`
- Read: `core/runner_supervisor.py`
- Read: `core/process_utils.py`
- Create output directory only: `/private/tmp/nyxsuite-perf-YYYYMMDD-HHMMSS/`

- [ ] **Step 1: Confirm repo and git state**

Run:

```bash
pwd
git status --short
git rev-parse HEAD
```

Expected: working directory is `/Users/heisnberg/Documents/nyxsuite v6`; record whether the tree is dirty. Do not modify or revert unrelated changes.

- [ ] **Step 2: Create a timestamped diagnostic output directory**

Run:

```bash
RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="/private/tmp/nyxsuite-perf-$RUN_ID"
mkdir -p "$OUT"
printf '%s\n' "$OUT"
```

Expected: prints a `/private/tmp/nyxsuite-perf-*` path. Use that same `OUT` for all artifacts.

- [ ] **Step 3: Inventory existing NyxSuite, browser, and AdsPower processes**

Run:

```bash
ps -axo pid,ppid,pcpu,rss,etime,command | rg -i 'bridge_app.py|main.py|nyxify_runner.py|NyxSuite|NyxBot|NyxifyRunner|SunBrowser|AdsPower|Google Chrome|chrome --type=extension|chrome-extension' > "$OUT/process-inventory.txt"
lsof -nP -iTCP:8865 -iTCP:8866 -iTCP:8869 -iTCP:8870 -sTCP:LISTEN > "$OUT/listening-ports.txt" 2>&1 || true
```

Expected: `process-inventory.txt` shows any existing bridge, runner, Chrome, SunBrowser, or AdsPower processes; `listening-ports.txt` shows which local NyxSuite ports are already bound.

- [ ] **Step 4: Record current local endpoint responsiveness if bridge is already running**

Run:

```bash
for url in \
  http://127.0.0.1:8870/ \
  http://127.0.0.1:8870/bridge/status \
  http://127.0.0.1:8865/token \
  http://127.0.0.1:8866/token
do
  printf '\n== %s ==\n' "$url" >> "$OUT/endpoint-baseline.txt"
  curl -sS -o /dev/null -w 'http_code=%{http_code} total=%{time_total} connect=%{time_connect} starttransfer=%{time_starttransfer}\n' "$url" >> "$OUT/endpoint-baseline.txt" 2>&1 || true
done
```

Expected: if the bridge is running, endpoints should answer quickly; if not, connection failures are acceptable and become baseline evidence.

- [ ] **Step 5: Summarize initial suspects without choosing a root cause**

Write `"$OUT/initial-notes.md"` with this structure:

```markdown
# Initial Notes

- Existing bridge process:
- Existing Nyx runner process:
- Existing Nyxify runner process:
- Chrome/extension processes visible:
- AdsPower/SunBrowser processes visible:
- Ports already bound:
- Immediate endpoint delays:
- No root cause chosen yet.
```

Expected: concise facts only, no fixes.

---

### Task 2: Add A Read-Only Process Sampler

**Files:**
- Create temporary script: `/private/tmp/nyxsuite-perf-YYYYMMDD-HHMMSS/sample_processes.py`
- Output: `/private/tmp/nyxsuite-perf-YYYYMMDD-HHMMSS/process-samples.jsonl`

- [ ] **Step 1: Write a sampler using Python stdlib and `ps`**

Create `/private/tmp/nyxsuite-perf-YYYYMMDD-HHMMSS/sample_processes.py` with:

```python
#!/usr/bin/env python3
import json
import subprocess
import sys
import time

PATTERNS = [
    "bridge_app.py",
    "main.py",
    "nyxify_runner.py",
    "NyxSuite",
    "NyxBot",
    "NyxifyRunner",
    "Google Chrome",
    "chrome --type=extension",
    "chrome-extension",
    "SunBrowser",
    "AdsPower",
]

def snapshot():
    result = subprocess.run(
        ["ps", "-axo", "pid=,ppid=,pcpu=,rss=,etime=,command="],
        capture_output=True,
        text=True,
        check=False,
    )
    rows = []
    for line in result.stdout.splitlines():
        parts = line.strip().split(None, 5)
        if len(parts) < 6:
            continue
        pid, ppid, pcpu, rss, etime, command = parts
        if not any(pattern.lower() in command.lower() for pattern in PATTERNS):
            continue
        rows.append({
            "ts": time.time(),
            "pid": int(pid),
            "ppid": int(ppid),
            "pcpu": float(pcpu),
            "rss_kb": int(rss),
            "etime": etime,
            "command": command[:500],
        })
    return rows

def main():
    out_path = sys.argv[1]
    duration = float(sys.argv[2]) if len(sys.argv) > 2 else 120.0
    interval = float(sys.argv[3]) if len(sys.argv) > 3 else 0.5
    deadline = time.monotonic() + duration
    with open(out_path, "a", encoding="utf-8") as handle:
        while time.monotonic() < deadline:
            for row in snapshot():
                handle.write(json.dumps(row, sort_keys=True) + "\n")
            handle.flush()
            time.sleep(interval)

if __name__ == "__main__":
    main()
```

Expected: a sampler that records CPU and RSS for NyxSuite, Chrome extension, AdsPower, and runner processes without reading sensitive app data.

- [ ] **Step 2: Run a short smoke sample**

Run:

```bash
python3 "$OUT/sample_processes.py" "$OUT/process-samples-smoke.jsonl" 3 0.5
wc -l "$OUT/process-samples-smoke.jsonl"
```

Expected: line count greater than zero if any matching process exists. Zero lines is acceptable only if no matching processes are running.

- [ ] **Step 3: Prepare a summarizer for peak CPU/RAM by command**

Create `/private/tmp/nyxsuite-perf-YYYYMMDD-HHMMSS/summarize_processes.py` with:

```python
#!/usr/bin/env python3
import json
import sys
from collections import defaultdict

groups = defaultdict(lambda: {"samples": 0, "peak_cpu": 0.0, "peak_rss_kb": 0, "pids": set()})

def label(command):
    lowered = command.lower()
    if "bridge_app.py" in lowered or "nyxsuite" in lowered:
        return "bridge"
    if "nyxify_runner.py" in lowered or "nyxifyrunner" in lowered:
        return "nyxify_runner"
    if "main.py" in lowered or "nyxbot" in lowered:
        return "nyx_runner"
    if "chrome --type=extension" in lowered or "chrome-extension" in lowered:
        return "chrome_extension"
    if "google chrome" in lowered:
        return "chrome"
    if "sunbrowser" in lowered:
        return "sunbrowser"
    if "adspower" in lowered:
        return "adspower"
    return "other"

for path in sys.argv[1:]:
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            key = label(row.get("command", ""))
            item = groups[key]
            item["samples"] += 1
            item["peak_cpu"] = max(item["peak_cpu"], float(row.get("pcpu") or 0.0))
            item["peak_rss_kb"] = max(item["peak_rss_kb"], int(row.get("rss_kb") or 0))
            item["pids"].add(int(row.get("pid") or 0))

for key, item in sorted(groups.items()):
    print(f"{key}: samples={item['samples']} pids={sorted(item['pids'])} peak_cpu={item['peak_cpu']:.1f}% peak_rss_mb={item['peak_rss_kb']/1024:.1f}")
```

Expected: this groups process evidence into bridge, runner, Chrome, extension, SunBrowser, and AdsPower buckets.

---

### Task 3: Measure Bridge Startup And Toggle Delay

**Files:**
- Read: `bridge_app.py`
- Read: `core/timing_diag.py`
- Read: `core/webui_server.py`
- Runtime logs: app data logs, usually `/Users/heisnberg/Library/Application Support/NyxSuite/logs/` on macOS frozen/source app-data mode.
- Output: `$OUT/bridge-startup-*`

- [ ] **Step 1: Stop only duplicate test bridge processes if needed**

Before stopping anything, list candidates:

```bash
ps -axo pid,ppid,pcpu,rss,etime,command | rg -i 'bridge_app.py|NyxSuite'
```

Expected: identify whether a bridge is already running. If it is the user's active bridge, ask before stopping it. Do not kill Chrome, AdsPower, or runner processes unless explicitly required for this measurement.

- [ ] **Step 2: Start the bridge with timing diagnostics enabled**

Preferred source-run command:

```bash
NYXSUITE_TIMING=1 NYXSUITE_NO_OPEN=1 ./.venv/bin/python bridge_app.py > "$OUT/bridge-startup-stdout.log" 2> "$OUT/bridge-startup-stderr.log" &
BRIDGE_PID=$!
printf '%s\n' "$BRIDGE_PID" > "$OUT/bridge.pid"
```

If `.venv/bin/python` does not exist, use `python3 bridge_app.py` and record that fallback.

Expected: bridge starts without auto-opening the dashboard. Timing logs should eventually include `bridge.build`, `bridge.start_servers`, and `bridge.startup`.

- [ ] **Step 3: Sample processes during bridge startup**

Run immediately after starting:

```bash
python3 "$OUT/sample_processes.py" "$OUT/process-samples-bridge-startup.jsonl" 45 0.25
```

Expected: process samples cover the whole startup window.

- [ ] **Step 4: Poll bridge readiness**

Run in a second shell while the sampler is running:

```bash
for i in $(seq 1 60); do
  date -u '+%Y-%m-%dT%H:%M:%SZ' >> "$OUT/bridge-readiness.txt"
  curl -sS -o /dev/null -w 'status total=%{time_total} code=%{http_code}\n' 'http://127.0.0.1:8870/bridge/status' >> "$OUT/bridge-readiness.txt" 2>&1 || true
  sleep 0.5
done
```

Expected: the first successful `http_code=200` marks dashboard readiness. Any multi-second gap before first success must be correlated with timing logs and process samples.

- [ ] **Step 5: Measure bridge toggle from extension popup path**

Use the live browser if available. Open the Nyx or Nyxify extension popup and click the NyxSuite bridge toggle OFF then ON. While doing that, run:

```bash
python3 "$OUT/sample_processes.py" "$OUT/process-samples-bridge-toggle.jsonl" 90 0.25
```

Expected: capture CPU/RAM during the exact user-visible delay. Record the manual timestamps in `$OUT/live-run-notes.md`:

```markdown
# Live Run Notes

- Browser used:
- Extension used: Nyx / Nyxify
- Toggle OFF clicked at UTC:
- Toggle OFF visual state changed at UTC:
- Toggle ON clicked at UTC:
- Toggle ON visual state changed at UTC:
- Delay observed:
- Visible symptom:
```

- [ ] **Step 6: Summarize startup/toggle process peaks**

Run:

```bash
python3 "$OUT/summarize_processes.py" "$OUT/process-samples-bridge-startup.jsonl" "$OUT/process-samples-bridge-toggle.jsonl" > "$OUT/process-summary-bridge.txt"
cat "$OUT/process-summary-bridge.txt"
```

Expected: identify whether bridge, Chrome extension, Chrome browser, AdsPower, SunBrowser, or runner process peaks during bridge start/toggle.

---

### Task 4: Measure Popup Open Delay In The Live Browser

**Files:**
- Read: `nyx_extension/popup.js`
- Read: `nyxify_extension/popup.js`
- Read: `nyx_extension/background.js`
- Read: `nyxify_extension/background.js`
- Output: `$OUT/popup-*`

- [ ] **Step 1: Enable popup console timing for Nyx popup**

Open the Nyx extension popup in the user's browser. If DevTools can attach to the popup, run:

```javascript
window.__NYX_TIMING__ = true;
```

Expected: Nyx popup console logs lines like `[nyx-timing] <label> | <ms> ms` if the code path calls `diagTiming`. If no timing appears, record that current Nyx popup timing hooks are insufficient.

- [ ] **Step 2: Measure Nyx popup open with process sampling**

Run the sampler:

```bash
python3 "$OUT/sample_processes.py" "$OUT/process-samples-nyx-popup.jsonl" 60 0.25
```

While it runs, open and close the Nyx extension popup at least five times. Record manual timestamps:

```markdown
# Nyx Popup Measurements

| Attempt | Open clicked UTC | First visible popup UTC | Usable controls UTC | Notes |
|---|---|---|---|---|
| 1 |  |  |  |  |
| 2 |  |  |  |  |
| 3 |  |  |  |  |
| 4 |  |  |  |  |
| 5 |  |  |  |  |
```

Expected: enough repeated samples to separate one-off cold startup from consistent slow first paint.

- [ ] **Step 3: Measure Nyxify popup open with process sampling**

Run:

```bash
python3 "$OUT/sample_processes.py" "$OUT/process-samples-nyxify-popup.jsonl" 60 0.25
```

While it runs, open and close the Nyxify extension popup at least five times. Record:

```markdown
# Nyxify Popup Measurements

| Attempt | Open clicked UTC | First visible popup UTC | Usable controls UTC | Notes |
|---|---|---|---|---|
| 1 |  |  |  |  |
| 2 |  |  |  |  |
| 3 |  |  |  |  |
| 4 |  |  |  |  |
| 5 |  |  |  |  |
```

Expected: direct comparison of Nyx vs Nyxify popup delay.

- [ ] **Step 4: Measure popup local API calls from terminal**

With bridge running, run:

```bash
for i in $(seq 1 20); do
  printf 'bridge/status attempt=%s ' "$i" >> "$OUT/popup-endpoint-timing.txt"
  curl -sS -o /dev/null -w 'code=%{http_code} total=%{time_total} starttransfer=%{time_starttransfer}\n' 'http://127.0.0.1:8870/bridge/status' >> "$OUT/popup-endpoint-timing.txt" 2>&1 || true
done
```

Expected: if terminal endpoint calls are fast while popup is slow, root cause is likely extension/browser UI, Chrome storage, service worker startup, or popup rendering. If endpoint calls are also slow, root cause is likely bridge status snapshot, controller status, AdsPower/CDP scan, SQLite/task-store read, or process checks.

- [ ] **Step 5: Summarize popup process peaks**

Run:

```bash
python3 "$OUT/summarize_processes.py" "$OUT/process-samples-nyx-popup.jsonl" "$OUT/process-samples-nyxify-popup.jsonl" > "$OUT/process-summary-popup.txt"
cat "$OUT/process-summary-popup.txt"
```

Expected: identify whether popup open correlates with Chrome extension CPU/RAM, bridge CPU, AdsPower/SunBrowser scanning, or broad system pressure.

---

### Task 5: Correlate Bridge Timing Logs With Status Snapshot Cost

**Files:**
- Read: `core/webui_server.py`
- Read: `core/nyx_controller.py`
- Read: `core/nyxify_controller.py`
- Read: `core/adspower_live.py`
- Output: `$OUT/timing-analysis.md`

- [ ] **Step 1: Gather timing log lines**

Run:

```bash
rg -n 'timing \\|' "$OUT" "/Users/heisnberg/Library/Application Support/NyxSuite/logs" > "$OUT/timing-lines.txt" 2>&1 || true
```

Expected: lines for `bridge.build`, `bridge.start_servers`, `bridge.startup`, `dashboard.status`, `watch.snapshot`, and `action.dispatch` if `NYXSUITE_TIMING=1` was active.

- [ ] **Step 2: Classify slow timing entries**

Create `$OUT/timing-analysis.md`:

```markdown
# Timing Analysis

## Slow Entries Over 250 ms

| Label | Product/Extra | Elapsed ms | Source log | Timestamp nearby |
|---|---|---:|---|---|

## Interpretation

- If `bridge.startup` is slow but `bridge.build` and `bridge.start_servers` are fast:
- If `bridge.build` is slow:
- If `bridge.start_servers` is slow:
- If `dashboard.status` is slow:
- If `watch.snapshot nyx` is slow:
- If `watch.snapshot nyxify` is slow:
- If `action.dispatch` is slow when pressing Start:
```

Expected: timing analysis connects the user-visible delay to one bridge layer, or states that existing timing is insufficient.

- [ ] **Step 3: Test whether AdsPower live CDP scan is implicated**

Run endpoint timing under two conditions:

Condition A, default:

```bash
for i in $(seq 1 20); do
  curl -sS -o /dev/null -w 'default code=%{http_code} total=%{time_total}\n' 'http://127.0.0.1:8870/bridge/status' >> "$OUT/adspower-live-comparison.txt" 2>&1 || true
done
```

Condition B, increase cache interval before bridge start:

```bash
ADSPOWER_LIVE_REFRESH_INTERVAL_SECONDS=30 NYXSUITE_TIMING=1 NYXSUITE_NO_OPEN=1 ./.venv/bin/python bridge_app.py
```

Then repeat the 20 curl calls and write them to the same comparison file.

Expected: if long status delays disappear when `ADSPOWER_LIVE_REFRESH_INTERVAL_SECONDS=30`, the CDP live-open profile scan in `core/adspower_live.py` is a strong suspect. If no change, continue investigating status store reads and process checks.

---

### Task 6: Form And Test One Root-Cause Hypothesis

**Files:**
- Output: `$OUT/root-cause-hypothesis.md`

- [ ] **Step 1: Fill the evidence matrix**

Write:

```markdown
# Root Cause Hypothesis

## Evidence Matrix

| Symptom | User-visible delay | Highest CPU process | Highest RAM growth | Slow timing label | Slow endpoint | Browser evidence |
|---|---:|---|---|---|---|---|
| Bridge cold start |  |  |  |  |  |  |
| Bridge popup toggle ON |  |  |  |  |  |  |
| Nyx popup open |  |  |  |  |  |  |
| Nyxify popup open |  |  |  |  |  |  |

## Single Hypothesis

I think the root cause is:

Because:

## Minimal Test

Change or condition tested:

Expected result if hypothesis is true:

Observed result:

## Verdict

Confirmed / rejected / insufficient evidence:
```

Expected: only one root-cause hypothesis is selected after evidence is filled.

- [ ] **Step 2: Choose exactly one minimal test**

Use one of these based on the evidence:

- If bridge/status endpoint is slow: temporarily add narrower timing around `NyxController.status_snapshot()`, `NyxifyController.status_snapshot()`, `store.list_tasks()`, `annotate_rows_with_open_state()`, and `runner.resolve_pid()`.
- If only popup UI is slow: add first-paint timing to `nyxify_extension/popup.js` matching the Nyx popup diagnostic style, and test whether cached first paint is missing for Nyxify.
- If bridge start is slow before server readiness: add timing around imports/build/start server phases in `bridge_app.py`.
- If CPU peaks in Chrome extension process: inspect background service worker logs and storage/message handlers in `nyx_extension/background.js` or `nyxify_extension/background.js`.
- If CPU/RAM peaks in AdsPower/SunBrowser: test with AdsPower closed, then with AdsPower open but no profiles, then with current profiles.

Expected: exactly one variable changes for the minimal test.

- [ ] **Step 3: Re-run only the matching scenario**

Run the same sampler and endpoint/browser measurement for the affected scenario only. Save new files with `-hypothesis-test` suffix.

Expected: the change either materially reduces the delay or does not. Do not stack additional fixes.

---

### Task 7: If A Fix Is Needed, Add Focused Tests Before The Fix

**Files:**
- Modify only after root cause is confirmed.
- Candidate tests:
  - `tests/test_popup_first_paint.py`
  - `tests/test_controller_fast_ack.py`
  - `tests/test_runner_fast_state.py`
  - `tests/test_process_utils_timeouts.py`
  - `tests/test_bridge_duplicate_open.py`
  - Add `tests/test_nyxify_popup_first_paint.py` only if Nyxify popup lacks cached first paint.

- [ ] **Step 1: Select the smallest regression test**

Examples:

- For slow Nyxify popup first paint, add a source test asserting Nyxify popup stores and renders last-known runner status before live refresh.
- For slow bridge action ACK, extend `tests/test_controller_fast_ack.py` to prove action responses use `light_status()` and do not build full row annotations.
- For slow process checks, extend `tests/test_process_utils_timeouts.py` or `tests/test_runner_fast_state.py` to prove bounded/cached process checks.
- For slow `adspower_live` refresh, add a unit test proving live CDP refresh is backgrounded and cached.

Expected: the test fails before the fix or proves the specific performance guard is present.

- [ ] **Step 2: Run the focused failing/passing test**

Run the exact test selected:

```bash
./.venv/bin/python -m pytest tests/test_popup_first_paint.py -q
```

or:

```bash
./.venv/bin/python -m pytest tests/test_controller_fast_ack.py tests/test_runner_fast_state.py tests/test_process_utils_timeouts.py -q
```

Expected: record exact result in `$OUT/test-results.txt`.

- [ ] **Step 3: Implement only the root-cause fix**

Allowed fix examples, depending on confirmed evidence:

- Add cached first paint to `nyxify_extension/popup.js`.
- Move expensive status work out of action ACKs.
- Cache or background a slow process/CDP lookup.
- Reduce popup startup storage/message work.
- Add timing around the confirmed slow boundary for future safe diagnostics.

Expected: no unrelated refactors.

- [ ] **Step 4: Re-run focused tests and affected live measurement**

Run:

```bash
./.venv/bin/python -m pytest tests/test_popup_first_paint.py tests/test_controller_fast_ack.py tests/test_runner_fast_state.py tests/test_process_utils_timeouts.py tests/test_bridge_duplicate_open.py tests/test_nyxify_bridge_waits.py -q
```

Then repeat the affected live scenario from Task 3 or Task 4.

Expected: tests pass and measured delay improves. If not, revert only the attempted fix and return to Task 6 with a new hypothesis.

---

### Task 8: Produce The Final Root-Cause Report

**Files:**
- Create: `$OUT/final-root-cause-report.md`
- Optional stable memory update after user approval only.

- [ ] **Step 1: Write final report**

Use:

```markdown
# NyxSuite Performance Root Cause Report

## Summary

- Main CPU consumer during reproduction:
- Main RAM consumer during reproduction:
- Slow symptom reproduced: yes/no
- Root cause confirmed: yes/no
- Confirmed root cause:

## Evidence

- Process sample files:
- Timing log files:
- Endpoint timing files:
- Browser/live run notes:
- Test results:

## What Was Not The Cause

- Component:
- Evidence:

## Fix Recommendation

- Minimal fix:
- Files to change:
- Regression test:
- Verification command:
- Live browser verification:

## Remaining Unknowns

- Unknown:
- How to resolve:
```

Expected: report is evidence-first and names the real cause, or honestly states that evidence is insufficient.

- [ ] **Step 2: Share concise user-facing summary**

Tell the user:

- Which component eats the most CPU/RAM.
- Whether the slow popup is caused by bridge, extension popup, browser, AdsPower/CDP, or system load.
- The exact measured delay before/after if a fix was implemented.
- Which tests and live browser checks were run.
- Whether Agent Memory should be updated with stable facts.

Expected: English only, direct and practical.

---

## Likely Suspect Map To Validate, Not Assume

- Slow `/bridge/status` or `watch.snapshot`: inspect `core/nyx_controller.py`, `core/nyxify_controller.py`, `core/adspower_live.py`, and store reads.
- Slow Start button but fast status: inspect `core/runner_supervisor.py` and `core/process_utils.py` process scans/spawn path.
- Slow bridge cold start: inspect imports/build/server binding in `bridge_app.py`.
- Slow popup with fast endpoints: inspect `nyx_extension/popup.js`, `nyxify_extension/popup.js`, background service worker startup, Chrome storage calls, and missing cached first-paint behavior.
- High CPU/RAM in Chrome/SunBrowser/AdsPower only: NyxSuite bridge may be waiting on browser/AdsPower state rather than causing the load.

## Completion Criteria

- At least one live browser reproduction is performed, unless browser control is unavailable and the user is told why.
- CPU/RAM samples exist for bridge startup, bridge toggle, Nyx popup, and Nyxify popup.
- Endpoint timings exist for `/bridge/status`.
- Timing logs are reviewed or the report states why timing logs were unavailable.
- Root cause is supported by synchronized process, timing, endpoint, and browser evidence.
- No fix is proposed without naming the measured failing boundary.
