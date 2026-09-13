# NyxSuite Windows Performance Probe (v2)

Read-only, one-shot evidence collector for the NyxSuite Windows performance
investigation. It measures environment/launcher stage costs, bridge toggle
OFF/ON timing, endpoint latency, and CPU/RAM per component — and captures popup
open times interactively so you only run it once.

It does **not** change application code and does not log secrets, tokens, emails,
phone numbers, or OTPs.

## Contents

- `windows_perf_probe.ps1` — the v2 probe (PowerShell 5.1+).
- `summarize_processes.py` — turns `process-samples.jsonl` into peak CPU/RAM.
- `WINDOWS_PERF_PROBE_README.md` — this file.

## Before you run

1. Start the NyxSuite bridge normally and wait for the dashboard
   (`http://127.0.0.1:8870/` returns 200).
2. Load both extensions (Nyx and Nyxify).
3. Open Chrome/Edge with at least one SnapBoard tab.
4. Let AdsPower/SunBrowser run if you normally use them.

## Run

From the folder where you extracted this zip:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\windows_perf_probe.ps1 -Mode all -Seconds 60
```

The install root is auto-detected from the running bridge. If detection fails,
pass it explicitly (the folder with `bridge_app.py` + `portable_launch_nyx.ps1`):

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\windows_perf_probe.ps1 -Mode all -Seconds 60 `
  -InstallRoot "C:\path\to\NyxSuite"
```

### Modes

| Mode | What it does |
|---|---|
| `env` | install root, venv/python, PowerShell startup, the 3 launcher probes (`import sys`, deps, Playwright Chromium) |
| `endpoints` | 20 timed calls each to `8870/bridge/status`, `8865/status`, `8866/status` |
| `toggle` | 3 OFF/ON cycles: shutdown response, port-down, dashboard-ready |
| `sample` | fast CPU/RAM sampling (honors `-IntervalMs`) |
| `all` | env, endpoints, toggle, sample, then interactive popup capture |

`-Cycles N` controls toggle repetitions. `-NoPrompt` skips the popup questions.

## During the sample window

The probe prints `>>> OPEN the Nyxify popup 5x and the Nyx popup 5x NOW`. Click
each extension icon open/close and time **open → usable controls** (a phone
stopwatch is fine). When sampling finishes, the probe prompts you to type the
numbers (comma-separated) and writes `popup-timings.md`.

Optional (Nyx popup only): right-click inside the popup → Inspect, run
`window.__NYX_TIMING__ = true`, reopen; it logs `[nyx-timing] label | ms`.

## Output

`%TEMP%\nyxsuite-perf-<utcstamp>\`:

- `env-report.txt` — install root, version, venv, launcher probe timings
- `launcher-overhead.txt` — end-to-end launcher fast-path time (no-op entry), isolating launcher cost from bridge startup
- `endpoint-timings.txt` — per-call + min/median/max
- `toggle-timing.txt` — OFF response, port-down, ON dashboard-ready (×cycles)
- `bridge-internal-timing.txt` — bridge's own `timing | ...` lines (`bridge.build` / `bridge.start_servers` / `bridge.startup`) captured with `NYXSUITE_TIMING=1`
- `process-samples.jsonl` — CPU/RSS samples
- `popup-timings.md` — your popup numbers (+ medians if entered)
- `chrome-task-manager-snapshot.txt` — top-CPU entry you entered
- `probe-notes.txt`, `sample-notes.txt`

Split the ON delay with:
`bridge_startup ≈ ON dashboard_ready_ms − launcher_overhead_ms − (pythonw interpreter start)`,
where `bridge.startup` from `bridge-internal-timing.txt` is the bridge-side number.

Summarize process peaks:

```powershell
python .\summarize_processes.py "$env:TEMP\nyxsuite-perf-<stamp>\process-samples.jsonl"
```

If `python` is not on PATH, use the bridge venv, e.g.
`<install root>\venv\Scripts\python.exe`.

## Notes

- Windows does not use the macOS `launchd` path, so the macOS `kickstart -k`
  fix does not explain any Windows behavior. Do not assume a shared cause.
- The v1 run showed a ~11.4 s toggle-ON delay and a ~2.0 s port-down after OFF.
  `env-report.txt` is designed to show whether the delay is in the launcher's
  readiness probes or in the bridge itself.
- Share only the generated `*.txt` / `*.jsonl` / `*.md` files.
