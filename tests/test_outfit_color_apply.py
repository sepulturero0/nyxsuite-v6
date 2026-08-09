"""End-to-end outfit colour apply (``pick_configured_color_option``).

The operator's per-model colour choice (fixed, or random-from-pool) must now be
applied to the created avatar by clicking the matching swatch in the live colour
wheel — while staying fully backward compatible: when nothing is configured, or
the swatch can't be matched, it falls back to the existing random colour pick and
never raises (colour is cosmetic and must not fail a profile).
"""
import unittest
from unittest import mock

from core.bitmoji.outfit_flow import BitmojiOutfitMixin


class _FakeLocator:
    def __init__(self, ok=True):
        self._ok = ok

    @property
    def first(self):
        return self

    async def wait_for(self, state=None, timeout=None):
        if not self._ok:
            raise Exception("picker not visible")
        return None


class _FakeCtx:
    def __init__(self, clicked=True, picker_visible=True):
        self._clicked = clicked
        self._picker_visible = picker_visible
        self.evaluated = []

    def locator(self, selector):
        return _FakeLocator(self._picker_visible)

    async def evaluate(self, js, arg=None):
        self.evaluated.append(arg)
        return self._clicked


class _StubColor(BitmojiOutfitMixin):
    def __init__(self, ctx):
        self.logger = None
        self._ctx = ctx
        self.random_called = False
        self.random_args = None

    async def wait_if_paused(self):
        return None

    async def get_editor_context(self):
        return self._ctx

    async def human_delay(self, *a, **k):
        return None

    async def pick_random_color_option(self, profile_id, outfit_seed="", preferred_color=None,
                                       active_panel_only=False, ctx=None):
        self.random_called = True
        self.random_args = (profile_id, outfit_seed, preferred_color)
        return "RANDOM"


class PickConfiguredColorTests(unittest.IsolatedAsyncioTestCase):
    async def test_configured_color_clicks_swatch(self):
        ctx = _FakeCtx(clicked=True)
        stub = _StubColor(ctx)
        with mock.patch("core.bitmoji_config.load_models", return_value={}), \
             mock.patch("core.bitmoji_config.resolve_option_color", return_value="#ec2020"):
            result = await stub.pick_configured_color_option("p1", "M", ("tops",))
        self.assertTrue(result)
        self.assertFalse(stub.random_called)
        self.assertEqual(ctx.evaluated, [None, "#ec2020"])

    async def test_swatch_not_matched_falls_back_to_random(self):
        ctx = _FakeCtx(clicked=False)
        stub = _StubColor(ctx)
        with mock.patch("core.bitmoji_config.load_models", return_value={}), \
             mock.patch("core.bitmoji_config.resolve_option_color", return_value="#ec2020"):
            result = await stub.pick_configured_color_option("p1", "M", ("tops",), "seed", preferred_color={"x": 1})
        self.assertEqual(result, "RANDOM")
        self.assertTrue(stub.random_called)
        # preferred_color/seed forwarded to the fallback so legacy behaviour is intact
        self.assertEqual(stub.random_args, ("p1", "seed", {"x": 1}))

    async def test_no_config_uses_random_without_touching_picker(self):
        ctx = _FakeCtx(clicked=True)
        stub = _StubColor(ctx)
        with mock.patch("core.bitmoji_config.load_models", return_value={}), \
             mock.patch("core.bitmoji_config.resolve_option_color", return_value=None):
            result = await stub.pick_configured_color_option("p1", "M", ("tops", "outfits"))
        self.assertEqual(result, "RANDOM")
        self.assertTrue(stub.random_called)
        self.assertEqual(ctx.evaluated, [])  # never opened the colour wheel

    async def test_string_feature_is_accepted(self):
        ctx = _FakeCtx(clicked=True)
        stub = _StubColor(ctx)
        with mock.patch("core.bitmoji_config.load_models", return_value={}), \
             mock.patch("core.bitmoji_config.resolve_option_color", return_value="#010203"):
            result = await stub.pick_configured_color_option("p1", "M", "footwear")
        self.assertTrue(result)
        self.assertEqual(ctx.evaluated, [None, "#010203"])

    async def test_tries_each_feature_until_a_colour_resolves(self):
        ctx = _FakeCtx(clicked=True)
        stub = _StubColor(ctx)

        def resolve(model, feature, models):
            return "#123456" if feature == "outfits" else None

        with mock.patch("core.bitmoji_config.load_models", return_value={}), \
             mock.patch("core.bitmoji_config.resolve_option_color", side_effect=resolve):
            result = await stub.pick_configured_color_option("p1", "M", ("tops", "outfits"))
        self.assertTrue(result)
        self.assertEqual(ctx.evaluated, [None, "#123456"])


if __name__ == "__main__":
    unittest.main()
