from __future__ import annotations

import base64
import hmac
import json
import os
from pathlib import Path
from urllib.parse import urlparse

import http.server


ROOT = Path(__file__).resolve().parent
SECURITY_ENV = ROOT / "config" / "security.env"


def _load_env_file(path: Path) -> None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, value = stripped.split("=", 1)
        name = name.strip()
        if not name or name in os.environ:
            continue
        os.environ[name] = value.strip().strip("'\"")


_load_env_file(SECURITY_ENV)


def _basic_auth_user() -> str:
    return str(os.environ.get("INFRA_CONTROL_BASIC_AUTH_USER") or "infra-control").strip()


def _basic_auth_password() -> str:
    return str(os.environ.get("INFRA_CONTROL_BASIC_AUTH_PASSWORD") or "").strip()


def _enabled() -> bool:
    return bool(_basic_auth_user() and _basic_auth_password())


def _header_valid(header: str | None) -> bool:
    if not _enabled():
        return True
    if not header or not header.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(header[6:], validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return False
    user, separator, password = decoded.partition(":")
    if not separator:
        return False
    return hmac.compare_digest(user, _basic_auth_user()) and hmac.compare_digest(password, _basic_auth_password())


def _send_json(handler: http.server.BaseHTTPRequestHandler, payload: object, status: int) -> None:
    body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    handler.send_response(status)
    handler.send_header("content-type", "application/json; charset=utf-8")
    handler.send_header("cache-control", "no-store")
    handler.send_header("x-content-type-options", "nosniff")
    if status == 401:
        handler.send_header("www-authenticate", 'Basic realm="infra-control"')
    handler.send_header("content-length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _wrap_handler(method):
    def wrapped(self, *args, **kwargs):
        path = urlparse(self.path).path
        if not _enabled():
            if path == "/api/health":
                return _send_json(self, {"ok": True, "service": "infra-control"}, 200)
            if os.environ.get("INFRA_CONTROL_ALLOW_NO_AUTH", "").strip().lower() in ("1", "true", "yes"):
                return method(self, *args, **kwargs)
            return _send_json(self, {"error": "auth_unconfigured"}, 503)
        authenticated = _header_valid(self.headers.get("authorization"))
        if path == "/api/health" and not authenticated:
            return _send_json(self, {"ok": True, "service": "infra-control"}, 200)
        if not authenticated:
            return _send_json(self, {"error": "authentication_required"}, 401)
        return method(self, *args, **kwargs)

    return wrapped


_OriginalBaseHTTPRequestHandler = http.server.BaseHTTPRequestHandler


class _InfraControlAuthHandler(_OriginalBaseHTTPRequestHandler):
    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        if cls.__module__ != "__main__" and not cls.__module__.endswith("server"):
            return
        for name in ("do_GET", "do_POST"):
            method = getattr(cls, name, None)
            if method is not None and not getattr(method, "_infra_control_auth_wrapped", False):
                wrapped = _wrap_handler(method)
                wrapped._infra_control_auth_wrapped = True
                setattr(cls, name, wrapped)


http.server.BaseHTTPRequestHandler = _InfraControlAuthHandler
