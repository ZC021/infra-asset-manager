#!/usr/bin/env bash
set -euo pipefail

ROOT="${INFRA_CONTROL_ROOT:-/opt/infra-asset-manager}"
OPS_DIR="${INFRA_CONTROL_SNIPE_OPS_DIR:-${ROOT}/var/imports/snipe-ops}"
RECEIPT="${ROOT}/var/receipts/daily-snipe-ops-refresh.json"
LOG="${ROOT}/var/receipts/daily-snipe-ops-refresh.log"
IMPORT_FILE="${ROOT}/var/receipts/daily-snipe-ops-refresh-import.json"
HEALTH_FILE="${ROOT}/var/receipts/daily-snipe-ops-refresh-health.json"
JIRA_SYNC="${INFRA_CONTROL_JIRA_SYNC:-1}"
JIRA_ENV_FILE="${INFRA_CONTROL_JIRA_ENV_FILE:-/etc/internal-jira-mcp/env}"
JIRA_RECEIPT="${ROOT}/var/receipts/jira-api-sync.json"

now_kst() {
  TZ=Asia/Seoul date +%FT%T%z
}

STARTED_AT="$(now_kst)"

mkdir -p "$(dirname "$RECEIPT")" "$(dirname "$LOG")"

write_receipt() {
  local passed="$1"
  local message="$2"
  python3 - "$RECEIPT" "$passed" "$message" "$STARTED_AT" "$(now_kst)" "$OPS_DIR" "$LOG" "$IMPORT_FILE" "$HEALTH_FILE" "$JIRA_RECEIPT" <<'PY'
import json
import sys
from pathlib import Path

receipt = Path(sys.argv[1])
import_path = Path(sys.argv[8])
health_path = Path(sys.argv[9])
jira_path = Path(sys.argv[10])

def load_json(path):
    if not path.exists() or not path.read_text(encoding="utf-8").strip():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))

payload = {
    "passed": sys.argv[2] == "true",
    "message": sys.argv[3],
    "timezone": "Asia/Seoul",
    "started_at": sys.argv[4],
    "finished_at": sys.argv[5],
    "ops_dir": sys.argv[6],
    "log_file": sys.argv[7],
    "import_result": load_json(import_path),
    "health": load_json(health_path),
    "jira_api": load_json(jira_path),
}
receipt.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY
}

{
  printf '\n[%s] START daily snipe ops refresh\n' "$STARTED_AT"
  printf 'root=%s ops_dir=%s\n' "$ROOT" "$OPS_DIR"
} >> "$LOG"

if [[ ! -r "${OPS_DIR}/recon_stage.csv" ]]; then
  write_receipt false "recon_stage.csv is not readable" "{}" "{}"
  exit 1
fi

set +e
cd "$ROOT" && INFRA_CONTROL_SNIPE_OPS_DIR="$OPS_DIR" PYTHONPATH="$ROOT" python3 - <<'PY' > "$IMPORT_FILE"
import json
import os
from pathlib import Path
from infra_control.connectors.snipe_ops import import_assets

print(json.dumps(import_assets(Path(os.environ["INFRA_CONTROL_SNIPE_OPS_DIR"])), ensure_ascii=False))
PY
IMPORT_RC=$?
set -e
printf '[%s] import exit=%s %s\n' "$(now_kst)" "$IMPORT_RC" "$(tr '\n' ' ' < "$IMPORT_FILE")" >> "$LOG"
if [[ "$IMPORT_RC" != "0" ]]; then
  : > "$HEALTH_FILE"
  write_receipt false "snipe ops import failed"
  exit "$IMPORT_RC"
fi

if [[ "$JIRA_SYNC" != "0" ]]; then
  set +e
  cd "$ROOT" && PYTHONPATH="$ROOT" python3 scripts/sync_jira_api.py --optional --env-file "$JIRA_ENV_FILE" --receipt "$JIRA_RECEIPT" >> "$LOG" 2>&1
  JIRA_RC=$?
  set -e
  printf '[%s] jira api sync exit=%s %s\n' "$(now_kst)" "$JIRA_RC" "$(tr '\n' ' ' < "$JIRA_RECEIPT" 2>/dev/null || true)" >> "$LOG"
  if [[ "$JIRA_RC" != "0" ]]; then
    write_receipt false "jira api sync failed"
    exit "$JIRA_RC"
  fi
else
  printf '[%s] jira api sync disabled by INFRA_CONTROL_JIRA_SYNC=0\n' "$(now_kst)" >> "$LOG"
fi

set +e
cd "$ROOT" && python3 scripts/health_check.py http://127.0.0.1:8000 > "$HEALTH_FILE"
HEALTH_RC=$?
set -e
printf '[%s] health exit=%s %s\n' "$(now_kst)" "$HEALTH_RC" "$(tr '\n' ' ' < "$HEALTH_FILE")" >> "$LOG"
if [[ "$HEALTH_RC" != "0" ]]; then
  write_receipt false "health check failed"
  exit "$HEALTH_RC"
fi

write_receipt true "daily snipe ops refresh completed"
printf '[%s] END daily snipe ops refresh\n' "$(now_kst)" >> "$LOG"
