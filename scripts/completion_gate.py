#!/usr/bin/env python3
"""Receipt-based completion gate for infra-control automation.

Runs as `python3 -m scripts.completion_gate` from the infra-control repo
root. Reads a curated set of receipt JSON files (or the ones named on the
command line) and returns ACCEPT only when every receipt is present,
``passed`` is ``True`` (or equivalent), and the timestamp is within the
declared freshness budget.

Designed for harness v5.4: every cron-driven sync surface (Amaranth,
hourly Intune/NAC, daily Snipe ops, Jira API) writes its own receipt and
this gate is what consolidates them before a release/convergence claim.

Output: writes a single completion-gate receipt JSON. Exits 0 on ACCEPT,
non-zero otherwise.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable


KST = timezone(timedelta(hours=9), "KST")
DEFAULT_ROOT = Path(os.environ.get("INFRA_CONTROL_ROOT", "/opt/infra-asset-manager"))


# Default receipt registry. Each entry: (id, relative receipt path, max age).
DEFAULT_REGISTRY: tuple[tuple[str, str, int | None], ...] = (
    ("amaranth-approval-sync", "var/receipts/amaranth-approval-sync.json", 60),
    ("hourly-intune-nac-refresh", "var/receipts/hourly-intune-nac-refresh.json", 180),
    ("jira-api-sync", "var/receipts/jira-api-sync.json", 180),
    ("daily-snipe-ops-refresh", "var/receipts/daily-snipe-ops-refresh.json", 36 * 60),
    ("snipe-ops-hourly-import", "var/receipts/snipe-ops-hourly-import.json", 180),
)


@dataclass
class ReceiptVerdict:
    id: str
    path: str
    status: str
    detail: dict[str, Any] = field(default_factory=dict)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def to_aware(value: str) -> datetime | None:
    if not value:
        return None
    text = value.replace("Z", "+00:00")
    # Normalize +HHMM / -HHMM to +HH:MM (Python 3.10 fromisoformat strictness)
    if len(text) >= 5 and (text[-5] in "+-") and text[-4:].isdigit():
        text = text[:-2] + ":" + text[-2:]
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def receipt_passed(payload: dict[str, Any]) -> bool:
    # Two receipt shapes in the wild:
    #   * {"passed": true, ...}                  (sync_amaranth, daily, hourly)
    #   * {"verdict": "ACCEPT", "passed": true}  (hourly script-style)
    # Treat both as the truth source. "status": "ACCEPT" is also honored.
    if isinstance(payload.get("passed"), bool):
        return bool(payload["passed"])
    verdict = str(payload.get("verdict") or payload.get("status") or "").upper()
    if verdict in {"ACCEPT", "PASS", "OK"}:
        return True
    if verdict in {"REJECT", "FAIL", "ERROR"}:
        return False
    return False


def receipt_timestamp(payload: dict[str, Any]) -> str:
    for key in ("finished_at", "last_sync_at", "generated_at", "synced_at"):
        value = payload.get(key)
        if value:
            return str(value)
    return ""


def evaluate(receipt_id: str, path: Path, max_age_minutes: int | None) -> ReceiptVerdict:
    detail: dict[str, Any] = {"path": str(path)}
    if not path.exists():
        return ReceiptVerdict(receipt_id, str(path), "missing", detail)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        detail["error"] = f"invalid JSON: {exc}"
        return ReceiptVerdict(receipt_id, str(path), "invalid", detail)
    if not isinstance(payload, dict):
        detail["error"] = "receipt is not a JSON object"
        return ReceiptVerdict(receipt_id, str(path), "invalid", detail)

    passed = receipt_passed(payload)
    ts = receipt_timestamp(payload)
    detail["passed"] = passed
    detail["timestamp"] = ts
    if not passed:
        detail["message"] = str(payload.get("message") or payload.get("verdict") or "not passed")
        return ReceiptVerdict(receipt_id, str(path), "rejected", detail)

    if max_age_minutes is not None and ts:
        ts_dt = to_aware(ts)
        if ts_dt is None:
            detail["error"] = f"unparseable timestamp: {ts}"
            return ReceiptVerdict(receipt_id, str(path), "invalid", detail)
        age = utc_now() - ts_dt
        detail["age_seconds"] = int(age.total_seconds())
        if age > timedelta(minutes=max_age_minutes):
            detail["error"] = (
                f"stale: {int(age.total_seconds() / 60)} min > budget {max_age_minutes} min"
            )
            return ReceiptVerdict(receipt_id, str(path), "stale", detail)

    return ReceiptVerdict(receipt_id, str(path), "passed", detail)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=DEFAULT_ROOT,
        help="infra-control root (default: $INFRA_CONTROL_ROOT or /opt/infra-asset-manager)",
    )
    parser.add_argument(
        "--receipt",
        type=Path,
        default=None,
        help="output completion-gate receipt path (default: <root>/var/receipts/completion-gate.json)",
    )
    parser.add_argument(
        "--scope",
        default="infra-control automation",
        help="free-form description of what this gate is validating",
    )
    parser.add_argument(
        "--require",
        action="append",
        default=[],
        metavar="id=path[:max_age_minutes]",
        help="add an additional required receipt (repeatable)",
    )
    parser.add_argument(
        "--skip",
        action="append",
        default=[],
        metavar="id",
        help="skip a default receipt (repeatable)",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="suppress per-receipt stdout; still writes the receipt JSON",
    )
    return parser.parse_args()


def build_registry(args: argparse.Namespace) -> list[tuple[str, Path, int | None]]:
    skip = set(args.skip)
    entries: list[tuple[str, Path, int | None]] = []
    for receipt_id, rel_path, max_age in DEFAULT_REGISTRY:
        if receipt_id in skip:
            continue
        entries.append((receipt_id, args.root / rel_path, max_age))
    for raw in args.require:
        if "=" not in raw:
            raise SystemExit(f"invalid --require entry, expected id=path[:age]: {raw}")
        receipt_id, _, rest = raw.partition("=")
        path_part, _, age_part = rest.partition(":")
        max_age = int(age_part) if age_part else None
        entries.append((receipt_id.strip(), Path(path_part).resolve(), max_age))
    return entries


def main() -> int:
    args = parse_args()
    entries = build_registry(args)
    verdicts = [evaluate(rid, path, max_age) for rid, path, max_age in entries]

    accepted = [v for v in verdicts if v.status == "passed"]
    rejected = [v for v in verdicts if v.status != "passed"]
    status = "ACCEPT" if not rejected else "REJECT"

    payload = {
        "status": status,
        "scope": args.scope,
        "generated_at": utc_now().isoformat(),
        "generated_at_kst": datetime.now(KST).isoformat(),
        "root": str(args.root),
        "receipts": [
            {
                "id": v.id,
                "path": v.path,
                "status": v.status,
                **v.detail,
            }
            for v in verdicts
        ],
        "summary": {
            "total": len(verdicts),
            "accepted": len(accepted),
            "rejected_or_missing": len(rejected),
        },
    }

    receipt_path = args.receipt or (args.root / "var" / "receipts" / "completion-gate.json")
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    receipt_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    if not args.quiet:
        for v in verdicts:
            print(f"  [{v.status:>8}] {v.id:30s} {v.detail.get('message') or v.detail.get('error') or ''}")
        print(f"completion-gate: {status} ({len(accepted)}/{len(verdicts)} passed)")
        print(f"receipt: {receipt_path}")

    return 0 if status == "ACCEPT" else 1


if __name__ == "__main__":
    raise SystemExit(main())
