from __future__ import annotations

import base64
import json
import os
import sys
import urllib.request
from pathlib import Path


BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
ROOT = Path(__file__).resolve().parents[1]
SECURITY_ENV = ROOT / "config" / "security.env"


def load_env_file(path: Path) -> None:
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
        if name and name not in os.environ:
            os.environ[name] = value.strip().strip("'\"")


def auth_header() -> str | None:
    load_env_file(SECURITY_ENV)
    user = os.environ.get("INFRA_CONTROL_BASIC_AUTH_USER", "").strip()
    password = os.environ.get("INFRA_CONTROL_BASIC_AUTH_PASSWORD", "").strip()
    if not user or not password:
        return None
    token = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


AUTH_HEADER = auth_header()


def get(path: str) -> object:
    request = urllib.request.Request(BASE + path)
    if AUTH_HEADER:
        request.add_header("Authorization", AUTH_HEADER)
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def main() -> None:
    health = get("/api/health")
    summary = get("/api/summary")
    passed = bool(health.get("ok")) and int(summary.get("assets", 0)) > 0
    print(json.dumps({"passed": passed, "health": health, "summary": summary}, ensure_ascii=False, indent=2))
    raise SystemExit(0 if passed else 1)


if __name__ == "__main__":
    main()
