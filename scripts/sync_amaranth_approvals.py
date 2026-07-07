#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence


SCRIPT_VERSION = "2026-05-28.1"
DEFAULT_ROOT = Path(os.environ.get("INFRA_CONTROL_ROOT", "/opt/infra-asset-manager"))
DEFAULT_UPSTREAM_COMMAND = shlex.split(
    os.environ.get(
        "INFRA_CONTROL_AMARANTH_UPSTREAM_COMMAND",
        str(DEFAULT_ROOT / "scripts" / "run_amaranth_approval_sync.sh"),
    )
)
DEFAULT_UPSTREAM_SNAPSHOT = Path(
    os.environ.get("INFRA_CONTROL_AMARANTH_SNAPSHOT", str(DEFAULT_ROOT / "var" / "approval" / "approval_tasks.json"))
)
DEFAULT_UPSTREAM_DETAILS = Path(
    os.environ.get("INFRA_CONTROL_AMARANTH_DETAILS", str(DEFAULT_ROOT / "var" / "approval" / "approval_details.json"))
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_json(path: Path) -> dict[str, object]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SystemExit(f"missing Amaranth approval snapshot: {path}") from exc
    if not isinstance(data, dict):
        raise SystemExit(f"Amaranth approval snapshot is not an object: {path}")
    return data


def write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def approval_count(snapshot: dict[str, object]) -> int:
    counts = snapshot.get("counts")
    if isinstance(counts, dict):
        try:
            return int(counts.get("total") or 0)
        except (TypeError, ValueError):
            return 0
    items = snapshot.get("items")
    return len(items) if isinstance(items, list) else 0


def snapshot_sync_time(snapshot: dict[str, object]) -> str:
    for key in ("lastSyncedAt", "generated_at_utc", "generatedAt"):
        value = str(snapshot.get(key) or "").strip()
        if value:
            return value
    return utc_now()


def run_upstream(command: Sequence[str] | None) -> dict[str, object]:
    if not command:
        return {"skipped": True, "stdout": "", "stderr": "", "returncode": 0}
    completed = subprocess.run(command, check=False, capture_output=True, text=True)
    result = {
        "skipped": False,
        "command": list(command),
        "returncode": completed.returncode,
        "stdout": completed.stdout[-4000:],
        "stderr": completed.stderr[-4000:],
    }
    if completed.returncode != 0:
        raise SystemExit(f"Amaranth approval upstream sync failed: {json.dumps(result, ensure_ascii=False)}")
    return result


def upsert_source(
    *,
    db_path: Path,
    source_id: str,
    last_sync_at: str,
    record_count: int,
    upstream_snapshot: Path,
    copied_snapshot: Path,
    copied_details: bool,
) -> None:
    metadata = {
        "record_count": record_count,
        "source_mode": "read-only",
        "script_version": SCRIPT_VERSION,
        "upstream_snapshot": str(upstream_snapshot),
        "copied_snapshot": str(copied_snapshot),
        "copied_details": copied_details,
    }
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO integration_sources
              (id, name, kind, endpoint, status, last_sync_at, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              name = excluded.name,
              kind = excluded.kind,
              endpoint = excluded.endpoint,
              status = excluded.status,
              last_sync_at = excluded.last_sync_at,
              metadata_json = excluded.metadata_json
            """,
            (
                source_id,
                "Amaranth Approval Snapshot",
                "approval-source",
                str(upstream_snapshot),
                "read-only",
                last_sync_at,
                json.dumps(metadata, ensure_ascii=False),
            ),
        )


def sync_amaranth_approvals(
    *,
    root: Path = DEFAULT_ROOT,
    db_path: Path | None = None,
    upstream_command: Sequence[str] | None = DEFAULT_UPSTREAM_COMMAND,
    upstream_snapshot: Path = DEFAULT_UPSTREAM_SNAPSHOT,
    upstream_details: Path | None = None,
    receipt_path: Path | None = None,
) -> dict[str, object]:
    root = Path(root)
    upstream_snapshot = Path(upstream_snapshot)
    if upstream_details is None:
        upstream_details = (
            DEFAULT_UPSTREAM_DETAILS
            if upstream_snapshot == DEFAULT_UPSTREAM_SNAPSHOT
            else upstream_snapshot.with_name("approval_details.json")
        )
    else:
        upstream_details = Path(upstream_details)
    db_path = Path(db_path) if db_path else root / "var" / "infra-control.sqlite3"
    receipt_path = Path(receipt_path) if receipt_path else root / "var" / "receipts" / "amaranth-approval-sync.json"
    target_snapshot = root / "var" / "approval_tasks.json"
    target_details = root / "var" / "approval_details.json"

    started_at = utc_now()
    upstream_result = run_upstream(upstream_command)
    snapshot = read_json(upstream_snapshot)
    target_snapshot.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(upstream_snapshot, target_snapshot)

    copied_details = False
    if upstream_details.exists():
        shutil.copy2(upstream_details, target_details)
        copied_details = True

    record_count = approval_count(snapshot)
    last_sync_at = snapshot_sync_time(snapshot)
    upsert_source(
        db_path=db_path,
        source_id="amaranth-approvals",
        last_sync_at=last_sync_at,
        record_count=record_count,
        upstream_snapshot=upstream_snapshot,
        copied_snapshot=target_snapshot,
        copied_details=copied_details,
    )

    payload = {
        "passed": True,
        "script_version": SCRIPT_VERSION,
        "started_at": started_at,
        "finished_at": utc_now(),
        "source_id": "amaranth-approvals",
        "last_sync_at": last_sync_at,
        "record_count": record_count,
        "upstream": upstream_result,
        "snapshot": str(target_snapshot),
        "details": str(target_details) if copied_details else "",
    }
    write_json(receipt_path, payload)
    return payload


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sync read-only Amaranth approval snapshots into infra-control.")
    parser.add_argument("--root", default=str(DEFAULT_ROOT))
    parser.add_argument("--db", default="")
    parser.add_argument("--receipt", default="")
    parser.add_argument("--upstream-snapshot", default=str(DEFAULT_UPSTREAM_SNAPSHOT))
    parser.add_argument("--upstream-details", default="")
    parser.add_argument("--skip-upstream", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    payload = sync_amaranth_approvals(
        root=Path(args.root),
        db_path=Path(args.db) if args.db else None,
        upstream_command=None if args.skip_upstream else DEFAULT_UPSTREAM_COMMAND,
        upstream_snapshot=Path(args.upstream_snapshot),
        upstream_details=Path(args.upstream_details) if args.upstream_details else None,
        receipt_path=Path(args.receipt) if args.receipt else None,
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
