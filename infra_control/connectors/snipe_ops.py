from __future__ import annotations

import csv
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from infra_control.db import connect, encode_json, init_db


ROOT = Path(__file__).resolve().parents[2]
ROOT_OPS_DIR = Path("/root/snipe-it/frontend-next/var/ops")
LOCAL_OPS_DIR = ROOT / "var" / "imports" / "snipe-ops"
DEFAULT_OPS_DIR = Path(os.environ["INFRA_CONTROL_SNIPE_OPS_DIR"]) if os.environ.get("INFRA_CONTROL_SNIPE_OPS_DIR") else None


SERVER_HINTS = ("server", "서버", "idrac", "ilo", "ipmi", "vm-", "esxi", "hyper-v")
LAPTOP_HINTS = ("notebook", "laptop", "macbook", "elitebook", "그램", "노트북", "thinkpad", "surface", "갤럭시북")
TERMINAL_UNUSED_HINTS = ("폐기", "불용", "매각", "처분", "분실", "수리", "반납완료")
IDLE_HINTS = ("재고", "유휴", "stock", "idle", "미사용", "반납", "입력x")
RETIRED_USAGE_STATUS = "종료/제외"


def can_read(path: Path) -> bool:
    try:
        return path.is_file() and os.access(path, os.R_OK)
    except OSError:
        return False


def default_ops_dir() -> Path:
    if DEFAULT_OPS_DIR is not None:
        return DEFAULT_OPS_DIR
    for path in (LOCAL_OPS_DIR, ROOT_OPS_DIR):
        if can_read(path / "recon_stage.csv"):
            return path
    return LOCAL_OPS_DIR


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def stable_id(*parts: str) -> str:
    raw = "|".join(part or "" for part in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8-sig", errors="replace") as handle:
        return list(csv.DictReader(handle))


def read_json(path: Path) -> Any:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8", errors="replace"))


def user_directory(ops_dir: Path) -> dict[str, dict[str, str]]:
    users: dict[str, dict[str, str]] = {}
    for row in read_csv(ops_dir / "organization_users.csv"):
        upn = (row.get("userPrincipalName") or "").strip().lower()
        local = upn.split("@", 1)[0] if upn else ""
        if local:
            users[local] = row
    return users


def infer_server(row: dict[str, str]) -> bool:
    fields = [
        row.get("ledger_item_kind", ""),
        row.get("ledger_model", ""),
        row.get("intune_model", ""),
        row.get("intune_device_name", ""),
        row.get("intune_managed_device_name", ""),
        row.get("ledger_notes", ""),
        row.get("asset_tag", ""),
    ]
    text = " ".join(fields).lower()
    return any(hint in text for hint in SERVER_HINTS)


def infer_laptop(row: dict[str, str]) -> bool:
    text = " ".join(
        [
            row.get("ledger_item_kind", ""),
            row.get("ledger_model", ""),
            row.get("intune_model", ""),
            row.get("ledger_notes", ""),
        ]
    ).lower()
    return any(hint in text for hint in LAPTOP_HINTS)


def active_usage_evidence(row: dict[str, str]) -> list[str]:
    has_checkout = bool(str(row.get("checkout_candidate_username") or "").strip())
    has_intune_user = bool(str(row.get("intune_user_upn") or row.get("intune_user_localpart") or "").strip())
    has_nac_user = bool(str(row.get("nac_username_norm") or row.get("nac_username") or "").strip())
    recent_intune = str(row.get("recent_sync_intune_30d") or "").strip().upper() == "Y"
    recent_nac = str(row.get("nac_recently_used") or "").strip().upper() == "Y" or str(
        row.get("recent_seen_nac_14d") or ""
    ).strip().upper() == "Y"
    status_text = " ".join(
        [
            row.get("nac_status", ""),
            row.get("operational_status", ""),
            row.get("user_status", ""),
        ]
    ).lower()
    evidence: list[str] = []
    if has_intune_user and recent_intune:
        evidence.append("Intune 최근 동기화")
    if has_nac_user and recent_nac:
        evidence.append("NAC 최근 접속")
    if "사용중" in status_text:
        evidence.append("NAC/운영상태 사용중")
    if has_checkout:
        evidence.append("대여/checkout 배정")
    return evidence


def has_active_usage_evidence(row: dict[str, str]) -> bool:
    return bool(active_usage_evidence(row))


def usage_reason_from_evidence(evidence: list[str]) -> str:
    if any(item.startswith("NAC") for item in evidence) and any(item.startswith("Intune") for item in evidence):
        return "NAC+Intune 사용 근거"
    if any(item.startswith("NAC") for item in evidence):
        return "NAC 사용 근거"
    if any(item.startswith("Intune") for item in evidence):
        return "Intune 사용 근거"
    if any("checkout" in item for item in evidence):
        return "대여/checkout 배정 근거"
    return "판정 근거 부족"


def infer_usage(row: dict[str, str]) -> tuple[str, str]:
    text = " ".join(str(value or "") for value in row.values()).lower()
    if any(hint in text for hint in TERMINAL_UNUSED_HINTS):
        return RETIRED_USAGE_STATUS, "명시적 제외 상태"
    evidence = active_usage_evidence(row)
    if evidence:
        return "사용중", usage_reason_from_evidence(evidence)
    if any(hint in text for hint in IDLE_HINTS):
        return "미사용", "예외 키워드 기준"
    if infer_laptop(row):
        return "확인필요", "NAC/Intune 근거 없음"
    if infer_server(row):
        return "확인필요", "서버 운영 상태 미확인"
    return "확인필요", "판정 근거 부족"


def normalize_asset(row: dict[str, str], users: dict[str, dict[str, str]], now: str) -> dict[str, Any]:
    owner_key = (
        row.get("checkout_candidate_username")
        or row.get("intune_user_localpart")
        or row.get("nac_username_norm")
        or row.get("ledger_owner_norm")
        or ""
    ).strip().lower()
    user = users.get(owner_key, {})
    hostname = row.get("intune_device_name") or row.get("intune_managed_device_name") or ""
    manufacturer = row.get("ledger_manufacturer") or row.get("intune_manufacturer") or ""
    model = row.get("ledger_model") or row.get("intune_model") or ""
    asset_tag = row.get("asset_tag") or row.get("ledger_asset_tag") or row.get("intune_asset_tag_candidate") or ""
    serial = row.get("serial_number_norm") or row.get("ledger_serial") or row.get("intune_serial") or ""
    category = row.get("ledger_item_kind") or "asset"
    evidence = active_usage_evidence(row)
    metadata = {
        "join_method": row.get("join_method"),
        "asset_presence_status": row.get("asset_presence_status"),
        "user_confidence_status": row.get("user_confidence_status"),
        "ops_priority": row.get("ops_priority"),
        "review_bucket": row.get("review_bucket"),
        "recon_decision": row.get("recon_decision"),
        "recon_reason": row.get("recon_reason"),
        "intune_last_sync_utc": row.get("intune_last_sync_utc"),
        "nac_last_seen_utc": row.get("nac_last_seen_utc"),
        "usage_evidence": evidence,
        "usage_basis": usage_reason_from_evidence(evidence) if evidence else "",
        "source_row": row,
    }
    usage_status, usage_reason = infer_usage(row)
    return {
        "id": stable_id("snipe-ops", asset_tag, serial, hostname),
        "source": "snipe-ops",
        "external_id": asset_tag,
        "asset_tag": asset_tag,
        "hostname": hostname,
        "serial": serial,
        "model": model,
        "manufacturer": manufacturer,
        "category": category,
        "status": row.get("asset_presence_status") or row.get("ledger_status") or "unknown",
        "owner": user.get("displayName") or owner_key,
        "owner_email": user.get("mail") or row.get("intune_user_upn") or "",
        "department": user.get("department") or user.get("companyName") or "",
        "primary_ip": "",
        "os": row.get("intune_operating_system") or "",
        "location": "",
        "room": "",
        "rack": "",
        "rack_unit": "",
        "maintenance_date": "",
        "usage_status": usage_status,
        "usage_reason": usage_reason,
        "purpose": row.get("ledger_notes") or "",
        "ports": "",
        "is_server": 1 if infer_server(row) else 0,
        "is_virtual": 0,
        "is_network": 0,
        "metadata_json": encode_json(metadata),
        "updated_at": now,
    }


def import_assets(ops_dir: Path | None = None) -> dict[str, Any]:
    init_db()
    ops_dir = ops_dir or default_ops_dir()
    now = utc_now()
    recon_rows = read_csv(ops_dir / "recon_stage.csv")
    users = user_directory(ops_dir)
    assets = [normalize_asset(row, users, now) for row in recon_rows]
    active_asset_ids = {asset["id"] for asset in assets}
    dashboard = read_json(ops_dir / "ops_dashboard.json") or {}
    report = read_json(ops_dir / "graph_user_directory_report.json") or {}
    history = read_json(ops_dir / "asset_change_history.json") or {}

    with connect() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO integration_sources
            (id, name, kind, endpoint, status, last_sync_at, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "snipe-ops",
                "Snipe-IT Ops Snapshot",
                "asset-source",
                str(ops_dir),
                "read-only",
                now,
                encode_json({"ops_dashboard": dashboard, "graph_user_directory_report": report}),
            ),
        )
        if active_asset_ids:
            placeholders = ",".join("?" for _ in active_asset_ids)
            conn.execute(
                f"DELETE FROM assets WHERE source = ? AND id NOT IN ({placeholders})",
                ("snipe-ops", *active_asset_ids),
            )
        else:
            conn.execute("DELETE FROM assets WHERE source = ?", ("snipe-ops",))
        conn.executemany(
            """
            INSERT OR REPLACE INTO assets
            (id, source, external_id, asset_tag, hostname, serial, model, manufacturer, category,
             status, owner, owner_email, department, primary_ip, os, location, room, rack,
             rack_unit, maintenance_date, usage_status, usage_reason, purpose, ports,
             is_server, is_virtual, is_network, metadata_json, updated_at)
            VALUES
            (:id, :source, :external_id, :asset_tag, :hostname, :serial, :model, :manufacturer,
             :category, :status, :owner, :owner_email, :department, :primary_ip, :os, :location,
             :room, :rack, :rack_unit, :maintenance_date, :usage_status, :usage_reason,
             :purpose, :ports, :is_server, :is_virtual, :is_network, :metadata_json, :updated_at)
            """,
            assets,
        )
        change_items = history.get("items", []) if isinstance(history, dict) else []
        for item in change_items:
            asset_tag = item.get("assetTag")
            action = item.get("action")
            field = item.get("field") or "owner"
            old_value = item.get("previousOwnerName") or item.get("previousValue")
            new_value = item.get("nextOwnerName") or item.get("nextValue")
            # Skip if an identical snipe-ops change is already recorded, so the
            # hourly sync stops re-inserting the same no-op event every run.
            already = conn.execute(
                """
                SELECT 1 FROM change_history
                WHERE source = 'snipe-ops'
                  AND asset_tag IS ? AND action IS ? AND field IS ?
                  AND old_value IS ? AND new_value IS ?
                LIMIT 1
                """,
                (asset_tag, action, field, old_value, new_value),
            ).fetchone()
            if already:
                continue
            conn.execute(
                """
                INSERT OR REPLACE INTO change_history
                (id, asset_tag, changed_at, actor, action, field, old_value, new_value, reason, source, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item.get("id") or stable_id("change", item.get("assetTag", ""), item.get("timestamp", "")),
                    asset_tag,
                    item.get("timestamp"),
                    item.get("actor"),
                    action,
                    field,
                    old_value,
                    new_value,
                    item.get("note"),
                    "snipe-ops",
                    encode_json(item),
                ),
            )

    return {
        "imported_assets": len(assets),
        "likely_servers": sum(asset["is_server"] for asset in assets),
        "users": len(users),
        "change_history": len(history.get("items", [])) if isinstance(history, dict) else 0,
        "source": str(ops_dir),
        "synced_at": now,
    }


if __name__ == "__main__":
    print(json.dumps(import_assets(), ensure_ascii=False, indent=2))
