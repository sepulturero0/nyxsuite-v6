import asyncio
import os
import random
import time

SNAPCHAT_SIGNUP_URL = "https://accounts.snapchat.com/v2/signup"
SNAPCHAT_PAGE_READY_TIMEOUT_MS = 120000
SNAPCHAT_POLL_INTERVAL_MS = 500
SNAPCHAT_HANDOFF_TIMEOUT_SECONDS = 300
SNAPCHAT_HANDOFF_LOG_INTERVAL_SECONDS = 30


def _env_int(name, default):
    try:
        return int(os.getenv(name, str(default)))
    except Exception:
        return int(default)


SNAPCHAT_BLANK_SHELL_MAX_REFRESH_ATTEMPTS = max(
    0, _env_int("NYXIFY_SNAPCHAT_BLANK_SHELL_MAX_REFRESH_ATTEMPTS", 3)
)
COOKIE_WARMUP_ENABLED = str(os.getenv("NYXIFY_COOKIE_WARMUP_ENABLED", "1")).strip().lower() not in {
    "0",
    "false",
    "no",
    "off",
}
COOKIE_WARMUP_MIN_SITES = max(1, _env_int("NYXIFY_COOKIE_WARMUP_MIN_SITES", 5))
COOKIE_WARMUP_MAX_SITES = max(COOKIE_WARMUP_MIN_SITES, _env_int("NYXIFY_COOKIE_WARMUP_MAX_SITES", 10))
COOKIE_WARMUP_MIN_SECONDS = max(0, _env_int("NYXIFY_COOKIE_WARMUP_MIN_SECONDS", 60))
COOKIE_WARMUP_MAX_SECONDS = max(COOKIE_WARMUP_MIN_SECONDS, _env_int("NYXIFY_COOKIE_WARMUP_MAX_SECONDS", 120))
COOKIE_WARMUP_MAX_CONCURRENT_TABS = max(1, _env_int("NYXIFY_COOKIE_WARMUP_MAX_CONCURRENT_TABS", 4))
# Hard safety cap for a single warm-up site (navigation + browsing + close). A
# site that hangs past this is force-closed and skipped so warm-up can never
# stall the run before signup. Generously above the per-site browse budget.
COOKIE_WARMUP_PER_SITE_HARD_TIMEOUT = max(
    30, _env_int("NYXIFY_COOKIE_WARMUP_PER_SITE_HARD_TIMEOUT", 90))
# Absolute cap for the whole warm-up phase, regardless of per-site timing —
# after this the remaining tabs are dropped and the signup proceeds.
COOKIE_WARMUP_TOTAL_HARD_TIMEOUT = max(
    60, _env_int("NYXIFY_COOKIE_WARMUP_TOTAL_HARD_TIMEOUT", 240))
# The curated warm-up pool lives in the lightweight config module so the
# dashboard editor and the runner share one source of truth. Kept here under the
# original name (tests patch this attribute; the resolver samples from it when no
# custom list is saved).
from core.nyxify_runtime_config import DEFAULT_COOKIE_WARMUP_SITES

COOKIE_WARMUP_GOOD_WEBSITES = tuple(DEFAULT_COOKIE_WARMUP_SITES)

from playwright.async_api import async_playwright
from core.browser_window import maximize_browser_window
from core.browser_theme import apply_dark_mode_preferences, apply_dark_mode_to_page

KEEP_ENABLED_EXTENSION_TOKENS = (
    "dark reader",
    "dark mode",
    "night eye",
    "night mode",
    "midnight lizard",
    "super dark mode",
    "stylus",
    "stylebot",
    "turn off the lights",
)


async def _is_locator_visible(locator):
    try:
        return await locator.first.is_visible()
    except Exception:
        return False


async def _click_locator_with_fallback(page, locator):
    button = locator.first

    try:
        await button.scroll_into_view_if_needed()
    except Exception:
        pass

    try:
        await button.click()
        return True
    except Exception:
        pass

    try:
        await button.click(force=True)
        return True
    except Exception:
        pass

    try:
        handle = await button.element_handle()
        if handle is not None:
            await page.evaluate("(el) => el.click()", handle)
            return True
    except Exception:
        pass

    return False


async def _is_any_locator_visible(locators):
    for locator in locators:
        if await _is_locator_visible(locator):
            return True
    return False


def _is_snapchat_signup_url(url):
    return "accounts.snapchat.com/v2/signup" in str(url or "")


def _is_adspower_start_url(url):
    return "start.adspower.net" in str(url or "").strip().lower()


def _format_context_page_snapshot(context):
    pages = []
    for index, page in enumerate(getattr(context, "pages", []) or []):
        try:
            pages.append(f"{index}:{page.url}")
        except Exception:
            pages.append(f"{index}:<unreadable>")
    return "[" + ", ".join(pages) + "]"


async def _is_snapchat_signup_page_usable(page):
    try:
        if not _is_snapchat_signup_url(page.url):
            return False
    except Exception:
        return False

    try:
        visible_core_fields = 0
        for selector in ("#firstname", "#day", "#year", "#username", "#password"):
            try:
                if await page.locator(selector).first.is_visible():
                    visible_core_fields += 1
            except Exception:
                continue
        if visible_core_fields >= 3:
            return True
    except Exception:
        pass

    try:
        if await page.locator(
            "div[class*='InitialSignupForm_formWrapper'], form[class*='InitialSignupForm_form']"
        ).first.is_visible():
            return True
    except Exception:
        pass

    try:
        if await _is_any_locator_visible(
            [
                page.locator("[data-testid='mwp-cookie-landing-screen']"),
                page.locator("[data-testid='mwp-cookie-modal-body']"),
                page.locator(".cookie-landing-screen"),
                page.locator("[class*='cookie-landing-screen']"),
                page.get_by_role("button", name="Accept All"),
                page.locator("button:has-text('Accept All')"),
            ]
        ):
            return True
    except Exception:
        pass

    return False


async def _is_blank_snapchat_signup_shell(page):
    try:
        current_url = str(page.url or "").strip().lower()
    except Exception:
        current_url = ""
    if not _is_snapchat_signup_url(current_url):
        return False

    try:
        return bool(
            await page.evaluate(
                """
                () => {
                    if (!["interactive", "complete"].includes(document.readyState)) {
                        return false;
                    }
                    const isVisible = (node) => {
                        if (!node) return false;
                        const style = window.getComputedStyle(node);
                        if (!style) return false;
                        if (style.display === "none" || style.visibility === "hidden" || style.opacity === "0") {
                            return false;
                        }
                        const rect = node.getBoundingClientRect();
                        return rect.width > 0 && rect.height > 0;
                    };
                    const controls = Array.from(document.querySelectorAll(
                        "#firstname, #username, input, button, form, select, textarea, a[href], [role='button']"
                    ));
                    if (controls.some(isVisible)) {
                        return false;
                    }
                    const bodyText = String((document.body && document.body.innerText) || "")
                        .replace(/\\s+/g, " ")
                        .trim();
                    const visibleElements = Array.from(document.body ? document.body.querySelectorAll("*") : [])
                        .filter(isVisible);
                    const hasSnapchatLogoOnly = visibleElements.some((node) => {
                        const attrs = [
                            node.getAttribute("alt"),
                            node.getAttribute("aria-label"),
                            node.getAttribute("title"),
                            node.getAttribute("src"),
                            node.id,
                            typeof node.className === "string" ? node.className : "",
                        ].join(" ").toLowerCase();
                        return attrs.includes("snapchat") || attrs.includes("ghost");
                    });
                    return bodyText.length <= 80 && (visibleElements.length <= 1 || hasSnapchatLogoOnly);
                }
                """
            )
        )
    except Exception:
        return False


async def _refresh_signup_after_blank_shell(page, logger, profile_id, attempt):
    if logger:
        logger.warning(
            f"Snapchat signup opened to a blank logo shell for AdsPower profile {profile_id}; "
            f"refreshing signup page (attempt {attempt}/{SNAPCHAT_BLANK_SHELL_MAX_REFRESH_ATTEMPTS})."
        )
    try:
        await page.reload(wait_until="domcontentloaded", timeout=45000)
        return True
    except Exception as exc:
        if logger:
            logger.warning(
                f"Snapchat signup blank-shell refresh failed for AdsPower profile {profile_id}: {exc}. "
                "Trying direct signup navigation."
            )
        try:
            await page.goto(SNAPCHAT_SIGNUP_URL, wait_until="domcontentloaded", timeout=45000)
            return True
        except Exception as exc2:
            if logger:
                logger.warning(
                    f"Snapchat signup blank-shell re-navigation failed for AdsPower profile {profile_id}: {exc2}"
                )
    return False


async def _wait_for_usable_signup_page(context, preferred_page, logger, profile_id, deadline=None):
    loop = asyncio.get_running_loop()
    if deadline is None:
        deadline = loop.time() + SNAPCHAT_HANDOFF_TIMEOUT_SECONDS
    next_log_at = 0
    last_stage = "waiting_for_signup_page"
    blank_shell_refresh_attempts = 0

    while True:
        now = loop.time()
        if now >= deadline:
            page_snapshot = _format_context_page_snapshot(context)
            raise RuntimeError(
                f"Snapchat signup handoff timed out after {SNAPCHAT_HANDOFF_TIMEOUT_SECONDS}s "
                f"for AdsPower profile {profile_id}. last_stage={last_stage}; pages={page_snapshot}"
            )

        pages = list(getattr(context, "pages", []) or [])
        if preferred_page in pages:
            pages.remove(preferred_page)
            pages.insert(0, preferred_page)

        for page in pages:
            try:
                page_url = str(page.url or "")
            except Exception:
                continue

            if not _is_snapchat_signup_url(page_url):
                continue

            last_stage = f"signup_url_seen:{page_url[:120]}"
            if await _is_snapchat_signup_page_usable(page):
                if logger:
                    logger.info(
                        f"Detected usable Snapchat signup page for AdsPower profile {profile_id}. "
                        f"url={page_url}"
                    )
                return page
            if (
                blank_shell_refresh_attempts < SNAPCHAT_BLANK_SHELL_MAX_REFRESH_ATTEMPTS
                and await _is_blank_snapchat_signup_shell(page)
            ):
                blank_shell_refresh_attempts += 1
                last_stage = f"blank_signup_shell_refresh:{blank_shell_refresh_attempts}"
                await _refresh_signup_after_blank_shell(
                    page, logger, profile_id, blank_shell_refresh_attempts
                )
                await page.wait_for_timeout(1500)
                break

        if logger and now >= next_log_at:
            logger.info(
                f"Waiting for Snapchat signup handoff for AdsPower profile {profile_id}. "
                f"pages={_format_context_page_snapshot(context)}"
            )
            next_log_at = now + SNAPCHAT_HANDOFF_LOG_INTERVAL_SECONDS

        await asyncio.sleep(SNAPCHAT_POLL_INTERVAL_MS / 1000)


async def _wait_for_snapchat_signup_ready(signup_page):
    remaining_ms = SNAPCHAT_PAGE_READY_TIMEOUT_MS
    blank_shell_refresh_attempts = 0

    while remaining_ms > 0:
        try:
            await signup_page.wait_for_url("**accounts.snapchat.com/v2/signup*", timeout=min(remaining_ms, 5000))
        except Exception:
            pass

        if _is_snapchat_signup_url(signup_page.url):
            form_visible = False
            cookie_visible = False

            try:
                form_visible = await signup_page.locator(
                    "div[class*='InitialSignupForm_formWrapper'], form[class*='InitialSignupForm_form'], #firstname"
                ).first.is_visible()
            except Exception:
                form_visible = False

            try:
                cookie_visible = await signup_page.locator(
                    "button:has-text('Accept All'), .cookie-landing-screen button:has-text('Accept All'), "
                    "button.sdsm-button.button-compact.button-primary:has-text('Accept All')"
                ).first.is_visible()
            except Exception:
                cookie_visible = False

            if form_visible or cookie_visible:
                return

            if (
                blank_shell_refresh_attempts < SNAPCHAT_BLANK_SHELL_MAX_REFRESH_ATTEMPTS
                and await _is_blank_snapchat_signup_shell(signup_page)
            ):
                blank_shell_refresh_attempts += 1
                await _refresh_signup_after_blank_shell(
                    signup_page, logger=None, profile_id="", attempt=blank_shell_refresh_attempts
                )
                await signup_page.wait_for_timeout(1500)
                remaining_ms -= 1500
                continue

        await signup_page.wait_for_timeout(SNAPCHAT_POLL_INTERVAL_MS)
        remaining_ms -= SNAPCHAT_POLL_INTERVAL_MS

    raise RuntimeError("Snapchat signup page did not become ready in time.")


async def _accept_snapchat_cookies_if_present(signup_page, logger, profile_id, timeout_ms=SNAPCHAT_PAGE_READY_TIMEOUT_MS):
    remaining_ms = max(500, int(timeout_ms or SNAPCHAT_PAGE_READY_TIMEOUT_MS))
    cookie_seen = False
    clear_polls = 0
    modal_seen_logged = False
    button_seen_logged = False
    candidate_locators = [
        signup_page.locator("xpath=//button[normalize-space(.)='Accept All' or .//span[normalize-space(.)='Accept All']]"),
        signup_page.locator("div[class*='modal-footer'] button.sdsm-button.button-compact.button-primary"),
        signup_page.locator("[data-testid='mwp-cookie-modal-body']").locator("xpath=ancestor::div[contains(@class,'cookie-landing-screen') or contains(@class,'sdsm-modal-content')][1]//button[contains(@class,'button-primary')]"),
        signup_page.locator("button.sdsm-button.button-compact.button-primary:has-text('Accept All')"),
        signup_page.get_by_role("button", name="Accept All"),
        signup_page.locator("button:has-text('Accept All')"),
        signup_page.locator("span:has-text('Accept All')").locator("xpath=ancestor::button[1]"),
    ]
    cookie_modal_locators = [
        signup_page.locator("[data-testid='mwp-cookie-landing-screen']"),
        signup_page.locator("[data-testid='mwp-cookie-modal-body']"),
        signup_page.locator(".cookie-landing-screen"),
        signup_page.locator("[class*='cookie-landing-screen']"),
        signup_page.locator(".sdsm-modal-content"),
    ]
    signup_form = signup_page.locator(
        "div[class*='InitialSignupForm_formWrapper'], form[class*='InitialSignupForm_form'], #firstname"
    ).first
    core_field_locators = [
        signup_page.locator("#firstname"),
        signup_page.locator("#day"),
        signup_page.locator("#year"),
        signup_page.locator("#username"),
        signup_page.locator("#password"),
    ]

    while remaining_ms > 0:
        try:
            if await signup_form.is_visible():
                visible_core_fields = 0
                for locator in core_field_locators:
                    try:
                        if await locator.first.is_visible():
                            visible_core_fields += 1
                    except Exception:
                        continue

                if visible_core_fields >= 3:
                    if logger:
                        logger.info(
                            f"Snapchat signup form is interactive for AdsPower profile {profile_id}. "
                            f"Proceeding without waiting for additional cookie checks. "
                            f"cookie_seen={cookie_seen}, visible_core_fields={visible_core_fields}"
                        )
                    return cookie_seen
        except Exception:
            pass

        cookie_modal_visible = await _is_any_locator_visible(cookie_modal_locators)
        button_visible = await _is_any_locator_visible(candidate_locators)
        button_clicked = False

        if cookie_modal_visible and not modal_seen_logged and logger:
            logger.info(f"Detected Snapchat cookie modal for AdsPower profile {profile_id}.")
            modal_seen_logged = True

        if button_visible and not button_seen_logged and logger:
            logger.info(f"Detected Snapchat 'Accept All' button for AdsPower profile {profile_id}.")
            button_seen_logged = True

        for locator in candidate_locators:
            try:
                button = locator.first
                button_text = ""
                try:
                    button_text = ((await button.inner_text()) or "").strip()
                except Exception:
                    button_text = ""

                if not await button.is_visible():
                    continue

                if button_text and "accept all" not in button_text.lower():
                    continue

                cookie_seen = True

                if not await _click_locator_with_fallback(signup_page, locator):
                    continue

                button_clicked = True

                await signup_page.wait_for_timeout(750)

                if not await _is_any_locator_visible(cookie_modal_locators) and not await _is_locator_visible(locator):
                    if logger:
                        logger.info(f"Accepted Snapchat cookie popup for AdsPower profile {profile_id}.")
                    return True
            except Exception:
                continue

        if not button_clicked:
            try:
                clicked_via_dom = await signup_page.evaluate(
                    """
                    () => {
                        const candidates = Array.from(document.querySelectorAll('button'));
                        const button = candidates.find((node) => (node.innerText || node.textContent || '').trim().toLowerCase() === 'accept all');
                        if (!button) {
                            return false;
                        }
                        button.click();
                        return true;
                    }
                    """
                )
                if clicked_via_dom:
                    cookie_seen = True
                    await signup_page.wait_for_timeout(750)
                    if not await _is_any_locator_visible(cookie_modal_locators):
                        if logger:
                            logger.info(f"Accepted Snapchat cookie popup for AdsPower profile {profile_id} via DOM fallback.")
                        return True
            except Exception:
                pass

        try:
            if await signup_form.is_visible():
                cookie_still_visible = await _is_any_locator_visible(cookie_modal_locators)

                if not cookie_still_visible:
                    clear_polls += 1
                else:
                    clear_polls = 0

                if clear_polls >= 6:
                    if logger:
                        logger.info(
                            f"{'Cookie popup did not appear' if not cookie_seen else 'Cookie popup no longer blocks the page'} "
                            f"for AdsPower profile {profile_id}."
                        )
                    return cookie_seen
        except Exception:
            pass

        try:
            await signup_page.wait_for_timeout(SNAPCHAT_POLL_INTERVAL_MS)
        except Exception as exc:
            raise RuntimeError(
                f"Snapchat cookie handling interrupted because the page closed for AdsPower profile {profile_id}: {exc}"
            ) from exc
        remaining_ms -= SNAPCHAT_POLL_INTERVAL_MS

    if logger:
        logger.warning(
            f"Snapchat cookie handling timed out for AdsPower profile {profile_id}. "
            f"cookie_seen={cookie_seen}, final_url={signup_page.url}"
        )
    return cookie_seen


async def _open_snapchat_signup_in_new_tab(context, logger, profile_id):
    handoff_deadline = asyncio.get_running_loop().time() + SNAPCHAT_HANDOFF_TIMEOUT_SECONDS
    signup_page = await context.new_page()
    await apply_dark_mode_to_page(signup_page, logger=logger)

    try:
        await signup_page.bring_to_front()
    except Exception:
        pass

    try:
        remaining_ms = max(1000, int((handoff_deadline - asyncio.get_running_loop().time()) * 1000))
        await signup_page.goto(SNAPCHAT_SIGNUP_URL, wait_until="commit", timeout=min(45000, remaining_ms))
    except Exception as exc:
        current_url = str(signup_page.url or "")
        if logger:
            logger.warning(
                f"Snapchat signup navigation raised for AdsPower profile {profile_id}. "
                f"Continuing handoff watcher so manual refresh can recover. "
                f"current_url={current_url}, error={exc}"
            )

    signup_page = await _wait_for_usable_signup_page(
        context,
        signup_page,
        logger,
        profile_id,
        deadline=handoff_deadline,
    )
    cookies_accepted = await _accept_snapchat_cookies_if_present(signup_page, logger, profile_id, timeout_ms=5000)

    try:
        await signup_page.wait_for_load_state("domcontentloaded", timeout=5000)
    except Exception as exc:
        if logger:
            logger.warning(
                f"Snapchat domcontentloaded wait timed out for AdsPower profile {profile_id}. "
                f"Continuing because the page is already interactive enough. error={exc}"
            )

    # Do not block on networkidle here. Snapchat is interactive before it settles,
    # and waiting for networkidle delays the fill while the page is already usable.
    await signup_page.wait_for_timeout(500)

    cookies_accepted = (
        await _accept_snapchat_cookies_if_present(signup_page, logger, profile_id, timeout_ms=2500)
        or cookies_accepted
    )

    final_url = signup_page.url
    if not _is_snapchat_signup_url(final_url):
        raise RuntimeError(f"New-tab signup navigation did not land on Snapchat signup. Final URL: {final_url}")

    if logger:
        logger.info(
            f"Opened Snapchat signup page for AdsPower profile {profile_id} in a new tab. "
            f"final_url={final_url}, cookies_accepted={cookies_accepted}"
        )

    return {
        "url": final_url,
        "method": "new_tab",
        "cookies_accepted": cookies_accepted,
        "page": signup_page,
    }


async def _open_snapchat_signup_with_timeout(context, logger, profile_id):
    return await _open_snapchat_signup_in_new_tab(context, logger, profile_id)


async def open_snapchat_signup(context, logger, profile_id):
    return await _open_snapchat_signup_with_timeout(context, logger, profile_id)


async def _safe_close_page(page):
    try:
        if page is not None and not page.is_closed():
            # run_before_unload=False skips any beforeunload handler, and the
            # timeout guards against page.close() itself hanging (seen on
            # Windows when a site holds the renderer busy) — after which the
            # whole-context cleanup still drops the tab.
            try:
                await asyncio.wait_for(page.close(run_before_unload=False), timeout=8)
            except (asyncio.TimeoutError, TypeError):
                await asyncio.wait_for(page.close(), timeout=8)
    except Exception:
        pass


async def clear_unrelated_tabs_except_adspower(context, logger=None, profile_id=""):
    closed_urls = []
    kept_urls = []

    for page in list(getattr(context, "pages", []) or []):
        try:
            page_url = str(page.url or "")
        except Exception:
            page_url = ""

        if _is_adspower_start_url(page_url):
            kept_urls.append(page_url)
            continue

        await _safe_close_page(page)
        closed_urls.append(page_url or "<unknown>")

    if logger and closed_urls:
        logger.info(
            f"Cleared unrelated tabs before signup for AdsPower profile {profile_id}: "
            f"closed={len(closed_urls)}, kept_adspower={len(kept_urls)}"
        )

    return {
        "closed": len(closed_urls),
        "kept": len(kept_urls),
        "closed_urls": closed_urls,
        "kept_urls": kept_urls,
    }


def _auto_dismiss_dialogs(page, logger=None, profile_id="", site_url=""):
    """Auto-dismiss any JS dialog (beforeunload / alert / confirm / prompt) a
    warm-up page raises.

    A random-navigation click during warm-up can trigger a ``beforeunload``
    confirm ("Leave site?") or a modal alert. Left unanswered it blocks every
    subsequent page call — including ``page.close()`` — so the tab never closes
    and the signup never proceeds (the reported Windows hang, which manually
    closing the tab worked around). Dismissing them keeps the page responsive."""
    def _on_dialog(dialog):
        try:
            asyncio.ensure_future(dialog.dismiss())
        except Exception:
            try:
                asyncio.ensure_future(dialog.accept())
            except Exception:
                pass
        if logger:
            try:
                logger.debug(
                    f"Auto-dismissed {dialog.type} dialog during warm-up for "
                    f"{profile_id} at {site_url}."
                )
            except Exception:
                pass

    try:
        page.on("dialog", _on_dialog)
    except Exception:
        pass


async def accept_cookie_consent_if_present(page, logger=None, profile_id="", site_url=""):
    try:
        clicked = await page.evaluate(
            """
            () => {
                const strongAcceptPatterns = [
                    /^accept$/i,
                    /^accept all$/i,
                    /accept (all )?(cookies|cookie)/i,
                    /allow all/i,
                    /^agree$/i,
                    /i agree/i,
                    /consent/i,
                ];
                const contextualAcceptPatterns = [/got it/i, /^ok$/i, /^okay$/i, /continue/i];
                const avoidPatterns = [
                    /reject/i,
                    /decline/i,
                    /deny/i,
                    /manage/i,
                    /settings/i,
                    /preferences/i,
                    /customi[sz]e/i,
                    /learn more/i,
                    /subscribe/i,
                    /sign in/i,
                    /log in/i,
                ];
                const isVisible = (node) => {
                    if (!node) return false;
                    const style = window.getComputedStyle(node);
                    if (!style || style.display === "none" || style.visibility === "hidden" || style.opacity === "0") {
                        return false;
                    }
                    const rect = node.getBoundingClientRect();
                    return rect.width > 8 && rect.height > 8;
                };
                const textOf = (node) => [
                    node.innerText,
                    node.textContent,
                    node.getAttribute && node.getAttribute("aria-label"),
                    node.getAttribute && node.getAttribute("title"),
                    node.getAttribute && node.getAttribute("value"),
                ].filter(Boolean).join(" ").replace(/\\s+/g, " ").trim();
                const contextText = (node) => {
                    let parent = node;
                    for (let i = 0; i < 4 && parent; i += 1) {
                        const text = textOf(parent);
                        if (/cookie|privacy|consent|gdpr|ccpa/i.test(text)) return text;
                        parent = parent.parentElement;
                    }
                    return "";
                };
                const nodes = Array.from(document.querySelectorAll(
                    "button, [role='button'], input[type='button'], input[type='submit'], a[href]"
                ));
                const candidates = nodes
                    .filter(isVisible)
                    .map((node) => ({ node, text: textOf(node), ctx: contextText(node) }))
                    .filter(({ text, ctx }) => text && (
                        strongAcceptPatterns.some((pattern) => pattern.test(text)) ||
                        (
                            /cookie|privacy|consent|gdpr|ccpa/i.test(ctx) &&
                            contextualAcceptPatterns.some((pattern) => pattern.test(text))
                        )
                    ))
                    .filter(({ text }) => !avoidPatterns.some((pattern) => pattern.test(text)))
                    .sort((a, b) => Number(/cookie|privacy|consent|gdpr|ccpa/i.test(b.ctx))
                        - Number(/cookie|privacy|consent|gdpr|ccpa/i.test(a.ctx)));
                if (!candidates.length) return false;
                const target = candidates[0].node;
                target.scrollIntoView({ block: "center", inline: "center" });
                target.dispatchEvent(new MouseEvent("mousemove", { bubbles: true, view: window }));
                target.dispatchEvent(new MouseEvent("mousedown", { bubbles: true, cancelable: true, view: window }));
                target.dispatchEvent(new MouseEvent("mouseup", { bubbles: true, cancelable: true, view: window }));
                target.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, view: window }));
                try { target.click(); } catch (_error) {}
                return true;
            }
            """
        )
    except Exception as exc:
        if logger:
            logger.debug(
                f"Cookie consent scan failed for {profile_id} at {site_url}: {exc}"
            )
        return False

    if clicked and logger:
        logger.info(f"Accepted cookie prompt during warm-up for {profile_id} at {site_url}.")
    return bool(clicked)


async def _warm_one_cookie_site(context, url, duration_seconds, logger, profile_id):
    page = await context.new_page()
    # Never let a beforeunload/alert/confirm from a stray navigation click block
    # the tab (and its close) — the Windows warm-up hang.
    _auto_dismiss_dialogs(page, logger=logger, profile_id=profile_id, site_url=url)
    try:
        await apply_dark_mode_to_page(page, logger=logger)
    except Exception:
        pass

    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=20000)
    except Exception as exc:
        if logger:
            logger.warning(f"Cookie warm-up navigation failed for {profile_id} at {url}: {exc}")
        await _safe_close_page(page)
        return False

    consent_clicked = await accept_cookie_consent_if_present(page, logger, profile_id, url)
    deadline = time.monotonic() + max(2, float(duration_seconds or 0))
    while time.monotonic() < deadline:
        try:
            await page.evaluate(
                "(amount) => window.scrollBy({ top: amount, behavior: 'smooth' })",
                random.randint(180, 900),
            )
        except Exception:
            pass

        if not consent_clicked:
            consent_clicked = await accept_cookie_consent_if_present(page, logger, profile_id, url)

        if random.random() < 0.35:
            try:
                await page.evaluate(
                    """
                    () => {
                        const isVisible = (node) => {
                            if (!node) return false;
                            const style = window.getComputedStyle(node);
                            if (!style || style.display === "none" || style.visibility === "hidden") {
                                return false;
                            }
                            const rect = node.getBoundingClientRect();
                            return rect.width > 8
                                && rect.height > 8
                                && rect.top >= 0
                                && rect.top < window.innerHeight
                                && rect.left >= 0
                                && rect.left < window.innerWidth;
                        };
                        const sameOrigin = (node) => {
                            if (!node || node.tagName !== "A") return true;
                            try {
                                const next = new URL(node.href, window.location.href);
                                return next.origin === window.location.origin;
                            } catch (_error) {
                                return false;
                            }
                        };
                        const candidates = Array.from(document.querySelectorAll("a[href], button, [role='button']"))
                            .filter((node) => isVisible(node) && sameOrigin(node));
                        if (!candidates.length) return false;
                        const target = candidates[Math.floor(Math.random() * candidates.length)];
                        target.scrollIntoView({ block: "center", inline: "center" });
                        target.dispatchEvent(new MouseEvent("mousemove", { bubbles: true, view: window }));
                        target.dispatchEvent(new MouseEvent("mousedown", { bubbles: true, cancelable: true, view: window }));
                        target.dispatchEvent(new MouseEvent("mouseup", { bubbles: true, cancelable: true, view: window }));
                        target.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, view: window }));
                        try { target.click(); } catch (_error) {}
                        return true;
                    }
                    """
                )
            except Exception:
                pass

        try:
            await page.wait_for_timeout(random.randint(900, 2600))
        except Exception:
            break

    await _safe_close_page(page)
    return True


def _resolve_cookie_warmup_settings():
    """Merge the dashboard config over the env/module defaults.

    Returns ``(enabled, sites)`` where ``sites`` is the pool to sample from.
    The dashboard toggle wins over the ``NYXIFY_COOKIE_WARMUP_ENABLED`` env
    default, and a non-empty custom site list replaces the built-in pool.
    """
    enabled = COOKIE_WARMUP_ENABLED
    sites = list(COOKIE_WARMUP_GOOD_WEBSITES)
    try:
        from core.nyxify_runtime_config import load_nyxify_config

        config = load_nyxify_config()
        if "cookie_warmup_enabled" in config:
            enabled = bool(config.get("cookie_warmup_enabled"))
        custom = config.get("cookie_warmup_sites") or []
        custom = [str(url).strip() for url in custom if str(url).strip()]
        if custom:
            sites = custom
    except Exception:
        pass
    return enabled, sites


async def _warm_ads_profile_cookies(context, logger, profile_id):
    warmup_enabled, warmup_sites = _resolve_cookie_warmup_settings()
    if not warmup_enabled or COOKIE_WARMUP_MAX_SECONDS <= 0 or not warmup_sites:
        if logger and not warmup_enabled:
            logger.info(f"Cookie warm-up disabled for {profile_id}; skipping.")
        return {"enabled": False, "visited": []}

    # Clamp the sample window to the available pool so a short custom list
    # (e.g. a single site) never trips random.randint's empty-range error.
    pool_size = len(warmup_sites)
    min_sites = min(COOKIE_WARMUP_MIN_SITES, pool_size)
    max_sites = min(COOKIE_WARMUP_MAX_SITES, pool_size)
    site_count = random.randint(min_sites, max_sites)
    total_seconds = random.randint(COOKIE_WARMUP_MIN_SECONDS, COOKIE_WARMUP_MAX_SECONDS)
    selected_sites = random.sample(warmup_sites, site_count)
    seconds_per_site = max(2, total_seconds / max(1, site_count))
    concurrency = max(1, min(COOKIE_WARMUP_MAX_CONCURRENT_TABS, site_count))
    baseline_pages = set(getattr(context, "pages", []) or [])
    visited = []

    if logger:
        logger.info(
            f"Starting AdsPower cookie warm-up for {profile_id}: "
            f"{site_count} sites over ~{total_seconds}s, max {concurrency} tabs at once."
        )

    semaphore = asyncio.Semaphore(concurrency)

    async def visit_site(url):
        async with semaphore:
            try:
                # Hard per-site cap: a site that wedges (unclosable dialog, stuck
                # renderer) can never hold the warm-up open past this.
                ok = await asyncio.wait_for(
                    _warm_one_cookie_site(context, url, seconds_per_site, logger, profile_id),
                    timeout=COOKIE_WARMUP_PER_SITE_HARD_TIMEOUT,
                )
            except asyncio.TimeoutError:
                if logger:
                    logger.warning(
                        f"Cookie warm-up site timed out (hard cap) for {profile_id} at {url}; "
                        "skipping so signup can proceed."
                    )
                return None
            except Exception as exc:
                if logger:
                    logger.warning(f"Cookie warm-up worker failed for {profile_id} at {url}: {exc}")
                return None
            return url if ok else None

    try:
        # Absolute cap for the whole phase: even if several sites hang at once,
        # warm-up yields to the signup after this. Partial cookies are fine —
        # warm-up is best-effort priming, never a hard prerequisite.
        try:
            results = await asyncio.wait_for(
                asyncio.gather(*(visit_site(url) for url in selected_sites)),
                timeout=COOKIE_WARMUP_TOTAL_HARD_TIMEOUT,
            )
            visited = [url for url in results if url]
        except asyncio.TimeoutError:
            if logger:
                logger.warning(
                    f"Cookie warm-up hit the total hard cap for {profile_id}; "
                    "proceeding to signup with whatever cookies were primed."
                )
    finally:
        for page in list(getattr(context, "pages", []) or []):
            if page not in baseline_pages:
                await _safe_close_page(page)

    if logger:
        logger.info(
            f"Finished AdsPower cookie warm-up for {profile_id}: visited={visited}"
        )
    return {"enabled": True, "visited": visited}


async def warm_ads_profile_cookies(context, logger, profile_id):
    return await _warm_ads_profile_cookies(context, logger, profile_id)


async def _warm_then_open_snapchat_signup(context, logger, profile_id):
    return await open_snapchat_signup(context, logger, profile_id)


async def disable_profile_extensions(
    adspower,
    profile_id,
    logger,
    keep_open=True,
    keep_playwright=False,
    open_signup=True,
    disable_extensions=True,
):
    normalized_profile_id = str(profile_id or "").strip()
    if not normalized_profile_id:
        raise ValueError("AdsPower profile id is required.")

    ws_endpoint = await asyncio.to_thread(adspower.open_profile, normalized_profile_id)
    playwright = await async_playwright().start()
    browser = None

    try:
        browser = await playwright.chromium.connect_over_cdp(ws_endpoint)

        context = None
        for _ in range(20):
            if browser.contexts:
                context = browser.contexts[0]
                break
            await asyncio.sleep(0.25)

        if context is None:
            raise RuntimeError("Connected to AdsPower browser, but no context became available.")

        await maximize_browser_window(browser, logger=logger)
        await apply_dark_mode_preferences(context, logger=logger)

        # The extension turn-off step is now opt-in. When it is skipped we still
        # do all the browser/context plumbing (open profile, attach CDP, dark
        # mode, optional signup open) so the account-creation flow is unchanged —
        # we just leave the profile's extensions exactly as AdsPower configured
        # them instead of visiting chrome://extensions/ to toggle them off.
        if not disable_extensions:
            if logger:
                logger.info(
                    f"Skipping AdsPower extension turn-off for {normalized_profile_id} "
                    "(disabled by config); leaving extensions as configured."
                )
            signup_result = (
                await open_snapchat_signup(context, logger, normalized_profile_id)
                if open_signup
                else {}
            )
            payload = {
                "profile_id": normalized_profile_id,
                "ws_endpoint": ws_endpoint,
                "disabled_now": [],
                "already_disabled": [],
                "missing_toggle": [],
                "kept_enabled": [],
                "remaining_enabled": [],
                "signup_url": signup_result.get("url"),
                "signup_method": signup_result.get("method"),
                "signup_page": signup_result.get("page"),
                "context": context,
                "playwright_instance": playwright if keep_playwright else None,
                "success": True,
                "skipped": True,
            }
            if keep_playwright:
                playwright = None
            return payload

        page = await context.new_page()
        await apply_dark_mode_to_page(page, logger=logger)
        await page.goto("chrome://extensions/", wait_until="domcontentloaded")
        await page.wait_for_timeout(1500)

        last_result = None

        for attempt in range(6):
            result = await page.evaluate(
                """
                (keepEnabledTokens) => {
                    const visited = new Set();

                    function walk(node, visitor) {
                        if (!node || visited.has(node)) {
                            return;
                        }
                        visited.add(node);
                        visitor(node);

                        const children = node.querySelectorAll ? node.querySelectorAll('*') : [];
                        for (const child of children) {
                            if (child.shadowRoot) {
                                walk(child.shadowRoot, visitor);
                            }
                        }
                    }

                    function findExtensionItems() {
                        const items = [];
                        walk(document, (node) => {
                            if (node.querySelectorAll) {
                                for (const item of node.querySelectorAll('extensions-item')) {
                                    items.push(item);
                                }
                            }
                        });
                        return items;
                    }

                    function readToggleState(toggle) {
                        if (!toggle) {
                            return false;
                        }
                        if (typeof toggle.checked === 'boolean') {
                            return toggle.checked;
                        }
                        const ariaPressed = toggle.getAttribute('aria-pressed');
                        if (ariaPressed === 'true' || ariaPressed === 'false') {
                            return ariaPressed === 'true';
                        }
                        return toggle.hasAttribute('checked');
                    }

                    function readItemMetadata(item, root) {
                        const data = item && typeof item === 'object'
                            ? (item.data || item.item || item.extensionInfo || item.extensionData || null)
                            : null;
                        const descriptionNode =
                            root.querySelector('#description') ||
                            root.querySelector('[id="description"]') ||
                            root.querySelector('.description');
                        const textContent = String((root && root.textContent) || '').trim();

                        return {
                            type: String(
                                (data && (data.type || data.appType || data.extensionType || data.kind)) ||
                                item.getAttribute('type') ||
                                item.getAttribute('data-type') ||
                                ''
                            ).trim().toLowerCase(),
                            description: String((descriptionNode && descriptionNode.textContent) || '').trim().toLowerCase(),
                            text: textContent.toLowerCase(),
                        };
                    }

                    function isThemeItem(metadata) {
                        const type = String(metadata && metadata.type || '').trim().toLowerCase();
                        const description = String(metadata && metadata.description || '').trim().toLowerCase();
                        const text = String(metadata && metadata.text || '').trim().toLowerCase();
                        return (
                            type.includes('theme')
                            || description.includes('theme')
                            || text.includes(' theme ')
                            || text.includes('browser theme')
                            || text.includes('chrome theme')
                            || text.includes('appearance')
                        );
                    }

                    function shouldKeepEnabled(name) {
                        const normalized = String(name || '').trim().toLowerCase();
                        if (!normalized) {
                            return false;
                        }

                        if (keepEnabledTokens.some((token) => normalized.includes(token))) {
                            return true;
                        }

                        return (
                            (normalized.includes('dark') && (normalized.includes('mode') || normalized.includes('theme') || normalized.includes('reader'))) ||
                            (normalized.includes('night') && (normalized.includes('mode') || normalized.includes('theme')))
                        );
                    }

                    const disabledNow = [];
                    const alreadyDisabled = [];
                    const missingToggle = [];
                    const keptEnabled = [];

                    for (const item of findExtensionItems()) {
                        const root = item.shadowRoot;
                        if (!root) {
                            continue;
                        }

                        const nameNode =
                            root.querySelector('#name') ||
                            root.querySelector('.title') ||
                            root.querySelector('[id="name"]');
                        const name = (nameNode && nameNode.textContent || '').trim() || 'Unknown extension';
                        const metadata = readItemMetadata(item, root);
                        const toggle =
                            root.querySelector('#enableToggle') ||
                            root.querySelector('cr-toggle');

                        if (!toggle) {
                            missingToggle.push(name);
                            continue;
                        }

                        if (readToggleState(toggle)) {
                            if (shouldKeepEnabled(name) || isThemeItem(metadata)) {
                                keptEnabled.push(name);
                                continue;
                            }
                            toggle.click();
                            disabledNow.push(name);
                        } else {
                            alreadyDisabled.push(name);
                        }
                    }

                    const remainingEnabled = [];
                    for (const item of findExtensionItems()) {
                        const root = item.shadowRoot;
                        if (!root) {
                            continue;
                        }
                        const toggle =
                            root.querySelector('#enableToggle') ||
                            root.querySelector('cr-toggle');
                        const nameNode =
                            root.querySelector('#name') ||
                            root.querySelector('.title') ||
                            root.querySelector('[id="name"]');
                        const name = (nameNode && nameNode.textContent || '').trim() || 'Unknown extension';
                        const metadata = readItemMetadata(item, root);
                        if (toggle && readToggleState(toggle) && !shouldKeepEnabled(name) && !isThemeItem(metadata)) {
                            remainingEnabled.push(name);
                        }
                    }

                    return {
                        disabled_now: disabledNow,
                        already_disabled: alreadyDisabled,
                        missing_toggle: missingToggle,
                        kept_enabled: keptEnabled,
                        remaining_enabled: remainingEnabled,
                        item_count: findExtensionItems().length,
                    };
                }
                """,
                list(KEEP_ENABLED_EXTENSION_TOKENS),
            )

            last_result = result

            if result.get("item_count", 0) <= 0:
                await page.wait_for_timeout(1000)
                continue

            if not result.get("remaining_enabled"):
                if logger:
                    logger.info(
                        f"Disabled AdsPower profile extensions for {normalized_profile_id}: "
                        f"disabled_now={result.get('disabled_now', [])}, "
                        f"already_disabled={result.get('already_disabled', [])}, "
                        f"kept_enabled={result.get('kept_enabled', [])}"
                    )
                signup_result = (
                    await open_snapchat_signup(context, logger, normalized_profile_id)
                    if open_signup
                    else {}
                )
                payload = {
                    "profile_id": normalized_profile_id,
                    "ws_endpoint": ws_endpoint,
                    "disabled_now": result.get("disabled_now", []),
                    "already_disabled": result.get("already_disabled", []),
                    "missing_toggle": result.get("missing_toggle", []),
                    "kept_enabled": result.get("kept_enabled", []),
                    "remaining_enabled": [],
                    "signup_url": signup_result.get("url"),
                    "signup_method": signup_result.get("method"),
                    "signup_page": signup_result.get("page"),
                    "context": context,
                    "playwright_instance": playwright if keep_playwright else None,
                    "success": True,
                }
                if keep_playwright:
                    # Caller owns playwright lifetime — do NOT stop it in finally
                    playwright = None
                return payload

            await page.wait_for_timeout(800)

        remaining_enabled = (last_result or {}).get("remaining_enabled", [])
        if remaining_enabled:
            raise RuntimeError(
                "Some Chrome extensions remained enabled after automation: "
                + ", ".join(remaining_enabled)
            )

        if logger:
            logger.warning(
                f"AdsPower extension cleanup finished without a strict DOM verification for {normalized_profile_id}. "
                f"Treating as success because no enabled extensions were detected. "
                f"last_result={last_result}"
            )

        signup_result = (
            await open_snapchat_signup(context, logger, normalized_profile_id)
            if open_signup
            else {}
        )
        payload = {
            "profile_id": normalized_profile_id,
            "ws_endpoint": ws_endpoint,
            "disabled_now": (last_result or {}).get("disabled_now", []),
            "already_disabled": (last_result or {}).get("already_disabled", []),
            "missing_toggle": (last_result or {}).get("missing_toggle", []),
            "kept_enabled": (last_result or {}).get("kept_enabled", []),
            "remaining_enabled": [],
            "signup_url": signup_result.get("url"),
            "signup_method": signup_result.get("method"),
            "signup_page": signup_result.get("page"),
            "context": context,
            "playwright_instance": playwright if keep_playwright else None,
            "success": True,
            "verified": False,
        }
        if keep_playwright:
            playwright = None
        return payload
    finally:
        if browser and not keep_open:
            try:
                await browser.close()
            except Exception:
                pass
        if playwright is not None:
            try:
                await playwright.stop()
            except Exception:
                pass
