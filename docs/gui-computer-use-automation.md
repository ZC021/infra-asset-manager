# GUI Computer Use Automation

`infra-control` accepts GUI Computer Use receipts from a separate user-PC
validator. This lets another project own the GUI automation while Ralph enforces
the result in this project.

## Required User Path

The GUI validator must run from the user's desktop browser, not from headless or
CLI browser automation.

Required URL:

`http://asset-hub.example.com:8130`

Required actions:

1. Open the operator UI.
2. Read `/api/health` and keep `ui_build_id` for the receipt.
3. Click every mode: `servers`, `assets`, `in_use`, `unused`, `needs_review`,
   `owners`, `locations`, `maintenance`, `changes`, `sources`.
4. Search `ACME-A02-250379`.
5. Open the asset detail.
6. Confirm the detail contains `통합 인프라 사용 근거`.
7. Download the CSV export and confirm the header includes `usage_status`.

## Receipt Contract

The GUI project must send a JSON object like this:

```json
{
  "passed": true,
  "mode": "gui-computer-use",
  "plugin": "Computer Use",
  "environment": "gui",
  "base": "http://asset-hub.example.com:8130",
  "checked_at": "2026-04-17T17:10:00+09:00",
  "simulated": false,
  "operator_device": "acme-a02-250379",
  "observed": {
    "title": "infra-control",
    "ui_build_id": "current value from /api/health",
    "clicked_modes": [
      "servers",
      "assets",
      "in_use",
      "unused",
      "needs_review",
      "owners",
      "locations",
      "maintenance",
      "changes",
      "sources"
    ],
    "selected_asset": "ACME-A02-250379",
    "detail_contains": ["통합 인프라 사용 근거"],
    "csv_header": "asset_tag,usage_status,usage_reason,...",
    "screenshot": "computer-use-gui.png"
  }
}
```

Receipts with `playwright`, `headless`, `local-playwright`, or `cli` in the
mode/plugin/app fields are rejected.

The `ui_build_id` must match the current `/api/health` value. This prevents an
old GUI pass from validating a newly changed UI.

## Automatic Upload

Recommended:

```bash
curl -X POST \
  -H 'content-type: application/json' \
  --data-binary @computer-use-user-test.json \
  http://asset-hub.example.com:8130/api/receipts/computer-use
```

If `INFRA_CONTROL_RECEIPT_TOKEN` is set on the service, include:

```bash
-H "x-receipt-token: $INFRA_CONTROL_RECEIPT_TOKEN"
```

Fallback file import on the server:

```bash
python3 scripts/import_computer_use_receipt.py computer-use-user-test.json
```

## Gate

Ralph must be called with the user-facing base:

```bash
python3 scripts/ralph_quality_gate.py \
  --base http://127.0.0.1:8130 \
  --computer-use-base http://asset-hub.example.com:8130 \
  --require-computer-use
```
