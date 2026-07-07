from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from infra_control.db import connect, encode_json, init_db


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_EXCEL_DIR = Path(os.environ.get("INFRA_CONTROL_EXCEL_DIR", str(ROOT / "var" / "imports" / "local-excel")))
REQUIRED_EXCEL_SOURCE_NAMES = (
    "통합 본사 정보자산 관리대장.xlsx",
    "강남 IDC 정보자산 관리대장.xlsx",
    "강남 IDC 상면도 및 실장도.xlsx",
    "infra-assets.csv",
)
DEFAULT_DOWNLOAD_DIRS = (
    Path.home() / "Downloads",
    Path("/opt/infra-asset-manager/var/imports/ledger"),
)
DEFAULT_DOWNLOADED_EXCEL_FILES = tuple(
    directory / name for directory in DEFAULT_DOWNLOAD_DIRS for name in REQUIRED_EXCEL_SOURCE_NAMES
)
NS = {
    "a": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
}
KNOWN_HEADER_TERMS = (
    "자산관리번호",
    "자산명",
    "IP주소",
    "호스트명",
    "자산위치",
    "담당자",
    "관리부서",
    "시리얼",
    "모델명",
    "구분",
    "위치",
    "상면",
    "포트",
)
SECRET_HEADER_TERMS = ("계정", "password", "passwd", "pw", "비밀번호", "마스터", "token", "secret")
TERMINAL_UNUSED_TERMS = ("폐기", "불용", "매각", "처분", "분실", "수리", "반납완료")
IDLE_TERMS = ("재고", "유휴", "stock", "idle", "미사용", "반납", "입력x")
ACTIVE_TERMS = ("사용중", "사용 중", "정상", "운영", "active", "in use")
RETIRED_USAGE_STATUS = "종료/제외"
LAPTOP_TERMS = ("노트북", "notebook", "laptop", "macbook", "elitebook", "thinkpad", "surface", "gram")
SERVER_TERMS = ("서버", "가상서버", "vmware", "proxmox", "gpu", "idrac", "ilo", "ipmi")
NETWORK_TERMS = ("firewall", "switch", "방화벽", "스위치", "authentication", "nac", "네트워크", "router")
USAGE_REASON_PRIORITY = {
    "명시적 제외 상태": 100,
    "NAC+Intune 사용 근거": 95,
    "NAC 사용 근거": 94,
    "Intune 사용 근거": 94,
    "대여/checkout 배정 근거": 92,
    "통합 인프라 사용 근거": 90,
    "infra-assets 사용자 배정": 80,
    "infra-assets 재고/유휴": 70,
    "예외 키워드 기준": 70,
    "담당자 존재": 60,
    "노트북 기본 사용중 판정": 50,
    "NAC/Intune 근거 없음": 20,
    "판정 근거 부족": 10,
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def stable_id(*parts: str) -> str:
    raw = "|".join(part or "" for part in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def norm(value: Any) -> str:
    return str(value or "").strip()


def can_read(path: Path) -> bool:
    try:
        return path.is_file() and os.access(path, os.R_OK)
    except OSError:
        return False


def configured_excel_files() -> list[Path]:
    files: list[Path] = []
    raw = os.environ.get("INFRA_CONTROL_EXCEL_FILES", "")
    for item in raw.split(os.pathsep):
        if item.strip():
            files.append(Path(item.strip()))
    files.extend(DEFAULT_DOWNLOADED_EXCEL_FILES)
    return files


def unique_files(paths: list[Path]) -> list[Path]:
    seen: set[str] = set()
    result: list[Path] = []
    for path in paths:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        if can_read(path):
            result.append(path)
    return result


def compact(value: Any) -> str:
    return re.sub(r"\s+", "", norm(value).lower())


def text_of(node: ET.Element) -> str:
    return "".join(t.text or "" for t in node.findall(".//a:t", NS))


def col_index(ref: str) -> int:
    letters = re.match(r"([A-Z]+)", ref or "")
    if not letters:
        return 0
    total = 0
    for char in letters.group(1):
        total = total * 26 + ord(char) - ord("A") + 1
    return total - 1


def xlsx_sheets(path: Path) -> dict[str, list[list[str]]]:
    result: dict[str, list[list[str]]] = {}
    with zipfile.ZipFile(path) as zf:
        shared: list[str] = []
        if "xl/sharedStrings.xml" in zf.namelist():
            root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
            shared = [text_of(item) for item in root.findall("a:si", NS)]
        workbook = ET.fromstring(zf.read("xl/workbook.xml"))
        rels = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
        relmap = {rel.attrib["Id"]: rel.attrib["Target"] for rel in rels}
        for sheet in workbook.findall("a:sheets/a:sheet", NS):
            name = sheet.attrib.get("name", "Sheet")
            rid = sheet.attrib.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id", "")
            target = relmap.get(rid, "")
            sheet_path = "xl/" + target.lstrip("/") if not target.startswith("xl/") else target
            if sheet_path not in zf.namelist():
                continue
            root = ET.fromstring(zf.read(sheet_path))
            rows: list[list[str]] = []
            for row in root.findall("a:sheetData/a:row", NS):
                values: list[str] = []
                for cell in row.findall("a:c", NS):
                    idx = col_index(cell.attrib.get("r", ""))
                    while len(values) <= idx:
                        values.append("")
                    value = ""
                    node = cell.find("a:v", NS)
                    if node is not None:
                        value = node.text or ""
                        if cell.attrib.get("t") == "s":
                            try:
                                value = shared[int(value)]
                            except Exception:
                                value = ""
                    elif cell.attrib.get("t") == "inlineStr":
                        value = text_of(cell)
                    values[idx] = norm(value)
                if any(values):
                    rows.append(values)
            result[name] = rows
    return result


def csv_rows(path: Path) -> dict[str, list[list[str]]]:
    with path.open(newline="", encoding="utf-8-sig", errors="replace") as handle:
        return {path.stem: list(csv.reader(handle))}


def find_header(rows: list[list[str]]) -> tuple[int, list[str]] | None:
    for index, row in enumerate(rows[:20]):
        score = sum(1 for value in row if any(term in norm(value) for term in KNOWN_HEADER_TERMS))
        if score >= 2:
            return index, [norm(value) for value in row]
    return None


def value(row: dict[str, str], *aliases: str) -> str:
    compacted = {compact(key): val for key, val in row.items()}
    for alias in aliases:
        needle = compact(alias)
        if needle in compacted and compacted[needle]:
            return norm(compacted[needle])
    for key, val in compacted.items():
        if any(compact(alias) in key for alias in aliases) and norm(val):
            return norm(val)
    return ""


def sanitize_row(row: dict[str, str]) -> dict[str, str]:
    clean: dict[str, str] = {}
    for key, val in row.items():
        if any(term in compact(key) for term in SECRET_HEADER_TERMS):
            continue
        if any(term in compact(val) for term in SECRET_HEADER_TERMS):
            continue
        clean[key] = val
    return clean


def parse_location(location: str) -> tuple[str, str, str]:
    text = norm(location)
    if not text:
        return "", "", ""
    parts = re.split(r"[-/\s]+", text)
    room = parts[0] if parts else text
    rack = parts[1] if len(parts) > 1 else ""
    rack_unit = parts[2] if len(parts) > 2 else ""
    return room, rack, rack_unit


def infer_flags(sheet: str, category: str, row: dict[str, str]) -> tuple[int, int, int]:
    text = " ".join([sheet, category, value(row, "자산명"), value(row, "설명"), value(row, "사용목적"), value(row, "모델명")]).lower()
    is_virtual = int("가상서버" in text or "vmware" in text or "proxmox" in text or "vm-" in text)
    is_network = int(any(term in text for term in NETWORK_TERMS))
    is_server = int(is_virtual or any(term in text for term in SERVER_TERMS))
    return is_server, is_virtual, is_network


def has_term(text: str, terms: tuple[str, ...]) -> bool:
    return any(term.lower() in text for term in terms)


def has_active_usage_evidence(row: dict[str, str], is_server: int, is_network: int) -> bool:
    status = " ".join(
        value(row, alias)
        for alias in (
            "상태",
            "운영상태",
            "사용상태",
            "운영 상태",
            "status",
            "operational_status",
        )
    ).lower()
    return bool(
        has_term(status, ACTIVE_TERMS)
        or value(row, "IP주소", "IP", "ip_address")
        or value(row, "포트", "port")
        or is_server
        or is_network
    )


def infer_usage(category: str, row: dict[str, str], is_server: int, is_network: int) -> tuple[str, str]:
    text = " ".join(row.values()).lower()
    if has_term(text, TERMINAL_UNUSED_TERMS):
        return RETIRED_USAGE_STATUS, "명시적 제외 상태"
    if has_active_usage_evidence(row, is_server, is_network):
        return "사용중", "통합 인프라 사용 근거"
    if has_term(text, IDLE_TERMS):
        return "미사용", "예외 키워드 기준"
    if has_term(text, LAPTOP_TERMS):
        return "사용중", "노트북 기본 사용중 판정"
    if value(row, "담당자", "사용자", "owner"):
        return "사용중", "담당자 존재"
    return "확인필요", "판정 근거 부족"


def usage_priority(reason: Any) -> int:
    return USAGE_REASON_PRIORITY.get(norm(reason), 0)


def should_replace_usage(existing: dict[str, Any], record: dict[str, Any]) -> bool:
    if not norm(record.get("usage_status")) or record.get("usage_status") == "확인필요":
        return False
    if not norm(existing.get("usage_status")):
        return True
    return usage_priority(record.get("usage_reason")) >= usage_priority(existing.get("usage_reason"))


def decoded_metadata(record: dict[str, Any]) -> dict[str, Any]:
    try:
        metadata = json.loads(norm(record.get("metadata_json")) or "{}")
    except json.JSONDecodeError:
        metadata = {}
    return metadata if isinstance(metadata, dict) else {}


def merged_metadata(existing: dict[str, Any], record: dict[str, Any]) -> str:
    existing_metadata = decoded_metadata(existing)
    record_metadata = decoded_metadata(record)
    for key in ("usage_evidence", "usage_basis", "manual_overrides"):
        if existing_metadata.get(key) and not record_metadata.get(key):
            record_metadata[key] = existing_metadata[key]
    if existing_metadata.get("source_row") and not record_metadata.get("usage_source_row"):
        record_metadata["usage_source_row"] = existing_metadata["source_row"]
    if existing_metadata.get("intune_last_sync_utc") and not record_metadata.get("intune_last_sync_utc"):
        record_metadata["intune_last_sync_utc"] = existing_metadata["intune_last_sync_utc"]
    if existing_metadata.get("nac_last_seen_utc") and not record_metadata.get("nac_last_seen_utc"):
        record_metadata["nac_last_seen_utc"] = existing_metadata["nac_last_seen_utc"]
    return encode_json(record_metadata)


def merge_asset(existing: dict[str, Any] | None, record: dict[str, Any], now: str) -> dict[str, Any]:
    if not existing:
        return {**record, "source": "excel-import", "external_id": record["asset_tag"], "updated_at": now}

    merged = dict(existing)
    replace_if_present = [
        "hostname",
        "serial",
        "model",
        "manufacturer",
        "category",
        "status",
        "owner",
        "owner_email",
        "department",
        "primary_ip",
        "os",
        "location",
        "room",
        "rack",
        "rack_unit",
        "maintenance_date",
        "purpose",
        "ports",
    ]
    manual_overrides = decoded_metadata(existing).get("manual_overrides")
    if not isinstance(manual_overrides, dict):
        manual_overrides = {}
    for key in replace_if_present:
        if norm(record.get(key)) and key not in manual_overrides:
            merged[key] = record[key]
    if should_replace_usage(existing, record) and "usage_status" not in manual_overrides:
        merged["usage_status"] = record["usage_status"]
        merged["usage_reason"] = record["usage_reason"]
    for key in ("is_server", "is_virtual", "is_network"):
        merged[key] = int(bool(existing.get(key)) or bool(record.get(key)))
    merged["source"] = "excel-import"
    merged["external_id"] = record["asset_tag"]
    merged["asset_tag"] = record["asset_tag"]
    merged["metadata_json"] = merged_metadata(existing, record)
    merged["updated_at"] = now
    return merged


def normalize_flat_issue_export(path: Path) -> list[dict[str, Any]]:
    lines = [norm(line) for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines()]
    records: list[dict[str, Any]] = []
    category = "asset"
    index = 0
    while index < len(lines):
        line = lines[index]
        group = re.match(r"(.+?)(\d+)\s+이슈$", line)
        if group:
            category = group.group(1).strip() or category
            index += 1
            continue
        if not re.match(r"^ITAM-\d+$", line):
            index += 1
            continue

        itam_key = line
        asset_line = lines[index + 1] if index + 1 < len(lines) else ""
        match = re.match(r"^(ACME-[A-Z0-9-]+)\s*(?:/\s*(.*))?$", asset_line)
        if not match:
            index += 1
            continue

        asset_tag = match.group(1)
        owner = norm(match.group(2) or "")
        manufacturer = lines[index + 2] if index + 2 < len(lines) else ""
        purchase_date = lines[index + 3] if index + 3 < len(lines) else ""
        serial = lines[index + 4] if index + 4 < len(lines) else ""
        usage_status = "사용중" if owner else "미사용"
        usage_reason = "infra-assets 사용자 배정" if owner else "infra-assets 재고/유휴"
        status = "사용중" if owner else "재고/유휴"
        records.append(
            {
                "source_file": path.name,
                "sheet": "flat-issue-export",
                "asset_tag": asset_tag,
                "hostname": "",
                "serial": serial,
                "model": "",
                "manufacturer": manufacturer,
                "category": category,
                "status": status,
                "owner": owner,
                "owner_email": "",
                "department": "",
                "primary_ip": "",
                "os": "",
                "location": "",
                "room": "",
                "rack": "",
                "rack_unit": "",
                "maintenance_date": "",
                "usage_status": usage_status,
                "usage_reason": usage_reason,
                "purpose": "ITAM flat export",
                "ports": "",
                "is_server": 0,
                "is_virtual": 0,
                "is_network": 0,
                "metadata_json": encode_json(
                    {
                        "source_file": path.name,
                        "sheet": "flat-issue-export",
                        "source_row": {
                            "itam_key": itam_key,
                            "asset_line": asset_line,
                            "manufacturer": manufacturer,
                            "purchase_date": purchase_date,
                            "serial": serial,
                            "category": category,
                        },
                    }
                ),
            }
        )
        index += 5
    return records


def normalize_records(path: Path) -> list[dict[str, Any]]:
    sheets = xlsx_sheets(path) if path.suffix.lower() == ".xlsx" else csv_rows(path)
    records: list[dict[str, Any]] = []
    for sheet_name, rows in sheets.items():
        header_info = find_header(rows)
        if not header_info:
            continue
        header_index, headers = header_info
        for raw in rows[header_index + 1 :]:
            if not any(norm(value) for value in raw):
                continue
            row = {headers[idx] if idx < len(headers) and headers[idx] else f"column_{idx+1}": norm(raw[idx]) for idx in range(len(raw))}
            asset_tag = value(row, "자산관리번호", "asset_tag", "자산번호")
            ip = value(row, "IP주소", "IP", "ip_address")
            hostname = value(row, "호스트명", "hostname", "장비명")
            model = value(row, "모델명", "모델", "제조사 모델명")
            serial = value(row, "시리얼", "일련번호", "serial", "S/N")
            if not asset_tag and not ip and not hostname and not serial:
                continue
            category = value(row, "구분", "행 레이블", "분류", "품목") or "asset"
            location = value(row, "자산위치", "위치", "상면", "랙")
            room, rack, rack_unit = parse_location(location)
            owner = value(row, "담당자", "사용자/담당자", "사용자", "owner")
            department = value(row, "관리부서", "부서", "department")
            ports = value(row, "포트", "port")
            is_server, is_virtual, is_network = infer_flags(sheet_name, category, row)
            usage_status, usage_reason = infer_usage(category, row, is_server, is_network)
            records.append(
                {
                    "source_file": path.name,
                    "sheet": sheet_name,
                    "asset_tag": asset_tag or stable_id(path.name, sheet_name, hostname, ip, serial),
                    "hostname": hostname,
                    "serial": serial,
                    "model": model,
                    "manufacturer": value(row, "제조사", "manufacturer"),
                    "category": category,
                    "status": value(row, "상태", "운영상태", "ledger_status") or usage_status,
                    "owner": owner,
                    "owner_email": "",
                    "department": department,
                    "primary_ip": ip,
                    "os": " ".join(part for part in [value(row, "운영체제", "OS"), value(row, "버전")] if part),
                    "location": location,
                    "room": room,
                    "rack": rack,
                    "rack_unit": rack_unit,
                    "maintenance_date": value(row, "유지보수", "점검일", "만료일", "유지보수일"),
                    "usage_status": usage_status,
                    "usage_reason": usage_reason,
                    "purpose": value(row, "사용목적", "상세 용도", "설명", "용도", "비고"),
                    "ports": ports,
                    "is_server": is_server,
                    "is_virtual": is_virtual,
                    "is_network": is_network,
                    "metadata_json": encode_json({"source_row": sanitize_row(row), "source_file": path.name, "sheet": sheet_name}),
                }
            )
    if not records and path.suffix.lower() == ".csv":
        return normalize_flat_issue_export(path)
    return records


def import_excel_assets(import_dir: Path = DEFAULT_EXCEL_DIR, extra_files: list[Path] | None = None) -> dict[str, Any]:
    init_db()
    now = utc_now()
    files = unique_files(
        [
            *sorted(import_dir.glob("*.xlsx")),
            *sorted(import_dir.glob("*.csv")),
            *(extra_files if extra_files is not None else configured_excel_files()),
        ]
    )
    imported = 0
    locations = 0
    written_ids: set[str] = set()
    written_tags: set[str] = set()
    with connect() as conn:
        for path in files:
            records = normalize_records(path)
            conn.execute(
                """
                INSERT OR REPLACE INTO integration_sources
                (id, name, kind, endpoint, status, last_sync_at, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    stable_id("excel-source", path.name),
                    path.name,
                    "excel-import",
                    str(path),
                    "imported",
                    now,
                    encode_json({"record_count": len(records)}),
                ),
            )
            for record in records:
                existing = conn.execute(
                    "SELECT * FROM assets WHERE asset_tag = ? ORDER BY source LIMIT 1",
                    (record["asset_tag"],),
                ).fetchone()
                asset_id = existing["id"] if existing else stable_id("excel-asset", record["asset_tag"], record["source_file"])
                written_ids.add(asset_id)
                written_tags.add(record["asset_tag"])
                merged = merge_asset(dict(existing) if existing else None, record, now)
                conn.execute(
                    """
                    INSERT OR REPLACE INTO assets
                    (id, source, external_id, asset_tag, hostname, serial, model, manufacturer, category,
                     status, owner, owner_email, department, primary_ip, os, location, room, rack, rack_unit,
                     maintenance_date, usage_status, usage_reason, purpose, ports, is_server, is_virtual,
                     is_network, metadata_json, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        asset_id,
                        merged["source"],
                        merged["external_id"],
                        merged["asset_tag"],
                        merged["hostname"],
                        merged["serial"],
                        merged["model"],
                        merged["manufacturer"],
                        merged["category"],
                        merged["status"],
                        merged["owner"],
                        merged["owner_email"],
                        merged["department"],
                        merged["primary_ip"],
                        merged["os"],
                        merged["location"],
                        merged["room"],
                        merged["rack"],
                        merged["rack_unit"],
                        merged["maintenance_date"],
                        merged["usage_status"],
                        merged["usage_reason"],
                        merged["purpose"],
                        merged["ports"],
                        merged["is_server"],
                        merged["is_virtual"],
                        merged["is_network"],
                        merged["metadata_json"],
                        merged["updated_at"],
                    ),
                )
                imported += 1
                if record["room"] or record["rack"]:
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO locations
                        (id, room, rack, rack_unit, asset_tag, label, source, metadata_json)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            stable_id("location", record["asset_tag"], record["location"]),
                            record["room"] or "미지정",
                            record["rack"],
                            record["rack_unit"],
                            record["asset_tag"],
                            record["hostname"] or record["asset_tag"],
                            "excel-import",
                            encode_json({"source_file": record["source_file"], "sheet": record["sheet"]}),
                        ),
                    )
                    locations += 1
                if record["maintenance_date"]:
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO maintenance_events
                        (id, asset_tag, event_date, vendor, event_type, status, note, source, metadata_json)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            stable_id("maintenance", record["asset_tag"], record["maintenance_date"]),
                            record["asset_tag"],
                            record["maintenance_date"],
                            "",
                            "maintenance",
                            "scheduled",
                            record["purpose"],
                            "excel-import",
                            encode_json({"source_file": record["source_file"], "sheet": record["sheet"]}),
                        ),
                    )
        if files and written_ids:
            placeholders = ",".join("?" for _ in written_ids)
            conn.execute(
                f"DELETE FROM assets WHERE source = 'excel-import' AND id NOT IN ({placeholders})",
                tuple(written_ids),
            )
        if files and written_tags:
            tag_placeholders = ",".join("?" for _ in written_tags)
            conn.execute(
                f"DELETE FROM locations WHERE source = 'excel-import' AND asset_tag NOT IN ({tag_placeholders})",
                tuple(written_tags),
            )
            conn.execute(
                f"DELETE FROM maintenance_events WHERE source = 'excel-import' AND asset_tag NOT IN ({tag_placeholders})",
                tuple(written_tags),
            )
    return {
        "files": [path.name for path in files],
        "imported_records": imported,
        "locations": locations,
        "synced_at": now,
    }


if __name__ == "__main__":
    print(json.dumps(import_excel_assets(), ensure_ascii=False, indent=2))
