import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import openpyxl

from infra_control.db import connect, init_db
from infra_control.connectors.ledger_software import import_software_ledger, norm, mask_key
from scripts.software_license_handoff import build_handoff
import server


FULL_KEY = "DEMO-LICENSE-KEY"


def make_ledger_xlsx(path: Path) -> None:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "라이선스 키"
    ws.append(["소프트웨어", "발급/활성일", "수량", "식별자 유형", "값", "비고"])
    ws.append(["TreeAge Pro Healthcare", "2025-12-03", "1", "Activation/Serial Code", FULL_KEY, "Order #34819"])
    ws.append(["GitHub Team", "2025-12-15", "-", "SaaS", "-", "월 정산"])
    ws.append(["범례", "", "", "", "", ""])  # 주석행 → 제외돼야

    ws2 = wb.create_sheet("전체 요약")
    ws2.append(["구분", "소프트웨어", "공급/구매처", "계약/결제 형태", "최근 활동일", "비고"])
    ws2.append(["Microsoft", "Microsoft Win VDA Device", "Example Vendor / Open Value",
                "계약 (2026-01-15 ~ 2029-01-31)", "2026-04-27", "누적 9"])
    wb.save(path)


class LedgerConnectorTest(unittest.TestCase):
    def test_import_masks_keys_and_classifies(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "infra-control.sqlite3"
            xlsx = Path(tmp) / "ledger.xlsx"
            make_ledger_xlsx(xlsx)

            result = import_software_ledger(path=str(xlsx), db_path=db_path)
            self.assertEqual("ok", result["status"])

            with connect(db_path) as conn:
                rows = [dict(r) for r in conn.execute("SELECT * FROM software_ledger").fetchall()]
                tables = {r["name"] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}

            self.assertIn("software_ledger", tables)
            self.assertIn("software_ledger_runs", tables)
            # 주석행('범례') 제외 → 3개 제품만
            products = {r["product"] for r in rows}
            self.assertIn("TreeAge Pro Healthcare", products)
            self.assertIn("GitHub Team", products)
            self.assertIn("Microsoft Win VDA Device", products)
            self.assertNotIn("범례", products)

            treeage = next(r for r in rows if r["product"] == "TreeAge Pro Healthcare")
            self.assertEqual(1, treeage["has_key"])
            self.assertEqual(mask_key(FULL_KEY), treeage["key_masked"])

            # 전체 라이선스 키는 DB 어디에도 평문으로 남지 않아야 한다
            dump = json.dumps(rows, ensure_ascii=False)
            self.assertNotIn(FULL_KEY, dump)

            github = next(r for r in rows if r["product"] == "GitHub Team")
            self.assertEqual("subscription", github["license_type"])
            self.assertEqual(0, github["has_key"])

            winvda = next(r for r in rows if r["product"] == "Microsoft Win VDA Device")
            self.assertEqual("volume", winvda["license_type"])
            self.assertEqual("2029-01-31", winvda["expiry"])
            self.assertTrue(winvda["norm_name"])

    def test_missing_file_skips_gracefully(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "infra-control.sqlite3"
            result = import_software_ledger(path=str(Path(tmp) / "nope.xlsx"), db_path=db_path)
            self.assertEqual("skipped", result["status"])

    def test_corrupt_or_encrypted_file_returns_error_not_crash(self):
        # 암호화(보유대장 CDFV2)/손상 파일이어도 bootstrap 을 죽이지 않고 status=error 로 흡수
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "infra-control.sqlite3"
            bad = Path(tmp) / "corrupt.xlsx"
            bad.write_bytes(b"PK\x03\x04 not really xlsx \x00\x01\x02")
            result = import_software_ledger(path=str(bad), db_path=db_path)
            self.assertEqual("error", result["status"])
            self.assertEqual(0, result["product_rows"])
            self.assertIn("reason", result)
            with connect(db_path) as conn:
                count = conn.execute("SELECT COUNT(*) FROM software_ledger").fetchone()[0]
            self.assertEqual(0, count)


class ComplianceApiTest(unittest.TestCase):
    def build_db(self, db_path: Path) -> None:
        init_db(db_path)
        with connect(db_path) as conn:
            sw = [
                ("ms365", "Microsoft 365 for Business", 100, 0, 0),
                ("office19", "Microsoft Office Standard 2019", 1, 0, 60),   # shelfware
                ("winrar", "WinRAR 7.x", 1, 1, 0),                          # illegal
                ("tv", "TeamViewer", 20, 0, 15),                            # over-deployed + matched
            ]
            conn.executemany(
                "INSERT INTO software_inventory_summary (sw_id, software_name, install_count, "
                "illegal_install_count, license_amount, imported_at) VALUES (?,?,?,?,?,?)",
                [(a, b, c, d, e, "t") for (a, b, c, d, e) in sw],
            )
            ledger = [
                ("TeamViewer", norm("TeamViewer"), "perpetual", "15"),
                ("GitHub Team", norm("GitHub Team"), "subscription", "-"),
            ]
            conn.executemany(
                "INSERT INTO software_ledger (product, norm_name, license_type, qty, imported_at) "
                "VALUES (?,?,?,?,?)",
                [(p, n, t, q, "t") for (p, n, t, q) in ledger],
            )

    def test_compliance_buckets(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "infra-control.sqlite3"
            self.build_db(db_path)
            with patch.object(server, "connect", lambda: connect(db_path)):
                res = server.software_compliance({})

        s = res["summary"]
        self.assertGreaterEqual(s["matched"], 1)
        self.assertGreaterEqual(s["ledger_only"], 1)
        self.assertEqual(1, s["over_deployed"])
        self.assertEqual(1, s["illegal_installs"])
        self.assertEqual(1, s["shelfware"])

        self.assertTrue(any(m["sweeper_name"] == "TeamViewer" for m in res["matched"]))
        self.assertTrue(any(x["product"] == "GitHub Team" for x in res["ledger_only"]))
        self.assertEqual("TeamViewer", res["over_deployed"][0]["software_name"])
        self.assertEqual("WinRAR 7.x", res["illegal_installs"][0]["software_name"])

    def test_search_filter(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "infra-control.sqlite3"
            self.build_db(db_path)
            with patch.object(server, "connect", lambda: connect(db_path)):
                res = server.software_compliance({"q": ["teamviewer"]})
            self.assertTrue(all("teamviewer" in json.dumps(m, ensure_ascii=False).lower()
                                for m in res["matched"]))
            self.assertEqual([], res["ledger_only"])

    def test_empty_ledger_still_reports_sweeper_buckets(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "infra-control.sqlite3"
            self.build_db(db_path)
            with connect(db_path) as conn:
                conn.execute("DELETE FROM software_ledger")
            with patch.object(server, "connect", lambda: connect(db_path)):
                res = server.software_compliance({})
            self.assertEqual(0, res["summary"]["ledger_products"])
            self.assertEqual(1, res["summary"]["illegal_installs"])
            self.assertEqual(1, res["summary"]["shelfware"])


class HandoffDryRunTest(unittest.TestCase):
    def payload(self) -> dict:
        return {
            "message_id": "m1",
            "subject": "[비쥬얼데이타] 한글 2024 Open 라이선스 발급 안내 (Order #34902)",
            "from": "vendor@example.com",
            "received_at": "2026-06-01T09:12:00+09:00",
            "body": "한컴오피스 한글 2024 Open 라이선스가 발급되었습니다.\n수량: 5\n"
                    "인증번호: TEST-LICENSE-CODE\n신청자: Alex Example (alex.kim@example.com)",
        }

    def test_dryrun_receipt_is_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "infra-control.sqlite3"
            init_db(db_path)
            receipt = build_handoff(self.payload(), "wb.xlsx", db_path=db_path)

        self.assertEqual("software-license-handoff.v1", receipt["schema"])
        self.assertEqual("dry-run", receipt["mode"])
        self.assertTrue(receipt["teams_draft"]["requires_operator_approval"])
        # 제품명 정제
        self.assertEqual("한컴오피스 한글 2024 Open", receipt["license"]["software_name"])
        # 키 마스킹 + 전체 키 미노출
        self.assertEqual("TEST****CODE", receipt["license"]["license_key_masked"])
        self.assertNotIn("TEST-LICENSE-CODE", json.dumps(receipt, ensure_ascii=False))
        # 워크북 셀 값은 승인 시점에만
        self.assertEqual("<<APPROVED-WRITE-ONLY>>", receipt["excel_append"]["row"]["값"])
        self.assertEqual("alex.kim@example.com", receipt["approval_match"]["requester_upn"])
        self.assertEqual("34902", receipt["license"]["order_id"])


if __name__ == "__main__":
    unittest.main()
