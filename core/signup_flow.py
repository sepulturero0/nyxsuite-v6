# PEP 563: keep annotations as strings so 3.10+ union syntax (e.g. "Path | None")
# is not evaluated at runtime - macOS ships Python 3.9, which would otherwise
# raise "unsupported operand type(s) for |" on import.
from __future__ import annotations

import asyncio
import os
import random
import time
from pathlib import Path

from core.signup_data import generate_birthday, get_random_name

PASSWORD = "ABC123wgmi*"

# Recovery tuning for a signup flow that never advances - the reCAPTCHA service
# is unreachable, no captcha challenge ever renders (no badge in the corner), or
# the expected signup/verification page disappears.
# We reload the page and re-enter the details up to N times; if it still won't
# progress we raise a classified error so the Nyxify runner runs its standard
# cleanup/retry (delete profile, rotate proxy, requeue as PENDING).
SIGNUP_STALL_SECONDS = int(os.getenv("NYXIFY_SIGNUP_STALL_SECONDS", "100"))
# Backstop for the "stuck on the signup page for a very long time and can't even
# click Agree and Continue" case: when the form has been sitting for this long
# and the submit button never became clickable, reload + re-enter as a last
# resort — even if a captcha is present (which suppresses the normal stall).
SIGNUP_HARD_STALL_SECONDS = int(os.getenv("NYXIFY_SIGNUP_HARD_STALL_SECONDS", "200"))
SIGNUP_MAX_REFRESH_ATTEMPTS = int(os.getenv("NYXIFY_SIGNUP_MAX_REFRESH_ATTEMPTS", "3"))
# How many times to (re-)order a verification email from SnapBoard when it has
# "no pending email order" before giving up and letting the runner retry.
EMAIL_ORDER_MAX_ATTEMPTS = int(os.getenv("NYXIFY_EMAIL_ORDER_MAX_ATTEMPTS", "4"))
# Phone numbers can be rejected before Snapchat sends an SMS. When that happens
# SnapBoard can issue a replacement number via its redo/force-new path.
PHONE_VERIFICATION_MAX_ATTEMPTS = int(os.getenv("NYXIFY_PHONE_VERIFICATION_MAX_ATTEMPTS", "2"))
WRONG_CODE_MAX_RECOVERY_ATTEMPTS = int(os.getenv("NYXIFY_WRONG_CODE_MAX_RECOVERY_ATTEMPTS", "2"))
# How many times to try "Use email instead" when a phone step appears during the
# email/OTP path. If the switch is present but never clickable, fall back to the
# phone verification path (get a number -> SMS OTP) instead of looping forever.
EMAIL_SWITCH_MAX_ATTEMPTS = int(os.getenv("NYXIFY_EMAIL_SWITCH_MAX_ATTEMPTS", "3"))
# How many times email verification may fail (wrong code / already-verified)
# before we click "Use Phone Number Instead" and switch to the phone -> SMS OTP
# verification path.
EMAIL_VERIFY_MAX_ATTEMPTS = int(os.getenv("NYXIFY_EMAIL_VERIFY_MAX_ATTEMPTS", "3"))
SIGNUP_FAST_SUBMIT_PRE_CLEAR_MS = int(os.getenv("NYXIFY_SIGNUP_FAST_SUBMIT_PRE_CLEAR_MS", "250"))
SIGNUP_FAST_SUBMIT_POST_CLEAR_MS = int(os.getenv("NYXIFY_SIGNUP_FAST_SUBMIT_POST_CLEAR_MS", "150"))
SIGNUP_FAST_SUBMIT_PAUSE_MIN_MS = int(os.getenv("NYXIFY_SIGNUP_FAST_SUBMIT_PAUSE_MIN_MS", "90"))
SIGNUP_FAST_SUBMIT_PAUSE_MAX_MS = int(os.getenv("NYXIFY_SIGNUP_FAST_SUBMIT_PAUSE_MAX_MS", "220"))
SIGNUP_FAST_SUBMIT_ENABLED_TIMEOUT_MS = int(
    os.getenv("NYXIFY_SIGNUP_FAST_SUBMIT_ENABLED_TIMEOUT_MS", "1200")
)
SIGNUP_CAPTCHALESS_SUBMIT_ENABLED_TIMEOUT_MS = int(
    os.getenv("NYXIFY_SIGNUP_CAPTCHALESS_SUBMIT_ENABLED_TIMEOUT_MS", "1000")
)
SIGNUP_FAST_SUBMIT_CLICK_TIMEOUT_MS = int(
    os.getenv("NYXIFY_SIGNUP_FAST_SUBMIT_CLICK_TIMEOUT_MS", "1000")
)
SIGNUP_JS_CLICK_TIMEOUT_MS = int(os.getenv("NYXIFY_SIGNUP_JS_CLICK_TIMEOUT_MS", "1500"))
SIGNUP_USERNAME_RETRY_SETTLE_MS = int(
    os.getenv("NYXIFY_SIGNUP_USERNAME_RETRY_SETTLE_MS", "700")
)
SIGNUP_UNABLE_TO_PROCESS_RETRY_SETTLE_MS = int(
    os.getenv("NYXIFY_SIGNUP_UNABLE_RETRY_SETTLE_MS", "300")
)

_USERNAME_TAKEN_ERROR_MARKERS = [
    "username is already taken",
    "username already taken",
    "username is taken",
    "username has been taken",
    "this username is taken",
    "this username has been taken",
    "already been taken",
    "try another username",
]

_USERNAME_INVALID_ERROR_MARKERS = [
    "invalid username",
    "letters and numbers with an optional hyphen",
    "optional hyphen, underscore, or period",
    "underscore, or period in between please",
]

_WRONG_VERIFICATION_CODE_ERROR_MARKERS = [
    "that's not the right code",
    "that is not the right code",
    "not the right code",
    "incorrect code",
    "invalid code",
    "wrong code",
    "verification code is incorrect",
    "code you entered is incorrect",
]

_UNABLE_TO_PROCESS_ERROR_MARKERS = [
    "we are sorry, we were unable to process your request",
    "unable to process your request",
    "nie udało nam się przetworzyć",
    "przetworzyć twojego polecenia",
]

_EMAIL_INPUT_SELECTORS = [
    "input[type='email']",
    "input[name='email']",
    "input[placeholder*='email' i]",
    "input[autocomplete='email']",
    "input[inputmode='email']",
    "input[aria-label*='email' i]",
]

_OTP_INPUT_SELECTORS = [
    "input[name='code']",
    "input[name='verificationCode']",
    "input[name^='otpDigits.']",
    ".EnterOTPForm_otpDigitsWrapper__gETt8 input",
    "input[maxlength='1'][name^='otpDigits']",
    "input[maxlength='6'][name*='code' i]",
    "input[placeholder*='code' i]",
    "input[data-testid*='code' i]",
]

_PHONE_COUNTRY_CODE_SELECTORS = [
    "#countryCode",
    "input[name='countryCode']",
    "input[aria-label*='country code' i]",
]

_PHONE_NUMBER_SELECTORS = [
    "#phoneNumber",
    "input[name='phoneNumber']",
    "input[autocomplete='tel-national']",
    "input[autocomplete='tel']",
    "input[inputmode='tel']",
    "input[aria-label*='phone number' i]",
    "input[placeholder*='phone number' i]",
]

_PHONE_SUBMIT_SELECTORS = [
    "button[type='submit']",
    "button:has-text('Continue')",
    "button:has-text('Next')",
    "button:has-text('Send')",
    "button:has-text('SMS')",
]

# ---------------------------------------------------------------------------
# JS helpers — set values without needing OS-level window focus
# ---------------------------------------------------------------------------

_JS_SET_INPUT = """
(args) => {
    const [selector, value] = args;
    const el = document.querySelector(selector);
    if (!el) return {ok: false, reason: 'not found'};

    el.focus();

    // React tracks value via the native property descriptor
    const proto = el.tagName === 'SELECT'
        ? window.HTMLSelectElement.prototype
        : window.HTMLInputElement.prototype;
    const nativeSetter = Object.getOwnPropertyDescriptor(proto, 'value').set;
    nativeSetter.call(el, value);

    // Fire all events React listens to
    el.dispatchEvent(new Event('focus',  {bubbles: true}));
    el.dispatchEvent(new Event('input',  {bubbles: true}));
    el.dispatchEvent(new Event('change', {bubbles: true}));
    el.dispatchEvent(new Event('blur',   {bubbles: true}));

    return {ok: true, actual: el.value};
}
"""

_JS_GET_VALUE = """
(selector) => {
    const el = document.querySelector(selector);
    return el ? el.value : null;
}
"""

_JS_CLICK = """
(selector) => {
    const el = document.querySelector(selector);
    if (!el) return false;
    el.click();
    return true;
}
"""

_JS_IS_DISABLED = """
(selector) => {
    const el = document.querySelector(selector);
    if (!el) return true;
    return el.disabled || el.hasAttribute('disabled');
}
"""


async def _safe_scroll_into_view(locator, timeout_ms: int = 4000) -> bool:
    """Scroll ``locator`` into view without letting it stall the flow.

    Playwright's ``scroll_into_view_if_needed()`` blocks for the full default
    timeout (30s) when an element is present but its actionability check never
    settles — observed on the Snapchat login/signup ``#username`` field on some
    Windows profiles, where the runner appeared "stuck on the login phase"
    (30-60s per field, humanized type then js_set fallback each waiting out the
    default). Bound it to a few seconds and treat failure as non-fatal: callers
    either set the value via JS (no scroll needed) or click with ``force=True``.
    """
    try:
        await locator.scroll_into_view_if_needed(timeout=timeout_ms)
        return True
    except Exception:
        return False


async def _js_set(page, selector: str, value: str, logger=None, label: str = "") -> bool:
    """Set an input/select value via JS (works without window focus)."""
    try:
        result = await page.evaluate(_JS_SET_INPUT, [selector, str(value)])
        if result and result.get("ok"):
            if logger and label:
                logger.info(f"Set {label} = {value!r}")
            return True
        if logger:
            logger.warning(f"_js_set {selector!r}: {result}")
    except Exception as exc:
        if logger:
            logger.warning(f"_js_set {selector!r} error: {exc}")

    # Fallback: Playwright locator.fill() — at least sets DOM value
    try:
        loc = page.locator(selector).first
        await _safe_scroll_into_view(loc)
        await loc.click()
        await loc.fill(str(value))
        actual = await loc.input_value()
        if actual == str(value):
            if logger and label:
                logger.info(f"Set {label} = {value!r} (fill fallback)")
            return True
    except Exception as exc2:
        if logger:
            logger.warning(f"_js_set fill fallback {selector!r} error: {exc2}")

    return False


def _random_delay_ms(min_ms: int = 120, max_ms: int = 420) -> int:
    low = int(min_ms)
    high = int(max_ms)
    if high < low:
        high = low
    return random.randint(low, high)


async def _human_pause(page, min_ms: int = 120, max_ms: int = 420) -> None:
    await page.wait_for_timeout(_random_delay_ms(min_ms, max_ms))


async def _human_clear_field(page, loc, logger=None) -> None:
    """Empty an input the way a person retyping a field would.

    Ctrl+A + one Backspace is unreliable on React-controlled inputs (the prior
    value sometimes survives, so the new text gets appended to the old one).
    Here we move the caret to the end and delete every remaining character,
    re-checking the value and backspacing any stragglers — so the field is
    truly empty before new text is typed.
    """
    try:
        await loc.click()
        await _human_pause(page, 90, 200)

        try:
            current = str(await loc.input_value() or "")
        except Exception:
            current = ""

        if current:
            # Fast path first: select-all then delete the selection.
            try:
                await loc.press("End")
            except Exception:
                pass
            await _human_pause(page, 40, 110)
            await loc.press("Control+A")
            await _human_pause(page, 40, 110)
            await loc.press("Delete")
            await _human_pause(page, 50, 130)

        # Verify and mop up: backspace any characters the select-all missed,
        # one keystroke at a time with a human cadence.
        for _ in range(4):
            try:
                remaining = str(await loc.input_value() or "")
            except Exception:
                remaining = ""
            if not remaining:
                break
            try:
                await loc.press("End")
            except Exception:
                pass
            for _ in range(len(remaining) + 2):
                await loc.press("Backspace")
                await page.wait_for_timeout(_random_delay_ms(40, 110))
    except Exception as exc:
        if logger:
            logger.warning(f"_human_clear_field error: {exc}")
        # Last-resort programmatic clear.
        try:
            await loc.fill("")
        except Exception:
            pass


async def _human_type_text(page, loc, text: str) -> None:
    """Type character-by-character with per-keystroke jitter and the occasional
    longer pause. Playwright's ``type(delay=N)`` applies one fixed delay to every
    keypress, which reads as robotic; varying each keystroke (and pausing every
    few characters as a person glances at the screen) looks far more natural.
    """
    for index, char in enumerate(text):
        await loc.type(char, delay=0)
        await page.wait_for_timeout(_random_delay_ms(75, 185))
        # Every handful of characters, take a brief "thinking" beat.
        if index and index % random.randint(5, 9) == 0:
            await page.wait_for_timeout(_random_delay_ms(180, 430))


async def _humanized_type(page, selector: str, value: str, logger=None, label: str = "") -> bool:
    text = str(value or "")
    try:
        loc = page.locator(selector).first
        await _safe_scroll_into_view(loc)
        await _human_clear_field(page, loc, logger)
        await _human_pause(page, 120, 300)
        await _human_type_text(page, loc, text)
        await _human_pause(page, 120, 280)
        await loc.dispatch_event("input")
        await loc.dispatch_event("change")
        actual = await loc.input_value()
        if actual == text:
            if logger and label:
                logger.info(f"Set {label} = {value!r} (humanized)")
            return True
        if logger:
            logger.warning(
                f"_humanized_type {selector!r}: value mismatch after type "
                f"(expected {text!r}, saw {actual!r}); falling back to JS set."
            )
    except Exception as exc:
        if logger:
            logger.warning(f"_humanized_type {selector!r} error: {exc}")

    return await _js_set(page, selector, value, logger, label)


async def _humanized_type_only(page, selector: str, value: str, logger=None, label: str = "") -> bool:
    text = str(value or "")
    try:
        loc = page.locator(selector).first
        await _safe_scroll_into_view(loc)
        await _human_clear_field(page, loc, logger)
        await _human_pause(page, 120, 300)
        await _human_type_text(page, loc, text)
        await _human_pause(page, 120, 280)
        actual = await loc.input_value()
        if actual == text:
            if logger and label:
                logger.info(f"Set {label} = {value!r} (type-only)")
            return True
    except Exception as exc:
        if logger:
            logger.warning(f"_humanized_type_only {selector!r} error: {exc}")
    return False


async def _type_otp_code(page, selectors, otp: str, logger=None, profile_id: str = "") -> bool:
    code = str(otp or "").strip()
    if not code:
        return False

    # Snapchat's OTP UI uses six separate inputs named otpDigits.0 .. otpDigits.5.
    try:
        otp_digit_inputs = []
        for index in range(len(code)):
            locator = page.locator(f"input[name='otpDigits.{index}']").first
            try:
                if await locator.is_visible():
                    otp_digit_inputs.append(locator)
            except Exception:
                continue

        if len(otp_digit_inputs) >= len(code):
            for index, char in enumerate(code):
                locator = otp_digit_inputs[index]
                await locator.click()
                await _human_pause(page, 60, 140)
                await locator.fill("")
                await _human_pause(page, 40, 100)
                await locator.type(char, delay=_random_delay_ms(55, 130))
                await _human_pause(page, 50, 120)

            actual_code = []
            for locator in otp_digit_inputs[: len(code)]:
                try:
                    actual_code.append((await locator.input_value() or "").strip())
                except Exception:
                    actual_code.append("")
            actual_joined = "".join(actual_code)
            if actual_joined == code:
                logger and logger.info(f"[{profile_id}] OTP typed across otpDigits inputs.")
                return True
            logger and logger.warning(
                f"[{profile_id}] otpDigits values mismatch after type: expected={code!r} actual={actual_joined!r}"
            )
    except Exception as exc:
        logger and logger.warning(f"[{profile_id}] otpDigits typing failed: {exc}")

    try:
        for selector in selectors:
            if await _humanized_type_only(page, selector, code, logger, f"[{profile_id}] OTP"):
                logger and logger.info(f"[{profile_id}] OTP typed into single input.")
                return True
    except Exception as exc:
        logger and logger.warning(f"[{profile_id}] Single-input OTP typing failed: {exc}")
    return False


async def _js_select_month(page, month: int, logger=None, profile_id: str = "") -> bool:
    for sel in ["select#month", "select[name='month']", "select[data-testid='month-input']"]:
        try:
            await _human_pause(page, 120, 260)
            result = await page.evaluate(_JS_SET_INPUT, [sel, str(month)])
            if result and result.get("ok"):
                if logger:
                    logger.info(f"[{profile_id}] Selected month {month}")
                return True
        except Exception:
            pass
        try:
            loc = page.locator(sel).first
            if await loc.is_visible():
                await _human_pause(page, 120, 260)
                await loc.select_option(str(month))
                if logger:
                    logger.info(f"[{profile_id}] Selected month {month} (select_option fallback)")
                return True
        except Exception:
            pass
    if logger:
        logger.warning(f"[{profile_id}] Could not select month")
    return False


async def _wait_visible(page, selector: str, timeout_ms: int = 30000) -> bool:
    deadline = timeout_ms
    while deadline > 0:
        try:
            if await page.locator(selector).first.is_visible():
                return True
        except Exception:
            pass
        await page.wait_for_timeout(500)
        deadline -= 500
    return False


async def _wait_enabled(page, selector: str, timeout_ms: int = 12000) -> bool:
    deadline = timeout_ms
    while deadline > 0:
        try:
            disabled = await page.evaluate(_JS_IS_DISABLED, selector)
            if not disabled:
                return True
        except Exception:
            pass
        await page.wait_for_timeout(500)
        deadline -= 500
    return False


async def _js_click(page, selector: str, *, timeout_ms: int | None = None) -> bool:
    try:
        ok = await page.evaluate(_JS_CLICK, selector)
        if ok:
            return True
    except Exception:
        pass
    try:
        click_timeout = max(1, int(timeout_ms if timeout_ms is not None else SIGNUP_JS_CLICK_TIMEOUT_MS))
        await page.locator(selector).first.click(force=True, timeout=click_timeout)
        return True
    except Exception:
        return False


async def _read_input_value(page, selectors) -> str:
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if await locator.is_visible():
                return str((await locator.input_value()) or "").strip()
        except Exception:
            continue
    return ""


async def _visible_any(page, selectors) -> str:
    for selector in selectors:
        try:
            if await page.locator(selector).first.is_visible():
                return selector
        except Exception:
            continue
    return ""


def _page_is_open(page) -> bool:
    try:
        return bool(page) and not page.is_closed()
    except Exception:
        return False


async def _resolve_active_signup_page(page, logger=None, profile_id: str = ""):
    candidates = []
    if _page_is_open(page):
        candidates.append(page)

    try:
        context = getattr(page, "context", None)
    except Exception:
        context = None

    if context is not None:
        for candidate in list(getattr(context, "pages", []) or []):
            if candidate in candidates or not _page_is_open(candidate):
                continue
            candidates.append(candidate)

    if not candidates:
        return page

    preferred = []
    for candidate in candidates:
        try:
            current_url = str(candidate.url or "").strip().lower()
        except Exception:
            current_url = ""
        if "accounts.snapchat.com" in current_url:
            preferred.append(candidate)
    candidates = preferred or candidates

    stage_selectors = [
        "#firstname",
        "#username",
        "#password",
        *_EMAIL_INPUT_SELECTORS,
        *_OTP_INPUT_SELECTORS,
        "button[type='submit']",
        "button:has-text('Agree and Continue')",
        "a:has-text('Use Phone Number Instead')",
        "[role='button']:has-text('Use Phone Number Instead')",
        "[data-testid='username']",
    ]
    for candidate in candidates:
        try:
            if await _visible_any(candidate, stage_selectors):
                if candidate is not page:
                    logger and logger.info(
                        f"[{profile_id}] Switched signup watcher to active page: {str(candidate.url or '').strip()!r}"
                    )
                return candidate
        except Exception:
            continue

    fallback = candidates[0]
    if fallback is not page:
        logger and logger.info(
            f"[{profile_id}] Switched signup watcher to fallback page: {str(getattr(fallback, 'url', '') or '').strip()!r}"
        )
    return fallback


# ---------------------------------------------------------------------------
# Step 1 — fill the signup form
# ---------------------------------------------------------------------------

_JS_DISMISS_COOKIES = """
() => {
    const candidates = Array.from(document.querySelectorAll('button'));
    const btn = candidates.find(b => (b.innerText || b.textContent || '').trim().toLowerCase() === 'accept all');
    if (btn) { btn.click(); return true; }
    return false;
}
"""

_JS_COOKIE_MODAL_VISIBLE = """
() => {
    const sels = [
        '[data-testid="mwp-cookie-landing-screen"]',
        '[data-testid="mwp-cookie-modal-body"]',
        '.cookie-landing-screen',
        '[class*="cookie-landing-screen"]',
        '.sdsm-modal-content',
    ];
    return sels.some(s => {
        const el = document.querySelector(s);
        return el && el.offsetParent !== null;
    });
}
"""


async def _dismiss_cookie_popup(page, logger=None, profile_id: str = "") -> bool:
    """Dismiss cookie popup if visible. Returns True if popup was found and dismissed."""
    try:
        modal_visible = await page.evaluate(_JS_COOKIE_MODAL_VISIBLE)
    except Exception:
        modal_visible = False

    if not modal_visible:
        # Also check for the button directly without modal wrapper
        try:
            btn = page.locator("button:has-text('Accept All')").first
            if not await btn.is_visible():
                return False
        except Exception:
            return False

    if logger:
        logger.info(f"[{profile_id}] Cookie popup detected — dismissing.")

    try:
        clicked = await page.evaluate(_JS_DISMISS_COOKIES)
        if clicked:
            await page.wait_for_timeout(800)
            if logger:
                logger.info(f"[{profile_id}] Cookie popup dismissed via JS.")
            return True
    except Exception:
        pass

    try:
        btn = page.locator("button:has-text('Accept All')").first
        if await btn.is_visible():
            await btn.click(force=True)
            await page.wait_for_timeout(800)
            if logger:
                logger.info(f"[{profile_id}] Cookie popup dismissed via locator.")
            return True
    except Exception:
        pass

    return False


def _sanitize_username(value: str) -> str:
    raw = str(value or "").strip().lower()
    cleaned = "".join(ch for ch in raw if ch.isalnum() or ch in {"_", ".", "-"})
    cleaned = cleaned.strip("._-")
    if len(cleaned) < 3:
        return ""
    return cleaned[:30]


def _resolve_signup_username(username: str, logger=None, profile_id: str = "") -> str:
    resolved = _sanitize_username(username)
    if resolved:
        return resolved
    if logger:
        logger.error(f"[{profile_id}] Username missing or invalid in Nyxify task payload.")
    raise ValueError("SnapBoard username is missing or invalid.")


async def _keep_signup_page_clear(page, logger=None, profile_id: str = "", duration_ms: int = 1200) -> bool:
    dismissed_any = False
    remaining_ms = max(0, int(duration_ms))

    while remaining_ms > 0:
        try:
            dismissed_any = await _dismiss_cookie_popup(page, logger, profile_id) or dismissed_any
        except Exception:
            pass

        step_ms = 200 if remaining_ms > 200 else remaining_ms
        if step_ms > 0:
            await page.wait_for_timeout(step_ms)
        remaining_ms -= step_ms

    return dismissed_any


def _same_username(lhs: str, rhs: str) -> bool:
    return str(lhs or "").strip().lower() == str(rhs or "").strip().lower()


async def _click_signup_submit(page, logger=None, profile_id: str = "", *, fast: bool = False) -> bool:
    submit_selectors = ["button:has-text('Agree and Continue')", "button[type='submit']"]
    submit_selector = await _visible_any(page, submit_selectors)
    submit_selector = submit_selector or "button[type='submit']"
    pre_clear_ms = SIGNUP_FAST_SUBMIT_PRE_CLEAR_MS if fast else 800
    post_clear_ms = SIGNUP_FAST_SUBMIT_POST_CLEAR_MS if fast else 500
    pause_min_ms = SIGNUP_FAST_SUBMIT_PAUSE_MIN_MS if fast else 350
    pause_max_ms = SIGNUP_FAST_SUBMIT_PAUSE_MAX_MS if fast else 900
    await _keep_signup_page_clear(page, logger, profile_id, duration_ms=pre_clear_ms)
    enabled_timeout_ms = SIGNUP_FAST_SUBMIT_ENABLED_TIMEOUT_MS if fast else 12000
    click_timeout_ms = SIGNUP_FAST_SUBMIT_CLICK_TIMEOUT_MS if fast else SIGNUP_JS_CLICK_TIMEOUT_MS
    captcha_present = await _recaptcha_widget_present(page)
    if not captcha_present:
        enabled_timeout_ms = min(enabled_timeout_ms, SIGNUP_CAPTCHALESS_SUBMIT_ENABLED_TIMEOUT_MS)
    enabled = await _wait_enabled(page, submit_selector, timeout_ms=enabled_timeout_ms)
    logger and logger.info(f"[{profile_id}] Submit button enabled={enabled}")
    if not enabled and not captcha_present:
        logger and logger.warning(
            f"[{profile_id}] Submit stayed disabled and no visible reCAPTCHA widget was found; refreshing signup."
        )
        return False
    await _human_pause(page, pause_min_ms, pause_max_ms)
    await _keep_signup_page_clear(page, logger, profile_id, duration_ms=post_clear_ms)
    clicked = await _js_click(page, submit_selector, timeout_ms=click_timeout_ms)
    logger and logger.info(f"[{profile_id}] Submit click={clicked}")
    return clicked


async def _is_username_taken_error_visible(page) -> bool:
    try:
        username_selector = await _visible_any(
            page,
            [
                "#username",
                "input[name='username']",
                "input[placeholder*='username' i]",
            ],
        )
        if not username_selector:
            return False

        return bool(
            await page.evaluate(
                """
                (usernameTakenMarkers) => {
                    const isVisible = (node) => {
                        if (!node) return false;
                        const style = window.getComputedStyle(node);
                        if (!style) return false;
                        if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
                            return false;
                        }
                        const rect = node.getBoundingClientRect();
                        return rect.width > 0 && rect.height > 0;
                    };
                    const selectors = [
                        "[data-testid*='error' i]",
                        "[class*='error' i]",
                        "[role='alert']",
                        "[aria-live='assertive']",
                    ];
                    const seen = new Set();
                    for (const selector of selectors) {
                        const nodes = Array.from(document.querySelectorAll(selector));
                        for (const node of nodes) {
                            if (!node || seen.has(node)) {
                                continue;
                            }
                            seen.add(node);
                            if (!isVisible(node)) {
                                continue;
                            }
                            const text = String(node.innerText || node.textContent || "").trim().toLowerCase();
                            if (usernameTakenMarkers.some((marker) => text.includes(marker))) {
                                return true;
                            }
                        }
                    }
                    return false;
                }
                """,
                list(_USERNAME_TAKEN_ERROR_MARKERS) + list(_USERNAME_INVALID_ERROR_MARKERS),
            )
        )
    except Exception:
        return False


async def _is_use_email_switch_visible(page) -> bool:
    selectors = [
        "div[class*='PhoneNumberVerification_useEmailInstead'] a[role='button']",
        "div[class*='useEmailInstead'] a[role='button']",
        "a:has-text('Use email instead')",
        "a:has-text('Use email')",
        "[role='button']:has-text('Use email instead')",
        "[role='button']:has-text('Use email')",
        "button:has-text('Use email instead')",
        "button:has-text('Use email')",
        "span:has-text('Use email instead')",
        "span:has-text('Use email')",
    ]
    if await _visible_any(page, selectors):
        return True

    try:
        return bool(
            await page.evaluate(
                """
                (usernameTakenMarkers) => {
                    const normalize = (value) => String(value || "")
                        .replace(/\\s+/g, " ")
                        .trim()
                        .toLowerCase();
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
                    const isEmailSwitchText = (text) => {
                        if (!text || text.includes("resend")) return false;
                        return (
                            text === "use email instead"
                            || text.includes("use email instead")
                            || text.includes("email instead")
                            || text.includes("verify with email")
                            || text.includes("use email")
                        );
                    };
                    const nodes = Array.from(document.querySelectorAll("a, button, div, span, p, label"));
                    return nodes.some((node) => isVisible(node) && isEmailSwitchText(normalize(node.innerText || node.textContent)));
                }
                """
            )
        )
    except Exception:
        return False


async def _detect_signup_handoff_stage(page, logger=None, profile_id: str = "") -> str:
    success_username = await _read_success_username(page)
    if success_username:
        logger and logger.info(f"[{profile_id}] Success username detected during signup handoff: {success_username}")
        return "welcome"

    if await _visible_any(page, _OTP_INPUT_SELECTORS):
        logger and logger.info(f"[{profile_id}] OTP step detected during signup handoff.")
        return "otp"

    if await _is_use_email_switch_visible(page):
        logger and logger.info(f"[{profile_id}] Use email instead switch detected during signup handoff.")
        return "email_switch"

    if await _visible_any(page, _EMAIL_INPUT_SELECTORS) or await _is_email_verification_step(page):
        logger and logger.info(f"[{profile_id}] Email verification step detected during signup handoff.")
        return "email"

    if await _is_phone_verification_step(page):
        logger and logger.info(f"[{profile_id}] Phone verification step detected during signup handoff.")
        return "phone"

    return ""


async def _emit_signup_progress(progress_callback, step: str, logger=None, profile_id: str = "") -> None:
    if progress_callback is None or not step:
        return
    try:
        result = progress_callback(step)
        if asyncio.iscoroutine(result):
            await result
    except Exception as exc:
        logger and logger.warning(f"[{profile_id}] Signup progress callback failed for {step!r}: {exc}")


async def _is_blank_signup_shell(page) -> bool:
    try:
        current_url = str(page.url or "").strip().lower()
    except Exception:
        current_url = ""
    if "accounts.snapchat.com" not in current_url or "/v2/signup" not in current_url:
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


async def _is_unable_to_process_error_visible(page) -> bool:
    try:
        if bool(
            await page.evaluate(
                """
                (payload) => {
                    const usernameTakenMarkers = Array.isArray(payload && payload.usernameTakenMarkers)
                        ? payload.usernameTakenMarkers
                        : [];
                    const unableMarkers = Array.isArray(payload && payload.unableMarkers)
                        ? payload.unableMarkers
                        : [];
                    const normalize = (value) => String(value || "")
                        .replace(/\\s+/g, " ")
                        .trim()
                        .toLowerCase();
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
                    const onSignupForm = ["#firstname", "#username", "#password", "#day", "#year"].some((selector) => {
                        const el = document.querySelector(selector);
                        return el && isVisible(el);
                    });
                    const nodes = Array.from(document.querySelectorAll(
                        "p[data-testid='error-text'], [data-testid='error-text'], [class*='GenericFormLevelErrorMessage'], [role='alert'], [aria-live='assertive'], [class*='error' i]"
                    ));
                    return nodes.some((node) => {
                        if (!isVisible(node)) return false;
                        const text = normalize(node.innerText || node.textContent);
                        if (usernameTakenMarkers.some((marker) => text.includes(marker))) {
                            return false;
                        }
                        if (unableMarkers.some((marker) => text.includes(marker))) {
                            return true;
                        }
                        return onSignupForm
                            && text
                            && Boolean(node.closest("[class*='GenericFormLevelErrorMessage']"));
                    });
                }
                """,
                {
                    "usernameTakenMarkers": list(_USERNAME_TAKEN_ERROR_MARKERS),
                    "unableMarkers": list(_UNABLE_TO_PROCESS_ERROR_MARKERS),
                },
            )
        ):
            return True
    except Exception:
        pass

    try:
        on_signup_form = bool(
            await _visible_any(page, ["#firstname", "#username", "#password", "#day", "#year"])
        )
        if not on_signup_form:
            return False
        if await _page_has_visible_text(page, _USERNAME_TAKEN_ERROR_MARKERS):
            return False
        return await _page_has_visible_text(page, _UNABLE_TO_PROCESS_ERROR_MARKERS)
    except Exception:
        return False


async def _page_has_visible_text(page, needles) -> bool:
    """Return True if any visible element's text contains any of ``needles``.

    Generic, language-aware-by-caller scan used by the specific signup-blocker
    detectors below so each one is a one-line list of phrases to match.
    """
    needle_list = [str(n or "").strip().lower() for n in (needles or []) if str(n or "").strip()]
    if not needle_list:
        return False
    try:
        return bool(
            await page.evaluate(
                """
                (needles) => {
                    const normalize = (value) => String(value || "")
                        .replace(/\\s+/g, " ")
                        .trim()
                        .toLowerCase();
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
                    const nodes = Array.from(document.querySelectorAll(
                        "p, span, div, h1, h2, h3, h4, h5, label, a, button, li"
                    ));
                    for (const node of nodes) {
                        if (!isVisible(node)) continue;
                        const text = normalize(node.innerText || node.textContent);
                        if (!text) continue;
                        if (needles.some((needle) => text.includes(needle))) {
                            return true;
                        }
                    }
                    return false;
                }
                """,
                needle_list,
            )
        )
    except Exception:
        return False


async def _is_recaptcha_connect_error_visible(page) -> bool:
    """Snapchat's "Could not connect to the reCAPTCHA service…" banner."""
    return await _page_has_visible_text(
        page,
        [
            "could not connect to the recaptcha service",
            "reload to get a recaptcha challenge",
        ],
    )


async def _is_account_creation_blocked_visible(page) -> bool:
    """"Account creation could not be completed at this time. Please try again on
    our mobile app." — Snapchat refuses web signup for this profile/IP."""
    return await _page_has_visible_text(
        page,
        [
            "account creation could not be completed",
            "try again on our mobile app",
            "please try again on our mobile app",
        ],
    )


async def _recaptcha_widget_present(page) -> bool:
    """True if a reCAPTCHA challenge/badge is actually present on the page.

    Snapchat renders the reCAPTCHA badge in the lower-right corner once the
    challenge has loaded. If it never appears the form can't be submitted, which
    is the "no captcha icon" stall the operator described.
    """
    try:
        return bool(
            await page.evaluate(
                """
                () => {
                    const isVisible = (node) => {
                        if (!node) return false;
                        const style = window.getComputedStyle(node);
                        if (!style) return false;
                        if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
                            return false;
                        }
                        const rect = node.getBoundingClientRect();
                        return rect.width > 0 && rect.height > 0;
                    };
                    const badge = document.querySelector('.grecaptcha-badge');
                    if (isVisible(badge)) {
                        return true;
                    }
                    const frames = document.querySelectorAll('iframe[src*="recaptcha"], iframe[title*="recaptcha" i]');
                    for (const frame of frames) {
                        if (isVisible(frame)) return true;
                    }
                    return false;
                }
                """
            )
        )
    except Exception:
        # If we can't tell, assume a captcha may be present so we don't refresh
        # a page that is merely slow.
        return True


async def _submit_is_clickable(page, submit_selectors) -> bool:
    """True when an "Agree and Continue" / submit button is both visible AND
    enabled — i.e. it could actually be clicked right now. A button that stays
    disabled forever is the "can't even click Agree and Continue" stall."""
    selector = await _visible_any(page, submit_selectors)
    if not selector:
        return False
    try:
        return not bool(await page.evaluate(_JS_IS_DISABLED, selector))
    except Exception:
        return False


async def _signup_form_is_blank(page) -> bool:
    """True when the signup form is showing but its key fields are empty — the
    state after a hard page refresh (commonly a manual operator refresh) that
    clears every React-controlled input. Signals that the saved credentials
    should be re-entered without waiting out the stall timer."""
    try:
        if not await _visible_any(page, ["#firstname"]):
            return False
        if str(await _read_input_value(page, ["#firstname"]) or "").strip():
            return False
        username_val = await _read_input_value(
            page,
            ["#username", "input[name='username']", "input[placeholder*='username' i]"],
        )
        return not str(username_val or "").strip()
    except Exception:
        return False


async def _is_non_english_signup_page(page) -> bool:
    """True when the signup page is clearly rendered in a language the bot's
    text-based handoff detection can't read (Arabic, Chinese, Cyrillic, …).

    The form itself is filled by element id, so language doesn't block filling —
    but the email/OTP/"use email instead"/success detection all rely on English
    text. Rather than get stuck, we surface this so the runner rotates to a fresh
    profile + proxy (which usually comes up in English).
    """
    try:
        info = await page.evaluate(
            """
            () => {
                const htmlLang = String(document.documentElement.getAttribute('lang') || '').trim().toLowerCase();
                const probe = ['button[type=submit]', 'h1', 'h2', 'h3', 'label', 'a[role=button]'];
                let sample = '';
                for (const sel of probe) {
                    for (const node of document.querySelectorAll(sel)) {
                        const t = String(node.innerText || node.textContent || '').trim();
                        if (t) sample += ' ' + t;
                    }
                    if (sample.length > 600) break;
                }
                sample = sample.slice(0, 600);
                const nonLatin = (sample.match(/[\\u0400-\\u04FF\\u0590-\\u05FF\\u0600-\\u06FF\\u0750-\\u077F\\u0E00-\\u0E7F\\u1100-\\u11FF\\u3040-\\u30FF\\u3400-\\u9FFF\\uAC00-\\uD7AF]/g) || []).length;
                const latin = (sample.match(/[A-Za-z]/g) || []).length;
                return { lang: htmlLang, nonLatin: nonLatin, latin: latin };
            }
            """
        ) or {}
    except Exception:
        return False

    lang = str(info.get("lang") or "").strip().lower()
    non_latin = int(info.get("nonLatin") or 0)
    latin = int(info.get("latin") or 0)

    # Primary: an explicit non-English document language, corroborated by the
    # visible UI text actually being non-Latin (guards against a stale/wrong
    # lang attribute on an otherwise-English page).
    if lang and not lang.startswith("en") and (non_latin >= 3 or latin == 0) and non_latin > 0:
        return True
    # Secondary: the prominent UI text is overwhelmingly non-Latin even if the
    # lang attribute is missing or lies.
    if non_latin >= 6 and non_latin > latin:
        return True
    return False


def _resolve_signup_password(password: str = "") -> str:
    return str(password or "").strip() or PASSWORD


async def _reload_and_refill_signup(page, snap_name, birthday, username, password="", logger=None, profile_id: str = "") -> bool:
    """Hard-refresh the signup page and re-enter every field, then resubmit.

    Used to recover a stalled form (no captcha challenge / reCAPTCHA service
    unreachable). Returns True if the form was re-filled and resubmitted.
    """
    try:
        await page.reload(wait_until="domcontentloaded")
    except Exception as exc:
        logger and logger.warning(f"[{profile_id}] Signup reload failed ({exc}); trying direct navigation.")
        try:
            await page.goto("https://accounts.snapchat.com/v2/signup", wait_until="domcontentloaded")
        except Exception as exc2:
            logger and logger.warning(f"[{profile_id}] Signup re-navigation also failed: {exc2}")
            return False

    await _keep_signup_page_clear(page, logger, profile_id, duration_ms=1500)
    ready = await _wait_visible(page, "#firstname", timeout_ms=30000)
    if not ready:
        logger and logger.warning(f"[{profile_id}] Signup form did not reappear after reload.")
        return False

    fill_result = await _fill_signup_form(page, snap_name, birthday, username, password, logger, profile_id)
    return bool(fill_result.get("submitted"))


async def _retry_taken_username(page, current_username: str, username_retry_provider, logger, profile_id: str) -> str:
    if username_retry_provider is None:
        return ""

    handoff_stage = await _detect_signup_handoff_stage(page, logger, profile_id)
    if handoff_stage == "email_switch":
        clicked_email = await _click_use_email_instead(page, logger, profile_id)
        if clicked_email:
            return ""
    elif handoff_stage in {"email", "otp", "phone", "welcome"}:
        logger and logger.info(
            f"[{profile_id}] Skipping replacement username retry because signup already reached {handoff_stage!r}."
        )
        return ""

    username_selectors = [
        "#username",
        "input[name='username']",
        "input[placeholder*='username' i]",
    ]
    username_selector = await _visible_any(page, username_selectors)
    if not username_selector:
        logger and logger.info(
            f"[{profile_id}] Username-taken message is still visible, but the username input is gone. "
            "Treating it as a stale error and continuing verification handoff."
        )
        clicked_email = await _click_use_email_instead(page, logger, profile_id)
        if clicked_email:
            await page.wait_for_timeout(900)
        return ""

    next_username = username_retry_provider(current_username, "signup_username_already_taken")
    if asyncio.iscoroutine(next_username):
        next_username = await next_username
    next_username = _sanitize_username(next_username)
    if not next_username:
        logger and logger.warning(
            f"[{profile_id}] Full Auto Mode has no replacement username available after a username-taken error. "
            f"Waiting for manual correction."
        )
        return ""
    if _same_username(current_username, next_username):
        raise RuntimeError("Full Auto Mode returned the same username after a username-taken error.")

    logger and logger.info(
        f"[{profile_id}] Username already taken. Retrying signup with {next_username!r}."
    )

    await _keep_signup_page_clear(page, logger, profile_id, duration_ms=500)
    if await _detect_signup_handoff_stage(page, logger, profile_id) in {"email", "otp", "phone", "welcome", "email_switch"}:
        logger and logger.info(f"[{profile_id}] Skipping retry type — page already transitioned.")
        return ""
    try:
        ok = await asyncio.wait_for(
            _humanized_type(page, username_selector, next_username, logger, f"[{profile_id}] retry username"),
            timeout=8.0,
        )
    except asyncio.TimeoutError:
        ok = False
    if not ok:
        handoff_stage = await _detect_signup_handoff_stage(page, logger, profile_id)
        if handoff_stage == "email_switch":
            clicked_email = await _click_use_email_instead(page, logger, profile_id)
            if clicked_email:
                logger and logger.info(
                    f"[{profile_id}] Replacement username input was not fillable, but email handoff was available."
                )
                return ""
        elif handoff_stage in {"email", "otp", "phone", "welcome"}:
            logger and logger.info(
                f"[{profile_id}] Replacement username input was not fillable because signup already reached {handoff_stage!r}."
            )
            return ""
        raise RuntimeError("Could not fill the replacement Snapchat username.")

    clicked = await _click_signup_submit(page, logger, profile_id, fast=True)
    if not clicked:
        raise RuntimeError("Could not submit signup after replacing the Snapchat username.")

    return next_username


async def _fill_signup_form(
    page, snap_name: str, birthday: dict, username: str, password: str = "", logger=None, profile_id: str = ""
) -> dict:
    resolved_username = _resolve_signup_username(username, logger, profile_id)
    resolved_password = _resolve_signup_password(password)

    try:
        await page.bring_to_front()
    except Exception:
        pass
    await _human_pause(page, 600, 1800)
    await _keep_signup_page_clear(page, logger, profile_id, duration_ms=1500)

    # Dismiss cookie popup before filling — it wipes React state if it appears mid-fill
    await _keep_signup_page_clear(page, logger, profile_id, duration_ms=400)

    logger and logger.info(
        f"[{profile_id}] Filling form: name={snap_name!r} "
        f"bday={birthday} username={resolved_username!r}"
    )

    # First name
    await _keep_signup_page_clear(page, logger, profile_id, duration_ms=500)
    ok = await _humanized_type(page, "#firstname", snap_name, logger, f"[{profile_id}] name")
    if not ok:
        logger and logger.error(f"[{profile_id}] FAILED to fill #firstname")
        return {"submitted": False, "username": resolved_username}
    await _keep_signup_page_clear(page, logger, profile_id, duration_ms=700)

    # Birthday month
    await _keep_signup_page_clear(page, logger, profile_id, duration_ms=300)
    await _js_select_month(page, birthday["month"], logger, profile_id)
    await _keep_signup_page_clear(page, logger, profile_id, duration_ms=500)

    # Birthday day
    await _keep_signup_page_clear(page, logger, profile_id, duration_ms=300)
    await _humanized_type(page, "#day", str(birthday["day"]), logger, f"[{profile_id}] day")
    await _keep_signup_page_clear(page, logger, profile_id, duration_ms=500)

    # Birthday year
    await _keep_signup_page_clear(page, logger, profile_id, duration_ms=300)
    await _humanized_type(page, "#year", birthday["year"], logger, f"[{profile_id}] year")
    await _keep_signup_page_clear(page, logger, profile_id, duration_ms=500)

    # Username
    await _keep_signup_page_clear(page, logger, profile_id, duration_ms=300)
    await _humanized_type(page, "#username", resolved_username, logger, f"[{profile_id}] username")
    await _keep_signup_page_clear(page, logger, profile_id, duration_ms=500)

    # Password
    await _keep_signup_page_clear(page, logger, profile_id, duration_ms=300)
    await _humanized_type(page, "#password", resolved_password, logger, f"[{profile_id}] password")
    await _keep_signup_page_clear(page, logger, profile_id, duration_ms=900)

    # Log field values to confirm React accepted them
    try:
        fname   = await page.evaluate(_JS_GET_VALUE, "#firstname")
        month_v = await page.evaluate(_JS_GET_VALUE, "select#month")
        day_v   = await page.evaluate(_JS_GET_VALUE, "#day")
        year_v  = await page.evaluate(_JS_GET_VALUE, "#year")
        uname_v = await page.evaluate(_JS_GET_VALUE, "#username")
        pw_v    = await page.evaluate(_JS_GET_VALUE, "#password")
        logger and logger.info(
            f"[{profile_id}] Field values after fill: "
            f"name={fname!r} month={month_v!r} day={day_v!r} "
            f"year={year_v!r} username={uname_v!r} password={'set' if pw_v else 'empty'}"
        )
    except Exception:
        pass

    clicked = await _click_signup_submit(page, logger, profile_id, fast=True)
    return {"submitted": clicked, "username": resolved_username}


async def _click_use_email_instead(page, logger=None, profile_id: str = "") -> bool:
    selectors = [
        "div[class*='PhoneNumberVerification_useEmailInstead'] a[role='button']",
        "div[class*='useEmailInstead'] a[role='button']",
        "a[role='button']",
        "a:has-text('Use email instead')",
        "a:has-text('Use email')",
        "[role='button']:has-text('Use email instead')",
        "[role='button']:has-text('Use email')",
    ]
    selector = await _visible_any(page, selectors)
    if selector:
        try:
            locator = page.locator(selector).first
            try:
                text = str(await locator.inner_text() or "").strip().lower()
            except Exception:
                text = ""
            if text and "email" in text:
                await _safe_scroll_into_view(locator)
                await locator.click(force=True)
                await page.wait_for_timeout(900)
                logger and logger.info(f"[{profile_id}] Clicked email verification switch with selector {selector!r}.")
                return True
        except Exception as exc:
            logger and logger.warning(f"[{profile_id}] Email switch click failed via locator: {exc}")

    try:
        clicked = await page.evaluate(
            """
            () => {
                const normalize = (value) => String(value || "")
                    .replace(/\\s+/g, " ")
                    .trim()
                    .toLowerCase();
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
                const isEmailSwitchText = (text) => {
                    if (!text || text.includes("resend")) return false;
                    return (
                        text === "use email instead"
                        || text.includes("use email instead")
                        || text.includes("email instead")
                        || text.includes("verify with email")
                        || text.includes("use email")
                    );
                };
                const fireClick = (node) => {
                    if (!node) return false;
                    const events = ['pointerdown', 'mousedown', 'mouseup', 'click'];
                    for (const type of events) {
                        try {
                            node.dispatchEvent(new MouseEvent(type, { bubbles: true, cancelable: true, view: window }));
                        } catch (_) {}
                    }
                    try { node.click(); } catch (_) {}
                    return true;
                };
                const clickableFor = (node) => {
                    if (!node) return null;
                    return node.closest('a, button, [role="button"], [tabindex], div[class*="useEmailInstead"], div[class*="PhoneNumberVerification_useEmailInstead"]') || node;
                };
                const direct =
                    document.querySelector("div[class*='PhoneNumberVerification_useEmailInstead'] a[role='button']") ||
                    document.querySelector("div[class*='useEmailInstead'] a[role='button']") ||
                    document.querySelector("div[class*='PhoneNumberVerification_useEmailInstead']") ||
                    document.querySelector("div[class*='useEmailInstead']");
                if (direct && isVisible(direct)) {
                    return fireClick(direct);
                }
                const nodes = Array.from(document.querySelectorAll('button, a, div, span, p, label, [role="button"], [tabindex]'));
                for (const node of nodes) {
                    if (!isVisible(node)) continue;
                    const text = normalize(node.innerText || node.textContent);
                    const role = (node.getAttribute('role') || '').trim().toLowerCase();
                    const className = (node.className || '').toString().toLowerCase();
                    const tagName = String(node.tagName || '').toLowerCase();
                    const clickable = tagName === 'a' || tagName === 'button' || role === 'button' || className.includes('useemailinstead');
                    if (!isEmailSwitchText(text)) continue;
                    const target = clickable ? node : clickableFor(node);
                    if (target && isVisible(target)) {
                        return fireClick(target);
                    }
                }
                return false;
            }
            """
        )
        if clicked:
            await page.wait_for_timeout(900)
            logger and logger.info(f"[{profile_id}] Clicked email verification switch via JS scan.")
            return True
    except Exception as exc:
        logger and logger.warning(f"[{profile_id}] Email switch click failed via JS: {exc}")
    return False


async def _click_use_phone_instead(page, logger=None, profile_id: str = "") -> bool:
    """Click the "Use Phone Number Instead" switch on the email verification step.

    Used as the fallback when email verification keeps failing — Snapchat lets the
    account switch to a phone number instead, which then runs the phone -> SMS OTP
    path. Mirrors :func:`_click_use_email_instead` (selectors -> locator click ->
    JS scan).
    """
    selectors = [
        "a:has-text('Use Phone Number Instead')",
        "[role='button']:has-text('Use Phone Number Instead')",
        "button:has-text('Use Phone Number Instead')",
        "div[class*='EmailVerification'] a[role='button']",
    ]
    selector = await _visible_any(page, selectors)
    if selector:
        try:
            locator = page.locator(selector).first
            text = ""
            try:
                text = str(await locator.inner_text() or "").strip().lower()
            except Exception:
                text = ""
            if text and "phone" in text:
                await _safe_scroll_into_view(locator)
                await locator.click(force=True)
                await page.wait_for_timeout(900)
                logger and logger.info(f"[{profile_id}] Clicked phone-instead switch with selector {selector!r}.")
                return True
        except Exception as exc:
            logger and logger.warning(f"[{profile_id}] Phone-instead switch click failed via locator: {exc}")

    try:
        clicked = await page.evaluate(
            """
            () => {
                const normalize = (value) => String(value || "")
                    .replace(/\\s+/g, " ")
                    .trim()
                    .toLowerCase();
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
                const isSwitchText = (text) => text && (
                    text === "use phone number instead"
                    || text.includes("use phone number instead")
                    || text.includes("phone number instead")
                    || text.includes("verif. with phone")
                    || text.includes("use phone")
                );
                const fireClick = (node) => {
                    if (!node) return false;
                    const events = ["pointerdown", "mousedown", "mouseup", "click"];
                    for (const type of events) {
                        try {
                            node.dispatchEvent(new MouseEvent(type, { bubbles: true, cancelable: true, view: window }));
                        } catch (_) {}
                    }
                    try { node.click(); } catch (_) {}
                    return true;
                };
                const nodes = Array.from(document.querySelectorAll("button, a, div, span, p, label, [role='button'], [tabindex]"));
                for (const node of nodes) {
                    if (!isVisible(node)) continue;
                    const text = normalize(node.innerText || node.textContent);
                    if (!isSwitchText(text)) continue;
                    const role = (node.getAttribute("role") || "").trim().toLowerCase();
                    const tagName = String(node.tagName || "").toLowerCase();
                    const target = (tagName === "a" || tagName === "button" || role === "button")
                        ? node
                        : (node.closest("a, button, [role='button'], [tabindex]") || node);
                    if (target && isVisible(target)) {
                        return fireClick(target);
                    }
                }
                return false;
            }
            """
        )
        if clicked:
            await page.wait_for_timeout(900)
            logger and logger.info(f"[{profile_id}] Clicked phone-instead switch via JS scan.")
            return True
    except Exception as exc:
        logger and logger.warning(f"[{profile_id}] Phone-instead switch click failed via JS: {exc}")
    return False


async def _is_email_verification_step(page) -> bool:
    try:
        direct_selector = await _visible_any(
            page,
            [
                *_EMAIL_INPUT_SELECTORS,
                "a:has-text('Use Phone Number Instead')",
                "[role='button']:has-text('Use Phone Number Instead')",
                "label:has-text('Email Address')",
                "text='Email Address'",
            ],
        )
        if direct_selector:
            return True
    except Exception:
        pass

    try:
        return bool(
            await page.evaluate(
                """
                () => {
                    const isVisible = (node) => {
                        if (!node) return false;
                        const style = window.getComputedStyle(node);
                        if (!style) return false;
                        if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
                            return false;
                        }
                        const rect = node.getBoundingClientRect();
                        return rect.width > 0 && rect.height > 0;
                    };

                    const visibleText = Array.from(document.querySelectorAll('a, button, label, span, div'))
                        .filter(isVisible)
                        .map((node) => String(node.innerText || node.textContent || '').trim().toLowerCase())
                        .filter(Boolean);

                    if (visibleText.some((text) => text.includes('use phone number instead'))) {
                        return true;
                    }

                    if (visibleText.some((text) => text === 'email address' || text.includes('email address'))) {
                        return true;
                    }

                    const emailLikeInputs = Array.from(document.querySelectorAll('input')).filter((input) => {
                        if (!isVisible(input)) return false;
                        const type = String(input.getAttribute('type') || '').toLowerCase();
                        const name = String(input.getAttribute('name') || '').toLowerCase();
                        const placeholder = String(input.getAttribute('placeholder') || '').toLowerCase();
                        const ariaLabel = String(input.getAttribute('aria-label') || '').toLowerCase();
                        const autocomplete = String(input.getAttribute('autocomplete') || '').toLowerCase();
                        const inputmode = String(input.getAttribute('inputmode') || '').toLowerCase();
                        return (
                            type === 'email'
                            || name.includes('email')
                            || placeholder.includes('email')
                            || ariaLabel.includes('email')
                            || autocomplete === 'email'
                            || inputmode === 'email'
                        );
                    });
                    return emailLikeInputs.length > 0;
                }
                """
            )
        )
    except Exception:
        return False


async def _is_phone_verification_step(page) -> bool:
    try:
        direct_selector = await _visible_any(
            page,
            [
                *_PHONE_NUMBER_SELECTORS,
                "label:has-text('Phone Number')",
                "text='Phone Number'",
            ],
        )
        if direct_selector and await _visible_any(page, _PHONE_NUMBER_SELECTORS):
            return True
    except Exception:
        pass

    try:
        return bool(
            await page.evaluate(
                """
                () => {
                    const isVisible = (node) => {
                        if (!node) return false;
                        const style = window.getComputedStyle(node);
                        if (!style) return false;
                        if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
                            return false;
                        }
                        const rect = node.getBoundingClientRect();
                        return rect.width > 0 && rect.height > 0;
                    };

                    const phoneInput = document.querySelector(
                        "#phoneNumber, input[name='phoneNumber'], input[autocomplete='tel-national'], input[autocomplete='tel']"
                    );
                    if (phoneInput && isVisible(phoneInput)) {
                        return true;
                    }

                    const visibleText = Array.from(document.querySelectorAll('label, span, div, h6'))
                        .filter(isVisible)
                        .map((node) => String(node.innerText || node.textContent || '').trim().toLowerCase())
                        .filter(Boolean);
                    const hasPhoneLabel = visibleText.some((text) => text === 'phone number' || text.includes('phone number'));
                    const hasCountryCode = !!Array.from(document.querySelectorAll("#countryCode, input[name='countryCode']"))
                        .find(isVisible);
                    return hasPhoneLabel && hasCountryCode;
                }
                """
            )
        )
    except Exception:
        return False


def _split_phone_number(phone: str) -> tuple[str, str]:
    raw = str(phone or "").strip()
    digits = "".join(ch for ch in raw if ch.isdigit())
    if not digits:
        return "", ""

    if raw.lstrip().startswith("+") and len(digits) > 10:
        return f"+{digits[:-10]}", digits[-10:]
    if len(digits) == 11 and digits.startswith("1"):
        return "+1", digits[1:]
    return "", digits


def _digits_only(value: str) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())


async def _phone_input_has_local_number(locator, local_number: str) -> bool:
    try:
        return _digits_only(await locator.input_value()) == str(local_number or "").strip()
    except Exception:
        return False


async def _dispatch_input_events(locator) -> None:
    for event_name in ("input", "change", "blur"):
        try:
            await locator.dispatch_event(event_name)
        except Exception:
            pass


async def _click_enabled_verification_entry_submit(signup_page, selectors) -> bool:
    for button_sel in selectors:
        try:
            button = signup_page.locator(button_sel).first
            try:
                visible = await button.is_visible()
            except Exception:
                visible = False
            if not visible and button_sel != "button[type='submit']":
                continue
            enabled = await _wait_enabled(signup_page, button_sel, timeout_ms=5000)
            if not enabled:
                continue
            await _human_pause(signup_page, 250, 800)
            if await _js_click(signup_page, button_sel):
                return True
        except Exception:
            continue
    return False


async def _fill_and_submit_phone_number(signup_page, phone: str, logger=None, profile_id: str = "") -> bool:
    _country_code, local_number = _split_phone_number(phone)
    if not local_number:
        return False

    for phone_sel in _PHONE_NUMBER_SELECTORS:
        try:
            loc = signup_page.locator(phone_sel).first
            if await loc.is_visible():
                typed = await _humanized_type_only(signup_page, phone_sel, local_number, logger, f"[{profile_id}] phone")
                if not typed and not await _phone_input_has_local_number(loc, local_number):
                    return False
                await _dispatch_input_events(loc)
                await signup_page.wait_for_timeout(400)
                clicked = await _click_enabled_verification_entry_submit(signup_page, _PHONE_SUBMIT_SELECTORS)
                if not clicked:
                    return False
                await signup_page.wait_for_timeout(1200)
                return True
        except Exception:
            continue
    return False


async def _read_success_username_from_page(page) -> str:
    selectors = [
        "h5[data-testid='username'] span",
        "h5[data-testid='username']",
        "[data-testid='username'] span",
        "[data-testid='username']",
        "h5[class*='UserProfileCard_username'] span",
        "h5[class*='UserProfileCard_username']",
    ]
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if await locator.count():
                text = str((await locator.text_content()) or "").strip()
                if text:
                    return text
        except Exception:
            continue
    try:
        text = await page.evaluate(
            """
            () => {
                const nodes = Array.from(
                    document.querySelectorAll(
                        "h5[data-testid='username'], [data-testid='username'], h5[class*='UserProfileCard_username']"
                    )
                );
                for (const node of nodes) {
                    const value = (node.innerText || node.textContent || '').trim();
                    if (value) {
                        return value;
                    }
                }
                return '';
            }
            """
        )
        text = str(text or "").strip()
        if text:
            return text
    except Exception:
        pass
    return ""


async def _read_success_username(page) -> str:
    try:
        candidates = [page]
        context = getattr(page, "context", None)
        if context is not None:
            for other_page in list(getattr(context, "pages", []) or []):
                if other_page not in candidates:
                    candidates.append(other_page)
        for candidate in candidates:
            text = await _read_success_username_from_page(candidate)
            if text:
                return text
    except Exception:
        pass
    return ""


async def _wait_for_final_success_username(page, logger=None, profile_id: str = "", timeout_ms: int = 300000) -> str:
    remaining_ms = max(1000, int(timeout_ms or 300000))

    while remaining_ms > 0:
        page = await _resolve_active_signup_page(page, logger, profile_id)
        success_username = await _read_success_username(page)
        if success_username:
            logger and logger.info(f"[{profile_id}] Final success username detected: {success_username}")
            return success_username

        try:
            context = getattr(page, "context", None)
            if context is not None:
                for candidate in list(getattr(context, "pages", []) or []):
                    current_url = str(getattr(candidate, "url", "") or "").strip()
                    if "accounts.snapchat.com" in current_url and "/welcome" in current_url:
                        try:
                            await candidate.bring_to_front()
                        except Exception:
                            pass
        except Exception:
            pass

        try:
            await page.wait_for_timeout(1000)
        except Exception:
            await asyncio.sleep(1)
        remaining_ms -= 1000

    try:
        current_url = str(page.url or "").strip()
    except Exception:
        current_url = ""
    logger and logger.warning(
        f"[{profile_id}] Timed out waiting for final success username. current_url={current_url!r}"
    )
    return ""


async def _wait_for_signup_progress(
    page,
    logger=None,
    profile_id: str = "",
    timeout_ms: int | None = 600000,
    username_retry_provider=None,
    username_state=None,
    progress_callback=None,
    resubmit_callback=None,
    stall_state=None,
    entry_transition_grace_ms: int = 0,
) -> str:
    email_selectors = _EMAIL_INPUT_SELECTORS
    otp_selectors = _OTP_INPUT_SELECTORS
    submit_selectors = ["button:has-text('Agree and Continue')", "button[type='submit']"]
    username_selectors = [
        "#username",
        "input[name='username']",
        "input[placeholder*='username' i]",
    ]
    remaining_ms = None if timeout_ms is None else max(1000, int(timeout_ms))
    username_retry_attempts = 0
    unable_to_process_attempts = 0
    username_taken_warning_logged = False
    manual_submit_username = ""
    email_switch_clicked = False
    email_switch_failures = 0
    last_progress_step = ""
    entry_transition_until = time.monotonic() + max(0, int(entry_transition_grace_ms or 0)) / 1000.0

    async def set_progress(step: str) -> None:
        nonlocal last_progress_step
        if step and step != last_progress_step:
            last_progress_step = step
            await _emit_signup_progress(progress_callback, step, logger, profile_id)

    async def try_email_switch_step() -> str:
        """Handle a detected "use email instead" switch on the phone step.

        Returns ``"switched"`` (clicked + re-wait), ``"retry"`` (keep waiting),
        or ``"phone"`` (the switch is present but not clickable — fall back to
        the phone -> SMS OTP path instead of looping forever).
        """
        nonlocal email_switch_clicked, email_switch_failures, remaining_ms
        if not email_switch_clicked:
            clicked_email = await _click_use_email_instead(page, logger, profile_id)
            if clicked_email:
                email_switch_clicked = True
                await page.wait_for_timeout(900)
                if remaining_ms is not None:
                    remaining_ms -= 900
                return "switched"
            email_switch_failures += 1
            if email_switch_failures >= EMAIL_SWITCH_MAX_ATTEMPTS:
                logger and logger.warning(
                    f"[{profile_id}] 'Use email instead' was not clickable after "
                    f"{EMAIL_SWITCH_MAX_ATTEMPTS} attempt(s); falling back to phone verification."
                )
                await set_progress("awaiting_phone_verification")
                return "phone"
        await page.wait_for_timeout(300)
        if remaining_ms is not None:
            remaining_ms -= 300
        return "retry"

    async def trigger_form_refresh(reason: str, progress_step: str) -> bool:
        """Reload + re-enter the signup form. Raises once the refresh budget is
        spent so the runner escalates to a fresh profile + proxy."""
        if resubmit_callback is None or stall_state is None:
            return False
        stall_state["refresh_attempts"] = int(stall_state.get("refresh_attempts", 0)) + 1
        attempt = stall_state["refresh_attempts"]
        if attempt > SIGNUP_MAX_REFRESH_ATTEMPTS:
            raise RuntimeError(
                f"signup_stuck_retry_exhausted: {reason} (still stuck after "
                f"{SIGNUP_MAX_REFRESH_ATTEMPTS} refresh attempts)."
            )
        logger and logger.warning(
            f"[{profile_id}] {reason}; reloading and re-entering signup details "
            f"(attempt {attempt}/{SIGNUP_MAX_REFRESH_ATTEMPTS})."
        )
        await set_progress(progress_step)
        await resubmit_callback()
        stall_state["form_since"] = time.monotonic()
        stall_state["page_issue_since"] = None
        return True

    while remaining_ms is None or remaining_ms > 0:
        page = await _resolve_active_signup_page(page, logger, profile_id)

        # Hard blockers that need a brand-new profile + proxy (runner cleanup).
        if await _is_account_creation_blocked_visible(page):
            raise RuntimeError(
                "account_creation_blocked: Snapchat could not complete account creation "
                "(\"please try again on our mobile app\")."
            )
        # reCAPTCHA service unreachable -> reload the page and re-enter details.
        if await _is_recaptcha_connect_error_visible(page):
            if await trigger_form_refresh("reCAPTCHA service unreachable", "refreshing_signup_recaptcha"):
                await page.wait_for_timeout(1500)
                if remaining_ms is not None:
                    remaining_ms -= 1500
                continue

        current_visible_username = await _read_input_value(page, username_selectors)
        if current_visible_username and isinstance(username_state, dict):
            tracked_username = str(username_state.get("value") or "").strip()
            manual_override = bool(username_state.get("manual_override"))
            if tracked_username and not _same_username(tracked_username, current_visible_username):
                username_state["value"] = current_visible_username
                username_state["manual_override"] = True
                manual_submit_username = ""
                if not manual_override:
                    logger and logger.info(
                        f"[{profile_id}] Manual username override detected: {current_visible_username!r}. "
                        f"Full Auto retry is disabled for this signup run."
                    )

        handoff_stage = await _detect_signup_handoff_stage(page, logger, profile_id)
        if handoff_stage and stall_state is not None:
            stall_state["form_since"] = None
            stall_state["page_issue_since"] = None
        if handoff_stage == "welcome":
            await set_progress("signup_complete")
            return "welcome"

        if handoff_stage == "otp":
            await set_progress("awaiting_otp")
            return "otp"

        if handoff_stage == "phone":
            if time.monotonic() < entry_transition_until:
                await page.wait_for_timeout(300)
                if remaining_ms is not None:
                    remaining_ms -= 300
                continue
            await set_progress("awaiting_phone_verification")
            return "phone"

        if handoff_stage == "email":
            if time.monotonic() < entry_transition_until:
                await page.wait_for_timeout(300)
                if remaining_ms is not None:
                    remaining_ms -= 300
                continue
            await set_progress("awaiting_email_verification")
            return "email"

        if handoff_stage == "email_switch":
            await set_progress("clicking_use_email_instead")
            switch_result = await try_email_switch_step()
            if switch_result == "phone":
                return "phone"
            continue

        username_taken_visible = await _is_username_taken_error_visible(page)
        if username_taken_visible:
            try:
                current_url = str(page.url or "").strip()
            except Exception:
                current_url = ""
            email_switch_visible = await _is_use_email_switch_visible(page)
            email_input_visible = bool(await _visible_any(page, email_selectors))
            otp_input_visible = bool(await _visible_any(page, otp_selectors))
            logger and logger.info(
                f"[{profile_id}] Username retry context: url={current_url!r}, "
                f"username_input={bool(current_visible_username)}, "
                f"email_switch={email_switch_visible}, "
                f"email_input={email_input_visible}, "
                f"otp_input={otp_input_visible}."
            )
            if email_switch_visible:
                await set_progress("clicking_use_email_instead")
                switch_result = await try_email_switch_step()
                if switch_result == "phone":
                    return "phone"
                continue
            if email_input_visible:
                await set_progress("awaiting_email_verification")
                return "email"
            if otp_input_visible:
                await set_progress("awaiting_otp")
                return "otp"
            if await _is_phone_verification_step(page):
                await set_progress("awaiting_phone_verification")
                return "phone"
            manual_override = bool(isinstance(username_state, dict) and username_state.get("manual_override"))
            if username_retry_provider is not None and not manual_override:
                current_username = ""
                if isinstance(username_state, dict):
                    current_username = str(username_state.get("value") or "").strip()
                username_retry_attempts += 1
                if username_retry_attempts > 50:
                    raise RuntimeError("Exceeded the Full Auto Mode username retry limit.")
                await set_progress("retrying_signup_username")
                next_username = await _retry_taken_username(
                    page,
                    current_username,
                    username_retry_provider,
                    logger,
                    profile_id,
                )
                if not next_username:
                    username_taken_warning_logged = True
                    await page.wait_for_timeout(SIGNUP_USERNAME_RETRY_SETTLE_MS)
                    continue
                if isinstance(username_state, dict):
                    username_state["value"] = next_username
                await page.wait_for_timeout(SIGNUP_USERNAME_RETRY_SETTLE_MS)
                continue
            if logger and manual_override and not username_taken_warning_logged:
                logger.warning(
                    f"[{profile_id}] Username is already taken after manual override. Waiting for operator correction."
                )
                username_taken_warning_logged = True
            if logger and not username_taken_warning_logged:
                logger.warning(
                    f"[{profile_id}] Username is already taken and Full Auto Mode is off."
                )
                username_taken_warning_logged = True

        if not username_taken_visible and await _is_unable_to_process_error_visible(page):
            unable_to_process_attempts += 1
            if unable_to_process_attempts >= 15:
                raise RuntimeError("unable_to_process: Snapchat was unable to process the signup request after 15 retries.")
            handoff_stage = await _detect_signup_handoff_stage(page, logger, profile_id)
            if handoff_stage == "welcome":
                await set_progress("signup_complete")
                return "welcome"
            if handoff_stage == "otp":
                await set_progress("awaiting_otp")
                return "otp"
            if handoff_stage == "phone":
                await set_progress("awaiting_phone_verification")
                return "phone"
            if handoff_stage == "email":
                await set_progress("awaiting_email_verification")
                return "email"
            if handoff_stage == "email_switch":
                await set_progress("clicking_use_email_instead")
                switch_result = await try_email_switch_step()
                if switch_result == "phone":
                    return "phone"
                continue
            logger and logger.warning(
                f"[{profile_id}] Snapchat unable-to-process error detected; "
                f"retrying Agree and Continue ({unable_to_process_attempts}/15)."
            )
            clicked = await _click_signup_submit(page, logger, profile_id, fast=True)
            if not clicked:
                logger and logger.warning(
                    f"[{profile_id}] Could not click Agree and Continue while retrying unable-to-process error."
                )
            await page.wait_for_timeout(SIGNUP_UNABLE_TO_PROCESS_RETRY_SETTLE_MS)
            if remaining_ms is not None:
                remaining_ms -= SIGNUP_UNABLE_TO_PROCESS_RETRY_SETTLE_MS
            continue
        unable_to_process_attempts = 0

        submit_selector = await _visible_any(page, submit_selectors)
        if submit_selector:
            try:
                disabled = await page.evaluate(_JS_IS_DISABLED, submit_selector)
                if not disabled:
                    manual_override = bool(isinstance(username_state, dict) and username_state.get("manual_override"))
                    current_username = ""
                    if isinstance(username_state, dict):
                        current_username = str(username_state.get("value") or "").strip()
                    if manual_override and current_username and not username_taken_visible and not _same_username(manual_submit_username, current_username):
                        logger and logger.info(
                            f"[{profile_id}] Signup submit is enabled after manual username correction; submitting {current_username!r}."
                        )
                        clicked = await _click_signup_submit(page, logger, profile_id, fast=True)
                        if clicked:
                            manual_submit_username = current_username
                            username_taken_warning_logged = False
                            await page.wait_for_timeout(1600)
                            continue
                        logger and logger.warning(
                            f"[{profile_id}] Could not submit after manual username correction."
                        )
                    else:
                        await set_progress("waiting_for_signup_handoff")
                        logger and logger.info(f"[{profile_id}] Signup submit is enabled again, allowing manual username fix to continue.")
                else:
                    await set_progress("waiting_for_signup_handoff")
                    logger and logger.info(f"[{profile_id}] Waiting on signup page for manual correction or verification handoff.")
            except Exception:
                pass

        # A2: still sitting on the signup form. If the captcha challenge never
        # rendered (no badge in the corner) and we've waited past the stall
        # window, reload + re-enter the details; escalate after the budget.
        if stall_state is not None and resubmit_callback is not None:
            on_form = bool(await _visible_any(page, ["#firstname", "#username", *submit_selectors]))
            if on_form:
                stall_state["page_issue_since"] = None
                # The page was (re)loaded and Snapchat cleared every field —
                # usually a manual operator refresh. Re-enter the saved
                # credentials right away instead of waiting out the stall timer.
                refill_callback = stall_state.get("refill")
                if refill_callback is not None and await _signup_form_is_blank(page):
                    refill_attempts = int(stall_state.get("blank_refill_attempts", 0)) + 1
                    stall_state["blank_refill_attempts"] = refill_attempts
                    if refill_attempts <= SIGNUP_MAX_REFRESH_ATTEMPTS:
                        logger and logger.info(
                            f"[{profile_id}] Signup form is blank (page was refreshed); "
                            f"re-entering the saved details "
                            f"(refill {refill_attempts}/{SIGNUP_MAX_REFRESH_ATTEMPTS})."
                        )
                        await set_progress("refilling_signup_form")
                        try:
                            await refill_callback()
                        except Exception as exc:
                            logger and logger.warning(
                                f"[{profile_id}] Blank-form refill failed: {exc}"
                            )
                        stall_state["form_since"] = time.monotonic()
                        await page.wait_for_timeout(1200)
                        if remaining_ms is not None:
                            remaining_ms -= 1200
                        continue
                else:
                    # Form has content again — reset the consecutive-refill guard
                    # so a later manual refresh is honored afresh.
                    stall_state["blank_refill_attempts"] = 0

                if stall_state.get("form_since") is None:
                    stall_state["form_since"] = time.monotonic()
                elapsed = time.monotonic() - float(stall_state.get("form_since") or time.monotonic())
                # Standard stall: no captcha challenge ever rendered.
                if elapsed >= SIGNUP_STALL_SECONDS and not await _recaptcha_widget_present(page):
                    if await trigger_form_refresh(
                        "signup form stalled with no captcha challenge", "refreshing_stalled_signup"
                    ):
                        await page.wait_for_timeout(1500)
                        if remaining_ms is not None:
                            remaining_ms -= 1500
                        continue
                # Hard-stall backstop: stuck on the signup page for a very long
                # time and Agree and Continue never became clickable — even with
                # a captcha present. Reload + re-enter as a last resort.
                elif elapsed >= SIGNUP_HARD_STALL_SECONDS and not await _submit_is_clickable(
                    page, submit_selectors
                ):
                    if await trigger_form_refresh(
                        "signup stuck; Agree and Continue never became clickable",
                        "refreshing_stuck_signup",
                    ):
                        await page.wait_for_timeout(1500)
                        if remaining_ms is not None:
                            remaining_ms -= 1500
                        continue
            else:
                stall_state["form_since"] = None
                stall_state["blank_refill_attempts"] = 0
                if await _is_blank_signup_shell(page):
                    if await trigger_form_refresh(
                        "signup page loaded a blank Snapchat shell", "refreshing_signup_blank_shell"
                    ):
                        await page.wait_for_timeout(1500)
                        if remaining_ms is not None:
                            remaining_ms -= 1500
                        continue
                if stall_state.get("page_issue_since") is None:
                    stall_state["page_issue_since"] = time.monotonic()
                page_issue_elapsed = time.monotonic() - float(
                    stall_state.get("page_issue_since") or time.monotonic()
                )
                if page_issue_elapsed >= SIGNUP_STALL_SECONDS:
                    if await trigger_form_refresh(
                        "signup page/form not detected", "refreshing_signup_page_issue"
                    ):
                        await page.wait_for_timeout(1500)
                        if remaining_ms is not None:
                            remaining_ms -= 1500
                        continue

        await page.wait_for_timeout(1000)
        if remaining_ms is not None:
            remaining_ms -= 1000

    return ""


# ---------------------------------------------------------------------------
# Steps 2+ — email verification + OTP
# ---------------------------------------------------------------------------

async def _emit_username(callback, username, logger, profile_id):
    normalized = str(username or "").strip()
    if not normalized or callback is None:
        return
    try:
        outcome = callback(normalized)
        if asyncio.iscoroutine(outcome):
            await outcome
    except Exception as exc:
        logger and logger.warning(
            f"[{profile_id}] username_detected callback raised: {exc}"
        )


def _is_valid_email(email):
    normalized = str(email or "").strip()
    if not normalized or "@" not in normalized:
        return False
    local_part, _, domain = normalized.partition("@")
    return bool(local_part and "." in domain)


async def _fetch_email_from_provider(email_fetcher, force_new: bool, logger=None, profile_id: str = "") -> str:
    if email_fetcher is None:
        return ""
    try:
        try:
            fetched_email = email_fetcher(force_new=force_new)
        except TypeError:
            fetched_email = email_fetcher()
        if asyncio.iscoroutine(fetched_email):
            fetched_email = await fetched_email
        fetched_email = str(fetched_email or "").strip()
        if _is_valid_email(fetched_email):
            return fetched_email
    except Exception as exc:
        logger and logger.warning(f"[{profile_id}] Could not fetch verification email from SnapBoard: {exc}")
    return ""


async def _fetch_phone_from_provider(phone_fetcher, force_new: bool, logger=None, profile_id: str = "") -> str:
    if phone_fetcher is None:
        return ""
    try:
        try:
            fetched_phone = phone_fetcher(force_new=force_new)
        except TypeError:
            fetched_phone = phone_fetcher()
        if asyncio.iscoroutine(fetched_phone):
            fetched_phone = await fetched_phone
        return str(fetched_phone or "").strip()
    except Exception as exc:
        logger and logger.warning(f"[{profile_id}] Could not fetch verification phone from SnapBoard: {exc}")
    return ""


async def _fetch_otp_from_provider(otp_fetcher, email: str = "") -> str:
    if otp_fetcher is None:
        return ""
    try:
        try:
            otp = otp_fetcher(email=str(email or "").strip())
        except TypeError:
            otp = otp_fetcher()
        if asyncio.iscoroutine(otp):
            otp = await otp
        return str(otp or "").strip()
    except Exception:
        return ""


async def _fetch_sms_from_provider(sms_fetcher, phone: str = "") -> str:
    if sms_fetcher is None:
        return ""
    try:
        try:
            code = sms_fetcher(phone=str(phone or "").strip())
        except TypeError:
            code = sms_fetcher()
        if asyncio.iscoroutine(code):
            code = await code
        return str(code or "").strip()
    except Exception:
        return ""


async def _fill_and_submit_verification_email(signup_page, email: str, logger=None, profile_id: str = "") -> bool:
    for email_sel in _EMAIL_INPUT_SELECTORS:
        try:
            loc = signup_page.locator(email_sel).first
            if await loc.is_visible():
                typed = await _humanized_type_only(signup_page, email_sel, email, logger, f"[{profile_id}] email")
                if not typed or str(await loc.input_value() or "").strip().lower() != str(email).strip().lower():
                    logger and logger.warning(f"[{profile_id}] Verification email value was not accepted by Snapchat.")
                    continue
                await signup_page.wait_for_timeout(400)
                enabled = await _wait_enabled(signup_page, "button[type='submit']", timeout_ms=5000)
                if not enabled:
                    logger and logger.warning(f"[{profile_id}] Verification email submit stayed disabled.")
                    continue
                await _human_pause(signup_page, 250, 800)
                clicked = await _js_click(signup_page, "button[type='submit']")
                if not clicked:
                    logger and logger.warning(f"[{profile_id}] Verification email submit click failed.")
                    continue
                await signup_page.wait_for_timeout(1200)
                return True
        except Exception:
            continue
    return False


async def _is_email_already_verified_error_visible(page) -> bool:
    try:
        return bool(
            await page.evaluate(
                """
                () => {
                    const normalize = (value) => String(value || "")
                        .replace(/\\s+/g, " ")
                        .trim()
                        .toLowerCase();
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
                    const nodes = Array.from(document.querySelectorAll(
                        "p[data-testid='error-text'], [data-testid='error-text'], [class*='GenericFormLevelErrorMessage'], [role='alert']"
                    ));
                    return nodes.some((node) => {
                        if (!isVisible(node)) return false;
                        const text = normalize(node.innerText || node.textContent);
                        return text.includes("email has already been verified by another account")
                            || (
                                text.includes("please enter another email")
                                && text.includes("verified by another account")
                            );
                    });
                }
                """
            )
        )
    except Exception:
        return False


async def _is_wrong_verification_code_error_visible(page) -> bool:
    return await _page_has_visible_text(page, _WRONG_VERIFICATION_CODE_ERROR_MARKERS)


async def _click_visible_verification_submit(signup_page, logger=None, profile_id: str = "") -> bool:
    for btn_sel in ["button[type='submit']", "button:has-text('Verify')", "button:has-text('Confirm')", "button:has-text('Continue')", "button:has-text('Next')"]:
        try:
            loc = signup_page.locator(btn_sel).first
            if await loc.is_visible():
                await _wait_enabled(signup_page, btn_sel, timeout_ms=5000)
                await _human_pause(signup_page, 250, 850)
                await loc.click()
                return True
        except Exception:
            continue
    return False


async def _wait_for_stage_after_otp(
    signup_page,
    logger,
    profile_id: str,
    username_retry_provider=None,
    username_state=None,
    progress_callback=None,
    resubmit_callback=None,
    stall_state=None,
    max_attempts: int = 8,
) -> str:
    stage = ""
    for attempt in range(max(1, int(max_attempts or 1))):
        signup_page = await _resolve_active_signup_page(signup_page, logger, profile_id)
        if attempt:
            await signup_page.wait_for_timeout(2500)
        stage = await _wait_for_signup_progress(
            signup_page,
            logger,
            profile_id,
            timeout_ms=300000 if attempt == 0 else 15000,
            username_retry_provider=username_retry_provider,
            username_state=username_state,
            progress_callback=progress_callback,
            resubmit_callback=resubmit_callback,
            stall_state=stall_state,
        )
        if stage != "otp":
            return stage
    return stage


async def _handle_optional_phone_sms_verification(
    signup_page,
    phone_fetcher,
    sms_fetcher,
    result: dict,
    logger,
    profile_id: str,
    username_detected_callback=None,
    username_retry_provider=None,
    username_state=None,
    progress_callback=None,
    resubmit_callback=None,
    stall_state=None,
) -> dict:
    if phone_fetcher is None:
        logger and logger.warning(f"[{profile_id}] Phone verification requested but no SnapBoard phone fetcher is available.")
        return result

    stage = ""
    max_attempts = max(1, int(PHONE_VERIFICATION_MAX_ATTEMPTS or 1))
    for attempt in range(max_attempts):
        force_new = attempt > 0
        await _emit_signup_progress(progress_callback, "fetching_phone_verification", logger, profile_id)
        try:
            phone = phone_fetcher(force_new=force_new)
        except TypeError:
            phone = phone_fetcher()
        if asyncio.iscoroutine(phone):
            phone = await phone
        phone = str(phone or "").strip()
        if not phone:
            logger and logger.warning(f"[{profile_id}] Could not retrieve phone number from SnapBoard.")
            return result

        signup_page = await _resolve_active_signup_page(signup_page, logger, profile_id)
        await _emit_signup_progress(progress_callback, "filling_phone_verification", logger, profile_id)
        if not await _fill_and_submit_phone_number(signup_page, phone, logger, profile_id):
            logger and logger.warning(f"[{profile_id}] Could not submit phone number for SMS verification.")
            if attempt + 1 < max_attempts:
                continue
            raise RuntimeError(
                f"phone_verification_rejected: Could not submit phone number after {max_attempts} attempt(s)."
            )
        result["phone_entered"] = True

        stage = await _wait_for_signup_progress(
            signup_page,
            logger,
            profile_id,
            timeout_ms=300000,
            username_retry_provider=username_retry_provider,
            username_state=username_state,
            progress_callback=progress_callback,
            resubmit_callback=resubmit_callback,
            stall_state=stall_state,
            entry_transition_grace_ms=5000,
        )
        if stage == "welcome":
            result["final_username"] = await _read_success_username(signup_page)
            await _emit_username(username_detected_callback, result["final_username"], logger, profile_id)
            return result
        if stage == "otp":
            break
        if attempt + 1 < max_attempts and stage == "phone":
            logger and logger.warning(
                f"[{profile_id}] Phone number was not accepted by Snapchat; requesting a replacement "
                f"({attempt + 2}/{max_attempts})."
            )
            continue
        logger and logger.warning(f"[{profile_id}] SMS OTP input did not appear after phone submission.")
        raise RuntimeError(
            f"phone_verification_rejected: SMS OTP input did not appear after {max_attempts} phone number attempt(s)."
        )

    if stage != "otp":
        logger and logger.warning(f"[{profile_id}] SMS OTP input did not appear after phone submission.")
        raise RuntimeError(
            f"phone_verification_rejected: SMS OTP input did not appear after {max_attempts} phone number attempt(s)."
        )

    if sms_fetcher is None:
        logger and logger.warning(f"[{profile_id}] SMS OTP requested but no SnapBoard SMS fetcher is available.")
        return result

    await _emit_signup_progress(progress_callback, "fetching_sms_otp", logger, profile_id)
    sms_code = await _fetch_sms_from_provider(sms_fetcher, phone)
    if not sms_code:
        # The code never came through — rather than failing the account (which
        # deletes the profile and recreates it), go back, rotate to a fresh
        # number, and refetch the SMS on the same account.
        logger and logger.warning(
            f"[{profile_id}] Could not retrieve SMS OTP from SnapBoard; "
            "trying back + fresh-number recovery."
        )
        sms_code, signup_page = await _recover_sms_via_new_phone(
            signup_page,
            phone_fetcher,
            sms_fetcher,
            logger,
            profile_id,
            progress_callback=progress_callback,
        )
    if not sms_code:
        logger and logger.warning(f"[{profile_id}] Could not retrieve SMS OTP from SnapBoard.")
        return result

    signup_page = await _resolve_active_signup_page(signup_page, logger, profile_id)
    try:
        await signup_page.bring_to_front()
    except Exception:
        pass

    otp_typed = await _type_otp_code(signup_page, _OTP_INPUT_SELECTORS, sms_code, logger, profile_id)
    await signup_page.wait_for_timeout(500)
    if otp_typed and await _click_visible_verification_submit(signup_page, logger, profile_id):
        result["sms_otp_entered"] = True
        logger and logger.info(f"[{profile_id}] SMS OTP submitted.")
        await signup_page.wait_for_timeout(1200)
    else:
        logger and logger.warning(f"[{profile_id}] Could not verify the SMS OTP was typed; not submitting.")
        result["sms_otp_entered"] = False

    wrong_sms_attempts = 0
    while result.get("sms_otp_entered") and await _is_wrong_verification_code_error_visible(signup_page):
        wrong_sms_attempts += 1
        if wrong_sms_attempts > WRONG_CODE_MAX_RECOVERY_ATTEMPTS:
            logger and logger.warning(
                f"[{profile_id}] Snapchat rejected replacement SMS codes after "
                f"{WRONG_CODE_MAX_RECOVERY_ATTEMPTS} recovery attempt(s)."
            )
            result["sms_otp_entered"] = False
            break

        logger and logger.warning(
            f"[{profile_id}] Snapchat rejected the SMS OTP; ordering a fresh number "
            f"({wrong_sms_attempts}/{WRONG_CODE_MAX_RECOVERY_ATTEMPTS})."
        )
        await _emit_signup_progress(progress_callback, "retrying_otp", logger, profile_id)
        sms_code, signup_page = await _recover_sms_via_new_phone(
            signup_page,
            phone_fetcher,
            sms_fetcher,
            logger,
            profile_id,
            progress_callback=progress_callback,
            max_attempts=1,
        )
        sms_code = str(sms_code or "").strip()
        if not sms_code:
            result["sms_otp_entered"] = False
            break

        signup_page = await _resolve_active_signup_page(signup_page, logger, profile_id)
        otp_typed = await _type_otp_code(signup_page, _OTP_INPUT_SELECTORS, sms_code, logger, profile_id)
        await signup_page.wait_for_timeout(500)
        if otp_typed and await _click_visible_verification_submit(signup_page, logger, profile_id):
            result["sms_otp_entered"] = True
            await signup_page.wait_for_timeout(1200)
        else:
            result["sms_otp_entered"] = False
            break

    if result.get("sms_otp_entered"):
        final_stage = await _wait_for_stage_after_otp(
            signup_page,
            logger,
            profile_id,
            username_retry_provider=username_retry_provider,
            username_state=username_state,
            progress_callback=progress_callback,
            resubmit_callback=resubmit_callback,
            stall_state=stall_state,
        )
        if final_stage == "welcome":
            result["final_username"] = await _read_success_username(signup_page)
            await _emit_username(username_detected_callback, result["final_username"], logger, profile_id)
        if not str(result.get("final_username") or "").strip():
            result["final_username"] = await _wait_for_final_success_username(
                signup_page,
                logger,
                profile_id,
                timeout_ms=240000,
            )
            await _emit_username(username_detected_callback, result["final_username"], logger, profile_id)

    return result


async def _click_verification_back_button(page, logger=None, profile_id: str = "") -> bool:
    """Click the back arrow on the email / "Enter Code" verification card.

    Snapchat's verification steps show a back chevron in the form header
    (``svg[class*='FormHeader_button']`` inside ``div[class*='FormHeader_title']``).
    Clicking it returns to the email / phone entry step so a fresh address or
    number can be submitted when the current one never produced an OTP.
    Best-effort: returns False if no back control is found.
    """
    selectors = [
        "svg[class*='FormHeader_button']",
        "[class*='FormHeader_title'] svg",
        "[class*='FormHeader'] button",
        "button[aria-label*='back' i]",
        "[role='button'][aria-label*='back' i]",
        "[class*='back' i][role='button']",
    ]
    for selector in selectors:
        try:
            loc = page.locator(selector).first
            if await loc.count() == 0 or not await loc.is_visible():
                continue
            await _safe_scroll_into_view(loc)
            await loc.click(force=True, timeout=2500)
            await page.wait_for_timeout(800)
            logger and logger.info(f"[{profile_id}] Clicked verification back button ({selector!r}).")
            return True
        except Exception:
            continue
    return False


async def _recover_otp_via_back_and_new_email(
    signup_page,
    otp_fetcher,
    email_fetcher,
    logger,
    profile_id,
    progress_callback=None,
    max_attempts: int = 2,
):
    """Recover when no OTP arrives — usually a stale SnapBoard or an unusable
    email. Go back to the email step, order a fresh email, resubmit it, wait for
    the code step again, and refetch the OTP. Strictly additive: it only runs
    after a normal OTP fetch already came back empty, and any failure simply
    returns ``("", page)`` so the caller gives up exactly as it did before.
    Returns ``(otp, signup_page)``.
    """
    if email_fetcher is None:
        return "", signup_page
    for attempt in range(1, max(1, int(max_attempts)) + 1):
        signup_page = await _resolve_active_signup_page(signup_page, logger, profile_id)
        if not await _is_email_verification_step(signup_page):
            clicked_back = await _click_verification_back_button(signup_page, logger, profile_id)
            if not clicked_back and not await _is_email_verification_step(signup_page):
                return "", signup_page

        # Wait for the email entry step to (re)appear before re-ordering.
        on_email_step = await _is_email_verification_step(signup_page)
        for _ in range(6):
            if on_email_step:
                break
            await signup_page.wait_for_timeout(500)
            on_email_step = await _is_email_verification_step(signup_page)
        if not on_email_step:
            continue

        logger and logger.info(
            f"[{profile_id}] OTP never arrived; ordering a fresh email and resubmitting "
            f"(attempt {attempt}/{max_attempts})."
        )
        await _emit_signup_progress(progress_callback, "fetching_replacement_email", logger, profile_id)
        new_email = await _fetch_email_from_provider(email_fetcher, force_new=True, logger=logger, profile_id=profile_id)
        if not _is_valid_email(new_email):
            continue
        await _emit_signup_progress(progress_callback, "filling_email_verification", logger, profile_id)
        if not await _fill_and_submit_verification_email(signup_page, new_email, logger, profile_id):
            continue

        stage = await _wait_for_signup_progress(
            signup_page,
            logger,
            profile_id,
            timeout_ms=120000,
            progress_callback=progress_callback,
            entry_transition_grace_ms=5000,
        )
        if stage != "otp":
            logger and logger.warning(
                f"[{profile_id}] Replacement email did not reach OTP stage; observed {stage!r}."
            )
            continue
        await _emit_signup_progress(progress_callback, "fetching_otp", logger, profile_id)
        otp = await _fetch_otp_from_provider(otp_fetcher, new_email)
        if otp:
            return str(otp), signup_page
    return "", signup_page


async def _recover_sms_via_new_phone(
    signup_page,
    phone_fetcher,
    sms_fetcher,
    logger,
    profile_id,
    progress_callback=None,
    max_attempts: int = 2,
):
    """Recover when the SMS code never arrives — the number went dead or the
    order got stuck. Go back to the phone-entry step, order a fresh number
    (force_new, which waits out SnapBoard's ~60s redo cooldown so the number
    actually changes), resubmit it, wait for the code step again, and refetch the
    SMS. Mirrors :func:`_recover_otp_via_back_and_new_email` for the phone path:
    rather than failing the whole account (which tears the profile down and
    recreates from scratch), we keep the same account and just rotate the number
    and get the OTP. Strictly additive — any failure returns ``("", page)`` so
    the caller behaves exactly as before. Returns ``(sms_code, signup_page)``.
    """
    if phone_fetcher is None or sms_fetcher is None:
        return "", signup_page
    for attempt in range(1, max(1, int(max_attempts)) + 1):
        signup_page = await _resolve_active_signup_page(signup_page, logger, profile_id)
        # Return to the phone-entry step so a fresh number can be submitted.
        if not await _is_phone_verification_step(signup_page):
            clicked_back = await _click_verification_back_button(signup_page, logger, profile_id)
            if not clicked_back and not await _is_phone_verification_step(signup_page):
                return "", signup_page

        on_phone_step = await _is_phone_verification_step(signup_page)
        for _ in range(6):
            if on_phone_step:
                break
            await signup_page.wait_for_timeout(500)
            on_phone_step = await _is_phone_verification_step(signup_page)
        if not on_phone_step:
            continue

        logger and logger.info(
            f"[{profile_id}] SMS code never arrived; ordering a fresh number and resubmitting "
            f"(attempt {attempt}/{max_attempts})."
        )
        await _emit_signup_progress(progress_callback, "fetching_phone_verification", logger, profile_id)
        phone = await _fetch_phone_from_provider(phone_fetcher, force_new=True, logger=logger, profile_id=profile_id)
        if not phone:
            continue
        await _emit_signup_progress(progress_callback, "filling_phone_verification", logger, profile_id)
        if not await _fill_and_submit_phone_number(signup_page, phone, logger, profile_id):
            continue

        stage = await _wait_for_signup_progress(
            signup_page,
            logger,
            profile_id,
            timeout_ms=120000,
            progress_callback=progress_callback,
            entry_transition_grace_ms=5000,
        )
        if stage == "welcome":
            # The fresh number was enough on its own — no SMS step at all.
            return "", signup_page
        if stage != "otp":
            continue
        await _emit_signup_progress(progress_callback, "fetching_sms_otp", logger, profile_id)
        sms_code = await _fetch_sms_from_provider(sms_fetcher, phone)
        if sms_code:
            return sms_code, signup_page
    return "", signup_page


async def _handle_verification(
    signup_page,
    email: str,
    otp_fetcher,
    logger,
    profile_id: str,
    username_detected_callback=None,
    email_fetcher=None,
    username_retry_provider=None,
    username_state=None,
    progress_callback=None,
    resubmit_callback=None,
    stall_state=None,
    phone_fetcher=None,
    sms_fetcher=None,
) -> dict:
    email = str(email or "").strip()
    if not _is_valid_email(email):
        email = ""
    result = {
        "reached_verification": False,
        "otp_entered": False,
        "phone_entered": False,
        "sms_otp_entered": False,
        "final_username": "",
        "email": email,
    }
    signup_page = await _resolve_active_signup_page(signup_page, logger, profile_id)
    await signup_page.wait_for_timeout(2000)

    async def switch_to_phone(reason: str) -> dict:
        """Email verification failed too many times — click "Use Phone Number
        Instead" and run the phone -> SMS OTP path instead of failing the account."""
        page = await _resolve_active_signup_page(signup_page, logger, profile_id)
        await _emit_signup_progress(progress_callback, "switching_to_phone", logger, profile_id)
        logger and logger.warning(f"[{profile_id}] {reason}; switching to phone verification.")
        clicked = await _click_use_phone_instead(page, logger, profile_id)
        if clicked:
            await page.wait_for_timeout(900)
        result["reached_verification"] = True
        return await _handle_optional_phone_sms_verification(
            page,
            phone_fetcher,
            sms_fetcher,
            result,
            logger,
            profile_id,
            username_detected_callback=username_detected_callback,
            username_retry_provider=username_retry_provider,
            username_state=username_state,
            progress_callback=progress_callback,
            resubmit_callback=resubmit_callback,
            stall_state=stall_state,
        )

    email_verify_failures = 0

    stage = await _wait_for_signup_progress(
        signup_page,
        logger,
        profile_id,
        timeout_ms=None,
        username_retry_provider=username_retry_provider,
        username_state=username_state,
        progress_callback=progress_callback,
        resubmit_callback=resubmit_callback,
        stall_state=stall_state,
    )
    if stage == "welcome":
        result["reached_verification"] = True
        result["final_username"] = await _read_success_username(signup_page)
        await _emit_username(username_detected_callback, result["final_username"], logger, profile_id)
        return result
    if stage == "phone":
        result["reached_verification"] = True
        return await _handle_optional_phone_sms_verification(
            signup_page,
            phone_fetcher,
            sms_fetcher,
            result,
            logger,
            profile_id,
            username_detected_callback=username_detected_callback,
            username_retry_provider=username_retry_provider,
            username_state=username_state,
            progress_callback=progress_callback,
            resubmit_callback=resubmit_callback,
            stall_state=stall_state,
        )

    # Fill email input if shown. Nyxify can start without an email; when
    # Snapchat asks for verification, fetch it from SnapBoard just in time.
    # SnapBoard may report "No pending email order for this account. Get email
    # first." — i.e. no email is ready yet — so we re-order/retry a few times
    # before giving up (the first attempt is "Get Email", later ones force a new
    # one). "Try until proceed", bounded so a dead row can't hang forever.
    if stage == "email" and not _is_valid_email(email) and email_fetcher is not None:
        for email_attempt in range(1, EMAIL_ORDER_MAX_ATTEMPTS + 1):
            await _emit_signup_progress(
                progress_callback,
                "fetching_email" if email_attempt == 1 else "fetching_replacement_email",
                logger,
                profile_id,
            )
            email = await _fetch_email_from_provider(
                email_fetcher,
                force_new=email_attempt > 1,
                logger=logger,
                profile_id=profile_id,
            )
            if _is_valid_email(email):
                logger and logger.info(
                    f"[{profile_id}] Retrieved verification email from SnapBoard "
                    f"(attempt {email_attempt}/{EMAIL_ORDER_MAX_ATTEMPTS})."
                )
                break
            logger and logger.warning(
                f"[{profile_id}] SnapBoard returned no verification email "
                f"(likely \"no pending order\") — attempt {email_attempt}/{EMAIL_ORDER_MAX_ATTEMPTS}."
            )
            signup_page = await _resolve_active_signup_page(signup_page, logger, profile_id)
            await signup_page.wait_for_timeout(3000)
        result["email"] = email
        if not _is_valid_email(email):
            raise RuntimeError(
                "email_order_unavailable: SnapBoard had no verification email to provide after "
                f"{EMAIL_ORDER_MAX_ATTEMPTS} attempts."
            )

    signup_page = await _resolve_active_signup_page(signup_page, logger, profile_id)
    if stage == "email" and _is_valid_email(email):
        await _emit_signup_progress(progress_callback, "filling_email_verification", logger, profile_id)
        submitted_email = await _fill_and_submit_verification_email(signup_page, email, logger, profile_id)
        if not submitted_email and email_fetcher is not None:
            logger and logger.warning(f"[{profile_id}] Could not submit verification email; requesting a replacement.")
            await _emit_signup_progress(progress_callback, "fetching_replacement_email", logger, profile_id)
            replacement_email = await _fetch_email_from_provider(
                email_fetcher,
                force_new=True,
                logger=logger,
                profile_id=profile_id,
            )
            if _is_valid_email(replacement_email):
                email = replacement_email
                result["email"] = email
                await _emit_signup_progress(progress_callback, "filling_email_verification", logger, profile_id)
                submitted_email = await _fill_and_submit_verification_email(
                    signup_page, email, logger, profile_id
                )
        if not submitted_email:
            logger and logger.warning(f"[{profile_id}] Verification email could not be submitted.")

        for replacement_attempt in range(1, EMAIL_VERIFY_MAX_ATTEMPTS + 1):
            signup_page = await _resolve_active_signup_page(signup_page, logger, profile_id)
            if not await _is_email_already_verified_error_visible(signup_page):
                break
            email_verify_failures += 1
            if email_verify_failures >= EMAIL_VERIFY_MAX_ATTEMPTS:
                return await switch_to_phone(
                    f"email already verified after {EMAIL_VERIFY_MAX_ATTEMPTS} attempt(s)"
                )
            if email_fetcher is None:
                logger and logger.warning(
                    f"[{profile_id}] Snapchat rejected the verification email as already verified, "
                    "but no SnapBoard email fetcher is available."
                )
                break

            logger and logger.warning(
                f"[{profile_id}] Snapchat rejected verification email {email!r} as already verified; "
                f"requesting replacement email ({replacement_attempt}/{EMAIL_VERIFY_MAX_ATTEMPTS})."
            )
            await _emit_signup_progress(progress_callback, "fetching_replacement_email", logger, profile_id)
            replacement_email = await _fetch_email_from_provider(
                email_fetcher,
                force_new=True,
                logger=logger,
                profile_id=profile_id,
            )
            if not _is_valid_email(replacement_email):
                logger and logger.warning(f"[{profile_id}] Replacement email request did not return a valid email.")
                break
            if replacement_email.strip().lower() == email.strip().lower():
                logger and logger.warning(
                    f"[{profile_id}] Replacement email request returned the same email {replacement_email!r}."
                )
                break

            email = replacement_email
            result["email"] = email
            await _emit_signup_progress(progress_callback, "filling_email_verification", logger, profile_id)
            submitted_replacement = await _fill_and_submit_verification_email(signup_page, email, logger, profile_id)
            if not submitted_replacement:
                logger and logger.warning(f"[{profile_id}] Could not submit replacement verification email.")
                break

    otp_selectors = _OTP_INPUT_SELECTORS
    if stage in {"email", "otp", "welcome"}:
        result["reached_verification"] = True
    if stage != "otp":
        stage = await _wait_for_signup_progress(
            signup_page,
            logger,
            profile_id,
            timeout_ms=None,
            username_retry_provider=username_retry_provider,
            username_state=username_state,
            progress_callback=progress_callback,
            resubmit_callback=resubmit_callback,
            stall_state=stall_state,
            entry_transition_grace_ms=5000 if stage == "email" else 0,
        )
    if stage == "welcome":
        result["reached_verification"] = True
        result["final_username"] = await _read_success_username(signup_page)
        await _emit_username(username_detected_callback, result["final_username"], logger, profile_id)
        return result
    # The email path sometimes surfaces the phone step ("Use email instead" was
    # present but not clickable, or Snapchat defaults to phone). Route to the
    # phone -> SMS OTP handler rather than giving up.
    if stage == "phone":
        result["reached_verification"] = True
        return await _handle_optional_phone_sms_verification(
            signup_page,
            phone_fetcher,
            sms_fetcher,
            result,
            logger,
            profile_id,
            username_detected_callback=username_detected_callback,
            username_retry_provider=username_retry_provider,
            username_state=username_state,
            progress_callback=progress_callback,
            resubmit_callback=resubmit_callback,
            stall_state=stall_state,
        )
    if stage != "otp":
        logger and logger.warning(f"[{profile_id}] OTP input did not appear. Manual verification needed.")
        return result

    result["reached_verification"] = True
    logger and logger.info(f"[{profile_id}] OTP field visible. Fetching from SnapBoard.")
    await _emit_signup_progress(progress_callback, "fetching_otp", logger, profile_id)
    otp = await _fetch_otp_from_provider(otp_fetcher, email)
    if not otp:
        logger and logger.warning(
            f"[{profile_id}] Could not retrieve OTP from SnapBoard; trying back + fresh-email recovery."
        )
        otp, signup_page = await _recover_otp_via_back_and_new_email(
            signup_page,
            otp_fetcher,
            email_fetcher,
            logger,
            profile_id,
            progress_callback=progress_callback,
        )
    if not otp:
        logger and logger.warning(f"[{profile_id}] Could not retrieve OTP from SnapBoard.")
        return result

    signup_page = await _resolve_active_signup_page(signup_page, logger, profile_id)
    try:
        await signup_page.bring_to_front()
    except Exception:
        pass

    otp_typed = await _type_otp_code(signup_page, otp_selectors, otp, logger, profile_id)
    await signup_page.wait_for_timeout(500)

    if otp_typed and await _click_visible_verification_submit(signup_page, logger, profile_id):
        result["otp_entered"] = True
        logger and logger.info(f"[{profile_id}] OTP submitted.")
        await signup_page.wait_for_timeout(1200)
    else:
        logger and logger.warning(f"[{profile_id}] Could not verify the OTP was typed; not submitting.")
        result["otp_entered"] = False

    wrong_otp_attempts = 0
    while result.get("otp_entered") and await _is_wrong_verification_code_error_visible(signup_page):
        wrong_otp_attempts += 1
        email_verify_failures += 1
        if email_verify_failures >= EMAIL_VERIFY_MAX_ATTEMPTS:
            logger and logger.warning(
                f"[{profile_id}] Snapchat rejected email OTP codes after "
                f"{EMAIL_VERIFY_MAX_ATTEMPTS} attempt(s)."
            )
            result["otp_entered"] = False
            return await switch_to_phone(
                f"email OTP rejected after {EMAIL_VERIFY_MAX_ATTEMPTS} attempt(s)"
            )

        logger and logger.warning(
            f"[{profile_id}] Snapchat rejected the email OTP; ordering a fresh email "
            f"({wrong_otp_attempts}/{EMAIL_VERIFY_MAX_ATTEMPTS})."
        )
        await _emit_signup_progress(progress_callback, "retrying_otp", logger, profile_id)
        otp, signup_page = await _recover_otp_via_back_and_new_email(
            signup_page,
            otp_fetcher,
            email_fetcher,
            logger,
            profile_id,
            progress_callback=progress_callback,
            max_attempts=1,
        )
        otp = str(otp or "").strip()
        if not otp:
            result["otp_entered"] = False
            break

        signup_page = await _resolve_active_signup_page(signup_page, logger, profile_id)
        otp_typed = await _type_otp_code(signup_page, otp_selectors, otp, logger, profile_id)
        await signup_page.wait_for_timeout(500)
        if otp_typed and await _click_visible_verification_submit(signup_page, logger, profile_id):
            result["otp_entered"] = True
            await signup_page.wait_for_timeout(1200)
        else:
            result["otp_entered"] = False
            break

    if result["otp_entered"]:
        final_stage = await _wait_for_stage_after_otp(
            signup_page,
            logger,
            profile_id,
            username_retry_provider=username_retry_provider,
            username_state=username_state,
            progress_callback=progress_callback,
            resubmit_callback=resubmit_callback,
            stall_state=stall_state,
        )
        if final_stage == "welcome":
            result["final_username"] = await _read_success_username(signup_page)
            await _emit_username(username_detected_callback, result["final_username"], logger, profile_id)
        elif final_stage == "phone":
            return await _handle_optional_phone_sms_verification(
                signup_page,
                phone_fetcher,
                sms_fetcher,
                result,
                logger,
                profile_id,
                username_detected_callback=username_detected_callback,
                username_retry_provider=username_retry_provider,
                username_state=username_state,
                progress_callback=progress_callback,
                resubmit_callback=resubmit_callback,
                stall_state=stall_state,
            )
        if not str(result.get("final_username") or "").strip():
            result["final_username"] = await _wait_for_final_success_username(
                signup_page,
                logger,
                profile_id,
                timeout_ms=240000,
            )
            await _emit_username(username_detected_callback, result["final_username"], logger, profile_id)

    return result


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

async def perform_snapchat_signup(
    signup_page,
    model: str,
    username: str,
    email: str,
    names_dir: Path,
    logger,
    profile_id: str,
    otp_fetcher,
    password: str = "",
    username_detected_callback=None,
    email_fetcher=None,
    username_retry_provider=None,
    progress_callback=None,
    phone_fetcher=None,
    sms_fetcher=None,
) -> dict:
    result = {
        "snap_name": "",
        "username": username,
        "email": str(email or "").strip() if _is_valid_email(email) else "",
        "reached_verification": False,
        "otp_entered": False,
        "phone_entered": False,
        "sms_otp_entered": False,
        "error": "",
    }

    # Wrap so callback is only invoked once per unique username.
    emitted_usernames: set[str] = set()

    async def _on_username(detected: str):
        normalized = str(detected or "").strip()
        if not normalized or normalized in emitted_usernames:
            return
        emitted_usernames.add(normalized)
        await _emit_username(username_detected_callback, normalized, logger, profile_id)

    try:
        await _keep_signup_page_clear(signup_page, logger, profile_id, duration_ms=1500)
        ready = await _wait_visible(signup_page, "#firstname", timeout_ms=30000)
        if not ready:
            raise RuntimeError("Signup form (#firstname) did not become visible in time.")

        if await _is_non_english_signup_page(signup_page):
            logger and logger.info(
                f"[{profile_id}] Signup page appears localized; continuing with language-neutral selectors."
            )

        snap_name = get_random_name(model, names_dir) or model
        result["snap_name"] = snap_name
        birthday = generate_birthday(model)

        fill_result = await _fill_signup_form(signup_page, snap_name, birthday, username, password, logger, profile_id)
        result["username"] = fill_result.get("username", username)
        if not fill_result.get("submitted"):
            submitted_after_refresh = False
            for attempt in range(1, SIGNUP_MAX_REFRESH_ATTEMPTS + 1):
                logger and logger.warning(
                    f"[{profile_id}] Agree and Continue did not click after filling signup; "
                    f"refreshing and re-entering signup details "
                    f"(attempt {attempt}/{SIGNUP_MAX_REFRESH_ATTEMPTS})."
                )
                await _emit_signup_progress(progress_callback, "refreshing_stuck_signup", logger, profile_id)
                active = await _resolve_active_signup_page(signup_page, logger, profile_id)
                submitted_after_refresh = await _reload_and_refill_signup(
                    active,
                    snap_name,
                    birthday,
                    str(result.get("username") or username or "").strip(),
                    password,
                    logger,
                    profile_id,
                )
                if submitted_after_refresh:
                    break
                await active.wait_for_timeout(1500)
            if not submitted_after_refresh:
                raise RuntimeError(
                    "signup_stuck_retry_exhausted: Agree and Continue was not clickable "
                    f"after {SIGNUP_MAX_REFRESH_ATTEMPTS} refresh attempts."
                )

        username_state = {
            "value": str(result.get("username") or "").strip(),
            "manual_override": False,
        }

        # Keep refresh counters/timers across every watcher pass so all recovery
        # paths share one bounded budget for the whole signup.
        stall_state = {
            "refresh_attempts": 0,
            "form_since": None,
            "blank_refill_attempts": 0,
            "page_issue_since": None,
        }

        async def resubmit_callback():
            active = await _resolve_active_signup_page(signup_page, logger, profile_id)
            return await _reload_and_refill_signup(
                active,
                snap_name,
                birthday,
                str(username_state.get("value") or username or "").strip(),
                password,
                logger,
                profile_id,
            )

        async def refill_callback():
            # Re-enter the saved credentials into an already-loaded (blank) form
            # WITHOUT a page reload — used when the signup page was refreshed out
            # from under us and the empty form reappeared.
            active = await _resolve_active_signup_page(signup_page, logger, profile_id)
            return await _fill_signup_form(
                active,
                snap_name,
                birthday,
                str(username_state.get("value") or username or "").strip(),
                password,
                logger,
                profile_id,
            )

        stall_state["refill"] = refill_callback

        verification = await _handle_verification(
            signup_page,
            email,
            otp_fetcher,
            logger,
            profile_id,
            username_detected_callback=_on_username,
            email_fetcher=email_fetcher,
            username_retry_provider=username_retry_provider,
            username_state=username_state,
            progress_callback=progress_callback,
            resubmit_callback=resubmit_callback,
            stall_state=stall_state,
            phone_fetcher=phone_fetcher,
            sms_fetcher=sms_fetcher,
        )
        result.update(verification)
        result["username"] = str(username_state.get("value") or result.get("username") or "").strip()
        email = str(result.get("email") or email or "").strip()

        retry_count = 0
        while (
            result.get("reached_verification")
            and not result.get("otp_entered")
            and not str(result.get("final_username") or "").strip()
            and retry_count < 2
        ):
            retry_count += 1
            logger and logger.info(
                f"[{profile_id}] Verification still pending after pass {retry_count}; re-entering verification watcher."
            )
            await signup_page.wait_for_timeout(3000)
            verification = await _handle_verification(
                signup_page,
                email,
                otp_fetcher,
                logger,
                profile_id,
                username_detected_callback=_on_username,
                email_fetcher=email_fetcher,
                username_retry_provider=username_retry_provider,
                username_state=username_state,
                progress_callback=progress_callback,
                resubmit_callback=resubmit_callback,
                stall_state=stall_state,
                phone_fetcher=phone_fetcher,
                sms_fetcher=sms_fetcher,
            )
            result.update(verification)
            result["username"] = str(username_state.get("value") or result.get("username") or "").strip()
            email = str(result.get("email") or email or "").strip()

        # Fallback: if OTP delivery failed but the operator may rescue it
        # manually, keep watching for the welcome screen so the rename can
        # still fire as soon as the username is observed.
        if not str(result.get("final_username") or "").strip() and result.get("reached_verification"):
            logger and logger.info(
                f"[{profile_id}] Watching for manual OTP recovery before giving up."
            )
            late_username = await _wait_for_final_success_username(
                signup_page,
                logger,
                profile_id,
                timeout_ms=600000,
            )
            if late_username:
                result["final_username"] = late_username
                await _on_username(late_username)

    except Exception as exc:
        result["error"] = str(exc)
        logger and logger.error(f"[{profile_id}] Signup error: {exc}")

    return result
