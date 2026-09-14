"""Portable thin client for a private PM Assistant Tailscale Serve URL.

The remote client intentionally uses the installed system browser in application
mode instead of embedding pywebview/pythonnet. This avoids fragile .NET runtime
packaging on Windows while keeping the laptop package small and portable.
"""
from __future__ import annotations

import ctypes
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from urllib.parse import urlsplit

APP_VERSION = "1.0.0"
MUTEX_NAME = r"Local\PM_Assistant_Remote_Client_A79F0C11"
ERROR_ALREADY_EXISTS = 183


def app_dir() -> Path:
    return Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent


def config_path() -> Path:
    return app_dir() / "remote_client.json"


def client_profile_dir() -> Path:
    """Dedicated browser profile for PM Assistant Remote.

    It is intentionally stored under LOCALAPPDATA instead of next to the portable
    executable, so moving/replacing the Client folder does not copy session data.
    """
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
        return base / "PM Assistant Remote" / "ClientWebProfile"
    return Path.home() / ".local" / "share" / "pm-assistant-remote" / "client-web-profile"


def validate_server_url(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("Некорректный адрес сервера.")
    value = value.strip()
    parts = urlsplit(value)
    if parts.scheme.lower() != "https" or not parts.hostname:
        raise ValueError("Адрес должен начинаться с https:// и вести на Tailscale Serve.")
    host = parts.hostname.lower().rstrip(".")
    if not host.endswith(".ts.net"):
        raise ValueError("Разрешены только приватные Tailscale Serve адреса *.ts.net.")
    if parts.username or parts.password or parts.query or parts.fragment or parts.port not in (None, 443):
        raise ValueError("Адрес Tailscale Serve должен быть без логина, параметров и нестандартного порта.")
    if parts.path not in ("", "/"):
        raise ValueError("Используйте корневой адрес PM Assistant без дополнительного пути.")
    return f"https://{host}"


def save_url(value: str) -> Path:
    url = validate_server_url(value)
    target = config_path()
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps({"server_url": url}, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, target)
    return target


def load_url() -> str:
    try:
        data = json.loads(config_path().read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError("Сервер не настроен. Запустите configure_remote_client.cmd в этой папке.") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("Не удалось прочитать remote_client.json.") from exc
    return validate_server_url(data.get("server_url", ""))


def message_box(text: str, title: str = "PM Assistant Remote") -> None:
    if os.name == "nt":
        ctypes.windll.user32.MessageBoxW(None, text, title, 0x40)
    else:
        print(f"{title}: {text}", file=sys.stderr)


def acquire_mutex():
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


def release_mutex(handle) -> None:
    if os.name == "nt" and handle:
        ctypes.windll.kernel32.CloseHandle(handle)


def find_tailscale() -> Path | None:
    found = shutil.which("tailscale") or shutil.which("tailscale.exe")
    candidates = [found]
    if os.name == "nt":
        for env_name in ("ProgramFiles", "ProgramW6432", "LOCALAPPDATA"):
            base = os.environ.get(env_name)
            if base:
                candidates.append(str(Path(base) / "Tailscale" / "tailscale.exe"))
    for value in candidates:
        if value and Path(value).is_file():
            return Path(value)
    return None


def tailscale_running(ts: Path) -> bool:
    try:
        completed = subprocess.run([str(ts), "status", "--json"], capture_output=True,
                                   text=True, encoding="utf-8", errors="replace", timeout=12)
        if completed.returncode != 0:
            return False
        data = json.loads(completed.stdout)
        return isinstance(data, dict) and str(data.get("BackendState", "")).lower() == "running"
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return False


def server_reachable(url: str) -> bool:
    request = urllib.request.Request(url + "/api/auth/status", headers={"User-Agent": "PM-Assistant-Remote/" + APP_VERSION})
    try:
        with urllib.request.urlopen(request, timeout=12) as response:
            return response.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def find_app_browser() -> Path | None:
    """Prefer Microsoft Edge because it is present on supported Windows builds.

    Chrome is a fallback. Both support --app=<url>, which gives a chromeless app
    window without requiring pywebview/pythonnet inside our executable.
    """
    if os.name != "nt":
        return None
    candidates: list[Path] = []
    for env_name in ("ProgramFiles(x86)", "ProgramFiles", "ProgramW6432", "LOCALAPPDATA"):
        base = os.environ.get(env_name)
        if not base:
            continue
        root = Path(base)
        candidates.extend([
            root / "Microsoft" / "Edge" / "Application" / "msedge.exe",
            root / "Google" / "Chrome" / "Application" / "chrome.exe",
        ])
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate).casefold()
        if key in seen:
            continue
        seen.add(key)
        if candidate.is_file():
            return candidate
    return None


def app_browser_command(browser: Path, url: str, profile: Path | None = None) -> list[str]:
    url = validate_server_url(url)
    profile = profile or client_profile_dir()
    profile.mkdir(parents=True, exist_ok=True)
    return [
        str(browser),
        f"--app={url}",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-mode",
    ]


def run_window(url: str) -> None:
    """Open the validated private URL without embedding a .NET webview runtime."""
    browser = find_app_browser()
    if browser is not None:
        process = subprocess.Popen(app_browser_command(browser, url), shell=False)
        # Keep this launcher alive while the dedicated app-mode browser is alive.
        # Some Chromium builds delegate immediately to another process; in that
        # case wait() returns quickly, which is harmless.
        process.wait()
        return
    if not webbrowser.open(validate_server_url(url), new=1, autoraise=True):
        raise RuntimeError("Не удалось открыть Microsoft Edge/Chrome или системный браузер.")


def main() -> int:
    if len(sys.argv) >= 3 and sys.argv[1] == "--save-url":
        try:
            target = save_url(sys.argv[2])
        except ValueError as exc:
            message_box(str(exc), "PM Assistant Remote — ошибка")
            return 2
        message_box(f"Адрес сервера сохранён:\n{load_url()}\n\nФайл: {target}")
        return 0

    mutex = acquire_mutex()
    if mutex is None:
        message_box("PM Assistant Remote уже запущен.")
        return 2
    try:
        try:
            url = load_url()
        except ValueError as exc:
            message_box(str(exc), "PM Assistant Remote — настройка")
            return 2
        ts = find_tailscale()
        if ts is None:
            message_box("Tailscale не найден. Установите Tailscale на ноутбук и войдите в тот же аккаунт, что на серверном ПК.")
            return 3
        if not tailscale_running(ts):
            message_box("Tailscale сейчас не подключён. Откройте Tailscale и дождитесь состояния Connected.")
            return 4
        if not server_reachable(url):
            message_box("Сервер PM Assistant недоступен. Проверьте, что основной ПК включён, PM Assistant Remote Server запущен и Tailscale Serve активен.")
            return 5
        run_window(url)
        return 0
    except Exception as exc:
        message_box(f"Не удалось открыть PM Assistant Remote.\n\n{exc}", "PM Assistant Remote — ошибка")
        return 1
    finally:
        release_mutex(mutex)


if __name__ == "__main__":
    raise SystemExit(main())
