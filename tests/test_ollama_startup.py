"""Ollama startup and remote-mode readiness checks."""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from pm_app.ollama import CHAT_MODEL, EMBED_MODEL, OllamaError
from pm_app.web import AppState
from remote_server import check_ollama

ROOT = Path(__file__).resolve().parents[1]


class OllamaStartupTests(unittest.TestCase):
    def _wait_job(self, state: AppState, jid: str) -> dict:
        for _ in range(100):
            with state.job_lock:
                job = dict(state.jobs[jid])
            if job["state"] != "running":
                return job
            time.sleep(0.01)
        self.fail("job did not finish")

    def test_missing_model_error_is_actionable_but_safe(self):
        with tempfile.TemporaryDirectory() as t:
            state = AppState(ROOT, Path(t))
            jid = state.start_job(lambda progress: (_ for _ in ()).throw(
                OllamaError("Required local model is missing: qwen3.5:9b. No automatic download is performed.")))
            job = self._wait_job(state, jid)
            self.assertEqual(job["state"], "error")
            self.assertIn("qwen3.5:9b", job["error"])
            self.assertIn("ollama pull qwen3.5:9b", job["error"])

    def test_arbitrary_low_level_ollama_error_stays_hidden(self):
        with tempfile.TemporaryDirectory() as t:
            state = AppState(ROOT, Path(t))
            jid = state.start_job(lambda progress: (_ for _ in ()).throw(
                OllamaError("PRIVATE MODEL OUTPUT SHOULD NOT LEAK")))
            job = self._wait_job(state, jid)
            self.assertEqual(job["state"], "error")
            self.assertNotIn("PRIVATE MODEL OUTPUT", job["error"])
            self.assertIn("PM Assistant Remote Server", job["error"])

    def test_remote_check_distinguishes_unavailable_missing_and_ready(self):
        with patch("remote_server.OllamaClient.models", side_effect=OllamaError(
                "Local Ollama request failed. Keep start_ollama.cmd running; see its console.")):
            self.assertEqual(check_ollama(), 20)
        with patch("remote_server.OllamaClient.models", side_effect=OllamaError(
                "Required local model is missing: qwen3.5:9b. No automatic download is performed.")):
            self.assertEqual(check_ollama(), 21)
        with patch("remote_server.OllamaClient.models", return_value={CHAT_MODEL: "abc", EMBED_MODEL: "def"}):
            self.assertEqual(check_ollama(), 0)

    def test_launchers_use_local_system_ollama_and_required_models(self):
        scripts = ROOT / "scripts" / "windows"
        start_ollama = (scripts / "start_ollama.cmd").read_text(encoding="utf-8")
        start_remote = (scripts / "start_remote_server.cmd").read_text(encoding="utf-8")
        restart_remote = (scripts / "restart_remote_ollama.cmd").read_text(encoding="utf-8")
        build_remote = (scripts / "build_remote.cmd").read_text(encoding="utf-8-sig")
        self.assertIn('set "OLLAMA_HOST=127.0.0.1:11434"', start_ollama)
        self.assertIn('set "OLLAMA_NO_CLOUD=1"', start_ollama)
        self.assertIn("qwen3.5:9b", start_ollama)
        self.assertIn("qwen3-embedding:0.6b", start_ollama)
        self.assertIn("--check-ollama", start_remote)
        self.assertIn("start_ollama.cmd", start_remote)
        self.assertIn("taskkill /F /IM ollama.exe", restart_remote)
        self.assertIn("restart_remote_ollama.cmd", build_remote)


if __name__ == "__main__":
    unittest.main()
