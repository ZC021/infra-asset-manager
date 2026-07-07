from __future__ import annotations

import json
import os
import secrets
from hashlib import sha256
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
RECEIPT_PATH = ROOT / "var" / "receipts" / "computer-use-user-test.json"
ARCHIVE_DIR = ROOT / "var" / "receipts" / "computer-use-archive"
REQUIRED_UI_MODES = (
    "servers",
    "assets",
    "asset_groups",
    "in_use",
    "unused",
    "needs_review",
    "owners",
    "locations",
    "maintenance",
    "changes",
    "sources",
)
GUI_COMPUTER_USE_MODES = ("codex-computer-use", "gui-computer-use", "desktop-computer-use")
NON_GUI_COMPUTER_USE_TERMS = ("playwright", "headless", "local-playwright", "cli")
DEFAULT_EXTERNAL_BASE = "http://asset-hub.example.com:8000"
ACCEPTED_ENDPOINT_USAGE_EVIDENCE = (
    "NAC 사용 근거",
    "Intune 사용 근거",
    "NAC+Intune 사용 근거",
    "NAC 최근 접속",
    "Intune 최근 동기화",
    "대여/checkout 배정",
    "Jira board",
)
UI_BUILD_FILES = (
    ROOT / "static" / "index.html",
    ROOT / "static" / "app.js",
    ROOT / "static" / "styles.css",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def expected_base(default: str = DEFAULT_EXTERNAL_BASE) -> str:
    return os.environ.get("INFRA_CONTROL_COMPUTER_USE_BASE", default)


def current_ui_build_id() -> str:
    digest = sha256()
    for path in UI_BUILD_FILES:
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()[:16]


def upload_token() -> str:
    return os.environ.get("INFRA_CONTROL_RECEIPT_TOKEN", "")


def token_required() -> bool:
    return bool(upload_token())


def token_matches(raw: str) -> bool:
    expected = upload_token()
    return bool(expected) and secrets.compare_digest(raw, expected)


def receipt_summary(payload: dict[str, Any]) -> dict[str, Any]:
    observed = payload.get("observed", {}) if isinstance(payload.get("observed"), dict) else {}
    return {
        "passed": payload.get("passed"),
        "mode": payload.get("mode"),
        "plugin": payload.get("plugin"),
        "app": payload.get("app"),
        "base": payload.get("base"),
        "simulated": payload.get("simulated"),
        "checked_at": payload.get("checked_at"),
        "operator_device": payload.get("operator_device"),
        "clicked_modes": observed.get("clicked_modes"),
        "selected_asset": observed.get("selected_asset"),
        "selected_asset_usage_basis": observed.get("selected_asset_usage_basis"),
        "selected_asset_usage_evidence": observed.get("selected_asset_usage_evidence"),
        "scroll_click_preserved_position": observed.get("scroll_click_preserved_position"),
        "csv_header": observed.get("csv_header"),
        "ui_build_id": observed.get("ui_build_id"),
        "screenshot": observed.get("screenshot"),
    }


def validate_receipt(payload: dict[str, Any], expected: str | None = None) -> tuple[bool, list[str], dict[str, Any]]:
    expected = expected or expected_base()
    errors: list[str] = []
    observed = payload.get("observed", {}) if isinstance(payload.get("observed"), dict) else {}
    clicked_modes = observed.get("clicked_modes")
    if not isinstance(clicked_modes, list):
        clicked_modes = []

    mode = str(payload.get("mode") or "")
    plugin = str(payload.get("plugin") or "")
    app = str(payload.get("app") or "")
    mode_blob = " ".join([mode, plugin, app]).lower()
    missing_modes = [mode_name for mode_name in REQUIRED_UI_MODES if mode_name not in clicked_modes]
    is_gui_computer_use = (
        mode in GUI_COMPUTER_USE_MODES
        or plugin == "Computer Use"
        or str(payload.get("environment") or "") == "gui"
    )
    is_non_gui = any(term in mode_blob for term in NON_GUI_COMPUTER_USE_TERMS)

    if payload.get("passed") is not True:
        errors.append("receipt passed must be true")
    if payload.get("simulated") is not False:
        errors.append("receipt simulated must be false")
    if payload.get("base") != expected:
        errors.append(f"receipt base must be {expected}")
    if not is_gui_computer_use:
        errors.append("receipt must come from GUI Computer Use")
    if is_non_gui:
        errors.append("CLI/headless/playwright receipt is not accepted as GUI Computer Use")
    if missing_modes:
        errors.append("missing clicked modes: " + ", ".join(missing_modes))
    if observed.get("selected_asset") != "ACME-A02-250379":
        errors.append("selected_asset must be ACME-A02-250379")
    observed_blob = json.dumps(observed, ensure_ascii=False)
    if not any(evidence in observed_blob for evidence in ACCEPTED_ENDPOINT_USAGE_EVIDENCE):
        errors.append("detail evidence must include NAC/Intune, checkout, or Jira board usage evidence")
    if observed.get("scroll_click_preserved_position") is not True:
        errors.append("scroll_click_preserved_position must be true")
    if "usage_status" not in str(observed.get("csv_header") or ""):
        errors.append("csv_header must include usage_status")
    current_build = current_ui_build_id()
    if observed.get("ui_build_id") != current_build:
        errors.append(f"ui_build_id must match current UI build {current_build}")

    detail = {
        **receipt_summary(payload),
        "expected_base": expected,
        "current_ui_build_id": current_build,
        "missing_modes": missing_modes,
        "is_gui_computer_use": is_gui_computer_use,
        "is_non_gui": is_non_gui,
        "errors": errors,
    }
    return not errors, errors, detail


def save_receipt(payload: dict[str, Any], expected: str | None = None) -> dict[str, Any]:
    valid, errors, detail = validate_receipt(payload, expected)
    if not valid:
        raise ValueError("; ".join(errors))
    payload = {
        **payload,
        "received_at": utc_now(),
        "receipt_contract": "infra-control-gui-computer-use-v1",
    }
    RECEIPT_PATH.parent.mkdir(parents=True, exist_ok=True)
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    RECEIPT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    archive_name = f"computer-use-user-test-{utc_now().replace(':', '').replace('+', 'Z')}.json"
    (ARCHIVE_DIR / archive_name).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"passed": True, "receipt": str(RECEIPT_PATH), "archive": str(ARCHIVE_DIR / archive_name), "detail": detail}


def load_current_receipt() -> dict[str, Any] | None:
    if not RECEIPT_PATH.exists():
        return None
    return json.loads(RECEIPT_PATH.read_text(encoding="utf-8"))
