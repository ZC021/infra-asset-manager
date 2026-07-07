from __future__ import annotations

import tempfile
import importlib.util
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from infra_control import db
from infra_control import computer_use_receipts
from infra_control.connectors import excel_assets, snipe_ops
import server


JIRA_IMPORT_SPEC = importlib.util.spec_from_file_location(
    "import_jira_board", Path(__file__).resolve().parents[1] / "scripts" / "import_jira_board.py"
)
assert JIRA_IMPORT_SPEC and JIRA_IMPORT_SPEC.loader
import_jira_board = importlib.util.module_from_spec(JIRA_IMPORT_SPEC)
JIRA_IMPORT_SPEC.loader.exec_module(import_jira_board)
sys.modules["import_jira_board"] = import_jira_board


def load_script_module(name: str, relative_path: str):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).resolve().parents[1] / relative_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


ASSET_WHY_AUDIT = load_script_module("asset_why_audit", "scripts/asset_why_audit.py")
SYNC_JIRA_API = load_script_module("sync_jira_api", "scripts/sync_jira_api.py")


class SnipeUsageRulesTest(unittest.TestCase):
    def test_active_nac_overrides_weak_idle_ledger_status(self) -> None:
        status, reason = snipe_ops.infer_usage(
            {
                "ledger_item_kind": "노트북",
                "ledger_status": "유휴",
                "nac_status": "사용중",
                "nac_recently_used": "Y",
                "recent_seen_nac_14d": "Y",
                "nac_username_norm": "user.name",
            }
        )

        self.assertEqual(status, "사용중")
        self.assertEqual(reason, "NAC 사용 근거")

    def test_active_nac_and_intune_make_explicit_usage_basis(self) -> None:
        status, reason = snipe_ops.infer_usage(
            {
                "intune_user_upn": "user.name@example.com",
                "recent_sync_intune_30d": "Y",
                "nac_username_norm": "user.name",
                "recent_seen_nac_14d": "Y",
            }
        )

        self.assertEqual(status, "사용중")
        self.assertEqual(reason, "NAC+Intune 사용 근거")

    def test_laptop_without_nac_or_intune_requires_review(self) -> None:
        status, reason = snipe_ops.infer_usage({"ledger_item_kind": "노트북", "ledger_model": "MacBook Air"})

        self.assertEqual(status, "확인필요")
        self.assertEqual(reason, "NAC/Intune 근거 없음")

    def test_terminal_exception_marks_laptop_retired(self) -> None:
        status, reason = snipe_ops.infer_usage(
            {
                "ledger_item_kind": "노트북",
                "ledger_status": "매각처분",
                "nac_recently_used": "Y",
            }
        )

        self.assertEqual(status, "종료/제외")
        self.assertEqual(reason, "명시적 제외 상태")


class SnipeImportSnapshotRulesTest(unittest.TestCase):
    def test_default_ops_dir_prefers_infra_control_local_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy_ops = root / "legacy" / "ops"
            local_ops = root / "infra" / "var" / "imports" / "snipe-ops"
            legacy_ops.mkdir(parents=True)
            local_ops.mkdir(parents=True)
            (legacy_ops / "recon_stage.csv").write_text("asset_tag\nACME-A02-LEGACY\n", encoding="utf-8")
            (local_ops / "recon_stage.csv").write_text("asset_tag\nACME-A02-LOCAL\n", encoding="utf-8")

            with patch.object(snipe_ops, "DEFAULT_OPS_DIR", None), patch.object(
                snipe_ops, "ROOT_OPS_DIR", legacy_ops
            ), patch.object(snipe_ops, "LOCAL_OPS_DIR", local_ops):
                selected = snipe_ops.default_ops_dir()

        self.assertEqual(selected, local_ops)

    def test_import_prunes_stale_snipe_snapshot_rows_for_same_asset_tag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ops_dir = root / "ops"
            ops_dir.mkdir()
            db_path = root / "infra-control.sqlite3"
            (ops_dir / "organization_users.csv").write_text("userPrincipalName,displayName,mail,department,companyName\n", encoding="utf-8")
            (ops_dir / "recon_stage.csv").write_text(
                "\n".join(
                    [
                        ",".join(
                            [
                                "asset_tag",
                                "ledger_asset_tag",
                                "ledger_item_kind",
                                "ledger_model",
                                "ledger_serial",
                                "ledger_status",
                                "intune_asset_tag_candidate",
                                "intune_serial",
                                "intune_device_name",
                                "intune_user_upn",
                                "intune_user_localpart",
                                "recent_sync_intune_30d",
                                "nac_username_norm",
                                "nac_recently_used",
                                "recent_seen_nac_14d",
                                "nac_status",
                                "checkout_candidate_username",
                            ]
                        ),
                        ",".join(
                            [
                                "ACME-A02-000001",
                                "ACME-A02-000001",
                                "노트북",
                                "ThinkPad",
                                "SERIAL1",
                                "정상",
                                "ACME-A02-000001",
                                "SERIAL1",
                                "ACME-A02-000001",
                                "user.name@example.com",
                                "user.name",
                                "Y",
                                "user.name",
                                "Y",
                                "Y",
                                "사용중",
                                "user.name",
                            ]
                        ),
                    ]
                ),
                encoding="utf-8",
            )

            db.init_db(db_path)
            with db.connect(db_path) as conn:
                conn.execute(
                    """
                    INSERT INTO assets
                    (id, source, asset_tag, usage_status, usage_reason, metadata_json, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "stale-snipe-row",
                        "snipe-ops",
                        "ACME-A02-000001",
                        "사용중",
                        "NAC/Intune 근거 없음",
                        "{}",
                        "2026-05-01T00:00:00+00:00",
                    ),
                )

            with patch.object(snipe_ops, "init_db", lambda: db.init_db(db_path)), patch.object(
                snipe_ops, "connect", lambda: db.connect(db_path)
            ):
                snipe_ops.import_assets(ops_dir)

            with db.connect(db_path) as conn:
                rows = conn.execute(
                    """
                    SELECT id, asset_tag, usage_status, usage_reason FROM assets
                    WHERE source = 'snipe-ops' AND asset_tag = ?
                    """,
                    ("ACME-A02-000001",),
                ).fetchall()

        self.assertEqual(len(rows), 1)
        self.assertNotEqual(rows[0]["id"], "stale-snipe-row")
        self.assertEqual(rows[0]["usage_status"], "사용중")
        self.assertEqual(rows[0]["usage_reason"], "NAC+Intune 사용 근거")


class ExcelUsageRulesTest(unittest.TestCase):
    def test_downloaded_laptop_disposal_is_retired(self) -> None:
        status, reason = excel_assets.infer_usage(
            "노트북",
            {"품목": "노트북", "상태": "매각처분", "모델": "MacBook Pro", "사용자/담당자": "-"},
            is_server=0,
            is_network=0,
        )

        self.assertEqual(status, "종료/제외")
        self.assertEqual(reason, "명시적 제외 상태")

    def test_active_excel_status_is_in_use(self) -> None:
        status, reason = excel_assets.infer_usage(
            "노트북",
            {"품목": "노트북", "상태": "정상", "모델": "ThinkPad", "사용자/담당자": "Example User"},
            is_server=0,
            is_network=0,
        )

        self.assertEqual(status, "사용중")
        self.assertEqual(reason, "통합 인프라 사용 근거")

    def test_downloaded_excel_aliases_are_understood(self) -> None:
        row = {
            "품목": "노트북",
            "일련번호": "ABC123",
            "사용자/담당자": "Example User",
            "상세 용도": "공용",
        }

        self.assertEqual(excel_assets.value(row, "구분", "행 레이블", "분류", "품목"), "노트북")
        self.assertEqual(excel_assets.value(row, "시리얼", "일련번호", "serial", "S/N"), "ABC123")
        self.assertEqual(excel_assets.value(row, "담당자", "사용자/담당자", "사용자", "owner"), "Example User")
        self.assertEqual(excel_assets.value(row, "사용목적", "상세 용도", "설명", "용도", "비고"), "공용")

    def test_infra_assets_flat_csv_is_parsed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "infra-assets.csv"
            path.write_text(
                "\n".join(
                    [
                        "Stock(재고/유휴)",
                        "1",
                        "노트북2 이슈",
                        "ITAM-1",
                        "ACME-A02-000001 /",
                        "APPLE",
                        "2025.1.1",
                        "SERIAL1",
                        "",
                        "",
                        "",
                        "ITAM-2",
                        "ACME-A02-000002 / Example User / Example User",
                        "LG",
                        "2025.1.2",
                        "SERIAL2",
                    ]
                ),
                encoding="utf-8",
            )

            rows = excel_assets.normalize_records(path)

        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["usage_status"], "미사용")
        self.assertEqual(rows[0]["usage_reason"], "infra-assets 재고/유휴")
        self.assertEqual(rows[1]["owner"], "Example User / Example User")
        self.assertEqual(rows[1]["usage_status"], "사용중")
        self.assertEqual(rows[1]["usage_reason"], "infra-assets 사용자 배정")


class ComputerUseReceiptRulesTest(unittest.TestCase):
    def valid_payload(self) -> dict:
        return {
            "passed": True,
            "mode": "gui-computer-use",
            "plugin": "Computer Use",
            "environment": "gui",
            "app": "Google Chrome",
            "base": computer_use_receipts.DEFAULT_EXTERNAL_BASE,
            "simulated": False,
            "checked_at": "2026-04-21T05:10:00Z",
            "operator_device": "test-device",
            "observed": {
                "clicked_modes": list(computer_use_receipts.REQUIRED_UI_MODES),
                "selected_asset": "ACME-TEST-0001",
                "selected_asset_usage_basis": "NAC 사용 근거",
                "selected_asset_usage_evidence": "NAC 최근 접속 / NAC/운영상태 사용중",
                "scroll_click_preserved_position": True,
                "csv_header": "asset_tag,usage_status,usage_reason,usage_basis,usage_evidence",
                "ui_build_id": "build-1",
            },
        }

    @patch.object(computer_use_receipts, "current_ui_build_id", return_value="build-1")
    def test_gui_receipt_accepts_nac_intune_usage_evidence(self, _build_id) -> None:
        valid, errors, detail = computer_use_receipts.validate_receipt(self.valid_payload())

        self.assertTrue(valid, errors)
        self.assertEqual(detail["selected_asset_usage_basis"], "NAC 사용 근거")

    @patch.object(computer_use_receipts, "current_ui_build_id", return_value="build-1")
    def test_gui_receipt_rejects_legacy_infra_only_usage_evidence(self, _build_id) -> None:
        payload = self.valid_payload()
        payload["observed"]["selected_asset_usage_basis"] = "통합 인프라 사용 근거"
        payload["observed"]["selected_asset_usage_evidence"] = "통합 인프라 사용 근거"

        valid, errors, _detail = computer_use_receipts.validate_receipt(payload)

        self.assertFalse(valid)
        self.assertIn("detail evidence must include NAC/Intune, checkout, or Jira board usage evidence", errors)

    @patch.object(computer_use_receipts, "current_ui_build_id", return_value="build-1")
    def test_gui_receipt_rejects_scroll_jump_regression(self, _build_id) -> None:
        payload = self.valid_payload()
        payload["observed"]["scroll_click_preserved_position"] = False

        valid, errors, _detail = computer_use_receipts.validate_receipt(payload)

        self.assertFalse(valid)
        self.assertIn("scroll_click_preserved_position must be true", errors)


class AssetGroupRulesTest(unittest.TestCase):
    def test_asset_group_uses_first_two_asset_tag_segments(self) -> None:
        self.assertEqual(server.asset_group("ACME-TEST-0001"), "ACME-A02")
        self.assertEqual(server.asset_group("ACME-B03-190010"), "ACME-B03")
        self.assertEqual(server.asset_group("ACME-VA-240018"), "ACME-VA")

    def test_asset_group_falls_back_for_non_standard_tags(self) -> None:
        self.assertEqual(server.asset_group("0db6445951875607ced2c188"), "기타")
        self.assertEqual(server.asset_group("C2960X-24TS-LL"), "기타")
        self.assertEqual(server.asset_group(""), "미지정")


class JiraBoardMetadataRulesTest(unittest.TestCase):
    def test_jira_board_metadata_is_exposed_and_included_as_evidence(self) -> None:
        row = server.enrich_asset(
            {
                "asset_tag": "ACME-A02-220295",
                "usage_reason": "Jira In Progress(사용중)",
                "metadata_json": json.dumps(
                    {
                        "jira_board": {
                            "lane": "inprogress",
                            "lane_label": "In Progress(사용중)",
                            "issue": "ITAM-564",
                        }
                    },
                    ensure_ascii=False,
                ),
            }
        )

        self.assertEqual(row["jira_lane"], "In Progress(사용중)")
        self.assertEqual(row["jira_issue"], "ITAM-564")
        self.assertIn("Jira board In Progress(사용중) ITAM-564", row["usage_evidence"])

    def test_jira_stock_with_active_endpoint_evidence_requires_review(self) -> None:
        status, usage_status, usage_reason, reconciliation = import_jira_board.reconcile_usage(
            "stock",
            {"asset_tag": "ACME-A02-210197", "category": "노트북", "model": "LG"},
            {"usage_basis": "Intune 사용 근거", "usage_evidence": ["Intune 최근 동기화"]},
            "재고/유휴",
            "미사용",
            "Jira Stock(재고/유휴)",
        )

        self.assertEqual(status, "확인필요")
        self.assertEqual(usage_status, "확인필요")
        self.assertEqual(usage_reason, "Jira/NAC-Intune 충돌 확인필요")
        self.assertEqual(reconciliation["decision"], "conflict_jira_inactive_endpoint_active")

    def test_jira_inprogress_without_endpoint_evidence_requires_review_for_endpoint_asset(self) -> None:
        status, usage_status, usage_reason, reconciliation = import_jira_board.reconcile_usage(
            "inprogress",
            {"asset_tag": "ACME-A02-250396", "category": "노트북", "model": "MacBook"},
            {"usage_basis": "", "usage_evidence": []},
            "In Progress(사용중)",
            "사용중",
            "Jira In Progress(사용중)",
        )

        self.assertEqual(status, "확인필요")
        self.assertEqual(usage_status, "확인필요")
        self.assertEqual(usage_reason, "Jira/NAC-Intune 충돌 확인필요")
        self.assertEqual(reconciliation["decision"], "conflict_jira_active_endpoint_inactive")

    def test_jira_and_endpoint_active_stays_in_use(self) -> None:
        status, usage_status, usage_reason, reconciliation = import_jira_board.reconcile_usage(
            "inprogress",
            {"asset_tag": "ACME-TEST-0001", "category": "노트북", "model": "MacBook"},
            {"usage_basis": "NAC 사용 근거", "usage_evidence": ["NAC 최근 접속"]},
            "In Progress(사용중)",
            "사용중",
            "Jira In Progress(사용중)",
        )

        self.assertEqual(status, "In Progress(사용중)")
        self.assertEqual(usage_status, "사용중")
        self.assertEqual(usage_reason, "Jira In Progress(사용중)")
        self.assertEqual(reconciliation["decision"], "match_active")

    def test_jira_api_issue_is_mapped_to_board_import_record(self) -> None:
        sync_jira_api = load_script_module("sync_jira_api", "scripts/sync_jira_api.py")

        records = sync_jira_api.records_from_issues(
            [
                {
                    "key": "ITAM-100",
                    "fields": {
                        "status": {"name": "진행 중"},
                        "customfield_asset": "ACME-TEST-0001",
                        "customfield_owner": {"name": "example.owner"},
                    },
                },
                {
                    "key": "ITAM-101",
                    "fields": {
                        "status": {"name": "Stock"},
                        "customfield_asset": "ACME-A02-250380",
                        "customfield_owner": "",
                    },
                },
            ],
            asset_tag_field="customfield_asset",
            owner_field="customfield_owner",
        )

        self.assertEqual(
            records,
            [
                {"asset_tag": "ACME-TEST-0001", "lane": "inprogress", "issue": "ITAM-100", "owner": "example.owner", "page": None, "lines": []},
                {"asset_tag": "ACME-A02-250380", "lane": "stock", "issue": "ITAM-101", "owner": "", "page": None, "lines": []},
            ],
        )

    def test_jira_sync_is_read_only_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "infra-control.sqlite3"
            db.init_db(db_path)
            with db.connect(db_path) as conn:
                conn.execute(
                    """
                    INSERT INTO assets
                    (id, source, asset_tag, category, model, status, owner, owner_email,
                     usage_status, usage_reason, metadata_json, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "asset-1",
                        "snipe-ops",
                        "ACME-TEST-0001",
                        "노트북",
                        "ThinkPad",
                        "정상",
                        "existing.owner",
                        "existing.owner@example.com",
                        "사용중",
                        "NAC 사용 근거",
                        json.dumps({"usage_evidence": ["NAC 최근 접속"]}, ensure_ascii=False),
                        "2026-05-11T00:00:00+00:00",
                    ),
                )

            with patch.object(import_jira_board, "init_db", lambda: db.init_db(db_path)), patch.object(
                import_jira_board, "connect", lambda: db.connect(db_path)
            ):
                result = import_jira_board.apply_records(
                    [{"asset_tag": "ACME-TEST-0001", "lane": "stock", "issue": "ITAM-100", "owner": "jira.owner", "page": None, "lines": []}],
                    source_file="Jira REST API board 118",
                    source_id="jira-api",
                    source_name="Jira Hardware Asset API",
                    actor="jira-api-sync",
                )
                result_again = import_jira_board.apply_records(
                    [{"asset_tag": "ACME-TEST-0001", "lane": "stock", "issue": "ITAM-100", "owner": "jira.owner", "page": None, "lines": []}],
                    source_file="Jira REST API board 118",
                    source_id="jira-api",
                    source_name="Jira Hardware Asset API",
                    actor="jira-api-sync",
                )

            with db.connect(db_path) as conn:
                asset = conn.execute("SELECT * FROM assets WHERE asset_tag = ?", ("ACME-TEST-0001",)).fetchone()
                source = conn.execute("SELECT * FROM integration_sources WHERE id = ?", ("jira-api",)).fetchone()
                history_count = conn.execute("SELECT COUNT(*) FROM change_history WHERE source = ?", ("jira-api",)).fetchone()[0]

        self.assertTrue(result["passed"])
        self.assertTrue(result_again["passed"])
        self.assertEqual(asset["status"], "정상")
        self.assertEqual(asset["usage_status"], "사용중")
        self.assertEqual(asset["usage_reason"], "NAC 사용 근거")
        self.assertEqual(asset["owner"], "existing.owner")
        self.assertEqual(asset["owner_email"], "existing.owner@example.com")
        metadata = json.loads(asset["metadata_json"])
        self.assertEqual(metadata["jira_board"]["issue"], "ITAM-100")
        self.assertEqual(metadata["jira_board"]["lane"], "stock")
        self.assertEqual(result["updated_assets"], 1)
        self.assertEqual(result["field_changes"], {"metadata_json": 1})
        self.assertEqual(result_again["updated_assets"], 0)
        self.assertEqual(result_again["field_changes"], {})
        self.assertIsNotNone(source)
        self.assertEqual(source["name"], "Jira Hardware Asset API")
        self.assertEqual(source["status"], "read-only")
        self.assertEqual(history_count, 0)

    def test_jira_sync_preserves_missing_records_with_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "infra-control.sqlite3"
            db.init_db(db_path)
            with db.connect(db_path) as conn:
                conn.execute(
                    """
                    INSERT INTO assets
                    (id, source, asset_tag, category, model, status, owner, owner_email,
                     usage_status, usage_reason, metadata_json, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "asset-1",
                        "snipe-ops",
                        "ACME-TEST-0001",
                        "노트북",
                        "ThinkPad",
                        "정상",
                        "existing.owner",
                        "existing.owner@example.com",
                        "사용중",
                        "NAC 사용 근거",
                        "{}",
                        "2026-05-11T00:00:00+00:00",
                    ),
                )

            with patch.object(import_jira_board, "init_db", lambda: db.init_db(db_path)), patch.object(
                import_jira_board, "connect", lambda: db.connect(db_path)
            ):
                result = import_jira_board.apply_records(
                    [
                        {"asset_tag": "ACME-TEST-0001", "lane": "stock", "issue": "ITAM-100", "owner": "", "page": None, "lines": []},
                        {"asset_tag": "ACME-A02-250477", "lane": "inprogress", "issue": "ITAM-981", "owner": "owner.user", "page": None, "lines": []},
                    ],
                    source_file="Jira REST API board 118",
                    source_id="jira-api",
                    source_name="Jira Hardware Asset API",
                    actor="jira-api-sync",
                    fail_on_missing=False,
                )

            with db.connect(db_path) as conn:
                source = conn.execute("SELECT * FROM integration_sources WHERE id = ?", ("jira-api",)).fetchone()
                asset_rows = conn.execute("SELECT COUNT(*) FROM assets WHERE asset_tag = ?", ("ACME-A02-250477",)).fetchone()[0]

        self.assertTrue(result["passed"])
        self.assertEqual(asset_rows, 0)
        self.assertEqual(result["missing_assets"], ["ACME-A02-250477"])
        self.assertEqual(
            result["missing_records"],
            [
                {
                    "asset_tag": "ACME-A02-250477",
                    "issue": "ITAM-981",
                    "lane": "inprogress",
                    "lane_label": "In Progress(사용중)",
                    "owner": "owner.user",
                    "page": None,
                    "source_file": "Jira REST API board 118",
                }
            ],
        )
        metadata = json.loads(source["metadata_json"])
        self.assertEqual(metadata["missing_assets"], ["ACME-A02-250477"])
        self.assertEqual(metadata["missing_records"], result["missing_records"])

    def test_jira_api_records_preserve_searchable_hardware_fields(self) -> None:
        records = SYNC_JIRA_API.records_from_issues(
            [
                {
                    "key": "ITAM-999",
                    "fields": {
                        "summary": "ACME-A02-250477 / Jamie Owner B / Jamie Owner B",
                        "status": {"name": "진행 중"},
                        "custom_asset": "ACME-A02-250477",
                        "custom_owner": "jamie.lee@example.com",
                        "custom_model": "MacBook Air (13-inch, M5)",
                        "custom_manufacturer": {"value": "APPLE"},
                        "custom_specification": "M5 (CPU 10Core / 24GB / 2TB)",
                    },
                }
            ],
            asset_tag_field="custom_asset",
            owner_field="custom_owner",
            model_field="custom_model",
            manufacturer_field="custom_manufacturer",
            specification_field="custom_specification",
        )

        self.assertEqual(records[0]["asset_tag"], "ACME-A02-250477")
        self.assertEqual(records[0]["lane"], "inprogress")
        self.assertEqual(records[0]["summary"], "ACME-A02-250477 / Jamie Owner B / Jamie Owner B")
        self.assertEqual(records[0]["model"], "MacBook Air (13-inch, M5)")
        self.assertEqual(records[0]["manufacturer"], "APPLE")
        self.assertIn("M5", records[0]["specification"])
        self.assertIn("MacBook Air (13-inch, M5)", records[0]["lines"])


class ApprovalWritebackRulesTest(unittest.TestCase):
    def write_approval_fixture(self, root: Path) -> dict[str, Path]:
        snapshot = root / "approval_tasks.json"
        workflows = root / "approval_workflows.json"
        details = root / "approval_details.json"
        queue = root / "amaranth_writeback_queue.json"
        snapshot.write_text(
            json.dumps(
                {
                    "items": [
                        {
                            "docId": "doc-1",
                            "docNo": "AP-1",
                            "docTitle": "구독형 서비스 계정사용 신청",
                            "sourceBoxCode": "120",
                            "sourceLabel": "시행함",
                            "operYn": "N",
                            "readYn": "Y",
                            "formName": "계정신청",
                            "createdBy": "신청자",
                            "deptName": "IT",
                        }
                    ]
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        workflows.write_text(json.dumps({"version": 2, "records": {}}, ensure_ascii=False), encoding="utf-8")
        details.write_text(json.dumps({"items": {}}, ensure_ascii=False), encoding="utf-8")
        return {"snapshot": snapshot, "workflows": workflows, "details": details, "queue": queue}

    def test_request_writeback_creates_local_outbox_item_when_external_api_is_unconfigured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = self.write_approval_fixture(Path(tmp))
            with patch.object(server, "APPROVAL_SNAPSHOT", paths["snapshot"]), patch.object(
                server, "APPROVAL_WORKFLOWS", paths["workflows"]
            ), patch.object(server, "APPROVAL_DETAILS", paths["details"]), patch.object(
                server, "AMARANTH_WRITEBACK_QUEUE", paths["queue"]
            ), patch.dict("os.environ", {"AMARANTH_WRITEBACK_ENDPOINT": ""}, clear=False):
                result = server.update_approval_workflow(
                    {
                        "docId": "doc-1",
                        "action": "request_writeback",
                        "localStatus": "done",
                        "dispatchMemo": "계정 발급 완료",
                        "comment": "처리 내용 원문 반영 요청",
                        "actor": "operator-ui",
                    }
                )

                queue_payload = json.loads(paths["queue"].read_text(encoding="utf-8"))

        self.assertTrue(result["passed"])
        self.assertEqual(result["record"]["writebackStatus"], "local_outbox_unsent")
        self.assertEqual(len(queue_payload["items"]), 1)
        self.assertEqual(queue_payload["items"][0]["docId"], "doc-1")
        self.assertEqual(queue_payload["items"][0]["status"], "local_outbox_unsent")

    def test_request_writeback_deduplicates_local_outbox_by_doc_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = self.write_approval_fixture(Path(tmp))
            with patch.object(server, "APPROVAL_SNAPSHOT", paths["snapshot"]), patch.object(
                server, "APPROVAL_WORKFLOWS", paths["workflows"]
            ), patch.object(server, "APPROVAL_DETAILS", paths["details"]), patch.object(
                server, "AMARANTH_WRITEBACK_QUEUE", paths["queue"]
            ), patch.dict("os.environ", {"AMARANTH_WRITEBACK_ENDPOINT": ""}, clear=False):
                first = server.update_approval_workflow(
                    {
                        "docId": "doc-1",
                        "action": "request_writeback",
                        "localStatus": "done",
                        "dispatchMemo": "계정 발급 완료",
                        "comment": "처리 내용 원문 반영 요청",
                        "actor": "operator-ui",
                    }
                )
                second = server.update_approval_workflow(
                    {
                        "docId": "doc-1",
                        "action": "request_writeback",
                        "localStatus": "done",
                        "dispatchMemo": "계정 발급 최종",
                        "comment": "중복 요청 갱신",
                        "actor": "operator-ui",
                    }
                )

                queue_payload = json.loads(paths["queue"].read_text(encoding="utf-8"))

        first_id = first["record"]["writebackQueueId"]
        self.assertEqual(len(queue_payload["items"]), 1)
        self.assertEqual(queue_payload["items"][0]["id"], first_id)
        self.assertEqual(second["record"]["writebackQueueId"], first_id)
        self.assertEqual(queue_payload["items"][0]["status"], "local_outbox_unsent")
        self.assertEqual(queue_payload["items"][0]["dispatchMemo"], "계정 발급 최종")
        self.assertEqual(server.queue_record_count(queue_payload), 1)

    def test_sources_include_oidc_and_writeback_integration_statuses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            queue = Path(tmp) / "amaranth_writeback_queue.json"
            queue.write_text(
                json.dumps({"version": 1, "items": [{"id": "wb-1", "status": "queued"}]}, ensure_ascii=False),
                encoding="utf-8",
            )
            with patch.object(server, "AMARANTH_WRITEBACK_QUEUE", queue), patch.dict(
                "os.environ",
                {
                    "AMARANTH_WRITEBACK_ENDPOINT": "",
                    "OIDC_ISSUER_URL": "",
                    "OIDC_CLIENT_ID": "",
                },
                clear=False,
            ):
                rows = server.virtual_integration_sources()

        by_id = {row["id"]: row for row in rows}
        self.assertEqual(by_id["amaranth-writeback"]["status"], "local-outbox")
        self.assertEqual(by_id["amaranth-writeback"]["record_count"], 1)
        self.assertEqual(by_id["oidc"]["status"], "not_configured")


class DashboardCompletionRulesTest(unittest.TestCase):
    def test_assets_endpoint_default_returns_all_current_scale_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "infra-control.sqlite3"
            db.init_db(db_path)
            with db.connect(db_path) as conn:
                conn.executemany(
                    """
                    INSERT INTO assets
                    (id, source, asset_tag, usage_status, usage_reason, metadata_json, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            f"asset-{index}",
                            "snipe-ops",
                            f"ACME-A02-{index:06d}",
                            "사용중",
                            "NAC+Intune 사용 근거",
                            "{}",
                            "2026-05-12T00:00:00+00:00",
                        )
                        for index in range(835)
                    ],
                )

            with patch.object(server, "connect", lambda: db.connect(db_path)):
                rows = server.assets({}, only_servers=False)

        self.assertEqual(len(rows), 835)

    def test_data_quality_exposes_operator_ui_signals(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "infra-control.sqlite3"
            db.init_db(db_path)
            with db.connect(db_path) as conn:
                rows = []
                for index in range(501):
                    rows.append(
                        (
                            f"asset-{index}",
                            "snipe-ops" if index else "excel-import",
                            "DUP-1" if index in {0, 1} else f"ACME-A02-{index:06d}",
                            "확인필요" if index == 0 else "사용중",
                            "Jira/NAC-Intune 충돌 확인필요" if index == 0 else "NAC+Intune 사용 근거",
                            "{}",
                            "2026-05-12T00:00:00+00:00",
                        )
                    )
                conn.executemany(
                    """
                    INSERT INTO assets
                    (id, source, asset_tag, usage_status, usage_reason, metadata_json, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    rows,
                )
                conn.execute(
                    """
                    INSERT INTO integration_sources
                    (id, name, kind, endpoint, status, last_sync_at, metadata_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    ("stale-excel", "stale.xlsx", "excel-import", "/tmp/stale.xlsx", "imported", "2026-04-01T00:00:00+00:00", "{}"),
                )

            with patch.object(server, "connect", lambda: db.connect(db_path)):
                result = server.data_quality()

        cards = {item["id"]: item for item in result["cards"]}
        triage = {item["label"]: item for item in result["triage"]}
        self.assertEqual(cards["partial_assets"]["value"], "501개")
        self.assertNotIn("/", cards["partial_assets"]["value"])
        self.assertEqual(cards["partial_changes"]["value"], "0건")
        self.assertNotIn("/", cards["partial_changes"]["value"])
        self.assertEqual(cards["partial_assets"]["tone"], "ok")
        self.assertNotIn("maintenance_empty", cards)
        self.assertNotIn("status_freshness", cards)
        self.assertNotIn("latest_status_at", result["counts"])
        self.assertNotIn("status_age_hours", result["counts"])
        self.assertNotIn("multi_targets", result["counts"])
        self.assertNotIn("synthetic_targets", result["counts"])
        self.assertNotIn("원격 대상 정리", triage)
        self.assertEqual(result["counts"]["duplicate_asset_tags"], 0)
        self.assertEqual(result["counts"]["jira_nac_conflicts"], 1)
        self.assertEqual(triage["Jira/NAC 충돌"]["count"], 1)
        self.assertEqual(triage["중복 자산번호"]["target_filter"], {"dup_tag": "1"})

    def test_data_quality_counts_software_unlicensed_installs_from_summary_units(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "infra-control.sqlite3"
            db.init_db(db_path)
            with db.connect(db_path) as conn:
                conn.executemany(
                    """
                    INSERT INTO software_inventory_summary
                    (sw_id, software_name, install_count, legal_install_count, illegal_install_count,
                     temp_install_count, license_amount, residue_amount, assign_amount,
                     assign_uninstall_amount, collected_at, imported_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        ("SW-1", "Unauthorized One", 10, None, 3, None, 10, 7, None, None, "2026-06-22T00:00:00+00:00", "2026-06-22T00:00:00+00:00"),
                        ("SW-2", "Unauthorized Two", 4, None, 2, None, 4, 2, None, None, "2026-06-22T00:00:00+00:00", "2026-06-22T00:00:00+00:00"),
                        ("SW-3", "Clean Three", 1, None, 0, None, 1, 0, None, None, "2026-06-22T00:00:00+00:00", "2026-06-22T00:00:00+00:00"),
                    ],
                )

            with patch.object(server, "connect", lambda: db.connect(db_path)):
                result = server.data_quality()

        cards = {item["id"]: item for item in result["cards"]}
        triage = {item["label"]: item for item in result["triage"]}
        self.assertEqual(result["counts"]["software_unlicensed_installs"], 5)
        self.assertEqual(result["counts"]["software_unlicensed_titles"], 2)
        self.assertEqual(cards["software_inventory"]["detail"], "라이선스 확인 5건")
        self.assertEqual(cards["software_inventory"]["tone"], "warn")
        self.assertEqual(triage["SW 라이선스 확인"]["count"], 5)

    def test_repeated_jira_today_uses_kst_start_of_day_utc_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "infra-control.sqlite3"
            db.init_db(db_path)
            with db.connect(db_path) as conn:
                kst_start = conn.execute("SELECT datetime('now','+9 hours','start of day','-9 hours')").fetchone()[0]
                before_kst_start = conn.execute(
                    "SELECT datetime(datetime('now','+9 hours','start of day','-9 hours'), '-1 second')"
                ).fetchone()[0]
                conn.executemany(
                    """
                    INSERT INTO change_history
                    (id, asset_tag, changed_at, actor, action, field, old_value, new_value, reason, source, metadata_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        ("old-1", "ACME-A02-OLD", before_kst_start, "jira", "update", "status", "a", "b", "", "jira-api", "{}"),
                        ("old-2", "ACME-A02-OLD", before_kst_start, "jira", "update", "status", "a", "b", "", "jira-api", "{}"),
                        ("new-1", "ACME-A02-NEW", kst_start, "jira", "update", "status", "a", "b", "", "jira-api", "{}"),
                        ("new-2", "ACME-A02-NEW", kst_start, "jira", "update", "status", "a", "b", "", "jira-api", "{}"),
                    ],
                )

            with patch.object(server, "connect", lambda: db.connect(db_path)):
                result = server.data_quality()

        self.assertEqual(result["counts"]["repeated_jira_today"], 1)

    def test_software_inventory_adds_computed_residue_without_overwriting_source_residue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "infra-control.sqlite3"
            db.init_db(db_path)
            with db.connect(db_path) as conn:
                conn.executemany(
                    """
                    INSERT INTO software_inventory_summary
                    (sw_id, software_name, install_count, legal_install_count, illegal_install_count,
                     temp_install_count, license_amount, residue_amount, assign_amount,
                     assign_uninstall_amount, collected_at, imported_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        ("SW-A", "Residue App", 7, None, 0, None, 10, 99, None, None, "2026-06-22T00:00:00+00:00", "2026-06-22T00:00:00+00:00"),
                        ("SW-B", "Site License App", 30, None, 0, None, 999, 999, None, None, "2026-06-22T00:00:00+00:00", "2026-06-22T00:00:00+00:00"),
                    ],
                )

            with patch.object(server, "connect", lambda: db.connect(db_path)):
                rows = server.software_inventory({})

        by_id = {row["sw_id"]: row for row in rows}
        self.assertEqual(by_id["SW-A"]["residue_amount"], 99)
        self.assertEqual(by_id["SW-A"]["computed_residue"], 3)
        self.assertIs(by_id["SW-A"]["residue_reconciles"], False)
        self.assertEqual(by_id["SW-B"]["residue_amount"], 999)
        self.assertIsNone(by_id["SW-B"]["computed_residue"])
        self.assertIsNone(by_id["SW-B"]["residue_reconciles"])

    def test_software_compliance_summary_keeps_title_counts_and_adds_unit_totals(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "infra-control.sqlite3"
            db.init_db(db_path)
            with db.connect(db_path) as conn:
                conn.executemany(
                    """
                    INSERT INTO software_inventory_summary
                    (sw_id, software_name, install_count, legal_install_count, illegal_install_count,
                     temp_install_count, license_amount, residue_amount, assign_amount,
                     assign_uninstall_amount, collected_at, imported_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        ("SW-I", "Illegal Units", 5, None, 3, None, 10, 5, None, None, "2026-06-22T00:00:00+00:00", "2026-06-22T00:00:00+00:00"),
                        ("SW-O", "Over Units", 5, None, 0, None, 2, -3, None, None, "2026-06-22T00:00:00+00:00", "2026-06-22T00:00:00+00:00"),
                        ("SW-S", "Shelfware Units", 2, None, 0, None, 10, 8, None, None, "2026-06-22T00:00:00+00:00", "2026-06-22T00:00:00+00:00"),
                    ],
                )

            with patch.object(server, "connect", lambda: db.connect(db_path)):
                result = server.software_compliance({})

        self.assertEqual(result["summary"]["illegal_installs"], 1)
        self.assertEqual(result["summary"]["over_deployed"], 1)
        self.assertEqual(result["summary"]["shelfware"], 1)
        self.assertEqual(result["summary"]["illegal_install_units"], 3)
        self.assertEqual(result["summary"]["over_deployed_units"], 3)
        self.assertEqual(result["summary"]["shelfware_idle_units"], 8)

    def test_data_quality_exposes_jira_only_missing_assets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "infra-control.sqlite3"
            db.init_db(db_path)
            with db.connect(db_path) as conn:
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
                                "record_count": 1,
                                "missing_assets": ["ACME-A02-250477"],
                                "missing_records": [
                                    {
                                        "asset_tag": "ACME-A02-250477",
                                        "issue": "ITAM-981",
                                        "lane": "inprogress",
                                        "lane_label": "In Progress(사용중)",
                                        "owner": "owner.user",
                                        "page": None,
                                        "source_file": "Jira REST API board 118",
                                    }
                                ],
                            },
                            ensure_ascii=False,
                        ),
                    ),
                )

            with patch.object(server, "connect", lambda: db.connect(db_path)):
                missing_rows = server.jira_missing_assets({})
                filtered_rows = server.jira_missing_assets({"q": ["250477"]})
                result = server.data_quality()

        triage = {item["label"]: item for item in result["triage"]}
        self.assertEqual(len(missing_rows), 1)
        self.assertEqual(filtered_rows[0]["asset_tag"], "ACME-A02-250477")
        self.assertEqual(filtered_rows[0]["jira_issue"], "ITAM-981")
        self.assertEqual(filtered_rows[0]["jira_lane_label"], "In Progress(사용중)")
        self.assertEqual(result["counts"]["jira_missing_assets"], 1)
        self.assertEqual(triage["Jira-only 자산"]["count"], 1)
        self.assertEqual(triage["Jira-only 자산"]["target_mode"], "jira_missing")

    def test_asset_detail_returns_jira_only_fallback_for_missing_asset_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "infra-control.sqlite3"
            db.init_db(db_path)
            with db.connect(db_path) as conn:
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
                                        "issue": "ITAM-999",
                                        "lane": "inprogress",
                                        "lane_label": "In Progress(사용중)",
                                        "owner": "jamie.lee",
                                        "page": None,
                                        "source_file": "Jira REST API board 118",
                                    }
                                ]
                            },
                            ensure_ascii=False,
                        ),
                    ),
                )

            with patch.object(server, "connect", lambda: db.connect(db_path)):
                detail = server.asset_detail("ACME-A02-250477")

        self.assertIsNone(detail["asset"])
        self.assertEqual(detail["jira_missing"]["asset_tag"], "ACME-A02-250477")
        self.assertEqual(detail["jira_missing"]["jira_issue"], "ITAM-999")
        self.assertEqual(detail["jira_missing"]["jira_owner"], "jamie.lee")
        self.assertEqual(detail["changes"], [])

    def test_in_use_assets_include_jira_only_inprogress_records_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "infra-control.sqlite3"
            db.init_db(db_path)
            with db.connect(db_path) as conn:
                conn.execute(
                    """
                    INSERT INTO assets
                    (id, source, asset_tag, usage_status, usage_reason, metadata_json, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    ("asset-1", "snipe-ops", "ACME-TEST-0001", "사용중", "NAC+Intune 사용 근거", "{}", "2026-05-12T00:00:00+00:00"),
                )
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
                                        "issue": "ITAM-999",
                                        "lane": "inprogress",
                                        "lane_label": "In Progress(사용중)",
                                        "owner": "jamie.lee",
                                        "summary": "ACME-A02-250477 / Jamie Owner B / Jamie Owner B",
                                        "manufacturer": "APPLE",
                                        "model": "MacBook Air (13-inch, M5)",
                                        "specification": "M5 (CPU 10Core / 24GB / 2TB)",
                                        "page": None,
                                        "source_file": "Jira REST API board 118",
                                    },
                                    {
                                        "asset_tag": "ACME-A02-250478",
                                        "issue": "ITAM-1000",
                                        "lane": "stock",
                                        "lane_label": "재고/유휴",
                                        "owner": "",
                                        "page": None,
                                        "source_file": "Jira REST API board 118",
                                    },
                                ]
                            },
                            ensure_ascii=False,
                        ),
                    ),
                )

            with patch.object(server, "connect", lambda: db.connect(db_path)):
                rows = server.assets({"usage": ["사용중"]}, only_servers=False)
                filtered = server.assets({"usage": ["사용중"], "q": ["250477"]}, only_servers=False)
                mac_filtered = server.assets({"usage": ["사용중"], "q": ["mac"]}, only_servers=False)
                result = server.summary()

        by_tag = {row["asset_tag"]: row for row in rows}
        self.assertIn("ACME-TEST-0001", by_tag)
        self.assertIn("ACME-A02-250477", by_tag)
        self.assertNotIn("ACME-A02-250478", by_tag)
        self.assertEqual(filtered[0]["asset_tag"], "ACME-A02-250477")
        self.assertEqual(filtered[0]["source"], "jira-api")
        self.assertEqual(filtered[0]["status"], "read-only")
        self.assertEqual(filtered[0]["usage_status"], "사용중")
        self.assertEqual(filtered[0]["jira_issue"], "ITAM-999")
        self.assertEqual(filtered[0]["usage_basis"], "Jira board 사용중(read-only)")
        self.assertEqual(filtered[0]["manufacturer"], "APPLE")
        self.assertEqual(filtered[0]["model"], "MacBook Air (13-inch, M5)")
        self.assertIn("M5", filtered[0]["purpose"])
        self.assertIn("ACME-A02-250477", {row["asset_tag"] for row in mac_filtered})
        self.assertEqual(result["in_use"], 2)
        self.assertEqual(result["jira_only_in_use"], 1)

    def test_assets_endpoint_filters_only_conflicting_duplicate_asset_tags(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "infra-control.sqlite3"
            db.init_db(db_path)
            with db.connect(db_path) as conn:
                conn.executemany(
                    """
                    INSERT INTO assets
                    (id, source, asset_tag, serial, usage_status, usage_reason, metadata_json, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        ("mirror-a", "snipe-ops", "ACME-A02-MIRROR", "SER-MIRROR", "사용중", "NAC", "{}", "2026-05-12T00:00:00+00:00"),
                        ("mirror-b", "excel-import", "ACME-A02-MIRROR", "SER-MIRROR", "확인필요", "Jira/NAC-Intune 충돌 확인필요", "{}", "2026-05-12T00:00:00+00:00"),
                        ("dup-a", "snipe-ops", "ACME-A02-DUP001", "SER-DUP-A", "사용중", "NAC", "{}", "2026-05-12T00:00:00+00:00"),
                        ("dup-b", "excel-import", "ACME-A02-DUP001", "SER-DUP-B", "미사용", "재고", "{}", "2026-05-12T00:00:00+00:00"),
                        ("unique", "snipe-ops", "ACME-A02-UNIQUE", "SER-UNIQUE", "사용중", "NAC", "{}", "2026-05-12T00:00:00+00:00"),
                    ],
                )

            with patch.object(server, "connect", lambda: db.connect(db_path)):
                rows = server.assets({"dup_tag": ["1"]}, only_servers=False, limit=50000)
                quality = server.data_quality()

        self.assertEqual(len(rows), 2)
        self.assertEqual({row["asset_tag"] for row in rows}, {"ACME-A02-DUP001"})
        self.assertEqual(quality["counts"]["duplicate_asset_tags"], 1)

    def test_dashboard_duplicate_asset_buttons_carry_duplicate_filter(self) -> None:
        app_js = (Path(__file__).resolve().parents[1] / "static" / "app.js").read_text(encoding="utf-8")

        self.assertIn('data-filter=\'${html(JSON.stringify({ dup_tag: "1" }))}\'', app_js)
        self.assertNotIn('<button type="button" data-mode="assets"><span>중복 자산번호</span>', app_js)
        self.assertIn("jira_missing", app_js)
        self.assertIn("/api/jira-missing-assets", app_js)

    def test_dashboard_routes_empty_asset_search_to_jira_only_detail(self) -> None:
        app_js = (Path(__file__).resolve().parents[1] / "static" / "app.js").read_text(encoding="utf-8")

        self.assertIn('query: initialParams.get("q") || ""', app_js)
        self.assertIn("fallbackToJiraMissing", app_js)
        self.assertIn('state.mode = "jira_missing"', app_js)
        self.assertIn("jiraMissingDetailPanel", app_js)
        self.assertIn('state.detail = record ? { jira_missing: record } : null', app_js)
        self.assertIn("Jira-only 사용중", app_js)

    def test_software_detail_panel_marks_unreported_legal_install_count_unknown(self) -> None:
        app_js = (Path(__file__).resolve().parents[1] / "static" / "app.js").read_text(encoding="utf-8")

        self.assertIn('kv("허가", software.legal_install_count == null ? "—" : number(software.legal_install_count), true)', app_js)

    def test_asset_why_audit_asks_about_partial_assets_and_jira_write_source(self) -> None:
        findings = ASSET_WHY_AUDIT.build_findings(
            {
                "summary": {"assets": 835},
                "quality": {
                    "counts": {
                        "assets": 835,
                        "asset_api_limit": 500,
                        "changes": 88457,
                        "change_api_limit": 300,
                        "repeated_jira_today": 1649,
                        "stale_sources": 0,
                        "duplicate_asset_tags": 0,
                        "missing_owner": 0,
                        "review_assets": 0,
                    }
                },
                "sources": [
                    {"id": "jira-api", "name": "Jira Hardware Asset API", "status": "imported"},
                    {"id": "jira-board-pdf", "name": "Jira Hardware Board PDF", "status": "imported"},
                ],
                "assets": [{} for _ in range(500)],
                "changes": [{} for _ in range(300)],
            }
        )

        ids = {item["id"] for item in findings}
        self.assertIn("asset_api_partial", ids)
        self.assertIn("change_history_partial", ids)
        self.assertIn("jira_repeated_changes", ids)
        self.assertIn("jira_source_not_read_only", ids)

    def test_remote_related_surfaces_are_not_user_visible(self) -> None:
        root = Path(__file__).resolve().parents[1]
        app_js = (root / "static" / "app.js").read_text(encoding="utf-8")
        ralph = (root / "scripts" / "ralph_quality_gate.py").read_text(encoding="utf-8")

        for token in (
            '["remote",',
            '["ports", "선번장"',
            'mode === "status"',
            "원격",
            "선번장",
            "최근 원격 상태 추이",
            "/api/status-trend",
            "remoteTable",
            "portMap",
            "remote_access",
        ):
            self.assertNotIn(token, app_js)

        self.assertNotIn('"remote",', ralph)
        self.assertNotIn('"status",', ralph)
        self.assertNotIn("/api/remote-access", ralph)
        self.assertNotIn("/api/status", ralph)

    def test_dashboard_ui_has_no_fake_todo_or_hardcoded_trend_placeholders(self) -> None:
        app_js = (Path(__file__).resolve().parents[1] / "static" / "app.js").read_text(encoding="utf-8")

        self.assertNotIn("자산 라벨링 정리", app_js)
        self.assertNotIn("분기 자산 실사 준비", app_js)
        self.assertNotIn("폐기 자산 데이터 백업", app_js)
        self.assertNotIn("<sup>3</sup>", app_js)
        self.assertNotIn("김지훈", app_js)
        self.assertNotIn("04/18", app_js)
        self.assertNotIn("TODO", app_js)
        self.assertIn("/api/data-quality", app_js)
        self.assertIn("운영 품질", app_js)

    def test_dashboard_counts_use_totals_not_preview_lengths_or_proxy_values(self) -> None:
        app_js = (Path(__file__).resolve().parents[1] / "static" / "app.js").read_text(encoding="utf-8")

        self.assertIn("const reviewTotal = Number(state.summary.needs_review || 0);", app_js)
        self.assertIn("확인필요 자산 큐 <em>${number(reviewTotal)}</em>", app_js)
        self.assertIn('usageBar("DB 사용중"', app_js)
        self.assertIn('Jira-only 사용중 ${number(jiraOnly)}건은 자산 DB 밖 항목이라 합계에서 분리됩니다.', app_js)
        self.assertIn('jiraOnly ? "사용중(+Jira)" : "사용중"', app_js)
        self.assertNotIn("reviewAssets.length || state.summary.needs_review", app_js)
        self.assertNotIn("state.insights.computer_use?.passed ? 2 : 0", app_js)

    def test_search_input_debounces_server_reload_and_software_partial_notice_is_visible(self) -> None:
        app_js = (Path(__file__).resolve().parents[1] / "static" / "app.js").read_text(encoding="utf-8")

        self.assertIn("let searchTimer = null;", app_js)
        self.assertIn("function scheduleSearch(value)", app_js)
        self.assertIn("window.setTimeout", app_js)
        self.assertIn("await commitSearch(event.currentTarget.value);", app_js)
        self.assertIn("기본 ${number(swLimit)}건 표시 / 전체 ${number(swTotal)}건", app_js)

    def test_dashboard_ui_formats_timestamps_as_kst(self) -> None:
        app_js = (Path(__file__).resolve().parents[1] / "static" / "app.js").read_text(encoding="utf-8")

        self.assertIn("Asia/Seoul", app_js)
        self.assertIn("KST", app_js)
        self.assertNotIn('replace("+00:00", "")', app_js)

    def test_location_view_has_readable_rack_workspace_contract(self) -> None:
        root = Path(__file__).resolve().parents[1]
        app_js = (root / "static" / "app.js").read_text(encoding="utf-8")
        css = (root / "static" / "styles.css").read_text(encoding="utf-8")

        for token in (
            "rack-summary-strip",
            "rack-workspace",
            "rack-picker",
            "rack-detail-panel",
            "rack-stat",
            "selected?.used",
            "selected?.review",
        ):
            self.assertIn(token, app_js)

        for selector in (
            ".rack-summary-strip",
            ".rack-workspace",
            ".rack-picker",
            ".rack-detail-panel",
            ".rack-card.selected",
            ":root[data-theme=\"dark\"] .metric,",
            ":root[data-theme=\"dark\"] .rack-detail-panel",
        ):
            self.assertIn(selector, css)

    def test_operator_receipts_are_written_with_kst_timezone(self) -> None:
        root = Path(__file__).resolve().parents[1]
        daily = (root / "scripts" / "daily_snipe_ops_refresh.sh").read_text(encoding="utf-8")
        hourly = (root / "scripts" / "hourly_intune_nac_refresh.sh").read_text(encoding="utf-8")
        ralph = (root / "scripts" / "ralph_quality_gate.py").read_text(encoding="utf-8")

        self.assertIn("Asia/Seoul", daily)
        self.assertIn("timezone", daily)
        self.assertIn("Asia/Seoul", hourly)
        self.assertIn("timezone", hourly)
        self.assertIn("Asia/Seoul", ralph)

    def test_server_refresh_scripts_run_jira_api_sync(self) -> None:
        root = Path(__file__).resolve().parents[1]
        daily = (root / "scripts" / "daily_snipe_ops_refresh.sh").read_text(encoding="utf-8")
        hourly = (root / "scripts" / "hourly_intune_nac_refresh.sh").read_text(encoding="utf-8")

        for script in (daily, hourly):
            self.assertIn("INFRA_CONTROL_JIRA_SYNC", script)
            self.assertIn("sync_jira_api.py", script)
            self.assertIn("jira-api-sync.json", script)


if __name__ == "__main__":
    unittest.main()
