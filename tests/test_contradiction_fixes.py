"""Regression tests for admin asset-management contradiction fixes (2026-06-22).

Each test is written to FAIL against the pre-fix code and PASS after the fix, so
it pins the corrected behavior rather than merely asserting "nothing crashed".

Covered findings:
  - SUM-2 / state-4 / INGEST-04 : jira-missing snapshot must be reconciled against
    the live assets table so an asset is never counted as both "in DB" and
    "Jira-missing", and in_use is never double-counted.
  - search-02 : the 담당자(owner) facet count (active-only) must equal the owner
    drill-down list (which previously had no active gate).
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from infra_control import db
from infra_control.connectors import excel_assets
import server


LEDGER_HEADER = "자산관리번호,담당자,호스트명\n"


def _run_excel_import(db_path: Path, import_dir: Path) -> None:
    with patch.object(excel_assets, "init_db", lambda: db.init_db(db_path)), patch.object(
        excel_assets, "connect", lambda: db.connect(db_path)
    ):
        excel_assets.import_excel_assets(import_dir=import_dir, extra_files=[])


class JiraMissingReconciliationTest(unittest.TestCase):
    def test_jira_missing_excludes_assets_already_in_db(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "infra-control.sqlite3"
            db.init_db(db_path)
            with db.connect(db_path) as conn:
                # An asset that is already a real row in the DB and in use.
                conn.execute(
                    """
                    INSERT INTO assets
                    (id, source, asset_tag, usage_status, usage_reason, metadata_json, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    ("a-in-db", "snipe-ops", "ACME-A02-250477", "사용중", "NAC 사용 근거", "{}", "2026-05-12T00:00:00+00:00"),
                )
                # Jira snapshot still lists 250477 (stale) plus a genuinely-missing 250999.
                conn.execute(
                    """
                    INSERT INTO integration_sources
                    (id, name, kind, endpoint, status, last_sync_at, metadata_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "jira-api",
                        "Jira Hardware Asset API",
                        "asset-status-reference",
                        "Jira REST API board 118",
                        "read-only",
                        "2026-05-14T04:21:14+00:00",
                        json.dumps(
                            {
                                "missing_records": [
                                    {
                                        "asset_tag": "ACME-A02-250477",
                                        "issue": "ITAM-1",
                                        "lane": "inprogress",
                                        "lane_label": "In Progress(사용중)",
                                    },
                                    {
                                        "asset_tag": "ACME-A02-250999",
                                        "issue": "ITAM-2",
                                        "lane": "inprogress",
                                        "lane_label": "In Progress(사용중)",
                                    },
                                ]
                            },
                            ensure_ascii=False,
                        ),
                    ),
                )

            with patch.object(server, "connect", lambda: db.connect(db_path)):
                missing_tags = {row["asset_tag"] for row in server.jira_missing_assets({})}
                overlay_tags = {row["asset_tag"] for row in server.jira_in_use_overlay_assets()}
                result = server.summary()

        # The stale tag that now lives in the DB must not be reported as missing.
        self.assertNotIn("ACME-A02-250477", missing_tags)
        # The genuinely-absent tag is still surfaced.
        self.assertIn("ACME-A02-250999", missing_tags)
        # The in-use overlay never re-counts a tag that exists in the DB.
        self.assertNotIn("ACME-A02-250477", overlay_tags)
        self.assertIn("ACME-A02-250999", overlay_tags)
        # No double count: 1 DB in-use + 1 genuinely-missing in-use overlay.
        self.assertEqual(result["jira_only_in_use"], 1)
        self.assertEqual(result["in_use"], 2)


class OwnerFacetDrilldownParityTest(unittest.TestCase):
    def test_owner_count_matches_drilldown_rows(self) -> None:
        directory = {
            "owner@example.com": {"displayName": "Example User", "mail": "owner@example.com"},
            "owner": {"displayName": "Example User", "mail": "owner@example.com"},
        }
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "infra-control.sqlite3"
            db.init_db(db_path)
            with db.connect(db_path) as conn:
                # Active owner (resolves via directory email key) -> facet counts it.
                conn.execute(
                    """
                    INSERT INTO assets
                    (id, source, asset_tag, owner, owner_email, usage_status, metadata_json, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    ("a-active", "snipe-ops", "ACME-A02-000001", "Example User", "owner@example.com", "사용중", "{}", "2026-05-12T00:00:00+00:00"),
                )
                # Inactive/unknown owner that literally stores the same display name,
                # but has no directory match -> facet skips it, drill-down used to keep it.
                conn.execute(
                    """
                    INSERT INTO assets
                    (id, source, asset_tag, owner, owner_email, usage_status, metadata_json, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    ("a-inactive", "excel-import", "ACME-A02-000002", "Example User", "", "사용중", "{}", "2026-05-12T00:00:00+00:00"),
                )

            with patch.object(server, "connect", lambda: db.connect(db_path)), patch.object(
                server, "active_user_directory", lambda: directory
            ):
                facet = {row["owner"]: row for row in server.owners({})}
                drilldown = server.assets({"owner": ["Example User"]}, only_servers=False)

        self.assertIn("Example User", facet)
        facet_count = facet["Example User"]["assets"]
        drilldown_tags = {row["asset_tag"] for row in drilldown}
        # The badge count and the rows it links to must agree (both active-only).
        self.assertEqual(facet_count, len(drilldown_tags))
        self.assertEqual(facet_count, 1)
        self.assertIn("ACME-A02-000001", drilldown_tags)
        self.assertNotIn("ACME-A02-000002", drilldown_tags)


class IngestionProvenanceTest(unittest.TestCase):
    """state-1: a manual console edit must survive the next excel import."""

    def test_manual_edit_survives_excel_reimport(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            db_path = tmpdir / "infra-control.sqlite3"
            db.init_db(db_path)
            csv_path = tmpdir / "ledger.csv"
            csv_path.write_text(LEDGER_HEADER + "ACME-A02-000001,Spreadsheet Owner,host1\n", encoding="utf-8")

            _run_excel_import(db_path, tmpdir)
            with db.connect(db_path) as conn:
                imported = conn.execute(
                    "SELECT owner FROM assets WHERE asset_tag = ?", ("ACME-A02-000001",)
                ).fetchone()
            self.assertEqual(imported["owner"], "Spreadsheet Owner")

            # Operator corrects the owner in the console (audited manual_update).
            with patch.object(server, "connect", lambda: db.connect(db_path)):
                server.update_asset({"asset_tag": "ACME-A02-000001", "updates": {"owner": "Manual Owner"}})

            # Spreadsheet is re-imported unchanged (still says Spreadsheet Owner).
            _run_excel_import(db_path, tmpdir)

            with db.connect(db_path) as conn:
                row = conn.execute(
                    "SELECT owner, metadata_json FROM assets WHERE asset_tag = ?", ("ACME-A02-000001",)
                ).fetchone()

        # The manual correction must NOT be silently reverted by the import.
        self.assertEqual(row["owner"], "Manual Owner")
        meta = json.loads(row["metadata_json"])
        self.assertIn("owner", meta.get("manual_overrides", {}))


class ExcelOrphanReconciliationTest(unittest.TestCase):
    """INGEST-03: assets dropped from the spreadsheet must not linger forever."""

    def test_orphan_excel_rows_pruned_on_reimport(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            db_path = tmpdir / "infra-control.sqlite3"
            db.init_db(db_path)
            csv_path = tmpdir / "ledger.csv"
            csv_path.write_text(
                LEDGER_HEADER + "ACME-A02-000001,Owner A,host1\nACME-A02-000002,Owner B,host2\n", encoding="utf-8"
            )
            _run_excel_import(db_path, tmpdir)
            with db.connect(db_path) as conn:
                first = {
                    r[0] for r in conn.execute(
                        "SELECT asset_tag FROM assets WHERE source = 'excel-import'"
                    ).fetchall()
                }

            # ACME-A02-000002 is decommissioned and removed from the spreadsheet.
            csv_path.write_text(LEDGER_HEADER + "ACME-A02-000001,Owner A,host1\n", encoding="utf-8")
            _run_excel_import(db_path, tmpdir)
            with db.connect(db_path) as conn:
                after = {
                    r[0] for r in conn.execute(
                        "SELECT asset_tag FROM assets WHERE source = 'excel-import'"
                    ).fetchall()
                }

        self.assertEqual(first, {"ACME-A02-000001", "ACME-A02-000002"})
        self.assertEqual(after, {"ACME-A02-000001"})

    def test_orphan_locations_and_maintenance_pruned_on_reimport(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            db_path = tmpdir / "infra-control.sqlite3"
            db.init_db(db_path)
            csv_path = tmpdir / "ledger.csv"
            header = "자산관리번호,담당자,호스트명,자산위치,유지보수\n"
            csv_path.write_text(
                header
                + "ACME-A02-000001,Owner A,host1,전산실 / R01,2026-12-01\n"
                + "ACME-A02-000002,Owner B,host2,전산실 / R02,2026-12-02\n",
                encoding="utf-8",
            )
            _run_excel_import(db_path, tmpdir)
            with db.connect(db_path) as conn:
                loc1 = {r[0] for r in conn.execute("SELECT asset_tag FROM locations").fetchall()}
                mnt1 = {r[0] for r in conn.execute("SELECT asset_tag FROM maintenance_events").fetchall()}

            # ACME-A02-000002 is decommissioned and removed from the spreadsheet.
            csv_path.write_text(header + "ACME-A02-000001,Owner A,host1,전산실 / R01,2026-12-01\n", encoding="utf-8")
            _run_excel_import(db_path, tmpdir)
            with db.connect(db_path) as conn:
                loc2 = {r[0] for r in conn.execute("SELECT asset_tag FROM locations").fetchall()}
                mnt2 = {r[0] for r in conn.execute("SELECT asset_tag FROM maintenance_events").fetchall()}

        self.assertIn("ACME-A02-000002", loc1)
        self.assertIn("ACME-A02-000002", mnt1)
        self.assertNotIn("ACME-A02-000002", loc2)
        self.assertNotIn("ACME-A02-000002", mnt2)
        self.assertIn("ACME-A02-000001", loc2)


class AssetStateGuardTest(unittest.TestCase):
    """state-5: lifecycle invariants; state-3: refuse ambiguous multi-row edits."""

    def _insert(self, conn, **cols) -> None:
        base = {
            "id": "x", "source": "excel-import", "asset_tag": "ACME-A02-1",
            "status": "", "usage_status": "", "owner": "", "metadata_json": "{}",
            "updated_at": "2026-05-12T00:00:00+00:00",
        }
        base.update(cols)
        keys = ", ".join(base)
        marks = ", ".join("?" for _ in base)
        conn.execute(f"INSERT INTO assets ({keys}) VALUES ({marks})", tuple(base.values()))

    def test_retired_asset_rejects_in_use_and_owner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "infra-control.sqlite3"
            db.init_db(db_path)
            with db.connect(db_path) as conn:
                self._insert(conn, id="r1", asset_tag="ACME-A02-RET", status="폐기", usage_status="종료/제외")
            with patch.object(server, "connect", lambda: db.connect(db_path)):
                with self.assertRaises(ValueError):
                    server.update_asset({"asset_tag": "ACME-A02-RET", "updates": {"usage_status": "사용중"}})
                with self.assertRaises(ValueError):
                    server.update_asset({"asset_tag": "ACME-A02-RET", "updates": {"owner": "Someone"}})
                # A benign, non-conflicting edit must still succeed.
                ok = server.update_asset({"asset_tag": "ACME-A02-RET", "updates": {"purpose": "보관"}})
                self.assertTrue(ok["passed"])

    def test_retired_asset_is_not_needs_review_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "infra-control.sqlite3"
            db.init_db(db_path)
            with db.connect(db_path) as conn:
                self._insert(conn, id="r1", asset_tag="ACME-A02-RET", usage_status="종료/제외", category="노트북")
                self._insert(conn, id="r2", asset_tag="ACME-A02-CHK", usage_status="확인필요", category="노트북")
            with patch.object(server, "connect", lambda: db.connect(db_path)):
                result = server.summary()

        self.assertEqual(result["retired"], 1)
        self.assertEqual(result["needs_review"], 1)
        self.assertEqual(result["operational_assets"], 1)
        self.assertEqual(result["asset_categories"]["notebook"]["count"], 2)
        self.assertEqual(result["asset_categories"]["notebook"]["operational"], 1)

    def test_old_operational_notebook_is_replacement_and_sale_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "infra-control.sqlite3"
            db.init_db(db_path)
            with db.connect(db_path) as conn:
                self._insert(
                    conn,
                    id="old",
                    asset_tag="ACME-A02-210001",
                    usage_status="사용중",
                    category="노트북",
                    metadata_json=json.dumps({"source_row": {"구매일": "2021-01-01"}}, ensure_ascii=False),
                )
                self._insert(
                    conn,
                    id="new",
                    asset_tag="ACME-A02-240001",
                    usage_status="사용중",
                    category="노트북",
                    metadata_json=json.dumps({"source_row": {"구매일": "2099-01-01"}}, ensure_ascii=False),
                )
                self._insert(
                    conn,
                    id="retired",
                    asset_tag="ACME-A02-210002",
                    usage_status="종료/제외",
                    category="노트북",
                    metadata_json=json.dumps({"source_row": {"구매일": "2021-01-01"}}, ensure_ascii=False),
                )
            with patch.object(server, "connect", lambda: db.connect(db_path)):
                result = server.summary()
                rows = server.assets({"lifecycle": ["notebook_refresh"]}, only_servers=False)

        self.assertEqual(result.get("notebook_replacement_due"), 1)
        self.assertEqual(result.get("notebook_sale_candidates"), 1)
        self.assertEqual([row["asset_tag"] for row in rows], ["ACME-A02-210001"])
        self.assertEqual(rows[0]["lifecycle_label"], "교체/매각 대상")
        self.assertGreater(float(rows[0]["notebook_age_years"]), 4.0)
        self.assertEqual(rows[0]["notebook_refresh_priority"], "교체 계획")
        self.assertEqual(rows[0]["notebook_refresh_decision"], "교체 후 회수/매각")
        self.assertIn("구매일", rows[0]["notebook_refresh_reason"])
        self.assertIn("담당 확인", rows[0]["notebook_refresh_flags"])

    def test_update_refuses_ambiguous_duplicate_tag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "infra-control.sqlite3"
            db.init_db(db_path)
            with db.connect(db_path) as conn:
                self._insert(conn, id="d1", source="snipe-ops", asset_tag="ACME-A02-DUP", owner="Owner A")
                self._insert(conn, id="d2", source="excel-import", asset_tag="ACME-A02-DUP", owner="Owner B")
            with patch.object(server, "connect", lambda: db.connect(db_path)):
                with self.assertRaises(ValueError):
                    server.update_asset({"asset_tag": "ACME-A02-DUP", "updates": {"owner": "Owner C"}})
            # Neither row should have been mutated.
            with db.connect(db_path) as conn:
                owners = {r[0] for r in conn.execute(
                    "SELECT owner FROM assets WHERE asset_tag = 'ACME-A02-DUP'"
                ).fetchall()}
        self.assertEqual(owners, {"Owner A", "Owner B"})


if __name__ == "__main__":
    unittest.main()
