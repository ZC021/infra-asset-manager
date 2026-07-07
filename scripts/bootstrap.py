from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from infra_control.connectors.excel_assets import import_excel_assets
from infra_control.connectors.ledger_software import import_software_ledger
from infra_control.connectors.snipe_ops import import_assets
from infra_control.db import init_db


RECEIPT = ROOT / "var" / "receipts" / "bootstrap.json"


def main() -> None:
    db_path = init_db()
    snipe_result = import_assets()
    excel_result = import_excel_assets()
    ledger_result = import_software_ledger()   # 파일 없으면 status=skipped 로 그레이스풀
    payload = {
        "passed": True,
        "db": str(db_path),
        "snipe_ops": snipe_result,
        "excel_import": excel_result,
        "software_ledger": ledger_result,
    }
    RECEIPT.parent.mkdir(parents=True, exist_ok=True)
    RECEIPT.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps({"passed": True, "receipt": str(RECEIPT), **payload}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
