#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import urlopen

from infra_control.db import DB_PATH, connect, encode_json, init_db


SOURCE_ID = "ops-review-sweeper"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def text_value(row: dict[str, object], key: str) -> str:
    value = row.get(key)
    return "" if value is None else str(value)


def numeric_value(row: dict[str, object], key: str) -> float | None:
    value = row.get(key)
    if value in (None, ""):
        return None
    return float(value)


def int_value(row: dict[str, object], key: str) -> int | None:
    value = row.get(key)
    if value in (None, ""):
        return None
    return int(value)


def payload_items(payload: dict[str, object], name: str) -> list[dict[str, object]]:
    section = payload.get(name)
    if isinstance(section, dict) and isinstance(section.get("items"), list):
        return [item for item in section["items"] if isinstance(item, dict)]
    legacy_name = f"{name}_items"
    if isinstance(payload.get(legacy_name), list):
        return [item for item in payload[legacy_name] if isinstance(item, dict)]
    return []


def import_snapshot(conn, payload: dict[str, object], imported_at: str | None = None) -> dict[str, int]:
    imported_at = imported_at or utc_now()
    source = payload.get("source") if isinstance(payload.get("source"), dict) else {}
    source_id = text_value(source, "source_id") or SOURCE_ID
    source_collected_at = text_value(source, "collected_at")
    software_items = payload_items(payload, "software")
    install_items = payload_items(payload, "installs")

    with conn:
        conn.execute("DELETE FROM software_inventory_summary")
        conn.execute("DELETE FROM software_inventory_installs")
        conn.executemany(
            """
            INSERT INTO software_inventory_summary (
              sw_id, software_name, install_count, legal_install_count,
              illegal_install_count, temp_install_count, license_amount,
              residue_amount, assign_amount, assign_uninstall_amount,
              collected_at, imported_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    text_value(item, "sw_id"),
                    text_value(item, "software_name"),
                    numeric_value(item, "install_count"),
                    numeric_value(item, "legal_install_count"),
                    numeric_value(item, "illegal_install_count"),
                    numeric_value(item, "temp_install_count"),
                    numeric_value(item, "license_amount"),
                    numeric_value(item, "residue_amount"),
                    numeric_value(item, "assign_amount"),
                    numeric_value(item, "assign_uninstall_amount"),
                    text_value(item, "collected_at") or source_collected_at,
                    imported_at,
                )
                for item in software_items
                if text_value(item, "sw_id")
            ],
        )
        conn.executemany(
            """
            INSERT INTO software_inventory_installs (
              install_id, sw_id, equip_id, software_name, equip_code, equip_name,
              asset_no, user_key, user_name, dept_name, is_licensed,
              install_date, report_date, collected_at, imported_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    text_value(item, "install_id"),
                    text_value(item, "sw_id"),
                    text_value(item, "equip_id"),
                    text_value(item, "software_name"),
                    text_value(item, "equip_code"),
                    text_value(item, "equip_name"),
                    text_value(item, "asset_no"),
                    text_value(item, "user_key"),
                    text_value(item, "user_name"),
                    text_value(item, "dept_name"),
                    int_value(item, "is_licensed"),
                    text_value(item, "install_date"),
                    text_value(item, "report_date"),
                    text_value(item, "collected_at") or source_collected_at,
                    imported_at,
                )
                for item in install_items
                if text_value(item, "install_id")
            ],
        )
        conn.execute(
            """
            INSERT OR REPLACE INTO integration_sources (
              id, name, kind, endpoint, status, last_sync_at, metadata_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source_id,
                "Sweeper Software Snapshot",
                "software-inventory-source",
                text_value(source, "endpoint"),
                "read-only",
                imported_at,
                encode_json({"source_collected_at": source_collected_at}),
            ),
        )
        conn.execute(
            """
            INSERT INTO software_inventory_runs (
              source_id, imported_at, source_collected_at,
              software_rows, install_rows, detail_json
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                source_id,
                imported_at,
                source_collected_at,
                len(software_items),
                len(install_items),
                encode_json({"transport": text_value(source, "transport") or "stdin"}),
            ),
        )
    return {"software_rows": len(software_items), "install_rows": len(install_items)}


def fetch_json(base_url: str, path: str) -> dict[str, object]:
    with urlopen(base_url.rstrip("/") + path, timeout=30) as response:
        data = json.load(response)
    if not isinstance(data, dict):
        raise ValueError(f"JSON object required from {path}")
    return data


def build_payload(base_url: str, install_limit: int) -> dict[str, object]:
    health = fetch_json(base_url, "/healthz")
    software = fetch_json(base_url, "/api/sweeper/software?limit=1000")
    installs = fetch_json(base_url, f"/api/sweeper/installs?limit={install_limit}")
    run = health.get("latest_sweeper_run") if isinstance(health.get("latest_sweeper_run"), dict) else {}
    return {
        "source": {
            "source_id": SOURCE_ID,
            "endpoint": base_url,
            "transport": "http",
            "collected_at": run.get("finished_at") or run.get("started_at") or "",
        },
        "software": software,
        "installs": installs,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=str(DB_PATH))
    parser.add_argument("--stdin", action="store_true")
    parser.add_argument("--base-url", default="http://127.0.0.1:8183")
    parser.add_argument("--install-limit", type=int, default=20000)
    args = parser.parse_args()

    db_path = Path(args.db)
    init_db(db_path)
    payload = json.load(sys.stdin) if args.stdin else build_payload(args.base_url, args.install_limit)
    with connect(db_path) as conn:
        result = import_snapshot(conn, payload)
    print(json.dumps({"status": "ok", **result}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
