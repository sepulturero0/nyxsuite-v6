"""Outfit selection must survive Bitmoji rotating an item id out of its catalog.

Configured outfit ids (e.g. footwear=969, bottom=788) periodically disappear
from Bitmoji's catalog; the exact-match scan then never finds them and the whole
profile used to fail ("scroll forever" in nyx_bot.log). _apply_outfit_piece now
prefers the configured item but, when it's genuinely gone, dresses the avatar
with ANOTHER item from the *same configured pool* (random per profile) so the
substitute is always operator-approved. Picking any random catalog item is an
opt-in last resort (NYX_OUTFIT_FALLBACK_CATALOG=1) used only when the whole pool
has rotated out.
"""
import unittest
from unittest import mock

from core.bitmoji import outfit_flow
from core.bitmoji.outfit_flow import BitmojiOutfitMixin


class _StubOutfit(BitmojiOutfitMixin):
    """Simulates the live editor: opening a category always works, and an item
    click succeeds unless that exact selector is in ``missing`` (or everything
    fails when ``all_fail``)."""

    def __init__(self, missing_selectors=(), all_fail=False):
        self.logger = None
        self.missing = set(missing_selectors)
        self.all_fail = all_fail
        self.clicked_items = []
        self.catalog_fallback_calls = []

    async def wait_if_paused(self):
        return None

    async def safe_click(self, selector_key, profile_id=None, retries=None):
        if str(selector_key).startswith("categories."):
            return True  # opening the category always works
        if self.all_fail or selector_key in self.missing:
            raise Exception(f"item not found: {selector_key}")
        self.clicked_items.append(selector_key)
        return True

    async def get_editor_context(self):
        return object()

    async def reset_editor_panel_scroll(self, ctx):
        return None

    async def wait_for_category_items(self, ctx=None, timeout=None):
        return None

    async def _click_any_item_in_open_category(self, category_key, param, profile_id, blocked_ids=None):
        self.catalog_fallback_calls.append((category_key, param, tuple(blocked_ids or ())))
        return True


class OutfitFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Skip the 0.6s inter-retry sleeps.
        self._sleep = mock.patch.object(outfit_flow.asyncio, "sleep", new=mock.AsyncMock())
        self._sleep.start()

    async def asyncTearDown(self):
        self._sleep.stop()

    async def test_pool_fallback_selects_another_pool_item(self):
        pool = ["top=chosen", "top=alt1", "top=alt2"]
        stub = _StubOutfit(missing_selectors={"top=chosen"})
        with mock.patch.object(outfit_flow, "_OUTFIT_ALLOW_FALLBACK", True):
            ok = await stub._apply_outfit_piece(
                "categories.tops", "top=chosen", "prof1",
                fallback_param="top", fallback_pool=pool,
            )
        self.assertTrue(ok)
        # It clicked an alternate from the pool, never the missing chosen item.
        self.assertTrue(stub.clicked_items)
        self.assertNotIn("top=chosen", stub.clicked_items)
        self.assertTrue(all(sel in {"top=alt1", "top=alt2"} for sel in stub.clicked_items))
        # And it never resorted to the any-catalog net.
        self.assertEqual(stub.catalog_fallback_calls, [])

    async def test_pool_fallback_is_deterministic_per_profile(self):
        pool = ["x=chosen", "x=a", "x=b", "x=c", "x=d"]
        picks = set()
        for _ in range(3):
            stub = _StubOutfit(missing_selectors={"x=chosen"})
            with mock.patch.object(outfit_flow, "_OUTFIT_ALLOW_FALLBACK", True):
                await stub._apply_outfit_piece(
                    "categories.tops", "x=chosen", "profX",
                    fallback_param="x", fallback_pool=pool,
                )
            picks.add(stub.clicked_items[0])
        self.assertEqual(len(picks), 1, "same profile must pick the same fallback item on reruns")

    async def test_pool_fallback_skips_blocked_ids(self):
        # Every non-blocked alternate is missing except top=930; top=924 is blocked
        # and must never be clicked even though it is present in the catalog.
        pool = ["top=chosen", "top=924", "top=930"]
        stub = _StubOutfit(missing_selectors={"top=chosen"})
        with mock.patch.object(outfit_flow, "_OUTFIT_ALLOW_FALLBACK", True):
            ok = await stub._apply_outfit_piece(
                "categories.tops", "top=chosen", "prof1",
                fallback_param="top", blocked_ids={"924"}, fallback_pool=pool,
            )
        self.assertTrue(ok)
        self.assertEqual(stub.clicked_items, ["top=930"])

    async def test_no_fallback_without_pool(self):
        stub = _StubOutfit(missing_selectors={"top=chosen"})
        with mock.patch.object(outfit_flow, "_OUTFIT_ALLOW_FALLBACK", True):
            with self.assertRaises(Exception):
                await stub._apply_outfit_piece("categories.tops", "top=chosen", "prof1", fallback_param="top")
        self.assertEqual(stub.catalog_fallback_calls, [])

    async def test_no_fallback_when_disabled(self):
        stub = _StubOutfit(missing_selectors={"bottom=chosen"})
        with mock.patch.object(outfit_flow, "_OUTFIT_ALLOW_FALLBACK", False):
            with self.assertRaises(Exception):
                await stub._apply_outfit_piece(
                    "categories.bottoms", "bottom=chosen", "prof1",
                    fallback_param="bottom", fallback_pool=["bottom=chosen", "bottom=alt"],
                )
        self.assertEqual(stub.clicked_items, [])

    async def test_exact_item_preferred_no_fallback(self):
        stub = _StubOutfit(missing_selectors=set())
        with mock.patch.object(outfit_flow, "_OUTFIT_ALLOW_FALLBACK", True):
            ok = await stub._apply_outfit_piece(
                "categories.tops", "top=chosen", "prof1",
                fallback_param="top", fallback_pool=["top=chosen", "top=alt"],
            )
        self.assertTrue(ok)
        self.assertEqual(stub.clicked_items, ["top=chosen"], "the exact item must be used when present")

    async def test_catalog_net_used_when_pool_exhausted_and_enabled(self):
        # Whole pool retired; the opt-in catalog net dresses the avatar so the
        # profile still completes.
        stub = _StubOutfit(all_fail=True)
        with mock.patch.object(outfit_flow, "_OUTFIT_ALLOW_FALLBACK", True), \
             mock.patch.object(outfit_flow, "_OUTFIT_ALLOW_CATALOG_FALLBACK", True):
            ok = await stub._apply_outfit_piece(
                "categories.tops", "top=chosen", "prof1",
                fallback_param="top", fallback_pool=["top=chosen", "top=alt"],
            )
        self.assertTrue(ok)
        self.assertEqual(len(stub.catalog_fallback_calls), 1)

    async def test_catalog_net_off_by_default(self):
        # Same fully-retired pool, but the catalog net is disabled (default) -> the
        # step fails rather than picking a random catalog item.
        stub = _StubOutfit(all_fail=True)
        with mock.patch.object(outfit_flow, "_OUTFIT_ALLOW_FALLBACK", True), \
             mock.patch.object(outfit_flow, "_OUTFIT_ALLOW_CATALOG_FALLBACK", False):
            with self.assertRaises(Exception):
                await stub._apply_outfit_piece(
                    "categories.tops", "top=chosen", "prof1",
                    fallback_param="top", fallback_pool=["top=chosen", "top=alt"],
                )
        self.assertEqual(stub.catalog_fallback_calls, [])


if __name__ == "__main__":
    unittest.main()
