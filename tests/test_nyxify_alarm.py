from datetime import datetime, timedelta, timezone

from core.nyxify_alarm import NyxifyAlarmTracker, detect_nyxify_alarm_incidents


NOW = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)


def task(status="RUNNING", updated_at=None, **overrides):
    row = {
        "id": 1,
        "row_key": "row-1",
        "status": status,
        "last_step": "running_signup",
        "error": "",
        "updated_at": (updated_at or NOW).isoformat(),
    }
    row.update(overrides)
    return row


def test_failed_task_creates_one_failure_incident():
    incidents = detect_nyxify_alarm_incidents(
        [task(status="FAILED", error="Popup could not be controlled.")],
        now=NOW,
    )

    assert len(incidents) == 1
    assert incidents[0]["kind"] == "failure"
    assert incidents[0]["reason"] == "Popup could not be controlled."


def test_running_task_alarms_only_after_five_minutes_without_updates():
    recent = task(updated_at=NOW - timedelta(minutes=4, seconds=59))
    stale = task(updated_at=NOW - timedelta(minutes=5), last_step="awaiting_otp")

    assert detect_nyxify_alarm_incidents([recent], now=NOW) == []
    incidents = detect_nyxify_alarm_incidents([stale], now=NOW)
    assert len(incidents) == 1
    assert incidents[0]["kind"] == "stuck"
    assert "awaiting_otp" in incidents[0]["reason"]


def test_alarm_tracker_deduplicates_until_incident_clears():
    tracker = NyxifyAlarmTracker()
    failed = task(status="FAILED", error="Failed.")

    assert len(tracker.observe([failed], now=NOW)) == 1
    assert tracker.observe([failed], now=NOW) == []
    assert tracker.observe([], now=NOW) == []
    assert len(tracker.observe([failed], now=NOW)) == 1
