from __future__ import annotations

import argparse
import json
import re
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from infra_control.db import connect, encode_json, init_db


LANES = {
    "new": ("신규", "미사용", "Jira New(신규)"),
    "stock": ("재고/유휴", "미사용", "Jira Stock(재고/유휴)"),
    "inprogress": ("In Progress(사용중)", "사용중", "Jira In Progress(사용중)"),
}

ACTIVE_ENDPOINT_EVIDENCE = (
    "NAC 사용 근거",
    "Intune 사용 근거",
    "NAC+Intune 사용 근거",
    "NAC 최근 접속",
    "Intune 최근 동기화",
    "대여/checkout 배정",
)
VOLATILE_JIRA_BOARD_KEYS = {"synced_at", "last_seen_at"}
ENDPOINT_CATEGORY_TERMS = (
    "노트북",
    "데스크탑",
    "데스크톱",
    "일체형",
    "PC",
    "맥북",
    "MacBook",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def compact(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def clean_owner(value: Any) -> str:
    owner = compact(value)
    if owner in {"-", "미확인", "없음", "N/A", "n/a"}:
        return ""
    return owner


def load_records(path: Path) -> list[dict[str, Any]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError("Jira board import JSON must be a list")
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        asset_tag = compact(item.get("asset_tag")).upper()
        lane = compact(item.get("lane")).lower().replace("_", "")
        issue = compact(item.get("issue")).upper()
        if lane == "in_progress":
            lane = "inprogress"
        if not asset_tag or lane not in LANES or not issue.startswith("ITAM-"):
            continue
        key = f"{asset_tag}|{issue}"
        if key in seen:
            continue
        seen.add(key)
        records.append(
            {
                "asset_tag": asset_tag,
                "lane": lane,
                "issue": issue,
                "owner": clean_owner(item.get("owner")),
                "page": item.get("page"),
                "lines": item.get("lines") if isinstance(item.get("lines"), list) else [],
            }
        )
    return records


def parse_metadata(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value or "{}"))
    except json.JSONDecodeError:
        parsed = {}
    return parsed if isinstance(parsed, dict) else {}


def stable_jira_board(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {key: item for key, item in value.items() if key not in VOLATILE_JIRA_BOARD_KEYS}


def missing_record(record: dict[str, Any], source_file: str) -> dict[str, Any]:
    lane_label, _, _ = LANES[record["lane"]]
    item = {
        "asset_tag": record["asset_tag"],
        "issue": record["issue"],
        "lane": record["lane"],
        "lane_label": lane_label,
        "owner": record.get("owner") or "",
        "page": record.get("page"),
        "source_file": source_file,
    }
    for key in ("summary", "manufacturer", "model", "specification"):
        value = compact(record.get(key))
        if value:
            item[key] = value
    lines = [compact(line) for line in record.get("lines", []) if compact(line)]
    if lines:
        item["lines"] = lines
    return item


def has_active_endpoint_evidence(metadata: dict[str, Any]) -> bool:
    blob = json.dumps(metadata, ensure_ascii=False)
    return any(term in blob for term in ACTIVE_ENDPOINT_EVIDENCE)


def is_endpoint_asset(existing: dict[str, Any], metadata: dict[str, Any]) -> bool:
    blob = " ".join(
        [
            compact(existing.get("asset_tag")),
            compact(existing.get("category")),
            compact(existing.get("model")),
            compact((metadata.get("source_row") or {}).get("category")),
            compact((metadata.get("usage_source_row") or {}).get("ledger_item_kind")),
            compact((metadata.get("usage_source_row") or {}).get("ledger_model")),
        ]
    )
    return any(term.lower() in blob.lower() for term in ENDPOINT_CATEGORY_TERMS)


def reconcile_usage(
    lane: str,
    existing: dict[str, Any],
    metadata: dict[str, Any],
    lane_label: str,
    jira_usage_status: str,
    jira_usage_reason: str,
) -> tuple[str, str, str, dict[str, Any]]:
    endpoint_active = has_active_endpoint_evidence(metadata)
    endpoint_relevant = is_endpoint_asset(existing, metadata)
    jira_active = lane == "inprogress"
    jira_inactive = lane in {"new", "stock"}

    decision = "jira_only"
    review_required = False
    usage_status = jira_usage_status
    usage_reason = jira_usage_reason
    status = lane_label

    if jira_inactive and endpoint_active:
        decision = "conflict_jira_inactive_endpoint_active"
        review_required = True
        usage_status = "확인필요"
        usage_reason = "Jira/NAC-Intune 충돌 확인필요"
        status = "확인필요"
    elif jira_active and endpoint_relevant and not endpoint_active:
        decision = "conflict_jira_active_endpoint_inactive"
        review_required = True
        usage_status = "확인필요"
        usage_reason = "Jira/NAC-Intune 충돌 확인필요"
        status = "확인필요"
    elif jira_active and endpoint_active:
        decision = "match_active"
    elif jira_inactive and not endpoint_active:
        decision = "match_inactive"

    reconciliation = {
        "decision": decision,
        "review_required": review_required,
        "jira_signal": "active" if jira_active else "inactive",
        "endpoint_signal": "active" if endpoint_active else ("inactive" if endpoint_relevant else "unknown"),
        "endpoint_relevant": endpoint_relevant,
    }
    return status, usage_status, usage_reason, reconciliation


def change(
    conn: Any,
    asset_tag: str,
    field: str,
    old_value: Any,
    new_value: Any,
    actor: str,
    reason: str,
    now: str,
    source_id: str,
) -> bool:
    old = compact(old_value)
    new = compact(new_value)
    if old == new:
        return False
    conn.execute(f"UPDATE assets SET {field} = ?, updated_at = ? WHERE asset_tag = ?", (new, now, asset_tag))
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
            "jira_board_import",
            field,
            old,
            new,
            reason,
            source_id,
            "{}",
        ),
    )
    return True


def apply_records(
    records: list[dict[str, Any]],
    *,
    source_file: str,
    source_id: str = "jira-board-pdf",
    source_name: str = "Jira Hardware Board PDF",
    source_kind: str = "asset-status-reference",
    actor: str = "jira-board-import",
    dry_run: bool = False,
    fail_on_missing: bool = True,
) -> dict[str, Any]:
    init_db()
    now = utc_now()
    lane_counts = Counter(record["lane"] for record in records)
    field_changes: Counter[str] = Counter()
    missing: list[str] = []
    missing_records: list[dict[str, Any]] = []
    updated_assets: set[str] = set()

    with connect() as conn:
        for record in records:
            row = conn.execute("SELECT * FROM assets WHERE asset_tag = ?", (record["asset_tag"],)).fetchone()
            if not row:
                missing.append(record["asset_tag"])
                missing_records.append(missing_record(record, source_file))
                continue
            existing = dict(row)
            lane_label, jira_usage_status, jira_usage_reason = LANES[record["lane"]]
            metadata = parse_metadata(existing.get("metadata_json"))
            _, _, _, reconciliation = reconcile_usage(
                record["lane"],
                existing,
                metadata,
                lane_label,
                jira_usage_status,
                jira_usage_reason,
            )
            existing_board = metadata.get("jira_board") if isinstance(metadata.get("jira_board"), dict) else {}
            next_board = {
                "lane": record["lane"],
                "lane_label": lane_label,
                "issue": record["issue"],
                "page": record.get("page"),
                "source_file": source_file,
                "reconciliation": reconciliation,
            }
            if record.get("owner"):
                next_board["owner"] = record["owner"]
            if record.get("lines"):
                next_board["lines"] = record["lines"]

            board_changed = encode_json(stable_jira_board(existing_board)) != encode_json(next_board)

            if dry_run:
                if board_changed:
                    field_changes["metadata_json"] += 1
                continue

            changed_any = False
            if board_changed:
                next_board["synced_at"] = now
                metadata["jira_board"] = next_board
                new_metadata = encode_json(metadata)
                conn.execute("UPDATE assets SET metadata_json = ?, updated_at = ? WHERE asset_tag = ?", (new_metadata, now, record["asset_tag"]))
                field_changes["metadata_json"] += 1
                changed_any = True
            if changed_any:
                updated_assets.add(record["asset_tag"])

        if not dry_run:
            conn.execute(
                """
                INSERT OR REPLACE INTO integration_sources
                (id, name, kind, endpoint, status, last_sync_at, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source_id,
                    source_name,
                    source_kind,
                    source_file,
                    "read-only",
                    now,
                    encode_json(
                        {
                            "record_count": len(records),
                            "lane_counts": dict(lane_counts),
                            "missing_assets": missing,
                            "missing_records": missing_records,
                            "field_changes": dict(field_changes),
                            "changed_assets": len(updated_assets),
                            "source_mode": "read-only",
                            "writes_asset_fields": False,
                        }
                    ),
                ),
            )

    return {
        "passed": not missing if fail_on_missing else True,
        "dry_run": dry_run,
        "source_file": source_file,
        "records": len(records),
        "lane_counts": dict(lane_counts),
        "missing_assets": missing,
        "missing_records": missing_records,
        "updated_assets": len(updated_assets),
        "field_changes": dict(field_changes),
        "synced_at": now,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("records_json", type=Path)
    parser.add_argument("--source-file", default="")
    parser.add_argument("--actor", default="jira-board-import")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--receipt", type=Path)
    args = parser.parse_args()

    source_file = args.source_file or args.records_json.name
    result = apply_records(load_records(args.records_json), source_file=source_file, actor=args.actor, dry_run=args.dry_run)
    if args.receipt:
        args.receipt.parent.mkdir(parents=True, exist_ok=True)
        args.receipt.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result["passed"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
