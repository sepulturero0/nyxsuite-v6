"""Popup first-paint: the Nyx popup must render the last-known status
immediately on open, before the live refresh lands, so controls are usable as
soon as the bridge is reachable instead of waiting for the first poll.
"""
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class PopupFirstPaintSourceTests(unittest.TestCase):
    def test_popup_renders_stored_status_on_open(self):
        popup = (ROOT / "nyx_extension" / "popup.js").read_text(encoding="utf-8")

        # A last-known runner status is persisted after each refresh and painted
        # synchronously on popup open (before the live refresh overwrites it).
        self.assertIn('const LAST_RUNNER_STATUS_KEY = "nyxLastRunnerStatus";', popup)
        self.assertIn("function persistLastRunnerStatus", popup)
        self.assertIn("function renderStoredRunnerStatus", popup)
        self.assertIn("renderRunnerStatus(entry.runnerStatus)", popup)
        self.assertIn("renderStoredRunnerStatus();", popup)  # called from init
        self.assertIn("persistLastRunnerStatus(runnerStatus);", popup)

    def test_popup_status_cache_does_not_write_on_every_live_update(self):
        popup = (ROOT / "nyx_extension" / "popup.js").read_text(encoding="utf-8")

        self.assertIn("let lastPersistedRunnerStatusSignature = \"\";", popup)
        self.assertIn("function compactRunnerStatus", popup)
        self.assertIn("if (signature === lastPersistedRunnerStatusSignature) return;", popup)


if __name__ == "__main__":
    unittest.main()
