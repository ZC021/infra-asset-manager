#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any


KST = timezone(timedelta(hours=9), "KST")
DEFAULT_BASE_URL = "http://127.0.0.1:8000"


def kst_now() -> str:
    return datetime.now(KST).isoformat()


def get_json(base_url: str, path: str) -> Any:
    with urllib.request.urlopen(base_url.rstrip("/") + path, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def severity_rank(value: str) -> int:
    return {"info": 0, "warn": 1, "high": 2}.get(value, 0)


def add_finding(
    findings: list[dict[str, Any]],
    *,
    finding_id: str,
    severity: str,
    question: str,
    evidence: dict[str, Any],
    expected: str,
    next_action: str,
) -> None:
    findings.append(
        {
            "id": finding_id,
            "severity": severity,
            "question": question,
            "evidence": evidence,
            "expected": expected,
            "next_action": next_action,
        }
    )


def jira_sources(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for source in sources:
        blob = " ".join(str(source.get(key) or "") for key in ["id", "name", "kind", "endpoint"]).lower()
        if "jira" in blob:
            rows.append(source)
    return rows


def build_findings(payload: dict[str, Any]) -> list[dict[str, Any]]:
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    quality = payload.get("quality") if isinstance(payload.get("quality"), dict) else {}
    counts = quality.get("counts") if isinstance(quality.get("counts"), dict) else {}
    assets = payload.get("assets") if isinstance(payload.get("assets"), list) else []
    changes = payload.get("changes") if isinstance(payload.get("changes"), list) else []
    sources = payload.get("sources") if isinstance(payload.get("sources"), list) else []
    findings: list[dict[str, Any]] = []

    total_assets = int(summary.get("assets") or counts.get("assets") or 0)
    asset_limit = int(counts.get("asset_api_limit") or 0)
    if total_assets and len(assets) < total_assets:
        add_finding(
            findings,
            finding_id="asset_api_partial",
            severity="high",
            question="왜 자산 목록 API가 전체 자산을 반환하지 않고 일부만 보여주고 있나?",
            evidence={"returned": len(assets), "total": total_assets, "asset_api_limit": asset_limit},
            expected="현재 규모의 자산 목록은 기본 화면/API에서 전체가 보여야 한다.",
            next_action="기본 자산 조회 제한을 제거하거나 명시적 페이지네이션과 전체 건수 UI를 제공한다.",
        )

    total_changes = int(counts.get("changes") or 0)
    change_limit = int(counts.get("change_api_limit") or 0)
    if total_changes and len(changes) < total_changes:
        add_finding(
            findings,
            finding_id="change_history_partial",
            severity="warn",
            question="왜 변경 이력은 전체 맥락 없이 최신 일부만 보여주고 있나?",
            evidence={"returned": len(changes), "total": total_changes, "change_api_limit": change_limit},
            expected="변경 이력은 최신 일부를 보여주더라도 필터, offset, 전체 건수, 원인별 집계가 같이 있어야 한다.",
            next_action="변경 이력 페이지에 limit/offset, 자산/field/source 필터, 반복 변경 집계를 붙인다.",
        )

    repeated_jira = int(counts.get("repeated_jira_today") or 0)
    if repeated_jira:
        add_finding(
            findings,
            finding_id="jira_repeated_changes",
            severity="high",
            question="왜 read-only여야 할 Jira가 오늘도 반복 변경 이력을 만들고 있나?",
            evidence={"repeated_jira_today": repeated_jira},
            expected="Jira는 자산 필드 변경 원천이 아니라 비교/표시용 read-only 소스여야 한다.",
            next_action="Jira 동기화가 자산 필드를 쓰지 않는지 검증하고, 반복 이력은 원인별로 정리한다.",
        )

    writable_jira_sources = [source for source in jira_sources(sources) if source.get("status") != "read-only"]
    if writable_jira_sources:
        add_finding(
            findings,
            finding_id="jira_source_not_read_only",
            severity="high",
            question="왜 Jira 계열 연동 상태가 read-only가 아닌가?",
            evidence={"jira_sources": writable_jira_sources},
            expected="Jira 계열 source status는 모두 read-only여야 한다.",
            next_action="Jira integration source status와 receipt status를 read-only로 고정한다.",
        )

    stale_sources = int(counts.get("stale_sources") or 0)
    if stale_sources:
        add_finding(
            findings,
            finding_id="stale_sources",
            severity="warn",
            question="왜 오래 갱신되지 않은 연동 소스가 운영 화면에 그대로 남아 있나?",
            evidence={"stale_sources": stale_sources},
            expected="운영 판단에 쓰는 소스는 최신성 기준과 담당 조치가 명확해야 한다.",
            next_action="stale source별 마지막 동기화 시각, 장애 원인, 소유자를 표시한다.",
        )

    duplicate_asset_tags = int(counts.get("duplicate_asset_tags") or 0)
    if duplicate_asset_tags:
        add_finding(
            findings,
            finding_id="duplicate_asset_tags",
            severity="high",
            question="왜 같은 자산번호가 여러 레코드에 존재하나?",
            evidence={"duplicate_asset_tags": duplicate_asset_tags},
            expected="자산번호는 운영자가 클릭했을 때 단일 판단 대상으로 귀결되어야 한다.",
            next_action="중복 자산번호의 source별 충돌 내용을 보여주고 우선순위 병합 규칙을 검증한다.",
        )

    missing_owner = int(counts.get("missing_owner") or 0)
    if total_assets and missing_owner / total_assets >= 0.2:
        add_finding(
            findings,
            finding_id="missing_owner_high",
            severity="warn",
            question="왜 담당자 미지정 자산 비율이 높은가?",
            evidence={"missing_owner": missing_owner, "total_assets": total_assets, "ratio": round(missing_owner / total_assets, 3)},
            expected="담당자 공백은 운영 조치가 필요한 큐로 분리되어야 한다.",
            next_action="미지정 자산을 분류별/소스별로 묶고, 실제 재고인지 사용자 누락인지 구분한다.",
        )

    review_assets = int(counts.get("review_assets") or 0)
    if review_assets:
        add_finding(
            findings,
            finding_id="review_assets_present",
            severity="warn",
            question="왜 확인필요 자산이 운영 대기열로 남아 있나?",
            evidence={"review_assets": review_assets, "jira_nac_conflicts": int(counts.get("jira_nac_conflicts") or 0)},
            expected="확인필요는 원인별로 분류되고 다음 조치가 보여야 한다.",
            next_action="Jira/NAC 충돌, 담당자 누락, 소스 stale 등 원인별 액션 큐를 분리한다.",
        )

    return sorted(findings, key=lambda item: (-severity_rank(str(item.get("severity"))), str(item.get("id"))))


def collect_payload(base_url: str) -> dict[str, Any]:
    return {
        "summary": get_json(base_url, "/api/summary"),
        "quality": get_json(base_url, "/api/data-quality"),
        "sources": get_json(base_url, "/api/sources"),
        "assets": get_json(base_url, "/api/assets"),
        "changes": get_json(base_url, "/api/changes"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Ask strict why-questions about infra-control asset data and UI/API limits.")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--fail-on", choices=["none", "warn", "high"], default="none")
    args = parser.parse_args()

    payload = collect_payload(args.base_url)
    findings = build_findings(payload)
    result = {
        "checked_at": kst_now(),
        "base_url": args.base_url,
        "passed": not findings,
        "finding_count": len(findings),
        "findings": findings,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    threshold = severity_rank(args.fail_on)
    if threshold and any(severity_rank(str(item.get("severity"))) >= threshold for item in findings):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
