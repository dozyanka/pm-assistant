"""Deployment/runtime hardening for Windows desktop and container modes."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pm_app.db import Store
from pm_app.ollama import OllamaClient, OllamaError
from pm_app.web import AppState, LocalServer


class DeploymentRuntimeTests(unittest.TestCase):
    def test_default_ollama_host_is_loopback(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PM_OLLAMA_HOST", None)
            self.assertEqual(OllamaClient().host, "127.0.0.1")

    def test_docker_ollama_hosts_are_explicitly_allowed(self):
        self.assertEqual(OllamaClient("host.docker.internal").host, "host.docker.internal")
        self.assertEqual(OllamaClient("ollama").host, "ollama")

    def test_arbitrary_ollama_host_is_rejected(self):
        with self.assertRaises(OllamaError):
            OllamaClient("example.com")
        with self.assertRaises(OllamaError):
            OllamaClient("10.0.0.5")

    def test_container_bind_requires_explicit_flag(self):
        with tempfile.TemporaryDirectory() as td:
            state = AppState(Path(__file__).resolve().parents[1], Path(td))
            with self.assertRaises(ValueError):
                LocalServer(("0.0.0.0", 0), state)

    def test_explicit_container_bind_is_allowed(self):
        with tempfile.TemporaryDirectory() as td:
            state = AppState(Path(__file__).resolve().parents[1], Path(td))
            server = LocalServer(("0.0.0.0", 0), state, allow_container_bind=True)
            try:
                self.assertGreater(server.server_port, 0)
            finally:
                server.server_close()


if __name__ == "__main__":
    unittest.main()
