from __future__ import annotations

import contextlib
import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
VAR_DIR = ROOT / "var"
DB_PATH = VAR_DIR / "infra-control.sqlite3"


SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS integration_sources (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  kind TEXT NOT NULL,
  endpoint TEXT,
  status TEXT NOT NULL DEFAULT 'unknown',
  last_sync_at TEXT,
  metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS assets (
  id TEXT PRIMARY KEY,
  source TEXT NOT NULL,
  external_id TEXT,
  asset_tag TEXT,
  hostname TEXT,
  serial TEXT,
  model TEXT,
  manufacturer TEXT,
  category TEXT,
  status TEXT,
  owner TEXT,
  owner_email TEXT,
  department TEXT,
  primary_ip TEXT,
  os TEXT,
  location TEXT,
  room TEXT,
  rack TEXT,
  rack_unit TEXT,
  maintenance_date TEXT,
  usage_status TEXT,
  usage_reason TEXT,
  purpose TEXT,
  ports TEXT,
  is_server INTEGER NOT NULL DEFAULT 0,
  is_virtual INTEGER NOT NULL DEFAULT 0,
  is_network INTEGER NOT NULL DEFAULT 0,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_assets_asset_tag ON assets(asset_tag);
CREATE INDEX IF NOT EXISTS idx_assets_serial ON assets(serial);
CREATE INDEX IF NOT EXISTS idx_assets_owner ON assets(owner);
CREATE INDEX IF NOT EXISTS idx_assets_server ON assets(is_server);
CREATE INDEX IF NOT EXISTS idx_assets_ip ON assets(primary_ip);

CREATE TABLE IF NOT EXISTS change_history (
  id TEXT PRIMARY KEY,
  asset_tag TEXT,
  changed_at TEXT,
  actor TEXT,
  action TEXT,
  field TEXT,
  old_value TEXT,
  new_value TEXT,
  reason TEXT,
  source TEXT,
  metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_change_history_asset ON change_history(asset_tag);
CREATE INDEX IF NOT EXISTS idx_change_history_time ON change_history(changed_at);

CREATE TABLE IF NOT EXISTS locations (
  id TEXT PRIMARY KEY,
  room TEXT NOT NULL,
  rack TEXT,
  rack_unit TEXT,
  asset_tag TEXT,
  label TEXT,
  source TEXT,
  metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_locations_room ON locations(room);
CREATE INDEX IF NOT EXISTS idx_locations_asset ON locations(asset_tag);

CREATE TABLE IF NOT EXISTS maintenance_events (
  id TEXT PRIMARY KEY,
  asset_tag TEXT NOT NULL,
  event_date TEXT,
  vendor TEXT,
  event_type TEXT,
  status TEXT,
  note TEXT,
  source TEXT,
  metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_maintenance_asset ON maintenance_events(asset_tag);

CREATE TABLE IF NOT EXISTS software_inventory_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_id TEXT NOT NULL,
  imported_at TEXT NOT NULL,
  source_collected_at TEXT,
  software_rows INTEGER NOT NULL DEFAULT 0,
  install_rows INTEGER NOT NULL DEFAULT 0,
  detail_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_software_inventory_runs_time ON software_inventory_runs(imported_at);

CREATE TABLE IF NOT EXISTS software_inventory_summary (
  sw_id TEXT PRIMARY KEY,
  software_name TEXT,
  install_count REAL,
  legal_install_count REAL,
  illegal_install_count REAL,
  temp_install_count REAL,
  license_amount REAL,
  residue_amount REAL,
  assign_amount REAL,
  assign_uninstall_amount REAL,
  collected_at TEXT,
  imported_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_software_inventory_summary_name ON software_inventory_summary(software_name);
CREATE INDEX IF NOT EXISTS idx_software_inventory_summary_illegal ON software_inventory_summary(illegal_install_count);

CREATE TABLE IF NOT EXISTS software_inventory_installs (
  install_id TEXT PRIMARY KEY,
  sw_id TEXT,
  equip_id TEXT,
  software_name TEXT,
  equip_code TEXT,
  equip_name TEXT,
  asset_no TEXT,
  user_key TEXT,
  user_name TEXT,
  dept_name TEXT,
  is_licensed INTEGER,
  install_date TEXT,
  report_date TEXT,
  collected_at TEXT,
  imported_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_software_inventory_installs_sw ON software_inventory_installs(sw_id);
CREATE INDEX IF NOT EXISTS idx_software_inventory_installs_user ON software_inventory_installs(user_key);
CREATE INDEX IF NOT EXISTS idx_software_inventory_installs_asset ON software_inventory_installs(asset_no);

CREATE TABLE IF NOT EXISTS software_ledger (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  product TEXT NOT NULL,
  norm_name TEXT,
  vendor TEXT,
  source_sheet TEXT,
  license_type TEXT,
  qty TEXT,
  has_key INTEGER NOT NULL DEFAULT 0,
  key_masked TEXT,
  identifier_type TEXT,
  activated_at TEXT,
  expiry TEXT,
  amount_usd TEXT,
  note TEXT,
  collected_at TEXT,
  imported_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_software_ledger_norm ON software_ledger(norm_name);
CREATE INDEX IF NOT EXISTS idx_software_ledger_type ON software_ledger(license_type);

CREATE TABLE IF NOT EXISTS software_ledger_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_id TEXT NOT NULL,
  imported_at TEXT NOT NULL,
  ledger_file TEXT,
  product_rows INTEGER NOT NULL DEFAULT 0,
  detail_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_software_ledger_runs_time ON software_ledger_runs(imported_at);
"""


ASSET_MIGRATIONS = {
    "usage_status": "ALTER TABLE assets ADD COLUMN usage_status TEXT",
    "usage_reason": "ALTER TABLE assets ADD COLUMN usage_reason TEXT",
    "purpose": "ALTER TABLE assets ADD COLUMN purpose TEXT",
    "ports": "ALTER TABLE assets ADD COLUMN ports TEXT",
    "is_virtual": "ALTER TABLE assets ADD COLUMN is_virtual INTEGER NOT NULL DEFAULT 0",
    "is_network": "ALTER TABLE assets ADD COLUMN is_network INTEGER NOT NULL DEFAULT 0",
}


POST_MIGRATION_SQL = """
CREATE INDEX IF NOT EXISTS idx_assets_usage ON assets(usage_status);
CREATE INDEX IF NOT EXISTS idx_assets_network ON assets(is_network);
CREATE INDEX IF NOT EXISTS idx_assets_virtual ON assets(is_virtual);
CREATE INDEX IF NOT EXISTS idx_software_inventory_installs_licensed ON software_inventory_installs(is_licensed);
"""


def _open(path: Path = DB_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


@contextlib.contextmanager
def connect(path: Path = DB_PATH):
    """Yield a connection; commit on success, rollback on error, always close."""
    conn = _open(path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(path: Path = DB_PATH) -> Path:
    with connect(path) as conn:
        conn.executescript(SCHEMA)
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(assets)").fetchall()}
        for column, sql in ASSET_MIGRATIONS.items():
            if column not in existing:
                conn.execute(sql)
        conn.executescript(POST_MIGRATION_SQL)
    return path


def encode_json(value: Any) -> str:
    return json.dumps(value or {}, ensure_ascii=False, sort_keys=True)


def rows_to_dicts(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]
