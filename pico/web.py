"""Local web interface for a configured Pico runtime."""

from __future__ import annotations

import json
import mimetypes
import re
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from .runtime import Pico
from .workspace import WorkspaceContext, now

ASSET_ROOT = Path(__file__).with_name("web_assets")
ASSET_PATHS = {
    "/": ASSET_ROOT / "index.html",
    "/app.js": ASSET_ROOT / "app.js",
    "/styles.css": ASSET_ROOT / "styles.css",
    "/lucide.min.js": ASSET_ROOT / "lucide.min.js",
}
MAX_REQUEST_BYTES = 64 * 1024
SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class ApprovalBroker:
    """Bridge the synchronous tool approval call to a browser decision."""

    def __init__(self, timeout=300):
        self.timeout = float(timeout)
        self._condition = threading.Condition()
        self._counter = 0
        self._pending = None
        self._decision = None

    def request(self, name, args):
        with self._condition:
            self._counter += 1
            approval_id = f"approval-{self._counter}"
            self._pending = {
                "id": approval_id,
                "tool": str(name),
                "args": dict(args or {}),
                "created_at": now(),
            }
            self._decision = None
            self._condition.notify_all()
            deadline = time.monotonic() + self.timeout
            while self._decision is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._condition.wait(remaining)
            approved = bool(self._decision)
            self._pending = None
            self._decision = None
            self._condition.notify_all()
            return approved

    def resolve(self, approval_id, approved):
        with self._condition:
            if not self._pending or self._pending["id"] != str(approval_id):
                return False
            self._decision = bool(approved)
            self._condition.notify_all()
            return True

    def snapshot(self):
        with self._condition:
            return dict(self._pending) if self._pending else None

    def cancel(self):
        with self._condition:
            if self._pending:
                self._decision = False
                self._condition.notify_all()


class WebRuntime:
    """Thread-safe state shared by the HTTP handlers and AgentLoop worker."""

    def __init__(self, agent, approval_timeout=300, agent_factory=None):
        self.agent = agent
        self.agent_factory = agent_factory
        self.approvals = ApprovalBroker(timeout=approval_timeout)
        self.agent.approval_callback = self.approvals.request
        self._lock = threading.RLock()
        self._worker = None
        self.active = False
        self.error = ""
        self.last_answer = ""
        self.started_at = ""

    def submit(self, message):
        message = str(message or "").strip()
        if not message:
            raise ValueError("message must not be empty")
        with self._lock:
            if self.active:
                raise RuntimeError("Pico is already working on a request")
            self.active = True
            self.error = ""
            self.last_answer = ""
            self.started_at = now()
            self._worker = threading.Thread(target=self._run, args=(message,), daemon=True)
            self._worker.start()

    def _run(self, message):
        try:
            answer = self.agent.ask(message)
            with self._lock:
                self.last_answer = str(answer)
        except Exception as exc:  # noqa: BLE001 - surface provider/runtime failures in the UI
            with self._lock:
                self.error = str(exc)
        finally:
            with self._lock:
                self.active = False

    def wait(self, timeout=None):
        worker = self._worker
        if worker:
            worker.join(timeout)
        return not bool(worker and worker.is_alive())

    def reset_session(self):
        with self._lock:
            self._require_idle()
            self.agent.reset()
            self.error = ""
            self.last_answer = ""

    def new_session(self):
        with self._lock:
            self._require_idle()
            self._replace_agent()
            return self.agent.session["id"]

    def switch_session(self, session_id):
        session_id = str(session_id or "").strip()
        if not session_id:
            raise ValueError("session_id must not be empty")
        if not SESSION_ID_PATTERN.fullmatch(session_id):
            raise ValueError("session_id contains invalid characters")
        with self._lock:
            self._require_idle()
            if not self.agent.session_store.path(session_id).is_file():
                raise ValueError("session does not exist")
            self._replace_agent(session_id=session_id)
            return self.agent.session["id"]

    def configure(self, settings):
        settings = dict(settings or {})
        workspace = Path(str(settings.get("workspace") or "")).expanduser()
        provider = str(settings.get("provider") or "").strip()
        model = str(settings.get("model") or "").strip()
        if not workspace.is_dir():
            raise ValueError("workspace must be an existing directory")
        if provider not in {"ollama", "openai", "anthropic", "deepseek"}:
            raise ValueError("provider is not supported")
        if not model:
            raise ValueError("model must not be empty")
        if self.agent_factory is None:
            raise RuntimeError("runtime configuration is unavailable")
        with self._lock:
            self._require_idle()
            normalized = {
                "workspace": str(workspace.resolve()),
                "provider": provider,
                "model": model,
                "endpoint": str(settings.get("endpoint") or "").strip(),
            }
            self.agent = self.agent_factory(normalized)
            self.agent.approval_callback = self.approvals.request
            self.error = ""
            self.last_answer = ""
            self.started_at = ""
            return self.agent.session["id"]

    def _replace_agent(self, session_id=None):
        previous = self.agent
        kwargs = {
            "model_client": previous.model_client,
            "workspace": WorkspaceContext.build(previous.root),
            "session_store": previous.session_store,
            "approval_policy": previous.approval_policy,
            "max_steps": previous.max_steps,
            "max_new_tokens": previous.max_new_tokens,
            "read_only": previous.read_only,
            "shell_env_allowlist": previous.shell_env_allowlist,
            "secret_env_names": previous.secret_env_names,
            "feature_flags": previous.feature_flags,
            "allowed_tools": previous.allowed_tools,
            "approval_callback": self.approvals.request,
        }
        if session_id:
            self.agent = Pico.from_session(session_id=session_id, **kwargs)
        else:
            self.agent = Pico(**kwargs)
        self.agent.web_config = dict(
            getattr(
                previous,
                "web_config",
                {
                    "workspace": self.agent.workspace.cwd,
                    "provider": "custom",
                    "model": str(getattr(self.agent.model_client, "model", "")),
                    "endpoint": "",
                },
            )
        )
        self.agent.web_config["workspace"] = self.agent.workspace.cwd
        self.error = ""
        self.last_answer = ""
        self.started_at = ""

    def _require_idle(self):
        if self.active:
            raise RuntimeError("wait for the current request to finish")

    def state(self):
        with self._lock:
            agent = self.agent
            model = getattr(agent.model_client, "model", agent.model_client.__class__.__name__)
            task_state = agent.current_task_state.to_dict() if agent.current_task_state else None
            sessions = []
            for path in sorted(
                agent.session_store.root.glob("*.json"),
                key=lambda item: item.stat().st_mtime,
                reverse=True,
            )[:30]:
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                    history = payload.get("history", [])
                    first_user = next(
                        (item.get("content", "") for item in history if item.get("role") == "user"),
                        "",
                    )
                    title = str(payload.get("title", "")).strip() or str(first_user).strip()
                    if not title:
                        title = f"New session {path.stem[-6:]}"
                    sessions.append(
                        {
                            "id": path.stem,
                            "title": title[:72],
                            "active": path.stem == agent.session["id"],
                            "updated_at": path.stat().st_mtime,
                        }
                    )
                except (OSError, ValueError, TypeError):
                    continue
            payload = {
                "runtime": {
                    "active": self.active,
                    "error": self.error,
                    "last_answer": self.last_answer,
                    "started_at": self.started_at,
                    "pending_approval": self.approvals.snapshot(),
                },
                "agent": {
                    "workspace": agent.workspace.cwd,
                    "repo_root": agent.workspace.repo_root,
                    "branch": agent.workspace.branch,
                    "model": str(model),
                    "provider": agent.model_client.__class__.__name__,
                    "approval": agent.approval_policy,
                    "max_steps": agent.max_steps,
                    "session_id": agent.session["id"],
                    "configuration": dict(
                        getattr(
                            agent,
                            "web_config",
                            {
                                "workspace": agent.workspace.cwd,
                                "provider": "custom",
                                "model": str(model),
                                "endpoint": "",
                            },
                        )
                    ),
                },
                "history": list(agent.session.get("history", [])),
                "task_state": task_state,
                "sessions": sessions,
            }
            return agent.redact_artifact(payload)


def _json_bytes(payload):
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def build_web_server(agent, host="127.0.0.1", port=8765, approval_timeout=300, agent_factory=None):
    runtime = WebRuntime(agent, approval_timeout=approval_timeout, agent_factory=agent_factory)

    class PicoWebHandler(BaseHTTPRequestHandler):
        server_version = "PicoWeb/0.1"

        def do_GET(self):
            path = urlsplit(self.path).path
            if path == "/api/state":
                self._send_json(runtime.state())
                return
            if path == "/api/health":
                self._send_json({"status": "ok"})
                return
            asset = ASSET_PATHS.get(path)
            if asset and asset.is_file():
                content_type = mimetypes.guess_type(asset.name)[0] or "application/octet-stream"
                self._send(asset.read_bytes(), content_type)
                return
            self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

        def do_POST(self):
            path = urlsplit(self.path).path
            try:
                self._validate_origin()
                payload = self._read_json()
                if path == "/api/messages":
                    runtime.submit(payload.get("message"))
                    self._send_json({"status": "accepted"}, HTTPStatus.ACCEPTED)
                    return
                if path == "/api/approval":
                    resolved = runtime.approvals.resolve(payload.get("id"), payload.get("approved"))
                    if not resolved:
                        self._send_json({"error": "approval is no longer pending"}, HTTPStatus.CONFLICT)
                        return
                    self._send_json({"status": "resolved"})
                    return
                if path == "/api/session/reset":
                    runtime.reset_session()
                    self._send_json({"status": "reset"})
                    return
                if path == "/api/session/new":
                    session_id = runtime.new_session()
                    self._send_json({"status": "created", "session_id": session_id})
                    return
                if path == "/api/session/switch":
                    session_id = runtime.switch_session(payload.get("session_id"))
                    self._send_json({"status": "switched", "session_id": session_id})
                    return
                if path == "/api/configuration":
                    session_id = runtime.configure(payload)
                    self._send_json({"status": "configured", "session_id": session_id})
                    return
                self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            except (TypeError, ValueError) as exc:
                self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            except PermissionError as exc:
                self._send_json({"error": str(exc)}, HTTPStatus.FORBIDDEN)
            except RuntimeError as exc:
                self._send_json({"error": str(exc)}, HTTPStatus.CONFLICT)
            except Exception as exc:  # noqa: BLE001 - keep handler failures as JSON responses
                self._send_json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

        def _read_json(self):
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError as exc:
                raise ValueError("invalid content length") from exc
            if length <= 0 or length > MAX_REQUEST_BYTES:
                raise ValueError("request body size is invalid")
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError("request body must be valid JSON") from exc
            if not isinstance(payload, dict):
                raise TypeError("request body must be a JSON object")
            return payload

        def _validate_origin(self):
            origin = self.headers.get("Origin", "").rstrip("/")
            if not origin:
                return
            expected = f"http://{self.headers.get('Host', '')}".rstrip("/")
            if origin != expected:
                raise PermissionError("cross-origin requests are not allowed")

        def _send_json(self, payload, status=HTTPStatus.OK):
            self._send(_json_bytes(payload), "application/json; charset=utf-8", status)

        def _send(self, body, content_type, status=HTTPStatus.OK):
            self.send_response(int(status))
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; base-uri 'none'; form-action 'self'",
            )
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            return

    server = ThreadingHTTPServer((host, int(port)), PicoWebHandler)
    server.runtime = runtime
    return server


def run_web_server(agent, host="127.0.0.1", port=8765, agent_factory=None):
    server = build_web_server(agent, host=host, port=port, agent_factory=agent_factory)
    bound_host, bound_port = server.server_address[:2]
    display_host = "127.0.0.1" if bound_host in {"0.0.0.0", "::"} else bound_host
    print(f"\nPico web UI: http://{display_host}:{bound_port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        server.runtime.approvals.cancel()
        server.server_close()
    return 0
