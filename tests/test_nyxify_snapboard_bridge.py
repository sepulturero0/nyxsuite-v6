from pathlib import Path
import tempfile
import unittest

from core.nyxify_local_api import NyxifyLocalApiServer
from core.nyxify_task_store import NyxifyTaskStore


ROOT = Path(__file__).resolve().parents[1]


class NyxifySnapboardBridgeTests(unittest.TestCase):
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

    def test_extension_requires_expected_email_for_email_otp(self):
        content = (ROOT / "nyxify_extension" / "content.js").read_text(encoding="utf-8")

        self.assertNotIn("if (!expected) {\n      return true;", content)
        self.assertIn("Missing expected email for OTP check", content)

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
        self.assertIn("if (!payload.force && config.proxyBlockerEnabled === false && config.proxyCheckerEnabled === false) return;", content)

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
        self.assertIn('id="scanBannedButton"', popup_html)
        self.assertIn('id="removeBannedButton"', popup_html)
        self.assertIn('id="warmupBannedButton"', popup_html)
        self.assertIn('id="bannedAdspowerIds"', popup_html)
        self.assertIn("scanBannedRows", popup_js)
        self.assertIn("removeBannedRows", popup_js)
        self.assertIn("warmupBannedRows", popup_js)
        self.assertIn("formatBannedAdspowerIds", popup_js)

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
        self.assertIn(
            "if (config.lockG5) {\n"
            "      lockProviderToG5();\n"
            "    } else {\n"
            "      lockProviderToAM();\n"
            "    }",
            content,
        )
        self.assertIn(
            "if (config.lockTV) {\n"
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
        self.assertIn("async function waitForRedoReady(", content)
        # Both reorder paths route through the cooldown wait.
        self.assertIn("waitForRedoReady(function () { return findRedoEmailButton(rowId); })", content)
        self.assertIn("waitForRedoReady(function () { return findRedoPhoneButton(rowId); })", content)

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
            for name in ("snapboardFetchWithRefresh", "snapboardFetchWithRelogin", "sendMessageToSnapboardTab"):
                if name + "(" in call:
                    return name
            return call

        # email/phone can be stale ("no pending order") -> full refresh+relogin.
        self.assertEqual(dispatcher_for('action: "email_fetch"'), "snapboardFetchWithRefresh")
        self.assertEqual(dispatcher_for('action: "phone_fetch"'), "snapboardFetchWithRefresh")
        # otp/sms use the lighter relogin-only recovery (no disruptive reload).
        self.assertEqual(dispatcher_for('action: "otp"'), "snapboardFetchWithRelogin")
        self.assertEqual(dispatcher_for('action: "sms"'), "snapboardFetchWithRelogin")

    def test_snapboard_refresh_bridge_is_wired_in_background_and_content(self):
        background = (ROOT / "nyxify_extension" / "background.js").read_text(encoding="utf-8")
        content = (ROOT / "nyxify_extension" / "content.js").read_text(encoding="utf-8")

        self.assertIn("async function processSnapboardRefreshRequest()", background)
        self.assertIn('"/snapboard_refresh/pending"', background)
        self.assertIn('"/snapboard_refresh/result"', background)
        self.assertIn("await refreshSnapboardTab({ force: true })", background)
        self.assertLess(
            background.index("await processSnapboardRefreshRequest();"),
            background.index("const emailRequests"),
        )
        self.assertIn("chrome.tabs.query", background)
        self.assertIn("https://snapboard.onrender.com/*", background)

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


if __name__ == "__main__":
    unittest.main()
