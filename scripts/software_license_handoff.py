#!/usr/bin/env python3
"""Software license handoff (Phase 3, DRY-RUN).

Outlook/M365 라이선스 이메일 payload → 검토용 핸드오프 레코드(software-license-handoff.v1):
  - 소프트웨어명/키(마스킹)/주문번호/설치파일 추출
  - 소프트웨어 관리대장(software_ledger / Excel '라이선스 키' 시트)에 추가할 행 '제안'
  - 전자결재 캐시와 신청자 매칭(가능 시), Teams 초안(마스킹) 작성
설계: docs/software-license-handoff-automation.md

안전 기본값(엄수):
  - 항상 dry-run. Excel 워크북/Teams 전송/전체 키 노출 금지.
  - 전체 라이선스 키는 receipt/stdout/log 어디에도 남기지 않는다(마스킹만).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from infra_control.connectors.ledger_software import norm, mask_key  # 동일 정규화/마스킹 재사용
from infra_control.db import connect, init_db

SCHEMA_ID = "software-license-handoff.v1"
RECEIPT_DIR = ROOT / "var" / "receipts" / "software-license-handoff"
APPROVAL_THEME_HINTS = ("sw", "라이선스", "라이센스", "자산", "소프트웨어", "license")
REQUESTER_EMAIL_PATTERN = os.environ.get(
    "INFRA_CONTROL_REQUESTER_EMAIL_PATTERN",
    r"[\w.\-]+@example\.com",
)
# 설치파일 카탈로그(스펙 unresolved) — 알려진 것만, 없으면 빈값
INSTALL_FILE_CATALOG = {
    "hancom": "intranet://sw-repo/hancom/",
    "adobe": "intranet://sw-repo/adobe/",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def text(v: object) -> str:
    return "" if v is None else str(v)


_NAME_STOP = ("라이선스", "라이센스", "발급", "안내", "구매", "견적", "수량",
              "신청자", "인증번호", "가 ", "이 ", "을 ", "를 ", "은 ", "는 ")


def _trim_name(name: str) -> str:
    # 제품명 뒤에 붙은 한국어 문장 꼬리/조사 제거
    for w in _NAME_STOP:
        idx = name.find(w)
        if idx > 3:
            name = name[:idx]
    return name.strip(" -/·,.")


def extract_software_name(subject: str, body: str) -> str:
    blob = f"{subject}\n{body}"
    # 알려진 제품 키워드 우선 (경계는 _trim_name 으로 정리)
    for pat in (r"한컴오피스[^\n,()]*", r"한글\s?20\d\d[^\n,()]*", r"Adobe[^\n,()]*",
                r"Microsoft[^\n,()]*", r"TreeAge[^\n,()]*", r"EndNote[^\n,()]*",
                r"GitHub[^\n,()]*", r"Zoom[^\n,()]*", r"Figma[^\n,()]*", r"Docker[^\n,()]*"):
        m = re.search(pat, blob, re.IGNORECASE)
        if m:
            return _trim_name(m.group(0).strip())
    # 폴백: 제목에서 벤더 접두/주문번호 제거
    s = re.sub(r"^\[[^\]]*\]\s*", "", subject)
    s = re.sub(r"\(?\s*Order\s*#?\s*\d+\s*\)?", "", s, flags=re.IGNORECASE)
    return _trim_name(s.strip())


def extract_license_key(body: str) -> str:
    for pat in (r"(?:인증번호|라이선스\s*키|license\s*key|serial|activation)\s*[:：]\s*([^\s,\n]+)",
                r"\b([A-Z0-9]{4,5}(?:-[A-Z0-9]{4,5}){2,5})\b"):
        m = re.search(pat, body, re.IGNORECASE)
        if m:
            return m.group(1)
    return ""


def extract_qty(body: str) -> str:
    m = re.search(r"(?:수량|qty|seats?|copy)\s*[:：]?\s*(\d+)", body, re.IGNORECASE)
    if m:
        return m.group(1)
    m = re.search(r"(\d+)\s*(?:copy|seats?|개|카피)", body, re.IGNORECASE)
    return m.group(1) if m else ""


def extract_order_id(subject: str, body: str) -> str:
    m = re.search(r"(?:order|주문번호|주문)\s*#?\s*[:：]?\s*(\w+)", f"{subject}\n{body}", re.IGNORECASE)
    return m.group(1) if m else ""


def extract_requester(body: str) -> dict[str, str]:
    upn = ""
    m = re.search(REQUESTER_EMAIL_PATTERN, body)
    if m:
        upn = m.group(0)
    name = ""
    m = re.search(r"신청자\s*[:：]?\s*([^\s(,\n]+)", body)
    if m:
        name = m.group(1)
    return {"name": name, "upn": upn}


def install_file_ref(software_name: str) -> str:
    nl = software_name.lower()
    for key, ref in INSTALL_FILE_CATALOG.items():
        if key in nl or key in norm(software_name):
            return ref
    return ""


def match_approval(software_name: str, requester: dict[str, str]) -> dict[str, object]:
    """전자결재 캐시와 best-effort 매칭. 데이터 없으면 review-needed(low)."""
    result = {"doc_id": "", "doc_title": "", "requester_name": requester.get("name", ""),
              "requester_upn": requester.get("upn", ""), "confidence": "low", "evidence": []}
    try:
        from server import enriched_approval_items, load_approval_details  # type: ignore
    except Exception:
        result["evidence"].append("approval cache unavailable in this environment")
        return result
    try:
        details = load_approval_details().get("items", {})
        sw_tokens = {t for t in norm(software_name).split() if len(t) > 1}
        best = None
        best_score = 0.0
        for item in enriched_approval_items():
            doc_id = text(item.get("docId"))
            detail = details.get(doc_id, {}) if isinstance(details, dict) else {}
            blob = " ".join([text(item.get("docTitle")), text(detail.get("contentsText"))]).lower()
            if not any(h in blob for h in APPROVAL_THEME_HINTS):
                continue
            ct = {t for t in norm(blob).split() if len(t) > 1}
            inter = sw_tokens & ct
            score = len(inter) / max(1, len(sw_tokens)) if sw_tokens else 0.0
            if requester.get("upn") and requester["upn"].lower() in blob:
                score += 0.3
            if score > best_score:
                best_score, best = score, (item, detail, inter)
        if best:
            item, detail, inter = best
            result.update({
                "doc_id": text(item.get("docId")),
                "doc_title": text(item.get("docTitle")),
                "requester_name": requester.get("name") or text(item.get("createdBy")),
                "confidence": "high" if best_score >= 0.7 else ("medium" if best_score >= 0.4 else "low"),
                "evidence": [f"shared_tokens={sorted(inter)}", f"score={round(best_score, 2)}"],
            })
    except Exception as exc:  # pragma: no cover
        result["evidence"].append(f"match error: {exc}")
    return result


def next_license_seq(db_path: Path | None) -> int:
    try:
        cm = connect(db_path) if db_path else connect()
        with cm as conn:
            row = conn.execute("SELECT COUNT(*) FROM software_ledger").fetchone()
            return int(row[0]) + 1
    except Exception:
        return 1


def build_handoff(payload: dict[str, object], workbook: str, db_path: Path | None = None) -> dict[str, object]:
    subject = text(payload.get("subject"))
    body = text(payload.get("body"))
    software_name = extract_software_name(subject, body)
    raw_key = extract_license_key(body)
    requester = extract_requester(body)
    attachments = payload.get("attachments") if isinstance(payload.get("attachments"), list) else []
    first_attach = text(attachments[0].get("name")) if attachments and isinstance(attachments[0], dict) else ""

    proposed_row = {
        "소프트웨어": software_name,
        "발급/활성일": text(payload.get("received_at"))[:10],
        "수량": extract_qty(body),
        "식별자 유형": "Activation/Serial Code" if raw_key else "",
        "값": "<<APPROVED-WRITE-ONLY>>",   # 전체 키는 승인 실행 시점에만 워크북 셀에
        "비고": f"Order #{extract_order_id(subject, body)} / {requester.get('upn','')} / 자동 핸드오프(dry-run)",
    }

    receipt = {
        "schema": SCHEMA_ID,
        "mode": "dry-run",
        "detected_at": utc_now(),
        "mail": {
            "message_id": text(payload.get("message_id")),
            "subject": subject,
            "from": text(payload.get("from")),
            "received_at": text(payload.get("received_at")),
        },
        "license": {
            "software_name": software_name,
            "norm_name": norm(software_name),
            "license_key_masked": mask_key(raw_key),
            "license_key_secret_ref": "vault://pending" if raw_key else "",
            "order_id": extract_order_id(subject, body),
            "install_file_ref": install_file_ref(software_name),
        },
        "excel_append": {
            "workbook": workbook,
            "worksheet": "라이선스 키",
            "next_asset_no": f"SW-{next_license_seq(db_path):04d}",
            "row": proposed_row,
        },
        "approval_match": match_approval(software_name, requester),
        "teams_draft": {
            "recipient": requester.get("upn", ""),
            "message_preview": (
                f"[{software_name}] 라이선스가 발급되었습니다. 키: {mask_key(raw_key)} "
                f"(설치파일: {install_file_ref(software_name) or 'TBD'}). 상세는 승인 후 안내."
            ),
            "requires_operator_approval": True,
        },
    }
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample", default=str(ROOT / "var" / "samples" / "license_email_sample.json"))
    parser.add_argument("--stdin", action="store_true")
    parser.add_argument("--db", default=None)
    parser.add_argument("--workbook", default="ACME_software_purchases.xlsx")
    parser.add_argument("--approve", action="store_true")
    args = parser.parse_args()

    if args.approve:
        print(json.dumps({"status": "refused",
                          "reason": "approved execution(엑셀/Teams 쓰기)은 미구현 — 안전 기본값(dry-run)만 지원"},
                         ensure_ascii=False))
        return 2

    payload = json.load(sys.stdin) if args.stdin else json.loads(Path(args.sample).read_text(encoding="utf-8"))
    db_path = Path(args.db) if args.db else None
    if db_path:
        init_db(db_path)
    receipt = build_handoff(payload, args.workbook, db_path)

    RECEIPT_DIR.mkdir(parents=True, exist_ok=True)
    out = RECEIPT_DIR / f"handoff-{text(payload.get('message_id')) or 'sample'}.json"
    out.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(receipt, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
