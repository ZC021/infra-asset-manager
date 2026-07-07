# Infra Asset Manager

Infra Asset Manager is a lightweight internal IT inventory hub for tracking
hardware assets, software licenses, SaaS subscriptions, audit findings, and
reconciliation status across several read-only source systems.

This public export contains code only. Company data, local databases, CSV/Excel
imports, deployment receipts, caches, backups, and environment files are not
included.

## What It Does

- Reconciles asset inventory from local spreadsheets and upstream ops snapshots.
- Tracks usage status, owners, missing assets, duplicate records, and audit views.
- Imports software license and SaaS ledger data for compliance checks.
- Serves a static browser UI plus JSON API endpoints for search, detail views,
  source health, approval context, and audit summaries.

## Architecture

- `server.py`: stdlib `ThreadingHTTPServer` application and JSON API router.
- `static/`: single-page browser UI.
- `infra_control/db.py`: SQLite schema, migrations, and connection helpers.
- `infra_control/connectors/`: importers for asset spreadsheets, ops snapshots,
  and software license ledgers.
- `scripts/`: operational import, sync, health-check, and quality-gate helpers.
- `tests/`: unit tests using synthetic ACME/example fixture data.

## Stack

- Python 3.11+
- SQLite
- HTML/CSS/JavaScript frontend
- Optional import dependencies such as `openpyxl` for Excel ingestion

## Setup

```bash
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install openpyxl pytest
INFRA_CONTROL_ALLOW_NO_AUTH=1 python3 server.py --host 127.0.0.1 --port 8000
```

For authenticated local use, set Basic auth credentials via environment
variables or a private `config/security.env` file. Do not commit that file.

Import data into `var/` using the scripts under `scripts/`; keep all generated
DB, CSV, Excel, receipt, and cache files out of git.

## Screenshots

Screenshots are intentionally omitted from this sanitized export. Add public,
synthetic screenshots here after loading non-company sample data.

## Sanitization / 공개 범위

사내 프로젝트를 공개용으로 정리(비식별화)한 저장소입니다. 회사명·내부 데이터·운영 환경 정보(호스트명·내부 주소·계정)는 포함하지 않으며, 예시 데이터는 전부 합성(synthetic)입니다. 세부 구현과 운영 경험은 면접에서 상세히 설명할 수 있습니다.

This is a sanitized public export of an internal project. It contains no company names, internal data, or production environment details (hostnames, internal addresses, accounts); all sample data is synthetic.
