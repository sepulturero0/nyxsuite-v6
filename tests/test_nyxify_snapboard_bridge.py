from pathlib import Path
import tempfile
import time
import unittest

from core.nyxify_local_api import (
    NyxifyLocalApiServer,
    _EmailFetchStore,
    _PhoneFetchStore,
    _ProxyRotateStore,
    _SmsFetchStore,
    EMAIL_PHONE_FETCH_DISPATCH_LEASE_SECONDS,
    PROXY_ROTATE_DISPATCH_LEASE_SECONDS,
    VERIFICATION_DISPATCH_LEASE_SECONDS,
    _task_allows_proxy_rotate_result_update,
)
from core.nyxify_task_store import NyxifyTaskStore


ROOT = Path(__file__).resolve().parents[1]


class NyxifySnapboardBridgeTests(unittest.TestCase):
    def test_proxy_rotation_request_is_released_after_dispatch_lease(self):
        store = _ProxyRotateStore()
        store.request("snapboard:lease", max_clicks=3)
        first = store.pop_pending()
        self.assertEqual(first["row_key"], "snapboard:lease")
        self.assertIsNone(store.pop_pending())

        store._pending["snapboard:lease"]["dispatched_at"] = (
            time.monotonic() - PROXY_ROTATE_DISPATCH_LEASE_SECONDS - 1
        )
        retry = store.pop_pending()
        self.assertEqual(retry["row_key"], "snapboard:lease")

    def test_proxy_rotation_result_from_old_request_is_rejected(self):
        store = _ProxyRotateStore()
        first_id = store.request("snapboard:old", max_clicks=3)
        store.pop_pending()
        store._pending["snapboard:old"]["dispatched_at"] = (
            time.monotonic() - PROXY_ROTATE_DISPATCH_LEASE_SECONDS - 1
        )
        second_id = store.request("snapboard:old", max_clicks=3)
        self.assertNotEqual(first_id, second_id)
        self.assertFalse(store.store_result("snapboard:old", proxy="old", request_id=first_id))
        self.assertIsNone(store.get_result("snapboard:old"))
        self.assertTrue(store.store_result("snapboard:old", proxy="new", request_id=second_id))

    def test_stale_running_task_is_requeued_but_recent_running_task_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = NyxifyTaskStore(Path(tmp) / "tasks.db")
            store.upsert_task("snapboard:stale", "Clea", "1.1.1.1", username="stale")
            store.upsert_task("snapboard:recent", "Clea", "1.1.1.2", username="recent")
            claimed = store.claim_pending_tasks(limit=2)
            self.assertEqual(len(claimed), 2)
            with store._connect() as conn:
                conn.execute(
                    "UPDATE tasks SET updated_at = datetime('now', '-120 seconds') WHERE row_key = ?",
                    ("snapboard:stale",),
                )
            self.assertEqual(store.reset_orphaned_running_tasks(stale_after_seconds=30), 1)
            rows = {row["row_key"]: row for row in store.list_tasks()}
            self.assertEqual(rows["snapboard:stale"]["status"], "PENDING")
            self.assertEqual(rows["snapboard:recent"]["status"], "RUNNING")

    def test_content_script_polls_pending_adspower_id_updates(self):
        content = (ROOT / "nyxify_extension" / "content.js").read_text(encoding="utf-8")

        self.assertIn("function pollPendingAdspowerUpdate()", content)
        self.assertIn('"/adspower_update/pending"', content)
        self.assertIn('"/adspower_update/result"', content)
        self.assertIn("startAdspowerUpdatePoll();", content)

    def test_adspower_name_bridge_does_not_write_snapboard_username_field(self):
        content = (ROOT / "nyxify_extension" / "content.js").read_text(encoding="utf-8")
        fn = content.split("function requestAdspowerNameUpdate", 1)[1].split("function ", 1)[0]

        self.assertIn('callPageUpdateField(rowId, "adspowerName", adspowerName);', fn)
        self.assertNotIn('callPageUpdateField(rowId, "name", adspowerName);', fn)
        self.assertNotIn('"input.cell-input.input-name"', content)
        self.assertNotIn('"input.input-name"', content)
        self.assertNotIn('onchange*=\\"name\\"', content)

    def test_task_store_persists_snapboard_row_password(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = NyxifyTaskStore(Path(tmp) / "tasks.db")

            store.upsert_task(
                row_key="snapboard:505811",
                model="Clea",
                ip_address="198.51.100.10",
                proxy_address="198.51.100.10:9000:user:pass",
                username="cleaopala",
                email="clea@example.com",
                password="KyotoRiver%12",
            )

            row = store.list_tasks()[0]
            self.assertEqual(row["password"], "KyotoRiver%12")

            claimed = store.claim_pending_tasks(limit=1)
            self.assertEqual(claimed[0]["password"], "KyotoRiver%12")

    def test_task_store_updates_snapboard_row_password_on_resync(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = NyxifyTaskStore(Path(tmp) / "tasks.db")

            store.upsert_task(
                row_key="snapboard:505811",
                model="Clea",
                ip_address="198.51.100.10",
                username="cleaopala",
                password="OldPassword1!",
            )
            store.upsert_task(
                row_key="snapboard:505811",
                model="Clea",
                ip_address="198.51.100.10",
                username="cleaopala",
                password="NewPassword2!",
            )

            row = store.list_tasks()[0]
            self.assertEqual(row["password"], "NewPassword2!")

    def test_task_store_does_not_overwrite_running_proxy_on_extension_resync(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = NyxifyTaskStore(Path(tmp) / "tasks.db")

            task_id, _action = store.upsert_task(
                row_key="snapboard:505811",
                model="Clea",
                ip_address="23.54.1.2",
                proxy_address="23.54.1.2:9000:user:pass",
                username="cleaopala",
                password="KyotoRiver%12",
            )
            store.update_task_state(
                task_id,
                status="RUNNING",
                last_step="creating_adspower_profile",
                adspower_profile_id="k1valid",
            )

            store.upsert_task(
                row_key="snapboard:505811",
                model="Clea",
                ip_address="45.10.1.1",
                proxy_address="45.10.1.1:9000:user:pass",
                username="cleaopala",
                password="KyotoRiver%12",
            )

            row = store.list_tasks()[0]
            self.assertEqual(row["ip_address"], "23.54.1.2")
            self.assertEqual(row["proxy_address"], "23.54.1.2:9000:user:pass")

    def test_proxy_rotate_result_update_is_blocked_after_adspower_create(self):
        self.assertFalse(_task_allows_proxy_rotate_result_update({
            "status": "RUNNING",
            "last_step": "creating_adspower_profile",
            "adspower_profile_id": "",
        }))
        self.assertFalse(_task_allows_proxy_rotate_result_update({
            "status": "DONE",
            "last_step": "signup_complete",
            "adspower_profile_id": "k1valid",
        }))
        self.assertTrue(_task_allows_proxy_rotate_result_update({
            "status": "RUNNING",
            "last_step": "checking_proxy",
            "adspower_profile_id": "",
        }))

    def test_task_store_priority_claim_only_starts_matching_proxy_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = NyxifyTaskStore(Path(tmp) / "tasks.db")

            store.upsert_task(
                row_key="snapboard:1",
                model="Clea",
                ip_address="45.10.1.1",
                proxy_address="45.10.1.1:9000:user:pass",
                username="cleaone",
                password="Password1!",
            )
            store.upsert_task(
                row_key="snapboard:2",
                model="Clea",
                ip_address="23.54.1.2",
                proxy_address="23.54.1.2:9000:user:pass",
                username="cleatwo",
                password="Password2!",
            )

            claimed = store.claim_pending_tasks(limit=2, proxy_priority_patterns=["23.54"])
            rows = {row["row_key"]: row for row in store.list_tasks()}

            self.assertEqual([row["row_key"] for row in claimed], ["snapboard:2"])
            self.assertEqual(rows["snapboard:1"]["status"], "PENDING")
            self.assertEqual(rows["snapboard:2"]["status"], "RUNNING")

    def test_task_store_pending_otp_uses_submitted_email(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = NyxifyTaskStore(Path(tmp) / "tasks.db")

            store.upsert_task(
                row_key="snapboard:505811",
                model="Clea",
                ip_address="198.51.100.10",
                username="cleaopala",
                email="old@example.com",
                password="KyotoRiver%12",
            )

            store.request_otp_for_row("snapboard:505811", email="submitted@example.com")
            pending = store.get_pending_otp_request()

        self.assertEqual(pending["row_key"], "snapboard:505811")
        self.assertEqual(pending["email"], "submitted@example.com")

    def test_task_store_otp_pending_has_dispatch_lease(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = NyxifyTaskStore(Path(tmp) / "tasks.db")
            store.upsert_task(
                row_key="snapboard:505811",
                model="Clea",
                ip_address="198.51.100.10",
                username="cleaopala",
                email="submitted@example.com",
            )
            store.request_otp_for_row("snapboard:505811", email="submitted@example.com")

            first = store.get_pending_otp_request(lease_seconds=60)
            second = store.get_pending_otp_request(lease_seconds=60)

            self.assertIsNotNone(first)
            self.assertIsNone(second)

            with store._connect() as conn:
                conn.execute(
                    "UPDATE tasks SET otp_dispatched_at = ? WHERE row_key = ?",
                    (time.time() - 61, "snapboard:505811"),
                )

            third = store.get_pending_otp_request(lease_seconds=60)
            self.assertIsNotNone(third)
            self.assertEqual(third["dispatch_count"], 2)

    def test_sms_store_keeps_request_leased_and_clears_pending_on_error(self):
        store = _SmsFetchStore()
        store.request("snapboard:505811", phone="+15551234567")

        first = store.pop_pending()
        second = store.pop_pending()

        self.assertEqual(first["row_key"], "snapboard:505811")
        self.assertEqual(first["dispatch_count"], 1)
        self.assertIsNone(second)

        store._pending["snapboard:505811"]["dispatched_at"] = (
            time.monotonic() - VERIFICATION_DISPATCH_LEASE_SECONDS - 1
        )
        retry = store.pop_pending()
        self.assertEqual(retry["dispatch_count"], 2)

        store.store_result("snapboard:505811", error="SMS code not found on SnapBoard row.")
        self.assertIsNone(store.pop_pending())
        self.assertEqual(
            store.get_result("snapboard:505811")["error"],
            "SMS code not found on SnapBoard row.",
        )

    def test_email_phone_fetch_stores_use_long_dispatch_lease(self):
        for store_class in (_EmailFetchStore, _PhoneFetchStore):
            store = store_class()
            store.request("snapboard:lease")

            first = store.pop_pending()
            second = store.pop_pending()

            self.assertEqual(first["row_key"], "snapboard:lease")
            self.assertIsNone(second)

            store._pending["snapboard:lease"]["dispatched_at"] = (
                time.monotonic() - EMAIL_PHONE_FETCH_DISPATCH_LEASE_SECONDS - 1
            )
            retry = store.pop_pending()
            self.assertEqual(retry["row_key"], "snapboard:lease")

    def test_extension_requires_expected_email_for_email_otp(self):
        content = (ROOT / "nyxify_extension" / "content.js").read_text(encoding="utf-8")

        self.assertNotIn("if (!expected) {\n      return true;", content)
        self.assertIn("Missing expected email for OTP check", content)

    def test_extension_uses_snapboard_check_countdown_for_otp_wait(self):
        content = (ROOT / "nyxify_extension" / "content.js").read_text(encoding="utf-8")

        self.assertIn("function readAuthCheckCountdownMs(rowId, kind)", content)
        self.assertIn("OTP_FETCH_MAX_TIMEOUT_MS", content)
        self.assertIn("observeCountdown(true)", content)
        self.assertIn("observeCountdown(false)", content)

    def test_extension_finds_adspower_name_by_table_header_when_class_is_missing(self):
        content = (ROOT / "nyxify_extension" / "content.js").read_text(encoding="utf-8")

        self.assertIn("function findRowInputByHeader(rowId, selectors, aliases)", content)
        self.assertIn('"adspower name", "ads power name", "profile name", "browser name"', content)
        self.assertIn("readElementValue(input)", content)

    def test_extension_does_not_refresh_on_terminal_no_pending_fetch(self):
        background = (ROOT / "nyxify_extension" / "background.js").read_text(encoding="utf-8")

        self.assertIn("function isTerminalSnapboardFetchResponse(response)", background)
        self.assertIn("response.terminal", background)
        self.assertIn("return response;", background[background.index("if (isTerminalSnapboardFetchResponse(response))"):])

    def test_replace_banned_reset_clears_old_adspower_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = NyxifyTaskStore(Path(tmp) / "tasks.db")

            task_id, _action = store.upsert_task(
                row_key="snapboard:505811",
                model="Clea",
                ip_address="198.51.100.10",
                proxy_address="198.51.100.10:9000:user:pass",
                username="olduser",
                email="old@example.com",
                password="KyotoRiver%12",
                adspower_id="k1old",
            )
            store.update_task_state(
                task_id,
                status="DONE",
                last_step="completed",
                error="old error",
                adspower_profile_id="k1old",
                adspower_name="Snapchat: olduser",
                adspower_group="Snapchat",
                tags=["Snapchat"],
            )

            updated = store.replace_for_banned_account(
                row_key="snapboard:505811",
                model="Clea",
                ip_address="198.51.100.10",
                proxy_address="203.0.113.44:9100:user:pass",
                username="freshuser",
                email="fresh@example.com",
                password="KyotoRiver%12",
            )

            self.assertTrue(updated)
            row = store.list_tasks()[0]
            self.assertEqual(row["status"], "PENDING")
            self.assertEqual(row["last_step"], "replace_banned_pending")
            self.assertEqual(row["error"], "")
            self.assertEqual(row["username"], "freshuser")
            self.assertEqual(row["email"], "fresh@example.com")
            self.assertEqual(row["proxy_address"], "203.0.113.44:9100:user:pass")
            self.assertEqual(row["adspower_id"], "")
            self.assertEqual(row["adspower_profile_id"], "")
            self.assertEqual(row["adspower_name"], "")
            self.assertEqual(row["adspower_group"], "")
            self.assertEqual(row["tags"], [])

    def test_extension_extracts_and_flushes_snapboard_row_password(self):
        content = (ROOT / "nyxify_extension" / "content.js").read_text(encoding="utf-8")
        background = (ROOT / "nyxify_extension" / "background.js").read_text(encoding="utf-8")

        self.assertIn('["password", "pass", "snap password", "snapchat password", "account password"]', content)
        self.assertIn("password: password", content)
        self.assertIn("const password = String(row.password || \"\").trim();", background)
        self.assertIn("password: entry.password", background)

    def test_remove_banned_rotates_proxy_and_clears_snapboard_adspower_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = NyxifyTaskStore(Path(tmp) / "tasks.db")
            task_id, _action = store.upsert_task(
                row_key="snapboard:505811",
                model="Clea",
                ip_address="198.51.100.10",
                proxy_address="198.51.100.10:9000:user:pass",
                username="olduser",
                email="old@example.com",
                password="KyotoRiver%12",
                adspower_id="k1old",
            )
            store.update_task_state(task_id, status="DONE", last_step="completed")
            api = NyxifyLocalApiServer(store)

            def wait_for_value(wait_store, row_key, value_key, timeout_seconds=75):
                self.assertIs(wait_store, api.proxy_rotate_store)
                self.assertEqual(row_key, "snapboard:505811")
                self.assertEqual(value_key, "proxy")
                return "203.0.113.44:9100:user:pass", ""

            def wait_for_success(wait_store, row_key, timeout_seconds=30):
                self.assertIs(wait_store, api.adspower_update_store)
                self.assertEqual(row_key, "snapboard:505811")
                request = wait_store.pop_pending()
                self.assertEqual(request["adspower_id"], "")
                return True, ""

            api._wait_for_value_result = wait_for_value
            api._wait_for_update_success = wait_for_success

            result = api.remove_banned_rows(rows=[{
                "row_key": "snapboard:505811",
                "model": "Clea",
                "ip_address": "198.51.100.10",
                "adspower_id": "k1old",
                "status": "Banned",
            }])

            self.assertTrue(result["ok"])
            self.assertEqual(result["removed"], 1)
            self.assertEqual(result["warmup"], 0)
            row = store.list_tasks()[0]
            self.assertEqual(row["status"], "DONE")
            self.assertEqual(row["last_step"], "remove_banned_proxy_changed")
            self.assertEqual(row["proxy_address"], "203.0.113.44:9100:user:pass")
            self.assertEqual(row["adspower_id"], "")
            self.assertEqual(row["username"], "olduser")
            self.assertEqual(row["email"], "old@example.com")

    def test_remove_banned_api_clears_every_adspower_id_before_rotating_proxies(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = NyxifyTaskStore(Path(tmp) / "tasks.db")
            rows = []
            for index, row_key in enumerate(["snapboard:505811", "snapboard:505812"]):
                store.upsert_task(
                    row_key=row_key,
                    model="Clea",
                    ip_address=f"198.51.100.{10 + index}",
                    proxy_address=f"198.51.100.{10 + index}:9000:user:pass",
                    username=f"olduser{index}",
                    email=f"old{index}@example.com",
                    password="KyotoRiver%12",
                    adspower_id=f"k1old{index}",
                )
                rows.append({
                    "row_key": row_key,
                    "model": "Clea",
                    "ip_address": f"198.51.100.{10 + index}",
                    "adspower_id": f"k1old{index}",
                    "status": "Banned",
                })
            api = NyxifyLocalApiServer(store)
            events = []

            def wait_for_value(wait_store, row_key, value_key, timeout_seconds=75):
                events.append(("proxy", row_key))
                return f"203.0.113.{row_key[-1]}:9100:user:pass", ""

            def wait_for_success(wait_store, row_key, timeout_seconds=30):
                events.append(("adspower", row_key))
                return True, ""

            api._wait_for_value_result = wait_for_value
            api._wait_for_update_success = wait_for_success

            result = api.remove_banned_rows(rows=rows)

            self.assertTrue(result["ok"])
            self.assertEqual([event[0] for event in events], ["adspower", "adspower", "proxy", "proxy"])

    def test_remove_banned_forces_proxy_rotation_even_when_proxy_toggles_are_off(self):
        api = (ROOT / "core" / "nyxify_local_api.py").read_text(encoding="utf-8")
        content = (ROOT / "nyxify_extension" / "content.js").read_text(encoding="utf-8")

        self.assertIn("force=True", api)
        self.assertIn('"force": bool(request.get("force"))', api)
        self.assertIn("&& !priorityPatterns.length", content)

    def test_forced_runner_proxy_rotation_reaches_snapboard_even_without_filters(self):
        api = (ROOT / "core" / "nyxify_local_api.py").read_text(encoding="utf-8")
        background = (ROOT / "nyxify_extension" / "background.js").read_text(encoding="utf-8")
        content = (ROOT / "nyxify_extension" / "content.js").read_text(encoding="utf-8")

        self.assertIn('force = bool(payload.get("force"))', api)
        self.assertIn("force=force", api)
        self.assertIn("proxy_type: proxyPayload.proxy_type", background)
        self.assertIn(
            "function rotateProxyUntilChanged(rowId, timeoutMs, maxClicks, priorityPatterns, blockedPatterns, proxyType, force)",
            content,
        )
        self.assertIn("&& !force", content)

    def test_popup_remove_banned_clears_all_adspower_ids_before_proxy_rotation(self):
        background = (ROOT / "nyxify_extension" / "background.js").read_text(encoding="utf-8")
        api = (ROOT / "core" / "nyxify_local_api.py").read_text(encoding="utf-8")

        self.assertIn("async function removeBannedRowsDirectly", background)
        direct_fn = background.split("async function removeBannedRowsDirectly", 1)[1].split("\nasync function ", 1)[0]
        self.assertLess(direct_fn.index('action: "adspower_update"'), direct_fn.index('action: "proxy_rotate"'))
        self.assertIn('"/replace_banned/remove_result"', direct_fn)
        self.assertIn("force: true", direct_fn)
        self.assertIn('if self.path == "/replace_banned/remove_result":', api)

    def test_remove_banned_local_api_endpoints_are_wired(self):
        api = (ROOT / "core" / "nyxify_local_api.py").read_text(encoding="utf-8")
        controller = (ROOT / "core" / "nyxify_controller.py").read_text(encoding="utf-8")

        self.assertIn("class _ReplaceBannedScanStore", api)
        self.assertIn('"/replace_banned/snapshot"', api)
        self.assertIn('"/replace_banned/scan"', api)
        self.assertIn('"/replace_banned/remove"', api)
        self.assertIn('"/replace_banned/warmup"', api)
        self.assertIn("remove_for_banned_account", api)
        self.assertIn('"delete_adspower_profile"', controller)

    def test_nyxify_extension_scans_and_removes_banned_rows(self):
        content = (ROOT / "nyxify_extension" / "content.js").read_text(encoding="utf-8")
        background = (ROOT / "nyxify_extension" / "background.js").read_text(encoding="utf-8")
        popup_html = (ROOT / "nyxify_extension" / "popup.html").read_text(encoding="utf-8")
        popup_js = (ROOT / "nyxify_extension" / "popup.js").read_text(encoding="utf-8")

        self.assertIn("function extractSnapboardStatusRows", content)
        self.assertIn("NYXIFY_SCAN_BANNED_ROWS", content)
        self.assertIn("NYXIFY_SNAPBOARD_STATUS_ROWS", background)
        self.assertIn("NYXIFY_SCAN_BANNED_ROWS", background)
        self.assertIn("NYXIFY_REMOVE_BANNED_ROWS", background)
        self.assertIn("NYXIFY_WARMUP_BANNED_ROWS", background)
        self.assertNotIn('id="scanBannedButton"', popup_html)
        self.assertNotIn('id="removeBannedButton"', popup_html)
        self.assertNotIn('id="warmupBannedButton"', popup_html)
        self.assertNotIn('id="bannedAdspowerIds"', popup_html)
        self.assertNotIn("scanBannedRows", popup_js)
        self.assertNotIn("removeBannedRows", popup_js)
        self.assertNotIn("warmupBannedRows", popup_js)
        self.assertNotIn("formatBannedAdspowerIds", popup_js)

    def test_nyx_snapboard_menu_replace_and_add_to_nyx_pending(self):
        content = (ROOT / "nyx_extension" / "content.js").read_text(encoding="utf-8")
        background = (ROOT / "nyx_extension" / "background.js").read_text(encoding="utf-8")

        self.assertIn("Add to Nyx pending", content)
        self.assertIn("NYX_REPLACE_SNAPBOARD_ROW", content)
        self.assertIn("NYX_ADD_TO_NYX_PENDING", content)
        self.assertIn("password: password", content)
        self.assertIn("password: message.password", background)
        self.assertIn("const password = normalizeText(safeRow.password);", background)
        self.assertNotIn("window.confirm(", content)
        self.assertIn("NYX_REPLACE_SNAPBOARD_ROW", background)
        self.assertIn("NYX_ADD_TO_NYX_PENDING", background)

    def test_dashboard_replace_banned_controls_and_no_nyx_clear_queue(self):
        html = (ROOT / "webui" / "index.html").read_text(encoding="utf-8")
        popup_html = (ROOT / "nyxify_extension" / "popup.html").read_text(encoding="utf-8")
        dashboard = (ROOT / "webui" / "dashboard.js").read_text(encoding="utf-8")
        css = (ROOT / "webui" / "dashboard.css").read_text(encoding="utf-8")

        self.assertIn('id="scan-banned-nyxify"', html)
        self.assertIn('id="remove-banned-nyxify"', html)
        self.assertIn('id="warmup-banned-nyxify"', html)
        self.assertIn('id="banned-adspower-ids-nyxify"', html)
        self.assertNotIn("Scan SnapBoard for banned rows.", html)
        self.assertNotIn("Scan SnapBoard for banned rows.", popup_html)
        self.assertIn("scanBannedFromDashboard", dashboard)
        self.assertIn("removeBannedFromDashboard", dashboard)
        self.assertIn("warmupBannedFromDashboard", dashboard)
        self.assertIn("command-grid", css)
        nyx_config = dashboard.split("nyxify: {", 1)[0]
        self.assertNotIn('["Clear Queue", "/queue/clear", "bad"]', nyx_config)

    def test_proxy_ranking_has_bulk_red_ban_action(self):
        html = (ROOT / "webui" / "index.html").read_text(encoding="utf-8")
        dashboard = (ROOT / "webui" / "dashboard.js").read_text(encoding="utf-8")
        css = (ROOT / "webui" / "dashboard.css").read_text(encoding="utf-8")

        self.assertIn('id="proxyrank-ban-red"', html)
        self.assertIn("Ban all red", html)
        self.assertIn("banBadProxyRows", dashboard)
        self.assertIn("/proxy_ranking/ban_many", dashboard)
        self.assertIn("proxyrank-summary", css)

    def test_proxy_ranking_bulk_red_ban_refreshes_live_rows_before_posting(self):
        dashboard = (ROOT / "webui" / "dashboard.js").read_text(encoding="utf-8")

        self.assertNotEqual(
            dashboard.find("async function loadProxyRankingRows()"),
            -1,
            "Dashboard should centralize live proxy-ranking row loading.",
        )
        fn_start = dashboard.index("async function banBadProxyRows()")
        fn_end = dashboard.index('el("proxyrank-refresh")', fn_start)
        bulk_fn = dashboard[fn_start:fn_end]

        self.assertLess(
            bulk_fn.index("await loadProxyRankingRows"),
            bulk_fn.index("badProxyRows"),
            "Bulk red ban must compute red subnets from freshly loaded ranking rows.",
        )

    def test_dashboard_runner_controls_are_anchored_upper_left(self):
        html = (ROOT / "webui" / "index.html").read_text(encoding="utf-8")
        dashboard = (ROOT / "webui" / "dashboard.js").read_text(encoding="utf-8")
        css = (ROOT / "webui" / "dashboard.css").read_text(encoding="utf-8")

        self.assertIn('class="runner-dock"', html)
        self.assertIn('class="toolbar runner-controls"', html)
        self.assertLess(html.index('id="runner-suite"'), html.index('id="tiles-suite"'))
        self.assertIn(".runner-dock", css)
        self.assertIn(".runner-controls", css)
        self.assertIn("runner-start-stop", dashboard)

    def test_dashboard_actions_have_consistent_zones_per_product(self):
        html = (ROOT / "webui" / "index.html").read_text(encoding="utf-8")
        css = (ROOT / "webui" / "dashboard.css").read_text(encoding="utf-8")

        for product in ("nyx", "nyxify"):
            section_start = html.index(f'id="panel-{product}"')
            section_end = html.find('<section class="panel', section_start + 1)
            section = html[section_start:section_end if section_end != -1 else len(html)]

            self.assertIn(f'id="actions-queue-{product}"', section)
            self.assertIn(f'id="actions-row-{product}"', section)
            self.assertIn(f'id="actions-search-{product}"', section)
            self.assertLess(section.index(f'id="actions-queue-{product}"'), section.index(f'id="actions-row-{product}"'))
            self.assertLess(section.index(f'id="actions-row-{product}"'), section.index(f'id="actions-search-{product}"'))

        self.assertIn(".action-stack", css)
        self.assertIn(".action-row", css)
        self.assertIn(".action-top", css)

    def test_nyxify_popup_runner_buttons_are_above_settings_panel(self):
        popup_html = (ROOT / "nyxify_extension" / "popup.html").read_text(encoding="utf-8")
        popup_css = (ROOT / "nyxify_extension" / "styles.css").read_text(encoding="utf-8")
        popup_js = (ROOT / "nyxify_extension" / "popup.js").read_text(encoding="utf-8")

        self.assertLess(popup_html.index('class="runner-action-strip"'), popup_html.index('class="status-tiles"'))
        self.assertLess(popup_html.index('class="runner-action-strip"'), popup_html.index('class="control-panel"'))
        self.assertIn('id="pauseResumeRunnerButton" class="button runner-action-button" type="button" data-action="pause" disabled', popup_html)
        self.assertIn(".runner-action-strip", popup_css)
        self.assertIn(".runner-state-pill", popup_css)
        self.assertIn("pauseResumeButton.disabled = isOffline || !isActive", popup_js)

    def test_extension_popups_are_compact_and_auto_save_dashboard_settings(self):
        nyx_html = (ROOT / "nyx_extension" / "popup.html").read_text(encoding="utf-8")
        nyx_css = (ROOT / "nyx_extension" / "styles.css").read_text(encoding="utf-8")
        nyx_js = (ROOT / "nyx_extension" / "popup.js").read_text(encoding="utf-8")
        nyxify_html = (ROOT / "nyxify_extension" / "popup.html").read_text(encoding="utf-8")
        nyxify_css = (ROOT / "nyxify_extension" / "styles.css").read_text(encoding="utf-8")
        nyxify_js = (ROOT / "nyxify_extension" / "popup.js").read_text(encoding="utf-8")

        self.assertNotIn("Push AdsPower ID to SnapBoard", nyxify_html)
        self.assertNotIn("Apply AdsPower tags", nyxify_html)
        self.assertLess(nyxify_html.index('id="popupAutoFillRowToggle"'), nyxify_html.index('id="popupProxyBlockerToggle"'))
        self.assertLess(nyxify_html.index('id="popupAutoFillAccountTarget"'), nyxify_html.index('id="popupProxyBlockerToggle"'))
        self.assertIn('class="toggle-switch toggle-switch-warning" for="popupAutoFillRowToggle"', nyxify_html)
        self.assertIn(".toggle-switch-warning input:checked + .toggle-slider", nyxify_css)
        self.assertIn("background: #d49121", nyxify_css)

        for popup_html in (nyx_html, nyxify_html):
            self.assertNotIn('id="savePopupSettingsButton"', popup_html)
            self.assertNotIn("Save Dashboard Settings", popup_html)

        for popup_css in (nyx_css, nyxify_css):
            self.assertIn("width: 22px", popup_css)
            self.assertIn("height: 22px", popup_css)
            self.assertIn("font-size: 18px", popup_css)

        for popup_js in (nyx_js, nyxify_js):
            self.assertNotIn("savePopupSettingsButton", popup_js)
            self.assertIn("function schedulePopupSettingsSave()", popup_js)
            self.assertIn("function flushPopupSettingsSave()", popup_js)
            self.assertIn('element.addEventListener("input", schedulePopupSettingsSave);', popup_js)
            self.assertIn('element.addEventListener("blur", flushPopupSettingsSave);', popup_js)
            self.assertIn('element.addEventListener("change", flushPopupSettingsSave);', popup_js)
            self.assertIn("flushPopupSettingsSave();", popup_js)

        self.assertIn('const pushAdspowerIdEnabled = getCheckedSetting("popupPushAdspowerIdToggle", "pushAdspowerIdEnabled", undefined);', nyxify_js)
        self.assertIn('const adspowerTagsEnabled = getCheckedSetting("popupAdspowerTagsToggle", "adspowerTagsEnabled", undefined);', nyxify_js)
        self.assertIn("if (pushAdspowerIdEnabled !== undefined)", nyxify_js)
        self.assertIn("if (adspowerTagsEnabled !== undefined)", nyxify_js)

    def test_content_script_locks_selected_email_and_phone_providers(self):
        content = (ROOT / "nyxify_extension" / "content.js").read_text(encoding="utf-8")

        # The popup segmented controls must actively keep SnapBoard on either
        # side of the provider toggle, not only the G5/TV side.
        self.assertIn("function findAMProviderButton()", content)
        self.assertIn("function lockProviderToAM()", content)
        self.assertIn("function findTVProviderButton()", content)
        self.assertIn("function lockProviderToTV()", content)
        self.assertIn("function findSPProviderButton()", content)
        self.assertIn("function lockProviderToSP()", content)
        self.assertIn('data-provider="gmail500"', content)
        self.assertIn('data-provider="textverified"', content)
        self.assertIn("setemailprovider('gmail500')", content)
        self.assertIn("setphoneprovider('textverified')", content)
        self.assertIn('var emailProviderLock = activeAdaptiveProviderOverride("email")', content)
        self.assertIn('if (emailProviderLock === "5m")', content)
        self.assertIn(
            'var phoneProviderLock = activeAdaptiveProviderOverride("phone") || (config.lockTV ? "tv" : "sp");',
            content,
        )
        self.assertIn(
            'if (phoneProviderLock === "tv") {\n'
            "      lockProviderToTV();\n"
            "    } else {\n"
            "      lockProviderToSP();\n"
            "    }",
            content,
        )

    def test_content_script_auto_clicks_sign_in_when_logged_out(self):
        content = (ROOT / "nyxify_extension" / "content.js").read_text(encoding="utf-8")

        # Auto-login only clicks Sign In; Chrome supplies the saved credentials.
        self.assertIn("function isLoginScreenVisible()", content)
        self.assertIn("function findSignInButton()", content)
        self.assertIn("function loginCredentialsPrefilled()", content)
        self.assertIn("function attemptAutoLogin()", content)
        self.assertIn('button[type="submit"]', content)
        self.assertIn("startAutoLoginPoll();", content)

    def test_redo_email_and_phone_wait_out_the_cooldown(self):
        content = (ROOT / "nyxify_extension" / "content.js").read_text(encoding="utf-8")

        # The redo (get-new) buttons carry a ~60s cooldown during which they are
        # disabled and clicking is a no-op — reorder must wait it out so the
        # email/number actually changes instead of silently failing.
        self.assertIn("function findRedoEmailButton(rowId)", content)
        self.assertIn("function findRedoPhoneButton(rowId)", content)
        self.assertIn("function readRedoCooldownSeconds(button)", content)
        self.assertIn("function isRedoOnCooldown(button)", content)
        self.assertIn("var redoRefreshStateByKey = Object.create(null);", content)
        self.assertIn("function redoStateKey", content)
        self.assertIn("REDO_COOLDOWN_INTERNAL_MS = 65000", content)
        self.assertIn("memory.cooldownUntil", content)
        self.assertIn("SNAPBOARD_REDO_STATE_KEY", content)
        self.assertIn("await loadRedoRefreshMemory(stateKey)", content)
        self.assertIn("await persistRedoRefreshMemory()", content)
        self.assertIn("async function waitForRedoReady(", content)
        # Both reorder paths route through the cooldown wait.
        self.assertIn("waitForRedoReady(function () { return findRedoEmailButton(rowId); }, null, redoStateKey(rowId, \"email\"))", content)
        self.assertIn("waitForRedoReady(function () { return findRedoPhoneButton(rowId); }, null, redoStateKey(rowId, \"phone\"))", content)

    def test_content_script_adaptive_provider_fetch_cycles(self):
        content = (ROOT / "nyxify_extension" / "content.js").read_text(encoding="utf-8")

        self.assertIn("function providerCycle(kind)", content)
        self.assertIn('return kind === "email" ? ["am", "g5", "5m"] : ["sp", "tv"];', content)
        self.assertIn("async function activateProvider(kind, provider)", content)
        self.assertIn("setAdaptiveProviderOverride(kind, provider, ADAPTIVE_PROVIDER_LOCK_HOLD_MS);", content)
        self.assertIn("async function requestEmailFetchOnce(rowId, forceNew)", content)
        self.assertIn("async function requestPhoneFetchOnce(rowId, forceNew)", content)
        self.assertIn('persistManualProviderLock("email", provider);', content)
        self.assertIn('persistManualProviderLock("phone", provider);', content)
        self.assertIn("config.adaptiveEmailProviderEnabled !== true", content)
        self.assertIn("config.adaptivePhoneProviderEnabled !== true", content)
        self.assertIn("All adaptive email providers exhausted.", content)
        self.assertIn("All adaptive phone providers exhausted.", content)

    def test_manual_snapboard_provider_clicks_persist_provider_lock(self):
        content = (ROOT / "nyxify_extension" / "content.js").read_text(encoding="utf-8")

        self.assertIn("function captureManualProviderLock(event)", content)
        self.assertIn("event.isTrusted === false", content)
        self.assertIn("function persistManualProviderLock(kind, provider)", content)
        self.assertIn("emailProviderLock: provider", content)
        self.assertIn("lockG5: provider === \"g5\"", content)
        self.assertIn("lockTV: provider === \"tv\"", content)
        self.assertIn('type: "NYXIFY_SAVE_CONFIG"', content)
        self.assertIn('document.addEventListener("click", captureManualProviderLock, true)', content)

    def test_content_script_types_stored_login_credentials(self):
        content = (ROOT / "nyxify_extension" / "content.js").read_text(encoding="utf-8")

        # Chrome autofill does not reliably fill the SnapBoard login form, so the
        # extension types the stored credentials into any blank field, then
        # submits — and only submits once BOTH fields are populated.
        self.assertIn('var SNAPBOARD_LOGIN_KEY = "nyxifySnapboardLogin";', content)
        self.assertIn("function getSnapboardLoginCredentials()", content)
        self.assertIn("async function fillLoginCredentialsIfNeeded()", content)
        self.assertIn("function submitLoginForm(button)", content)
        self.assertIn("requestSubmit", content)
        # The prefilled gate now requires the password too (no empty submits).
        self.assertIn('var pass = document.getElementById("loginPassword");', content)
        self.assertIn("await fillLoginCredentialsIfNeeded();", content)

    def test_content_script_recovers_a_logged_out_board_on_demand(self):
        content = (ROOT / "nyxify_extension" / "content.js").read_text(encoding="utf-8")

        self.assertIn("async function ensureSnapboardLoggedIn(", content)
        self.assertIn('message.action === "ensure_logged_in"', content)

    def test_background_refresh_and_relogin_recovery(self):
        background = (ROOT / "nyxify_extension" / "background.js").read_text(encoding="utf-8")

        # A failed fetch tries an in-place re-login (typing the stored creds in
        # the content script) before any heavier reload.
        self.assertIn("async function ensureSnapboardLoggedIn(", background)
        self.assertIn('action: "ensure_logged_in"', background)
        self.assertIn("async function snapboardFetchWithRelogin(", background)
        # Only a board that was actually signed out triggers an OTP/SMS retry —
        # a "code not landed yet" empty result must not reload the whole board.
        self.assertIn("recovered.wasLoggedOut && recovered.loggedIn", background)

        def dispatcher_for(action_marker):
            # Which helper wraps this action's bridge fetch.
            tail = background.split(action_marker)[0][-400:]
            call = tail.rsplit("await ", 1)[-1]
            for name in (
                "snapboardFetchWithRefresh",
                "runVerificationCodeFetch",
                "snapboardFetchWithRelogin",
                "sendMessageToSnapboardTab",
            ):
                if name + "(" in call:
                    return name
            return call

        # email/phone can be stale ("no pending order") -> full refresh+relogin.
        self.assertEqual(dispatcher_for('action: "email_fetch"'), "snapboardFetchWithRefresh")
        self.assertEqual(dispatcher_for('action: "phone_fetch"'), "snapboardFetchWithRefresh")
        # otp/sms use a protected verification helper: relogin first, then only a
        # controlled refresh if the Check Code/SMS control itself is unresponsive.
        self.assertEqual(dispatcher_for('action: "otp"'), "runVerificationCodeFetch")
        self.assertEqual(dispatcher_for('action: "sms"'), "runVerificationCodeFetch")

    def test_background_processes_verification_before_snapboard_refresh(self):
        background = (ROOT / "nyxify_extension" / "background.js").read_text(encoding="utf-8")
        bridge_loop = background.split("async function processBridgeActionsOnce()", 1)[1].split(
            "function ensureBridgeLoop()", 1
        )[0]

        self.assertLess(bridge_loop.index('"/otp/pending"'), bridge_loop.index("await processSnapboardRefreshRequest();"))
        self.assertLess(bridge_loop.index('"/sms/pending"'), bridge_loop.index("await processSnapboardRefreshRequest();"))

    def test_background_wakes_existing_snapboard_tab_when_bridge_port_is_missing(self):
        background = (ROOT / "nyxify_extension" / "background.js").read_text(encoding="utf-8")

        self.assertIn("async function ensureSnapboardBridgeConnected()", background)
        self.assertIn('action: "bridge_ping"', background)
        self.assertIn("Waiting for SnapBoard tab", background)
        self.assertIn("const tabId = await findSnapboardTabId();", background)
        self.assertIn("await ensureSnapboardBridgeConnected()", background)
        self.assertIn("while (true)", background[background.index("function ensureBridgeLoop()"):])

    def test_content_answers_snapboard_bridge_ping(self):
        content = (ROOT / "nyxify_extension" / "content.js").read_text(encoding="utf-8")

        self.assertIn('message.action === "bridge_ping"', content)
        self.assertIn('sendResponse({ ok: true, bridge_ready: true })', content)

    def test_background_detaches_slow_email_phone_fetches_from_bridge_loop(self):
        background = (ROOT / "nyxify_extension" / "background.js").read_text(encoding="utf-8")
        bridge_loop = background.split("async function processBridgeActionsOnce()", 1)[1].split(
            "function ensureBridgeLoop()", 1
        )[0]

        self.assertIn("const emailFetchesInFlight = new Set();", background)
        self.assertIn("const phoneFetchesInFlight = new Set();", background)
        self.assertIn("startDetachedSnapboardFetch(", background)
        self.assertNotIn("await Promise.all(emailRequests.map", bridge_loop)
        self.assertNotIn("await Promise.all(phoneRequests.map", bridge_loop)

    def test_background_skips_email_phone_refresh_while_verification_code_fetch_is_active(self):
        background = (ROOT / "nyxify_extension" / "background.js").read_text(encoding="utf-8")
        fetch_with_refresh = background.split("async function snapboardFetchWithRefresh", 1)[1].split(
            "async function snapboardFetchWithRelogin", 1
        )[0]
        guard_index = fetch_with_refresh.index("if (verificationCodeFetchesInFlight.size)")
        refresh_index = fetch_with_refresh.index("const refreshed = await refreshSnapboardTab();")

        self.assertLess(guard_index, refresh_index)
        self.assertIn("return response;", fetch_with_refresh[guard_index:refresh_index])

    def test_background_passes_full_verification_timeout_to_snapboard(self):
        background = (ROOT / "nyxify_extension" / "background.js").read_text(encoding="utf-8")
        self.assertIn("const VERIFICATION_CODE_FETCH_TIMEOUT_MS = 180000;", background)
        self.assertIn("const MAX_VERIFICATION_BRIDGE_BATCH = 3;", background)

        bridge_loop = background.split("async function processBridgeActionsOnce()", 1)[1].split(
            "function ensureBridgeLoop()", 1
        )[0]
        otp_block = bridge_loop.split('action: "otp"', 1)[1].split("});", 1)[0]
        sms_block = bridge_loop.split('action: "sms"', 1)[1].split("});", 1)[0]

        self.assertIn("timeout_ms: VERIFICATION_CODE_FETCH_TIMEOUT_MS", otp_block)
        self.assertIn("timeout_ms: VERIFICATION_CODE_FETCH_TIMEOUT_MS", sms_block)
        self.assertIn('collectPendingBridgeRequests("/otp/pending", MAX_VERIFICATION_BRIDGE_BATCH)', bridge_loop)
        self.assertIn('collectPendingBridgeRequests("/sms/pending", MAX_VERIFICATION_BRIDGE_BATCH)', bridge_loop)

    def test_background_detaches_slow_otp_sms_fetches_from_bridge_loop(self):
        background = (ROOT / "nyxify_extension" / "background.js").read_text(encoding="utf-8")
        bridge_loop = background.split("async function processBridgeActionsOnce()", 1)[1].split(
            "function ensureBridgeLoop()", 1
        )[0]

        self.assertIn("const otpFetchesInFlight = new Set();", background)
        self.assertIn("const smsFetchesInFlight = new Set();", background)
        self.assertIn("startDetachedSnapboardFetch(otpFetchesInFlight", bridge_loop)
        self.assertIn("startDetachedSnapboardFetch(smsFetchesInFlight", bridge_loop)
        self.assertNotIn("const otpResponse = await runVerificationCodeFetch", bridge_loop)
        self.assertNotIn("const smsResponse = await runVerificationCodeFetch", bridge_loop)

    def test_background_focuses_existing_snapboard_during_otp_sms_wait(self):
        background = (ROOT / "nyxify_extension" / "background.js").read_text(encoding="utf-8")
        focus_helper = background.split("async function focusSnapboardForVerificationWait", 1)[1].split(
            "async function releaseSnapboardVerificationFocus", 1
        )[0]
        release_helper = background.split("async function releaseSnapboardVerificationFocus", 1)[1].split(
            "async function sendMessageToSnapboardTab", 1
        )[0]
        bridge_loop = background.split("async function processBridgeActionsOnce()", 1)[1].split(
            "function ensureBridgeLoop()", 1
        )[0]
        otp_worker = bridge_loop.split('startDetachedSnapboardFetch(otpFetchesInFlight', 1)[1].split(
            'startDetachedSnapboardFetch(smsFetchesInFlight', 1
        )[0]
        sms_worker = bridge_loop.split('startDetachedSnapboardFetch(smsFetchesInFlight', 1)[1].split(
            "await processSnapboardRefreshRequest();", 1
        )[0]

        self.assertIn("const snapboardVerificationFocusTokens = new Set();", background)
        self.assertIn("let snapboardVerificationPreviousChromeTarget = null;", background)
        self.assertIn("await getActiveChromeTabTarget();", focus_helper)
        self.assertIn("const tabId = await findSnapboardTabId();", focus_helper)
        self.assertIn("await focusChromeTabTarget({ tabId });", focus_helper)
        self.assertIn("Waiting for SnapBoard tab; OTP/SMS focus skipped.", focus_helper)
        self.assertNotIn("chrome.tabs.create", focus_helper)
        self.assertIn("if (snapboardVerificationFocusTokens.size)", release_helper)
        self.assertIn("await focusChromeTabTarget(previousTarget);", release_helper)

        self.assertLess(
            otp_worker.index('focusSnapboardForVerificationWait("OTP", otpRequest.row_key)'),
            otp_worker.index('action: "otp"'),
        )
        self.assertIn("await releaseSnapboardVerificationFocus(focusToken);", otp_worker)
        self.assertLess(
            sms_worker.index('focusSnapboardForVerificationWait("SMS", smsRequest.row_key)'),
            sms_worker.index('action: "sms"'),
        )
        self.assertIn("await releaseSnapboardVerificationFocus(focusToken);", sms_worker)

    def test_background_does_not_drop_new_port_after_snapboard_reload(self):
        background = (ROOT / "nyxify_extension" / "background.js").read_text(encoding="utf-8")

        # The old port can disconnect after the reloaded page has already
        # connected. Its callback must not delete that replacement port.
        self.assertIn("if (snapboardPorts.get(tabId) === port)", background)

    def test_snapboard_automation_continues_when_tab_is_backgrounded(self):
        content = (ROOT / "nyxify_extension" / "content.js").read_text(encoding="utf-8")

        # Auto-Fill, Full Auto username updates, and bridge refresh polling all
        # run while the SnapBoard tab is not foregrounded. The 6.6.0 visibility
        # pause stopped all of them until the page was revisited.
        self.assertNotIn("document.hidden", content)
        self.assertIn("scanObserver.observe(root, { childList: true, subtree: true, characterData: true });", content)

    def test_replacement_code_check_retries_until_the_fetch_window(self):
        content = (ROOT / "nyxify_extension" / "content.js").read_text(encoding="utf-8")
        check_fn = content.split("async function clickAuthCodeUntilFound", 1)[1].split(
            "async function rotateProxyUntilChanged", 1
        )[0]

        # A replacement request can temporarily remove the Check Code/SMS
        # control while SnapBoard re-renders. Do not fail after the old 2.5s
        # click interval; keep retrying and ignore the previous row code.
        self.assertIn("var previousCode = sms ? getSmsTextForRow(rowId) : getOtpTextForRow(rowId);", check_fn)
        self.assertIn("var rowCode = getOtpTextForRow(rowId);", content)
        self.assertIn("var rowCode = getSmsTextForRow(rowId);", content)
        self.assertNotIn("Date.now() - startedAt) >= OTP_CLICK_RETRY_INTERVAL_MS", check_fn)

    def test_content_retries_when_check_sms_click_is_ignored_ready(self):
        content = (ROOT / "nyxify_extension" / "content.js").read_text(encoding="utf-8")
        check_fn = content.split("async function clickAuthCodeUntilFound", 1)[1].split(
            "async function rotateProxyUntilChanged", 1
        )[0]

        self.assertIn("async function waitForAuthClickAcknowledgement", content)
        self.assertIn("VERIFICATION_CLICK_ACK_TIMEOUT_MS", content)
        self.assertIn("VERIFICATION_IGNORED_RECLICK_INTERVAL_MS", content)
        self.assertIn('reason: "click_ignored_ready"', content)
        self.assertIn("var ignoredClicks = 0;", check_fn)
        self.assertIn("memory.activeUntil = 0;", check_fn)
        self.assertIn("+ \", ignored_clicks=\" + ignoredClicks", check_fn)
        self.assertIn("var readyWithoutCountdown = authState.mode === \"ready\"", check_fn)
        self.assertIn("&& !readyWithoutCountdown", check_fn)

    def test_disabled_check_without_visible_countdown_does_not_arm_full_wait(self):
        content = (ROOT / "nyxify_extension" / "content.js").read_text(encoding="utf-8")
        ack_fn = content.split("async function waitForAuthClickAcknowledgement", 1)[1].split(
            "function waitlessOtpCode", 1
        )[0]
        check_fn = content.split("async function clickAuthCodeUntilFound", 1)[1].split(
            "async function rotateProxyUntilChanged", 1
        )[0]

        self.assertIn('reason: "visible_countdown"', ack_fn)
        self.assertIn('reason: "disabled_no_countdown"', ack_fn)
        self.assertIn('if (ack.reason === "visible_countdown")', check_fn)
        self.assertIn('memory.reason = ack.reason || "clicked_no_countdown";', check_fn)
        self.assertIn("memory.activeUntil = 0;", check_fn)
        self.assertIn('memory.reason = "reclicks_exhausted_no_countdown";', check_fn)
        self.assertIn("var reclicksExhaustedNoCountdown =", check_fn)
        self.assertIn("refresh_required: !reclicksExhaustedNoCountdown", check_fn)

    def test_verification_checks_are_keyed_by_submitted_email_or_phone(self):
        content = (ROOT / "nyxify_extension" / "content.js").read_text(encoding="utf-8")
        pending_otp = content.split("var codeResult = await runSnapboardVerificationCheck", 1)[1].split(
            'await fetch(apiConfig.localApiUrl + "/otp/result"', 1
        )[0]
        message_handler = content.split('if (message.action === "otp")', 1)[1].split(
            'if (message.action === "email_fetch")', 1
        )[0]
        sms_handler = content.split('if (message.action === "sms")', 1)[1].split(
            'if (message.action === "username_update")', 1
        )[0]

        self.assertIn("function verificationStateKey(rowId, kind, expectedValue)", content)
        self.assertIn("payload.request.email", pending_otp)
        self.assertIn("message.email || message.expected_email", message_handler)
        self.assertIn("message.phone || message.expected_phone", sms_handler)

    def test_background_refreshes_verification_after_message_channel_error(self):
        background = (ROOT / "nyxify_extension" / "background.js").read_text(encoding="utf-8")
        fetch_fn = background.split("async function snapboardFetchVerificationCode", 1)[1].split(
            "async function runVerificationCodeFetch", 1
        )[0]

        self.assertIn("function isSnapboardMessageChannelError(response)", background)
        self.assertIn('error.includes("message channel closed")', background)
        self.assertIn("!isSnapboardMessageChannelError(response)", fetch_fn)

    def test_content_protects_generic_refresh_during_verification_checks(self):
        content = (ROOT / "nyxify_extension" / "content.js").read_text(encoding="utf-8")
        poll_refresh = content.split("async function pollPendingSnapboardRefresh()", 1)[1].split(
            "function waitForProxyChange", 1
        )[0]
        message_handler = content.split('if (message.action === "otp")', 1)[1].split(
            'if (message.action === "username_update")', 1
        )[0]

        self.assertIn("var snapboardVerificationChecksInFlight = 0;", content)
        self.assertIn("if (snapboardVerificationChecksInFlight > 0)", poll_refresh)
        self.assertIn("return;", poll_refresh.split("window.location.reload();", 1)[0])
        self.assertIn("await runSnapboardVerificationCheck(async function ()", message_handler)

    def test_content_propagates_verification_refresh_required_and_timeout(self):
        content = (ROOT / "nyxify_extension" / "content.js").read_text(encoding="utf-8")
        message_handler = content.split('if (message.action === "otp")', 1)[1].split(
            'if (message.action === "username_update")', 1
        )[0]

        self.assertIn("function normalizeOtpFetchTimeoutMs", content)
        self.assertIn("normalizeOtpFetchTimeoutMs(message.timeout_ms)", message_handler)
        self.assertIn("refresh_required: !!codeResult.refresh_required", message_handler)
        self.assertIn("refresh_required: !!smsResult.refresh_required", message_handler)

    def test_snapboard_refresh_bridge_is_wired_in_background_and_content(self):
        background = (ROOT / "nyxify_extension" / "background.js").read_text(encoding="utf-8")
        content = (ROOT / "nyxify_extension" / "content.js").read_text(encoding="utf-8")

        self.assertIn("async function processSnapboardRefreshRequest()", background)
        self.assertIn('"/snapboard_refresh/pending"', background)
        self.assertIn('"/snapboard_refresh/result"', background)
        self.assertIn("await refreshSnapboardTab({ force: true })", background)
        self.assertIn("chrome.tabs.query", background)
        self.assertIn("https://snapboard-production.up.railway.app/*", background)

        self.assertIn("function startSnapboardRefreshPoll()", content)
        self.assertIn('"/snapboard_refresh/pending"', content)
        self.assertIn('"/snapboard_refresh/result"', content)
        self.assertIn("SNAPBOARD_REFRESH_ACK_KEY", content)
        self.assertIn("window.location.reload();", content)
        self.assertLess(
            content.index("startSnapboardRefreshPoll();"),
            content.index("startAutoLoginPoll();"),
        )

    def test_options_page_stores_snapboard_login_credentials(self):
        options_html = (ROOT / "nyxify_extension" / "options.html").read_text(encoding="utf-8")
        options_js = (ROOT / "nyxify_extension" / "options.js").read_text(encoding="utf-8")

        self.assertIn('id="snapboardLoginName"', options_html)
        self.assertIn('id="snapboardLoginPassword"', options_html)
        self.assertIn('type="password"', options_html)
        # Stored under a dedicated local key (kept out of the synced runner config).
        self.assertIn('SNAPBOARD_LOGIN_KEY = "nyxifySnapboardLogin"', options_js)
        self.assertIn("chrome.storage.local.set({", options_js)
        self.assertIn("chrome.storage.local.get(SNAPBOARD_LOGIN_KEY", options_js)

    def test_content_proxy_poll_requires_bridge_power(self):
        content = (ROOT / "nyxify_extension" / "content.js").read_text(encoding="utf-8")

        self.assertIn('var BRIDGE_POWER_KEY = "nyxsuiteBridgePower";', content)
        self.assertIn("async function isBridgePowerOn()", content)
        poll_fn = content.split("async function pollPendingProxyRotation()", 1)[1].split(
            "function readSnapboardRefreshAck", 1
        )[0]
        self.assertIn("await isBridgePowerOn()", poll_fn)

        # The final rotate click is also blocked when the bridge power gate is off.
        rotate_handler = content.split('if (message.action === "proxy_rotate")', 1)[1].split(
            "sendResponse({ ok: true, proxy: proxyResult.proxy });", 1
        )[0]
        self.assertIn("await isBridgePowerOn()", rotate_handler)
        self.assertIn("proxy rotation is blocked", rotate_handler)

    def test_background_proxy_gates_require_bridge_power(self):
        background = (ROOT / "nyxify_extension" / "background.js").read_text(encoding="utf-8")

        self.assertIn('bridgePower: "nyxsuiteBridgePower"', background)
        self.assertIn("async function isBridgePowerOn()", background)
        self.assertIn("async function refreshBridgePowerFromLiveness(force = false)", background)
        self.assertIn("BRIDGE_DASHBOARD_URL", background)
        self.assertIn("BRIDGE_POWER_PROBE_INTERVAL_MS", background)
        self.assertIn("await refreshBridgePowerFromLiveness()", background)

        prepare_rows = background.split("async function prepareProxyRows(", 1)[1].split(
            "\nasync function ", 1
        )[0]
        self.assertIn("await isBridgePowerOn()", prepare_rows)

        stored_rows = background.split("async function prepareStoredProxyRows(", 1)[1].split(
            "\nasync function ", 1
        )[0]
        self.assertIn("await isBridgePowerOn()", stored_rows)

        bridge_loop = background.split("async function processBridgeActionsOnce()", 1)[1].split(
            "function ensureBridgeLoop()", 1
        )[0]
        rotate_gate = bridge_loop.split('"/proxy/rotate_pending"', 1)[0][-200:]
        self.assertIn("isBridgePowerOn()", rotate_gate)


if __name__ == "__main__":
    unittest.main()
