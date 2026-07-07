import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from infra_control.db import connect, init_db
from scripts.import_sweeper_software import import_snapshot
import server


def sample_payload():
    return {
        "source": {
            "source_id": "ops-review-sweeper",
            "collected_at": "2026-05-27T06:56:43.990129+00:00",
        },
        "software": {
            "items": [
                {
                    "sw_id": "winrar",
                    "software_name": "WinRAR 7.x",
                    "install_count": 1,
                    "legal_install_count": 0,
                    "illegal_install_count": 1,
                    "temp_install_count": 0,
                    "license_amount": 0,
                    "residue_amount": 0,
                    "assign_amount": 0,
                    "assign_uninstall_amount": 0,
                    "collected_at": "2026-05-27T06:56:43.990129+00:00",
                }
            ]
        },
        "installs": {
            "items": [
                {
                    "install_id": "i1",
                    "sw_id": "winrar",
                    "equip_id": "eq1",
                    "software_name": "WinRAR 7.x",
                    "equip_code": "P123",
                    "equip_name": "ACME-A02-250376",
                    "asset_no": "",
                    "user_name": "이대규",
                    "user_key": "이대규",
                    "dept_name": "/ACME/SW개발본부/ITPS팀/",
                    "is_licensed": 0,
                    "install_date": "2026-04-21T17:41:16",
                    "report_date": "2026-04-21T17:41:16",
                    "collected_at": "2026-05-27T06:56:43.990129+00:00",
                }
            ]
        },
    }


class SweeperImportTest(unittest.TestCase):
    def test_init_db_creates_sweeper_inventory_tables(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "infra-control.sqlite3"

            init_db(db_path)
            with connect(db_path) as conn:
                tables = {
                    row["name"]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    ).fetchall()
                }

            self.assertIn("software_inventory_summary", tables)
            self.assertIn("software_inventory_installs", tables)
            self.assertIn("software_inventory_runs", tables)

    def test_import_snapshot_replaces_summary_and_installs_without_secret_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "infra-control.sqlite3"
            init_db(db_path)

            with connect(db_path) as conn:
                result = import_snapshot(conn, sample_payload(), imported_at="2026-05-27T07:30:00+00:00")
                summary = dict(conn.execute("SELECT * FROM software_inventory_summary").fetchone())
                install = dict(conn.execute("SELECT * FROM software_inventory_installs").fetchone())
                source = dict(conn.execute("SELECT * FROM integration_sources WHERE id = ?", ("ops-review-sweeper",)).fetchone())

            self.assertEqual({"software_rows": 1, "install_rows": 1}, result)
            self.assertEqual("WinRAR 7.x", summary["software_name"])
            self.assertEqual(1.0, summary["illegal_install_count"])
            self.assertEqual("이대규", install["user_key"])
            self.assertEqual(0, install["is_licensed"])
            self.assertEqual("read-only", source["status"])
            forbidden = {"LicenseKey", "LoginPassword", "password", "passwd", "login_account"}
            encoded = json.dumps({"summary": summary, "install": install}, ensure_ascii=False)
            self.assertFalse(any(term.lower() in encoded.lower() for term in forbidden))


class SoftwareInventoryApiTest(unittest.TestCase):
    def build_db(self, db_path: Path) -> None:
        init_db(db_path)
        with connect(db_path) as conn:
            conn.execute(
                """
                INSERT INTO assets (
                  id, source, asset_tag, hostname, owner, department,
                  usage_status, metadata_json, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "asset-1",
                    "snipe-ops",
                    "ACME-A02-250376",
                    "ACME-A02-250376",
                    "이대규",
                    "ITPS팀",
                    "사용중",
                    "{}",
                    "2026-05-27T00:00:00+00:00",
                ),
            )
            import_snapshot(conn, sample_payload(), imported_at="2026-05-27T07:30:00+00:00")

    def test_software_inventory_api_filters_and_orders_unlicensed_software(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "infra-control.sqlite3"
            self.build_db(db_path)

            with patch.object(server, "connect", lambda: connect(db_path)):
                rows = server.software_inventory({"q": ["winrar"]})

        self.assertEqual(1, len(rows))
        self.assertEqual("winrar", rows[0]["sw_id"])
        self.assertEqual("WinRAR 7.x", rows[0]["software_name"])
        self.assertEqual(1.0, rows[0]["illegal_install_count"])
        self.assertEqual("라이선스 확인 1", rows[0]["risk_label"])

    def test_software_inventory_api_returns_all_titles_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "infra-control.sqlite3"
            init_db(db_path)
            with connect(db_path) as conn:
                conn.executemany(
                    """
                    INSERT INTO software_inventory_summary
                      (sw_id, software_name, install_count, imported_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    [
                        (f"sw-{index:03d}", f"Tool {index:03d}", 1, "2026-05-27T07:30:00+00:00")
                        for index in range(501)
                    ],
                )

            with patch.object(server, "connect", lambda: connect(db_path)):
                rows = server.software_inventory({})
                quality = server.data_quality()

        self.assertEqual(501, len(rows))
        self.assertIsNone(quality["counts"]["software_api_limit"])

    def test_software_installs_api_filters_by_sw_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "infra-control.sqlite3"
            self.build_db(db_path)

            with patch.object(server, "connect", lambda: connect(db_path)):
                rows = server.software_installs({"sw_id": ["winrar"]})

        self.assertEqual(1, len(rows))
        self.assertEqual("ACME-A02-250376", rows[0]["asset_tag"])
        self.assertEqual("이대규", rows[0]["user_key"])
        self.assertEqual("라이선스 확인", rows[0]["license_status_label"])

    def test_software_users_api_groups_installs_by_user(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "infra-control.sqlite3"
            self.build_db(db_path)

            with patch.object(server, "connect", lambda: connect(db_path)):
                rows = server.software_users({"q": ["이대규"]})

        self.assertEqual(1, len(rows))
        self.assertEqual("이대규", rows[0]["user_key"])
        self.assertEqual("이대규", rows[0]["user_name"])
        self.assertEqual(1, rows[0]["install_count"])
        self.assertEqual(1, rows[0]["unlicensed_count"])
        self.assertEqual(1, rows[0]["software_count"])
        self.assertEqual(1, rows[0]["asset_count"])

    def test_software_installs_user_filter_matches_unknown_user_bucket(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "infra-control.sqlite3"
            self.build_db(db_path)
            with connect(db_path) as conn:
                conn.execute(
                    """
                    INSERT INTO software_inventory_installs (
                      install_id, sw_id, equip_id, software_name, equip_code, equip_name,
                      asset_no, user_key, user_name, dept_name, is_licensed,
                      install_date, report_date, collected_at, imported_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "unknown-user-install",
                        "unknown-sw",
                        "eq2",
                        "Unknown Tool",
                        "P456",
                        "ACME-A02-250377",
                        "",
                        "",
                        "",
                        "",
                        0,
                        "2026-04-21T17:41:16",
                        "2026-04-21T17:41:16",
                        "2026-05-27T06:56:43.990129+00:00",
                        "2026-05-27T07:30:00+00:00",
                    ),
                )

            with patch.object(server, "connect", lambda: connect(db_path)):
                rows = server.software_installs({"user": ["사용자 미확인"]})

        self.assertEqual(1, len(rows))
        self.assertEqual("Unknown Tool", rows[0]["software_name"])
        self.assertEqual("ACME-A02-250377", rows[0]["asset_tag"])

    def test_asset_detail_includes_installed_software_from_sweeper_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "infra-control.sqlite3"
            self.build_db(db_path)

            with patch.object(server, "connect", lambda: connect(db_path)):
                detail = server.asset_detail("ACME-A02-250376")

        self.assertIn("installed_software", detail)
        self.assertEqual(1, len(detail["installed_software"]))
        self.assertEqual("WinRAR 7.x", detail["installed_software"][0]["software_name"])
        self.assertEqual("라이선스 확인", detail["installed_software"][0]["license_status_label"])


class SoftwareInventoryUiContractTest(unittest.TestCase):
    def test_static_app_declares_software_inventory_mode_and_asset_card(self):
        app_js = (Path(__file__).resolve().parents[1] / "static" / "app.js").read_text(encoding="utf-8")

        self.assertIn("software_inventory", app_js)
        self.assertIn("SW 요약", app_js)
        self.assertIn("/api/software-inventory", app_js)
        self.assertIn("software_installs", app_js)
        self.assertIn("/api/software-installs", app_js)
        self.assertIn("SW 전체설치", app_js)
        self.assertIn("software_users", app_js)
        self.assertIn("SW 사용현황", app_js)
        self.assertIn("/api/software-users", app_js)
        self.assertIn("SW 사용자 상세", app_js)
        self.assertIn("installed_software", app_js)
        self.assertIn("설치된 소프트웨어", app_js)
        self.assertIn("notebook_refresh", app_js)
        self.assertIn("교체/매각", app_js)
        self.assertIn("notebook_replacement_due", app_js)
        self.assertIn("notebookRefreshOverview", app_js)
        self.assertIn("판단 큐", app_js)
        self.assertIn("notebook_refresh_priority", app_js)
        self.assertIn("notebook_refresh_decision", app_js)
        self.assertIn("즉시 검토", app_js)
        self.assertIn("정보 확인", app_js)


if __name__ == "__main__":
    unittest.main()
