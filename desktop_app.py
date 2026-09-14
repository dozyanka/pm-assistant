"""Native Windows launcher for PM Assistant.

The packaged desktop app keeps user data outside the installation directory and
runs the existing loopback HTTP application inside a WebView2 window.
"""
from __future__ import annotations

import ctypes
import os
import sys
import threading
import time
import webbrowser
from pathlib import Path

from pm_app import __version__
from pm_app.web import AppState, LocalServer

MUTEX_NAME = r"Local\PM_Assistant_Desktop_8F21F88B"
ERROR_ALREADY_EXISTS = 183


def resource_root() -> Path:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent


def user_data_dir() -> Path:
    override = os.environ.get("PM_DATA_DIR")
    if override:
        path = Path(override).expanduser()
    elif os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
        path = base / "PM Assistant" / "data"
    else:
        path = Path.home() / ".local" / "share" / "pm-assistant"
    path.mkdir(parents=True, exist_ok=True)
    return path


def acquire_windows_mutex():
    if os.name != "nt":
        return object()
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if not handle:
        return None
    if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        return None
    return handle


def release_windows_mutex(handle) -> None:
    if os.name == "nt" and handle:
        ctypes.windll.kernel32.CloseHandle(handle)


def message_box(text: str, title: str = "PM Assistant") -> None:
    if os.name == "nt":
        ctypes.windll.user32.MessageBoxW(None, text, title, 0x40)
    else:
        print(f"{title}: {text}", file=sys.stderr)


def run_server():
    root = resource_root()
    state = AppState(root, user_data_dir())
    server = LocalServer(("127.0.0.1", 0), state)
    thread = threading.Thread(target=server.serve_forever, name="pm-assistant-http", daemon=True)
    thread.start()
    return server, thread


def wait_until_ready(server: LocalServer, timeout: float = 5.0) -> str:
    # The server is already bound before the worker thread starts; a short wait is
    # enough to avoid a blank first navigation on slower Windows machines.
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if server.server_port:
            return f"http://127.0.0.1:{server.server_port}"
        time.sleep(0.05)
    raise RuntimeError("PM Assistant local server did not start in time.")


def run_native_window(url: str) -> None:
    try:
        import webview  # type: ignore
    except ImportError:
        webbrowser.open(url)
        message_box("Desktop WebView is not installed. PM Assistant was opened in the default browser.")
        input("Press Enter to stop PM Assistant... ")
        return

    webview.create_window(
        f"PM Assistant {__version__}",
        url,
        width=1440,
        height=900,
        min_size=(980, 680),
        resizable=True,
    )
    webview.start(debug=False)


def main() -> int:
    mutex = acquire_windows_mutex()
    if mutex is None:
        message_box("PM Assistant уже запущен.")
        return 2

    server = None
    thread = None
    try:
        server, thread = run_server()
        url = wait_until_ready(server)
        run_native_window(url)
        return 0
    except Exception as exc:
        message_box(f"Не удалось запустить PM Assistant.\n\n{exc}", "PM Assistant — ошибка запуска")
        return 1
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None:
            thread.join(timeout=5)
        release_windows_mutex(mutex)


if __name__ == "__main__":
    raise SystemExit(main())
