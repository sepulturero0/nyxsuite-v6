# Nyxify Continuous Delay Retry Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove recurring continuous-mode delays caused by AdsPower Local API proxy-check loops and make email, phone, and OTP retry waits observable and bounded.

**Architecture:** Treat AdsPower Local API unavailability as a checker-availability problem, not a bad-proxy signal, so GUI/no-API runs do not burn every SnapBoard proxy rotation before profile creation. Keep email/phone/OTP retry behavior intact, but add targeted tests and timing/status hooks so slow bridge dispatch, replacement cooldowns, and manual recovery waits are visible and do not silently block continuous mode.

**Tech Stack:** Python 3, asyncio, unittest, SQLite queue stores, AdsPower GUI/API control, SnapBoard bridge polling.

---

### Task 1: Stop Proxy Rotation When AdsPower Proxy Checker Is Unavailable

**Files:**
- Modify: `core/adspower.py`
- Test: `tests/test_adspower_cdp_fallback.py`

- [ ] **Step 1: Add failing tests for Local API connection-refused fallback**

Add these tests under `ProxyCheckNoApiTests`:

```python
    def test_unreachable_checker_falls_back_to_socket(self):
        m = self._manager()
        m._post_json = mock.Mock(side_effect=ConnectionError(
            "HTTPConnectionPool(host='localhost', port=50325): "
            "Failed to establish a new connection: [Errno 61] Connection refused"
        ))
        m.test_proxy_connection = mock.Mock(return_value={"ok": True, "message": "socket ok"})

        res = m.check_proxy_via_adspower("1.2.3.4:9999:u:p", 20, True)

        self.assertTrue(res.get("ok"))
        self.assertEqual(res.get("fallback"), "socket")
        self.assertTrue(res.get("checker_unavailable"))
        m.test_proxy_connection.assert_called_once_with("1.2.3.4:9999:u:p")

    def test_unreachable_checker_keeps_proxy_failure_when_socket_fails(self):
        m = self._manager()
        m._post_json = mock.Mock(side_effect=ConnectionError(
            "curl: (7) Failed to connect to 127.0.0.1 port 50325"
        ))
        m.test_proxy_connection = mock.Mock(return_value={"ok": False, "message": "socket refused"})

        res = m.check_proxy_via_adspower("1.2.3.4:9999:u:p", 20, True)

        self.assertFalse(res.get("ok"))
        self.assertEqual(res.get("fallback"), "socket")
        self.assertTrue(res.get("checker_unavailable"))
```

- [ ] **Step 2: Run the focused tests and verify they fail**

Run:

```bash
python3 -m unittest tests.test_adspower_cdp_fallback.ProxyCheckNoApiTests -v
```

Expected before implementation: the new connection-refused fallback tests fail because `_is_proxy_checker_unavailable_error()` only recognizes 404/method errors.

- [ ] **Step 3: Classify AdsPower Local API connection failures as checker unavailable**

Change `core/adspower.py`:

```python
def _is_proxy_checker_unavailable_error(error_message):
    normalized = str(error_message or "").strip().lower()
    return (
        "404" in normalized
        or "not found for url" in normalized
        or "method not allowed" in normalized
        or "connection refused" in normalized
        or "failed to establish a new connection" in normalized
        or "failed to connect" in normalized
        or "couldn't connect to server" in normalized
        or "max retries exceeded" in normalized
    )
```

- [ ] **Step 4: Verify proxy fallback tests pass**

Run:

```bash
python3 -m unittest tests.test_adspower_cdp_fallback.ProxyCheckNoApiTests -v
```

Expected: all `ProxyCheckNoApiTests` pass.

### Task 2: Make Continuous Mode Skip Non-Blocking SnapBoard Name Confirmation

**Files:**
- Modify: `nyxify_runner.py`
- Test: `tests/test_nyxify_continuous_mode.py`

- [ ] **Step 1: Add a failing test showing continuous handoff should not wait for AdsPower-name confirmation**

Add a test to `NyxifyContinuousModeTests`:

```python
    async def test_continuous_handoff_does_not_wait_for_adspower_name_confirmation(self):
        waits = []

        async def wait_for_update(path, row_key, label, timeout_seconds=30):
            waits.append((path, label, timeout_seconds))
            return label != "AdsPower name"

        with mock.patch.object(nyxify_runner, "_wait_for_snapboard_update", side_effect=wait_for_update):
            await self._run_task(True)

        self.assertFalse(
            any(label == "AdsPower name" for _path, label, _timeout in waits),
            waits,
        )
```

- [ ] **Step 2: Run the focused continuous-mode test and verify it fails**

Run:

```bash
python3 -m unittest tests.test_nyxify_continuous_mode.NyxifyContinuousModeTests.test_continuous_handoff_does_not_wait_for_adspower_name_confirmation -v
```

Expected before implementation: fails because `_apply_final_username()` currently awaits `/adspower_name_update/status`.

- [ ] **Step 3: Queue name update without blocking in continuous mode**

Change the AdsPower-name sync block in `_apply_final_username()`:

```python
                        if _request_snapboard_adspower_name_update(row_key_value, snapboard_adspower_name):
                            if continuous_mode_enabled:
                                snapboard_adspower_name_synced = False
                                logger.info(
                                    f"Task {task_id}: requested SnapBoard AdsPower name update; "
                                    "not waiting for confirmation before Nyx handoff."
                                )
                            else:
                                snapboard_adspower_name_synced = await _wait_for_snapboard_update(
                                    "/adspower_name_update/status",
                                    row_key_value,
                                    "AdsPower name",
                                )
```

- [ ] **Step 4: Verify continuous-mode tests pass**

Run:

```bash
python3 -m unittest tests.test_nyxify_continuous_mode.NyxifyContinuousModeTests -v
```

Expected: continuous handoff still queues Nyx, but the 30s AdsPower-name confirmation wait is skipped in continuous mode.

### Task 3: Add Timing Evidence for Email, Phone, OTP, and Manual Recovery Budgets

**Files:**
- Modify: `nyxify_runner.py`
- Modify: `core/signup_flow.py`
- Test: `tests/test_nyxify_bridge_waits.py`
- Test: `tests/test_signup_sms_recovery.py`

- [ ] **Step 1: Add regression tests for OTP timeout logging and request cleanup**

Add a test around `_request_snapboard_otp_from_store()` that uses a fake store returning no code, patches `_request_snapboard_refresh()` to return `False`, and asserts both `request_otp_for_row()` and `clear_otp_request()` were called once.

```python
async def test_otp_timeout_clears_pending_request_before_refresh_failure(self):
    store = _FakeStore()
    with mock.patch.object(nyxify_runner, "_request_snapboard_refresh", mock.AsyncMock(return_value=False)):
        code = await nyxify_runner._request_snapboard_otp_from_store(
            store,
            "snapboard:1",
            task_id=1,
            timeout_seconds=0.1,
            poll_seconds=0.05,
        )
    self.assertEqual(code, "")
    self.assertEqual(store.requested, ["snapboard:1"])
    self.assertEqual(store.cleared, ["snapboard:1"])
```

- [ ] **Step 2: Add a phone wait-budget test**

Add a test in `tests/test_signup_sms_recovery.py` that patches `_wait_for_signup_progress()` to return `"phone"` immediately twice and confirms `_handle_optional_phone_sms_verification()` raises `phone_verification_rejected` after `PHONE_VERIFICATION_MAX_ATTEMPTS`, not after manual recovery.

- [ ] **Step 3: Emit clear timing logs for long waits**

In `nyxify_runner._request_snapboard_otp_from_store()`, log elapsed time and retry budget on both timeout branches. In `core/signup_flow._handle_optional_phone_sms_verification()`, log the configured phone attempt count and the 300s stage wait before each phone submission.

- [ ] **Step 4: Verify bridge and signup recovery tests**

Run:

```bash
python3 -m unittest tests.test_nyxify_bridge_waits tests.test_signup_sms_recovery tests.test_signup_blockers -v
```

Expected: email/SnapBoard bridge retry behavior, SMS recovery, and blocker classification all pass.

### Task 4: Add Runtime Verification Checklist

**Files:**
- No source changes; manual verification only.

- [ ] **Step 1: Start NyxSuite in GUI control mode with continuous mode enabled**

Run the app normally, with `proxy_checker_enabled=true`, `continuous_mode_enabled=true`, and `max_parallel_profiles=1`.

- [ ] **Step 2: Verify proxy precheck behavior**

Temporarily stop AdsPower Local API or start AdsPower in no-API GUI mode. Expected: Nyxify does not burn `MAX_PROXY_ROTATION_ATTEMPTS` solely because port `50325` is unavailable. The log should show socket fallback or a direct actionable AdsPower availability message.

- [ ] **Step 3: Verify five-profile continuous run**

Queue five SnapBoard rows. Expected: five Nyxify rows reach `queued_for_nyx`; five Nyx rows with `source='nyxify_continuous'` reach `DONE`; email and OTP timings are logged with `[SNAPBOARD_TIMING]`; no continuous wait remains after the fifth Bitmoji row completes.

- [ ] **Step 4: Record observed timings**

Capture these fields without account data: proxy rotations, profile create duration, cookie warmup duration, email fetch duration, OTP fetch duration, success-to-Nyx-queue duration, and Nyx Bitmoji duration.
