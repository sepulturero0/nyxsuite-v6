import unittest
from unittest import mock


class SharedOutfitConfigTests(unittest.TestCase):
    def _catalog(self):
        return {
            "features": {
                "tops": {
                    "type": "outfit",
                    "options": [
                        {"id": "801", "colors_verified": True, "colors": ["#111111", "#222222"]},
                    ],
                },
                "bottoms": {
                    "type": "outfit",
                    "options": [
                        {"id": "356", "colors_verified": True, "colors": ["#333333"]},
                    ],
                },
                "footwear": {
                    "type": "outfit",
                    "options": [
                        {"id": "292", "colors_verified": True, "colors": ["#444444", "#555555"]},
                    ],
                },
            }
        }

    def test_sanitize_custom_preset_removes_outfits_and_keeps_per_item_colors(self):
        from core.bitmoji_outfit_config import sanitize_outfit_config

        raw = {
            "active_custom_preset_id": "night",
            "custom_presets": {
                "night": {
                    "name": "Night Fit",
                    "features": {
                        "outfits": {"mode": "random", "pool": ["999"]},
                        "tops": {
                            "mode": "random",
                            "pool": ["801"],
                            "colors_by_option": {
                                "801": ["#111111", "#ff0000", ""],
                                "missing": ["#222222"],
                            },
                        },
                    },
                },
            },
        }

        sanitized = sanitize_outfit_config(raw, self._catalog())

        preset = sanitized["custom_presets"]["night"]
        self.assertNotIn("outfits", preset["features"])
        self.assertEqual(preset["name"], "Night Fit")
        self.assertEqual(preset["features"]["tops"]["pool"], ["801"])
        self.assertEqual(preset["features"]["tops"]["colors_by_option"], {"801": ["#111111"]})

    def test_sanitize_preserves_tuck_in_defaulting_to_true(self):
        from core.bitmoji_outfit_config import sanitize_outfit_config

        raw = {
            "active_custom_preset_id": "a",
            "custom_presets": {
                "a": {
                    "name": "Tuck On",
                    "tuck_in": True,
                    "features": {"tops": {"mode": "random", "pool": ["801"]}},
                },
                "b": {
                    "name": "Tuck Off",
                    "tuck_in": False,
                    "features": {"tops": {"mode": "random", "pool": ["801"]}},
                },
                "c": {
                    "name": "Unset",
                    "features": {"tops": {"mode": "random", "pool": ["801"]}},
                },
            },
        }

        sanitized = sanitize_outfit_config(raw, self._catalog())
        presets = sanitized["custom_presets"]
        self.assertIs(presets["a"]["tuck_in"], True)
        self.assertIs(presets["b"]["tuck_in"], False)
        self.assertIs(presets["c"]["tuck_in"], True)

    def test_custom_outfit_generation_carries_tuck_in(self):
        from core.outfit_generator import generate_outfit

        outfit_config = {
            "active_custom_preset_id": "loose",
            "custom_presets": {
                "loose": {
                    "name": "Loose Fit",
                    "tuck_in": False,
                    "features": {
                        "tops": {"mode": "random", "pool": ["801"]},
                        "bottoms": {"mode": "random", "pool": ["356"]},
                        "footwear": {"mode": "random", "pool": ["292"]},
                    },
                },
            },
        }

        with mock.patch("core.outfit_generator.load_nyx_config", return_value={
            "outfit_style": "custom",
            "outfit_custom_preset_id": "loose",
        }), mock.patch("core.outfit_generator.load_outfit_config", return_value=outfit_config):
            outfit = generate_outfit("profile-x", model="Clea", outfit_seed="seed-loose")

        self.assertIs(outfit["tuck_in"], False)

    def test_custom_outfit_generation_uses_exact_selectors_and_item_color_pool(self):
        from core.outfit_generator import generate_outfit

        outfit_config = {
            "active_custom_preset_id": "night",
            "custom_presets": {
                "night": {
                    "name": "Night Fit",
                    "features": {
                        "tops": {
                            "mode": "random",
                            "pool": ["801"],
                            "colors_by_option": {"801": ["#111111", "#222222"]},
                        },
                        "bottoms": {
                            "mode": "random",
                            "pool": ["356"],
                            "colors_by_option": {"356": ["#333333"]},
                        },
                        "footwear": {
                            "mode": "random",
                            "pool": ["292"],
                            "colors_by_option": {"292": ["#444444"]},
                        },
                    },
                },
            },
        }

        with mock.patch("core.outfit_generator.load_nyx_config", return_value={
            "outfit_style": "custom",
            "outfit_custom_preset_id": "night",
        }), mock.patch("core.outfit_generator.load_outfit_config", return_value=outfit_config):
            outfit = generate_outfit("profile-a", model="Clea", outfit_seed="seed-a")

        self.assertEqual(outfit["mode"], "separates")
        self.assertIn("concat('&',substring-after(@src,'?'),'&')", outfit["top"]["selector"])
        self.assertIn("&top=", outfit["top"]["selector"])
        self.assertIn("801", outfit["top"]["selector"])
        self.assertEqual(outfit["top"]["preferred_color"]["hex"], "#222222")
        self.assertEqual(outfit["top"]["preferred_color"]["source"], "shared_outfit")
        self.assertEqual(outfit["bottom"]["preferred_color"]["hex"], "#333333")
        self.assertEqual(outfit["shoes"]["preferred_color"]["hex"], "#444444")
        self.assertEqual(outfit["top_pool"], [outfit["top"]])

    def test_custom_outfit_generation_randomizes_per_profile_from_shared_pool(self):
        from core.outfit_generator import generate_outfit

        outfit_config = {
            "active_custom_preset_id": "varied",
            "custom_presets": {
                "varied": {
                    "name": "Varied",
                    "features": {
                        "tops": {"mode": "random", "pool": ["801", "802"]},
                        "bottoms": {"mode": "random", "pool": ["356"]},
                        "footwear": {"mode": "random", "pool": ["292"]},
                    },
                },
            },
        }

        with mock.patch("core.outfit_generator.load_nyx_config", return_value={
            "outfit_style": "custom",
            "outfit_custom_preset_id": "varied",
        }), mock.patch("core.outfit_generator.load_outfit_config", return_value=outfit_config):
            first = generate_outfit("profile-a", model="Clea", outfit_seed="seed1")
            second = generate_outfit("profile-b", model="Clea", outfit_seed="seed2")

        self.assertNotEqual(first["top"]["selector"], second["top"]["selector"])


    def test_preserve_keeps_builtin_presets_and_stale_ids(self):
        from core.bitmoji_outfit_config import preserve_outfit_config

        raw = {
            "active_custom_preset_id": "night",
            "custom_presets": {
                "night": {
                    "name": "Night Fit",
                    "features": {
                        "tops": {
                            "mode": "random",
                            "pool": ["999999"],
                            "colors_by_option": {"999999": ["#abcdef"]},
                        },
                    },
                },
                "builtin_mix": {
                    "name": "Mix",
                    "features": {"tops": {"mode": "random", "pool": ["801"]}},
                },
            },
        }

        preserved = preserve_outfit_config(raw)

        # Config preservation: builtin presets survive, and custom garment ids /
        # colours that the (stale) live catalog no longer knows are kept verbatim.
        self.assertIn("builtin_mix", preserved["custom_presets"])
        night = preserved["custom_presets"]["night"]
        self.assertIn("999999", night["features"]["tops"]["pool"])
        self.assertEqual(
            night["features"]["tops"]["colors_by_option"]["999999"], ["#abcdef"]
        )
        self.assertEqual(preserved["active_custom_preset_id"], "night")

    def test_generate_prefers_active_preset_over_stale_runtime_id(self):
        from core.outfit_generator import generate_outfit

        outfit_config = {
            "active_custom_preset_id": "night",
            "custom_presets": {
                "night": {
                    "id": "night",
                    "name": "Night Fit",
                    "features": {
                        "tops": {"mode": "random", "pool": ["801"]},
                        "bottoms": {"mode": "random", "pool": ["356"]},
                        "footwear": {"mode": "random", "pool": ["292"]},
                    },
                },
            },
        }

        # The runtime config carries a stale preset id, but the editor's active
        # preset must win so the runner uses what the dashboard shows.
        with mock.patch("core.outfit_generator.load_nyx_config", return_value={
            "outfit_style": "custom",
            "outfit_custom_preset_id": "stale_missing_preset",
        }), mock.patch("core.outfit_generator.load_outfit_config", return_value=outfit_config):
            outfit = generate_outfit("profile-x", model="Clea", outfit_seed="seed-x")

        self.assertEqual(outfit["preset_id"], "night")
        self.assertEqual(outfit["preset_name"], "Night Fit")


if __name__ == "__main__":
    unittest.main()
