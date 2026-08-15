"""Catalog normalization + colour resolution for the Nyxmoji editor.

Covers the corrections after the live-scan teardown:
  * the catalog is item-faithful — labels and per-option colours are taken
    from each garment record only and are never inferred from Tops;
  * normalization is idempotent and defensive against missing features;
  * ``resolve_option_color`` returns the operator's configured colour (fixed, or
    a random pick from the pool) that the bot now applies.
"""
import unittest

from core.bitmoji_config import (
    RENDER_PARAMS,
    _normalize_catalog,
    load_catalog_raw,
    render_param_map,
    resolve_option_color,
    sanitize_models,
)


class NormalizeCatalogTests(unittest.TestCase):
    def _fake(self):
        return {
            "features": {
                "outfits": {
                    "label": "Outfits", "type": "outfit",
                    "options": [{"id": "1", "colors": []}, {"id": "2", "colors": []}],
                },
                "tops": {
                    "label": "Tops", "type": "outfit",
                    "options": [{"id": "1", "colors": ["#aaaaaa", "#bbbbbb"]},
                                {"id": "2", "colors": []}],
                },
            }
        }

    def test_keeps_item_faithful_labels_and_colors(self):
        out = _normalize_catalog(self._fake())
        self.assertEqual(out["features"]["outfits"]["label"], "Outfits")
        self.assertEqual(out["features"]["outfits"]["options"][0]["colors"], [])
        # Tops owns its own colours; nothing is inferred from the other slot.
        self.assertEqual(out["features"]["tops"]["options"][0]["colors"], ["#aaaaaa", "#bbbbbb"])

    def test_idempotent(self):
        once = _normalize_catalog(self._fake())
        twice = _normalize_catalog(once)
        self.assertEqual(twice["features"]["outfits"]["label"], "Outfits")
        self.assertEqual(twice["features"]["outfits"]["options"][0]["colors"], [])

    def test_handles_missing_features(self):
        self.assertEqual(_normalize_catalog({}), {})
        self.assertEqual(_normalize_catalog({"features": {}}), {"features": {}})

    def test_real_catalog_outfits_keeps_own_label(self):
        raw = load_catalog_raw()
        outfits = raw.get("features", {}).get("outfits")
        if not outfits:  # catalog not present in this environment
            self.skipTest("bitmoji_catalog.json has no outfits feature")
        self.assertEqual(outfits.get("label", ""), "Outfits")


class RenderParamTests(unittest.TestCase):
    def test_outfits_preview_param_is_top(self):
        self.assertEqual(RENDER_PARAMS["outfits"], ("top", False))
        self.assertEqual(RENDER_PARAMS["tops"], ("top", False))

    def test_render_param_map_shape(self):
        m = render_param_map()
        self.assertEqual(m["outfits"], {"param": "top", "color": False})


class ResolveOptionColorTests(unittest.TestCase):
    def test_sanitize_models_drops_legacy_per_model_outfit_features(self):
        models = {
            "M": {
                "hair_style": {"mode": "fixed", "id": "12"},
                "tops": {"mode": "random", "pool": ["801"], "colors": ["#111111"]},
                "outfits": {"mode": "fixed", "id": "999"},
                "outerwear": {"mode": "fixed", "id": "10000466"},
                "sock": {"mode": "fixed", "id": "5"},
                "footwear": {"mode": "random", "pool": ["292"]},
            },
        }

        sanitized = sanitize_models(models, {"features": {}})

        self.assertEqual(
            sanitized, {"M": {"hair_style": {"mode": "fixed", "id": "12"}}}
        )

    def test_fixed_returns_configured_color(self):
        models = {"M": {"tops": {"mode": "fixed", "id": "5", "color": "#ec2020"}}}
        self.assertEqual(resolve_option_color("M", "tops", models), "#ec2020")

    def test_fixed_without_color_returns_none(self):
        models = {"M": {"tops": {"mode": "fixed", "id": "5"}}}
        self.assertIsNone(resolve_option_color("M", "tops", models))

    def test_random_picks_from_pool(self):
        models = {"M": {"tops": {"mode": "random", "pool": ["1"], "colors": ["#111111", "#222222"]}}}
        for _ in range(30):
            self.assertIn(resolve_option_color("M", "tops", models), ["#111111", "#222222"])

    def test_random_without_colors_returns_none(self):
        models = {"M": {"tops": {"mode": "random", "pool": ["1", "2"]}}}
        self.assertIsNone(resolve_option_color("M", "tops", models))

    def test_unconfigured_returns_none(self):
        models = {"M": {"tops": {"mode": "random", "pool": ["1"], "colors": ["#111111"]}}}
        self.assertIsNone(resolve_option_color("M", "bottoms", models))
        self.assertIsNone(resolve_option_color("OTHER", "tops", models))


if __name__ == "__main__":
    unittest.main()
