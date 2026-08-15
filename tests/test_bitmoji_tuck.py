"""Tuck-in toggle behaviour for the Bitmoji outfit flow.

The real Bitmoji SDK renders the tuck control as a hidden checkbox
(``input#tuck-toggle.checkbox`` is always ``display:none``) plus a visible
``label.switch``; the disabled ("grayed out") state is an inline
``opacity: 0.2`` on ``.tuck-container`` and the authoritative on/off state is
the ``Tucked!``/``Untucked!`` caption text, not the checkbox's checked flag.

Regression cover for:
  - the toggle being enabled, untucked and clickable -> click the switch
  - an already-tucked top being left alone (return True without clicking)
  - a disabled/grayed-out toggle (top not tuckable) -> skip without clicking
  - the control being absent (e.g. not on the Tops/Bottoms panel) -> skip
  - a click that never flips the caption -> report failure after retries
"""
import unittest
from unittest import mock

from core.bitmoji.outfit_flow import BitmojiOutfitMixin


class _FakeSwitch:
    def __init__(self, visible=True):
        self._visible = visible
        self.clicked = False
        self.force_clicked = False

    @property
    def first(self):
        return self

    async def is_visible(self):
        return self._visible

    async def scroll_into_view_if_needed(self, timeout=None):
        return None

    async def click(self, force=False):
        if force:
            self.force_clicked = True
        self.clicked = True

    async def check(self, force=False):
        self.clicked = True


class _FakeCtx:
    def __init__(self, states, switch_visible=True):
        self._states = list(states)
        self._switch = _FakeSwitch(visible=switch_visible)
        self.evaluated = 0

    def locator(self, selector):
        return self._switch

    async def evaluate(self, js, arg=None):
        self.evaluated += 1
        return self._states.pop(0) if self._states else "ready"


class _StubTuck(BitmojiOutfitMixin):
    def __init__(self, ctx):
        self.logger = None
        self._ctx = ctx

    async def wait_if_paused(self):
        return None

    async def get_editor_context(self):
        return self._ctx

    async def human_delay(self, *a, **k):
        return None

    async def _read_tuck_state(self, ctx):
        return await ctx.evaluate("() => true")


class EnableTuckTests(unittest.IsolatedAsyncioTestCase):
    async def test_ready_toggle_gets_clicked_until_caption_flips(self):
        ctx = _FakeCtx(["ready", "checked"])
        stub = _StubTuck(ctx)
        with mock.patch("asyncio.sleep", new=mock.AsyncMock()) as sleep:
            result = await stub.enable_tuck_if_available()
        self.assertTrue(result)
        self.assertTrue(ctx._switch.clicked)
        self.assertFalse(ctx._switch.force_clicked)
        sleep.assert_not_awaited()

    async def test_already_tucked_returns_true_without_clicking(self):
        ctx = _FakeCtx(["checked"])
        stub = _StubTuck(ctx)
        result = await stub.enable_tuck_if_available()
        self.assertTrue(result)
        self.assertFalse(ctx._switch.clicked)

    async def test_disabled_toggle_skips_without_clicking(self):
        ctx = _FakeCtx(["disabled"])
        stub = _StubTuck(ctx)
        result = await stub.enable_tuck_if_available()
        self.assertFalse(result)
        self.assertFalse(ctx._switch.clicked)

    async def test_missing_toggle_skips_without_clicking(self):
        ctx = _FakeCtx(["missing"])
        stub = _StubTuck(ctx)
        result = await stub.enable_tuck_if_available()
        self.assertFalse(result)
        self.assertFalse(ctx._switch.clicked)

    async def test_click_that_never_flips_caption_reports_failure(self):
        ctx = _FakeCtx(["ready", "ready", "ready", "ready", "ready", "ready"])
        stub = _StubTuck(ctx)
        with mock.patch("asyncio.sleep", new=mock.AsyncMock()):
            result = await stub.enable_tuck_if_available()
        self.assertFalse(result)
        self.assertTrue(ctx._switch.clicked)

    async def test_state_turning_disabled_after_click_stops_retrying(self):
        ctx = _FakeCtx(["ready", "disabled"])
        stub = _StubTuck(ctx)
        with mock.patch("asyncio.sleep", new=mock.AsyncMock()):
            result = await stub.enable_tuck_if_available()
        self.assertFalse(result)
        self.assertTrue(ctx._switch.clicked)

    async def test_missing_context_returns_false(self):
        stub = _StubTuck(None)

        async def no_ctx():
            return None

        stub.get_editor_context = no_ctx
        result = await stub.enable_tuck_if_available()
        self.assertFalse(result)


class ReadTuckStateTests(unittest.IsolatedAsyncioTestCase):
    """The state reader runs real JS; verify its source covers every
    documented DOM shape (container, switch, caption) without regressing."""

    async def test_reader_source_js_contains_real_world_selectors(self):
        import inspect

        src = inspect.getsource(BitmojiOutfitMixin._read_tuck_state)
        self.assertIn(".tuck-container", src)
        self.assertIn("tuck-toggle", src)
        self.assertIn("untucked", src)
        self.assertIn("tucked", src)
        self.assertIn('"missing"', src)
        self.assertIn('"disabled"', src)


if __name__ == "__main__":
    unittest.main()