from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from infra_control.computer_use_receipts import load_current_receipt, validate_receipt
from infra_control.connectors.excel_assets import REQUIRED_EXCEL_SOURCE_NAMES
from infra_control.connectors.snipe_ops import has_active_usage_evidence
from infra_control.db import connect, init_db
from server import asset_group


RECEIPT = ROOT / "var" / "receipts" / "ralph-quality-gate.json"
SECURITY_ENV = ROOT / "config" / "security.env"
AUTH_ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1", "10.0.0.17"}
SECRET_TERMS = ("password", "passwd", "비밀번호", "token", "secret", "마스터 계정")
REQUIRED_UI_MODES = (
    "assets",
    "asset_groups",
    "servers",
    "in_use",
    "unused",
    "needs_review",
    "owners",
    "locations",
    "maintenance",
    "changes",
    "sources",
)
KST = timezone(timedelta(hours=9), "KST")
KST_ZONE_NAME = "Asia/Seoul"


def kst_now() -> str:
    return datetime.now(KST).isoformat()


def has_jira_inprogress_evidence(metadata: dict[str, object]) -> bool:
    board = metadata.get("jira_board") if isinstance(metadata.get("jira_board"), dict) else {}
    lane = str(board.get("lane") or board.get("lane_label") or "").strip().lower().replace(" ", "")
    issue = str(board.get("issue") or "").strip().upper()
    return lane in {"inprogress", "inprogress(사용중)"} and issue.startswith("ITAM-")


def is_explicit_jira_endpoint_conflict(metadata: dict[str, object]) -> bool:
    board = metadata.get("jira_board") if isinstance(metadata.get("jira_board"), dict) else {}
    reconciliation = board.get("reconciliation") if isinstance(board.get("reconciliation"), dict) else {}
    return reconciliation.get("decision") in {
        "conflict_jira_inactive_endpoint_active",
        "conflict_jira_active_endpoint_inactive",
    }


def security_env_values() -> dict[str, str]:
    values = {
        "INFRA_CONTROL_BASIC_AUTH_USER": os.environ.get("INFRA_CONTROL_BASIC_AUTH_USER", "").strip(),
        "INFRA_CONTROL_BASIC_AUTH_PASSWORD": os.environ.get("INFRA_CONTROL_BASIC_AUTH_PASSWORD", "").strip(),
    }
    if values["INFRA_CONTROL_BASIC_AUTH_USER"] and values["INFRA_CONTROL_BASIC_AUTH_PASSWORD"]:
        return values
    if not SECURITY_ENV.exists():
        return values
    for line in SECURITY_ENV.read_text(encoding="utf-8").splitlines():
        if not line or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def auth_header(base: str) -> str | None:
    host = urllib.parse.urlparse(base).hostname or ""
    if host not in AUTH_ALLOWED_HOSTS:
        return None
    values = security_env_values()
    user = values.get("INFRA_CONTROL_BASIC_AUTH_USER", "")
    password = values.get("INFRA_CONTROL_BASIC_AUTH_PASSWORD", "")
    if not user or not password:
        return None
    token = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


def request_for(base: str, path: str) -> urllib.request.Request:
    request = urllib.request.Request(base.rstrip("/") + path)
    header = auth_header(base)
    if header:
        request.add_header("Authorization", header)
    return request


def get_json(base: str, path: str) -> object:
    with urllib.request.urlopen(request_for(base, path), timeout=6) as response:
        return json.loads(response.read().decode("utf-8"))


def get_text(base: str, path: str) -> str:
    with urllib.request.urlopen(request_for(base, path), timeout=6) as response:
        return response.read().decode("utf-8", errors="replace")


def db_checks() -> list[dict[str, object]]:
    init_db()
    checks: list[dict[str, object]] = []
    with connect() as conn:
        counts = {
            "assets": conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0],
            "servers": conn.execute("SELECT COUNT(*) FROM assets WHERE is_server = 1").fetchone()[0],
            "in_use": conn.execute("SELECT COUNT(*) FROM assets WHERE usage_status = '사용중'").fetchone()[0],
            "unused": conn.execute("SELECT COUNT(*) FROM assets WHERE usage_status = '미사용'").fetchone()[0],
            "owners": conn.execute(
                """
                SELECT COUNT(DISTINCT owner) FROM assets
                WHERE COALESCE(TRIM(owner), '') <> ''
                  AND TRIM(owner) NOT IN ('-', '미확인', '없음', 'N/A', 'n/a')
                """
            ).fetchone()[0],
            "locations": conn.execute("SELECT COUNT(*) FROM locations").fetchone()[0],
        }
        asset_groups = {asset_group(row[0]) for row in conn.execute("SELECT asset_tag FROM assets").fetchall()}
        checks.append({"id": "db_assets_present", "passed": counts["assets"] > 0, "detail": counts})
        checks.append({"id": "db_asset_groups_present", "passed": len(asset_groups) > 0, "detail": {"asset_groups": len(asset_groups)}})
        checks.append({"id": "db_servers_present", "passed": counts["servers"] > 0, "detail": counts})
        checks.append({"id": "db_usage_present", "passed": counts["in_use"] > 0, "detail": counts})
        checks.append({"id": "db_locations_present", "passed": counts["locations"] > 0, "detail": counts})
        checks.append({"id": "db_owners_present", "passed": counts["owners"] > 0, "detail": counts})
        required_sources = conn.execute(
            """
            SELECT name, metadata_json FROM integration_sources
            WHERE name IN ({})
            """
            .format(",".join("?" for _ in REQUIRED_EXCEL_SOURCE_NAMES)),
            REQUIRED_EXCEL_SOURCE_NAMES,
        ).fetchall()
        source_counts: dict[str, int] = {}
        for source in required_sources:
            try:
                source_counts[source["name"]] = int(json.loads(source["metadata_json"]).get("record_count") or 0)
            except (TypeError, ValueError, json.JSONDecodeError):
                source_counts[source["name"]] = 0
        missing_required = [name for name in REQUIRED_EXCEL_SOURCE_NAMES if source_counts.get(name, 0) <= 0]
        checks.append(
            {
                "id": "required_download_excels_imported",
                "passed": not missing_required,
                "detail": {"missing": missing_required, "record_counts": source_counts},
            }
        )
        laptop_rows = conn.execute(
            """
            SELECT asset_tag, usage_status, usage_reason FROM assets
            WHERE usage_reason = '노트북 기본 사용중 판정'
            """
        ).fetchall()
        checks.append(
            {
                "id": "no_laptop_default_usage_without_nac_intune",
                "passed": len(laptop_rows) == 0,
                "detail": {"violations": [dict(row) for row in laptop_rows[:20]]},
            }
        )
        snipe_used_without_evidence: list[dict[str, object]] = []
        for row in conn.execute(
            """
            SELECT asset_tag, category, model, usage_status, usage_reason, metadata_json FROM assets
            WHERE source = 'snipe-ops' AND usage_status = '사용중'
            """
        ).fetchall():
            try:
                metadata = json.loads(row["metadata_json"] or "{}")
            except json.JSONDecodeError:
                metadata = {}
            source_row = metadata.get("source_row")
            has_active_evidence = isinstance(source_row, dict) and has_active_usage_evidence(
                {str(k): str(v or "") for k, v in source_row.items()}
            )
            if not has_active_evidence and not has_jira_inprogress_evidence(metadata):
                snipe_used_without_evidence.append(
                    {
                        "asset_tag": row["asset_tag"],
                        "usage_status": row["usage_status"],
                        "usage_reason": row["usage_reason"],
                    }
                )
        checks.append(
            {
                "id": "snipe_usage_requires_nac_intune_or_checkout_evidence",
                "passed": len(snipe_used_without_evidence) == 0,
                "detail": {"violations": snipe_used_without_evidence[:20]},
            }
        )
        active_not_used: list[dict[str, object]] = []
        for row in conn.execute(
            """
            SELECT asset_tag, usage_status, usage_reason, metadata_json FROM assets
            WHERE usage_status <> '사용중'
            """
        ).fetchall():
            try:
                metadata = json.loads(row["metadata_json"] or "{}")
            except json.JSONDecodeError:
                metadata = {}
            source_row = metadata.get("source_row")
            if (
                isinstance(source_row, dict)
                and has_active_usage_evidence({str(k): str(v or "") for k, v in source_row.items()})
                and not is_explicit_jira_endpoint_conflict(metadata)
            ):
                active_not_used.append(
                    {
                        "asset_tag": row["asset_tag"],
                        "usage_status": row["usage_status"],
                        "usage_reason": row["usage_reason"],
                    }
                )
        checks.append(
            {
                "id": "infra_active_assets_marked_in_use",
                "passed": len(active_not_used) == 0,
                "detail": {"violations": active_not_used[:20]},
            }
        )
        blob = "\n".join(
            row[0] or ""
            for row in conn.execute(
                "SELECT metadata_json FROM assets"
            ).fetchall()
        ).lower()
        leaked = [term for term in SECRET_TERMS if term.lower() in blob]
        checks.append({"id": "no_secret_terms_in_operator_payload", "passed": not leaked, "detail": {"terms": leaked}})
    return checks


def http_checks(base: str) -> list[dict[str, object]]:
    checks: list[dict[str, object]] = []
    summary = get_json(base, "/api/summary")
    checks.append({"id": "http_summary", "passed": int(summary.get("assets", 0)) > 0, "detail": summary})
    insights = get_json(base, "/api/insights")
    checks.append(
        {
            "id": "http_insights",
            "passed": isinstance(insights, dict)
            and isinstance(insights.get("summary"), dict)
            and isinstance(insights.get("usage_reasons"), list)
            and isinstance(insights.get("sources"), list)
            and isinstance(insights.get("computer_use"), dict),
            "detail": {
                "review_assets": len(insights.get("review_assets", [])) if isinstance(insights, dict) else 0,
                "usage_reasons": len(insights.get("usage_reasons", [])) if isinstance(insights, dict) else 0,
                "sources": len(insights.get("sources", [])) if isinstance(insights, dict) else 0,
            },
        }
    )
    for name, path in {
        "http_assets": "/api/assets",
        "http_asset_groups": "/api/asset-groups",
        "http_servers": "/api/servers",
        "http_owners": "/api/owners",
        "http_locations": "/api/locations",
        "http_maintenance": "/api/maintenance",
        "http_changes": "/api/changes",
        "http_sources": "/api/sources",
    }.items():
        data = get_json(base, path)
        requires_rows = name not in {"http_maintenance"}
        checks.append(
            {
                "id": name,
                "passed": isinstance(data, list) and (len(data) > 0 or not requires_rows),
                "detail": {"count": len(data) if isinstance(data, list) else 0},
            }
        )
    csv_body = get_text(base, "/api/export/assets.csv")
    checks.append(
        {
            "id": "csv_export_has_required_columns",
            "passed": all(col in csv_body.splitlines()[0] for col in ("asset_group", "usage_status", "serial", "owner", "primary_ip")),
            "detail": {"header": csv_body.splitlines()[0] if csv_body else ""},
        }
    )
    asset_group_rows = get_json(base, "/api/asset-groups")
    checks.append(
        {
            "id": "http_asset_groups_have_usage_breakdown",
            "passed": isinstance(asset_group_rows, list)
            and len(asset_group_rows) > 0
            and all("asset_group" in row and "in_use" in row and "needs_review" in row for row in asset_group_rows[:10]),
            "detail": {"count": len(asset_group_rows) if isinstance(asset_group_rows, list) else 0},
        }
    )
    return checks


def static_checks() -> list[dict[str, object]]:
    index = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    script = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
    dynamic_mode_renderer = 'data-mode="${id}"' in script and "const nav = [" in script
    missing_modes = [
        mode
        for mode in REQUIRED_UI_MODES
        if (
            f'data-mode="{mode}"' not in index
            and not (dynamic_mode_renderer and f'["{mode}",' in script)
        )
    ]
    missing_handlers = [
        mode
        for mode in ("asset_groups", "owners", "sources", "maintenance", "changes")
        if f'mode === "{mode}"' not in script and f"mode === \"{mode}\"" not in script
    ]
    missing_operator_signals = [
        token
        for token in ('id="insights"', "renderInsights", "decision-band", "review-list", "usage_reasons", "computer_use")
        if token not in index and token not in script
    ]
    return [
        {
            "id": "ui_required_modes_present",
            "passed": not missing_modes,
            "detail": {"missing": missing_modes},
        },
        {
            "id": "ui_required_modes_handled",
            "passed": not missing_handlers,
            "detail": {"missing": missing_handlers},
        },
        {
            "id": "ui_operator_decision_signals_present",
            "passed": not missing_operator_signals,
            "detail": {"missing": missing_operator_signals},
        },
    ]


def receipt_checks(require_computer_use: bool, base: str, computer_use_base: str | None) -> list[dict[str, object]]:
    checks: list[dict[str, object]] = []
    for name in ("bootstrap",):
        path = ROOT / "var" / "receipts" / f"{name}.json"
        checks.append({"id": f"receipt_{name}", "passed": path.exists(), "detail": str(path)})
    computer_use = ROOT / "var" / "receipts" / "computer-use-user-test.json"
    if not require_computer_use:
        checks.append({"id": "receipt_computer_use", "passed": True, "detail": str(computer_use)})
        return checks
    expected_base = computer_use_base or base
    detail: dict[str, object] = {"path": str(computer_use)}
    passed = False
    if computer_use.exists():
        try:
            payload = load_current_receipt() or {}
            passed, _, receipt_detail = validate_receipt(payload, expected_base)
            detail.update(receipt_detail)
        except json.JSONDecodeError as exc:
            detail["error"] = str(exc)
        except ValueError as exc:
            detail["error"] = str(exc)
    checks.append({"id": "receipt_computer_use", "passed": passed, "detail": detail})
    return checks


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    parser.add_argument("--computer-use-base")
    parser.add_argument("--iteration", type=int, default=1)
    parser.add_argument("--require-computer-use", action="store_true")
    args = parser.parse_args()

    checks = [
        *db_checks(),
        *http_checks(args.base),
        *static_checks(),
        *receipt_checks(args.require_computer_use, args.base, args.computer_use_base),
    ]
    failed = [item for item in checks if not item["passed"]]
    payload = {
        "passed": not failed,
        "timezone": KST_ZONE_NAME,
        "generated_at": kst_now(),
        "iteration": args.iteration,
        "base": args.base,
        "checks": checks,
        "failed": failed,
    }
    RECEIPT.parent.mkdir(parents=True, exist_ok=True)
    RECEIPT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    raise SystemExit(0 if payload["passed"] else 1)


if __name__ == "__main__":
    main()
