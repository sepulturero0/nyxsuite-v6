import plistlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock


class AgentHostMacOSLaunchdTests(unittest.TestCase):
    def test_start_agent_uses_launchd_on_macos(self):
        from agent_host import host_main

        launch_result = {"ok": True, "message": "Agent started via launchd."}

        with mock.patch.object(host_main.sys, "platform", "darwin"), \
             mock.patch.object(host_main, "_start_agent_via_launchd", return_value=launch_result) as launchd_start, \
             mock.patch.object(host_main.subprocess, "Popen") as popen:
            result = host_main._start_agent()

        self.assertEqual(result, launch_result)
        launchd_start.assert_called_once()
        popen.assert_not_called()

    def test_launchd_plist_preserves_command_and_environment(self):
        from agent_host import host_main

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "com.nyxsuite.bridge.plist"
            env = {
                "NYXSUITE_NO_OPEN": "1",
                "NYXSUITE_NO_TRAY": "",
                "IGNORED_NONE": None,
            }

            host_main._write_macos_launchd_agent(
                path=path,
                cmd=["/venv/bin/python", "/app/bridge_app.py"],
                cwd=Path("/app"),
                env=env,
            )

            data = plistlib.loads(path.read_bytes())

        self.assertEqual(data["Label"], "com.nyxsuite.bridge")
        self.assertEqual(data["ProgramArguments"], ["/venv/bin/python", "/app/bridge_app.py"])
        self.assertEqual(data["WorkingDirectory"], "/app")
        self.assertEqual(data["EnvironmentVariables"]["NYXSUITE_NO_OPEN"], "1")
        self.assertNotIn("NYXSUITE_NO_TRAY", data["EnvironmentVariables"])
        self.assertNotIn("IGNORED_NONE", data["EnvironmentVariables"])

    def test_start_via_launchd_bootstraps_without_kickstart(self):
        from agent_host import host_main

        plist_path = Path("/plist/com.nyxsuite.bridge.plist")
        ok_result = mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch.object(host_main, "_write_macos_launchd_agent", return_value=plist_path), \
             mock.patch.object(host_main, "_run_launchctl", return_value=ok_result) as run_launchctl, \
             mock.patch.object(host_main.os, "getuid", return_value=501):
            result = host_main._start_agent_via_launchd(
                cmd=["/venv/bin/python", "/app/bridge_app.py"],
                env={"NYXSUITE_NO_OPEN": "1"},
                root=Path("/app"),
            )

        self.assertEqual(result, {"ok": True, "message": "Agent started via launchd."})
        calls = [call.args[0] for call in run_launchctl.call_args_list]
        self.assertIn(["bootout", "gui/501/com.nyxsuite.bridge"], calls)
        self.assertIn(["bootstrap", "gui/501", str(plist_path)], calls)
        for args in calls:
            self.assertNotIn("kickstart", args)


if __name__ == "__main__":
    unittest.main()
