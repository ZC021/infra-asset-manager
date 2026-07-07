from __future__ import annotations

import argparse
import base64
import hmac
import csv
import io
import json
import os
import re
import uuid
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

from infra_control.computer_use_receipts import (
    current_ui_build_id,
    expected_base,
    load_current_receipt,
    receipt_summary,
    save_receipt,
    token_matches,
    token_required,
    validate_receipt,
)
from infra_control.db import DB_PATH, connect, init_db, rows_to_dicts


ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
VAR_DIR = ROOT / "var"
SECURITY_ENV = ROOT / "config" / "security.env"
APPROVAL_SNAPSHOT = VAR_DIR / "approval_tasks.json"
APPROVAL_DETAILS = VAR_DIR / "approval_details.json"
APPROVAL_WORKFLOWS = VAR_DIR / "approval_workflows.json"
AMARANTH_WRITEBACK_QUEUE = VAR_DIR / "amaranth_writeback_queue.json"
JIRA_MISSING_WORKFLOWS = VAR_DIR / "jira_missing_workflows.json"
ACTIVE_USER_DIRECTORY_CSV = VAR_DIR / "imports" / "snipe-ops" / "organization_users.csv"
APPROVAL_HOME_URL = (
    os.environ.get("APPROVAL_HOME_URL")
    or os.environ.get("AMARANTH_BASE_URL")
    or "https://work.example.com"
).rstrip("/")
APPROVAL_DETAIL_URL_TEMPLATE = os.environ.get("APPROVAL_DETAIL_URL_TEMPLATE", "").strip()
SOURCE_HIDE_STALE_HOURS = int(os.environ.get("INFRA_CONTROL_HIDE_STALE_SOURCE_HOURS", "168"))
WRITEBACK_LOCAL_OUTBOX_STATUSES = {"queued", "local_outbox_unsent", "ready"}
_ACTIVE_USER_DIRECTORY_CACHE: dict[str, object] = {"mtime_ns": None, "users": {}}
USAGE_IN_USE = "사용중"
USAGE_UNUSED = "미사용"
USAGE_RETIRED = "종료/제외"
USAGE_NEEDS_REVIEW = "확인필요"
KNOWN_USAGE_STATUSES = (USAGE_IN_USE, USAGE_UNUSED, USAGE_RETIRED)
NOTEBOOK_REFRESH_YEARS = 4
NOTEBOOK_REFRESH_LIFECYCLE = "notebook_refresh"

ASSET_FIELDS = [
    "asset_tag",
    "asset_group",
    "hostname",
    "serial",
    "manufacturer",
    "model",
    "category",
    "status",
    "jira_lane",
    "jira_issue",
    "usage_status",
    "usage_reason",
    "usage_basis",
    "usage_evidence",
    "lifecycle_label",
    "notebook_acquired_date",
    "notebook_age_years",
    "notebook_age_basis",
    "notebook_refresh_priority",
    "notebook_refresh_decision",
    "notebook_refresh_reason",
    "notebook_refresh_flags",
    "purpose",
    "owner",
    "owner_email",
    "department",
    "primary_ip",
    "ports",
    "os",
    "location",
    "room",
    "rack",
    "rack_unit",
    "maintenance_date",
    "is_server",
    "is_virtual",
    "is_network",
]

EDITABLE_FIELDS = {
    "hostname",
    "serial",
    "manufacturer",
    "model",
    "category",
    "status",
    "usage_status",
    "usage_reason",
    "purpose",
    "owner",
    "owner_email",
    "department",
    "primary_ip",
    "ports",
    "os",
    "location",
    "room",
    "rack",
    "rack_unit",
    "maintenance_date",
}

NOTEBOOK_TERMS = (
    "노트북",
    "notebook",
    "laptop",
    "macbook",
    "gram",
    "thinkpad",
    "elitebook",
    "xps",
    "surface",
    "probook",
)

ASSET_KIND_LABELS = {
    "server": "서버",
    "network": "네트워크",
    "notebook": "노트북",
    "other": "기타자산",
}

ASSET_NUMBER_CLASSES = {
    "ACME-A01": {"key": "desktop", "label": "데스크탑", "rollup": "other"},
    "ACME-A02": {"key": "notebook", "label": "노트북", "rollup": "notebook"},
    "ACME-A03": {"key": "all_in_one", "label": "일체형PC", "rollup": "other"},
    "ACME-A05": {"key": "printer", "label": "복합기", "rollup": "other"},
    "ACME-A07": {"key": "misc", "label": "기타자산", "rollup": "other"},
    "ACME-B01": {"key": "server", "label": "서버", "rollup": "server"},
    "ACME-B02": {"key": "storage", "label": "스토리지", "rollup": "other"},
    "ACME-B03": {"key": "network", "label": "네트워크", "rollup": "network"},
    "ACME-B06": {"key": "kvm", "label": "KVM", "rollup": "other"},
    "ACME-S01": {"key": "server", "label": "서버", "rollup": "server"},
    "ACME-VA": {"key": "virtual_server", "label": "가상서버", "rollup": "server"},
    "ACME-X01": {"key": "power_monitoring", "label": "전원/모니터링", "rollup": "other"},
}
UNNUMBERED_ASSET_CLASS = {"key": "unnumbered", "label": "관리번호 미지정", "rollup": "other"}
ASSET_CLASS_ORDER = [
    "server",
    "virtual_server",
    "network",
    "notebook",
    "desktop",
    "storage",
    "printer",
    "power_monitoring",
    "kvm",
    "all_in_one",
    "misc",
    "unnumbered",
]
ASSET_CLASS_LABELS = {
    str(spec["key"]): str(spec["label"])
    for spec in [*ASSET_NUMBER_CLASSES.values(), UNNUMBERED_ASSET_CLASS]
}
ROLLUP_GROUPS = {
    kind: [group for group, spec in ASSET_NUMBER_CLASSES.items() if spec["rollup"] == kind]
    for kind in ASSET_KIND_LABELS
}
KST = timezone(timedelta(hours=9), "KST")
KST_ZONE_NAME = "Asia/Seoul"
MAX_REQUEST_BODY = 5 * 1024 * 1024  # 5 MiB cap on POST request bodies


def _allow_no_auth() -> bool:
    return str(os.environ.get("INFRA_CONTROL_ALLOW_NO_AUTH", "")).strip().lower() in {"1", "true", "yes"}


def _csv_safe(value: object) -> str:
    text = str(value if value is not None else "")
    if text and text[0] in ("=", "+", "-", "@", "\t", "\r"):
        return "\u0027" + text
    return text


def _log_exc(method: str, path: str, exc: BaseException) -> None:
    import sys, traceback
    print(f"[infra-control] {method} {path} -> {exc.__class__.__name__}: {exc}", file=sys.stderr)
    traceback.print_exc()
ASSET_API_LIMIT = 50000
CHANGE_API_DEFAULT_LIMIT = 1000
CHANGE_API_MAX_LIMIT = 5000
SOFTWARE_API_MAX_LIMIT = 5000
RETIRED_STATUS_HINTS = ("폐기", "불용", "매각", "처분", "분실", "반납완료")
IGNORED_SERIAL_VALUES_SQL = "'-', '조립', '조립PC', 'N/A', 'n/a'"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def kst_now() -> str:
    return datetime.now(KST).isoformat()


def duplicate_asset_tag_conflict_sql() -> str:
    return f"""
      SELECT asset_tag
      FROM assets
      WHERE COALESCE(TRIM(asset_tag), '') <> ''
      GROUP BY asset_tag
      HAVING COUNT(DISTINCT CASE
        WHEN COALESCE(TRIM(serial), '') <> ''
          AND TRIM(serial) NOT IN ({IGNORED_SERIAL_VALUES_SQL})
        THEN UPPER(TRIM(serial))
      END) > 1
    """


def parse_timestamp(value: object) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def normalize_request_path(path: str) -> str:
    if path in {"/infra", "/infra/"}:
        return "/"
    if path.startswith("/infra/"):
        return path.removeprefix("/infra")
    return path


def basic_auth_credentials(security_env: Path = SECURITY_ENV) -> tuple[str, str] | None:
    user = os.environ.get("INFRA_CONTROL_BASIC_AUTH_USER", "").strip()
    password = os.environ.get("INFRA_CONTROL_BASIC_AUTH_PASSWORD", "").strip()
    if user and password:
        return user, password
    if security_env.exists():
        values: dict[str, str] = {}
        for line in security_env.read_text(encoding="utf-8").splitlines():
            if not line or line.lstrip().startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
        user = values.get("INFRA_CONTROL_BASIC_AUTH_USER", "").strip()
        password = values.get("INFRA_CONTROL_BASIC_AUTH_PASSWORD", "").strip()
        if user and password:
            return user, password
    return None


def request_authorized(path: str, authorization: str | None, credentials: tuple[str, str] | None = None) -> bool:
    if path == "/api/health":
        return True
    credentials = credentials if credentials is not None else basic_auth_credentials()
    if credentials is None:
        return _allow_no_auth()
    if not authorization or not authorization.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(authorization[6:], validate=True).decode("utf-8")
    except Exception:
        return False
    user, separator, password = decoded.partition(":")
    if not separator:
        return False
    expected_user, expected_password = credentials
    return hmac.compare_digest(user, expected_user) and hmac.compare_digest(password, expected_password)


class Handler(BaseHTTPRequestHandler):
    server_version = "infra-control/0.2"

    def log_message(self, fmt: str, *args: object) -> None:
        return

    def _security_headers(self) -> None:
        self.send_header("x-content-type-options", "nosniff")
        self.send_header("x-frame-options", "SAMEORIGIN")
        self.send_header("referrer-policy", "same-origin")
        self.send_header("content-security-policy", "frame-ancestors 'self'")

    def send_json(self, payload: object, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("cache-control", "no-store")
        self._security_headers()
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_static(self, path: Path, content_type: str) -> None:
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("content-type", content_type)
        self.send_header("cache-control", "no-store")
        self._security_headers()
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_unauthorized(self) -> None:
        body = json.dumps({"error": "unauthorized"}, ensure_ascii=False).encode("utf-8")
        self.send_response(401)
        self.send_header("www-authenticate", 'Basic realm="infra-control"')
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _csrf_ok(self) -> bool:
        """Reject cross-origin browser writes. Same-origin and non-browser (no Origin/Referer) pass."""
        host = (self.headers.get("host") or "").split(":")[0].strip().lower()
        candidate = self.headers.get("origin") or self.headers.get("referer") or ""
        if not candidate:
            return True
        try:
            src = (urlparse(candidate).hostname or "").strip().lower()
        except Exception:
            return False
        return (not host) or src == host

    def read_json_body(self) -> dict[str, object]:
        length = int(self.headers.get("content-length", "0") or "0")
        if length <= 0:
            return {}
        if length > MAX_REQUEST_BODY:
            raise ValueError("request body too large")
        raw = self.rfile.read(length).decode("utf-8", errors="replace")
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("JSON object required")
        return data

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = normalize_request_path(parsed.path)
        query = parse_qs(parsed.query)
        try:
            if not request_authorized(path, self.headers.get("authorization")):
                return self.send_unauthorized()
            if path == "/":
                return self.send_static(STATIC / "index.html", "text/html; charset=utf-8")
            if path == "/static/app.js":
                return self.send_static(STATIC / "app.js", "application/javascript; charset=utf-8")
            if path == "/static/styles.css":
                return self.send_static(STATIC / "styles.css", "text/css; charset=utf-8")
            if path == "/api/health":
                return self.send_json(
                    {
                        "ok": True,
                        "db": str(DB_PATH),
                        "service": "infra-control",
                        "ui_build_id": current_ui_build_id(),
                    }
                )
            if path == "/api/receipts/computer-use":
                return self.send_json(computer_use_receipt_status())
            if path == "/api/summary":
                return self.send_json(summary())
            if path == "/api/insights":
                return self.send_json(insights())
            if path == "/api/data-quality":
                return self.send_json(data_quality())
            if path == "/api/sources":
                return self.send_json(sources(query))
            if path == "/api/jira-missing-assets":
                return self.send_json(jira_missing_assets(query))
            if path == "/api/software-expirations":
                return self.send_json(software_expirations(query))
            if path == "/api/software-inventory":
                return self.send_json(software_inventory(query))
            if path == "/api/software-installs":
                return self.send_json(software_installs(query))
            if path == "/api/software-users":
                return self.send_json(software_users(query))
            if path == "/api/software-compliance":
                return self.send_json(software_compliance(query))
            if path == "/api/asset-groups":
                return self.send_json(asset_groups(query))
            if path == "/api/owners":
                return self.send_json(owners(query))
            if path == "/api/assets":
                return self.send_json(assets(query, only_servers=False))
            if path == "/api/servers":
                return self.send_json(assets(query, only_servers=True))
            if path == "/api/locations":
                return self.send_json(locations(query))
            if path == "/api/maintenance":
                return self.send_json(maintenance(query))
            if path == "/api/changes":
                return self.send_json(changes(query))
            if path == "/api/approvals":
                return self.send_json(approvals(query))
            if path == "/api/approval-workflow":
                return self.send_json(approval_workflow((query.get("doc_id") or [""])[0]))
            if path == "/api/asset":
                return self.send_json(asset_detail((query.get("asset_tag") or [""])[0]))
            if path == "/api/export/assets.csv":
                return self.send_csv()
            self.send_json({"error": "not_found"}, status=404)
        except ValueError as exc:
            self.send_json({"error": "bad_request", "detail": str(exc)}, status=400)
        except Exception as exc:  # noqa: BLE001
            _log_exc("GET", self.path, exc)
            self.send_json({"error": "server_error"}, status=500)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            if not request_authorized(normalize_request_path(parsed.path), self.headers.get("authorization")):
                return self.send_unauthorized()
            if not self._csrf_ok():
                return self.send_json({"error": "cross_origin_forbidden"}, status=403)
            if parsed.path == "/api/assets/update":
                return self.send_json(update_asset(self.read_json_body()))
            if parsed.path == "/api/approval-workflow":
                return self.send_json(update_approval_workflow(self.read_json_body()))
            if parsed.path == "/api/jira-missing-workflow":
                return self.send_json(update_jira_missing_workflow(self.read_json_body()))
            if parsed.path == "/api/receipts/computer-use":
                return self.send_json(receive_computer_use_receipt(self.read_json_body(), self.headers.get("x-receipt-token", "")))
            self.send_json({"error": "not_found"}, status=404)
        except ValueError as exc:
            self.send_json({"error": "bad_request", "detail": str(exc)}, status=400)
        except Exception as exc:  # noqa: BLE001
            _log_exc("POST", self.path, exc)
            self.send_json({"error": "server_error"}, status=500)

    def send_csv(self) -> None:
        query = parse_qs(urlparse(self.path).query)
        rows = assets(query, only_servers=False, limit=50000)
        out = io.StringIO()
        fields = [
            "asset_tag",
            "asset_group",
            "usage_status",
            "usage_reason",
            "usage_basis",
            "usage_evidence",
            "hostname",
            "serial",
            "manufacturer",
            "model",
            "category",
            "status",
            "jira_lane",
            "jira_issue",
            "purpose",
            "owner",
            "owner_email",
            "department",
            "primary_ip",
            "ports",
            "os",
            "location",
            "room",
            "rack",
            "rack_unit",
            "maintenance_date",
            "is_server",
            "is_virtual",
            "is_network",
        ]
        writer = csv.DictWriter(out, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: _csv_safe(row.get(field, "")) for field in fields})
        body = out.getvalue().encode("utf-8-sig")
        self.send_response(200)
        self.send_header("content-type", "text/csv; charset=utf-8")
        self.send_header("content-disposition", 'attachment; filename="infra-assets.csv"')
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def select_all(sql: str, params: tuple[object, ...] = ()) -> list[dict[str, object]]:
    with connect() as conn:
        return rows_to_dicts(conn.execute(sql, params).fetchall())


def query_int(query: dict[str, list[str]], name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = (query.get(name) or [""])[0]
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def read_json_file(path: Path, fallback: object) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return fallback


def write_json_file(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f"{path.suffix}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def env_text(name: str) -> str:
    return str(os.environ.get(name) or "").strip()


def load_writeback_queue() -> dict[str, object]:
    payload = read_json_file(AMARANTH_WRITEBACK_QUEUE, {"version": 1, "items": []})
    if not isinstance(payload, dict):
        return {"version": 1, "items": []}
    if not isinstance(payload.get("items"), list):
        payload["items"] = []
    payload.setdefault("version", 1)
    return payload


def save_writeback_queue(payload: dict[str, object]) -> None:
    write_json_file(AMARANTH_WRITEBACK_QUEUE, payload)


def writeback_endpoint() -> str:
    return env_text("AMARANTH_WRITEBACK_ENDPOINT") or env_text("AMARANTH_WRITEBACK_URL")


def oidc_configured() -> bool:
    return bool(env_text("OIDC_ISSUER_URL") and env_text("OIDC_CLIENT_ID"))


def normalize_writeback_status(value: object) -> str:
    status = normalize_text(value)
    return "local_outbox_unsent" if status in WRITEBACK_LOCAL_OUTBOX_STATUSES else status


def queue_record_count(payload: dict[str, object]) -> int:
    items = payload.get("items") if isinstance(payload.get("items"), list) else []
    doc_ids: set[str] = set()
    count = 0
    for item in items:
        if not isinstance(item, dict) or normalize_writeback_status(item.get("status")) != "local_outbox_unsent":
            continue
        doc_id = normalize_text(item.get("docId"))
        if doc_id:
            if doc_id in doc_ids:
                continue
            doc_ids.add(doc_id)
        count += 1
    return count


def writeback_status(record: dict[str, object] | None = None) -> dict[str, object]:
    queue = load_writeback_queue()
    endpoint = writeback_endpoint()
    queued = queue_record_count(queue)
    status = normalize_writeback_status(record.get("writebackStatus")) if isinstance(record, dict) else ""
    message = normalize_text(record.get("writebackMessage")) if isinstance(record, dict) else ""
    return {
        "enabled": True,
        "configured": False,
        "endpoint_configured": bool(endpoint),
        "status": status or "local_outbox",
        "label": "원문 반영 요청",
        "buttonLabel": "원문 반영 요청 등록",
        "reason": message or "내부 outbox 보관 — 외부 전송 미구현",
        "endpoint": endpoint or str(AMARANTH_WRITEBACK_QUEUE),
        "queued": queued,
        "unsent": queued,
    }


def virtual_integration_sources() -> list[dict[str, object]]:
    queue = load_writeback_queue()
    endpoint = writeback_endpoint()
    queued = queue_record_count(queue)
    return [
        {
            "id": "amaranth-writeback",
            "name": "Amaranth Writeback Outbox",
            "kind": "approval-writeback",
            "endpoint": endpoint or str(AMARANTH_WRITEBACK_QUEUE),
            "status": "local-outbox",
            "last_sync_at": "",
            "record_count": queued,
        },
        {
            "id": "oidc",
            "name": "SSO/OIDC",
            "kind": "auth-provider",
            "endpoint": env_text("OIDC_ISSUER_URL") or "OIDC_ISSUER_URL",
            "status": "configured" if oidc_configured() else "not_configured",
            "last_sync_at": "",
            "record_count": None,
        },
    ]


def computer_use_receipt_status() -> dict[str, object]:
    receipt = load_current_receipt()
    expected = expected_base()
    if not receipt:
        return {"passed": False, "expected_base": expected, "receipt": None}
    passed, errors, detail = validate_receipt(receipt, expected)
    return {"passed": passed, "expected_base": expected, "errors": errors, "detail": detail}


def receive_computer_use_receipt(payload: dict[str, object], token: str) -> dict[str, object]:
    if token_required() and not token_matches(token):
        raise ValueError("invalid receipt token")
    if not isinstance(payload, dict):
        raise ValueError("receipt JSON object required")
    return save_receipt(payload, expected_base())


def usage_evidence(row: dict[str, object]) -> tuple[str, str]:
    metadata: dict[str, object] = {}
    try:
        parsed = json.loads(str(row.get("metadata_json") or "{}"))
        if isinstance(parsed, dict):
            metadata = parsed
    except json.JSONDecodeError:
        metadata = {}

    source_row = metadata.get("source_row") if isinstance(metadata.get("source_row"), dict) else {}
    if not source_row and isinstance(metadata.get("usage_source_row"), dict):
        source_row = metadata["usage_source_row"]
    stored = metadata.get("usage_evidence") if isinstance(metadata.get("usage_evidence"), list) else []
    evidence = [str(item) for item in stored if str(item or "").strip()]
    jira_board = metadata.get("jira_board") if isinstance(metadata.get("jira_board"), dict) else {}

    def flag(name: str) -> bool:
        return str(source_row.get(name) or "").strip().upper() == "Y"

    intune_user = str(source_row.get("intune_user_upn") or source_row.get("intune_user_localpart") or "").strip()
    nac_user = str(source_row.get("nac_username_norm") or source_row.get("nac_username") or "").strip()
    checkout_user = str(source_row.get("checkout_candidate_username") or "").strip()
    intune_last = str(source_row.get("intune_last_sync_utc") or metadata.get("intune_last_sync_utc") or "").strip()
    nac_last = str(source_row.get("nac_last_seen_utc") or metadata.get("nac_last_seen_utc") or "").strip()
    nac_status = str(source_row.get("nac_status") or "").strip()

    if intune_user and flag("recent_sync_intune_30d") and not any(item.startswith("Intune") for item in evidence):
        evidence.append(f"Intune 최근 동기화 {intune_user} {intune_last}".strip())
    if nac_user and (flag("nac_recently_used") or flag("recent_seen_nac_14d")) and not any(item.startswith("NAC 최근") for item in evidence):
        evidence.append(f"NAC 최근 접속 {nac_user} {nac_last}".strip())
    if "사용중" in nac_status and not any(item.startswith("NAC/운영상태") for item in evidence):
        evidence.append("NAC/운영상태 사용중")
    if checkout_user and not any("checkout" in item for item in evidence):
        evidence.append(f"대여/checkout 배정 {checkout_user}")
    if jira_board:
        jira_lane = str(jira_board.get("lane_label") or jira_board.get("lane") or "").strip()
        jira_issue = str(jira_board.get("issue") or "").strip()
        jira_text = " ".join(part for part in ["Jira board", jira_lane, jira_issue] if part).strip()
        if jira_text and not any(item.startswith("Jira board") for item in evidence):
            evidence.append(jira_text)

    has_nac = any(item.startswith("NAC") for item in evidence)
    has_intune = any(item.startswith("Intune") for item in evidence)
    has_checkout = any("checkout" in item for item in evidence)
    if has_nac and has_intune:
        basis = "NAC+Intune 사용 근거"
    elif has_nac:
        basis = "NAC 사용 근거"
    elif has_intune:
        basis = "Intune 사용 근거"
    elif has_checkout:
        basis = "대여/checkout 배정 근거"
    else:
        basis = str(metadata.get("usage_basis") or row.get("usage_reason") or "").strip()
    return basis, " / ".join(evidence)


def jira_board_info(row: dict[str, object]) -> tuple[str, str]:
    try:
        metadata = json.loads(str(row.get("metadata_json") or "{}"))
    except json.JSONDecodeError:
        metadata = {}
    if not isinstance(metadata, dict):
        return "", ""
    board = metadata.get("jira_board") if isinstance(metadata.get("jira_board"), dict) else {}
    return (
        str(board.get("lane_label") or board.get("lane") or "").strip(),
        str(board.get("issue") or "").strip(),
    )


def asset_group(asset_tag: object) -> str:
    tag = str(asset_tag or "").strip().upper()
    parts = [part for part in tag.split("-") if part]
    if len(parts) >= 2 and parts[0] == "VN":
        return f"{parts[0]}-{parts[1]}"
    return "기타" if tag else "미지정"


def clear_active_user_directory_cache() -> None:
    _ACTIVE_USER_DIRECTORY_CACHE["mtime_ns"] = None
    _ACTIVE_USER_DIRECTORY_CACHE["users"] = {}


def owner_lookup_keys(value: object) -> set[str]:
    raw = normalize_text(value).lower()
    if not raw:
        return set()
    keys = {raw}
    if "@" in raw:
        keys.add(raw.split("@", 1)[0])
    if "/" in raw:
        keys.update(part.strip() for part in raw.split("/") if part.strip())
    return keys


def active_user_directory() -> dict[str, dict[str, str]]:
    path = ACTIVE_USER_DIRECTORY_CSV
    try:
        mtime_ns = path.stat().st_mtime_ns
    except OSError:
        clear_active_user_directory_cache()
        return {}
    if _ACTIVE_USER_DIRECTORY_CACHE.get("mtime_ns") == mtime_ns:
        users = _ACTIVE_USER_DIRECTORY_CACHE.get("users")
        return users if isinstance(users, dict) else {}

    users: dict[str, dict[str, str]] = {}
    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
        for row in csv.DictReader(handle):
            if not isinstance(row, dict):
                continue
            normalized = {
                "displayName": normalize_text(row.get("displayName")),
                "userPrincipalName": normalize_text(row.get("userPrincipalName")),
                "mail": normalize_text(row.get("mail")),
                "department": normalize_text(row.get("department")),
                "companyName": normalize_text(row.get("companyName")),
            }
            if not normalized["displayName"] and not normalized["mail"] and not normalized["userPrincipalName"]:
                continue
            for key in (
                owner_lookup_keys(normalized["displayName"])
                | owner_lookup_keys(normalized["userPrincipalName"])
                | owner_lookup_keys(normalized["mail"])
            ):
                users[key] = normalized
    _ACTIVE_USER_DIRECTORY_CACHE["mtime_ns"] = mtime_ns
    _ACTIVE_USER_DIRECTORY_CACHE["users"] = users
    return users


def resolve_owner_identity(owner: object, owner_email: object) -> dict[str, object]:
    owner_text = normalize_text(owner)
    email_text = normalize_text(owner_email)
    if not owner_text and not email_text:
        return {"status": "missing", "label": "미지정", "user": {}}
    users = active_user_directory()
    for key in owner_lookup_keys(email_text) | owner_lookup_keys(owner_text):
        user = users.get(key)
        if user:
            return {"status": "active", "label": "재직", "user": user}
    return {"status": "inactive_or_unknown", "label": "재직 확인필요", "user": {}}


def enrich_asset(row: dict[str, object]) -> dict[str, object]:
    enriched = dict(row)
    basis, evidence = usage_evidence(enriched)
    jira_lane, jira_issue = jira_board_info(enriched)
    number_class = asset_number_class(enriched)
    kind = asset_kind(enriched)
    owner_source = normalize_text(enriched.get("owner"))
    owner_identity = resolve_owner_identity(enriched.get("owner"), enriched.get("owner_email"))
    owner_user = owner_identity.get("user") if isinstance(owner_identity.get("user"), dict) else {}
    if owner_identity.get("status") == "active":
        enriched["owner"] = owner_user.get("displayName") or owner_source
        enriched["owner_email"] = owner_user.get("mail") or owner_user.get("userPrincipalName") or enriched.get("owner_email") or ""
        enriched["department"] = owner_user.get("department") or owner_user.get("companyName") or enriched.get("department") or ""
    enriched["asset_group"] = asset_group(enriched.get("asset_tag"))
    enriched["asset_number_class"] = number_class["key"]
    enriched["asset_number_class_label"] = number_class["label"]
    enriched["asset_number_rollup"] = number_class["rollup"]
    enriched["asset_kind"] = kind
    enriched["asset_kind_label"] = ASSET_KIND_LABELS[kind]
    enriched["jira_lane"] = jira_lane
    enriched["jira_issue"] = jira_issue
    enriched["usage_basis"] = basis or enriched.get("usage_reason") or ""
    enriched["usage_evidence"] = evidence
    enriched["owner_source"] = owner_source
    enriched["owner_status"] = owner_identity.get("status") or "missing"
    enriched["owner_status_label"] = owner_identity.get("label") or "미지정"
    enriched.update(notebook_lifecycle(enriched))
    return enriched


def enrich_assets(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    return [enrich_asset(row) for row in rows]


def truthy_int(value: object) -> bool:
    return str(value or "0").strip() in {"1", "true", "True"}


def is_notebook_asset(row: dict[str, object]) -> bool:
    if truthy_int(row.get("is_server")) or truthy_int(row.get("is_network")):
        return False
    text = " ".join(
        str(row.get(key) or "").lower()
        for key in ("category", "model", "manufacturer", "asset_tag")
    )
    return any(term.lower() in text for term in NOTEBOOK_TERMS)


def parse_asset_date(value: object) -> date | None:
    if value is None:
        return None
    if isinstance(value, (int, float)) and 20_000 <= float(value) <= 80_000:
        return date(1899, 12, 30) + timedelta(days=int(float(value)))
    text = str(value).strip()
    if not text:
        return None
    text = re.sub(r"\.0$", "", text)
    text = re.sub(r"\s+", " ", text)
    patterns = (
        r"(?P<y>20\d{2})[-./년\s]+(?P<m>\d{1,2})[-./월\s]+(?P<d>\d{1,2})",
        r"(?P<y>20\d{2})(?P<m>\d{2})(?P<d>\d{2})",
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if not match:
            continue
        try:
            return date(int(match.group("y")), int(match.group("m")), int(match.group("d")))
        except ValueError:
            return None
    return None


def asset_tag_year_date(asset_tag: object) -> date | None:
    match = re.match(r"^ACME-A\d{2}-(\d{2})", str(asset_tag or "").strip().upper())
    if not match:
        return None
    year = 2000 + int(match.group(1))
    return date(year, 1, 1)


def notebook_acquired_date(row: dict[str, object]) -> tuple[date | None, str]:
    source_row = source_row_for_asset(row)
    keys = (
        "purchase_date",
        "구매일",
        "구매일자",
        "취득일",
        "취득일자",
        "납품일",
        "납품일자",
        "입고일",
        "입고일자",
        "계산서발행일",
        "계산서 발행일",
        "도입일",
        "도입일자",
    )
    for key in keys:
        parsed = parse_asset_date(source_row.get(key))
        if parsed:
            return parsed, key
    parsed = asset_tag_year_date(row.get("asset_tag"))
    if parsed:
        return parsed, "자산번호연도"
    return None, ""


def notebook_refresh_judgment(row: dict[str, object], age_years: float, basis: str) -> dict[str, object]:
    if age_years >= 6:
        priority = "즉시 검토"
        rank = 10
    elif age_years >= 5:
        priority = "교체 계획"
        rank = 20
    else:
        priority = "정기 교체"
        rank = 30

    usage = row.get("usage_status")
    if usage == USAGE_UNUSED:
        decision = "매각 검토"
    elif usage == USAGE_IN_USE:
        decision = "교체 후 회수/매각"
    else:
        decision = "정보 확인"

    flags: list[str] = []
    if usage not in {USAGE_IN_USE, USAGE_UNUSED}:
        flags.append("사용 상태 확인")
    if row.get("owner_status") != "active":
        flags.append("담당 확인")
    if basis == "자산번호연도":
        flags.append("취득일 추정")

    return {
        "notebook_refresh_priority": priority,
        "notebook_refresh_decision": decision,
        "notebook_refresh_reason": f"{age_years:.1f}년 경과 · {basis} 기준",
        "notebook_refresh_flags": " · ".join(flags) if flags else "바로 판단 가능",
        "notebook_refresh_sort_rank": rank,
    }


def notebook_lifecycle(row: dict[str, object], today: date | None = None) -> dict[str, object]:
    if not is_notebook_asset(row):
        return {
            "notebook_acquired_date": "",
            "notebook_age_years": "",
            "notebook_age_basis": "",
            "replacement_due": False,
            "sale_candidate": False,
            "lifecycle_label": "",
            "notebook_refresh_priority": "",
            "notebook_refresh_decision": "",
            "notebook_refresh_reason": "",
            "notebook_refresh_flags": "",
            "notebook_refresh_sort_rank": 99,
        }
    acquired, basis = notebook_acquired_date(row)
    if not acquired:
        return {
            "notebook_acquired_date": "",
            "notebook_age_years": "",
            "notebook_age_basis": "",
            "replacement_due": False,
            "sale_candidate": False,
            "lifecycle_label": "취득일 확인필요",
            "notebook_refresh_priority": "정보 확인",
            "notebook_refresh_decision": "취득일 확인",
            "notebook_refresh_reason": "취득일 없음",
            "notebook_refresh_flags": "취득일 확인",
            "notebook_refresh_sort_rank": 90,
        }
    current = today or datetime.now(timezone.utc).date()
    age_years = max((current - acquired).days / 365.2425, 0)
    due = row.get("usage_status") != USAGE_RETIRED and age_years > NOTEBOOK_REFRESH_YEARS
    result = {
        "notebook_acquired_date": acquired.isoformat(),
        "notebook_age_years": f"{age_years:.1f}",
        "notebook_age_basis": basis,
        "replacement_due": due,
        "sale_candidate": due,
        "lifecycle_label": "교체/매각 대상" if due else "",
        "notebook_refresh_priority": "",
        "notebook_refresh_decision": "",
        "notebook_refresh_reason": "",
        "notebook_refresh_flags": "",
        "notebook_refresh_sort_rank": 99,
    }
    if due:
        result.update(notebook_refresh_judgment(row, age_years, basis))
    return result


def asset_number_class(row: dict[str, object]) -> dict[str, str]:
    group = asset_group(row.get("asset_tag"))
    spec = ASSET_NUMBER_CLASSES.get(group)
    if not spec:
        spec = UNNUMBERED_ASSET_CLASS
    return {key: str(value) for key, value in spec.items()}


def asset_kind(row: dict[str, object]) -> str:
    return asset_number_class(row)["rollup"]


def asset_category_summary(rows: list[dict[str, object]]) -> dict[str, dict[str, object]]:
    result: dict[str, dict[str, object]] = {
        key: {"label": label, "count": 0, "operational": 0, "active": 0, "idle": 0, "retired": 0, "issue": 0}
        for key, label in ASSET_KIND_LABELS.items()
    }
    for row in rows:
        item = result[asset_kind(row)]
        item["count"] = int(item["count"]) + 1
        usage = str(row.get("usage_status") or "")
        if usage == USAGE_IN_USE:
            item["operational"] = int(item.get("operational", 0)) + 1
            item["active"] = int(item["active"]) + 1
        elif usage == USAGE_UNUSED:
            item["operational"] = int(item.get("operational", 0)) + 1
            item["idle"] = int(item["idle"]) + 1
        elif usage == USAGE_RETIRED:
            item["retired"] = int(item.get("retired", 0)) + 1
        else:
            item["operational"] = int(item.get("operational", 0)) + 1
            item["issue"] = int(item["issue"]) + 1
    return result


def asset_number_class_summary(rows: list[dict[str, object]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        label = asset_number_class(row)["label"]
        counts[label] = counts.get(label, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


def summary() -> dict[str, object]:
    with connect() as conn:
        asset_rows = rows_to_dicts(
            conn.execute(
                """
                SELECT asset_tag, category, model, manufacturer, is_server, is_network, usage_status, metadata_json
                FROM assets
                """
            ).fetchall()
        )
        category_summary = asset_category_summary(asset_rows)
        notebook_refresh_count = sum(1 for row in asset_rows if notebook_lifecycle(row)["replacement_due"])
        approval_items = enriched_approval_items()
        jira_only_in_use = len(jira_in_use_overlay_assets())
        db_in_use = conn.execute("SELECT COUNT(*) FROM assets WHERE usage_status = ?", (USAGE_IN_USE,)).fetchone()[0]
        retired = conn.execute("SELECT COUNT(*) FROM assets WHERE usage_status = ?", (USAGE_RETIRED,)).fetchone()[0]
        software_inventory_total = conn.execute("SELECT COUNT(*) FROM software_inventory_summary").fetchone()[0]
        software_install_total = conn.execute("SELECT COUNT(*) FROM software_inventory_installs").fetchone()[0]
        software_user_total = conn.execute(
            """
            SELECT COUNT(*) FROM (
              SELECT COALESCE(NULLIF(TRIM(user_key), ''), NULLIF(TRIM(user_name), ''), '사용자 미확인') AS user_identity
              FROM software_inventory_installs
              GROUP BY user_identity
            )
            """
        ).fetchone()[0]
        return {
            "assets": conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0],
            "asset_groups": len({asset_group(row[0]) for row in conn.execute("SELECT asset_tag FROM assets").fetchall()}),
            "servers": category_summary["server"].get("operational", 0),
            "operational_assets": conn.execute("SELECT COUNT(*) FROM assets WHERE COALESCE(usage_status, '') <> ?", (USAGE_RETIRED,)).fetchone()[0],
            "asset_categories": category_summary,
            "asset_number_classes": asset_number_class_summary(asset_rows),
            "notebook_replacement_due": notebook_refresh_count,
            "notebook_sale_candidates": notebook_refresh_count,
            "in_use": db_in_use + jira_only_in_use,
            "jira_only_in_use": jira_only_in_use,
            "unused": conn.execute("SELECT COUNT(*) FROM assets WHERE usage_status = ?", (USAGE_UNUSED,)).fetchone()[0],
            "retired": retired,
            "needs_review": conn.execute(
                f"SELECT COUNT(*) FROM assets WHERE COALESCE(usage_status, '') NOT IN ({','.join('?' for _ in KNOWN_USAGE_STATUSES)})",
                KNOWN_USAGE_STATUSES,
            ).fetchone()[0],
            "locations": conn.execute("SELECT COUNT(*) FROM locations").fetchone()[0],
            "changes": conn.execute("SELECT COUNT(*) FROM change_history").fetchone()[0],
            "owners": len(owners({})),
            "sources": len(sources({})),
            "software_inventory": software_inventory_total,
            "software_inventory_installs": software_install_total,
            "software_users": software_user_total,
            "approvals": approval_summary(approval_items)["counts"]["my_work"],
            "approval_progress": approval_progress(approval_items),
        }


def insights() -> dict[str, object]:
    with connect() as conn:
        review_assets = rows_to_dicts(
            conn.execute(
                """
                SELECT asset_tag, hostname, category, owner, primary_ip, usage_status, usage_reason, source, updated_at
                FROM assets
                WHERE COALESCE(usage_status, '') NOT IN (?, ?, ?)
                ORDER BY is_server DESC, asset_tag
                LIMIT 12
                """,
                KNOWN_USAGE_STATUSES,
            ).fetchall()
        )
        usage_reasons = rows_to_dicts(
            conn.execute(
                """
                SELECT usage_status, usage_reason, COUNT(*) AS count
                FROM assets
                GROUP BY usage_status, usage_reason
                ORDER BY usage_status, count DESC
                """
            ).fetchall()
        )
    sources_summary = sources({})
    return {
        "summary": summary(),
        "review_assets": review_assets,
        "usage_reasons": usage_reasons,
        "sources": sources_summary,
        "computer_use": computer_use_receipt_status(),
        "ui_build_id": current_ui_build_id(),
    }


def timestamp_age_hours(value: object) -> float | None:
    parsed = parse_timestamp(value)
    if parsed is None:
        return None
    return round((datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds() / 3600, 1)


def query_truthy(query: dict[str, list[str]], name: str) -> bool:
    value = (query.get(name) or [""])[0]
    return str(value).strip().lower() in {"1", "true", "yes", "y", "all"}


def source_record_count(row: dict[str, object]) -> int:
    try:
        return int(row.get("record_count") or 0)
    except (TypeError, ValueError):
        return 0


def hidden_integration_source(row: dict[str, object]) -> bool:
    status = normalize_text(row.get("status")).lower()
    kind = normalize_text(row.get("kind")).lower()
    if status == "not_configured":
        return True
    if kind == "approval-writeback" and status == "local-outbox" and source_record_count(row) == 0:
        return True
    age_hours = timestamp_age_hours(row.get("last_sync_at"))
    return age_hours is not None and age_hours >= SOURCE_HIDE_STALE_HOURS


def quality_tone(status: str, count: int = 0) -> str:
    if status == "ok":
        return "ok"
    if status == "warn":
        return "warn"
    if count:
        return "bad"
    return "ok"


def parse_metadata_json(value: object) -> dict[str, object]:
    try:
        parsed = json.loads(str(value or "{}"))
    except json.JSONDecodeError:
        parsed = {}
    return parsed if isinstance(parsed, dict) else {}


def first_profile_value(*values: object) -> str:
    for value in values:
        text = normalize_text(value)
        if text:
            return text
    return ""


def profile_item(label: str, value: object, source: str = "") -> dict[str, str] | None:
    text = normalize_text(value)
    if not text:
        return None
    item = {"label": label, "value": text}
    if source:
        item["source"] = source
    return item


def append_profile_item(items: list[dict[str, str]], label: str, value: object, source: str = "") -> None:
    item = profile_item(label, value, source)
    if item:
        items.append(item)


def format_mac(value: object) -> str:
    raw = normalize_text(value)
    compacted = re.sub(r"[^0-9A-Fa-f]", "", raw).upper()
    if len(compacted) == 12:
        return ":".join(compacted[index : index + 2] for index in range(0, 12, 2))
    return raw


def normalize_capacity(value: str) -> str:
    match = re.search(r"\b(\d{1,4})\s*(TB|GB|G)\b", value, re.I)
    if not match:
        return normalize_text(value)
    number = match.group(1)
    unit = match.group(2).upper()
    if unit == "G":
        unit = "GB"
    return f"{number}{unit}"


def jira_spec_line(metadata: dict[str, object]) -> str:
    board = metadata.get("jira_board") if isinstance(metadata.get("jira_board"), dict) else {}
    lines = board.get("lines") if isinstance(board.get("lines"), list) else []
    candidates = [normalize_text(line) for line in lines if normalize_text(line)]
    candidates.extend(
        normalize_text(board.get(key))
        for key in ("specification", "spec", "hardware_spec")
        if normalize_text(board.get(key))
    )
    for line in reversed(candidates):
        text = line.lower()
        if any(token in text for token in ("cpu", "ghz", "gb", "tb", "ssd", "ryzen", "xeon", "m1", "m2", "m3", "m4", "m5")):
            return line
    return ""


def parse_hardware_spec(spec: str) -> dict[str, str]:
    text = normalize_text(spec)
    if not text:
        return {}

    result: dict[str, str] = {"raw": text}
    cpu_patterns = (
        r"\b(?:Intel\s+)?(?:Core\s+)?i[3579][-\s]?\d{3,5}[A-Z]*\s*(?:CPU)?(?:\s*@\s*[\d.]+\s*GHz)?",
        r"\bM\d\s?(?:Pro|Max|Ultra)?(?:\s*@\s*(?:CPU\s*)?[\d.]+\s*GHz)?",
        r"\bRyzen\s*\d(?:\s*\d{4,5}[A-Z]*)?(?:\s*@\s*[\d.]+\s*GHz)?",
        r"\bXeon[^\s/,)]*(?:\s*@\s*[\d.]+\s*GHz)?",
    )
    for pattern in cpu_patterns:
        match = re.search(pattern, text, re.I)
        if match:
            result["cpu"] = re.sub(r"\s+", " ", match.group(0)).strip()
            break

    parts = [part.strip() for part in re.split(r"[/|,]", text) if part.strip()]
    if not parts:
        parts = [text]

    for part in parts:
        lowered = part.lower()
        match = re.search(r"\b(\d{1,3})\s*(?:gb|g)\b", part, re.I)
        if match and int(match.group(1)) <= 128 and "ssd" not in lowered and "hdd" not in lowered:
            result.setdefault("ram", normalize_capacity(part))
            continue
        storage_match = re.search(r"\b(\d{3,4})\s*(?:gb|g)\b|\b\d{1,2}\s*tb\b", part, re.I)
        if storage_match or "ssd" in lowered or "hdd" in lowered:
            result.setdefault("storage", normalize_capacity(part))

    return result

def source_row_for_asset(asset: dict[str, object]) -> dict[str, object]:
    metadata = parse_metadata_json(asset.get("metadata_json"))
    source_row = metadata.get("source_row") if isinstance(metadata.get("source_row"), dict) else {}
    if not source_row and isinstance(metadata.get("usage_source_row"), dict):
        source_row = metadata["usage_source_row"]
    return source_row if isinstance(source_row, dict) else {}


def endpoint_profile(asset: dict[str, object]) -> dict[str, list[dict[str, str]]]:
    metadata = parse_metadata_json(asset.get("metadata_json"))
    source_row = source_row_for_asset(asset)

    board = metadata.get("jira_board") if isinstance(metadata.get("jira_board"), dict) else {}
    spec = parse_hardware_spec(jira_spec_line(metadata))
    intune_os = " ".join(
        value
        for value in [
            first_profile_value(source_row.get("intune_operating_system"), asset.get("os")),
            first_profile_value(source_row.get("intune_os_version")),
        ]
        if value
    )
    wireless_spec = first_profile_value(
        source_row.get("nac_wifi_standard"),
        source_row.get("wifi_standard"),
        source_row.get("wireless_standard"),
        source_row.get("wlan_standard"),
        source_row.get("network_adapter"),
        source_row.get("wifi_adapter"),
        source_row.get("wireless_adapter"),
        source_row.get("wlan_adapter"),
    )

    identity: list[dict[str, str]] = []
    append_profile_item(identity, "자산번호", asset.get("asset_tag"), "Asset DB")
    append_profile_item(identity, "시리얼", first_profile_value(asset.get("serial"), source_row.get("ledger_serial"), source_row.get("intune_serial")), "Asset DB")
    append_profile_item(identity, "Intune 시리얼", source_row.get("intune_serial"), "Intune")
    append_profile_item(identity, "제조사", first_profile_value(asset.get("manufacturer"), source_row.get("ledger_manufacturer"), source_row.get("intune_manufacturer")), "Asset DB")
    append_profile_item(identity, "모델", first_profile_value(asset.get("model"), source_row.get("ledger_model"), source_row.get("intune_model")), "Asset DB")
    append_profile_item(identity, "Jira 이슈", board.get("issue"), "Jira")

    hardware: list[dict[str, str]] = []
    append_profile_item(hardware, "CPU", spec.get("cpu"), "Jira")
    append_profile_item(hardware, "RAM", spec.get("ram"), "Jira")
    append_profile_item(hardware, "저장장치", spec.get("storage"), "Jira")
    append_profile_item(hardware, "스펙 원문", spec.get("raw"), "Jira")

    network: list[dict[str, str]] = []
    append_profile_item(network, "Wi-Fi MAC", format_mac(source_row.get("intune_wifi_mac")), "Intune")
    append_profile_item(network, "무선 규격", wireless_spec or ("NAC/Intune 수집 필드 없음" if source_row.get("intune_wifi_mac") or source_row.get("nac_asset_tag") else ""), "NAC")
    append_profile_item(network, "NAC 자산번호", source_row.get("nac_asset_tag"), "NAC")
    append_profile_item(network, "NAC 상태", source_row.get("nac_status"), "NAC")
    append_profile_item(network, "NAC 사용자", first_profile_value(source_row.get("nac_username_norm"), source_row.get("nac_username")), "NAC")
    append_profile_item(network, "NAC 최근 접속", source_row.get("nac_last_seen_utc"), "NAC")
    append_profile_item(network, "NAC 최근 사용", source_row.get("nac_recently_used") or source_row.get("recent_seen_nac_14d"), "NAC")

    management: list[dict[str, str]] = []
    append_profile_item(management, "Intune OS", intune_os, "Intune")
    append_profile_item(management, "Intune 최근 동기화", source_row.get("intune_last_sync_utc"), "Intune")
    append_profile_item(management, "Intune 사용자", source_row.get("intune_user_upn"), "Intune")
    append_profile_item(management, "Intune 준수 상태", source_row.get("intune_compliance_state"), "Intune")
    append_profile_item(management, "암호화", source_row.get("intune_is_encrypted"), "Intune")
    append_profile_item(management, "관리 에이전트", source_row.get("intune_management_agent"), "Intune")

    return {
        "identity": identity,
        "hardware": hardware,
        "network": network,
        "management": management,
    }


JIRA_MISSING_STATUS_LABELS = {
    "unreviewed": "미검토",
    "register_needed": "자산 등록 필요",
    "jira_stale": "Jira stale 정리",
    "blocked": "보류",
    "resolved": "처리 완료",
}


def load_jira_missing_workflows() -> dict[str, object]:
    payload = read_json_file(JIRA_MISSING_WORKFLOWS, {"version": 1, "records": {}})
    return payload if isinstance(payload, dict) else {"version": 1, "records": {}}


def save_jira_missing_workflows(payload: dict[str, object]) -> None:
    write_json_file(JIRA_MISSING_WORKFLOWS, payload)


def jira_missing_workflow_record(asset_tag: str) -> dict[str, object]:
    payload = load_jira_missing_workflows()
    records = payload.get("records") if isinstance(payload.get("records"), dict) else {}
    record = records.get(asset_tag) if isinstance(records.get(asset_tag), dict) else {}
    return record


def enrich_jira_missing_workflow(row: dict[str, object]) -> dict[str, object]:
    asset_tag = normalize_text(row.get("asset_tag"))
    workflow = jira_missing_workflow_record(asset_tag)
    status = normalize_text(workflow.get("status")) or "unreviewed"
    row["workflow_status"] = status
    row["workflow_label"] = JIRA_MISSING_STATUS_LABELS.get(status, status)
    row["workflow_note"] = normalize_text(workflow.get("note"))
    row["workflow_updated_at"] = normalize_text(workflow.get("updatedAt"))
    row["workflow_actor"] = normalize_text(workflow.get("actor"))
    row["next_action"] = JIRA_MISSING_STATUS_LABELS.get(status, "확인 필요")
    return row


def update_jira_missing_workflow(payload: dict[str, object]) -> dict[str, object]:
    asset_tag = normalize_text(payload.get("asset_tag") or payload.get("assetTag")).upper()
    if not asset_tag:
        raise ValueError("asset_tag required")
    status = normalize_text(payload.get("status")) or "unreviewed"
    if status not in JIRA_MISSING_STATUS_LABELS:
        raise ValueError("invalid status")
    note = normalize_text(payload.get("note"))[:1000]
    actor = normalize_text(payload.get("actor"))[:80] or "operator-ui"
    now = utc_now()
    workflows = load_jira_missing_workflows()
    records = workflows.setdefault("records", {})
    existing = records.get(asset_tag) if isinstance(records.get(asset_tag), dict) else {}
    history = existing.get("history") if isinstance(existing.get("history"), list) else []
    record = {
        "asset_tag": asset_tag,
        "status": status,
        "label": JIRA_MISSING_STATUS_LABELS[status],
        "note": note,
        "actor": actor,
        "updatedAt": now,
        "history": history[-49:] + [
            {
                "id": str(uuid.uuid4()),
                "at": now,
                "actor": actor,
                "status": status,
                "note": note,
            }
        ],
    }
    records[asset_tag] = record
    workflows["updatedAt"] = now
    save_jira_missing_workflows(workflows)
    return {"passed": True, "record": record}


def jira_missing_assets(query: dict[str, list[str]]) -> list[dict[str, object]]:
    search = (query.get("q") or [""])[0].strip().lower()
    with connect() as conn:
        row = conn.execute(
            """
            SELECT status, last_sync_at, metadata_json
            FROM integration_sources
            WHERE id = 'jira-api'
            """
        ).fetchone()
    if not row:
        return []

    metadata = parse_metadata_json(row["metadata_json"])
    raw_records = metadata.get("missing_records")
    records: list[dict[str, object]] = []
    if isinstance(raw_records, list):
        for item in raw_records:
            if not isinstance(item, dict):
                continue
            asset_tag = str(item.get("asset_tag") or "").strip()
            if not asset_tag:
                continue
            records.append(
                {
                    "asset_tag": asset_tag,
                    "jira_issue": str(item.get("issue") or "").strip(),
                    "jira_lane": str(item.get("lane") or "").strip(),
                    "jira_lane_label": str(item.get("lane_label") or item.get("lane") or "").strip(),
                    "jira_owner": str(item.get("owner") or "").strip(),
                    "jira_summary": str(item.get("summary") or "").strip(),
                    "manufacturer": str(item.get("manufacturer") or "").strip(),
                    "model": str(item.get("model") or "").strip(),
                    "specification": str(item.get("specification") or "").strip(),
                    "lines": item.get("lines") if isinstance(item.get("lines"), list) else [],
                    "jira_page": item.get("page"),
                    "source_file": str(item.get("source_file") or "").strip(),
                    "source_status": row["status"],
                    "last_sync_at": row["last_sync_at"] or "",
                }
            )

    if not records:
        raw_assets = metadata.get("missing_assets")
        if isinstance(raw_assets, list):
            for asset_tag in raw_assets:
                tag = str(asset_tag or "").strip()
                if not tag:
                    continue
                records.append(
                    {
                        "asset_tag": tag,
                        "jira_issue": "",
                        "jira_lane": "",
                        "jira_lane_label": "",
                        "jira_owner": "",
                        "jira_summary": "",
                        "manufacturer": "",
                        "model": "",
                        "specification": "",
                        "lines": [],
                        "jira_page": None,
                        "source_file": "",
                        "source_status": row["status"],
                        "last_sync_at": row["last_sync_at"] or "",
                    }
                )

    if records:
        with connect() as conn:
            existing_tags = {
                normalize_text(row[0])
                for row in conn.execute("SELECT asset_tag FROM assets").fetchall()
            }
        records = [
            item for item in records
            if normalize_text(item.get("asset_tag")) not in existing_tags
        ]

    records = [enrich_jira_missing_workflow(item) for item in records]

    if search:
        records = [
            item for item in records
            if search in " ".join(str(value or "") for value in item.values()).lower()
        ]
    return sorted(records, key=lambda item: str(item.get("asset_tag") or ""))


def jira_missing_asset(asset_tag: str) -> dict[str, object] | None:
    target = normalize_text(asset_tag).lower()
    if not target:
        return None
    for row in jira_missing_assets({"q": [target]}):
        if normalize_text(row.get("asset_tag")).lower() == target:
            return row
    return None


def jira_missing_record_is_in_use(record: dict[str, object]) -> bool:
    text = " ".join(
        normalize_text(record.get(key))
        for key in ("jira_lane", "jira_lane_label")
    ).lower()
    return "inprogress" in text or "in progress" in text or "사용중" in text


def jira_missing_asset_overlay(record: dict[str, object]) -> dict[str, object]:
    asset_tag = normalize_text(record.get("asset_tag"))
    jira_issue = normalize_text(record.get("jira_issue"))
    jira_lane = normalize_text(record.get("jira_lane"))
    jira_lane_label = normalize_text(record.get("jira_lane_label"))
    owner = normalize_text(record.get("jira_owner"))
    manufacturer = normalize_text(record.get("manufacturer"))
    model = normalize_text(record.get("model"))
    specification = normalize_text(record.get("specification"))
    summary_text = normalize_text(record.get("jira_summary"))
    purpose = specification or summary_text or jira_lane_label or jira_lane
    metadata = {
        "jira_board": {
            "issue": jira_issue,
            "lane": jira_lane,
            "lane_label": jira_lane_label,
            "owner": owner,
            "summary": summary_text,
            "manufacturer": manufacturer,
            "model": model,
            "specification": specification,
            "source_file": normalize_text(record.get("source_file")),
            "source_status": normalize_text(record.get("source_status")),
        },
        "jira_missing": record,
        "read_only": True,
    }
    return {
        "id": f"jira-missing:{asset_tag}",
        "source": "jira-api",
        "external_id": jira_issue,
        "asset_tag": asset_tag,
        "hostname": "",
        "serial": "",
        "model": model,
        "manufacturer": manufacturer,
        "category": "Jira-only",
        "status": "read-only",
        "owner": owner,
        "owner_email": "",
        "department": "",
        "primary_ip": "",
        "os": "",
        "location": "",
        "room": "",
        "rack": "",
        "rack_unit": "",
        "maintenance_date": "",
        "usage_status": "사용중",
        "usage_reason": "Jira board 사용중(read-only)",
        "purpose": purpose,
        "ports": "",
        "is_server": 0,
        "is_virtual": 0,
        "is_network": 0,
        "metadata_json": json.dumps(metadata, ensure_ascii=False),
        "updated_at": normalize_text(record.get("last_sync_at")),
    }


def jira_in_use_overlay_assets(
    search: str = "",
    only_servers: bool = False,
    asset_prefix: str = "",
    kind: str = "",
    asset_class: str = "",
    owner: str = "",
    duplicate_asset_tags_only: bool = False,
    duplicate_serials_only: bool = False,
) -> list[dict[str, object]]:
    if duplicate_asset_tags_only or duplicate_serials_only:
        return []
    search_lower = search.lower()
    rows: list[dict[str, object]] = []
    for record in jira_missing_assets({}):
        if not jira_missing_record_is_in_use(record):
            continue
        row = enrich_asset(jira_missing_asset_overlay(record))
        if only_servers and row.get("asset_kind") != "server":
            continue
        if asset_class and row.get("asset_number_class") != asset_class:
            continue
        if kind and row.get("asset_kind") != kind:
            continue
        if owner and normalize_text(row.get("owner")) != owner:
            continue
        if asset_prefix:
            tag = normalize_text(row.get("asset_tag")).upper()
            if tag != asset_prefix and not tag.startswith(f"{asset_prefix}-"):
                continue
        if search_lower:
            haystack = " ".join(str(value or "") for value in row.values()).lower()
            if search_lower not in haystack:
                continue
        rows.append(row)
    return rows


def data_quality() -> dict[str, object]:
    with connect() as conn:
        asset_total = conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0]
        change_total = conn.execute("SELECT COUNT(*) FROM change_history").fetchone()[0]
        latest_change_at = conn.execute("SELECT MAX(changed_at) FROM change_history").fetchone()[0]
        duplicate_asset_tags = conn.execute(
            f"""
            SELECT COUNT(*) FROM (
              {duplicate_asset_tag_conflict_sql()}
            )
            """
        ).fetchone()[0]
        duplicate_serials = conn.execute(
            """
            SELECT COUNT(*) FROM (
              SELECT serial
              FROM assets
              WHERE COALESCE(TRIM(serial), '') <> ''
                AND TRIM(serial) NOT IN ('-', '조립', '조립PC', 'N/A', 'n/a')
              GROUP BY serial
              HAVING COUNT(*) > 1
            )
            """
        ).fetchone()[0]
        conflicts = conn.execute(
            """
            SELECT COUNT(*) FROM assets
            WHERE usage_status = '확인필요'
              AND COALESCE(usage_reason, '') LIKE '%Jira/NAC-Intune%'
            """
        ).fetchone()[0]
        review_assets = conn.execute(
            """
            SELECT COUNT(*) FROM assets
            WHERE COALESCE(usage_status, '') NOT IN (?, ?, ?)
            """,
            KNOWN_USAGE_STATUSES,
        ).fetchone()[0]
        missing_owner = conn.execute(
            """
            SELECT COUNT(*) FROM assets
            WHERE COALESCE(TRIM(owner), '') = ''
               OR TRIM(owner) IN ('-', '미확인', '없음', 'N/A', 'n/a')
            """
        ).fetchone()[0]
        owner_rows = rows_to_dicts(
            conn.execute(
                """
                SELECT owner, owner_email FROM assets
                WHERE COALESCE(TRIM(owner), '') <> ''
                  AND TRIM(owner) NOT IN ('-', '미확인', '없음', 'N/A', 'n/a')
                """
            ).fetchall()
        )
        inactive_owner = sum(
            1 for row in owner_rows
            if resolve_owner_identity(row.get("owner"), row.get("owner_email")).get("status") == "inactive_or_unknown"
        )
        repeated_jira_today = conn.execute(
            """
            SELECT COUNT(*) FROM (
              SELECT asset_tag, field, old_value, new_value
              FROM change_history
              WHERE source = 'jira-api'
                AND datetime(changed_at) >= datetime('now','+9 hours','start of day','-9 hours')
              GROUP BY asset_tag, field, old_value, new_value
              HAVING COUNT(*) > 1
            )
            """
        ).fetchone()[0]
        software_inventory_total = conn.execute("SELECT COUNT(*) FROM software_inventory_summary").fetchone()[0]
        software_unlicensed_installs = int(conn.execute(
            "SELECT COALESCE(SUM(illegal_install_count), 0) FROM software_inventory_summary"
        ).fetchone()[0] or 0)
        software_unlicensed_titles = conn.execute(
            "SELECT COUNT(*) FROM software_inventory_summary WHERE illegal_install_count > 0"
        ).fetchone()[0]
        software_user_total = conn.execute(
            """
            SELECT COUNT(*) FROM (
              SELECT COALESCE(NULLIF(TRIM(user_key), ''), NULLIF(TRIM(user_name), ''), '사용자 미확인') AS user_identity
              FROM software_inventory_installs
              GROUP BY user_identity
            )
            """
        ).fetchone()[0]
        latest_software_import = conn.execute("SELECT MAX(imported_at) FROM software_inventory_runs").fetchone()[0]
    jira_missing = jira_missing_assets({})
    source_rows = sources({"include_hidden": ["1"]})
    visible_source_rows = sources({})
    source_items: list[dict[str, object]] = []
    stale_sources = 0
    for row in source_rows:
        age_hours = timestamp_age_hours(row.get("last_sync_at"))
        is_stale = age_hours is not None and age_hours >= 168
        if is_stale:
            stale_sources += 1
        source_items.append(
            {
                "id": row.get("id") or "",
                "name": row.get("name") or "",
                "kind": row.get("kind") or "",
                "status": row.get("status") or "",
                "record_count": row.get("record_count"),
                "last_sync_at": row.get("last_sync_at") or "",
                "age_hours": age_hours,
                "stale": is_stale,
            }
        )
    software_rows = software_expirations({})
    software_expiring = sum(1 for row in software_rows if row.get("expiry_status") in {"expired", "expiring_soon"})
    partial_assets = asset_total > ASSET_API_LIMIT
    partial_changes = change_total > CHANGE_API_DEFAULT_LIMIT
    return {
        "timezone": KST_ZONE_NAME,
        "generated_at": kst_now(),
        "cards": [
            {
                "id": "partial_assets",
                "label": "자산 목록",
                "value": f"{asset_total:,}개",
                "detail": "전체 자산 수",
                "tone": "warn" if partial_assets else "ok",
                "target_mode": "assets",
            },
            {
                "id": "partial_changes",
                "label": "변경 이력",
                "value": f"{change_total:,}건",
                "detail": "전체 변경 기록",
                "tone": "warn" if partial_changes else "ok",
                "target_mode": "changes",
            },
            {
                "id": "software_expirations",
                "label": "SW 만료일",
                "value": f"{software_expiring:,}",
                "detail": "30일 내/만료" if software_expiring else "긴급 만료 없음",
                "tone": "warn" if software_expiring else "ok",
                "target_mode": "software_expirations",
            },
            {
                "id": "software_inventory",
                "label": "SW 설치",
                "value": f"{software_inventory_total:,}",
                "detail": f"라이선스 확인 {software_unlicensed_installs:,}건" if software_unlicensed_installs else "라이선스 확인 없음",
                "tone": "warn" if software_unlicensed_installs else "ok",
                "target_mode": "software_inventory",
            },
        ],
        "counts": {
            "assets": asset_total,
            "asset_api_limit": ASSET_API_LIMIT,
            "changes": change_total,
            "change_api_limit": CHANGE_API_DEFAULT_LIMIT,
            "latest_change_at": latest_change_at or "",
            "stale_sources": stale_sources,
            "visible_sources": len(visible_source_rows),
            "software_expirations": len(software_rows),
            "software_expiring_soon": software_expiring,
            "software_inventory": software_inventory_total,
            "software_api_limit": None,
            "software_users": software_user_total,
            "software_unlicensed_installs": software_unlicensed_installs,
            "software_unlicensed_titles": software_unlicensed_titles,
            "software_inventory_latest_import": latest_software_import or "",
            "duplicate_asset_tags": duplicate_asset_tags,
            "duplicate_serials": duplicate_serials,
            "review_assets": review_assets,
            "jira_nac_conflicts": conflicts,
            "jira_missing_assets": len(jira_missing),
            "missing_owner": missing_owner,
            "inactive_owner": inactive_owner,
            "repeated_jira_today": repeated_jira_today,
        },
        "triage": [
            {
                "label": "Jira/NAC 충돌",
                "count": conflicts,
                "tone": quality_tone("bad", conflicts),
                "target_mode": "needs_review",
                "description": "사용 판정이 서로 충돌하는 자산",
            },
            {
                "label": "Jira-only 자산",
                "count": len(jira_missing),
                "tone": quality_tone("bad", len(jira_missing)),
                "target_mode": "jira_missing",
                "description": "Jira에는 있으나 현재 자산 테이블에는 없는 자산번호",
            },
            {
                "label": "소스 stale",
                "count": stale_sources,
                "tone": quality_tone("warn" if stale_sources else "ok", stale_sources),
                "target_mode": "sources",
                "target_filter": {"include_hidden": "1"},
                "description": "7일 이상 갱신되지 않은 연동",
            },
            {
                "label": "SW 만료 임박",
                "count": software_expiring,
                "tone": quality_tone("warn" if software_expiring else "ok", software_expiring),
                "target_mode": "software_expirations",
                "description": "만료되었거나 30일 이내 만료되는 소프트웨어/구독",
            },
            {
                "label": "SW 라이선스 확인",
                "count": software_unlicensed_installs,
                "tone": quality_tone("warn" if software_unlicensed_installs else "ok", software_unlicensed_installs),
                "target_mode": "software_inventory",
                "description": "Sweeper 원천 is_licensed=0 항목: 원장 대조 전 위반 확정 아님",
            },
            {
                "label": "담당 재직 확인",
                "count": inactive_owner,
                "tone": quality_tone("warn" if inactive_owner else "ok", inactive_owner),
                "target_mode": "owners",
                "description": "active 사용자 디렉터리와 매칭되지 않는 담당자 표기",
            },
            {
                "label": "중복 자산번호",
                "count": duplicate_asset_tags,
                "tone": quality_tone("bad", duplicate_asset_tags),
                "target_mode": "assets",
                "target_filter": {"dup_tag": "1"},
                "description": "같은 자산번호가 서로 다른 시리얼과 연결된 자산",
            },
            {
                "label": "Jira 반복 변경",
                "count": repeated_jira_today,
                "tone": quality_tone("warn" if repeated_jira_today else "ok", repeated_jira_today),
                "target_mode": "changes",
                "description": "오늘 같은 old/new 변경이 반복 기록된 항목",
            },
        ],
        "sources": source_items,
    }

NOT_MY_SCOPE_RULES = [
    (
        "AWS/서버/클라우드",
        [
            "aws",
            "ec2",
            "s3",
            "elasticache",
            "인스턴스",
            "스냅샷",
            "백업 주기",
            "서버 용량",
            "버킷",
            "클라우드",
        ],
    ),
    (
        "SharePoint/M365 용량·권한",
        ["sharepoint", "share point", "onedrive", "one drive", "m365", "microsoft 365", "용량 증설"],
    ),
    ("IT자산 반입·반출", ["반입", "반출"]),
]

MY_SCOPE_KEYWORDS = [
    "sw",
    "software",
    "소프트웨어",
    "라이선스",
    "license",
    "claude",
    "chatgpt",
    "구독형",
    "계정사용",
    "한컴",
    "자산",
    "대여",
    "구매",
    "수리",
]


def normalize_text(value: object) -> str:
    return str(value or "").strip()


def approval_blob(item: dict[str, object]) -> str:
    return " ".join(
        normalize_text(item.get(key))
        for key in ["docTitle", "docNo", "formName", "createdBy", "deptName", "sourceLabel", "needsActionReason"]
    ).lower()


def classify_approval(item: dict[str, object]) -> dict[str, str]:
    text = approval_blob(item)
    for theme, keywords in NOT_MY_SCOPE_RULES:
        if any(keyword.lower() in text for keyword in keywords):
            return {
                "bucket": "내 업무 아님",
                "theme": theme,
                "action": "이관/참조",
                "reason": "정책상 담당 범위 제외",
            }
    if any(keyword.lower() in text for keyword in MY_SCOPE_KEYWORDS):
        return {
            "bucket": "내가 처리",
            "theme": "SW/라이선스/자산",
            "action": "직접 처리",
            "reason": "담당 키워드 일치",
        }
    return {
        "bucket": "내가 1차 확인",
        "theme": "확인필요",
        "action": "검토 후 처리/이관",
        "reason": "자동 분류 불충분",
    }


def approval_source_status(item: dict[str, object]) -> dict[str, object]:
    source = normalize_text(item.get("sourceBoxCode"))
    source_label = normalize_text(item.get("sourceLabel"))
    if source == "120":
        oper = normalize_text(item.get("operYn")).upper()
        read = normalize_text(item.get("readYn")).upper()
        done = oper == "Y"
        touched = read == "Y"
        if done:
            label = "시행 완료"
        elif touched:
            label = "열람 후 미시행"
        else:
            label = "미열람·미시행"
        return {
            "type": "dispatch",
            "label": label,
            "done": done,
            "touched": touched,
            "basis": f"{source_label or '시행함'} · readYn={read or '-'} · operYn={oper or '-'}",
        }
    if source == "60":
        read = normalize_text(item.get("readYn")).upper()
        done = read == "Y"
        return {
            "type": "reference",
            "label": "열람 완료" if done else "미열람",
            "done": done,
            "touched": done,
            "basis": f"{source_label or '수신참조함'} · readYn={read or '-'}",
        }
    label = normalize_text(item.get("docStatus")) or "-"
    return {"type": "document", "label": label, "done": False, "touched": False, "basis": label}


def approval_status(item: dict[str, object]) -> str:
    return str(approval_source_status(item).get("label") or "-")


def approval_external_url(item: dict[str, object]) -> str:
    doc_id = normalize_text(item.get("docId") or item.get("id"))
    form_id = normalize_text(item.get("formId"))
    menu_id = normalize_text(item.get("menuId"))
    source = normalize_text(item.get("sourceBoxCode"))
    doc_auth = normalize_text(item.get("docAuth")) or "0"
    values = {
        "doc_id": doc_id,
        "docId": doc_id,
        "docID": doc_id,
        "form_id": form_id,
        "formId": form_id,
        "menu_id": menu_id,
        "menuId": menu_id,
        "box_code": source,
        "sourceBoxCode": source,
        "doc_auth": doc_auth,
        "docAuth": doc_auth,
    }
    if APPROVAL_DETAIL_URL_TEMPLATE:
        try:
            return APPROVAL_DETAIL_URL_TEMPLATE.format(**values)
        except KeyError:
            pass
    if not APPROVAL_HOME_URL:
        return ""
    if doc_id and form_id:
        params = urlencode(
            {
                "docID": doc_id,
                "formId": form_id,
                "docAuth": doc_auth,
                "MicroModuleCode": "eap",
                "callComp": "UBAP002",
                "popupUUID": str(uuid.uuid4()),
            }
        )
        return f"{APPROVAL_HOME_URL}/#popup?{params}"
    params = urlencode({"menuId": menu_id, "docId": doc_id, "boxCode": source})
    return f"{APPROVAL_HOME_URL}/?{params}"


def load_approval_snapshot() -> dict[str, object]:
    payload = read_json_file(APPROVAL_SNAPSHOT, {})
    if not isinstance(payload, dict):
        return {}
    return payload


def load_approval_workflows() -> dict[str, object]:
    payload = read_json_file(APPROVAL_WORKFLOWS, {"version": 2, "records": {}})
    if not isinstance(payload, dict):
        return {"version": 2, "records": {}}
    if not isinstance(payload.get("records"), dict):
        payload["records"] = {}
    payload.setdefault("version", 2)
    return payload


def load_approval_details() -> dict[str, object]:
    payload = read_json_file(APPROVAL_DETAILS, {"items": {}})
    if not isinstance(payload, dict):
        return {"items": {}}
    if not isinstance(payload.get("items"), dict):
        payload["items"] = {}
    return payload


def save_approval_workflows(payload: dict[str, object]) -> None:
    write_json_file(APPROVAL_WORKFLOWS, payload)


def approval_item_id(item: dict[str, object]) -> str:
    return normalize_text(item.get("docId")) or normalize_text(item.get("id"))


def enriched_approval_items() -> list[dict[str, object]]:
    snapshot = load_approval_snapshot()
    workflows = load_approval_workflows().get("records", {})
    items = snapshot.get("items") if isinstance(snapshot.get("items"), list) else []
    enriched: list[dict[str, object]] = []
    for raw in items:
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        classification = classify_approval(item)
        doc_id = approval_item_id(item)
        workflow = workflows.get(doc_id) if isinstance(workflows, dict) else None
        item.update(classification)
        item["docId"] = doc_id
        source_status = approval_source_status(item)
        item["sourceStatus"] = source_status
        item["statusLabel"] = source_status["label"]
        item["externalUrl"] = approval_external_url(item)
        item["localStatus"] = normalize_text(workflow.get("localStatus")) if isinstance(workflow, dict) else ""
        item["workflowUpdatedAt"] = normalize_text(workflow.get("updatedAt")) if isinstance(workflow, dict) else ""
        enriched.append(item)
    return enriched


def approval_summary(items: list[dict[str, object]]) -> dict[str, object]:
    counts: dict[str, int] = {
        "total": len(items),
        "dispatch_pending": 0,
        "dispatch_read_pending": 0,
        "dispatch_done": 0,
        "reference_unread": 0,
        "reference_read": 0,
        "source_completed": 0,
        "my_work": 0,
        "not_my_scope": 0,
        "first_review": 0,
    }
    themes: dict[str, int] = {}
    for item in items:
        source_status = approval_source_status(item)
        source = normalize_text(item.get("sourceBoxCode"))
        if source == "120":
            if source_status.get("done"):
                counts["dispatch_done"] += 1
            else:
                counts["dispatch_pending"] += 1
                if source_status.get("touched"):
                    counts["dispatch_read_pending"] += 1
        if source == "60":
            if source_status.get("done"):
                counts["reference_read"] += 1
            else:
                counts["reference_unread"] += 1
        if source_status.get("done"):
            counts["source_completed"] += 1
        bucket = normalize_text(item.get("bucket"))
        if bucket == "내 업무 아님":
            counts["not_my_scope"] += 1
        elif bucket == "내가 1차 확인":
            counts["first_review"] += 1
        else:
            counts["my_work"] += 1
        theme = normalize_text(item.get("theme")) or "미분류"
        themes[theme] = themes.get(theme, 0) + 1
    return {"counts": counts, "themes": themes}


def approval_progress(items: list[dict[str, object]]) -> dict[str, int]:
    work_items = [item for item in items if normalize_text(item.get("bucket")) == "내가 처리"]
    completed = 0
    in_progress = 0
    waiting = 0
    unread = 0
    dispatch_pending = 0
    reference_unread = 0
    for item in work_items:
        source_status = approval_source_status(item)
        source = normalize_text(item.get("sourceBoxCode"))
        if source_status.get("done"):
            completed += 1
        elif source_status.get("touched"):
            in_progress += 1
        else:
            waiting += 1
            unread += 1
        if source == "120" and not source_status.get("done"):
            dispatch_pending += 1
        if source == "60" and not source_status.get("done"):
            reference_unread += 1
    total = len(work_items)
    percent = round((completed / total) * 100) if total else 0
    touched_percent = round(((completed + in_progress) / total) * 100) if total else 0
    return {
        "completed": completed,
        "in_progress": in_progress,
        "waiting": waiting,
        "unread": unread,
        "dispatch_pending": dispatch_pending,
        "reference_unread": reference_unread,
        "total": total,
        "percent": percent,
        "touched_percent": touched_percent,
    }


def approvals(query: dict[str, list[str]]) -> dict[str, object]:
    filter_name = (query.get("filter") or ["my"])[0].strip() or "my"
    search = (query.get("q") or [""])[0].strip().lower()
    items = enriched_approval_items()
    if filter_name == "pending":
        items = [item for item in items if normalize_text(item.get("sourceBoxCode")) == "120" and not approval_source_status(item).get("done")]
    elif filter_name == "reference":
        items = [item for item in items if normalize_text(item.get("sourceBoxCode")) == "60"]
    elif filter_name == "unread":
        items = [item for item in items if normalize_text(item.get("sourceBoxCode")) == "60" and normalize_text(item.get("readYn")).upper() == "N"]
    elif filter_name == "not_my_scope":
        items = [item for item in items if item.get("bucket") == "내 업무 아님"]
    elif filter_name == "review":
        items = [item for item in items if item.get("bucket") == "내가 1차 확인"]
    elif filter_name == "my":
        items = [item for item in items if item.get("bucket") != "내 업무 아님"]
    if search:
        items = [item for item in items if search in approval_blob(item)]
    items.sort(key=lambda item: normalize_text(item.get("repDt")), reverse=True)
    snapshot = load_approval_snapshot()
    return {
        "owner": snapshot.get("owner") if isinstance(snapshot.get("owner"), dict) else {},
        "generated_at_utc": snapshot.get("generated_at_utc") or "",
        "lastSyncedAt": snapshot.get("lastSyncedAt") or snapshot.get("generated_at_utc") or "",
        "summary": approval_summary(enriched_approval_items()),
        "items": items[:500],
    }


SOFTWARE_NAME_HINTS = [
    "Claude",
    "ChatGPT",
    "OpenAI API",
    "Github",
    "GitHub",
    "Docker",
    "Cursor",
    "Notion",
    "Overleaf",
    "Zoom",
    "Slack",
    "Sharepoint",
    "SharePoint",
    "Windows VDA",
    "Windows",
    "Microsoft",
    "Adobe",
    "Adobe Acrobat",
    "Datadog",
    "W&B",
    "한컴",
    "한컴오피스",
    "V3Lite",
    "PurePaste",
    "Day Progress",
]

SOFTWARE_CANDIDATE_MARKERS = [
    "소프트웨어 구매 요청서",
    "소프트웨어 사용 승인",
    "구독형 서비스 계정사용",
    "계정 연장 신청",
    "라이선스",
    "license",
    "서비스 이용료",
    "구독",
    "약정",
]

SOFTWARE_NOISE_HEADLINE_MARKERS = [
    "월간 점검",
    "재해복구 훈련",
    "저장매체 사용",
    "정보시스템 권한 신청",
    "네트워크 장비",
]


def detail_for_doc(doc_id: str) -> dict[str, object]:
    detail = load_approval_details().get("items", {}).get(doc_id)
    return detail if isinstance(detail, dict) else {}


def value_after_label(lines: list[str], labels: set[str]) -> str:
    for idx, line in enumerate(lines):
        if line.strip() in labels:
            for value in lines[idx + 1 : idx + 4]:
                cleaned = normalize_text(value)
                if cleaned and cleaned not in labels and not re.fullmatch(r"[. ]+", cleaned):
                    return cleaned
    return ""


def parse_approval_deadline(text: str) -> tuple[str, str]:
    deadline_text = ""
    for marker in ("사용기한", "사용기간", "종료일자", "만료일"):
        idx = text.rfind(marker)
        if idx >= 0:
            deadline_text = text[idx : idx + 300]
            break
    if not deadline_text:
        if "영구" in text:
            return "", "permanent"
        return "", "missing"
    match = re.search(r"(20\d{2})\s*[.\-/년]\s*(\d{1,2})\s*[.\-/월]\s*(\d{1,2})", deadline_text)
    if match:
        year, month, day = (int(part) for part in match.groups())
        try:
            return datetime(year, month, day, tzinfo=timezone.utc).date().isoformat(), "date"
        except ValueError:
            return "", "missing"
    if "영구" in deadline_text:
        return "", "permanent"
    return "", "missing"


def software_names_from_text(item: dict[str, object], detail: dict[str, object]) -> str:
    text = normalize_text(detail.get("contentsText") or detail.get("contentText"))
    lines = [normalize_text(line) for line in text.splitlines() if normalize_text(line)]
    explicit = value_after_label(lines, {"소프트웨어명", "서비스명", "대상", "신청"})
    checked = detail.get("checkedLabels") if isinstance(detail.get("checkedLabels"), list) else []
    names: list[str] = []
    for value in checked:
        cleaned = normalize_text(value)
        if cleaned:
            names.append(cleaned)
    if explicit:
        names.append(explicit)
    combined = " ".join([normalize_text(item.get("docTitle")), normalize_text(item.get("formName")), text])
    for hint in SOFTWARE_NAME_HINTS:
        if hint.lower() in combined.lower():
            names.append(hint)
    deduped: list[str] = []
    seen: set[str] = set()
    for name in names:
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(name)
    return ", ".join(deduped[:6]) or normalize_text(item.get("docTitle")) or "-"


def is_software_expiration_candidate(item: dict[str, object], detail: dict[str, object]) -> bool:
    detail_text = normalize_text(detail.get("contentsText") or detail.get("contentText"))
    lines = [normalize_text(line) for line in detail_text.splitlines() if normalize_text(line)]
    explicit = value_after_label(lines, {"소프트웨어명", "서비스명", "대상", "신청"})
    checked = detail.get("checkedLabels") if isinstance(detail.get("checkedLabels"), list) else []
    candidate_text = " ".join(
        [
            normalize_text(item.get("docNo")),
            normalize_text(item.get("docTitle")),
            normalize_text(item.get("formName")),
            normalize_text(item.get("formTitle")),
            explicit,
            " ".join(normalize_text(value) for value in checked),
        ]
    )
    candidate_lower = candidate_text.lower()
    has_strong_marker = any(marker.lower() in candidate_lower for marker in SOFTWARE_CANDIDATE_MARKERS)
    has_product_marker = any(hint.lower() in candidate_lower for hint in SOFTWARE_NAME_HINTS)
    has_noise_marker = any(marker.lower() in candidate_lower for marker in SOFTWARE_NOISE_HEADLINE_MARKERS)
    if has_noise_marker and not has_strong_marker:
        return False
    return has_strong_marker or has_product_marker


def expiry_status(expiration_date: str, expiry_type: str) -> tuple[str, int | None]:
    if expiry_type == "permanent":
        return "permanent", None
    if not expiration_date:
        return "missing_expiry", None
    target = datetime.fromisoformat(expiration_date).date()
    days_left = (target - datetime.now(KST).date()).days
    if days_left < 0:
        return "expired", days_left
    if days_left <= 30:
        return "expiring_soon", days_left
    return "active", days_left


def software_expirations(query: dict[str, list[str]]) -> list[dict[str, object]]:
    search = (query.get("q") or [""])[0].strip().lower()
    rows: list[dict[str, object]] = []
    details_payload = load_approval_details()
    details = details_payload.get("items") if isinstance(details_payload.get("items"), dict) else {}
    for item in enriched_approval_items():
        doc_id = normalize_text(item.get("docId"))
        detail = details.get(doc_id) if isinstance(details.get(doc_id), dict) else {}
        if not is_software_expiration_candidate(item, detail):
            continue
        text = " ".join(
            [
                approval_blob(item),
                normalize_text(detail.get("contentsText") or detail.get("contentText")).lower(),
            ]
        )
        if not any(keyword.lower() in text for keyword in MY_SCOPE_KEYWORDS):
            continue
        expiration_date, expiry_type = parse_approval_deadline(normalize_text(detail.get("contentsText") or detail.get("contentText")))
        status, days_left = expiry_status(expiration_date, expiry_type)
        row = {
            "docId": doc_id,
            "docNo": normalize_text(item.get("docNo")),
            "docTitle": normalize_text(item.get("docTitle")),
            "software_name": software_names_from_text(item, detail),
            "requester": normalize_text(item.get("createdBy")),
            "department": normalize_text(item.get("deptName")),
            "expiration_date": expiration_date,
            "expiry_type": expiry_type,
            "expiry_status": status,
            "days_left": days_left,
            "repDt": normalize_text(item.get("repDt")),
            "statusLabel": normalize_text(item.get("statusLabel") or approval_status(item)),
            "externalUrl": normalize_text(item.get("externalUrl")),
        }
        if search and search not in " ".join(str(value or "") for value in row.values()).lower():
            continue
        rows.append(row)

    status_order = {"expired": 0, "expiring_soon": 1, "missing_expiry": 2, "active": 3, "permanent": 4}
    return sorted(
        rows,
        key=lambda row: (
            status_order.get(str(row.get("expiry_status")), 9),
            row.get("expiration_date") or "9999-12-31",
            row.get("software_name") or "",
        ),
    )


def software_risk_label(row: dict[str, object]) -> str:
    illegal = int(float(row.get("illegal_install_count") or 0))
    install_count = float(row.get("install_count") or 0)
    license_amount = row.get("license_amount")
    try:
        license_count = float(license_amount) if license_amount not in (None, "") else None
    except (TypeError, ValueError):
        license_count = None
    if illegal > 0:
        return f"라이선스 확인 {illegal}"
    if license_count and license_count > 0 and install_count > license_count:
        return f"초과 {int(install_count - license_count)}"
    return "정상"


def software_inventory(query: dict[str, list[str]]) -> list[dict[str, object]]:
    search = (query.get("q") or [""])[0].strip()
    limit_raw = (query.get("limit") or [""])[0].strip()
    limit = query_int(query, "limit", SOFTWARE_API_MAX_LIMIT, minimum=1, maximum=SOFTWARE_API_MAX_LIMIT) if limit_raw else None
    where: list[str] = []
    params: list[object] = []
    if search:
        where.append("(software_name LIKE ? OR sw_id = ?)")
        params.extend([f"%{search}%", search])
    sql = """
        SELECT
          sw_id, software_name, install_count, legal_install_count,
          illegal_install_count, temp_install_count, license_amount,
          residue_amount, assign_amount, assign_uninstall_amount,
          collected_at, imported_at
        FROM software_inventory_summary
    """
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += """
        ORDER BY
          illegal_install_count DESC,
          CASE
            WHEN license_amount IS NOT NULL AND license_amount > 0 AND install_count > license_amount
            THEN install_count - license_amount
            ELSE 0
          END DESC,
          install_count DESC,
          software_name
    """
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    rows = select_all(sql, tuple(params))
    for row in rows:
        try:
            license_amount = float(row.get("license_amount")) if row.get("license_amount") not in (None, "") else None
        except (TypeError, ValueError):
            license_amount = None
        if license_amount is not None and license_amount > 0 and license_amount != 999:
            install_count = float(row.get("install_count") or 0)
            computed_residue = license_amount - install_count
            row["computed_residue"] = computed_residue
            row["residue_reconciles"] = bool(row.get("residue_amount") == computed_residue)
        else:
            row["computed_residue"] = None
            row["residue_reconciles"] = None
        row["risk_label"] = software_risk_label(row)
    return rows


def software_compliance(query: dict[str, list[str]]) -> dict[str, object]:
    """관리대장(보유 라이선스) ↔ SWeeper(설치현황) 대조 컴플라이언스 뷰.

    software_ledger 가 비어 있어도 SWeeper 자체 라이선스 기반 버킷(초과/불법/유휴)은 동작한다.
    """
    from infra_control.connectors.ledger_software import norm

    search = (query.get("q") or [""])[0].strip().lower()

    def num(value: object) -> float:
        try:
            return float(value) if value not in (None, "") else 0.0
        except (TypeError, ValueError):
            return 0.0

    ledger = select_all(
        "SELECT product, norm_name, vendor, license_type, qty, expiry, source_sheet, has_key "
        "FROM software_ledger"
    )
    sweeper = select_all(
        "SELECT software_name, install_count, illegal_install_count, license_amount, residue_amount "
        "FROM software_inventory_summary"
    )

    sw_by_norm: dict[str, dict[str, object]] = {}
    for s in sweeper:
        s["_n"] = norm(str(s.get("software_name") or ""))
        s["_inst"] = num(s.get("install_count"))
        cur = sw_by_norm.get(s["_n"])
        if cur is None or s["_inst"] > num(cur.get("install_count")):
            sw_by_norm[s["_n"]] = s

    def _toks(text: str) -> set[str]:
        return {t for t in text.split() if len(t) > 1}

    # 단독으로는 매칭 근거가 못 되는 일반 토큰(벤더/수식어)
    generic = {"microsoft", "adobe", "ms", "for", "pro", "std", "the", "apps",
               "app", "cloud", "open", "win", "windows", "single", "device"}

    def find_sw(name_norm: str) -> dict[str, object] | None:
        if not name_norm:
            return None
        if name_norm in sw_by_norm:
            return sw_by_norm[name_norm]
        # 토큰 overlap (>1글자 토큰만, 작은 집합의 60%+ 일치) — 짧은/단일토큰 오매칭 방지
        at = _toks(name_norm)
        if not at:
            return None
        best: dict[str, object] | None = None
        best_score = 0.0
        for key, row in sw_by_norm.items():
            ct = _toks(key)
            if not ct:
                continue
            inter = at & ct
            if not inter:
                continue
            # 일반 토큰만 겹치면(예: 'microsoft' 단독) 매칭 근거로 인정하지 않음
            if not (inter - generic) and len(inter) < 2:
                continue
            score = len(inter) / min(len(at), len(ct))
            if score > best_score:
                best_score = score
                best = row
        return best if best_score >= 0.6 else None

    matched: list[dict[str, object]] = []
    ledger_only: list[dict[str, object]] = []
    matched_sw: set[str] = set()
    for item in ledger:
        sw = find_sw(str(item.get("norm_name") or ""))
        if sw is not None:
            matched_sw.add(str(sw.get("software_name")))
            matched.append({
                "product": item.get("product"),
                "sweeper_name": sw.get("software_name"),
                "license_type": item.get("license_type"),
                "ledger_qty": item.get("qty"),
                "sweeper_license": num(sw.get("license_amount")),
                "installs": num(sw.get("install_count")),
                "illegal": num(sw.get("illegal_install_count")),
                "expiry": item.get("expiry"),
                "source_sheet": item.get("source_sheet"),
            })
        else:
            ledger_only.append({
                "product": item.get("product"),
                "license_type": item.get("license_type"),
                "qty": item.get("qty"),
                "vendor": item.get("vendor"),
                "expiry": item.get("expiry"),
                "source_sheet": item.get("source_sheet"),
            })

    over_deployed: list[dict[str, object]] = []
    illegal_installs: list[dict[str, object]] = []
    shelfware: list[dict[str, object]] = []
    seen_over: set[str] = set()
    seen_shelf: set[str] = set()
    for s in sweeper:
        lic = num(s.get("license_amount"))
        inst = s["_inst"]
        ill = num(s.get("illegal_install_count"))
        name_norm = s["_n"]
        if ill > 0:
            illegal_installs.append({
                "software_name": s.get("software_name"),
                "illegal": ill,
                "installs": inst,
                "in_ledger": s.get("software_name") in matched_sw,
            })
        if lic > 0 and inst > lic and name_norm not in seen_over:
            seen_over.add(name_norm)
            over_deployed.append({"software_name": s.get("software_name"), "license": lic,
                                  "installs": inst, "over_by": inst - lic})
        if 0 < lic < 900 and inst < lic * 0.5 and name_norm not in seen_shelf:
            seen_shelf.add(name_norm)
            shelfware.append({"software_name": s.get("software_name"), "license": lic,
                              "installs": inst, "idle": lic - inst})

    expiring = sorted((item for item in ledger if str(item.get("expiry") or "")),
                      key=lambda item: str(item.get("expiry")))

    result: dict[str, object] = {
        "summary": {
            "ledger_products": len(ledger),
            "sweeper_titles": len(sweeper),
            "matched": len(matched),
            "ledger_only": len(ledger_only),
            "over_deployed": len(over_deployed),
            "over_deployed_units": sum(int(row["over_by"]) for row in over_deployed),
            "illegal_installs": len(illegal_installs),
            "illegal_install_units": sum(int(row["illegal"]) for row in illegal_installs),
            "shelfware": len(shelfware),
            "shelfware_idle_units": sum(int(row["idle"]) for row in shelfware),
            "ledger_with_expiry": len(expiring),
        },
        "matched": matched,
        "ledger_only": ledger_only,
        "over_deployed": sorted(over_deployed, key=lambda r: -r["over_by"]),
        "illegal_installs": sorted(illegal_installs, key=lambda r: -r["illegal"]),
        "shelfware": sorted(shelfware, key=lambda r: -r["idle"]),
        "expiring": list(expiring),
    }
    if search:
        def keep(row: dict[str, object]) -> bool:
            return search in " ".join(str(v or "") for v in row.values()).lower()
        for key in ("matched", "ledger_only", "over_deployed", "illegal_installs", "shelfware", "expiring"):
            result[key] = [r for r in result[key] if keep(r)]
    return result


def software_install_status_label(value: object) -> str:
    if str(value) == "0":
        return "라이선스 확인"
    if str(value) == "1":
        return "정상"
    return "확인필요"


def normalized_software_asset_tag(row: dict[str, object]) -> str:
    for key in ("asset_no", "equip_name", "equip_code"):
        value = normalize_text(row.get(key))
        if value:
            return value
    return ""


def software_installs(query: dict[str, list[str]]) -> list[dict[str, object]]:
    search = (query.get("q") or [""])[0].strip()
    sw_id = (query.get("sw_id") or [""])[0].strip()
    asset_tag = (query.get("asset_tag") or [""])[0].strip()
    user = (query.get("user") or [""])[0].strip()
    limit = query_int(query, "limit", 500, minimum=1, maximum=10000)
    where: list[str] = []
    params: list[object] = []
    if sw_id:
        where.append("sw_id = ?")
        params.append(sw_id)
    if asset_tag:
        where.append("(asset_no = ? OR equip_name = ? OR equip_code = ?)")
        params.extend([asset_tag, asset_tag, asset_tag])
    if user:
        where.append(
            """
            (user_key = ? OR user_name = ?
             OR COALESCE(NULLIF(TRIM(user_key), ''), NULLIF(TRIM(user_name), ''), '사용자 미확인') = ?)
            """
        )
        params.extend([user, user, user])
    if search:
        where.append(
            """
            (software_name LIKE ? OR user_name LIKE ? OR user_key LIKE ?
             OR dept_name LIKE ? OR asset_no LIKE ? OR equip_name LIKE ? OR equip_code LIKE ?)
            """
        )
        like = f"%{search}%"
        params.extend([like, like, like, like, like, like, like])
    sql = """
        SELECT
          install_id, sw_id, equip_id, software_name, equip_code, equip_name,
          asset_no, user_key, user_name, dept_name, is_licensed,
          install_date, report_date, collected_at, imported_at
        FROM software_inventory_installs
    """
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += """
        ORDER BY
          CASE WHEN is_licensed = 0 THEN 0 WHEN is_licensed = 1 THEN 1 ELSE 2 END,
          software_name,
          user_name,
          equip_name
        LIMIT ?
    """
    params.append(limit)
    rows = select_all(sql, tuple(params))
    for row in rows:
        row["asset_tag"] = normalized_software_asset_tag(row)
        row["license_status_label"] = software_install_status_label(row.get("is_licensed"))
    return rows


def software_users(query: dict[str, list[str]]) -> list[dict[str, object]]:
    search = (query.get("q") or [""])[0].strip()
    limit = query_int(query, "limit", 500, minimum=1, maximum=5000)
    identity_expr = "COALESCE(NULLIF(TRIM(user_key), ''), NULLIF(TRIM(user_name), ''), '사용자 미확인')"
    asset_expr = "COALESCE(NULLIF(TRIM(asset_no), ''), NULLIF(TRIM(equip_name), ''), NULLIF(TRIM(equip_code), ''))"
    where = ""
    params: list[object] = []
    if search:
        where = """
            WHERE user_key LIKE ? OR user_name LIKE ? OR dept_name LIKE ?
               OR software_name LIKE ? OR asset_no LIKE ? OR equip_name LIKE ? OR equip_code LIKE ?
        """
        like = f"%{search}%"
        params.extend([like, like, like, like, like, like, like])
    sql = f"""
        SELECT
          {identity_expr} AS user_key,
          MAX(NULLIF(TRIM(user_name), '')) AS user_name,
          MAX(NULLIF(TRIM(dept_name), '')) AS dept_name,
          COUNT(*) AS install_count,
          SUM(CASE WHEN is_licensed = 0 THEN 1 ELSE 0 END) AS unlicensed_count,
          COUNT(DISTINCT sw_id) AS software_count,
          COUNT(DISTINCT {asset_expr}) AS asset_count,
          MAX(COALESCE(report_date, install_date, collected_at, imported_at)) AS latest_seen_at
        FROM software_inventory_installs
        {where}
        GROUP BY {identity_expr}
        ORDER BY unlicensed_count DESC, install_count DESC, user_key
        LIMIT ?
    """
    params.append(limit)
    rows = select_all(sql, tuple(params))
    for row in rows:
        row["user_name"] = row.get("user_name") or row.get("user_key") or "사용자 미확인"
        row["dept_name"] = row.get("dept_name") or ""
    return rows


def find_approval(doc_id: str) -> dict[str, object] | None:
    normalized = normalize_text(doc_id)
    for item in enriched_approval_items():
        if normalize_text(item.get("docId")) == normalized:
            return item
    return None


def approval_workflow(doc_id: str) -> dict[str, object]:
    doc_id = normalize_text(doc_id)
    if not doc_id:
        raise ValueError("doc_id required")
    item = find_approval(doc_id)
    if not item:
        raise ValueError("approval not found")
    workflows = load_approval_workflows()
    record = workflows.get("records", {}).get(doc_id)
    source_detail = load_approval_details().get("items", {}).get(doc_id)
    if not isinstance(source_detail, dict):
        source_detail = {}
    return {
        "item": item,
        "record": record,
        "sourceDetail": source_detail,
        "externalUrl": item.get("externalUrl") or APPROVAL_HOME_URL,
        "homeUrl": APPROVAL_HOME_URL,
        "writeback": writeback_status(record if isinstance(record, dict) else {}),
    }


def normalize_workflow_comments(existing: dict[str, object]) -> list[dict[str, object]]:
    comments: list[dict[str, object]] = []
    raw_comments = existing.get("comments")
    if isinstance(raw_comments, list):
        for raw_comment in raw_comments:
            if not isinstance(raw_comment, dict):
                continue
            body = normalize_text(raw_comment.get("body") or raw_comment.get("comment"))[:4000]
            if not body:
                continue
            comments.append(
                {
                    "id": normalize_text(raw_comment.get("id")) or str(uuid.uuid4()),
                    "at": normalize_text(raw_comment.get("at") or raw_comment.get("createdAt")),
                    "actor": normalize_text(raw_comment.get("actor") or raw_comment.get("createdBy")),
                    "body": body,
                }
            )

    legacy_comment = normalize_text(existing.get("comment"))[:4000]
    if legacy_comment and not comments:
        comments.append(
            {
                "id": normalize_text(existing.get("updatedAt")) or "legacy-comment",
                "at": normalize_text(existing.get("updatedAt")),
                "actor": normalize_text(existing.get("updatedBy")),
                "body": legacy_comment,
            }
        )

    return comments[-100:]


def workflow_history_summary(action: str, local_status: str, comment: str) -> str:
    if action == "dispatch_done":
        return "내부 시행 표시 저장 (원천 미반영)"
    if action == "add_comment":
        return f"댓글 등록: {comment[:80]}" if comment else "댓글 등록"
    if action == "request_writeback":
        return "Amaranth 원문 반영 요청 등록"
    if local_status == "done":
        return "내부 처리 완료 저장"
    if local_status == "blocked":
        return "보류 저장"
    return "초안 저장"


def enqueue_writeback_request(
    *,
    item: dict[str, object],
    action: str,
    actor: str,
    dispatch_memo: str,
    comment: str,
    local_status: str,
    now: str,
) -> dict[str, object]:
    queue = load_writeback_queue()
    items = queue.get("items") if isinstance(queue.get("items"), list) else []
    doc_id = approval_item_id(item)
    queue_item = {
        "id": str(uuid.uuid4()),
        "status": "local_outbox_unsent",
        "docId": doc_id,
        "docNo": normalize_text(item.get("docNo")),
        "docTitle": normalize_text(item.get("docTitle")),
        "sourceBoxCode": normalize_text(item.get("sourceBoxCode")),
        "sourceLabel": normalize_text(item.get("sourceLabel")),
        "requestedAction": action,
        "localStatus": local_status,
        "dispatchMemo": dispatch_memo,
        "comment": comment,
        "actor": actor,
        "createdAt": now,
        "externalUrl": normalize_text(item.get("externalUrl")),
        "endpoint": writeback_endpoint(),
    }
    for existing in items:
        if (
            isinstance(existing, dict)
            and normalize_text(existing.get("docId")) == doc_id
            and normalize_writeback_status(existing.get("status")) == "local_outbox_unsent"
        ):
            queue_item["id"] = normalize_text(existing.get("id")) or queue_item["id"]
            existing.clear()
            existing.update(queue_item)
            queue["items"] = items[-500:]
            queue["updatedAt"] = now
            save_writeback_queue(queue)
            return existing
    items.append(queue_item)
    queue["items"] = items[-500:]
    queue["updatedAt"] = now
    save_writeback_queue(queue)
    return queue_item


def update_approval_workflow(payload: dict[str, object]) -> dict[str, object]:
    doc_id = normalize_text(payload.get("docId") or payload.get("doc_id"))
    if not doc_id:
        raise ValueError("docId required")
    item = find_approval(doc_id)
    if not item:
        raise ValueError("approval not found")
    action = normalize_text(payload.get("action")) or "save_draft"
    comment = normalize_text(payload.get("comment"))[:4000]
    local_status = normalize_text(payload.get("localStatus")) or ("done" if action == "dispatch_done" else "draft")
    if local_status not in {"draft", "done", "blocked"}:
        raise ValueError("invalid localStatus")
    if action == "add_comment" and not comment:
        raise ValueError("comment required")
    if action == "request_writeback" and not (comment or normalize_text(payload.get("dispatchMemo"))):
        raise ValueError("comment or dispatchMemo required")
    if action == "dispatch_done" and normalize_text(item.get("sourceBoxCode")) != "120":
        raise ValueError("dispatch action is only available for dispatch box items")
    actor = normalize_text(payload.get("actor"))[:80] or "operator-ui"
    now = utc_now()
    workflows = load_approval_workflows()
    records = workflows.setdefault("records", {})
    existing = records.get(doc_id) if isinstance(records.get(doc_id), dict) else {}
    dispatch_memo = (
        normalize_text(payload.get("dispatchMemo"))[:4000]
        if "dispatchMemo" in payload
        else normalize_text(existing.get("dispatchMemo"))[:4000]
    )
    comments = normalize_workflow_comments(existing)
    if action == "add_comment":
        comments.append(
            {
                "id": str(uuid.uuid4()),
                "at": now,
                "actor": actor,
                "body": comment,
            }
        )
    writeback_item: dict[str, object] | None = None
    if action == "request_writeback":
        writeback_item = enqueue_writeback_request(
            item=item,
            action=action,
            actor=actor,
            dispatch_memo=dispatch_memo,
            comment=comment,
            local_status=local_status,
            now=now,
        )
    history = existing.get("history") if isinstance(existing.get("history"), list) else []
    history = history[-49:] + [
        {
            "id": str(uuid.uuid4()),
            "at": now,
            "actor": actor,
            "action": action,
            "localStatus": local_status,
            "summary": normalize_text(payload.get("summary"))[:300] or workflow_history_summary(action, local_status, comment),
        }
    ]
    writeback_status_value = "local_outbox_unsent" if writeback_item else normalize_writeback_status(existing.get("writebackStatus")) or "local_only"
    writeback_message = (
        f"내부 outbox 보관됨 (미전송): {writeback_item['id']}"
        if writeback_item
        else normalize_text(existing.get("writebackMessage")) or "8000 내부 처리 기록"
    )
    record = {
        "docId": doc_id,
        "docNo": normalize_text(item.get("docNo")),
        "docTitle": normalize_text(item.get("docTitle")),
        "bucket": normalize_text(item.get("bucket")),
        "theme": normalize_text(item.get("theme")),
        "dispatchMemo": dispatch_memo,
        "comment": comment if action == "add_comment" and comment else normalize_text(existing.get("comment"))[:4000],
        "comments": comments[-100:],
        "localStatus": local_status,
        "writebackStatus": writeback_status_value,
        "writebackMessage": writeback_message,
        "writebackQueueId": normalize_text(writeback_item.get("id")) if writeback_item else normalize_text(existing.get("writebackQueueId")),
        "updatedAt": now,
        "updatedBy": actor,
        "history": history,
    }
    records[doc_id] = record
    workflows["updatedAt"] = now
    save_approval_workflows(workflows)
    return {"passed": True, "record": record, "item": item, "writeback": writeback_status(record)}


def notebook_sql_match() -> tuple[str, list[object]]:
    text_expr = (
        "LOWER(COALESCE(category, '') || ' ' || COALESCE(model, '') || ' ' || "
        "COALESCE(manufacturer, '') || ' ' || COALESCE(asset_tag, ''))"
    )
    return (
        "(" + " OR ".join([f"{text_expr} LIKE ?" for _ in NOTEBOOK_TERMS]) + ")",
        [f"%{term.lower()}%" for term in NOTEBOOK_TERMS],
    )


def asset_group_sql_match(groups: list[str]) -> tuple[str, list[object]]:
    clauses: list[str] = []
    params: list[object] = []
    for raw_group in groups:
        group = raw_group.strip().upper()
        if not group:
            continue
        clauses.append("(UPPER(COALESCE(asset_tag, '')) = ? OR UPPER(COALESCE(asset_tag, '')) LIKE ?)")
        params.extend([group, f"{group}-%"])
    if not clauses:
        return "1 = 0", []
    return "(" + " OR ".join(clauses) + ")", params


def append_unrecognized_asset_group_filter(where: list[str], params: list[object]) -> None:
    clause, clause_params = asset_group_sql_match(sorted(ASSET_NUMBER_CLASSES))
    where.append(f"NOT {clause}")
    params.extend(clause_params)


def append_asset_kind_filter(where: list[str], params: list[object], kind: str) -> None:
    groups = ROLLUP_GROUPS.get(kind, [])
    if kind in {"server", "network", "notebook"}:
        clause, clause_params = asset_group_sql_match(groups)
        where.append(clause)
        params.extend(clause_params)
        return
    if kind == "other":
        other_groups = [
            group for group, spec in ASSET_NUMBER_CLASSES.items()
            if spec["rollup"] == "other"
        ]
        numbered_clause, numbered_params = asset_group_sql_match(other_groups)
        unrecognized_clause, unrecognized_params = asset_group_sql_match(sorted(ASSET_NUMBER_CLASSES))
        where.append(f"({numbered_clause} OR NOT {unrecognized_clause})")
        params.extend(numbered_params)
        params.extend(unrecognized_params)


def append_asset_class_filter(where: list[str], params: list[object], class_key: str) -> None:
    if class_key == "unnumbered":
        append_unrecognized_asset_group_filter(where, params)
        return
    groups = [
        group for group, spec in ASSET_NUMBER_CLASSES.items()
        if spec["key"] == class_key
    ]
    clause, clause_params = asset_group_sql_match(groups)
    where.append(clause)
    params.extend(clause_params)


def assets(query: dict[str, list[str]], only_servers: bool, limit: int = ASSET_API_LIMIT) -> list[dict[str, object]]:
    search = (query.get("q") or [""])[0].strip()
    usage = (query.get("usage") or [""])[0].strip()
    lifecycle = (query.get("lifecycle") or [""])[0].strip()
    asset_prefix = (query.get("asset_prefix") or [""])[0].strip().upper()
    kind = (query.get("kind") or [""])[0].strip()
    asset_class = (query.get("class") or [""])[0].strip()
    owner = (query.get("owner") or [""])[0].strip()
    duplicate_asset_tags_only = (query.get("dup_tag") or [""])[0].strip().lower() in {"1", "true", "yes"}
    duplicate_serials_only = (query.get("dup_serial") or [""])[0].strip().lower() in {"1", "true", "yes"}
    sql = "SELECT * FROM assets"
    where: list[str] = []
    params: list[object] = []
    if only_servers:
        append_asset_kind_filter(where, params, "server")
    if asset_class:
        append_asset_class_filter(where, params, asset_class)
    elif kind:
        append_asset_kind_filter(where, params, kind)
    if usage:
        if usage == "확인필요":
            where.append(f"COALESCE(usage_status, '') NOT IN ({','.join('?' for _ in KNOWN_USAGE_STATUSES)})")
            params.extend(KNOWN_USAGE_STATUSES)
        elif usage == "운영대상":
            where.append("COALESCE(usage_status, '') <> ?")
            params.append(USAGE_RETIRED)
        else:
            where.append("usage_status = ?")
            params.append(usage)
    if asset_prefix:
        where.append("(UPPER(asset_tag) = ? OR UPPER(asset_tag) LIKE ?)")
        params.extend([asset_prefix, f"{asset_prefix}-%"])
    if duplicate_asset_tags_only:
        where.append(
            f"""
            COALESCE(TRIM(asset_tag), '') <> ''
            AND asset_tag IN (
              {duplicate_asset_tag_conflict_sql()}
            )
            """
        )
    if duplicate_serials_only:
        where.append(
            """
            COALESCE(TRIM(serial), '') <> ''
            AND TRIM(serial) NOT IN ('-', '조립', '조립PC', 'N/A', 'n/a')
            AND serial IN (
              SELECT serial
              FROM assets
              WHERE COALESCE(TRIM(serial), '') <> ''
                AND TRIM(serial) NOT IN ('-', '조립', '조립PC', 'N/A', 'n/a')
              GROUP BY serial
              HAVING COUNT(*) > 1
            )
            """
        )
    if search:
        like = f"%{search}%"
        where.append(
            """
            (asset_tag LIKE ? OR hostname LIKE ? OR serial LIKE ? OR model LIKE ? OR owner LIKE ?
             OR department LIKE ? OR primary_ip LIKE ? OR room LIKE ? OR rack LIKE ? OR purpose LIKE ?)
            """
        )
        params.extend([like, like, like, like, like, like, like, like, like, like])
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY is_server DESC, is_network DESC, asset_tag LIMIT ?"
    params.append(50000 if asset_prefix or owner or lifecycle else limit)
    rows = enrich_assets(select_all(sql, tuple(params)))
    if lifecycle == NOTEBOOK_REFRESH_LIFECYCLE:
        rows = [row for row in rows if row.get("replacement_due")]
        rows.sort(
            key=lambda row: (
                int(row.get("notebook_refresh_sort_rank") or 99),
                -float(row.get("notebook_age_years") or 0),
                normalize_text(row.get("asset_tag")),
            )
        )
    if owner:
        rows = [
            row for row in rows
            if normalize_text(row.get("owner_status")) == "active"
            and (normalize_text(row.get("owner")) == owner or normalize_text(row.get("owner_source")) == owner)
        ]
    if lifecycle == NOTEBOOK_REFRESH_LIFECYCLE:
        return rows
    if usage == "사용중":
        existing_tags = {normalize_text(row.get("asset_tag")) for row in rows}
        rows.extend(
            row for row in jira_in_use_overlay_assets(
                search=search,
                only_servers=only_servers,
                asset_prefix=asset_prefix,
                kind=kind,
                asset_class=asset_class,
                owner=owner,
                duplicate_asset_tags_only=duplicate_asset_tags_only,
                duplicate_serials_only=duplicate_serials_only,
            )
            if normalize_text(row.get("asset_tag")) not in existing_tags
        )
    return sorted(
        rows,
        key=lambda row: (
            -int(row.get("is_server") or 0),
            -int(row.get("is_network") or 0),
            normalize_text(row.get("asset_tag")),
        ),
    )


def changes(query: dict[str, list[str]]) -> list[dict[str, object]]:
    limit = query_int(query, "limit", CHANGE_API_DEFAULT_LIMIT, minimum=1, maximum=CHANGE_API_MAX_LIMIT)
    offset = query_int(query, "offset", 0, minimum=0, maximum=1_000_000)
    search = (query.get("q") or [""])[0].strip()
    asset_tag = (query.get("asset_tag") or [""])[0].strip()
    source = (query.get("source") or [""])[0].strip()
    field = (query.get("field") or [""])[0].strip()
    action = (query.get("action") or [""])[0].strip()
    where: list[str] = []
    params: list[object] = []
    if asset_tag:
        where.append("asset_tag = ?")
        params.append(asset_tag)
    if source:
        where.append("source = ?")
        params.append(source)
    if field:
        where.append("field = ?")
        params.append(field)
    if action:
        where.append("action = ?")
        params.append(action)
    if search:
        like = f"%{search}%"
        where.append(
            """
            (asset_tag LIKE ? OR actor LIKE ? OR action LIKE ? OR field LIKE ?
             OR old_value LIKE ? OR new_value LIKE ? OR reason LIKE ? OR source LIKE ?)
            """
        )
        params.extend([like, like, like, like, like, like, like, like])
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    params.extend([limit, offset])
    return select_all(
        f"""
        SELECT *
        FROM change_history
        {where_sql}
        ORDER BY changed_at DESC
        LIMIT ? OFFSET ?
        """,
        tuple(params),
    )


def asset_groups(query: dict[str, list[str]]) -> list[dict[str, object]]:
    search = (query.get("q") or [""])[0].strip().upper()
    rows = assets({"q": [search]} if search else {}, only_servers=False, limit=50000)
    grouped: dict[str, dict[str, object]] = {}
    owners_by_group: dict[str, set[str]] = {}
    samples_by_group: dict[str, list[str]] = {}
    for row in rows:
        group = str(row.get("asset_group") or asset_group(row.get("asset_tag")))
        item = grouped.setdefault(
            group,
            {
                "asset_group": group,
                "assets": 0,
                "in_use": 0,
                "unused": 0,
                "retired": 0,
                "needs_review": 0,
                "servers": 0,
                "network": 0,
                "latest_updated": "",
                "sample_assets": "",
                "owners": 0,
            },
        )
        item["assets"] = int(item["assets"]) + 1
        if row.get("usage_status") == USAGE_IN_USE:
            item["in_use"] = int(item["in_use"]) + 1
        elif row.get("usage_status") == USAGE_UNUSED:
            item["unused"] = int(item["unused"]) + 1
        elif row.get("usage_status") == USAGE_RETIRED:
            item["retired"] = int(item.get("retired", 0)) + 1
        else:
            item["needs_review"] = int(item["needs_review"]) + 1
        item["servers"] = int(item["servers"]) + (1 if row.get("asset_kind") == "server" else 0)
        item["network"] = int(item["network"]) + (1 if row.get("asset_kind") == "network" else 0)
        latest = str(row.get("updated_at") or "")
        if latest > str(item["latest_updated"] or ""):
            item["latest_updated"] = latest
        owner = str(row.get("owner") or "").strip()
        if row.get("owner_status") == "active" and owner and owner not in {"-", "미확인", "없음", "N/A", "n/a"}:
            owners_by_group.setdefault(group, set()).add(owner)
        samples = samples_by_group.setdefault(group, [])
        asset_tag = str(row.get("asset_tag") or "").strip()
        if asset_tag and len(samples) < 5:
            samples.append(asset_tag)
    for group, item in grouped.items():
        item["owners"] = len(owners_by_group.get(group, set()))
        item["sample_assets"] = ", ".join(samples_by_group.get(group, []))
    return sorted(grouped.values(), key=lambda row: (-int(row["assets"]), str(row["asset_group"])))


def locations(query: dict[str, list[str]]) -> list[dict[str, object]]:
    search = (query.get("q") or [""])[0].strip()
    params: list[object] = []
    where = ""
    if search:
        like = f"%{search}%"
        where = "WHERE l.room LIKE ? OR l.rack LIKE ? OR l.asset_tag LIKE ? OR a.hostname LIKE ?"
        params.extend([like, like, like, like])
    return enrich_assets(select_all(
        f"""
        SELECT l.*, a.hostname, a.primary_ip, a.owner, a.department, a.usage_status, a.usage_reason,
               a.metadata_json, a.is_server, a.is_network
        FROM locations l
        LEFT JOIN assets a ON a.asset_tag = l.asset_tag
        {where}
        ORDER BY l.room, l.rack, l.rack_unit, l.asset_tag
        LIMIT 1000
        """,
        tuple(params),
    ))


def sources(query: dict[str, list[str]]) -> list[dict[str, object]]:
    search = (query.get("q") or [""])[0].strip()
    include_hidden = query_truthy(query, "include_hidden") or query_truthy(query, "include_stale")
    params: list[object] = []
    where = ""
    if search:
        like = f"%{search}%"
        where = "WHERE name LIKE ? OR kind LIKE ? OR endpoint LIKE ? OR status LIKE ?"
        params.extend([like, like, like, like])
    rows = select_all(
        f"""
        SELECT
          id,
          name,
          kind,
          endpoint,
          status,
          last_sync_at,
          json_extract(metadata_json, '$.record_count') AS record_count
        FROM integration_sources
        {where}
        ORDER BY name
        """,
        tuple(params),
    )
    virtual_rows = virtual_integration_sources()
    if search:
        lowered = search.lower()
        virtual_rows = [
            row for row in virtual_rows
            if lowered in " ".join(str(row.get(key) or "") for key in ["name", "kind", "endpoint", "status"]).lower()
        ]
    if not include_hidden:
        rows = [row for row in rows if not hidden_integration_source(row)]
        virtual_rows = [row for row in virtual_rows if not hidden_integration_source(row)]
    return sorted(rows + virtual_rows, key=lambda row: (str(row.get("kind") or ""), str(row.get("name") or "")))


def owners(query: dict[str, list[str]]) -> list[dict[str, object]]:
    search = (query.get("q") or [""])[0].strip()
    params: list[object] = []
    where = [
        "COALESCE(TRIM(owner), '') <> ''",
        "TRIM(owner) NOT IN ('-', '미확인', '없음', 'N/A', 'n/a')",
    ]
    if search:
        like = f"%{search}%"
        where.append("(owner LIKE ? OR owner_email LIKE ? OR department LIKE ? OR asset_tag LIKE ? OR hostname LIKE ?)")
        params.extend([like, like, like, like, like])
    rows = enrich_assets(select_all(
        f"""
        SELECT *
        FROM assets
        WHERE {" AND ".join(where)}
        ORDER BY owner, asset_tag
        LIMIT 50000
        """,
        tuple(params),
    ))
    grouped: dict[str, dict[str, object]] = {}
    tags: dict[str, list[str]] = {}
    for row in rows:
        if row.get("owner_status") != "active":
            continue
        owner = normalize_text(row.get("owner"))
        item = grouped.setdefault(
            owner,
            {
                "owner": owner,
                "owner_email": "",
                "department": "",
                "assets": 0,
                "in_use": 0,
                "unused": 0,
                "retired": 0,
                "needs_review": 0,
                "servers": 0,
                "network": 0,
                "notebooks": 0,
                "other_assets": 0,
                "updated_at": "",
                "asset_tags": "",
                "category_summary": "",
                "category_counts": {key: 0 for key in ASSET_CLASS_ORDER},
                "category_breakdown": [],
            },
        )
        item["assets"] = int(item["assets"]) + 1
        usage = normalize_text(row.get("usage_status"))
        if usage == USAGE_IN_USE:
            item["in_use"] = int(item["in_use"]) + 1
        elif usage == USAGE_UNUSED:
            item["unused"] = int(item["unused"]) + 1
        elif usage == USAGE_RETIRED:
            item["retired"] = int(item.get("retired", 0)) + 1
        else:
            item["needs_review"] = int(item["needs_review"]) + 1
        kind = normalize_text(row.get("asset_kind")) or asset_kind(row)
        class_key = normalize_text(row.get("asset_number_class")) or asset_number_class(row)["key"]
        counts = item["category_counts"]
        if isinstance(counts, dict):
            counts[class_key] = int(counts.get(class_key, 0)) + 1
        if kind == "server":
            item["servers"] = int(item["servers"]) + 1
        elif kind == "network":
            item["network"] = int(item["network"]) + 1
        elif kind == "notebook":
            item["notebooks"] = int(item["notebooks"]) + 1
        else:
            item["other_assets"] = int(item["other_assets"]) + 1
        if not item["owner_email"] and normalize_text(row.get("owner_email")):
            item["owner_email"] = normalize_text(row.get("owner_email"))
        if not item["department"] and normalize_text(row.get("department")):
            item["department"] = normalize_text(row.get("department"))
        updated = normalize_text(row.get("updated_at"))
        if updated > normalize_text(item.get("updated_at")):
            item["updated_at"] = updated
        asset_tag = normalize_text(row.get("asset_tag"))
        if asset_tag:
            tags.setdefault(owner, []).append(asset_tag)
    for owner, item in grouped.items():
        counts = item["category_counts"] if isinstance(item["category_counts"], dict) else {}
        breakdown = [
            {"kind": class_key, "label": ASSET_CLASS_LABELS.get(class_key, class_key), "count": int(counts.get(class_key, 0))}
            for class_key in ASSET_CLASS_ORDER
            if int(counts.get(class_key, 0))
        ]
        item["category_breakdown"] = breakdown
        item["category_summary"] = " · ".join(f"{part['label']} {part['count']}" for part in breakdown)
        item["asset_tags"] = ", ".join(tags.get(owner, []))
    return sorted(grouped.values(), key=lambda row: (-int(row["assets"]), str(row["owner"])))[:1000]


def maintenance(query: dict[str, list[str]]) -> list[dict[str, object]]:
    search = (query.get("q") or [""])[0].strip()
    params: list[object] = []
    where = ""
    if search:
        like = f"%{search}%"
        where = "WHERE m.asset_tag LIKE ? OR a.hostname LIKE ? OR m.note LIKE ?"
        params.extend([like, like, like])
    return enrich_assets(select_all(
        f"""
        SELECT m.*, a.hostname, a.owner, a.department, a.primary_ip, a.usage_status, a.usage_reason, a.metadata_json
        FROM maintenance_events m
        LEFT JOIN assets a ON a.asset_tag = m.asset_tag
        {where}
        ORDER BY COALESCE(m.event_date, '') DESC, m.asset_tag
        LIMIT 1000
        """,
        tuple(params),
    ))


def asset_detail(asset_tag: str) -> dict[str, object]:
    if not asset_tag:
        raise ValueError("asset_tag required")
    with connect() as conn:
        asset = conn.execute("SELECT * FROM assets WHERE asset_tag = ?", (asset_tag,)).fetchone()
        if asset:
            enriched_asset = enrich_asset(dict(asset))
            return {
                "asset": enriched_asset,
                "endpoint_profile": endpoint_profile(enriched_asset),
                "installed_software": software_installs({"asset_tag": [asset_tag], "limit": ["500"]}),
                "maintenance": rows_to_dicts(
                    conn.execute(
                        "SELECT * FROM maintenance_events WHERE asset_tag = ? ORDER BY COALESCE(event_date, '') DESC",
                        (asset_tag,),
                    ).fetchall()
                ),
                "changes": rows_to_dicts(
                    conn.execute(
                        "SELECT * FROM change_history WHERE asset_tag = ? ORDER BY changed_at DESC LIMIT 100",
                        (asset_tag,),
                    ).fetchall()
                ),
            }

    jira_missing = jira_missing_asset(asset_tag)
    return {
        "asset": None,
        "jira_missing": jira_missing,
        "missing": {
            "asset_tag": asset_tag,
            "reason": "jira_only" if jira_missing else "asset_not_found",
        },
        "maintenance": [],
        "changes": [],
    }


def update_asset(payload: dict[str, object]) -> dict[str, object]:
    asset_tag = str(payload.get("asset_tag") or "").strip()
    actor = str(payload.get("actor") or "operator").strip()[:80]
    reason = str(payload.get("reason") or "manual update").strip()[:300]
    updates = payload.get("updates")
    if not asset_tag:
        raise ValueError("asset_tag required")
    if not isinstance(updates, dict):
        raise ValueError("updates object required")

    now = utc_now()
    changed: list[dict[str, str]] = []
    with connect() as conn:
        row = conn.execute("SELECT * FROM assets WHERE asset_tag = ?", (asset_tag,)).fetchone()
        if not row:
            raise ValueError("asset not found")
        duplicate_count = conn.execute(
            "SELECT COUNT(*) FROM assets WHERE asset_tag = ?", (asset_tag,)
        ).fetchone()[0]
        if duplicate_count > 1:
            raise ValueError("asset_tag matches multiple rows; resolve duplicate identity before editing")
        existing = dict(row)
        metadata = parse_metadata_json(existing.get("metadata_json"))
        overrides = metadata.get("manual_overrides")
        if not isinstance(overrides, dict):
            overrides = {}
        effective_status = (
            str(updates.get("status") or "").strip() if "status" in updates else str(existing.get("status") or "")
        )
        effective_usage = (
            str(updates.get("usage_status") or "").strip() if "usage_status" in updates else str(existing.get("usage_status") or "")
        )
        effective_owner = (
            str(updates.get("owner") or "").strip() if "owner" in updates else str(existing.get("owner") or "")
        )
        if any(hint in effective_status for hint in RETIRED_STATUS_HINTS):
            if effective_usage == "사용중":
                raise ValueError("retired/disposed asset cannot be marked 사용중")
            if effective_owner:
                raise ValueError("retired/disposed asset cannot hold an active owner")
        for field, raw_value in updates.items():
            if field not in EDITABLE_FIELDS:
                continue
            new_value = str(raw_value or "").strip()
            old_value = str(existing.get(field) or "")
            if new_value == old_value:
                continue
            overrides[field] = now
            conn.execute(f"UPDATE assets SET {field} = ?, updated_at = ? WHERE asset_tag = ?", (new_value, now, asset_tag))
            conn.execute(
                """
                INSERT INTO change_history
                (id, asset_tag, changed_at, actor, action, field, old_value, new_value, reason, source, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    asset_tag,
                    now,
                    actor,
                    "manual_update",
                    field,
                    old_value,
                    new_value,
                    reason,
                    "infra-control-ui",
                    "{}",
                ),
            )
            changed.append({"field": field, "old_value": old_value, "new_value": new_value})
        if changed:
            metadata["manual_overrides"] = overrides
            conn.execute(
                "UPDATE assets SET metadata_json = ? WHERE asset_tag = ?",
                (json.dumps(metadata, ensure_ascii=False), asset_tag),
            )
    return {"passed": True, "asset_tag": asset_tag, "changed": changed, "detail": asset_detail(asset_tag)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    init_db()
    if basic_auth_credentials() is None and not _allow_no_auth():
        import sys as _sys
        print("[infra-control] WARNING: no Basic-auth credentials; non-health requests are REFUSED (fail-closed). Set config/security.env or INFRA_CONTROL_ALLOW_NO_AUTH=1.", file=_sys.stderr)
    ThreadingHTTPServer.request_queue_size = 128
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"infra-control listening on http://{args.host}:{args.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
