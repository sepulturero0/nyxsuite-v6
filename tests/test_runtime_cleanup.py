import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core import process_utils


class RuntimeCleanupTests(unittest.TestCase):
    def test_clear_runtime_logs_and_cache_truncates_logs_and_removes_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            logs = root / "logs"
            cache = root / "cache"
            logs.mkdir()
            cache.mkdir()
            (logs / "nyx_bot.log").write_text("old log\n", encoding="utf-8")
            (logs / "bot.pid").write_text("1234\n", encoding="utf-8")
            (cache / "snapshot.json").write_text("{}", encoding="utf-8")
            (cache / "nested").mkdir()
            (cache / "nested" / "item").write_text("x", encoding="utf-8")

            with mock.patch.object(process_utils, "APP_DATA_DIR", root), \
                 mock.patch.object(process_utils, "LOGS_DIR", logs), \
                 mock.patch.object(process_utils, "CACHE_DIR", cache):
                result = process_utils.clear_runtime_logs_and_cache()

            self.assertEqual((logs / "nyx_bot.log").read_text(encoding="utf-8"), "")
            self.assertEqual((logs / "bot.pid").read_text(encoding="utf-8"), "1234\n")
            self.assertEqual(list(cache.iterdir()), [])
            self.assertEqual(result["logs_truncated"], 1)
            self.assertEqual(result["cache_removed"], 2)


class DashboardSettingsSourceTests(unittest.TestCase):
    def test_settings_exposes_cleanup_and_nyxify_is_default_sidebar_tab(self):
        root = Path(__file__).resolve().parents[1]
        html = (root / "webui" / "index.html").read_text(encoding="utf-8")
        script = (root / "webui" / "dashboard.js").read_text(encoding="utf-8")

        self.assertIn('id="clear-cache-logs-btn"', html)
        self.assertIn('callBridge("clear_cache_logs")', script)
        self.assertNotIn('data-tab="suite"', html)
        self.assertIn('data-tab="nyxify" title="Nyxify', html)
        self.assertIn('let active = "nyxify";', script)


if __name__ == "__main__":
    unittest.main()
