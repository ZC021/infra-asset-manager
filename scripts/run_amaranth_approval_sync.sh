#!/usr/bin/env bash
set -euo pipefail

ROOT="${INFRA_CONTROL_ROOT:-/opt/infra-asset-manager}"
CONFIG_DIR="${AMARANTH_APPROVAL_CONFIG_DIR:-$ROOT/config}"
OUTPUT_DIR="${AMARANTH_APPROVAL_OUTPUT_DIR:-$ROOT/var/approval}"
RECEIPT="${INFRA_CONTROL_AMARANTH_RECEIPT:-$ROOT/var/receipts/amaranth-approval-sync.json}"
COLLECTOR="${AMARANTH_APPROVAL_COLLECTOR:-$ROOT/connectors/amaranth_approval_sync.py}"
SYNC_SCRIPT="${INFRA_CONTROL_AMARANTH_SYNC_SCRIPT:-$ROOT/scripts/sync_amaranth_approvals.py}"

export INFRA_CONTROL_ROOT="$ROOT"
export AMARANTH_APPROVAL_CONFIG_DIR="$CONFIG_DIR"
export AMARANTH_APPROVAL_OUTPUT_DIR="$OUTPUT_DIR"

mkdir -p "$OUTPUT_DIR" "$ROOT/var/receipts" "$ROOT/var/log"

python3 "$COLLECTOR"

cd "$ROOT"
PYTHONPATH="$ROOT" python3 "$SYNC_SCRIPT" \
  --skip-upstream \
  --upstream-snapshot "$OUTPUT_DIR/approval_tasks.json" \
  --upstream-details "$OUTPUT_DIR/approval_details.json" \
  --receipt "$RECEIPT"
