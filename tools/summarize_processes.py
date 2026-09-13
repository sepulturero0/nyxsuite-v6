#!/usr/bin/env python3
"""Summarize NyxSuite process-samples JSONL into peak CPU/RSS per component.

Reads one or more JSONL files where each line has at least:
  command, pcpu (or cpu_pct), rss_kb (or rss_mb)

Usage:
  python tools/summarize_processes.py <file.jsonl> [more.jsonl ...]
"""
import json
import sys
from collections import defaultdict


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
    if "google chrome" in lowered or "chrome.exe" in lowered or "msedge.exe" in lowered:
        return "chrome"
    if "sunbrowser" in lowered:
        return "sunbrowser"
    if "adspower" in lowered:
        return "adspower"
    return "other"


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    groups = defaultdict(lambda: {"samples": 0, "peak_cpu": 0.0, "peak_rss_kb": 0, "pids": set()})
    for path in sys.argv[1:]:
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                row = json.loads(line)
                key = label(str(row.get("command", "")))
                item = groups[key]
                item["samples"] += 1
                cpu = row.get("pcpu")
                if cpu is None:
                    cpu = row.get("cpu_pct")
                rss_kb = row.get("rss_kb")
                if rss_kb is None:
                    rss_kb = float(row.get("rss_mb") or 0.0) * 1024.0
                item["peak_cpu"] = max(item["peak_cpu"], float(cpu or 0.0))
                item["peak_rss_kb"] = max(item["peak_rss_kb"], int(rss_kb or 0))
                item["pids"].add(int(row.get("pid") or 0))

    for key, item in sorted(groups.items()):
        print(
            f"{key}: samples={item['samples']} pids={sorted(item['pids'])} "
            f"peak_cpu={item['peak_cpu']:.1f}% peak_rss_mb={item['peak_rss_kb'] / 1024:.1f}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
