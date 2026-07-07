#!/usr/bin/env python3
"""
Read-only approval task collector for the ACME asset portal.

The collector uses the official Amaranth approval APIs only:
- /apiproxy/api99u02A01
- /apiproxy/api99u02A02
- /apiproxy/api99u02A04

It never calls approval, dispatch, update, or write endpoints.
"""

from __future__ import annotations

import argparse
import base64
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import subprocess
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


INFRA_CONTROL_ROOT = Path(os.environ.get("INFRA_CONTROL_ROOT", "/opt/infra-asset-manager"))
SYNC_DIR = Path(os.environ.get("AMARANTH_APPROVAL_CONFIG_DIR", str(INFRA_CONTROL_ROOT / "config")))
FRONTEND_APPROVAL_DIR = Path(
    os.environ.get("AMARANTH_APPROVAL_OUTPUT_DIR", str(INFRA_CONTROL_ROOT / "var" / "approval"))
)
SNAPSHOT_PATH = Path(
    os.environ.get("AMARANTH_APPROVAL_SNAPSHOT_PATH", str(FRONTEND_APPROVAL_DIR / "approval_tasks.json"))
)
ALLOWED_BOX_CODES = {"60", "120"}
DEFAULT_DOC_STATUS_CODES = ("10", "20", "30", "40", "50", "60", "70", "80", "90", "100", "110")
SOURCE_CONFIG = {
    "60": {
        "label": "수신참조함",
        "reason": "reference_unread",
        "filter_key": "readYN",
        "active_flag": "N",
    },
    "120": {
        "label": "시행함",
        "reason": "dispatch_pending",
        "filter_key": "operYN",
        "active_flag": "N",
    },
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def env_required(name: str) -> str:
    value = str(os.environ.get(name, "")).strip()
    if not value:
        raise SystemExit(f"{name} is required.")
    return value


def env_int(name: str, default: int) -> int:
    raw = str(os.environ.get(name, "")).strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise SystemExit(f"{name} must be an integer.") from exc


def env_optional(name: str, default: str = "") -> str:
    return str(os.environ.get(name, default)).strip()


def normalize_base_url(value: str) -> str:
    return value.rstrip("/")


def normalize_string(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def normalize_flag(value: Any) -> str:
    raw = normalize_string(value).upper()
    if raw in {"Y", "N"}:
        return raw
    if raw in {"YES", "TRUE"}:
        return "Y"
    if raw in {"NO", "FALSE"}:
        return "N"
    if raw in {"미열람", "미시행"}:
        return "N"
    if raw in {"열람", "시행", "완료"}:
        return "Y"
    return raw


def first_non_empty(*values: Any) -> str:
    for value in values:
        normalized = normalize_string(value)
        if normalized:
            return normalized
    return ""


def find_first_key(data: Any, keys: tuple[str, ...]) -> Any:
    if isinstance(data, dict):
        for key in keys:
            if key in data and data[key] not in (None, "", [], {}):
                return data[key]
        for value in data.values():
            found = find_first_key(value, keys)
            if found not in (None, "", [], {}):
                return found
    elif isinstance(data, list):
        for item in data:
            found = find_first_key(item, keys)
            if found not in (None, "", [], {}):
                return found
    return None


def json_dumps(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def build_wehago_sign(*, auth_token: str, hash_key: str, transaction_id: str, timestamp: str, path: str) -> str:
    plain = f"{auth_token}{transaction_id}{timestamp}{path}"
    digest = hmac.new(hash_key.encode("utf-8"), plain.encode("utf-8"), hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")


def encrypt_user_identity(raw_value: str, aes_key: str) -> str:
    key_bytes = aes_key.encode("utf-8")
    if len(key_bytes) != 16:
        raise SystemExit("AMARANTH_SSO_AES_KEY must be exactly 16 UTF-8 bytes.")

    stamped = f"{datetime.now().strftime('%Y%m%d%H%M%S')}▦{raw_value}"
    proc = subprocess.run(
        [
            "openssl",
            "enc",
            "-aes-128-cbc",
            "-K",
            key_bytes.hex(),
            "-iv",
            key_bytes.hex(),
            "-nosalt",
            "-base64",
            "-A",
        ],
        input=stamped.encode("utf-8"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", errors="replace").strip() or "openssl enc failed"
        raise SystemExit(f"Failed to encrypt loginId/empSeq for Amaranth auth: {stderr}")
    encrypted = proc.stdout.decode("utf-8", errors="replace").strip()
    if not encrypted:
        raise SystemExit("Amaranth identity encryption produced an empty value.")
    return encrypted


@dataclass(frozen=True)
class ApiAuth:
    auth_token: str
    hash_key: str


@dataclass(frozen=True)
class ApprovalConfig:
    base_url: str
    caller_name: str
    group_seq: str
    emp_seq: str
    comp_seq: str
    dept_seq: str
    biz_seq: str
    login_id: str
    emp_name: str
    server_access_token: str
    server_hash_key: str
    sso_aes_key: str
    direct_user_auth_token: str
    direct_user_hash_key: str
    lookback_days: int
    page_size: int
    portal_public_base_url: str
    request_timeout: int

    @property
    def a01_url(self) -> str:
        return f"{self.base_url}/apiproxy/api99u02A01"

    @property
    def a02_url(self) -> str:
        return f"{self.base_url}/apiproxy/api99u02A02"

    @property
    def a04_url(self) -> str:
        return f"{self.base_url}/apiproxy/api99u02A04"

    @property
    def auth_issue_url(self) -> str:
        return f"{self.base_url}/apiproxy/api99u01A01"


class ApprovalApiClient:
    def __init__(self, config: ApprovalConfig):
        self.config = config
        self.allowed_urls = {config.auth_issue_url, config.a01_url, config.a02_url, config.a04_url}
        self.user_auth: ApiAuth | None = None
        self.server_auth: ApiAuth | None = None

    def build_headers(self, *, path: str, auth: ApiAuth) -> dict[str, str]:
        timestamp = str(int(utc_now().timestamp()))
        transaction_id = secrets.token_hex(16)
        return {
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": f"Bearer {auth.auth_token}",
            "callerName": self.config.caller_name,
            "timestamp": timestamp,
            "groupSeq": self.config.group_seq,
            "wehago-sign": build_wehago_sign(
                auth_token=auth.auth_token,
                hash_key=auth.hash_key,
                transaction_id=transaction_id,
                timestamp=timestamp,
                path=path,
            ),
            "transaction-id": transaction_id,
        }

    def post_json(self, url: str, payload: dict[str, Any], *, auth: ApiAuth) -> dict[str, Any]:
        if url not in self.allowed_urls:
            raise SystemExit(f"Blocked request outside read-only policy: POST {url}")
        body_text = json_dumps(payload)
        path = url.removeprefix(self.config.base_url) or "/"
        try:
            request = Request(
                url,
                data=body_text.encode("utf-8"),
                headers=self.build_headers(path=path, auth=auth),
                method="POST",
            )
            with urlopen(request, timeout=self.config.request_timeout) as response:
                raw = response.read().decode("utf-8")
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace").strip()
            raise SystemExit(f"{path} failed with HTTP {exc.code}: {detail}") from exc
        except URLError as exc:
            raise SystemExit(f"{path} request failed: {exc.reason}") from exc

        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"Invalid JSON from {path}") from exc

    def resolve_user_auth(self) -> ApiAuth:
        if self.user_auth is not None:
            return self.user_auth

        if self.config.direct_user_auth_token and self.config.direct_user_hash_key:
            self.user_auth = ApiAuth(
                auth_token=self.config.direct_user_auth_token,
                hash_key=self.config.direct_user_hash_key,
            )
            return self.user_auth

        if not (self.config.server_access_token and self.config.server_hash_key and self.config.sso_aes_key):
            raise SystemExit(
                "Configure either direct user auth "
                "(AMARANTH_USER_AUTH_TOKEN + AMARANTH_USER_HASH_KEY) or server auth "
                "(AMARANTH_SERVER_ACCESS_TOKEN + AMARANTH_SERVER_HASH_KEY + AMARANTH_SSO_AES_KEY)."
            )

        server_auth = ApiAuth(
            auth_token=self.config.server_access_token,
            hash_key=self.config.server_hash_key,
        )
        identity_key = encrypt_user_identity(self.config.login_id or self.config.emp_seq, self.config.sso_aes_key)
        body: dict[str, str] = {
            "groupSeq": self.config.group_seq,
        }
        if self.config.login_id:
            body["loginIdEnc"] = identity_key
        else:
            body["empSeqEnc"] = identity_key

        response = self.post_json(
            self.config.auth_issue_url,
            {
                "header": {},
                "body": body,
            },
            auth=server_auth,
        )
        result_data = find_first_key(response, ("resultData",))
        if not isinstance(result_data, dict):
            raise SystemExit("api99u01A01 response did not include resultData.")

        auth_token = first_non_empty(result_data.get("authToken"), result_data.get("AUTH_TOKEN"))
        hash_key = first_non_empty(result_data.get("hashKey"), result_data.get("HASH_KEY"))
        if not auth_token or not hash_key:
            raise SystemExit("api99u01A01 response did not include authToken/hashKey.")

        self.user_auth = ApiAuth(auth_token=auth_token, hash_key=hash_key)
        return self.user_auth

    def resolve_read_auth(self) -> ApiAuth:
        if self.server_auth is not None:
            return self.server_auth

        if self.config.server_access_token and self.config.server_hash_key:
            self.server_auth = ApiAuth(
                auth_token=self.config.server_access_token,
                hash_key=self.config.server_hash_key,
            )
            return self.server_auth

        if self.config.direct_user_auth_token and self.config.direct_user_hash_key:
            self.server_auth = ApiAuth(
                auth_token=self.config.direct_user_auth_token,
                hash_key=self.config.direct_user_hash_key,
            )
            return self.server_auth

        if self.config.sso_aes_key:
            # Compatibility fallback for tenants that require issued user auth.
            self.server_auth = self.resolve_user_auth()
            return self.server_auth

        raise SystemExit(
            "Configure AMARANTH_SERVER_ACCESS_TOKEN + AMARANTH_SERVER_HASH_KEY "
            "or provide AMARANTH_USER_AUTH_TOKEN + AMARANTH_USER_HASH_KEY."
        )

    def fetch_box_list(self) -> list[dict[str, Any]]:
        payload = {
            "header": {
                "empSeq": self.config.emp_seq,
                "groupSeq": self.config.group_seq,
            },
            "body": {
                "companyInfo": {
                    "compSeq": self.config.comp_seq,
                    "deptSeq": self.config.dept_seq,
                    "bizSeq": self.config.biz_seq,
                }
            },
        }
        response = self.post_json(self.config.a01_url, payload, auth=self.resolve_read_auth())
        box_list = find_first_key(response, ("boxList",))
        if not isinstance(box_list, list):
            raise SystemExit("A01 response did not include boxList.")
        return [item for item in box_list if isinstance(item, dict)]

    def fetch_box_items(self, *, menu_id: str, box_code: str) -> list[dict[str, Any]]:
        from_dt = (utc_now() - timedelta(days=self.config.lookback_days)).strftime("%Y-%m-%d")
        to_dt = utc_now().strftime("%Y-%m-%d")
        source = SOURCE_CONFIG[box_code]
        collected: list[dict[str, Any]] = []
        page = 1
        total_expected: int | None = None

        while True:
            body = {
                "menuId": menu_id,
                "fromDt": from_dt,
                "toDt": to_dt,
                "pageSize": str(self.config.page_size),
                "keyWord": "",
                "readYN": source["active_flag"] if box_code == "60" else "",
                "operYN": source["active_flag"] if box_code == "120" else "",
                "searchKind": "1",
                "searchEmpSeq": [],
                "docStsList": [{"docSts": code} for code in DEFAULT_DOC_STATUS_CODES],
                "page": str(page),
                "sort": "30",
                "langCode": "kr",
                "companyInfo": {
                    "compSeq": self.config.comp_seq,
                    "deptSeq": self.config.dept_seq,
                    "bizSeq": self.config.biz_seq,
                },
            }
            payload = {
                "header": {
                    "empSeq": self.config.emp_seq,
                    "groupSeq": self.config.group_seq,
                },
                "body": body,
            }
            response = self.post_json(self.config.a02_url, payload, auth=self.resolve_read_auth())
            page_items = find_first_key(response, ("eaDocList", "list", "docList", "items"))
            if not isinstance(page_items, list):
                if page == 1:
                    raise SystemExit(f"A02 response for boxCode={box_code} did not include a document list.")
                break

            total_raw = find_first_key(response, ("totalCnt", "totalCount", "totCnt"))
            if total_expected is None and total_raw not in (None, ""):
                try:
                    total_expected = int(str(total_raw))
                except ValueError:
                    total_expected = None

            normalized_items = [item for item in page_items if isinstance(item, dict)]
            if not normalized_items:
                break

            collected.extend(normalized_items)
            if total_expected is not None and len(collected) >= total_expected:
                break
            if len(normalized_items) < self.config.page_size:
                break
            page += 1
            if page > 200:
                raise SystemExit(f"A02 pagination exceeded safety limit for boxCode={box_code}.")

        return collected

    def fetch_detail(self, *, doc_id: str, sp_doc_id: str = "") -> dict[str, Any]:
        payload = {
            "header": {
                "pId": "",
                "tId": "",
                "groupSeq": self.config.group_seq,
                "empSeq": self.config.emp_seq,
            },
            "body": {
                "docId": doc_id,
                "spDocId": sp_doc_id,
                "migYn": "0",
                "langCode": "kr",
                "spMigYn": "0",
                "companyInfo": {
                    "compSeq": self.config.comp_seq,
                    "deptSeq": self.config.dept_seq,
                    "bizSeq": self.config.biz_seq,
                },
            },
        }
        response = self.post_json(self.config.a04_url, payload, auth=self.resolve_read_auth())
        result_data = find_first_key(response, ("resultData",))
        if not isinstance(result_data, dict):
            raise SystemExit("A04 response did not include resultData.")
        return normalize_detail_item(result_data)


def build_config() -> ApprovalConfig:
    load_env_file(SYNC_DIR / "amaranth_approval.env")
    base_url = normalize_base_url(env_required("AMARANTH_BASE_URL"))
    return ApprovalConfig(
        base_url=base_url,
        caller_name=env_required("AMARANTH_CALLER_NAME"),
        group_seq=env_required("AMARANTH_GROUP_SEQ"),
        emp_seq=env_required("AMARANTH_EMP_SEQ"),
        comp_seq=env_required("AMARANTH_COMP_SEQ"),
        dept_seq=env_required("AMARANTH_DEPT_SEQ"),
        biz_seq=env_optional("AMARANTH_BIZ_SEQ", env_required("AMARANTH_COMP_SEQ")),
        login_id=env_required("AMARANTH_LOGIN_ID"),
        emp_name=env_optional("AMARANTH_EMP_NAME", env_required("AMARANTH_LOGIN_ID")),
        server_access_token=env_optional("AMARANTH_SERVER_ACCESS_TOKEN"),
        server_hash_key=env_optional("AMARANTH_SERVER_HASH_KEY"),
        sso_aes_key=env_optional("AMARANTH_SSO_AES_KEY"),
        direct_user_auth_token=env_optional("AMARANTH_USER_AUTH_TOKEN", env_optional("AMARANTH_BEARER_TOKEN")),
        direct_user_hash_key=env_optional("AMARANTH_USER_HASH_KEY", env_optional("AMARANTH_HASH_KEY")),
        lookback_days=max(1, env_int("APPROVAL_LOOKBACK_DAYS", 90)),
        page_size=max(1, env_int("APPROVAL_PAGE_SIZE", 30)),
        portal_public_base_url=normalize_base_url(
            env_optional("APPROVAL_PORTAL_PUBLIC_BASE_URL", "http://10.0.0.17:8000")
        ),
        request_timeout=max(5, env_int("AMARANTH_REQUEST_TIMEOUT_SECONDS", 30)),
    )


def map_menu_ids(box_list: list[dict[str, Any]]) -> dict[str, dict[str, str]]:
    mapped: dict[str, dict[str, str]] = {}
    for item in box_list:
        box_code = normalize_string(item.get("boxCode") or item.get("BOX_CODE"))
        if box_code not in ALLOWED_BOX_CODES:
            continue
        menu_id = first_non_empty(item.get("menuId"), item.get("MENU_ID"), item.get("menuID"))
        if not menu_id:
            continue
        mapped[box_code] = {
            "menuId": menu_id,
            "menuName": first_non_empty(item.get("menuName"), item.get("MENU_NAME"), SOURCE_CONFIG[box_code]["label"]),
            "boxCode": box_code,
        }
    return mapped


def normalize_rep_dt(value: Any) -> str:
    raw = normalize_string(value)
    if not raw:
        return ""
    for fmt_in, fmt_out in (
        ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%SZ"),
        ("%Y-%m-%d", "%Y-%m-%dT00:00:00Z"),
        ("%Y%m%d", "%Y-%m-%dT00:00:00Z"),
    ):
        try:
            parsed = datetime.strptime(raw, fmt_in)
            return parsed.replace(tzinfo=timezone.utc).strftime(fmt_out)
        except ValueError:
            continue
    return raw


def normalize_doc_item(item: dict[str, Any], *, box_code: str, menu_id: str) -> dict[str, Any]:
    source = SOURCE_CONFIG[box_code]
    doc_id = first_non_empty(item.get("docId"), item.get("DOC_ID"), item.get("docID"))
    doc_no = first_non_empty(item.get("docNo"), item.get("DOC_NO"))
    doc_title = first_non_empty(item.get("docTitle"), item.get("DOC_TITLE"), item.get("title"))
    doc_status = first_non_empty(item.get("docStsName"), item.get("DOC_STS_NAME"), item.get("docStsNm"), item.get("DOC_STSNM"))
    form_name = first_non_empty(item.get("formName"), item.get("FORM_NAME"), item.get("formNm"), item.get("FORM_NM"))
    created_by = first_non_empty(
        item.get("createdBy"),
        item.get("CREATED_BY"),
        item.get("createdNm"),
        item.get("CREATED_NM"),
        item.get("userNm"),
        item.get("USER_NM"),
        item.get("empName"),
        item.get("EMP_NAME"),
    )
    dept_name = first_non_empty(item.get("deptName"), item.get("DEPT_NAME"), item.get("deptNm"), item.get("DEPT_NM"))
    rep_dt = normalize_rep_dt(item.get("repDt") or item.get("REP_DT") or item.get("createdDt") or item.get("CREATED_DT"))
    read_yn = normalize_flag(item.get("readYN") or item.get("READYN") or item.get("readYn"))
    oper_yn = normalize_flag(item.get("operYN") or item.get("OPERYN") or item.get("operYn"))
    stable_key = doc_id or doc_no or sha256_hex(json_dumps(item))
    return {
        "id": f"{box_code}:{stable_key}",
        "sourceBoxCode": box_code,
        "sourceLabel": source["label"],
        "menuId": menu_id,
        "docId": doc_id,
        "docNo": doc_no,
        "docTitle": doc_title,
        "docStatus": doc_status,
        "formName": form_name,
        "createdBy": created_by,
        "deptName": dept_name,
        "repDt": rep_dt,
        "readYn": read_yn,
        "operYn": oper_yn,
        "needsAction": True,
        "needsActionReason": source["reason"],
    }


def filter_actionable_items(items: list[dict[str, Any]], *, box_code: str, menu_id: str) -> list[dict[str, Any]]:
    source = SOURCE_CONFIG[box_code]
    actionable: list[dict[str, Any]] = []
    for item in items:
        normalized = normalize_doc_item(item, box_code=box_code, menu_id=menu_id)
        if box_code == "60" and normalized["readYn"] != source["active_flag"]:
            continue
        if box_code == "120" and normalized["operYn"] != source["active_flag"]:
            continue
        actionable.append(normalized)
    return actionable


def normalize_file_list(file_list: Any) -> list[dict[str, str]]:
    normalized: list[dict[str, str]] = []
    if not isinstance(file_list, list):
        return normalized

    for item in file_list:
        if not isinstance(item, dict):
            continue
        normalized.append(
            {
                "fileId": first_non_empty(item.get("fileId"), item.get("FILE_ID")),
                "fileName": first_non_empty(
                    item.get("originalFileName"),
                    item.get("original_filename"),
                    item.get("fileName"),
                    item.get("FILE_NAME"),
                ),
                "fileExt": first_non_empty(item.get("fileExtsn"), item.get("fileExt"), item.get("FILE_EXT")),
                "fileSize": first_non_empty(item.get("fileSize"), item.get("FILE_SIZE")),
                "url": first_non_empty(item.get("url"), item.get("URL")),
            }
        )
    return normalized


def normalize_detail_item(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "docId": first_non_empty(item.get("docId"), item.get("DOC_ID")),
        "docNo": first_non_empty(item.get("docNo"), item.get("DOC_NO")),
        "docTitle": first_non_empty(item.get("docTitle"), item.get("DOC_TITLE")),
        "docStatus": first_non_empty(item.get("docStsName"), item.get("DOC_STS_NAME"), item.get("docStsNm"), item.get("DOC_STSNM")),
        "formName": first_non_empty(item.get("formName"), item.get("FORM_NAME"), item.get("formNm"), item.get("FORM_NM")),
        "createdBy": first_non_empty(item.get("empName"), item.get("EMP_NAME"), item.get("createdNm"), item.get("CREATED_NM")),
        "deptName": first_non_empty(item.get("deptName"), item.get("DEPT_NAME"), item.get("deptNm"), item.get("DEPT_NM")),
        "repDt": normalize_rep_dt(item.get("repDt") or item.get("REP_DT")),
        "createdDt": normalize_rep_dt(item.get("createdDt") or item.get("CREATED_DT")),
        "readYn": normalize_flag(item.get("readYN") or item.get("READYN") or item.get("readYn")),
        "operYn": normalize_flag(item.get("operYN") or item.get("OPERYN") or item.get("operYn")),
        "contents": first_non_empty(item.get("contents"), item.get("CONTENTS")),
        "lineName": first_non_empty(item.get("lineName"), item.get("LINE_NAME")),
        "dutyName": first_non_empty(item.get("dutyName"), item.get("DUTY_NAME")),
        "positionName": first_non_empty(item.get("positionName"), item.get("POSITION_NAME")),
        "commentCnt": first_non_empty(item.get("commentCnt"), item.get("COMMENT_CNT")),
        "attachCnt": first_non_empty(item.get("attachCnt"), item.get("ATTACH_CNT")),
        "spDocId": first_non_empty(item.get("spDocId"), item.get("SP_DOC_ID")),
        "actId": first_non_empty(item.get("actId"), item.get("ACT_ID")),
        "actIdC": first_non_empty(item.get("actIdC"), item.get("ACT_ID_C")),
        "docLineSeq": first_non_empty(item.get("docLineSeq"), item.get("DOC_LINE_SEQ")),
        "formId": first_non_empty(item.get("formId"), item.get("FORM_ID")),
        "btnList": item.get("btnList") if isinstance(item.get("btnList"), dict) else {},
        "fileList": normalize_file_list(item.get("fileList")),
    }


def sort_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        items,
        key=lambda item: (
            item.get("sourceBoxCode") != "60",
            item.get("repDt") or "",
            item.get("docId") or item.get("docNo") or "",
        ),
        reverse=True,
    )


def build_snapshot(config: ApprovalConfig, items: list[dict[str, Any]], boxes: dict[str, dict[str, str]]) -> dict[str, Any]:
    reference_count = sum(1 for item in items if item.get("sourceBoxCode") == "60")
    dispatch_count = sum(1 for item in items if item.get("sourceBoxCode") == "120")
    return {
        "owner": {
            "loginId": config.login_id,
            "empName": config.emp_name,
        },
        "generated_at_utc": utc_now().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "lastSyncedAt": utc_now().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "amaranth-official-api",
        "boxes": boxes,
        "counts": {
            "referenceUnread": reference_count,
            "dispatchPending": dispatch_count,
            "total": len(items),
        },
        "items": items,
    }


def write_snapshot(snapshot: dict[str, Any]) -> None:
    SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
    SNAPSHOT_PATH.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    config = build_config()
    client = ApprovalApiClient(config)
    box_list = client.fetch_box_list()
    boxes = map_menu_ids(box_list)

    missing = sorted(ALLOWED_BOX_CODES - set(boxes))
    if missing:
        raise SystemExit(f"Required approval boxes not found: {', '.join(missing)}")

    all_items: list[dict[str, Any]] = []
    for box_code in ("60", "120"):
        menu_id = boxes[box_code]["menuId"]
        page_items = client.fetch_box_items(menu_id=menu_id, box_code=box_code)
        all_items.extend(filter_actionable_items(page_items, box_code=box_code, menu_id=menu_id))

    deduped: dict[str, dict[str, Any]] = {}
    for item in all_items:
        deduped[item["id"]] = item

    sorted_items = sort_items(list(deduped.values()))
    snapshot = build_snapshot(config, sorted_items, boxes)
    write_snapshot(snapshot)
    print(
        json.dumps(
            {
                "ok": True,
                "path": str(SNAPSHOT_PATH),
                "referenceUnread": snapshot["counts"]["referenceUnread"],
                "dispatchPending": snapshot["counts"]["dispatchPending"],
                "total": snapshot["counts"]["total"],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
