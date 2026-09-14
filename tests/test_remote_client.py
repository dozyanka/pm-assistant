"""Remote client launches a browser app window without pythonnet/pywebview."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import remote_client


class RemoteClientTests(unittest.TestCase):
    def test_browser_command_uses_only_validated_tailscale_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            cmd = remote_client.app_browser_command(
                Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
                "https://pc.tail-name.ts.net/",
                Path(tmp) / "profile",
            )
            self.assertEqual(cmd[1], "--app=https://pc.tail-name.ts.net")
            self.assertTrue(any(x.startswith("--user-data-dir=") for x in cmd))
            self.assertIn("--disable-background-mode", cmd)

    def test_run_window_uses_edge_app_mode_without_shell(self):
        process = MagicMock()
        browser = Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe")
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(remote_client, "find_app_browser", return_value=browser), \
             patch.object(remote_client, "client_profile_dir", return_value=Path(tmp) / "profile"), \
             patch.object(remote_client.subprocess, "Popen", return_value=process) as popen:
            remote_client.run_window("https://pc.tail-name.ts.net")
        args, kwargs = popen.call_args
        self.assertEqual(args[0][0], str(browser))
        self.assertIn("--app=https://pc.tail-name.ts.net", args[0])
        self.assertIs(kwargs.get("shell"), False)
        process.wait.assert_called_once_with()

    def test_run_window_falls_back_to_system_browser(self):
        with patch.object(remote_client, "find_app_browser", return_value=None), \
             patch.object(remote_client.webbrowser, "open", return_value=True) as opened:
            remote_client.run_window("https://pc.tail-name.ts.net")
        opened.assert_called_once_with("https://pc.tail-name.ts.net", new=1, autoraise=True)

    def test_client_and_spec_do_not_embed_pythonnet_webview(self):
        root = Path(__file__).resolve().parents[1]
        source = (root / "remote_client.py").read_text(encoding="utf-8")
        spec = (root / "packaging" / "windows" / "PM_Assistant_Remote_Client.spec").read_text(encoding="utf-8")
        self.assertNotIn("import webview", source)
        self.assertNotIn("collect_all(\"webview\")", spec)
        self.assertIn('excludes=["webview", "clr", "pythonnet", "Python.Runtime"]', spec)


if __name__ == "__main__":
    unittest.main()
