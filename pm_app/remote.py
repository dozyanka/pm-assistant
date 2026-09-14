"""Trusted Tailscale Serve configuration for PM Assistant remote mode.

The application server always listens on loopback. Remote requests are accepted
only when they arrive through a local Tailscale Serve reverse proxy, carry the
expected Tailscale identity, and target the configured tailnet hostname.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from email.header import decode_header, make_header
from pathlib import Path


HOST_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$")


def normalize_tailnet_host(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("Некорректное имя Tailscale-сервера.")
    host = value.strip().lower().rstrip(".")
    if not host or len(host) > 253 or not host.endswith(".ts.net") or not HOST_RE.fullmatch(host):
        raise ValueError("Ожидалось DNS-имя Tailscale вида computer.tailnet.ts.net.")
    return host


def decode_identity_header(value: str | None) -> str:
    if not value or not isinstance(value, str):
        return ""
    if len(value) > 512 or "\r" in value or "\n" in value:
        return ""
    try:
        decoded = str(make_header(decode_header(value)))
    except Exception:
        return ""
    return decoded.strip().casefold()


def normalize_owner_login(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("Некорректный Tailscale login.")
    login = decode_identity_header(value)
    if not login or len(login) > 320 or any(ord(ch) < 32 for ch in login):
        raise ValueError("Некорректный Tailscale login.")
    return login


@dataclass(frozen=True)
class RemoteAccessConfig:
    host: str
    owner_login: str

    def __post_init__(self):
        object.__setattr__(self, "host", normalize_tailnet_host(self.host))
        object.__setattr__(self, "owner_login", normalize_owner_login(self.owner_login))

    @property
    def origin(self) -> str:
        return f"https://{self.host}"

    def to_dict(self) -> dict[str, str]:
        return {"host": self.host, "owner_login": self.owner_login}

    @classmethod
    def from_dict(cls, value: dict) -> "RemoteAccessConfig":
        if not isinstance(value, dict):
            raise ValueError("Некорректная конфигурация удалённого доступа.")
        return cls(value.get("host", ""), value.get("owner_login", ""))


def default_remote_root() -> Path:
    override = os.environ.get("PM_REMOTE_HOME")
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
        return base / "PM Assistant Remote"
    return Path.home() / ".local" / "share" / "pm-assistant-remote"


def remote_config_path() -> Path:
    return default_remote_root() / "server.json"


def remote_data_dir() -> Path:
    return default_remote_root() / "data"


def save_remote_config(config: RemoteAccessConfig, path: Path | None = None) -> Path:
    target = path or remote_config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(json.dumps(config.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, target)
    return target


def load_remote_config(path: Path | None = None) -> RemoteAccessConfig:
    target = path or remote_config_path()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError("Удалённый доступ ещё не настроен. Запустите setup_remote_server.cmd.") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("Не удалось прочитать конфигурацию удалённого доступа.") from exc
    return RemoteAccessConfig.from_dict(raw)
