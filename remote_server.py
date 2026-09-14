"""Secure PM Assistant server for Tailscale Serve.

The HTTP application is intentionally bound only to 127.0.0.1:8765. Tailscale
Serve is the only supported remote ingress. The backend independently verifies
Tailscale identity headers for the configured owner before serving any page or
API response remotely.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

from pm_app import __version__
from pm_app.ollama import OllamaClient, OllamaError
from pm_app.remote import (RemoteAccessConfig, load_remote_config, remote_config_path,
                           remote_data_dir, save_remote_config)
from pm_app.web import AppState, LocalServer

MUTEX_NAME = r"Local\PM_Assistant_Remote_Server_3D5C2E8F"
ERROR_ALREADY_EXISTS = 183


def resource_root() -> Path:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent


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


def tailscale_status(ts: Path) -> dict:
    try:
        completed = subprocess.run([str(ts), "status", "--json"], capture_output=True,
                                   text=True, encoding="utf-8", errors="replace", timeout=20)
    except OSError as exc:
        raise ValueError("Не удалось запустить Tailscale CLI.") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise ValueError("Tailscale не подключён. Откройте Tailscale и войдите в свой аккаунт. " + detail[:300])
    try:
        data = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError("Tailscale вернул нечитаемый статус.") from exc
    if not isinstance(data, dict):
        raise ValueError("Tailscale вернул некорректный статус.")
    return data


def config_from_status(data: dict) -> RemoteAccessConfig:
    self_node = data.get("Self") or {}
    dns = str(self_node.get("DNSName") or "").strip().rstrip(".")
    uid = self_node.get("UserID")
    users = data.get("User") or {}
    user_row = users.get(str(uid), {}) if uid is not None else {}
    login = str(user_row.get("LoginName") or "").strip()
    if not dns:
        raise ValueError("Tailscale не сообщил MagicDNS-имя этого компьютера.")
    if not login:
        raise ValueError("Tailscale не сообщил login владельца этого компьютера.")
    return RemoteAccessConfig(dns, login)


def configure() -> int:
    ts = find_tailscale()
    if ts is None:
        print("Tailscale не найден. Установите официальный Tailscale for Windows и войдите в свой аккаунт.")
        return 2
    try:
        config = config_from_status(tailscale_status(ts))
        target = save_remote_config(config)
    except ValueError as exc:
        print(f"Ошибка настройки: {exc}")
        return 2
    print("Конфигурация PM Assistant Remote сохранена:")
    print(f"  Сервер: {config.origin}")
    print(f"  Разрешённый владелец Tailscale: {config.owner_login}")
    print(f"  Файл: {target}")
    print("Важно: PM Assistant всё равно потребует свой логин и пароль.")
    return 0


def copy_database(source: Path) -> int:
    source = source.expanduser().resolve()
    target_dir = remote_data_dir()
    target = target_dir / "project_memory.sqlite3"
    if not source.is_file():
        print(f"Исходная база не найдена: {source}")
        return 2
    if target.exists():
        print(f"Удалённая база уже существует и не будет перезаписана: {target}")
        return 3
    target_dir.mkdir(parents=True, exist_ok=True)
    try:
        with sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True, timeout=15) as src, \
             sqlite3.connect(target, timeout=15) as dst:
            src.backup(dst)
    except sqlite3.Error as exc:
        try:
            target.unlink(missing_ok=True)
        except OSError:
            pass
        print(f"Не удалось скопировать базу: {exc}")
        return 4
    print(f"База безопасно скопирована в: {target}")
    return 0


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



def check_ollama() -> int:
    """Validate that the loopback Ollama instance exposes both PM Assistant models.

    Exit codes are intentionally distinct so Windows helpers can tell an unavailable
    service from a service that is running with the wrong model directory.
    """
    try:
        models = OllamaClient().models()
    except OllamaError as exc:
        message = str(exc)
        print(f"Ollama check failed: {message}")
        if message.startswith("Required local model is missing:"):
            print("Ollama отвечает, но PM Assistant не видит одну из своих моделей.")
            print("Установите недостающие модели: ollama pull qwen3.5:9b и ollama pull qwen3-embedding:0.6b")
            print("После этого повторите проверку.")
            return 21
        if message.startswith("Local Ollama request failed"):
            print("Ollama не отвечает на 127.0.0.1:11434.")
            return 20
        return 22
    print("Ollama OK. Требуемые модели доступны:")
    for name in ("qwen3.5:9b", "qwen3-embedding:0.6b"):
        digest = models.get(name, "")
        print(f"  {name}  {digest[:12] if digest else ''}")
    return 0

def run_server() -> int:
    try:
        config = load_remote_config()
    except ValueError as exc:
        print(exc)
        print("Сначала запустите setup_remote_server.cmd.")
        return 2

    mutex = acquire_mutex()
    if mutex is None:
        print("PM Assistant Remote Server уже запущен.")
        return 3

    data_dir = remote_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    server = None
    try:
        state = AppState(resource_root(), data_dir, remote_access=config)
        server = LocalServer(("127.0.0.1", 8765), state)
        print(f"PM Assistant Remote Server {__version__}")
        print("Backend: http://127.0.0.1:8765 (ТОЛЬКО loopback)")
        print(f"Tailscale URL: {config.origin}")
        print(f"Удалённо разрешён только Tailscale login: {config.owner_login}")
        print("Ollama: 127.0.0.1:11434")
        try:
            models = OllamaClient().models()
            print("Ollama и требуемые модели доступны: " + ", ".join(models))
        except OllamaError as exc:
            print("ВНИМАНИЕ: AI-операции сейчас недоступны.")
            print(f"Причина: {exc}")
            if str(exc).startswith("Required local model is missing:"):
                print(r"Запустите scripts\windows\start_ollama.cmd и установите недостающую модель через ollama pull.")
        if not state.auth.has_users():
            print("ВАЖНО: первый аккаунт создайте локально на этом ПК через http://127.0.0.1:8765")
        print("Не используйте Tailscale Funnel. Ctrl+C останавливает backend.")
        server.serve_forever()
        return 0
    except KeyboardInterrupt:
        print("\nPM Assistant Remote Server остановлен.")
        return 0
    except OSError as exc:
        print(f"Не удалось запустить сервер на 127.0.0.1:8765: {exc}")
        return 4
    finally:
        if server is not None:
            server.server_close()
        release_mutex(mutex)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--configure", action="store_true")
    parser.add_argument("--print-config", action="store_true")
    parser.add_argument("--copy-db")
    parser.add_argument("--check-ollama", action="store_true")
    args = parser.parse_args()
    if args.configure:
        return configure()
    if args.print_config:
        try:
            cfg = load_remote_config()
        except ValueError as exc:
            print(exc)
            return 2
        print(cfg.origin)
        print(cfg.owner_login)
        return 0
    if args.copy_db:
        return copy_database(Path(args.copy_db))
    if args.check_ollama:
        return check_ollama()
    return run_server()


if __name__ == "__main__":
    raise SystemExit(main())
