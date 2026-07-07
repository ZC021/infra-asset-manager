#!/usr/bin/env bash
set -euo pipefail

ROOT="${INFRA_CONTROL_ROOT:-/opt/infra-asset-manager}"
SERVICE_USER="${INFRA_CONTROL_SERVICE_USER:-infraasset}"
SERVICE_GROUP="${INFRA_CONTROL_SERVICE_GROUP:-$SERVICE_USER}"
SYNC_DIR="${SNIPEIT_SYNC_DIR:-/root/snipe-it/sync}"
OPS_SRC="${SNIPEIT_OPS_SRC:-/root/snipe-it/frontend-next/var/ops}"
OPS_DST="${INFRA_CONTROL_SNIPE_OPS_DIR:-${ROOT}/var/imports/snipe-ops}"
LOG_DIR="${ROOT}/var/log"
RUN_DIR="${ROOT}/var/run"
RECEIPT_DIR="${ROOT}/var/receipts"

now_kst() {
  TZ=Asia/Seoul date +%FT%T%z
}

run_id_kst() {
  TZ=Asia/Seoul date +%Y%m%dT%H%M%S%z
}

RUN_ID="$(run_id_kst)"
LOG_FILE="${LOG_DIR}/hourly-intune-nac-refresh.log"
STEPS_FILE="${RUN_DIR}/hourly-intune-nac-refresh-${RUN_ID}.jsonl"
RECEIPT_FILE="${RECEIPT_DIR}/hourly-intune-nac-refresh.json"
SKIP_UPSTREAM="${INFRA_CONTROL_SKIP_UPSTREAM:-0}"
JIRA_SYNC="${INFRA_CONTROL_JIRA_SYNC:-1}"
JIRA_ENV_FILE="${INFRA_CONTROL_JIRA_ENV_FILE:-/etc/infra-asset-manager/jira.env}"
JIRA_RECEIPT_FILE="${RECEIPT_DIR}/jira-api-sync.json"
AMARANTH_SYNC="${INFRA_CONTROL_AMARANTH_SYNC:-1}"
AMARANTH_RECEIPT_FILE="${RECEIPT_DIR}/amaranth-approval-sync.json"

case "${1:-}" in
  -h|--help)
    cat <<'EOF'
Usage: hourly_intune_nac_refresh.sh [--skip-upstream]

Refresh Intune/NAC-derived Snipe-IT ops data and import it into infra-control.

Options:
  --skip-upstream  Skip the Snipe-IT/Intune/NAC upstream pipeline and only copy/import the current ops snapshot.
EOF
    exit 0
    ;;
  --skip-upstream)
    SKIP_UPSTREAM=1
    ;;
  "")
    ;;
  *)
    echo "unknown argument: $1" >&2
    exit 2
    ;;
esac

mkdir -p "$LOG_DIR" "$RUN_DIR" "$RECEIPT_DIR" "$OPS_DST"
exec 9>"${RUN_DIR}/hourly-intune-nac-refresh.lock"
if ! flock -n 9; then
  printf '%s hourly Intune/NAC refresh already running\n' "$(now_kst)" >> "$LOG_FILE"
  exit 0
fi

STARTED_AT="$(now_kst)"
: > "$STEPS_FILE"
touch "$LOG_FILE"

append_step() {
  local name="$1"
  local started_at="$2"
  local finished_at="$3"
  local exit_code="$4"
  /usr/bin/python3 - "$STEPS_FILE" "$name" "$started_at" "$finished_at" "$exit_code" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
entry = {
    "name": sys.argv[2],
    "started_at": sys.argv[3],
    "finished_at": sys.argv[4],
    "exit_code": int(sys.argv[5]),
}
with path.open("a", encoding="utf-8") as handle:
    handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
PY
}

run_step() {
  local name="$1"
  shift
  local started_at
  local finished_at
  local rc
  started_at="$(now_kst)"
  {
    printf '\n[%s] START %s\n' "$started_at" "$name"
    printf 'command:'
    printf ' %q' "$@"
    printf '\n'
  } >> "$LOG_FILE"
  set +e
  "$@" >> "$LOG_FILE" 2>&1
  rc=$?
  set -e
  finished_at="$(now_kst)"
  printf '[%s] END %s exit=%s\n' "$finished_at" "$name" "$rc" >> "$LOG_FILE"
  append_step "$name" "$started_at" "$finished_at" "$rc"
  return "$rc"
}

write_receipt() {
  local verdict="$1"
  local message="$2"
  local finished_at
  finished_at="$(now_kst)"
  /usr/bin/python3 - "$RECEIPT_FILE" "$STEPS_FILE" "$RUN_ID" "$STARTED_AT" "$finished_at" "$verdict" "$message" "$OPS_SRC" "$OPS_DST" "$LOG_FILE" <<'PY'
import json
import sys
from pathlib import Path

receipt = Path(sys.argv[1])
steps_path = Path(sys.argv[2])
steps = []
if steps_path.exists():
    with steps_path.open(encoding="utf-8") as handle:
        steps = [json.loads(line) for line in handle if line.strip()]
payload = {
    "passed": sys.argv[6] == "ACCEPT",
    "verdict": sys.argv[6],
    "message": sys.argv[7],
    "run_id": sys.argv[3],
    "timezone": "Asia/Seoul",
    "started_at": sys.argv[4],
    "finished_at": sys.argv[5],
    "ops_source": sys.argv[8],
    "ops_dest": sys.argv[9],
    "log_file": sys.argv[10],
    "steps": steps,
}
receipt.parent.mkdir(parents=True, exist_ok=True)
receipt.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY
}

fail() {
  write_receipt "REJECT" "$1"
  exit 1
}

if [[ "$SKIP_UPSTREAM" != "1" ]]; then
  run_step "nac_genian_asset_sync" env \
    SNIPEIT_ASSET_SOURCE=genian \
    SNIPEIT_GENIAN_MODE=api \
    SNIPEIT_EXTERNAL_SOURCE_READ_ONLY=1 \
    SNIPEIT_DELETE_NON_OWNED_BY_LEDGER=0 \
    "${SYNC_DIR}/sync.sh" --assets || fail "NAC Genian asset sync failed"

  run_step "intune_nac_recon_orchestrator" "${SYNC_DIR}/run_recon_orchestrator.sh" || fail "Intune/NAC recon orchestrator failed"
else
  printf '%s skipping upstream Snipe-IT/Intune/NAC pipeline by INFRA_CONTROL_SKIP_UPSTREAM=1\n' "$(now_kst)" >> "$LOG_FILE"
fi

run_step "copy_ops_snapshot" bash -lc "find '$OPS_SRC' -maxdepth 1 -type f \\( -name '*.json' -o -name '*.csv' \\) -exec cp -p {} '$OPS_DST'/ \\; && chown -R '$SERVICE_USER:$SERVICE_GROUP' '$OPS_DST'" || fail "Copying Snipe-IT ops snapshot failed"

run_step "import_snipe_ops_snapshot" runuser -u "$SERVICE_USER" -- bash -lc "cd '$ROOT' && INFRA_CONTROL_SNIPE_OPS_DIR='$OPS_DST' PYTHONPATH='$ROOT' python3 - <<'PY'
import json
from pathlib import Path
from infra_control.connectors.snipe_ops import import_assets

result = import_assets(Path('$OPS_DST'))
receipt = Path('var/receipts/snipe-ops-hourly-import.json')
receipt.write_text(json.dumps({'passed': True, 'result': result}, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
print(json.dumps({'passed': True, 'result': result}, ensure_ascii=False, indent=2))
PY" || fail "Importing Snipe-IT ops snapshot into infra-control failed"

if [[ "$JIRA_SYNC" != "0" ]]; then
  run_step "jira_api_sync" bash -lc "cd '$ROOT' && PYTHONPATH='$ROOT' python3 scripts/sync_jira_api.py --optional --env-file '$JIRA_ENV_FILE' --receipt '$JIRA_RECEIPT_FILE'" || fail "Jira API sync into infra-control failed"
else
  printf '%s skipping Jira API sync by INFRA_CONTROL_JIRA_SYNC=0\n' "$(now_kst)" >> "$LOG_FILE"
fi

if [[ "$AMARANTH_SYNC" != "0" ]]; then
  run_step "amaranth_approval_sync" bash -lc "cd '$ROOT' && PYTHONPATH='$ROOT' python3 scripts/sync_amaranth_approvals.py --receipt '$AMARANTH_RECEIPT_FILE'" || fail "Amaranth approval sync into infra-control failed"
else
  printf '%s skipping Amaranth approval sync by INFRA_CONTROL_AMARANTH_SYNC=0\n' "$(now_kst)" >> "$LOG_FILE"
fi

run_step "health_check" runuser -u "$SERVICE_USER" -- bash -lc "cd '$ROOT' && PYTHONPATH='$ROOT' python3 scripts/health_check.py" || fail "infra-control health check failed"

write_receipt "ACCEPT" "hourly Intune/NAC/Amaranth refresh completed"
