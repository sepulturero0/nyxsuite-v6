"""Redacted timing diagnostics for the bridge.

Every diagnostic reducer logs a flat, single-line record in the form::

    timing | <label> | <elapsed_ms> ms | <extra>

where ``<extra>`` is an optional, pre-sanitised tag. No phone numbers, email
addresses, one-time codes, usernames, passwords, or AdsPower credentials are
ever logged — callers must pass only an allow-listed state token (e.g. a step
name or a product name). This keeps the timing output safe to share when
investigating the "popup takes N seconds" / "Start feels slow" class of issues.

The diagnostics are cheap (one :func:`time.monotonic` sample and one log line)
and are gated off by default so they add no noise or cost to production runs
unless ``NYXSUITE_TIMING=1`` is set (see :func:`enabled`).
"""

import os
import re
import time

from core.logger import logger

# Tokens we are allowed to pass through in the ``extra`` field. Anything not on
# this safe list (phone, email, code, username, path, etc.) is dropped. This is
# the redaction boundary: callers may only use these labels.
_ALLOWED_EXTRA = {
    "nyx", "nyxify", "bridge", "dashboard", "popup",
    "start", "stop", "restart", "pause", "resume",
    "starting", "stopping", "running", "stopped", "blocked", "paused", "waiting",
    "sse_snapshot", "sse_update", "watch_loop", "status",
    "snapboard", "sms", "otp", "code", "retry", "healthy",
}

# Drop any free-form extra that contains anything not strictly ASCII word
# characters, spaces, dashes, or periods — makes the tag injection-proof.
_SAFE_TAG_RE = re.compile(r"[^A-Za-z0-9 _.\-]")


def enabled() -> bool:
    """Timing diagnostics are opt-in via ``NYXSUITE_TIMING=1``."""
    return str(os.getenv("NYXSUITE_TIMING", "")).strip().lower() in {"1", "true", "yes", "on"}


def now() -> float:
    """Start-of-measurement marker (monotonic seconds)."""
    return time.monotonic()


def _sanitize_extra(extra: str) -> str:
    tag = str(extra or "").strip()
    if not tag:
        return ""
    # Redaction boundary: only allow clamps we explicitly listed.
    words = [w for w in tag.split() if w in _ALLOWED_EXTRA]
    if not words:
        return ""
    return " ".join(words)


def log_timing(label: str, start: float, extra: str = "") -> None:
    """Log the elapsed milliseconds for ``label`` since ``start`` (from now()).

    No-op unless :func:`enabled` returns true. ``extra`` is sanitised so a
    misbehaving caller cannot leak sensitive data into the log.
    """
    if not enabled():
        return
    elapsed_ms = (time.monotonic() - start) * 1000.0
    tag = _sanitize_extra(extra)
    if tag:
        logger.info("timing | %s | %.1f ms | %s", label, elapsed_ms, tag)
    else:
        logger.info("timing | %s | %.1f ms", label, elapsed_ms)


class elapsed:
    """Context manager that logs timing for a scope on exit.

    Usage::

        with elapsed("bridge.build"):
            ...
    """

    def __init__(self, label: str, extra: str = ""):
        self.label = label
        self.extra = extra
        self._start = None

    def __enter__(self):
        self._start = now()
        return self

    def __exit__(self, _exc_type, _exc, _tb):
        log_timing(self.label, self._start, self.extra)
        return False
