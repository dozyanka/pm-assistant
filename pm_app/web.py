"""Loopback web UI with local accounts, safe imports and project management."""
from __future__ import annotations

import base64
import hmac
import json
import secrets
import sqlite3
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import __version__
from .auth import AuthManager, SESSION_DAYS
from .db import Store, parse_jsonl
from .importers import generate_project_id, parse_upload
from .ollama import OllamaError
from .remote import RemoteAccessConfig, decode_identity_header
from .service import Service
from .texts import SUMMARY_QUESTION

MAX_BODY = 12 * 1024 * 1024
STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/js/00-core.js": ("js/00-core.js", "text/javascript; charset=utf-8"),
    "/js/01-questions.js": ("js/01-questions.js", "text/javascript; charset=utf-8"),
    "/js/02-messages.js": ("js/02-messages.js", "text/javascript; charset=utf-8"),
    "/js/03-registry.js": ("js/03-registry.js", "text/javascript; charset=utf-8"),
    "/js/04-tasks.js": ("js/04-tasks.js", "text/javascript; charset=utf-8"),
    "/js/05-post-meeting.js": ("js/05-post-meeting.js", "text/javascript; charset=utf-8"),
    "/js/06-participants.js": ("js/06-participants.js", "text/javascript; charset=utf-8"),
    "/js/07-sources-and-qa.js": ("js/07-sources-and-qa.js", "text/javascript; charset=utf-8"),
    "/js/08-account-and-import.js": ("js/08-account-and-import.js", "text/javascript; charset=utf-8"),
    "/js/09-bootstrap.js": ("js/09-bootstrap.js", "text/javascript; charset=utf-8"),
    "/css/00-base.css": ("css/00-base.css", "text/css; charset=utf-8"),
    "/css/01-diagnostics.css": ("css/01-diagnostics.css", "text/css; charset=utf-8"),
    "/css/02-registry.css": ("css/02-registry.css", "text/css; charset=utf-8"),
    "/css/03-workspace.css": ("css/03-workspace.css", "text/css; charset=utf-8"),
    "/css/04-tasks.css": ("css/04-tasks.css", "text/css; charset=utf-8"),
    "/css/05-participants.css": ("css/05-participants.css", "text/css; charset=utf-8"),
}


class AppState:
    def __init__(self, root: Path, data_dir: Path, client=None, remote_access: RemoteAccessConfig | None = None):
        self.root = Path(root)
        self.store = Store(Path(data_dir) / "project_memory.sqlite3")
        self.auth = AuthManager(self.store)
        self.service = Service(self.store, self.root, client)
        self.remote_access = remote_access
        self.token = secrets.token_urlsafe(32)
        self.operation_lock = threading.Lock()
        self.job_lock = threading.Lock()
        self.jobs: dict[str, dict] = {}
        self.login_lock = threading.Lock()
        self.login_failures: dict[str, list[float]] = {}


    def login_allowed(self, key: str) -> bool:
        now = time.monotonic()
        with self.login_lock:
            recent = [stamp for stamp in self.login_failures.get(key, []) if now - stamp < 600]
            self.login_failures[key] = recent
            return len(recent) < 8

    def record_login_failure(self, key: str) -> None:
        now = time.monotonic()
        with self.login_lock:
            recent = [stamp for stamp in self.login_failures.get(key, []) if now - stamp < 600]
            recent.append(now)
            self.login_failures[key] = recent[-8:]

    def clear_login_failures(self, key: str) -> None:
        with self.login_lock:
            self.login_failures.pop(key, None)

    def start_job(self, operation) -> str:
        if not self.operation_lock.acquire(blocking=False):
            raise ValueError("Another operation is running. Finish it before starting the next one.")
        jid = secrets.token_urlsafe(18)
        with self.job_lock:
            if len(self.jobs) >= 20:
                self.jobs.pop(next(iter(self.jobs)))
            self.jobs[jid] = {"id": jid, "state": "running", "stage": "models"}

        def progress(stage):
            with self.job_lock:
                self.jobs[jid]["stage"] = stage

        def worker():
            try:
                result = operation(progress)
                with self.job_lock:
                    self.jobs[jid].update(state="done", result=result)
            except Exception as exc:
                if isinstance(exc, OllamaError):
                    detail = str(exc)
                    if detail.startswith("Required local model is missing:"):
                        model = detail.removeprefix("Required local model is missing: ").split(". No automatic", 1)[0].strip()
                        safe_error = (
                            f"Ollama работает, но не видит требуемую модель {model}. "
                            f"На основном ПК выполните: ollama pull {model}. "
                            "После этого повторите операцию. Данные проекта не изменены."
                        )
                    elif detail.startswith("Local Ollama request failed"):
                        safe_error = (
                            "Ollama не отвечает на основном ПК по адресу 127.0.0.1:11434. "
                            "Запустите start_ollama.cmd и оставьте окно Ollama открытым. Данные проекта не изменены."
                        )
                    else:
                        safe_error = (
                            "Локальная модель не смогла сформировать корректно проверяемый ответ. "
                            "Данные проекта не изменены. Повторите запрос; если ошибка повторяется, "
                            "проверьте окно PM Assistant Remote Server — там теперь указана точная причина проблемы с Ollama."
                        )
                elif isinstance(exc, ValueError):
                    safe_error = str(exc)
                else:
                    safe_error = "Локальная операция не завершена. Данные проекта не изменены."
                with self.job_lock:
                    self.jobs[jid].update(state="error", error=safe_error)
            finally:
                self.operation_lock.release()
        threading.Thread(target=worker, daemon=True).start()
        return jid

    def quick_write(self, fn):
        if not self.operation_lock.acquire(blocking=False):
            raise ValueError("An answer or index is being built. Import/edit after it finishes.")
        try:
            return fn()
        finally:
            self.operation_lock.release()


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, address, state: AppState, allow_container_bind: bool = False):
        if address[0] != "127.0.0.1":
            if not (allow_container_bind and address[0] == "0.0.0.0"):
                raise ValueError("Only IPv4 loopback is permitted unless the explicit container bind mode is used.")
        self.state = state
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    server_version = "PM-Assistant/" + __version__
    sys_version = ""

    def setup(self):
        super().setup()
        self.connection.settimeout(30)

    @property
    def app(self) -> AppState:
        return self.server.state

    @property
    def allowed_origin(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"

    @property
    def remote_config(self) -> RemoteAccessConfig | None:
        return self.app.remote_access

    def _peer_is_loopback(self) -> bool:
        return self.client_address[0] in {"127.0.0.1", "::1"}

    @staticmethod
    def _host_only(value: str | None) -> str:
        if not value or not isinstance(value, str) or len(value) > 300:
            return ""
        candidate = value.strip().lower()
        if candidate.startswith("["):
            end = candidate.find("]")
            return candidate[1:end] if end > 0 else ""
        return candidate.rsplit(":", 1)[0] if candidate.count(":") == 1 else candidate

    def is_remote_proxy_request(self) -> bool:
        cfg = self.remote_config
        if cfg is None or not self._peer_is_loopback():
            return False
        forwarded_host = self._host_only(self.headers.get("X-Forwarded-Host"))
        host = self._host_only(self.headers.get("Host"))
        target_host = forwarded_host or host
        if target_host != cfg.host:
            return False
        if self.headers.get("X-Forwarded-Proto", "").strip().lower() != "https":
            return False
        return decode_identity_header(self.headers.get("Tailscale-User-Login")) == cfg.owner_login

    def request_origin(self) -> str | None:
        proxy_markers = any(self.headers.get(name) for name in (
            "X-Forwarded-Host", "X-Forwarded-Proto", "Tailscale-User-Login",
            "Tailscale-User-Name", "Tailscale-User-Profile-Pic"))
        if proxy_markers:
            if self.is_remote_proxy_request():
                return self.remote_config.origin
            return None
        local_host = self.headers.get("Host", "")
        if self._peer_is_loopback() and local_host == self.allowed_origin.removeprefix("http://"):
            return self.allowed_origin
        return None

    def _login_key(self) -> str:
        if self.is_remote_proxy_request():
            return "tailscale:" + self.remote_config.owner_login
        return "local:" + self.client_address[0]

    def _session_cookie(self, token: str, max_age: int) -> str:
        parts = [f"pm_session={token}", "HttpOnly", "SameSite=Strict", "Path=/", f"Max-Age={max_age}"]
        if self.is_remote_proxy_request():
            parts.append("Secure")
        return "; ".join(parts)

    def log_message(self, fmt, *args):
        return

    def _session_token(self) -> str | None:
        raw = self.headers.get("Cookie", "")
        if not raw:
            return None
        cookie = SimpleCookie()
        try:
            cookie.load(raw)
        except Exception:
            return None
        item = cookie.get("pm_session")
        return item.value if item else None

    def current_user(self) -> dict | None:
        if not self.app.auth.has_users():
            # Initial account creation is intentionally local-only, even when remote mode is configured.
            if self.is_remote_proxy_request():
                return None
            return {"id": 0, "username": "bootstrap", "display_name": "Администратор", "role": "admin", "bootstrap": True}
        return self.app.auth.user_for_token(self._session_token())

    def headers_ok(self, write: bool = False, require_token: bool = True) -> bool:
        expected = self.request_origin()
        if expected is None:
            return False
        site = self.headers.get("Sec-Fetch-Site")
        if site not in (None, "same-origin", "none"):
            return False
        origin = self.headers.get("Origin")
        if origin and origin != expected:
            return False
        if write:
            if origin != expected:
                return False
            if self.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/json":
                return False
            if require_token:
                token = self.headers.get("X-PM-Token", "")
                if not token.isascii() or not hmac.compare_digest(token, self.app.token):
                    return False
        return True

    def send_data(self, status: int, data: bytes, content_type: str = "application/json; charset=utf-8",
                  extra_headers: dict[str, str] | None = None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        if self.is_remote_proxy_request():
            self.send_header("Strict-Transport-Security", "max-age=31536000")
        self.send_header("Content-Security-Policy",
            "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; "
            "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'; object-src 'none'")
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def json(self, status: int, value: dict, extra_headers: dict[str, str] | None = None):
        self.send_data(status, json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8"),
                       extra_headers=extra_headers)

    def _require_user(self) -> dict | None:
        user = self.current_user()
        if user is None:
            self.json(401, {"error": "Требуется вход в PM Assistant.", "auth_required": True})
        return user

    @staticmethod
    def _decode_upload(payload: dict) -> tuple[str, bytes]:
        file_name = payload.get("file_name")
        encoded = payload.get("data_base64")
        if not isinstance(file_name, str) or not isinstance(encoded, str):
            raise ValueError("Не передан файл для импорта.")
        try:
            data = base64.b64decode(encoded, validate=True)
        except Exception as exc:
            raise ValueError("Не удалось прочитать файл из запроса.") from exc
        return file_name, data

    def do_GET(self):
        if not self.headers_ok():
            self.json(403, {"error": "Use the exact local address printed in the app console."})
            return
        url = urlsplit(self.path)
        params = parse_qs(url.query)
        one = lambda key, default="": params.get(key, [default])[0]
        try:
            if url.path in STATIC:
                file_name, mime = STATIC[url.path]
                self.send_data(200, (Path(__file__).parent / "static" / file_name).read_bytes(), mime)
                return
            if url.path == "/api/auth/status":
                user = None if not self.app.auth.has_users() else self.app.auth.user_for_token(self._session_token())
                self.json(200, {"setup_required": not self.app.auth.has_users(), "authenticated": bool(user),
                                "user": user, "version": __version__,
                                "remote_access": bool(self.is_remote_proxy_request())})
                return
            user = self._require_user()
            if user is None:
                return
            if url.path == "/api/state":
                self.json(200, {"version": __version__, "csrf_token": self.app.token,
                                "projects": self.app.store.projects(), "imports": self.app.store.import_batches(),
                                "busy": self.app.operation_lock.locked(), "user": user,
                                "remote_access": bool(self.is_remote_proxy_request())})
            elif url.path == "/api/users":
                self.json(200, {"users": self.app.auth.list_users(user)})
            elif url.path == "/api/import-batches":
                self.json(200, {"imports": self.app.store.import_batches()})
            elif url.path == "/api/messages":
                self.json(200, {"messages": self.app.store.current(one("project"), one("source") or None)})
            elif url.path == "/api/history":
                self.json(200, {"answers": self.app.store.answers(one("project"))})
            elif url.path == "/api/revisions":
                self.json(200, {"revisions": self.app.store.revision_history(one("project"), int(one("message")))})
            elif url.path == "/api/registry":
                topic = one("topic", "")
                topic_filter = topic if "topic" in params else None
                self.json(200, self.app.store.registry(one("project"), topic_filter))
            elif url.path == "/api/registry-history":
                self.json(200, {"history": self.app.store.registry_history(one("project"), int(one("entry")))})
            elif url.path == "/api/meetings":
                self.json(200, {"meetings": self.app.service.meeting_sessions(one("project"))})
            elif url.path == "/api/participants":
                self.json(200, {"participants": self.app.store.participants(one("project"))})
            elif url.path == "/api/job":
                with self.app.job_lock:
                    job = dict(self.app.jobs.get(one("id"), {}))
                if not job:
                    self.json(404, {"error": "This operation is no longer available. Refresh the page and retry."})
                else:
                    self.json(200, job)
            elif url.path == "/favicon.ico":
                self.send_data(204, b"", "image/x-icon")
            else:
                self.json(404, {"error": "Not found."})
        except (ValueError, sqlite3.Error) as exc:
            self.json(400, {"error": str(exc) if isinstance(exc, ValueError) else "Database operation failed."})

    def do_POST(self):
        path = urlsplit(self.path).path
        no_csrf = path in {"/api/auth/setup", "/api/auth/login"}
        if not self.headers_ok(write=True, require_token=not no_csrf) or self.headers.get("Transfer-Encoding"):
            self.json(403, {"error": "Request blocked by local origin/token checks. Reload the page."})
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= MAX_BODY:
                self.json(413, {"error": "Upload/request exceeds the 12 MiB limit or is empty."})
                return
            raw = self.rfile.read(size)
            if len(raw) != size:
                raise ValueError("Incomplete request body.")
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError("Expected a JSON object.")

            if path == "/api/auth/setup":
                if self.is_remote_proxy_request():
                    self.json(403, {"error": "Первый аккаунт PM Assistant создаётся только на основном компьютере."})
                    return
                user = self.app.auth.setup_first_admin(payload.get("username"), payload.get("display_name"), payload.get("password"))
                user, token = self.app.auth.login(user["username"], payload.get("password"))
                cookie = self._session_cookie(token, SESSION_DAYS * 86400)
                self.json(200, {"user": user, "csrf_token": self.app.token}, {"Set-Cookie": cookie})
                return
            if path == "/api/auth/login":
                login_key = self._login_key()
                if not self.app.login_allowed(login_key):
                    self.json(429, {"error": "Слишком много неудачных попыток входа. Повторите позже."})
                    return
                try:
                    user, token = self.app.auth.login(payload.get("username"), payload.get("password"))
                except ValueError:
                    self.app.record_login_failure(login_key)
                    raise
                self.app.clear_login_failures(login_key)
                cookie = self._session_cookie(token, SESSION_DAYS * 86400)
                self.json(200, {"user": user, "csrf_token": self.app.token}, {"Set-Cookie": cookie})
                return

            user = self._require_user()
            if user is None:
                return
            if path == "/api/auth/logout":
                self.app.auth.logout(self._session_token())
                self.json(200, {"ok": True}, {"Set-Cookie": self._session_cookie("", 0)})
            elif path == "/api/users":
                created = self.app.auth.create_user(user, payload.get("username"), payload.get("display_name"),
                                                    payload.get("password"), payload.get("role") or "pm")
                self.json(200, {"user": created})
            elif path == "/api/projects":
                name = payload.get("name")
                requested = payload.get("id")
                if requested is None:
                    existing = {p["id"] for p in self.app.store.projects()}
                    requested = generate_project_id(name, existing)
                self.app.quick_write(lambda: self.app.store.create_project(requested, name))
                self.json(200, {"ok": True, "project_id": requested})
            elif path == "/api/projects/rename":
                self.app.quick_write(lambda: self.app.store.rename_project(payload.get("project_id"), payload.get("name")))
                self.json(200, {"ok": True})
            elif path == "/api/projects/delete":
                pid = payload.get("project_id")
                self.app.store.require_project(pid)
                self.app.quick_write(lambda: self.app.store.delete_project(pid))
                self.json(200, {"ok": True})
            elif path == "/api/messages/edit":
                pid = payload.get("project_id")
                message_fk = payload.get("message_id")
                if type(message_fk) is not int:
                    raise ValueError("Invalid message.")
                result = self.app.quick_write(lambda: self.app.store.revise_message_text(pid, message_fk, payload.get("text")))
                self.json(200, result)
            elif path == "/api/import/preview":
                file_name, data = self._decode_upload(payload)
                selected = payload.get("project_id") or None
                if selected:
                    self.app.store.require_project(selected)
                bundle = parse_upload(file_name, data, selected_project=selected)
                preview = self.app.store.preview_import(bundle.rows, selected_project=None if bundle.is_dataset else selected,
                                                        names=bundle.names or None)
                self.json(200, {**preview, "is_dataset": bundle.is_dataset, "batch_name": bundle.batch_name,
                                "projects": [{"id": pid, "name": bundle.names.get(pid, pid),
                                              "message_count": sum(1 for r in bundle.rows if r["project_id"] == pid)}
                                             for pid in bundle.project_ids]})
            elif path == "/api/import/apply":
                file_name, data = self._decode_upload(payload)
                selected = payload.get("project_id") or None
                if selected:
                    self.app.store.require_project(selected)
                bundle = parse_upload(file_name, data, selected_project=selected)
                def apply_import():
                    result = self.app.store.import_rows(bundle.rows, selected_project=None if bundle.is_dataset else selected,
                                                        names=bundle.names or None, allow_revisions=True)
                    batch = None
                    if bundle.is_dataset and bundle.batch_key and bundle.batch_name:
                        batch = self.app.store.record_import_batch(bundle.batch_key, bundle.batch_name, bundle.file_name,
                            bundle.file_format, bundle.content_hash, bundle.project_ids, len(bundle.rows))
                    return result, batch
                result, batch = self.app.quick_write(apply_import)
                self.json(200, {**result, "batch": batch, "projects": bundle.project_ids, "is_dataset": bundle.is_dataset})
            # Legacy routes remain for automated compatibility but are no longer exposed in the UI.
            elif path == "/api/import-demo":
                self.json(200, self.app.quick_write(self.app.service.demo_import))
            elif path == "/api/import-full-demo":
                self.json(200, self.app.quick_write(self.app.service.full_demo_import))
            elif path == "/api/import-source-project-1":
                self.json(200, self.app.quick_write(self.app.service.source_project_1_import))
            elif path == "/api/import":
                pid = payload.get("project_id")
                self.app.store.require_project(pid)
                rows = parse_jsonl(payload.get("text"))
                allow = payload.get("allow_revisions", False)
                if type(allow) is not bool:
                    raise ValueError("allow_revisions must be a boolean.")
                result = self.app.quick_write(lambda: self.app.store.import_rows(rows, selected_project=pid, allow_revisions=allow))
                self.json(200, result)
            elif path == "/api/participants/infer":
                pid = payload.get("project_id")
                self.app.store.require_project(pid)
                jid = self.app.start_job(lambda report: self.app.service.infer_participant_roles(pid, report))
                self.json(202, {"job_id": jid})
            elif path == "/api/participants/role":
                pid = payload.get("project_id")
                self.app.store.require_project(pid)
                speaker = payload.get("speaker")
                side = payload.get("side")
                result = self.app.quick_write(lambda: self.app.store.set_participant_role(pid, speaker, side))
                self.json(200, {"participant": result})
            elif path == "/api/registry/sync":
                pid = payload.get("project_id")
                self.app.store.require_project(pid)
                topic = payload.get("topic_id")
                if topic is not None and not isinstance(topic, str):
                    raise ValueError("Invalid topic_id.")
                jid = self.app.start_job(lambda report: self.app.service.registry_sync(pid, report, topic_id=topic))
                self.json(202, {"job_id": jid})
            elif path == "/api/registry/action":
                pid = payload.get("project_id")
                self.app.store.require_project(pid)
                entry_id = payload.get("entry_id")
                if type(entry_id) is not int:
                    raise ValueError("Invalid registry entry.")
                result = self.app.quick_write(lambda: self.app.store.registry_action(pid, entry_id, payload.get("action"), payload.get("patch")))
                self.json(200, {"entry": result})
            elif path == "/api/post-meeting":
                pid = payload.get("project_id")
                self.app.store.require_project(pid)
                source_id = payload.get("source_id")
                meeting_date = payload.get("meeting_date")
                jid = self.app.start_job(lambda report: self.app.service.post_meeting(pid, source_id, meeting_date, report))
                self.json(202, {"job_id": jid})
            elif path in ("/api/ask", "/api/search"):
                pid = payload.get("project_id")
                self.app.store.require_project(pid)
                source = payload.get("source_id") or None
                if source is not None and not isinstance(source, str):
                    raise ValueError("Invalid source_id.")
                topic = payload.get("topic_id") or None
                if topic is not None and not isinstance(topic, str):
                    raise ValueError("Invalid topic_id.")
                question = SUMMARY_QUESTION if payload.get("summary") is True else payload.get("question")
                if not isinstance(question, str) or not 1 <= len(question.strip()) <= 1200:
                    raise ValueError("Enter a question/search query (1-1200 characters).")
                op = self.app.service.ask if path == "/api/ask" else self.app.service.search
                jid = self.app.start_job(lambda report: op(pid, question, source, report, topic_id=topic))
                self.json(202, {"job_id": jid})
            else:
                self.json(404, {"error": "Not found."})
        except (ValueError, TypeError, sqlite3.Error) as exc:
            self.json(400, {"error": str(exc) if isinstance(exc, (ValueError, TypeError)) else "Database operation failed."})
        except (TimeoutError, OSError):
            self.json(408, {"error": "Local request timed out or could not be read."})


def serve(root: Path, data_dir: Path, port: int = 8765):
    state = AppState(root, data_dir)
    server = LocalServer(("127.0.0.1", port), state)
    print(f"PM Assistant {__version__}: http://127.0.0.1:{server.server_port}", flush=True)
    print("Local workspace. Account sign-in is required after the first administrator is created.", flush=True)
    print("Keep the Ollama window open. Ctrl+C stops this app. No packages will be downloaded.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nPM Assistant stopped.")
    finally:
        server.server_close()
