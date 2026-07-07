#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import ssl
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib import error, parse, request


SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent
for path in (SCRIPT_DIR, ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from import_jira_board import apply_records


KST = timezone(timedelta(hours=9), "KST")
KST_ZONE_NAME = "Asia/Seoul"
DEFAULT_ENV_FILE = Path("/etc/jira-sync/env")
DEFAULT_BOARD_ID = "118"


class SkipSync(Exception):
    pass


@dataclass(frozen=True)
class JiraConfig:
    base_url: str
    rest_api_base: str
    agile_api_base: str
    token: str
    jql: str
    board_id: str
    verify_ssl: bool
    timeout_seconds: int
    max_results: int
    asset_tag_field: str
    owner_field: str
    model_field: str
    manufacturer_field: str
    specification_field: str


def kst_now() -> str:
    return datetime.now(KST).isoformat()


def load_env_file(path: Path) -> bool:
    if not path.exists():
        return False
    loaded = False
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        os.environ.setdefault(key, value.strip().strip("\"").strip("'"))
        loaded = True
    return loaded


def env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def read_token_file(path: str) -> str:
    token_path = Path(path).expanduser()
    return token_path.read_text(encoding="utf-8", errors="replace").strip()


def bearer_token() -> str:
    token = os.environ.get("JIRA_BEARER_TOKEN", "").strip() or os.environ.get("JIRA_PAT", "").strip()
    if token:
        return token
    token_file = os.environ.get("JIRA_TOKEN_FILE", "").strip()
    if token_file:
        return read_token_file(token_file)
    raise SkipSync("Jira credential is not configured")


def http_json(base: str, path: str, token: str, params: dict[str, Any] | None, timeout: int, verify_ssl: bool) -> Any:
    query = f"?{parse.urlencode(params)}" if params else ""
    url = f"{base.rstrip('/')}{path}{query}"
    req = request.Request(url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json", "User-Agent": "infra-control-jira-api-sync/0.1"})
    context = None if verify_ssl else ssl._create_unverified_context()
    backoff = 1.0
    for attempt in range(1, 6):
        try:
            with request.urlopen(req, timeout=timeout, context=context) as response:
                return json.loads(response.read().decode("utf-8"))
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:400]
            if exc.code == 429 and attempt < 5:
                time.sleep(backoff)
                backoff *= 2
                continue
            raise RuntimeError(f"Jira API {exc.code} for {path}: {detail}") from exc


def resolve_field_id(fields: list[dict[str, Any]], name: str) -> str | None:
    target = name.strip().lower()
    for field in fields:
        if str(field.get("name", "")).strip().lower() == target:
            field_id = field.get("id")
            if field_id:
                return str(field_id)
    return None


def pick_field_id(fields: list[dict[str, Any]], env_id: str, env_name: str, defaults: list[str]) -> str:
    if os.environ.get(env_id):
        return os.environ[env_id].strip()
    candidates = [os.environ.get(env_name, "").strip(), *defaults]
    for candidate in candidates:
        if not candidate:
            continue
        field_id = resolve_field_id(fields, candidate)
        if field_id:
            return field_id
    raise RuntimeError(f"Could not resolve Jira field for {env_id}/{env_name}")


def optional_field_id(fields: list[dict[str, Any]], env_id: str, env_name: str, defaults: list[str]) -> str:
    if os.environ.get(env_id):
        return os.environ[env_id].strip()
    candidates = [os.environ.get(env_name, "").strip(), *defaults]
    for candidate in candidates:
        if not candidate:
            continue
        field_id = resolve_field_id(fields, candidate)
        if field_id:
            return field_id
    return ""


def split_order_by(jql: str) -> tuple[str, str]:
    match = re.search(r"\border\s+by\b", jql, flags=re.IGNORECASE)
    if not match:
        return jql.strip(), ""
    return jql[: match.start()].strip(), jql[match.start() :].strip()


def merge_jql(base_jql: str, extra_clause: str) -> str:
    base, order_by = split_order_by(base_jql)
    merged = f"({base}) AND ({extra_clause})"
    return f"{merged} {order_by}" if order_by else merged


def jql_from_board(rest_api_base: str, agile_api_base: str, token: str, board_id: str, timeout: int, verify_ssl: bool) -> str:
    config = http_json(agile_api_base, f"/board/{board_id}/configuration", token, None, timeout, verify_ssl)
    filter_id = ((config or {}).get("filter") or {}).get("id")
    if filter_id is None:
        raise RuntimeError(f"Jira board {board_id} has no filter id")
    filter_payload = http_json(rest_api_base, f"/filter/{int(filter_id)}", token, None, timeout, verify_ssl)
    jql = str((filter_payload or {}).get("jql") or "").strip()
    if not jql:
        raise RuntimeError(f"Jira board {board_id} filter has no JQL")
    return jql


def load_config(env_file: Path | None, optional: bool) -> JiraConfig:
    if env_file:
        loaded = load_env_file(env_file)
        if not loaded and not optional:
            raise SkipSync(f"Jira env file is not readable: {env_file}")

    token = bearer_token()
    base_url = os.environ.get("JIRA_BASE_URL", "https://jira.example.com").strip().rstrip("/")
    api_version = os.environ.get("JIRA_API_VERSION", "2").strip()
    rest_api_base = os.environ.get("JIRA_REST_API_BASE", "").strip().rstrip("/") or f"{base_url}/rest/api/{api_version}"
    agile_api_base = os.environ.get("JIRA_AGILE_API_BASE", "").strip().rstrip("/") or f"{base_url}/rest/agile/1.0"
    timeout = int(os.environ.get("JIRA_TIMEOUT_SECONDS", "30"))
    max_results = int(os.environ.get("JIRA_MAX_RESULTS", "100"))
    verify_ssl = env_bool("JIRA_VERIFY_SSL", True)
    board_id = os.environ.get("JIRA_BOARD_ID", DEFAULT_BOARD_ID).strip()
    jql = os.environ.get("JIRA_JQL", "").strip()
    if not jql and board_id:
        jql = jql_from_board(rest_api_base, agile_api_base, token, board_id, timeout, verify_ssl)
    if not jql:
        raise SkipSync("Jira JQL or board id is not configured")
    only_issuetype = os.environ.get("JIRA_ONLY_ISSUETYPE", "").strip()
    if only_issuetype:
        jql = merge_jql(jql, f'issuetype = "{only_issuetype}"')

    fields = http_json(rest_api_base, "/field", token, None, timeout, verify_ssl)
    if not isinstance(fields, list):
        raise RuntimeError("Unexpected Jira /field response")
    return JiraConfig(
        base_url=base_url,
        rest_api_base=rest_api_base,
        agile_api_base=agile_api_base,
        token=token,
        jql=jql,
        board_id=board_id,
        verify_ssl=verify_ssl,
        timeout_seconds=timeout,
        max_results=max_results,
        asset_tag_field=pick_field_id(fields, "JIRA_FIELD_ASSET_TAG_ID", "JIRA_FIELD_ASSET_TAG_NAME", ["Asset Number", "Asset Tag"]),
        owner_field=pick_field_id(fields, "JIRA_FIELD_ACTUAL_USER_ID", "JIRA_FIELD_ACTUAL_USER_NAME", ["Actual User", "Username"]),
        model_field=optional_field_id(fields, "JIRA_FIELD_MODEL_ID", "JIRA_FIELD_MODEL_NAME", ["Model Name", "Model"]),
        manufacturer_field=optional_field_id(
            fields,
            "JIRA_FIELD_MANUFACTURER_ID",
            "JIRA_FIELD_MANUFACTURER_NAME",
            ["Hardware Manufacturer", "Manufacturer"],
        ),
        specification_field=optional_field_id(
            fields,
            "JIRA_FIELD_SPECIFICATION_ID",
            "JIRA_FIELD_SPECIFICATION_NAME",
            ["Specification", "Hardware Specification"],
        ),
    )


def search_issues(config: JiraConfig) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    start_at = 0
    fields = list(dict.fromkeys([
        "summary",
        "status",
        "issuetype",
        config.asset_tag_field,
        config.owner_field,
        config.model_field,
        config.manufacturer_field,
        config.specification_field,
    ]))
    fields = [field for field in fields if field]
    while True:
        payload = http_json(
            config.rest_api_base,
            "/search",
            config.token,
            {"jql": config.jql, "startAt": start_at, "maxResults": config.max_results, "fields": ",".join(fields)},
            config.timeout_seconds,
            config.verify_ssl,
        )
        batch = payload.get("issues", []) if isinstance(payload, dict) else []
        total = int(payload.get("total", 0)) if isinstance(payload, dict) else 0
        issues.extend(batch)
        if not batch:
            break
        start_at += len(batch)
        if start_at >= total:
            break
    return issues


def scalar(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (str, int, float, bool)):
        return str(value).strip()
    if isinstance(value, dict):
        for key in ("value", "name", "key", "displayName", "emailAddress"):
            item = value.get(key)
            if item:
                return str(item).strip()
    if isinstance(value, list):
        return "|".join(part for part in (scalar(item) for item in value) if part)
    return str(value).strip()


def owner_name(value: Any) -> str:
    raw = scalar(value)
    if "@" in raw:
        return raw.split("@", 1)[0].strip()
    return raw


def lane_from_status(status: str) -> str:
    normalized = re.sub(r"[\s_-]+", "", status or "").lower()
    if normalized in {"inprogress", "inprogress(사용중)", "진행중", "사용중"} or "진행" in normalized:
        return "inprogress"
    if any(term in normalized for term in ("stock", "유휴", "재고", "폐기", "판매", "분실", "도난", "완료")):
        return "stock"
    if any(term in normalized for term in ("new", "신규", "todo", "해야할일", "open")):
        return "new"
    return "stock"


def records_from_issues(
    issues: list[dict[str, Any]],
    *,
    asset_tag_field: str,
    owner_field: str,
    model_field: str = "",
    manufacturer_field: str = "",
    specification_field: str = "",
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for issue in issues:
        fields = issue.get("fields") if isinstance(issue, dict) else {}
        if not isinstance(fields, dict):
            continue
        issue_key = scalar(issue.get("key")).upper()
        asset_tag = scalar(fields.get(asset_tag_field)).upper()
        status = fields.get("status")
        status_name = scalar(status.get("name") if isinstance(status, dict) else status)
        summary = scalar(fields.get("summary"))
        model = scalar(fields.get(model_field)) if model_field else ""
        manufacturer = scalar(fields.get(manufacturer_field)) if manufacturer_field else ""
        specification = scalar(fields.get(specification_field)) if specification_field else ""
        if not issue_key.startswith("ITAM-") or not asset_tag:
            continue
        key = (asset_tag, issue_key)
        if key in seen:
            continue
        seen.add(key)
        record = {
            "asset_tag": asset_tag,
            "lane": lane_from_status(status_name),
            "issue": issue_key,
            "owner": owner_name(fields.get(owner_field)),
            "page": None,
            "lines": [item for item in [summary, manufacturer, model, specification] if item],
        }
        for key, value in {
            "summary": summary,
            "model": model,
            "manufacturer": manufacturer,
            "specification": specification,
        }.items():
            if value:
                record[key] = value
        records.append(record)
    return records


def write_receipt(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync Jira hardware asset board into infra-control via Jira REST API.")
    parser.add_argument("--env-file", type=Path, default=Path(os.environ.get("INFRA_CONTROL_JIRA_ENV_FILE", DEFAULT_ENV_FILE)))
    parser.add_argument("--receipt", type=Path, default=ROOT / "var" / "receipts" / "jira-api-sync.json")
    parser.add_argument("--optional", action="store_true", help="Return success with skipped receipt when Jira config is missing.")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    started_at = kst_now()
    payload: dict[str, Any] = {
        "passed": False,
        "status": "failed",
        "timezone": KST_ZONE_NAME,
        "started_at": started_at,
        "finished_at": "",
        "source": "jira-api",
    }
    try:
        config = load_config(args.env_file, optional=args.optional)
        issues = search_issues(config)
        records = records_from_issues(
            issues,
            asset_tag_field=config.asset_tag_field,
            owner_field=config.owner_field,
            model_field=config.model_field,
            manufacturer_field=config.manufacturer_field,
            specification_field=config.specification_field,
        )
        import_result = apply_records(
            records,
            source_file=f"Jira REST API board {config.board_id or 'jql'}",
            source_id="jira-api",
            source_name="Jira Hardware Asset API",
            actor="jira-api-sync",
            dry_run=args.dry_run,
            fail_on_missing=False,
        )
        payload.update(
            {
                "passed": import_result["passed"],
                "status": "dry_run" if args.dry_run else "read-only",
                "base_url": config.base_url,
                "board_id": config.board_id,
                "issues_fetched": len(issues),
                "records": len(records),
                "import_result": import_result,
            }
        )
    except SkipSync as exc:
        if not args.optional:
            payload["message"] = str(exc)
            payload["finished_at"] = kst_now()
            write_receipt(args.receipt, payload)
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 2
        payload.update({"passed": True, "status": "skipped", "message": str(exc)})
    except Exception as exc:
        payload.update({"passed": False, "status": "failed", "message": str(exc)})
        payload["finished_at"] = kst_now()
        write_receipt(args.receipt, payload)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 1

    payload["finished_at"] = kst_now()
    write_receipt(args.receipt, payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
