from __future__ import annotations

import json
from pathlib import Path
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
APP_JS = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
STYLES = (ROOT / "static" / "styles.css").read_text(encoding="utf-8")


def run_theme_js(expression: str):
    html_start = APP_JS.index("const html =")
    html_end = APP_JS.index("\n\nfunction safeExternalUrl", html_start)
    theme_start = APP_JS.index("const THEME_STORAGE_KEY")
    theme_end = APP_JS.index("\nconst visibleModes")
    script = f"""
const store = {{}};
global.window = {{
  localStorage: {{
    getItem(key) {{ return Object.prototype.hasOwnProperty.call(store, key) ? store[key] : null; }},
    setItem(key, value) {{ store[key] = String(value); }},
  }},
  matchMedia() {{ return {{ addEventListener() {{}} }}; }},
}};
global.document = {{
  documentElement: {{
    attrs: {{}},
    setAttribute(key, value) {{ this.attrs[key] = value; }},
    getAttribute(key) {{ return this.attrs[key]; }},
  }},
}};
{APP_JS[html_start:html_end]}
{APP_JS[theme_start:theme_end]}
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


class ThemeUiTest(unittest.TestCase):
    def test_theme_selector_is_user_controlled_and_persisted(self) -> None:
        self.assertIn("THEME_STORAGE_KEY", APP_JS)
        self.assertIn("applyTheme", APP_JS)
        self.assertIn("localStorage.setItem(THEME_STORAGE_KEY", APP_JS)
        self.assertIn("data-theme-choice", APP_JS)
        self.assertIn('aria-label="화면 테마 선택"', APP_JS)

    def test_dark_mode_can_be_forced_without_system_preference(self) -> None:
        self.assertIn(':root[data-theme="dark"]', STYLES)
        self.assertIn(':root[data-theme="light"]', STYLES)
        self.assertIn(".theme-switch", STYLES)
        self.assertIn("color-scheme: dark", STYLES)
        self.assertIn("color-scheme: light", STYLES)

    def test_dark_theme_hover_guard_covers_light_hover_surfaces(self) -> None:
        required_selectors = [
            ':root[data-theme="dark"] .brand-home:hover',
            ':root[data-theme="dark"] .owner-kind-button:hover',
            ':root[data-theme="dark"] .owner-total:hover',
            ':root[data-theme="dark"] .quality-card:hover',
            ':root[data-theme="dark"] .triage-chip:hover',
            ':root[data-theme="dark"] .btn:not(.primary):not(.btn-add):hover',
            ':root[data-theme="dark"] .dashboard-table tbody tr:hover > *',
            ':root[data-theme="dark"] .data-table tbody tr:hover > *',
            ':root[data-theme="dark"] .data-table tr[data-asset]:hover > *',
            ':root[data-theme="dark"] .data-table tr[data-asset-group]:hover > *',
            ':root[data-theme="dark"] .data-table tr[data-approval]:hover > *',
        ]
        for selector in required_selectors:
            with self.subTest(selector=selector):
                self.assertIn(selector, STYLES)

    def test_zebra_striping_does_not_override_selected_rows(self) -> None:
        self.assertIn(
            ".data-table tbody tr:nth-child(even):not(.selected):not(:hover):not(.row-stale) td",
            STYLES,
        )
        self.assertIn(
            ".dashboard-table tbody tr:nth-child(even):not(.selected):not(:hover) td",
            STYLES,
        )

    def test_system_dark_selected_rows_use_opaque_dark_cells(self) -> None:
        self.assertIn("@media (prefers-color-scheme: dark)", STYLES)
        self.assertIn(".data-table tr.selected > *", STYLES)
        self.assertIn("background: #1d2b45", STYLES)
        self.assertIn(
            ".data-table tr[data-asset].selected,\n"
            "  .data-table tr[data-asset-group].selected,\n"
            "  .data-table tr[data-approval].selected,\n"
            "  .data-table tr.selected {\n"
            "    background: transparent",
            STYLES,
        )

    def test_edit_form_inputs_are_covered_in_dark_and_system_dark_modes(self) -> None:
        self.assertIn(":root[data-theme=\"dark\"] .edit-form input", STYLES)
        self.assertIn("@media (prefers-color-scheme: dark)", STYLES)
        self.assertIn(".edit-form input,\n  .edit-form select,\n  .edit-form textarea", STYLES)

    def test_theme_choice_updates_root_and_persists(self) -> None:
        result = run_theme_js(
            '[setTheme("dark"), document.documentElement.getAttribute("data-theme"), '
            'store[THEME_STORAGE_KEY], themeSelector().includes(\'data-theme-choice="dark" aria-pressed="true"\')]'
        )
        self.assertEqual(result, ["dark", "dark", "dark", True])

    def test_invalid_theme_falls_back_to_system(self) -> None:
        result = run_theme_js(
            '[setTheme("bad-value"), document.documentElement.getAttribute("data-theme"), store[THEME_STORAGE_KEY]]'
        )
        self.assertEqual(result, ["system", "system", "system"])
