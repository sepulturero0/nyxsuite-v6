"""Nyxify failure/stall alarm detection and platform sound playback."""

from datetime import datetime, timezone
import subprocess
import sys


DEFAULT_STUCK_AFTER_SECONDS = 300
ALARM_REPEAT_SECONDS = 8.0


def _parse_updated_at(value):
    try:
        parsed = datetime.fromisoformat(str(value or "").strip().replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def detect_nyxify_alarm_incidents(tasks, now=None, stuck_after_seconds=DEFAULT_STUCK_AFTER_SECONDS):
    """Return active Nyxify failure/stall incidents from task rows."""
    current_time = now or datetime.now(timezone.utc)
    incidents = []
    for row in tasks or []:
        status = str(row.get("status") or "").strip().upper()
        row_key = str(row.get("row_key") or row.get("id") or "").strip()
        if not row_key:
            continue
        last_step = str(row.get("last_step") or "").strip()
        error = str(row.get("error") or "").strip()
        updated_at = str(row.get("updated_at") or "").strip()

        if status == "FAILED":
            reason = error or last_step or "Nyxify task failed."
            incidents.append({
                "key": f"failed:{row_key}:{updated_at}:{last_step}:{error}",
                "kind": "failure",
                "row_key": row_key,
                "task_id": row.get("id"),
                "last_step": last_step,
                "reason": reason,
            })
            continue

        if status != "RUNNING":
            continue

        updated = _parse_updated_at(updated_at)
        if updated is None:
            continue
        age_seconds = max(0.0, (current_time - updated).total_seconds())
        if age_seconds < float(stuck_after_seconds):
            continue
        incidents.append({
            "key": f"stuck:{row_key}:{updated_at}:{last_step}",
            "kind": "stuck",
            "row_key": row_key,
            "task_id": row.get("id"),
            "last_step": last_step,
            "reason": f"No progress for {int(age_seconds // 60)} minutes ({last_step or 'unknown step'}).",
        })
    return incidents


class NyxifyAlarmTracker:
    """Deduplicate an incident until its task recovers or changes state."""

    def __init__(self):
        self._active = {}

    def observe(self, tasks, now=None, stuck_after_seconds=DEFAULT_STUCK_AFTER_SECONDS):
        incidents = detect_nyxify_alarm_incidents(tasks, now, stuck_after_seconds)
        current = {incident["key"]: incident for incident in incidents}
        fresh = [incident for incident in incidents if incident["key"] not in self._active]
        self._active = current
        return fresh

    def clear(self):
        self._active.clear()

    def active_incidents(self):
        return list(self._active.values())


def play_alarm_sound():
    """Play an audible warning pattern without showing UI."""
    try:
        if sys.platform.startswith("win"):
            import winsound

            # MessageBeep is too easy to miss because it is just a normal
            # Windows notification. Use a short descending warning pattern.
            for frequency, duration in ((880, 220), (660, 220), (880, 220), (660, 420)):
                winsound.Beep(frequency, duration)
            return
        if sys.platform == "darwin":
            subprocess.Popen(
                ["afplay", "/System/Library/Sounds/Basso.aiff"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return
        print("\a\a\a", end="", flush=True)
    except Exception:
        try:
            print("\a\a\a", end="", flush=True)
        except Exception:
            pass
