# Snapchat Verification Workflow Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans or inline execution with TDD. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Nyxify's Snapchat verification phase recover from SnapBoard no-pending/mismatch/timeout states without spam-clicking, stale OTP checks, or long timer loops.

**Architecture:** Treat every submitted email or phone as an explicit verification attempt. The runner stores the exact submitted value for OTP/SMS lookup, the extension checks only that matching row value, and every refresh/retry shares the same attempt deadline.

**Tech Stack:** Python async runner and signup flow, SQLite task store/local API, Chrome extension JavaScript bridge, unittest.

---

### Task 1: Attempt-scoped OTP requests

**Files:**
- Modify: `core/nyxify_task_store.py`
- Modify: `nyxify_runner.py`
- Test: `tests/test_nyxify_bridge_waits.py`

- [ ] Add failing tests proving OTP requests include the exact submitted email and that refresh retry does not reset the full OTP timer.
- [ ] Update `request_otp_for_row` to accept an optional `email` and persist it for the pending request.
- [ ] Update runner OTP fetch to pass the current submitted email.
- [ ] Keep existing callers compatible when no email is supplied.

### Task 2: Bounded SnapBoard email/phone retries

**Files:**
- Modify: `nyxify_runner.py`
- Test: `tests/test_nyxify_bridge_waits.py`

- [ ] Add failing tests proving an email/phone fetch refresh cannot extend past the original deadline.
- [ ] Add failing tests proving no-pending errors return quickly after SnapBoard reports terminal empty.
- [ ] Update `_request_snapboard_value` so refresh/requeue preserves the original deadline and returns terminal errors without re-looping.

### Task 3: Email/phone submit validation

**Files:**
- Modify: `core/signup_flow.py`
- Test: `tests/test_signup_blockers.py`

- [ ] Add failing tests proving email and phone submit helpers return `False` when typing fails, submit stays disabled, or click fails.
- [ ] Update helpers to check type result, visible value, enabled state, and click result before returning success.

### Task 4: Verification recovery flow

**Files:**
- Modify: `core/signup_flow.py`
- Modify: `nyxify_runner.py`
- Test: `tests/test_signup_blockers.py`

- [ ] Ensure email replacement updates the active attempt email before OTP fetch.
- [ ] Ensure recovery uses the verification back button only to return to email/phone form.
- [ ] Keep max replacement attempts at two after the initial value.

### Task 5: Extension no-pending and mismatch handling

**Files:**
- Modify: `nyxify_extension/content.js`
- Modify: `nyxify_extension/background.js`

- [ ] Make no-pending email/phone a classified terminal response after one Get/Request attempt.
- [ ] Keep OTP/SMS checking gated by row value matching the exact submitted email/phone.
- [ ] Avoid SnapBoard refresh loops caused by repeated stale no-pending responses.

### Task 6: Verification

**Files:**
- Test: `tests/test_nyxify_bridge_waits.py`
- Test: `tests/test_signup_blockers.py`
- Test: `tests/test_signup_sms_recovery.py`

- [ ] Run focused unittest suite.
- [ ] Report local-only changes and any live-test instructions.
