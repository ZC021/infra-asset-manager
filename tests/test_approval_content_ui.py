from __future__ import annotations

import json
from pathlib import Path
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
APP_JS = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
STYLES = (ROOT / "static" / "styles.css").read_text(encoding="utf-8")


def run_approval_js(expression: str):
    start = APP_JS.index("const html =")
    end = APP_JS.index("\nfunction captureScroll")
    snippet = APP_JS[start:end]
    script = f"""
global.window = {{ location: {{ origin: "http://localhost" }} }};
{snippet}
const result = {expression};
console.log(JSON.stringify(result));
"""
    completed = subprocess.run(
        ["node", "-e", script],
        check=True,
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


class ApprovalContentUiTest(unittest.TestCase):
    def test_source_detail_uses_structured_renderer_and_keeps_raw_text(self) -> None:
        self.assertIn("function formatApprovalContent", APP_JS)
        self.assertIn("function renderApprovalContent", APP_JS)
        self.assertIn("renderApprovalContent(contentsText, { checkedLabels })", APP_JS)
        self.assertIn("approval-summary-grid", APP_JS)
        self.assertIn("approval-section", APP_JS)
        self.assertIn("approval-raw", APP_JS)
        self.assertIn("원문 텍스트", APP_JS)

    def test_renderer_keeps_xss_escaping_boundary(self) -> None:
        self.assertIn("html(pair.value)", APP_JS)
        self.assertIn("html(section.title)", APP_JS)
        self.assertIn("html(line.text)", APP_JS)
        self.assertIn("html(formatted.raw)", APP_JS)

    def test_source_detail_styles_cover_light_and_dark_surfaces(self) -> None:
        self.assertIn(".approval-content-view", STYLES)
        self.assertIn(".approval-summary-grid", STYLES)
        self.assertIn(".approval-line.bullet", STYLES)
        self.assertIn(":root[data-theme=\"dark\"] .approval-section", STYLES)

    def test_numbered_body_lines_are_not_split_into_fake_sections(self) -> None:
        formatted = run_approval_js(
            'formatApprovalContent("신청내용\\n1. 서버 재기동\\n2. 로그 확인\\n3. 담당자 공유")'
        )
        self.assertEqual([section["title"] for section in formatted["sections"]], ["신청내용"])
        self.assertEqual(
            [line["text"] for line in formatted["sections"][0]["lines"]],
            ["1. 서버 재기동", "2. 로그 확인", "3. 담당자 공유"],
        )

    def test_long_summary_value_keeps_label_context_without_orphaning(self) -> None:
        long_reason = "업무상 필요한 사용 사유입니다. " * 25
        raw = "상세사유\n" + long_reason + "\n신청내용\n본문 확인"
        formatted = run_approval_js(
            f"formatApprovalContent({json.dumps(raw)})"
        )
        self.assertEqual(formatted["pairs"][0]["label"], "상세사유")
        self.assertLessEqual(len(formatted["pairs"][0]["value"]), 360)
        section_lines = [line["text"] for section in formatted["sections"] for line in section["lines"]]
        self.assertNotIn(long_reason.strip(), section_lines)

    def test_software_request_highlights_are_extracted_from_noisy_approval_text(self) -> None:
        raw = "\n".join([
            "사용자 정보",
            "부서명",
            "미국법인",
            "신청자명",
            "손태민",
            "신청내용",
            "신청",
            "대상",
            "Dev Tool",
            "Github Docker",
            "AI Agent(GenAI)",
            "Claude Standard Claude Premium",
            "사용용도",
            "손태민 (Claude Premium), 그 외 (Claude Standard) 신청합니다.",
            "사용기한",
            ". . 까지",
            "영구",
            "참 조 문 서",
            "선택된 문서가 없습니다.",
        ])
        formatted = run_approval_js(f"formatApprovalContent({json.dumps(raw)})")
        self.assertEqual(
            formatted["highlights"]["summary"],
            [
                {"label": "부서", "value": "미국법인"},
                {"label": "신청자", "value": "손태민"},
                {"label": "사용용도", "value": "손태민 (Claude Premium), 그 외 (Claude Standard) 신청합니다."},
                {"label": "사용기한", "value": "영구"},
            ],
        )
        self.assertIn(
            {"category": "AI Agent(GenAI)", "value": "Claude Standard Claude Premium"},
            formatted["highlights"]["requests"],
        )

    def test_checked_html_labels_override_noisy_request_choices(self) -> None:
        raw = "\n".join([
            "신청내용",
            "Dev Tool",
            "Github Docker",
            "AI Agent(GenAI)",
            "Claude Standard Claude Premium",
        ])
        formatted = run_approval_js(
            f"formatApprovalContent({json.dumps(raw)}, {{ checkedLabels: ['Claude Premium'] }})"
        )
        self.assertEqual(
            formatted["highlights"]["requests"],
            [{"category": "선택 항목", "value": "Claude Premium"}],
        )

    def test_checked_labels_are_extracted_from_approval_html(self) -> None:
        html = "<label><input type='checkbox' checked='true'>&nbsp;Claude Standard&nbsp;</label><label><input type='checkbox' checked>&nbsp;Claude Premium</label><label><input type='checkbox'>&nbsp;Docker</label>"
        labels = run_approval_js(f"extractCheckedApprovalLabelsFromHtml({json.dumps(html)})")
        self.assertEqual(labels, ["Claude Standard", "Claude Premium"])

    def test_checked_label_extraction_stops_at_next_input_and_ignores_radio_duration(self) -> None:
        html = "<p>&nbsp;<input type='checkbox' checked='true'>&nbsp;Claude Standard&nbsp;<input type='checkbox' checked='true'>&nbsp;Claude Premium</p><p><input type='radio' checked='true'>&nbsp;영구</p>"
        labels = run_approval_js(f"extractCheckedApprovalLabelsFromHtml({json.dumps(html)})")
        self.assertEqual(labels, ["Claude Standard", "Claude Premium"])

    def test_rendered_approval_content_has_readable_focus_blocks(self) -> None:
        rendered = run_approval_js(
            'renderApprovalContent("사용자 정보\\n부서명\\n미국법인\\n신청자명\\n손태민\\n신청내용\\nAI Agent(GenAI)\\nClaude Premium\\n사용용도\\n개발 업무\\n사용기한\\n영구")'
        )
        self.assertIn("approval-readable-focus", rendered)
        self.assertIn("신청자 정보", rendered)
        self.assertIn("신청 항목", rendered)
        self.assertIn("Claude Premium", rendered)

    def test_external_url_allows_only_http_family_links(self) -> None:
        self.assertEqual(run_approval_js('safeExternalUrl("javascript:alert(1)")'), "")
        self.assertEqual(run_approval_js('safeExternalUrl("data:text/html,hi")'), "")
        self.assertEqual(run_approval_js('safeExternalUrl("https://example.com/doc")'), "https://example.com/doc")
        self.assertEqual(run_approval_js('safeExternalUrl("/approval/105020")'), "http://localhost/approval/105020")

    def test_rendered_approval_content_escapes_raw_markup(self) -> None:
        rendered = run_approval_js('renderApprovalContent("<img src=x onerror=alert(1)>")')
        self.assertIn("&lt;img src=x onerror=alert(1)&gt;", rendered)
        self.assertNotIn("<img src=x", rendered)
