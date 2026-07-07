from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from infra_control.computer_use_receipts import DEFAULT_EXTERNAL_BASE, save_receipt


def read_payload(path: str) -> dict[str, object]:
    if path == "-":
        data = json.loads(sys.stdin.read())
    else:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("receipt JSON object required")
    return data


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("receipt", help="GUI Computer Use receipt JSON path, or '-' for stdin")
    parser.add_argument("--expected-base", default=DEFAULT_EXTERNAL_BASE)
    args = parser.parse_args()

    payload = read_payload(args.receipt)
    result = save_receipt(payload, args.expected_base)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
