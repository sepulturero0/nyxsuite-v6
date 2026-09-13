# Windows Live Performance Investigation

**Scope:** Windows only. The macOS fix (removing the immediate `launchctl kickstart -k` in
`agent_host/host_main.py`) does **not** apply to Windows and must not be assumed to explain any
Windows behavior. Collect evidence first; report a root cause only if the evidence supports it.

**Do not:** change code, stop the bridge by force, or delete logs during collection.

## What to measure

1. NyxSuite bridge toggle OFF/ON time.
2. Nyx popup open time.
3. Nyxify popup open time.
4. CPU/RAM peaks for `bridge_app`, the Chrome extension process, AdsPower/SunBrowser, the Nyx
   runner (`main.py`), and the Nyxify runner (`nyxify_runner.py`).
5. Endpoint timings:
   - `http://127.0.0.1:8870/bridge/status`
   - `http://127.0.0.1:8865/status`
   - `http://127.0.0.1:8866/status`

## Prerequisites

- Windows machine with the NyxSuite bridge installed and the Nyx + Nyxify extensions loaded.
- Chrome/Edge open with at least one SnapBoard tab (so the popups have real data).
- PowerShell 5.1+ (the built-in Windows PowerShell is fine).

## Step 1 — Automated collection

From the NyxSuite install root (the folder containing `bridge_app.py` and
`portable_launch_nyx.ps1`), run:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File tools\windows_perf_probe.ps1 -Mode all -Seconds 90
```

If `tools\windows_perf_probe.ps1` is not beside `bridge_app.py` (source checkout layout), run it
from the repo and pass the install root explicitly:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File "C:\path\to\repo\tools\windows_perf_probe.ps1" `
  -Mode all -Seconds 90 -InstallRoot "C:\path\to\NyxSuite\app"
```

Modes:

| Mode | What it does |
|---|---|
| `endpoints` | 20 timed calls to each of the three endpoints |
| `toggle` | times `/bridge/shutdown` OFF, then launcher ON, to dashboard-ready |
| `sample` | samples CPU/RAM every 250 ms for `-Seconds` |
| `all` | endpoints, then toggle, then sample |

Output goes to `%TEMP%\nyxsuite-perf-<utcstamp>\`:
`endpoint-timings.txt`, `toggle-timing.txt`, `process-samples.jsonl`, `probe-notes.txt`.

## Step 2 — Manual popup-open measurement

The script cannot open extension popups. While `-Mode sample` (or the sample phase of `all`) is
running, click the extension icon, close it, and repeat. Record wall-clock UTC for each open:

```markdown
# Windows Popup Measurements

| Attempt | Extension | Icon clicked UTC | First visible UTC | Usable controls UTC | Notes |
|---|---|---|---|---|---|
| 1 | Nyxify |  |  |  |  |
| 2 | Nyxify |  |  |  |  |
| 3 | Nyxify |  |  |  |  |
| 4 | Nyxify |  |  |  |  |
| 5 | Nyxify |  |  |  |  |
| 1 | Nyx |  |  |  |  |
| 2 | Nyx |  |  |  |  |
| 3 | Nyx |  |  |  |  |
| 4 | Nyx |  |  |  |  |
| 5 | Nyx |  |  |  |  |
```

Optional (Nyx popup only): open the popup, right-click inside it, choose **Inspect**, then in the
popup console run `window.__NYX_TIMING__ = true` and reopen; it logs `[nyx-timing] label | ms`.
The Nyxify popup has no timing hook, so its open time is manual.

After the sample finishes, summarize the process peaks:

```powershell
python "C:\path\to\repo\tools\summarize_processes.py" "%TEMP%\nyxsuite-perf-<stamp>\process-samples.jsonl"
```

`process-samples.jsonl` includes both `pcpu`/`rss_kb` and `cpu_pct`/`rss_mb`, so
`tools\summarize_processes.py` parses it directly.

## Step 3 — Toggle measurement

`-Mode toggle` (included in `all`) reports:

- OFF: `/bridge/shutdown` response time and time until `:8870` stops answering.
- ON: time from launching `bridge_app.py` (via `portable_launch_nyx.ps1`, the same launcher the
  native host uses) until `:8870` answers again.

Also toggle manually from the popup once and note the same two phases, so the scripted and
popup-driven paths can be compared.

## Step 4 — Report

Fill this in from the collected files. Do not speculate; state "insufficient evidence" where a
metric is missing.

```markdown
# Windows NyxSuite Performance Report

## Summary
- Bridge toggle OFF time:
- Bridge toggle ON time (scripted):
- Bridge toggle ON time (popup):
- Nyx popup open time (median of 5):
- Nyxify popup open time (median of 5):

## CPU/RAM peaks
| Component | Peak CPU % | Peak RSS MB |
|---|---:|---:|
| bridge_app |  |  |
| Chrome extension process |  |  |
| Chrome browser |  |  |
| AdsPower |  |  |
| SunBrowser |  |  |
| Nyx runner (main.py) |  |  |
| Nyxify runner (nyxify_runner.py) |  |  |

## Endpoint timings
| Endpoint | min ms | median ms | max ms | payload |
|---|---:|---:|---:|---:|
| 8870/bridge/status |  |  |  |  |
| 8865/status |  |  |  |  |
| 8866/status |  |  |  |  |

## Windows root cause
- Is the Windows delay explained by the macOS cause (launchd kickstart)? launcher is
  `portable_launch_nyx.ps1`, not launchd, so this must be verified independently:
- Confirmed Windows root cause (or "insufficient evidence"):
- Evidence:
```

## Notes on the Windows start path (context, not a conclusion)

On Windows, `host_main._start_agent()` runs
`powershell -File portable_launch_nyx.ps1 -EntryScript bridge_app.py -Quiet`. There is no launchd
and no `kickstart`; the macOS 10 s throttle cannot occur here. The Windows delay, if any, likely
lives in the portable launcher (setup/venv checks, process startup) or in process/endpoint work.
Confirm with the measurements above before naming a cause.
