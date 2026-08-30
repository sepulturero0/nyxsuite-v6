import asyncio
import json
import os
import random
import re
from datetime import datetime, timezone

from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from core.process_utils import LOGS_DIR
from core.outfit_generator import generate_outfit, BLOCKED_TOP_IDS, BLOCKED_FOOTWEAR_IDS
from snap_selectors.selectors import BITMOJI_SELECTORS, MODEL_ALIASES

KNOWN_SKIN_TONE_FILLS = {
    "#f6b892",
    "#fab787",
    "#f1ac88",
}


def _env_int(name, default):
    try:
        return max(1, int(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


# Per-click retry budget inside safe_click (a transient slow-rendering panel
# used to burn all 3 attempts and fail the whole profile at a random trait).
_STEP_CLICK_RETRIES = _env_int("BITMOJI_STEP_CLICK_RETRIES", 4)
# How many times a whole face/outfit *unit* (open category/subcategory + click
# the exact trait) is replayed when the trait click can't land — the category
# panel gets re-opened between attempts so the correct item finally renders.
# This never selects a different item; it only re-tries the intended one.
_STEP_UNIT_RETRIES = _env_int("BITMOJI_STEP_UNIT_RETRIES", 3)
# Seconds to wait for a category/subcategory panel to actually render clickable
# trait items before giving up on an attempt.
_PANEL_ITEMS_TIMEOUT = float(os.getenv("BITMOJI_PANEL_ITEMS_TIMEOUT", "8"))
# Bitmoji constantly rotates its clothing catalog, so a configured outfit item id
# (e.g. footwear=969) can vanish — the exact-match scan then never finds it, the
# panel scrolls to the bottom repeatedly, and the whole profile fails ("scroll
# forever"). When enabled (default), a piece whose exact id is gone falls back to
# ANOTHER item from the *same configured pool* (picked randomly per profile) so the
# avatar only ever wears an operator-approved item. Set NYX_OUTFIT_FALLBACK_ANY=0
# to restore strict exact-item behaviour (a retired id then fails the profile).
_OUTFIT_ALLOW_FALLBACK = os.getenv("NYX_OUTFIT_FALLBACK_ANY", "1").strip().lower() not in ("0", "false", "no", "")
# Last-resort safety net: if even the whole configured pool has rotated out of the
# catalog, click any available item of the category so the profile still completes.
# Off by default — operators asked for fallbacks to stay inside the curated pool, so
# a fully-retired pool now fails the step instead of dressing the avatar at random.
# Set NYX_OUTFIT_FALLBACK_CATALOG=1 to re-enable the any-available-item net.
_OUTFIT_ALLOW_CATALOG_FALLBACK = os.getenv("NYX_OUTFIT_FALLBACK_CATALOG", "0").strip().lower() in ("1", "true", "yes")
# Limit the bounded panel walk for one outfit selector. The final scan below
# still runs after these passes so an item at the current scroll position is
# checked once more.
_OUTFIT_PANEL_SCAN_STEPS = 3


# Every garment path resolves one active editor panel before inspecting tiles.
# A non-scrollable panel is valid, but an ambiguous collection of generic
# containers is unsafe: stale panels can contain the same tile ids.
_OUTFIT_ACTIVE_PANEL_RESOLVER = r"""
    const activePanel = (() => {
        const currentSelectors = [
            '#current-category.traits-container.scrollable',
            '#current-category.fashion-traits-container.scrollable',
            '#current-category'
        ];
        const genericSelectors = [
            '[class*="traits-container"]',
            '[class*="fashion-traits"]',
            '[class*="avatar-builder-category-container"]',
            '[class*="scrollable"]'
        ];
        const itemSelector = [
            '.mix-and-match-container',
            '[class*="mix-and-match-container"]',
            '.facial-feature-wrapper[tabindex="0"]',
            '.colour-picker-option'
        ].join(', ');
        const isVisible = (el) => {
            if (!el) return false;
            const rect = el.getBoundingClientRect();
            return rect.width > 0 && rect.height > 0 && rect.bottom > 0 && rect.right > 0;
        };
        const visiblePanels = (selectors, requireItems) => {
            const panels = [];
            const seen = new Set();
            for (const selector of selectors) {
                for (const panel of document.querySelectorAll(selector)) {
                    if (!panel || seen.has(panel) || !isVisible(panel)) continue;
                    seen.add(panel);
                    if (requireItems && !Array.from(panel.querySelectorAll(itemSelector)).some(isVisible)) continue;
                    panels.push(panel);
                }
            }
            return panels;
        };

        const activePanels = visiblePanels(['[data-nyx-active]'], false);
        if (activePanels.length === 1) return activePanels[0];
        if (activePanels.length > 1) return null;

        const currentPanels = visiblePanels(currentSelectors, false);
        if (currentPanels.length === 1) return currentPanels[0];
        if (currentPanels.length > 1) return null;

        const genericPanels = visiblePanels(genericSelectors, true);
        // Generic selectors can name nested wrappers around one actual panel.
        // Keep only innermost candidates; sibling candidates remain ambiguous.
        const innermost = genericPanels.filter((panel) => !genericPanels.some(
            (other) => other !== panel && typeof panel.contains === 'function' && panel.contains(other)
        ));
        return innermost.length === 1 ? innermost[0] : null;
    })();
"""

_OUTFIT_CATEGORY_ITEMS_READY = """() => {""" + _OUTFIT_ACTIVE_PANEL_RESOLVER + r"""
    if (!activePanel) return false;
    const isVisible = (el) => {
        if (!el) return false;
        const rect = el.getBoundingClientRect();
        return rect.width > 0 && rect.height > 0 && rect.bottom > 0 && rect.right > 0;
    };
    const selector = [
        '.mix-and-match-container',
        '[class*="mix-and-match-container"]',
        '.facial-feature-wrapper[tabindex="0"]',
        '.colour-picker-option'
    ].join(', ');
    return Array.from(activePanel.querySelectorAll(selector)).some(isVisible);
}"""

_OUTFIT_ACTIVE_PANEL_COLOR_PICKER_READY = """() => {""" + _OUTFIT_ACTIVE_PANEL_RESOLVER + r"""
    if (!activePanel) return false;
    const isVisible = (el) => {
        if (!el) return false;
        const rect = el.getBoundingClientRect();
        return rect.width > 0 && rect.height > 0 && rect.bottom > 0 && rect.right > 0;
    };
    const pickers = Array.from(activePanel.querySelectorAll(
        'div.colour-picker-container, div.colour-picker'
    )).filter(isVisible);
    return pickers.some((picker) => Array.from(
        picker.querySelectorAll('.colour-picker-option')
    ).some(isVisible));
}"""


class BitmojiOutfitMixin:
    async def get_visible_locators(self, locator, max_candidates=60):
        visible = []

        try:
            count = await locator.count()
        except Exception:
            return visible

        for index in range(min(count, max_candidates)):
            candidate = locator.nth(index)
            try:
                if await candidate.is_visible():
                    visible.append(candidate)
            except Exception:
                continue

        return visible

    async def wait_for_category_items(self, ctx=None, timeout=None):
        """Wait until the currently-open category/subcategory panel has rendered
        at least one clickable trait/outfit item.

        Random "stops" at paired earrings / outfit pieces were almost always a
        click firing into a panel that had switched category but not yet painted
        its items — the exact-match scan then found nothing and the step failed.
        Gating the click on real items being present removes that race without
        ever changing *which* item is chosen."""
        if timeout is None:
            timeout = _PANEL_ITEMS_TIMEOUT
        deadline = asyncio.get_event_loop().time() + float(timeout)
        while asyncio.get_event_loop().time() < deadline:
            await self.wait_if_paused()
            try:
                if ctx is None:
                    ctx = await self.get_editor_context()
                if ctx is not None:
                    if await ctx.evaluate(_OUTFIT_CATEGORY_ITEMS_READY):
                        return True
            except Exception:
                pass
            await asyncio.sleep(0.2)
        return False

    async def enable_tuck_if_available(self):
        ctx = await self.get_editor_context()
        if ctx is None:
            return False

        # Wait for the just-clicked top to fully apply. The tuck control re-
        # renders for the new top, including its disabled flag and the
        # "Tucked!"/"Untucked!" caption. Reading too early can hit a stale
        # state from the previously selected top.
        await self.human_delay(0.7, 1.1, kind="think")

        tuck_state = await self._read_tuck_state(ctx)
        if tuck_state in (None, "missing", "disabled", "checked"):
            return tuck_state == "checked"

        tuck_switch = ctx.locator("label[for='tuck-toggle'].switch, .tuck-container .switch").first
        tuck_checkbox = ctx.locator("input#tuck-toggle[type='checkbox'], .tuck-container input[type='checkbox']").first

        try:
            if await tuck_switch.is_visible():
                await tuck_switch.scroll_into_view_if_needed(timeout=4000)
                try:
                    await tuck_switch.click()
                except Exception:
                    await tuck_switch.click(force=True)
            else:
                await tuck_checkbox.scroll_into_view_if_needed(timeout=4000)
                await tuck_checkbox.check(force=True)
        except Exception:
            try:
                await ctx.evaluate(
                    """() => {
                        const input = document.querySelector("input#tuck-toggle[type='checkbox'], .tuck-container input[type='checkbox']");
                        if (!input) return false;
                        if (!input.checked) input.click();
                        return !!input.checked;
                    }"""
                )
            except Exception:
                return False

        await self.human_delay(0.3, 0.6, kind="think")

        # The caption flips to "Tucked!" once the store commits the toggle;
        # poll briefly so a slow re-render isn't mistaken for a failure.
        for _ in range(5):
            tuck_state = await self._read_tuck_state(ctx)
            if tuck_state == "checked":
                return True
            if tuck_state in (None, "missing", "disabled"):
                return False
            await asyncio.sleep(0.3)
        return False

    async def _read_tuck_state(self, ctx):
        try:
            return await ctx.evaluate(
                """() => {
                    const container = document.querySelector(".tuck-container");
                    if (!container) return "missing";
                    const containerStyle = window.getComputedStyle(container);
                    if (containerStyle.display === "none" || containerStyle.visibility === "hidden") return "missing";
                    if (containerStyle.pointerEvents === "none") return "disabled";
                    if (parseFloat(containerStyle.opacity || "1") < 0.5) return "disabled";
                    const input = container.querySelector("input#tuck-toggle[type='checkbox'], .tuck-container input[type='checkbox']");
                    if (input && (input.disabled || input.getAttribute("aria-disabled") === "true")) return "disabled";
                    const label = container.querySelector("label[for='tuck-toggle'].switch, .tuck-container .switch");
                    if (label) {
                        const labelStyle = window.getComputedStyle(label);
                        if (labelStyle.pointerEvents === "none") return "disabled";
                        if (parseFloat(labelStyle.opacity || "1") < 0.5) return "disabled";
                        if (label.classList.contains("disabled") || label.classList.contains("is-disabled")) return "disabled";
                    }
                    const caption = container.querySelector(".tuck-text, .tuck-container .tuck-text");
                    if (caption) {
                        const text = (caption.textContent || "").trim();
                        if (/untucked/i.test(text)) return "ready";
                        if (/tucked/i.test(text)) return "checked";
                    }
                    if (input) return input.checked ? "checked" : "ready";
                    return "ready";
                }"""
            )
        except Exception:
            return None

    def get_random_hair_selector(self, model, profile_id):
        model_options = BITMOJI_SELECTORS.get("random_hair", {}).get(model, [])
        if not model_options:
            return None

        return random.SystemRandom().choice(model_options)

    async def scroll_target_into_panel(self, target):
        await target.evaluate(
            """(node) => {
                const isScrollable = (el) => {
                    const style = window.getComputedStyle(el);
                    const overflowY = style.overflowY;
                    return (
                        (overflowY === 'auto' || overflowY === 'scroll') &&
                        el.scrollHeight > el.clientHeight
                    );
                };

                let parent = node.parentElement;
                while (parent) {
                    if (isScrollable(parent)) {
                        const parentRect = parent.getBoundingClientRect();
                        const nodeRect = node.getBoundingClientRect();
                        const offset = nodeRect.top - parentRect.top - (parent.clientHeight / 2) + (nodeRect.height / 2);
                        parent.scrollTop += offset;
                        break;
                    }
                    parent = parent.parentElement;
                }

                node.scrollIntoView({ block: 'center', inline: 'nearest' });
            }"""
        )

    async def scroll_editor_panel(self, ctx, direction="down", amount=None):
        return await ctx.evaluate(
            """({ direction, amount }) => {""" + _OUTFIT_ACTIVE_PANEL_RESOLVER + r"""
                if (!activePanel) {
                    return { moved: false, found: false };
                }
                const panel = activePanel;
                const adaptiveStep = Math.max(180, Math.min(420, Math.floor(panel.clientHeight * 0.58)));
                const deltaBase = (typeof amount === 'number' && amount > 0) ? amount : adaptiveStep;
                const delta = direction === 'down' ? deltaBase : -deltaBase;
                const before = panel.scrollTop;
                panel.scrollTop += delta;
                return {
                    found: true,
                    moved: Math.abs(panel.scrollTop - before) > 4,
                    atTop: panel.scrollTop <= 4,
                    atBottom: (panel.scrollTop + panel.clientHeight) >= (panel.scrollHeight - 4),
                };
            }""",
            {"direction": direction, "amount": amount}
        )

    async def reveal_selector_in_panel(self, ctx, selector):
        locator = ctx.locator(selector)

        try:
            if await locator.count() == 0:
                return None
        except Exception:
            return None

        try:
            await self.scroll_target_into_panel(locator.first)
            await asyncio.sleep(0.2)
        except Exception:
            pass

        return await self.first_actionable_locator(locator)

    async def reset_editor_panel_scroll(self, ctx):
        await ctx.evaluate(
            """() => {""" + _OUTFIT_ACTIVE_PANEL_RESOLVER + r"""
                if (!activePanel) return false;
                activePanel.scrollTop = 0;
                return true;
            }"""
        )

    def should_scroll_panel_for_selector(self, selector_key, selector):
        if selector_key.startswith("traits.") or selector_key.startswith("items."):
            return True

        panel_scan_markers = [
            "mix-and-match-container",
            "/avatar/top?",
            "/avatar/bottom?",
            "/avatar/one_piece?",
            "/avatar/footwear?",
            "/avatar/outerwear?",
            "head-trait-container",
            "/avatar/hair?",
            "hair=",
            "hair_tone=",
            "hair_treatment",
        ]

        return any(marker in selector for marker in panel_scan_markers)

    def is_outfit_selector(self, selector):
        return any(
            marker in selector
            for marker in [
                "/avatar/top?", "/avatar/bottom?", "/avatar/one_piece?",
                "/avatar/footwear?", "/avatar/outerwear?",
            ]
        )

    def extract_outfit_src_markers(self, selector):
        markers = []

        if isinstance(selector, dict):
            selector = selector.get("selector", "")

        contains_matches = re.findall(r"contains\(@src,'([^']+)'\)", str(selector))
        for match in contains_matches:
            if match not in markers:
                markers.append(match)

        for token in [
            "/avatar/top?", "/avatar/bottom?", "/avatar/one_piece?", "/avatar/footwear?", "/avatar/outerwear?",
            "top=", "bottom=", "footwear=", "outerwear=",
        ]:
            if token in selector:
                start = selector.index(token)
                end = selector.find("'", start)
                if end == -1:
                    end = len(selector)
                value = selector[start:end]
                if value not in markers:
                    markers.append(value)

        return markers

    @staticmethod
    def _xpath_string_expression_value(expression):
        """Decode the string-only XPath expression emitted by ``build_selector``.

        This deliberately supports only quoted literals and nested ``concat``
        calls; it never evaluates XPath or JavaScript from a selector.
        """
        value = str(expression or "").strip()
        if len(value) >= 2 and value[0] in "'\"" and value[-1] == value[0]:
            return value[1:-1]
        if not value.startswith("concat(") or not value.endswith(")"):
            return None

        parts = []
        start = 0
        quote = None
        depth = 0
        body = value[len("concat("):-1]
        for index, char in enumerate(body):
            if quote:
                if char == quote:
                    quote = None
                continue
            if char in "'\"":
                quote = char
            elif char == "(":
                depth += 1
            elif char == ")":
                if depth == 0:
                    return None
                depth -= 1
            elif char == "," and depth == 0:
                parts.append(body[start:index])
                start = index + 1
        if quote or depth:
            return None
        parts.append(body[start:])

        values = [BitmojiOutfitMixin._xpath_string_expression_value(part) for part in parts]
        return "".join(values) if values and all(part is not None for part in values) else None

    @staticmethod
    def _xpath_expression_until_comma(selector_text, start):
        """Return one XPath expression and the following top-level comma."""
        quote = None
        depth = 0
        for index in range(start, len(selector_text)):
            char = selector_text[index]
            if quote:
                if char == quote:
                    quote = None
                continue
            if char in "'\"":
                quote = char
            elif char == "(":
                depth += 1
            elif char == ")":
                if depth == 0:
                    return None, None
                depth -= 1
            elif char == "," and depth == 0:
                return selector_text[start:index], index
        return None, None

    def extract_outfit_requirements(self, selector):
        if isinstance(selector, dict):
            selector = selector.get("selector", "")

        selector_text = str(selector)
        required_fragments = []
        required_params = {}

        contains_matches = re.findall(r"contains\(@src,'([^']+)'\)", selector_text)
        for match in contains_matches:
            if "=" in match:
                key, value = match.split("=", 1)
                required_params[key] = value
                continue
            if match not in required_fragments:
                required_fragments.append(match)

        # Live garment selectors match an exact query parameter with an XPath
        # ``contains(concat(...), '&param=value&')`` clause. Preserve that exact
        # identity for the in-panel DOM matcher instead of falling back to a
        # same-category sibling merely because it is visible.
        literal_exact_params = re.findall(
            r"contains\(\s*concat\(\s*['\"]&['\"]\s*,\s*"
            r"substring-after\(\s*@src\s*,\s*['\"]\?['\"]\s*\)\s*,\s*['\"]&['\"]\s*\)\s*,\s*"
            r"['\"]&([A-Za-z][A-Za-z0-9_]*)=([^&'\"]+)&['\"]\s*\)",
            selector_text,
        )
        for key, value in literal_exact_params:
            required_params[key] = value

        # ``build_selector`` safely emits an id as a separate XPath expression,
        # e.g. ``concat('&top=', '1062', '&')``. Decode just that string-only
        # expression so quoted ids still keep their exact parameter identity.
        prefix_pattern = re.compile(
            r"concat\(\s*(['\"])&(?P<param>[A-Za-z][A-Za-z0-9_]*)=\1\s*,\s*",
        )
        for prefix in prefix_pattern.finditer(selector_text):
            expression, comma_index = self._xpath_expression_until_comma(selector_text, prefix.end())
            if expression is None or comma_index is None:
                continue
            if not re.match(r"\s*,\s*(['\"])&\1\s*\)", selector_text[comma_index:]):
                continue
            option_id = self._xpath_string_expression_value(expression)
            if option_id is not None:
                required_params[prefix.group("param")] = option_id

        return {
            "fragments": required_fragments,
            "params": required_params,
        }

    def outfit_option_id_from_selector(self, selector, feature):
        """Return the actual garment option id encoded by a selected selector.

        A requested selector may have fallen back to a different configured pool
        member, so callers must derive the id from the selector that actually
        clicked.  Unknown catalog-wide fallbacks intentionally yield ``None``.
        """
        requirements = self.extract_outfit_requirements(selector)
        params = requirements.get("params", {})
        feature_params = {
            "dresses": ("bottom", "top"),
            "tops": ("top",),
            "outfits": ("top",),
            "bottoms": ("bottom",),
            "footwear": ("footwear",),
            "outerwear": ("outerwear",),
        }
        for param in feature_params.get(str(feature), ()):
            option_id = str(params.get(param) or "").strip()
            if option_id:
                return option_id
        return None

    def normalize_outfit_entry(self, entry):
        if isinstance(entry, dict):
            return entry
        return {"selector": entry}

    def outfit_preferred_color_for_selection(self, requested_entry, selected_entry):
        """Return colour metadata that belongs to the garment actually clicked."""
        requested = self.normalize_outfit_entry(requested_entry)
        selected = self.normalize_outfit_entry(selected_entry)
        if selected.get("known") is False:
            return None

        requested_selector = str(requested.get("selector") or "")
        selected_selector = str(selected.get("selector") or "")
        if selected_selector and selected_selector != requested_selector:
            return selected.get("preferred_color")
        if "preferred_color" in selected:
            return selected.get("preferred_color")
        return requested.get("preferred_color")

    def extract_src_fragments(self, selector):
        if isinstance(selector, dict):
            selector = selector.get("selector", "")

        return re.findall(r"contains\(@src,'([^']+)'\)", str(selector))

    def extract_fill_values(self, selector):
        if isinstance(selector, dict):
            selector = selector.get("selector", "")

        return re.findall(r"@fill='([^']+)'", str(selector), flags=re.IGNORECASE)

    def extract_path_fragments(self, selector):
        if isinstance(selector, dict):
            selector = selector.get("selector", "")

        return re.findall(r"contains\(@d,'([^']+)'\)", str(selector))

    def estimate_skin_tone_fraction(self, fill_value):
        normalized_fill = str(fill_value or "").strip().lower()
        return {
            "#f6b892": 0.25,
            "#fab787": 0.5,
            "#f1ac88": 0.75,
        }.get(normalized_fill, 0.5)

    def is_skin_tone_selector_key(self, selector_key):
        return selector_key.startswith("traits.") and "skin_tone" in selector_key

    async def click_exact_skin_tone_from_dom(self, ctx, target_fill):
        try:
            clicked = await ctx.evaluate(
                """(targetFill) => {
                    const normalize = (value) => String(value || "").trim().toLowerCase();
                    const selectors = [
                        ".swatch-trait-preview .container[tabindex='0']",
                        "#current-category .container[tabindex='0']",
                        ".avatar-builder-category-container .container[tabindex='0']",
                        ".trait-preview .container[tabindex='0']",
                    ];
                    const isVisible = (el) => {
                        if (!el) return false;
                        const rect = el.getBoundingClientRect();
                        return rect.width > 0 && rect.height > 0 && rect.bottom > 0 && rect.right > 0;
                    };

                    const candidates = [];
                    const seen = new Set();
                    for (const selector of selectors) {
                        for (const el of document.querySelectorAll(selector)) {
                            if (!seen.has(el) && isVisible(el)) {
                                seen.add(el);
                                candidates.push(el);
                            }
                        }
                    }

                    const exact = candidates.find((el) =>
                        Array.from(el.querySelectorAll("rect, circle, path, ellipse, stop"))
                            .some((node) => normalize(node.getAttribute("fill")) === targetFill)
                    );
                    if (!exact) return false;

                    exact.scrollIntoView({ block: "center", inline: "nearest" });
                    exact.dispatchEvent(new MouseEvent("pointerdown", { bubbles: true, cancelable: true, view: window }));
                    exact.dispatchEvent(new MouseEvent("mousedown", { bubbles: true, cancelable: true, view: window }));
                    exact.dispatchEvent(new MouseEvent("pointerup", { bubbles: true, cancelable: true, view: window }));
                    exact.dispatchEvent(new MouseEvent("mouseup", { bubbles: true, cancelable: true, view: window }));
                    exact.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, view: window }));
                    return true;
                }""",
                target_fill,
            )
            return bool(clicked)
        except Exception:
            return False

    async def reopen_skin_tone_category(self, ctx):
        selector = self.resolve_selector("categories.skin_tone")
        try:
            target = await self.first_actionable_locator(ctx.locator(selector))
            if target is not None:
                await self.scroll_target_into_panel(target)
                await self.human_delay(0.05, 0.12, kind="think", respect_speed=False, respect_jitter=False)
                try:
                    await target.click(timeout=2500)
                except Exception:
                    await target.click(force=True, timeout=2500)
                await self.human_delay(0.05, 0.12, kind="think", respect_speed=False, respect_jitter=False)
                return True
        except Exception:
            pass

        try:
            if await self.click_by_dom_signature(ctx, "categories.skin_tone", selector):
                await self.human_delay(0.05, 0.12, kind="think", respect_speed=False, respect_jitter=False)
                return True
        except Exception:
            pass

        return False

    async def click_skin_tone_trait(self, ctx, selector):
        fill_values = self.extract_fill_values(selector)
        if not fill_values:
            return False

        target_fill = str(fill_values[0] or "").strip().lower()
        if target_fill not in KNOWN_SKIN_TONE_FILLS:
            if self.logger:
                self.logger.warning(f"Unknown configured skin tone fill: {target_fill}")
            return False

        if await self.click_exact_skin_tone_from_dom(ctx, target_fill):
            return True

        await self.reopen_skin_tone_category(ctx)
        if await self.click_exact_skin_tone_from_dom(ctx, target_fill):
            return True

        items_selector = BITMOJI_SELECTORS.get("items", {}).get("skin_tone", "")
        fallback_selector = BITMOJI_SELECTORS.get("items", {}).get("fallback", "")

        visible_items = []
        if items_selector:
            visible_items = await self.get_visible_locators(ctx.locator(items_selector))
        if not visible_items and fallback_selector:
            visible_items = await self.get_visible_locators(ctx.locator(fallback_selector))
        if not visible_items:
            return False

        exact_matches = []

        for index, option in enumerate(visible_items[:16]):
            try:
                matched = await option.evaluate(
                    """(el, targetFill) => {
                        const normalizeColor = (value) => String(value || "").trim().toLowerCase();
                        const hexToRgb = (value) => {
                            const hex = normalizeColor(value).replace(/^#/, "");
                            if (/^[0-9a-f]{3}$/i.test(hex)) {
                                return {
                                    r: Number.parseInt(hex[0] + hex[0], 16),
                                    g: Number.parseInt(hex[1] + hex[1], 16),
                                    b: Number.parseInt(hex[2] + hex[2], 16),
                                };
                            }
                            if (!/^[0-9a-f]{6}$/i.test(hex)) {
                                return null;
                            }
                            return {
                                r: Number.parseInt(hex.slice(0, 2), 16),
                                g: Number.parseInt(hex.slice(2, 4), 16),
                                b: Number.parseInt(hex.slice(4, 6), 16),
                            };
                        };
                        const cssToRgb = (value) => {
                            const normalized = normalizeColor(value);
                            if (!normalized || normalized === "transparent" || normalized === "none") {
                                return null;
                            }
                            const hexColor = hexToRgb(normalized);
                            if (hexColor) {
                                return hexColor;
                            }
                            const match = normalized.match(/^rgba?\\(([^)]+)\\)$/);
                            if (!match) {
                                return null;
                            }
                            const parts = match[1].split(",").map((part) => Number.parseFloat(part.trim()));
                            if (parts.length < 3 || parts.slice(0, 3).some((part) => Number.isNaN(part))) {
                                return null;
                            }
                            return { r: parts[0], g: parts[1], b: parts[2] };
                        };
                        const rgbToHex = (value) => {
                            const asHex = (part) => Math.max(0, Math.min(255, Math.round(part)))
                                .toString(16)
                                .padStart(2, "0");
                            return `#${asHex(value.r)}${asHex(value.g)}${asHex(value.b)}`;
                        };

                        const nodes = [el, ...Array.from(el.querySelectorAll("*")).slice(0, 24)];

                        for (const node of nodes) {
                            const computed = window.getComputedStyle(node);
                            const rawValues = [
                                node.getAttribute?.("fill"),
                                node.getAttribute?.("stroke"),
                                node.style?.backgroundColor,
                                computed.backgroundColor,
                                node.style?.color,
                                computed.color,
                            ];

                            for (const rawValue of rawValues) {
                                const normalized = normalizeColor(rawValue);
                                if (normalized === targetFill) {
                                    return true;
                                }
                                const parsed = cssToRgb(rawValue);
                                if (parsed && rgbToHex(parsed) === targetFill) {
                                    return true;
                                }
                            }
                        }

                        return false;
                    }""",
                    target_fill,
                )
            except Exception:
                matched = False

            if matched:
                exact_matches.append(index)

        if not exact_matches:
            if self.logger:
                self.logger.warning(
                    f"Skin tone exact match not found for fill {target_fill}. visible_items={len(visible_items)}"
                )
            await self.reopen_skin_tone_category(ctx)
            if await self.click_exact_skin_tone_from_dom(ctx, target_fill):
                return True
            return False

        target = visible_items[exact_matches[0]]
        await self.scroll_target_into_panel(target)
        await self.human_delay()
        await target.click()
        return True

    async def click_by_dom_signature(self, ctx, selector_key, selector, steps=10):
        src_fragments = self.extract_src_fragments(selector)
        fill_values = self.extract_fill_values(selector)
        path_fragments = self.extract_path_fragments(selector)

        if not src_fragments and not fill_values and not path_fragments:
            return False

        selector_group = "trait"
        if selector_key.startswith("categories."):
            selector_group = "category"
        elif selector_key.startswith("subcategories."):
            selector_group = "subcategory"

        if self.should_scroll_panel_for_selector(selector_key, selector):
            try:
                await self.reset_editor_panel_scroll(ctx)
            except Exception:
                pass

        for _ in range(max(1, steps)):
            try:
                clicked = await ctx.evaluate(
                    """({ selectorGroup, srcFragments, fillValues, pathFragments }) => {
                        const isVisible = (el) => {
                            if (!el) return false;
                            const rect = el.getBoundingClientRect();
                            return rect.width > 0 && rect.height > 0 && rect.bottom > 0 && rect.right > 0;
                        };

                        const triggerClick = (el) => {
                            if (!el || !isVisible(el)) return false;
                            el.scrollIntoView({ block: "center", inline: "center" });
                            el.focus?.();
                            el.dispatchEvent(new MouseEvent("pointerdown", { bubbles: true, cancelable: true, view: window }));
                            el.dispatchEvent(new MouseEvent("mousedown", { bubbles: true, cancelable: true, view: window }));
                            el.dispatchEvent(new MouseEvent("pointerup", { bubbles: true, cancelable: true, view: window }));
                            el.dispatchEvent(new MouseEvent("mouseup", { bubbles: true, cancelable: true, view: window }));
                            el.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, view: window }));
                            return true;
                        };

                        const candidateSelectors = selectorGroup === "category"
                            ? [
                                ".category-item",
                                "[class*='category-item']",
                                ".top-category-container [tabindex='0']",
                                ".top-category-container > div",
                            ]
                            : selectorGroup === "subcategory"
                                ? [
                                    ".subcategory",
                                    "[class*='subcategory']",
                                    "[class*='sub-category']",
                                    "[data-testid*='subcategory']",
                                ]
                                : [
                                    ".head-trait-container[tabindex='0']",
                                    ".facial-feature-wrapper[tabindex='0']",
                                    ".container[tabindex='0']",
                                    ".mix-and-match-container",
                                    "[class*='mix-and-match-container']",
                                    ".trait-preview [tabindex='0']",
                                    "#current-category [tabindex='0']",
                                    "[class*='traits-container'] [tabindex='0']",
                                    "div[tabindex='0']",
                                ];

                        const visibleCandidates = [];
                        const seen = new Set();
                        for (const selector of candidateSelectors) {
                            for (const el of document.querySelectorAll(selector)) {
                                if (!el || seen.has(el) || !isVisible(el)) continue;
                                seen.add(el);
                                visibleCandidates.push(el);
                            }
                        }

                        const normalizedFills = fillValues.map((fill) => String(fill || "").trim().toLowerCase());
                        const normalizedPaths = pathFragments.map((path) => String(path || "").trim());
                        const hexToRgb = (value) => {
                            const hex = String(value || "").trim().replace(/^#/, "");
                            if (!/^[0-9a-f]{6}$/i.test(hex)) return null;
                            return {
                                r: Number.parseInt(hex.slice(0, 2), 16),
                                g: Number.parseInt(hex.slice(2, 4), 16),
                                b: Number.parseInt(hex.slice(4, 6), 16),
                            };
                        };
                        const colorDistance = (left, right) => {
                            if (!left || !right) return Number.POSITIVE_INFINITY;
                            return Math.abs(left.r - right.r) + Math.abs(left.g - right.g) + Math.abs(left.b - right.b);
                        };

                        const getCandidateScore = (el) => {
                            if (srcFragments.length) {
                                const imgs = Array.from(el.querySelectorAll("img"));
                                const allImgSrc = imgs.map((img) => String(img.src || ""));
                                const hasSrcMatch = srcFragments.every((fragment) =>
                                    allImgSrc.some((src) => src.includes(fragment))
                                );
                                if (!hasSrcMatch) return null;
                            }

                            let fillScore = 0;
                            if (normalizedFills.length) {
                                const coloredNodes = Array.from(el.querySelectorAll("rect, circle, path, ellipse, stop"));
                                const candidateFills = coloredNodes
                                    .map((node) => String(node.getAttribute("fill") || "").trim().toLowerCase())
                                    .filter(Boolean);
                                if (!candidateFills.length) return null;

                                const exactMatch = normalizedFills.every((fill) => candidateFills.includes(fill));
                                if (exactMatch) {
                                    fillScore = 0;
                                } else {
                                    const candidateColors = candidateFills.map(hexToRgb).filter(Boolean);
                                    if (!candidateColors.length) return null;

                                    fillScore = normalizedFills.reduce((total, fill) => {
                                        const targetColor = hexToRgb(fill);
                                        if (!targetColor) return total + 1000;
                                        const bestDistance = candidateColors.reduce(
                                            (best, candidateColor) => Math.min(best, colorDistance(targetColor, candidateColor)),
                                            Number.POSITIVE_INFINITY
                                        );
                                        return total + (Number.isFinite(bestDistance) ? bestDistance : 1000);
                                    }, 0);
                                }
                            }

                            if (normalizedPaths.length) {
                                const pathNodes = Array.from(el.querySelectorAll("path"));
                                const hasPathMatch = normalizedPaths.every((fragment) =>
                                    pathNodes.some((node) => String(node.getAttribute("d") || "").includes(fragment))
                                );
                                if (!hasPathMatch) return null;
                            }

                            return {
                                fillScore,
                                areaScore: -(el.getBoundingClientRect().width * el.getBoundingClientRect().height),
                            };
                        };

                        const ranked = visibleCandidates
                            .map((el) => ({ el, score: getCandidateScore(el) }))
                            .filter((entry) => entry.score !== null)
                            .sort((left, right) =>
                                left.score.fillScore - right.score.fillScore ||
                                left.score.areaScore - right.score.areaScore
                            );

                        return triggerClick(ranked[0]?.el || null);
                    }""",
                    {
                        "selectorGroup": selector_group,
                        "srcFragments": src_fragments,
                        "fillValues": fill_values,
                        "pathFragments": path_fragments,
                    },
                )
                if clicked:
                    return True
            except Exception:
                pass

            if not self.should_scroll_panel_for_selector(selector_key, selector):
                break

            panel_state = await self.scroll_editor_panel(ctx, "down")
            if not panel_state or not panel_state.get("found") or not panel_state.get("moved"):
                break
            await asyncio.sleep(0.2)

        return False

    async def find_target_with_panel_scan(self, ctx, selector, steps=8, step_size=None):
        await self.reset_editor_panel_scroll(ctx)
        target = await self.first_actionable_locator(ctx.locator(selector))
        if target is not None:
            return target

        target = await self.reveal_selector_in_panel(ctx, selector)
        if target is not None:
            return target

        for _ in range(max(steps, 14)):
            panel_state = await self.scroll_editor_panel(ctx, "down", step_size)
            if not panel_state or not panel_state.get("found"):
                break
            await asyncio.sleep(0.35)
            target = await self.first_actionable_locator(ctx.locator(selector))
            if target is not None:
                return target

            target = await self.reveal_selector_in_panel(ctx, selector)
            if target is not None:
                return target

            if panel_state.get("atBottom") or not panel_state.get("moved"):
                break

        return None

    def describe_outfit_entry(self, entry):
        selector = self.normalize_outfit_entry(entry).get("selector", "")
        markers = self.extract_outfit_src_markers(selector)
        return ", ".join(markers[:3]) if markers else str(selector)[:120]

    def compact_selector_name(self, selector_key):
        normalized = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(selector_key or "outfit")).strip("_")
        return (normalized or "outfit")[:80]

    async def write_outfit_failure_snapshot(self, ctx, selector, requirements, scroll_history, profile_id="", selector_key=""):
        try:
            LOGS_DIR.mkdir(parents=True, exist_ok=True)
            failure_dir = LOGS_DIR / "outfit_failures"
            failure_dir.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            safe_profile = self.compact_selector_name(profile_id or "unknown_profile")
            safe_key = self.compact_selector_name(selector_key or "outfit_selector")
            snapshot_path = failure_dir / f"{timestamp}_{safe_profile}_{safe_key}.json"

            dom_snapshot = {}
            try:
                dom_snapshot = await ctx.evaluate(
                    """() => {
                        const selectors = [
                            '#current-category.traits-container.scrollable',
                            '#current-category.fashion-traits-container.scrollable',
                            '#current-category',
                            '[class*="traits-container"]',
                            '[class*="fashion-traits"]',
                            '[class*="avatar-builder-category-container"]',
                            '[class*="scrollable"]'
                        ];
                        const isVisible = (el) => {
                            if (!el) return false;
                            const rect = el.getBoundingClientRect();
                            return rect.width > 0 && rect.height > 0 && rect.bottom > 0 && rect.right > 0;
                        };
                        const panels = [];
                        const seen = new Set();
                        for (const selector of selectors) {
                            for (const el of document.querySelectorAll(selector)) {
                                if (!el || seen.has(el) || !isVisible(el)) continue;
                                seen.add(el);
                                panels.push({
                                    selector,
                                    id: el.id || "",
                                    className: String(el.className || ""),
                                    scrollTop: el.scrollTop,
                                    scrollHeight: el.scrollHeight,
                                    clientHeight: el.clientHeight,
                                    itemCount: el.querySelectorAll('.mix-and-match-container, [class*="mix-and-match-container"]').length,
                                    visibleText: String(el.innerText || "").slice(0, 500)
                                });
                            }
                        }
                        const visibleItems = Array.from(
                            document.querySelectorAll('.mix-and-match-container img, [class*="mix-and-match-container"] img')
                        )
                            .filter((img) => isVisible(img))
                            .slice(0, 40)
                            .map((img) => img.src || "");
                        return {
                            url: location.href,
                            title: document.title,
                            panels,
                            visibleItems
                        };
                    }"""
                )
            except Exception as exc:
                dom_snapshot = {"snapshot_error": str(exc)}

            payload = {
                "at": datetime.now(timezone.utc).isoformat(),
                "profile_id": str(profile_id or ""),
                "selector_key": str(selector_key or ""),
                "selector": str(selector or ""),
                "requirements": requirements,
                "scroll_history": scroll_history or [],
                "dom": dom_snapshot,
            }
            snapshot_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            if self.logger:
                self.logger.warning(f"Outfit selector failure snapshot written: {snapshot_path}")
            return str(snapshot_path)
        except Exception as exc:
            if self.logger:
                self.logger.warning(f"Could not write outfit failure snapshot: {exc}")
            return ""

    async def click_outfit_item(self, ctx, selector, profile_id="", selector_key=""):
        requirements = self.extract_outfit_requirements(selector)
        if not requirements["fragments"] and not requirements["params"]:
            raise Exception(f"Could not parse outfit selector: {selector}")

        await self.reset_editor_panel_scroll(ctx)
        scroll_history = []

        for attempt_index in range(_OUTFIT_PANEL_SCAN_STEPS):
            clicked = await ctx.evaluate(
                """({ requirements }) => {""" + _OUTFIT_ACTIVE_PANEL_RESOLVER + r"""
                    const isVisible = (el) => {
                        const rect = el.getBoundingClientRect();
                        return rect.width > 0 && rect.height > 0 && rect.bottom > 0 && rect.right > 0;
                    };
                    const panel = activePanel;
                    if (!panel) return false;

                    const items = Array.from(
                        panel.querySelectorAll('.mix-and-match-container, [class*="mix-and-match-container"]')
                    ).filter(isVisible);
                    const match = items.find((item) => {
                        const img = item.querySelector('img');
                        if (!img || !img.src) return false;
                        let url = null;
                        try {
                            url = new URL(img.src, document.baseURI);
                        } catch (error) {
                            return false;
                        }
                        const matchesFragments = requirements.fragments.every((fragment) => img.src.includes(fragment));
                        if (!matchesFragments) return false;
                        return Object.entries(requirements.params).every(
                            ([key, value]) => url.searchParams.get(key) === value
                        );
                    });

                    if (!match) return false;

                    match.scrollIntoView({ block: 'center', inline: 'nearest' });
                    match.click();
                    return true;
                }""",
                {"requirements": requirements},
            )

            if clicked:
                return True

            panel_state = await self.scroll_editor_panel(ctx, "down")
            if panel_state:
                scroll_history.append({
                    "attempt": attempt_index + 1,
                    **panel_state,
                })
            if not panel_state or not panel_state.get("moved"):
                break
            await asyncio.sleep(0.35)

        clicked = await ctx.evaluate(
            """({ requirements }) => {""" + _OUTFIT_ACTIVE_PANEL_RESOLVER + r"""
                const isVisible = (el) => {
                    if (!el) return false;
                    const rect = el.getBoundingClientRect();
                    return rect.width > 0 && rect.height > 0 && rect.bottom > 0 && rect.right > 0;
                };

                const panel = activePanel;
                if (!panel) return false;
                const candidates = Array.from(
                    panel.querySelectorAll(
                        '.mix-and-match-container, [class*="mix-and-match-container"]'
                    )
                ).filter(isVisible);

                const match = candidates.find((item) => {
                    const img = item.querySelector('img');
                    if (!img || !img.src) return false;
                    const matchesFragments = requirements.fragments.every((fragment) => img.src.includes(fragment));
                    if (!matchesFragments) return false;

                    let url = null;
                    try {
                        url = new URL(img.src);
                    } catch (error) {
                        url = null;
                    }

                    return Object.entries(requirements.params).every(([key, value]) => {
                        if (url && url.searchParams.get(key) === value) {
                            return true;
                        }
                        return img.src.includes(`${key}=${value}`);
                    });
                });

                if (!match) return false;

                match.scrollIntoView({ block: 'center', inline: 'nearest' });
                match.dispatchEvent(new MouseEvent('pointerdown', { bubbles: true, cancelable: true, view: window }));
                match.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true, view: window }));
                match.dispatchEvent(new MouseEvent('pointerup', { bubbles: true, cancelable: true, view: window }));
                match.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, cancelable: true, view: window }));
                match.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
                return true;
            }""",
            {"requirements": requirements},
        )
        if clicked:
            return True

        await self.write_outfit_failure_snapshot(
            ctx,
            selector,
            requirements,
            scroll_history,
            profile_id=profile_id,
            selector_key=selector_key,
        )
        raise Exception(f"Failed to click outfit selector: {selector}")

    async def click_random_earring(self, profile_id):
        selectors = BITMOJI_SELECTORS["items"].get("random_earrings", [])
        if not selectors:
            raise Exception("No random earring selectors configured.")

        rng = random.SystemRandom()

        # Retry the whole earring pick a few times: the paired-earring panel can
        # take a moment to paint its options, which is the classic "stops at
        # paired earrings" symptom. Each pass waits for items and tries every
        # configured earring option — so it always lands a real earring rather
        # than failing the profile.
        for attempt in range(_STEP_UNIT_RETRIES):
            ctx = await self.get_editor_context()
            await self.wait_for_category_items(ctx)

            indices = list(range(len(selectors)))
            rng.shuffle(indices)
            for index in indices:
                selector = selectors[index]
                try:
                    target = await self.first_actionable_locator(ctx.locator(selector))
                    if target is None:
                        continue
                    await self.scroll_target_into_panel(target)
                    await self.human_delay()
                    await target.click()
                    return True
                except Exception:
                    continue

            if attempt < _STEP_UNIT_RETRIES - 1:
                if self.logger:
                    self.logger.warning(
                        f"Earring options not ready (attempt {attempt + 1}/{_STEP_UNIT_RETRIES}); "
                        "re-opening the earrings panel."
                    )
                await self._reopen_earrings_panel(profile_id)
                await asyncio.sleep(0.6)

        raise Exception("Failed to click a random earring option.")

    async def _reopen_earrings_panel(self, profile_id):
        """Re-open Accessories -> Earrings -> Paired so the earring options
        repaint before the next pick attempt. Best-effort: a failure here just
        lets the next attempt try against whatever is currently shown."""
        for step_key in ("categories.earrings", "subcategories.paired_earring"):
            try:
                await self.safe_click(step_key, profile_id, retries=2)
                await self.human_delay(0.2, 0.5, kind="think")
            except Exception:
                continue

    async def safe_click(self, selector_key, profile_id=None, retries=None):
        await self.wait_if_paused()

        if retries is None:
            retries = _STEP_CLICK_RETRIES

        if selector_key == "traits.random_earrings":
            return await self.click_random_earring(profile_id)

        selector = self.resolve_selector(selector_key)

        for attempt in range(retries):
            ctx = None
            try:
                await self.wait_if_paused()
                ctx = await self.get_editor_context()

                if self.is_outfit_selector(selector):
                    # Don't scan for the exact outfit item until the panel has
                    # actually rendered items (removes the empty-panel race).
                    await self.wait_for_category_items(ctx)
                    await self.click_outfit_item(ctx, selector, profile_id=profile_id, selector_key=selector_key)
                    return True

                if selector_key.startswith("items."):
                    items = ctx.locator(selector)
                    visible_items = await self.get_visible_locators(items)
                    count = len(visible_items)

                    if count == 0:
                        fallback = BITMOJI_SELECTORS["items"]["fallback"]
                        items = ctx.locator(fallback)
                        visible_items = await self.get_visible_locators(items)
                        count = len(visible_items)

                    if count == 0:
                        raise Exception(f"No items found for selector: {selector}")

                    if "skin_tone" in selector_key:
                        index = 2
                    elif "hair" in selector_key:
                        index = 3
                    else:
                        random.seed(profile_id)
                        index = random.randint(0, count - 1)

                    index = max(0, min(index, count - 1))

                    print(f"[DEBUG] items click -> index={index} / count={count}")

                    target = visible_items[index]
                    await self.scroll_target_into_panel(target)
                    await self.human_delay()
                    await target.click()
                    return True

                if self.is_skin_tone_selector_key(selector_key):
                    if await self.click_skin_tone_trait(ctx, selector):
                        return True

                base_selector, nth_index = self.parse_nth_selector(selector)
                if base_selector is not None:
                    candidates = await self.get_visible_locators(ctx.locator(base_selector))
                    if nth_index >= len(candidates):
                        raise Exception(f"nth selector index out of range for: {base_selector} >> nth={nth_index}")
                    target = candidates[nth_index]
                    await self.scroll_target_into_panel(target)
                    await self.human_delay()
                    await target.click()
                    return True

                target = await self.first_actionable_locator(ctx.locator(selector))
                if self.should_scroll_panel_for_selector(selector_key, selector):
                    scanned_target = await self.find_target_with_panel_scan(ctx, selector)
                    if scanned_target is not None:
                        target = scanned_target

                if target is None:
                    if (not self.is_skin_tone_selector_key(selector_key)) and await self.click_by_dom_signature(ctx, selector_key, selector):
                        return True
                    raise Exception(f"No visible target found for selector: {selector}")
                await self.scroll_target_into_panel(target)
                await self.human_delay()
                await target.click()
                return True
            except PlaywrightTimeoutError:
                if self.logger:
                    self.logger.warning(f"Retry click ({attempt+1}) -> {selector}")
                if ctx is not None and self.should_scroll_panel_for_selector(selector_key, selector):
                    await self.reset_editor_panel_scroll(ctx)
                await asyncio.sleep(1)
            except Exception as exc:
                if self.logger:
                    self.logger.warning(f"Retry click ({attempt+1}) -> {selector} | {exc}")
                try:
                    if ctx is not None and self.is_skin_tone_selector_key(selector_key):
                        if await self.click_skin_tone_trait(ctx, selector):
                            return True
                    if ctx is not None and (not self.is_skin_tone_selector_key(selector_key)) and (not self.is_outfit_selector(selector)) and await self.click_by_dom_signature(ctx, selector_key, selector):
                        return True
                except Exception:
                    pass
                if ctx is not None and self.should_scroll_panel_for_selector(selector_key, selector):
                    await self.reset_editor_panel_scroll(ctx)
                await asyncio.sleep(1)

        raise Exception(f"Failed to click selector: {selector}")

    async def apply_face_model(self, model, profile_id):
        model = MODEL_ALIASES.get(model, model)
        if model not in BITMOJI_SELECTORS["models"]:
            raise Exception(f"Model not defined: {model}")

        return await self._apply_model_face_steps(model, profile_id)

    def _bitmoji_config_selector(self, model, feature, bitmoji_models):
        """Return a live-editor selector for this model's configured option for
        ``feature`` (fixed or random-from-pool), or ``None`` to use the preset."""
        try:
            from core.bitmoji_config import build_selector, resolve_option
            option_id = resolve_option(model, feature, bitmoji_models)
            if not option_id:
                return None
            selector = build_selector(feature, option_id)
            if selector and self.logger:
                self.logger.info(f"{model} | Bitmoji config: {feature}={option_id}")
            return selector
        except Exception as exc:
            if self.logger:
                self.logger.warning(f"Bitmoji config lookup failed for {feature}: {exc}")
            return None

    async def _apply_model_face_steps(self, model, profile_id):
        face_steps = BITMOJI_SELECTORS["models"][model]["face"]
        print(f"Applying model: {model}")

        # Per-model "Configure Bitmoji" overrides (fixed option or random pool).
        # Loaded once per run; unconfigured features fall back to the presets.
        try:
            from core.bitmoji_config import load_models as _load_bitmoji_models
            bitmoji_models = _load_bitmoji_models()
        except Exception:
            bitmoji_models = {}

        # The face sequence is: open a category (and maybe a subcategory), then
        # click the exact trait. Track the most recent open_* steps so that when
        # a trait click can't land we can *replay them* (re-open the panel) and
        # retry the same trait — instead of failing the whole profile at a
        # random trait like paired earrings. The replay never changes which
        # trait is selected.
        context_steps = []  # list of (step_name, selector_key) since last trait

        for step in face_steps:
            await self.wait_if_paused()
            refresh_runtime_settings = getattr(self, "refresh_runtime_settings", None)
            if callable(refresh_runtime_settings):
                refresh_runtime_settings()
            step_name = step.get("step")
            selector_key = step.get("selector")
            print(f"Applying step: {step_name}")

            # Surface the exact face step (e.g. "face_hair_style") so a failure
            # here is recorded as that step rather than a generic bitmoji_failed.
            report_step = getattr(self, "report_step", None)
            if callable(report_step) and step_name:
                report_step(f"face_{step_name}")

            if self.logger:
                self.logger.info(f"{model} | Step: {step_name}")

            if not selector_key:
                print(f"[STOP] Missing selector at step: {step_name}")
                if self.logger:
                    self.logger.warning(f"{model} STOPPED at {step_name} (no selector)")
                return False

            # Per-model Bitmoji config wins when this feature is configured;
            # otherwise fall back to the legacy hair randomizer / preset.
            config_selector = self._bitmoji_config_selector(model, step_name, bitmoji_models)
            if config_selector:
                selector_key = config_selector
            elif bool(getattr(self, "hair_randomizer_enabled", False)) and step_name == "hair_style":
                random_hair_selector = self.get_random_hair_selector(model, profile_id)
                if random_hair_selector:
                    selector_key = random_hair_selector
                    if self.logger:
                        self.logger.info(f"{model} | Hair randomizer selected")

            is_open_step = bool(step_name) and str(step_name).startswith("open_")

            applied = await self._apply_face_step_with_recovery(
                model, profile_id, step_name, selector_key,
                context_steps=context_steps, is_open_step=is_open_step,
            )
            if not applied:
                print(f"[STOP] Failed at step: {step_name}")
                if self.logger:
                    self.logger.warning(f"{model} FAILED at {step_name}")
                return False

            # An open_ step becomes context for the trait that follows; a trait
            # click resets the context (its panel is now consumed).
            if is_open_step:
                context_steps.append((step_name, selector_key))
            else:
                context_steps = []

        print(f"{model} face applied")
        return True

    async def _apply_face_step_with_recovery(self, model, profile_id, step_name,
                                             selector_key, context_steps, is_open_step):
        """Click one face step, retrying the whole unit on failure.

        For a trait step, a failed click replays the recent open_* category /
        subcategory steps (re-opening the panel) and waits for its items to
        render before trying the same trait again. This makes transient
        panel-not-ready failures recover instead of aborting the profile."""
        last_exc = None
        for attempt in range(_STEP_UNIT_RETRIES):
            try:
                await self.safe_click(selector_key, profile_id)
                await self.human_delay()
                return True
            except Exception as exc:
                last_exc = exc
                if self.logger:
                    self.logger.warning(
                        f"{model} step {step_name} attempt {attempt + 1}/{_STEP_UNIT_RETRIES} "
                        f"failed: {exc}"
                    )
                if attempt >= _STEP_UNIT_RETRIES - 1:
                    break
                # Re-open the category/subcategory chain that leads to this trait
                # so its panel repaints, then wait for the items to appear.
                if not is_open_step and context_steps:
                    for ctx_step_name, ctx_selector_key in context_steps:
                        try:
                            await self.safe_click(ctx_selector_key, profile_id, retries=2)
                            await self.human_delay(0.2, 0.5, kind="think")
                        except Exception:
                            continue
                    try:
                        await self.wait_for_category_items()
                    except Exception:
                        pass
                await asyncio.sleep(0.6)

        if self.logger and last_exc is not None:
            self.logger.warning(f"{model} step {step_name} exhausted retries: {last_exc}")
        return False

    async def apply_outfit(self, profile_id, model="", outfit_seed=""):
        await self.wait_if_paused()
        outfit = generate_outfit(profile_id, model=model, outfit_seed=outfit_seed)
        print(f"Applying outfit: {outfit}")
        if self.logger:
            self.logger.info(
                f"[{profile_id}] Outfit plan: model={model or '-'} mode={outfit.get('mode', '-')}"
            )

        report_step = getattr(self, "report_step", None)

        def report(step):
            if callable(report_step):
                report_step(step)

        if outfit["mode"] == "dress":
            report("outfit_dress")
            dress_entry = self.normalize_outfit_entry(outfit["dress"])
            if self.logger:
                self.logger.info(f"[{profile_id}] Outfit dress: {self.describe_outfit_entry(dress_entry)}")
            selected_dress = await self._apply_outfit_piece(
                "categories.dresses", dress_entry["selector"], profile_id,
                fallback_param="top", fallback_pool=outfit.get("dress_pool"),
            )
            await self.pick_configured_color_option(
                profile_id, model, ("dresses",), outfit_seed,
                preferred_color=self.outfit_preferred_color_for_selection(dress_entry, selected_dress),
                selected_option_id=self.outfit_option_id_from_selector(selected_dress, "dresses"),
            )
        else:
            report("outfit_top")
            top_entry = self.normalize_outfit_entry(outfit["top"])
            bottom_entry = self.normalize_outfit_entry(outfit["bottom"])
            if self.logger:
                self.logger.info(f"[{profile_id}] Outfit top: {self.describe_outfit_entry(top_entry)}")
            selected_top = await self._apply_outfit_piece(
                "categories.tops", top_entry["selector"], profile_id,
                fallback_param="top", blocked_ids=BLOCKED_TOP_IDS,
                fallback_pool=outfit.get("top_pool"),
            )
            await self.pick_configured_color_option(
                profile_id, model, ("tops", "outfits"), outfit_seed,
                preferred_color=self.outfit_preferred_color_for_selection(top_entry, selected_top),
                selected_option_id=self.outfit_option_id_from_selector(selected_top, "tops"),
            )
            report("outfit_bottom")
            if self.logger:
                self.logger.info(f"[{profile_id}] Outfit bottom: {self.describe_outfit_entry(bottom_entry)}")
            selected_bottom = await self._apply_outfit_piece(
                "categories.bottoms", bottom_entry["selector"], profile_id,
                fallback_param="bottom", fallback_pool=outfit.get("bottom_pool"),
            )
            # Tuck only becomes clickable once BOTH pieces are in place: the
            # SDK's handleTucking disables the toggle while the current bottom
            # is still underwear/none, and re-enables it on the bottom click.
            # Waiting until now is the only state where a tuckable top+bottom
            # pair actually exposes an enabled switch. A preset with
            # tuck_in=false skips tucking entirely (top stays in its default
            # state) while still carrying on with colour selection.
            if outfit.get("tuck_in", True):
                await self.enable_tuck_if_available()
            await self.pick_configured_color_option(
                profile_id, model, ("bottoms",), outfit_seed,
                preferred_color=self.outfit_preferred_color_for_selection(bottom_entry, selected_bottom),
                selected_option_id=self.outfit_option_id_from_selector(selected_bottom, "bottoms"),
            )

        report("outfit_footwear")
        shoe_entry = self.normalize_outfit_entry(outfit["shoes"])
        if self.logger:
            self.logger.info(f"[{profile_id}] Outfit footwear: {self.describe_outfit_entry(shoe_entry)}")
        selected_shoes = await self._apply_outfit_piece(
            "categories.footwear", shoe_entry["selector"], profile_id,
            fallback_param="footwear", blocked_ids=BLOCKED_FOOTWEAR_IDS,
            fallback_pool=outfit.get("shoes_pool"),
        )
        await self.pick_configured_color_option(
            profile_id, model, ("footwear",), outfit_seed,
            preferred_color=self.outfit_preferred_color_for_selection(shoe_entry, selected_shoes),
            selected_option_id=self.outfit_option_id_from_selector(selected_shoes, "footwear"),
        )
        await self.human_delay()

    async def _apply_outfit_piece(self, category_key, item_selector, profile_id,
                                  fallback_param=None, blocked_ids=None, fallback_pool=None):
        """Open a clothing category and click the exact configured item, retrying
        the pair as a unit. A failed item click re-opens the category (so its
        grid repaints) and waits for items before trying the same item again.

        The configured piece is always preferred. Only if its id has rotated out
        of Bitmoji's catalog (so it can never be found) do we fall back — and the
        fallback stays inside the *same configured pool*: another item from
        ``fallback_pool`` is picked at random (deterministically per profile) so
        the avatar only ever wears an operator-approved item. Picking any random
        catalog item is off by default (see ``_OUTFIT_ALLOW_CATALOG_FALLBACK``)."""
        last_exc = None
        for attempt in range(_STEP_UNIT_RETRIES):
            try:
                await self.safe_click(category_key, profile_id)
                ctx = await self.get_editor_context()
                await self.reset_editor_panel_scroll(ctx)
                await self.wait_for_category_items(ctx)
                await self.safe_click(item_selector, profile_id)
                return item_selector
            except Exception as exc:
                last_exc = exc
                if self.logger:
                    self.logger.warning(
                        f"[{profile_id}] Outfit piece {item_selector} attempt "
                        f"{attempt + 1}/{_STEP_UNIT_RETRIES} failed: {exc}"
                    )
                await asyncio.sleep(0.6)
        # Phase 2: the exact configured item never appeared — almost always because
        # its id rotated out of the catalog. Prefer another item from the SAME
        # configured pool (random per profile) so the profile completes with a
        # curated piece instead of failing/scrolling forever.
        if _OUTFIT_ALLOW_FALLBACK and fallback_pool:
            try:
                fallback_selector = await self._apply_pool_fallback_piece(
                    category_key, item_selector, fallback_pool, profile_id,
                    fallback_param=fallback_param, blocked_ids=blocked_ids,
                )
                if fallback_selector:
                    return fallback_selector
            except Exception as fb_exc:
                if self.logger:
                    self.logger.warning(f"[{profile_id}] Outfit pool fallback for {category_key} failed: {fb_exc}")
        # Last-resort net (opt-in only): the whole configured pool is gone too, so
        # dress the avatar with any available catalog item of this category.
        if _OUTFIT_ALLOW_FALLBACK and _OUTFIT_ALLOW_CATALOG_FALLBACK and fallback_param:
            try:
                if await self._click_any_item_in_open_category(category_key, fallback_param, profile_id, blocked_ids):
                    # The catalogue-wide net cannot tell us which tile it clicked,
                    # so make that uncertainty explicit instead of pretending the
                    # requested id was selected. It remains truthy for legacy
                    # callers that only need to know this step completed.
                    return {"known": False}
            except Exception as fb_exc:
                if self.logger:
                    self.logger.warning(f"[{profile_id}] Outfit catalog fallback for {fallback_param} failed: {fb_exc}")
        # Nothing worked — surface the original failure for the runner to record.
        raise last_exc or Exception(f"Failed to apply outfit piece: {item_selector}")

    def _outfit_selector_text(self, entry):
        """Return the raw selector string for a pool entry (str or {'selector': ...})."""
        if isinstance(entry, dict):
            return str(entry.get("selector", ""))
        return str(entry or "")

    async def _apply_pool_fallback_piece(self, category_key, current_selector, fallback_pool,
                                         profile_id, fallback_param=None, blocked_ids=None):
        """Fallback that stays inside the configured pool.

        When the exact chosen item can't be found, try the *other* items from the
        same style pool in a deterministic per-profile random order, clicking the
        first one that lands. Unlike the old any-catalog fallback this never
        leaves the curated pool, so the substitute is always operator-approved."""
        current_text = self._outfit_selector_text(current_selector)
        blocked = {str(b) for b in (blocked_ids or ())}

        alternates = []
        seen = {current_text}
        for entry in fallback_pool or []:
            text = self._outfit_selector_text(entry)
            if not text or text in seen:
                continue
            if fallback_param and blocked and any(f"{fallback_param}={bid}" in text for bid in blocked):
                continue
            seen.add(text)
            alternates.append((text, entry))

        if not alternates:
            return False

        # Deterministic per-profile order so reruns stay stable.
        random.Random(f"{profile_id}:{current_text}").shuffle(alternates)

        await self.safe_click(category_key, profile_id)
        ctx = await self.get_editor_context()
        await self.reset_editor_panel_scroll(ctx)
        await self.wait_for_category_items(ctx)

        for selector, entry in alternates:
            try:
                await self.safe_click(selector, profile_id, retries=1)
                if self.logger:
                    self.logger.info(
                        f"[{profile_id}] Configured item unavailable in catalog; "
                        f"selected another item from the same pool as fallback."
                    )
                return entry if isinstance(entry, dict) else selector
            except Exception as exc:
                if self.logger:
                    self.logger.warning(f"[{profile_id}] Pool fallback candidate failed: {exc}")
                continue

        return False

    async def _click_any_item_in_open_category(self, category_key, param, profile_id, blocked_ids=None):
        """Opt-in last-resort net: click any available catalog item in the
        (re-opened) category. Only reached when the whole configured pool has
        rotated out AND NYX_OUTFIT_FALLBACK_CATALOG=1.

        Picks deterministically from a per-profile hash so reruns stay stable,
        and skips blocked ids. Returns True if an item was clicked."""
        await self.safe_click(category_key, profile_id)
        ctx = await self.get_editor_context()
        await self.reset_editor_panel_scroll(ctx)
        await self.wait_for_category_items(ctx)
        clicked = await ctx.evaluate(
            """({ param, blocked, seed }) => {""" + _OUTFIT_ACTIVE_PANEL_RESOLVER + r"""
                const isVisible = (el) => {
                    const r = el.getBoundingClientRect();
                    return r.width > 0 && r.height > 0;
                };
                const panel = activePanel;
                if (!panel) return false;
                const items = Array.from(
                    panel.querySelectorAll('.mix-and-match-container, [class*="mix-and-match-container"]')
                ).filter((el) => {
                    if (!isVisible(el)) return false;
                    const img = el.querySelector('img');
                    if (!img || !img.src) return false;
                    if (param && blocked && blocked.length) {
                        const m = img.src.match(new RegExp('[?&]' + param + '=([0-9]+)'));
                        if (m && blocked.indexOf(m[1]) !== -1) return false;
                    }
                    return true;
                });
                if (!items.length) return false;
                let h = 2166136261;
                for (const c of String(seed)) { h ^= c.charCodeAt(0); h = Math.imul(h, 16777619) >>> 0; }
                const el = items[h % items.length];
                el.scrollIntoView({ block: 'center', inline: 'nearest' });
                el.click();
                return true;
            }""",
            {"param": param, "blocked": [str(b) for b in (blocked_ids or [])], "seed": str(profile_id or "")},
        )
        if clicked and self.logger:
            self.logger.info(
                f"[{profile_id}] Configured {param} item unavailable in catalog; "
                f"selected an available {param} as fallback."
            )
        return bool(clicked)

    async def _pick_random_color_option_from_active_panel(self, profile_id, outfit_seed="", preferred_color=None,
                                                          ctx=None):
        """Pick a safe random swatch only from the resolved garment panel.

        This path is used after a configured garment colour cannot be applied.
        It deliberately refuses to use a stale colour picker outside the active
        garment panel; callers that need the historic document-wide picker use
        ``pick_random_color_option`` without ``active_panel_only`` instead.
        """
        await self.wait_if_paused()
        if ctx is None:
            ctx = await self.get_editor_context()
        if ctx is None:
            return False

        # The panel-scoped picker can re-render after the garment click; wait
        # (bounded) for a colour picker to be present so the configured colour is
        # not skipped because it was clicked too early.
        deadline = asyncio.get_event_loop().time() + 6.0
        while asyncio.get_event_loop().time() < deadline:
            await self.wait_if_paused()
            try:
                if await ctx.evaluate(_OUTFIT_ACTIVE_PANEL_COLOR_PICKER_READY):
                    break
            except Exception:
                pass
            await asyncio.sleep(0.25)

        preferred_parts = []
        preferred_hex = ""
        if isinstance(preferred_color, dict):
            preferred_parts = [
                str(part) for part in preferred_color.get("background_contains", []) if str(part)
            ]
            preferred_hex = str(preferred_color.get("hex") or "").strip()
        seed_source = str(outfit_seed).strip() or f"{profile_id}:{random.random()}"
        try:
            clicked = await ctx.evaluate(
                r"""({ seed, preferredParts, preferredHex }) => {""" + _OUTFIT_ACTIVE_PANEL_RESOLVER + r"""
                    if (!activePanel) return false;
                    const isVisible = (el) => {
                        if (!el) return false;
                        const rect = el.getBoundingClientRect();
                        return rect.width > 0 && rect.height > 0 && rect.bottom > 0 && rect.right > 0;
                    };
                    const pickers = Array.from(activePanel.querySelectorAll(
                        'div.colour-picker-container, div.colour-picker'
                    )).filter(isVisible);
                    const options = Array.from(new Set(pickers.flatMap((picker) => Array.from(
                        picker.querySelectorAll('.colour-picker-option')
                    )))).filter(isVisible);
                    if (!options.length) return false;

                    const isNeon = (el) => {
                        const parts = String(getComputedStyle(el).backgroundColor || '').match(/\d+/g);
                        if (!parts || parts.length < 3) return false;
                        const [r, g, b] = parts.slice(0, 3).map(Number);
                        const blocked = [[255,107,14],[246,249,10],[175,255,83],[50,255,40],[40,255,136],[10,255,214],[255,0,151],[78,21,86],[19,19,19],[42,41,45],[104,112,114],[29,88,82],[255,116,23]];
                        if (blocked.some(([br, bgc, bb]) => r === br && g === bgc && b === bb)) return true;
                        const max = Math.max(r, g, b);
                        const min = Math.min(r, g, b);
                        const l = (max + min) / 510.0;
                        if (max === min) return false;
                        const d = (max - min) / 255.0;
                        const s = l > 0.5 ? d / (2.0 - (max + min) / 255.0) : d / ((max + min) / 255.0);
                        if (s > 0.75 && l > 0.35 && l < 0.85) return true;
                        if (l < 0.22) return true;
                        if (g > r && g > b && s < 0.45 && l < 0.45) return true;
                        let h;
                        if (max === r) h = (g - b) / 255.0 / d + (g < b ? 6 : 0);
                        else if (max === g) h = (b - r) / 255.0 / d + 2;
                        else h = (r - g) / 255.0 / d + 4;
                        h /= 6;
                        return h > 0.15 && h < 0.45 && s > 0.55 && l > 0.3;
                    };

                    const toRGB = (value) => {
                        let hex = String(value || '').trim().replace(/^#/, '');
                        if (hex.length === 3) hex = hex.split('').map((char) => char + char).join('');
                        if (!/^[0-9a-fA-F]{6}$/.test(hex)) return null;
                        return [parseInt(hex.slice(0, 2), 16), parseInt(hex.slice(2, 4), 16), parseInt(hex.slice(4, 6), 16)];
                    };
                    const parseRGB = (value) => {
                        const parts = String(value || '').match(/\d+/g);
                        return parts && parts.length >= 3 ? parts.slice(0, 3).map(Number) : null;
                    };
                    const preferredByHex = (() => {
                        const want = toRGB(preferredHex);
                        if (!want) return null;
                        let best = null;
                        let bestDistance = Infinity;
                        for (const option of options) {
                            const rgb = parseRGB(getComputedStyle(option).backgroundColor || '');
                            if (!rgb) continue;
                            const distance = (rgb[0] - want[0]) ** 2 + (rgb[1] - want[1]) ** 2 + (rgb[2] - want[2]) ** 2;
                            if (distance < bestDistance) {
                                best = option;
                                bestDistance = distance;
                            }
                        }
                        // An explicit operator colour (shared_outfit) is
                        // authoritative: click the closest available swatch rather
                        // than dropping to a random colour, so every account using
                        // the preset gets the configured colour consistently.
                        return best ? best : null;
                    })();
                    const preferred = options.find((option) => {
                        if (!preferredParts.length) return false;
                        const inlineBackground = option.style.background || '';
                        const computedBackground = getComputedStyle(option).background || '';
                        const background = `${inlineBackground} ${computedBackground}`;
                        return preferredParts.every((part) => background.includes(part));
                    });
                    const eligible = options.filter((option) => !isNeon(option));
                    const selected = preferredByHex || preferred || eligible[(() => {
                        if (!eligible.length) return -1;
                        let hash = 2166136261;
                        for (const char of String(seed)) {
                            hash ^= char.charCodeAt(0);
                            hash = Math.imul(hash, 16777619) >>> 0;
                        }
                        return hash % eligible.length;
                    })()];
                    if (!selected) return false;
                    selected.scrollIntoView({ block: 'center', inline: 'nearest' });
                    selected.click();
                    return true;
                }""",
                {"seed": seed_source, "preferredParts": preferred_parts, "preferredHex": preferred_hex},
            )
        except Exception:
            return False
        if clicked:
            await self.human_delay(0.3, 0.8, kind="think")
        return bool(clicked)

    async def pick_random_color_option(self, profile_id, outfit_seed="", preferred_color=None,
                                       active_panel_only=False, ctx=None):
        if active_panel_only:
            return await self._pick_random_color_option_from_active_panel(
                profile_id, outfit_seed, preferred_color=preferred_color, ctx=ctx,
            )
        await self.wait_if_paused()

        ctx = await self.get_editor_context()
        color_picker = ctx.locator("div.colour-picker-container, div.colour-picker")

        try:
            await color_picker.first.wait_for(state="visible", timeout=10000)
        except Exception:
            return False

        color_options = ctx.locator("div.colour-picker-container .colour-picker-option, div.colour-picker .colour-picker-option")
        visible_options = await self.get_visible_locators(color_options)
        count = len(visible_options)
        if count == 0:
            return False

        preferred_hex = ""
        if isinstance(preferred_color, dict):
            preferred_hex = str(preferred_color.get("hex") or "").strip()
        if preferred_hex:
            best_option = None
            best_distance = None
            for index in range(count):
                try:
                    option = visible_options[index]
                    distance = await option.evaluate(
                        """(el, targetHex) => {
                            const toRGB = (value) => {
                                let hex = String(value || '').trim().replace(/^#/, '');
                                if (hex.length === 3) hex = hex.split('').map((char) => char + char).join('');
                                if (!/^[0-9a-fA-F]{6}$/.test(hex)) return null;
                                return [parseInt(hex.slice(0, 2), 16), parseInt(hex.slice(2, 4), 16), parseInt(hex.slice(4, 6), 16)];
                            };
                            const parse = (value) => {
                                const parts = String(value || '').match(/\\d+/g);
                                return parts && parts.length >= 3 ? parts.slice(0, 3).map(Number) : null;
                            };
                            const want = toRGB(targetHex);
                            const actual = parse(window.getComputedStyle(el).backgroundColor || '');
                            if (!want || !actual) return null;
                            return (actual[0] - want[0]) ** 2 + (actual[1] - want[1]) ** 2 + (actual[2] - want[2]) ** 2;
                        }""",
                        preferred_hex,
                    )
                    if distance is None:
                        continue
                    if best_distance is None or distance < best_distance:
                        best_distance = distance
                        best_option = option
                except Exception:
                    continue
            # Authoritative configured colour: click the closest swatch even if it
            # is not within the tolerance, so an explicitly configured colour is
            # applied consistently instead of falling back to a random one.
            if best_option is not None and best_distance is not None:
                await best_option.scroll_into_view_if_needed(timeout=4000)
                await self.human_delay(0.2, 0.5, kind="think")
                await best_option.click()
                await self.human_delay(0.3, 0.7, kind="think")
                return True

        if preferred_color and preferred_color.get("background_contains"):
            required_parts = preferred_color.get("background_contains", [])
            for index in range(count):
                try:
                    option = visible_options[index]
                    matches_preferred = await option.evaluate(
                        """(el, requiredParts) => {
                            const inlineBackground = el.style.background || "";
                            const computedBackground = window.getComputedStyle(el).background || "";
                            const haystack = `${inlineBackground} ${computedBackground}`;
                            return requiredParts.every((part) => haystack.includes(part));
                        }""",
                        required_parts,
                    )
                    if not matches_preferred:
                        continue
                    await option.scroll_into_view_if_needed(timeout=4000)
                    await self.human_delay(0.2, 0.5, kind="think")
                    await option.click()
                    await self.human_delay(0.3, 0.7, kind="think")
                    return True
                except Exception:
                    continue

        seed_source = str(outfit_seed).strip() or f"{profile_id}:{random.random()}"
        rng = random.Random(seed_source)
        indices = list(range(count))
        rng.shuffle(indices)

        for index in indices:
            try:
                option = visible_options[index]
                is_neon = await option.evaluate(r"""(el) => {
                    const style = window.getComputedStyle(el);
                    const bg = style.backgroundColor;
                    if (!bg || !bg.startsWith('rgb')) return false;
                    const parts = bg.match(/(\d+)/g);
                    if (!parts || parts.length < 3) return false;
                    const r = parseInt(parts[0]);
                    const g = parseInt(parts[1]);
                    const b = parseInt(parts[2]);
                    const blockedColors = [[255,107,14],[246,249,10],[175,255,83],[50,255,40],[40,255,136],[10,255,214],[255,0,151],[78,21,86],[19,19,19],[42,41,45],[104,112,114],[29,88,82],[255,116,23]];
                    const isBlockedExact = blockedColors.some(([br, bgc, bb]) => r === br && g === bgc && b === bb);
                    if (isBlockedExact) return true;
                    const max = Math.max(r, g, b);
                    const min = Math.min(r, g, b);
                    const l = (max + min) / 510.0;
                    if (max === min) return false;
                    const d = (max - min) / 255.0;
                    const s = l > 0.5 ? d / (2.0 - (max + min) / 255.0) : d / ((max + min) / 255.0);
                    if (s > 0.75 && l > 0.35 && l < 0.85) return true;
                    if (l < 0.22) return true;
                    if (g > r && g > b && s < 0.45 && l < 0.45) return true;
                    let h;
                    if (max === r) h = (g - b) / 255.0 / d + (g < b ? 6 : 0);
                    else if (max === g) h = (b - r) / 255.0 / d + 2;
                    else h = (r - g) / 255.0 / d + 4;
                    h /= 6;
                    if (h > 0.15 && h < 0.45 && s > 0.55 && l > 0.3) return true;
                    return false;
                }""")

                if is_neon:
                    continue

                await option.scroll_into_view_if_needed(timeout=4000)
                await self.human_delay(0.2, 0.6, kind="think")
                await option.click()
                await self.human_delay(0.3, 0.8, kind="think")
                return True
            except Exception:
                continue

        return False

    async def pick_configured_color_option(self, profile_id, model, features, outfit_seed="", preferred_color=None,
                                           selected_option_id=None):
        """Apply the operator's per-model colour choice for an outfit piece.

        Reads the "Configure Nyxmoji" colour config for ``features`` (a fixed
        colour, or a random pick from the chosen colour pool) and clicks the
        nearest matching swatch in the live colour wheel. When nothing is
        configured — or the swatch can't be matched — we fall back to the normal
        random colour pick, so unconfigured models behave exactly as before.
        Colour is cosmetic and must never fail a profile, so this never raises."""
        if isinstance(features, str):
            features = (features,)
        selected_option_id = str(selected_option_id or "").strip() or None

        # A completed live scan makes an empty swatch list authoritative.  Avoid
        # even opening a picker for those garments: an old picker left in the DOM
        # could otherwise recolour a different item, and the legacy random
        # fallback would choose a colour the selected garment does not support.
        selected_item_is_verified_color_capable = False
        if selected_option_id:
            try:
                from core.bitmoji_config import catalog_option, load_catalog_raw, option_colors
                catalog = load_catalog_raw()
                for selected_feature in features:
                    selected_item = catalog_option(selected_feature, selected_option_id, catalog)
                    if selected_item is None or selected_item.get("colors_verified") is not True:
                        continue
                    if not option_colors(selected_feature, selected_option_id, catalog):
                        if self.logger:
                            self.logger.info(
                                f"[{profile_id}] Nyxmoji colour skipped: {selected_feature}="
                                f"{selected_option_id} has no verified swatches"
                            )
                        return False
                    selected_item_is_verified_color_capable = True
            except Exception as exc:
                if self.logger:
                    self.logger.warning(
                        f"[{profile_id}] colour capability lookup failed for {features}: {exc}"
                    )
        target_hex = None
        configured_colour_requested = False
        preferred_hex_requested = (
            isinstance(preferred_color, dict)
            and bool(str(preferred_color.get("hex") or "").strip())
        )
        if preferred_hex_requested and str(preferred_color.get("source") or "") == "shared_outfit":
            # The live scanner captures this as one global wheel shared by all
            # garment panels, so do not require the picker to be nested in the
            # active garment container.
            return await self.pick_random_color_option(
                profile_id, outfit_seed, preferred_color=preferred_color,
                active_panel_only=False,
            )
        try:
            from core.bitmoji_config import load_models as _load_models, resolve_option_color
            models = _load_models()
            model_config = models.get(model, {}) if isinstance(models, dict) else {}
            for feat in features:
                selection = model_config.get(feat) if isinstance(model_config, dict) else None
                if isinstance(selection, dict):
                    mode = str(selection.get("mode") or "").lower()
                    if mode == "fixed":
                        configured_colour_requested = configured_colour_requested or bool(
                            str(selection.get("color") or "").strip()
                        )
                    elif mode == "random":
                        colors = selection.get("colors")
                        configured_colour_requested = configured_colour_requested or (
                            isinstance(colors, (list, tuple))
                            and any(str(color).strip() for color in colors if color is not None)
                        )
                if selected_option_id:
                    target_hex = resolve_option_color(model, feat, models, selected_option_id)
                else:
                    target_hex = resolve_option_color(model, feat, models)
                if target_hex:
                    break
        except Exception as exc:
            if self.logger:
                self.logger.warning(f"[{profile_id}] colour config lookup failed for {features}: {exc}")

        if not target_hex:
            if selected_item_is_verified_color_capable and configured_colour_requested:
                if self.logger:
                    self.logger.info(
                        f"[{profile_id}] Nyxmoji colour skipped: configured colour is unavailable for "
                        f"{selected_option_id}"
                    )
                return False
            return await self.pick_random_color_option(
                profile_id, outfit_seed, preferred_color=preferred_color,
                active_panel_only=preferred_hex_requested,
            )

        preserve_verified_configured_colour = (
            selected_item_is_verified_color_capable and configured_colour_requested
        )

        def log_configured_picker_unavailable(reason):
            if self.logger:
                self.logger.info(
                    f"[{profile_id}] Nyxmoji colour skipped: configured swatch {reason} for "
                    f"{selected_option_id}; selected garment left unchanged"
                )

        ctx = None
        try:
            await self.wait_if_paused()
            ctx = await self.get_editor_context()
            picker_ready = False
            deadline = asyncio.get_event_loop().time() + 10.0
            while asyncio.get_event_loop().time() < deadline:
                await self.wait_if_paused()
                try:
                    if await ctx.evaluate(_OUTFIT_ACTIVE_PANEL_COLOR_PICKER_READY):
                        picker_ready = True
                        break
                except Exception:
                    pass
                await asyncio.sleep(0.2)
            if not picker_ready:
                if preserve_verified_configured_colour:
                    log_configured_picker_unavailable("is unavailable in the active panel")
                    return False
                return await self.pick_random_color_option(
                    profile_id, outfit_seed, preferred_color=preferred_color,
                    active_panel_only=True, ctx=ctx,
                )
            clicked = await ctx.evaluate(
                r"""(targetHex) => {""" + _OUTFIT_ACTIVE_PANEL_RESOLVER + r"""
                    const toRGB = (h) => {
                        h = String(h).replace('#', '');
                        if (h.length === 3) h = h.split('').map(c => c + c).join('');
                        if (h.length !== 6) return null;
                        return [parseInt(h.slice(0, 2), 16), parseInt(h.slice(2, 4), 16), parseInt(h.slice(4, 6), 16)];
                    };
                    const parse = (s) => { const m = String(s).match(/\d+/g); return (m && m.length >= 3) ? m.slice(0, 3).map(Number) : null; };
                    const want = toRGB(targetHex);
                    if (!want || !activePanel) return false;
                    const isVisible = (el) => {
                        if (!el) return false;
                        const r = el.getBoundingClientRect();
                        return r.width > 0 && r.height > 0 && r.bottom > 0 && r.right > 0;
                    };
                    const pickers = Array.from(activePanel.querySelectorAll(
                        'div.colour-picker-container, div.colour-picker'
                    )).filter(isVisible);
                    const opts = Array.from(new Set(pickers.flatMap((picker) => Array.from(
                        picker.querySelectorAll('.colour-picker-option')
                    )))).filter(isVisible);
                    let best = null, bestD = Infinity;
                    for (const el of opts) {
                        const rgb = parse(getComputedStyle(el).backgroundColor || '');
                        if (!rgb) continue;
                        const d = (rgb[0] - want[0]) ** 2 + (rgb[1] - want[1]) ** 2 + (rgb[2] - want[2]) ** 2;
                        if (d < bestD) { bestD = d; best = el; }
                    }
                    if (!best || bestD > 1600) return false;   // ~40 per channel tolerance
                    best.scrollIntoView({ block: 'center', inline: 'nearest' });
                    best.click();
                    return true;
                }""",
                target_hex,
            )
            if clicked:
                await self.human_delay(0.3, 0.7, kind="think")
                if self.logger:
                    self.logger.info(f"[{profile_id}] Nyxmoji colour applied: {target_hex}")
                return True
        except Exception as exc:
            if self.logger:
                self.logger.warning(f"[{profile_id}] colour apply failed for {features}: {exc}")
        if preserve_verified_configured_colour:
            log_configured_picker_unavailable("could not be matched or clicked")
            return False
        # Configured colour couldn't be matched — keep a valid look only if the
        # currently active garment panel still owns a usable picker.
        return await self.pick_random_color_option(
            profile_id, outfit_seed, preferred_color=preferred_color,
            active_panel_only=True, ctx=ctx,
        )
