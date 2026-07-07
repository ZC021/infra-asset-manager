const initialParams = new URLSearchParams(window.location.search);

const THEME_STORAGE_KEY = "infra-control.theme";
const THEME_VALUES = ["system", "light", "dark"];
const THEME_ORDER = ["light", "system", "dark"];
const THEME_META = {
  light: { label: "일반", icon: "☀" },
  system: { label: "자동", icon: "◐" },
  dark: { label: "다크", icon: "☾" },
};

function loadTheme() {
  try {
    const v = window.localStorage.getItem(THEME_STORAGE_KEY);
    return THEME_VALUES.includes(v) ? v : "system";
  } catch (_) {
    return "system";
  }
}

function applyTheme(theme) {
  if (!THEME_VALUES.includes(theme)) theme = "system";
  const root = document.documentElement;
  // "system" intentionally has no CSS override; prefers-color-scheme remains active.
  root.setAttribute("data-theme", theme);
}

function setTheme(theme) {
  if (!THEME_VALUES.includes(theme)) theme = "system";
  try { window.localStorage.setItem(THEME_STORAGE_KEY, theme); } catch (_) {}
  applyTheme(theme);
  return theme;
}

applyTheme(loadTheme());

const systemThemeMedia = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;
if (systemThemeMedia?.addEventListener) {
  systemThemeMedia.addEventListener("change", () => {
    if (loadTheme() === "system") applyTheme("system");
  });
}

function themeSelector() {
  const current = loadTheme();
  return `
    <div class="theme-switch" role="group" aria-label="화면 테마 선택">
      ${THEME_ORDER.map((theme) => {
        const meta = THEME_META[theme];
        const active = theme === current;
        return `
          <button type="button" class="theme-choice${active ? " active" : ""}" data-theme-choice="${theme}" aria-pressed="${active ? "true" : "false"}" title="${html(meta.label)} 모드">
            <span aria-hidden="true">${html(meta.icon)}</span>
            <strong>${html(meta.label)}</strong>
          </button>
        `;
      }).join("")}
    </div>
  `;
}

const visibleModes = new Set([
  "dashboard",
  "assets",
  "asset_groups",
  "servers",
  "network",
  "notebooks",
  "notebook_refresh",
  "other_assets",
  "in_use",
  "unused",
  "retired",
  "needs_review",
  "owners",
  "locations",
  "maintenance",
  "software_inventory",
  "software_installs",
  "software_users",
  "software_expirations",
  "changes",
  "sources",
  "jira_missing",
  "approvals",
]);

function initialMode() {
  if (initialParams.get("mode") === "approvals" || initialParams.get("docId")) return "approvals";
  const requested = initialParams.get("mode") || "dashboard";
  return visibleModes.has(requested) ? requested : "dashboard";
}

const state = {
  mode: initialMode(),
  query: initialParams.get("q") || "",
  assetPrefix: initialParams.get("prefix") || "",
  ownerFilter: initialParams.get("owner") || "",
  assetKind: initialParams.get("kind") || "",
  assetClass: initialParams.get("class") || "",
  dupTagOnly: initialParams.get("dup_tag") === "1",
  dupSerialOnly: initialParams.get("dup_serial") === "1",
  sourceIncludeHidden: initialParams.get("include_hidden") === "1",
  changeAsset: initialParams.get("asset_tag") || "",
  changeSource: initialParams.get("source") || "",
  changeField: initialParams.get("field") || "",
  selectedAsset: "",
  selectedSoftware: "",
  selectedSoftwareUser: "",
  selectedApproval: initialParams.get("docId") || "",
  approvalFilter: initialParams.get("approvalFilter") || "pending",
  selectedRack: "",
  summary: {},
  insights: {},
  dataQuality: { cards: [], triage: [], counts: {}, sources: [] },
  rows: [],
  detail: null,
  dashboardApprovals: null,
  tableFilters: {},
  openTableFilter: null,
  loading: false,
  lastUpdated: "",
};

let searchTimer = null;

const nav = [
  {
    title: "",
    items: [
      ["dashboard", "대시보드", "▦"],
      ["assets", "전체자산", "AS"],
    ],
  },
  {
    title: "자산",
    items: [
      ["servers", "서버", "SV"],
      ["network", "네트워크", "NW"],
      ["notebooks", "노트북", "NB"],
      ["notebook_refresh", "교체/매각", "RP"],
      ["other_assets", "기타자산", "ET"],
      ["in_use", "사용중", "ON"],
      ["unused", "미사용", "ID"],
      ["retired", "종료/제외", "EX"],
      ["needs_review", "확인필요", "RV"],
    ],
  },
  {
    title: "인프라",
    items: [
      ["locations", "상면도", "RK"],
      ["maintenance", "유지보수", "MT"],
      ["software_inventory", "SW 요약", "SW"],
      ["software_installs", "SW 전체설치", "IN"],
      ["software_users", "SW 사용현황", "US"],
      ["software_expirations", "SW 만료일", "EX"],
    ],
  },
  {
    title: "관리",
    items: [
      ["approvals", "전자결재", "AP"],
      ["owners", "담당자", "OW"],
      ["asset_groups", "자산번호", "NO"],
      ["changes", "변경 이력", "LG"],
      ["sources", "연동", "SC"],
      ["jira_missing", "Jira-only", "JM"],
    ],
  },
];

const modeLabels = {
  dashboard: "대시보드",
  assets: "전체 자산",
  asset_groups: "자산번호",
  servers: "서버",
  network: "네트워크",
  notebooks: "노트북",
  notebook_refresh: "노트북 교체/매각",
  other_assets: "기타자산",
  in_use: "사용중",
  unused: "미사용",
  retired: "종료/제외",
  needs_review: "확인필요",
  owners: "담당자",
  locations: "상면도",
  maintenance: "유지보수",
  software_inventory: "SW 요약",
  software_installs: "SW 전체설치",
  software_users: "SW 사용현황",
  software_expirations: "SW 만료일",
  changes: "변경 이력",
  sources: "연동",
  jira_missing: "Jira-only 자산",
  approvals: "전자결재",
};

const assetScopedModes = ["assets", "servers", "network", "notebooks", "notebook_refresh", "other_assets", "in_use", "unused", "retired", "needs_review"];
const filterableTableModes = new Set([
  "assets",
  "asset_groups",
  "servers",
  "network",
  "notebooks",
  "notebook_refresh",
  "other_assets",
  "in_use",
  "unused",
  "retired",
  "needs_review",
  "owners",
  "maintenance",
  "software_inventory",
  "software_installs",
  "software_users",
  "software_expirations",
  "changes",
  "sources",
  "jira_missing",
  "approvals",
]);
const modeAssetKinds = {
  servers: "server",
  network: "network",
  notebooks: "notebook",
  notebook_refresh: "notebook",
  other_assets: "other",
};
const assetKindLabels = {
  server: "서버",
  network: "네트워크",
  notebook: "노트북",
  other: "기타자산",
};
const assetClassLabels = {
  server: "서버",
  virtual_server: "가상서버",
  network: "네트워크",
  notebook: "노트북",
  desktop: "데스크탑",
  storage: "스토리지",
  printer: "복합기",
  power_monitoring: "전원/모니터링",
  kvm: "KVM",
  all_in_one: "일체형PC",
  misc: "기타자산",
  unnumbered: "관리번호 미지정",
};

const number = (value) => Number(value || 0).toLocaleString("ko-KR");
const kstFormatter = new Intl.DateTimeFormat("ko-KR", {
  timeZone: "Asia/Seoul",
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hour12: false,
});
function compact(value) {
  const raw = String(value || "").trim();
  if (!raw) return "";
  if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/.test(raw)) return raw;
  const normalized = raw.endsWith("Z") || /[+-]\d{2}:?\d{2}$/.test(raw) ? raw : `${raw}Z`;
  const date = new Date(normalized);
  if (Number.isNaN(date.getTime())) return raw;
  const parts = Object.fromEntries(kstFormatter.formatToParts(date).map((part) => [part.type, part.value]));
  return `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute}:${parts.second} KST`;
}
const truthy = (value) => Number(value || 0) === 1 || value === true;
const html = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({
  "&": "&amp;",
  "<": "&lt;",
  ">": "&gt;",
  '"': "&quot;",
  "'": "&#039;",
})[char]);

function safeExternalUrl(value) {
  const raw = String(value || "").trim();
  if (!raw) return "";
  try {
    const url = new URL(raw, window.location.origin);
    return ["http:", "https:"].includes(url.protocol) ? url.href : "";
  } catch (_) {
    return "";
  }
}

const APPROVAL_SUMMARY_LABELS = new Map([
  ["품 의 번 호", "품의번호"],
  ["품의번호", "품의번호"],
  ["작 성 일 자", "작성일자"],
  ["작성일자", "작성일자"],
  ["기 안 부 서", "기안부서"],
  ["기안부서", "기안부서"],
  ["기 안 자", "기안자"],
  ["기안자", "기안자"],
  ["수신및참조", "수신/참조"],
  ["수신 및 참조", "수신/참조"],
  ["시 행 자", "시행자"],
  ["시행자", "시행자"],
  ["시 행 일 자", "시행일자"],
  ["시행일자", "시행일자"],
  ["제 목", "제목"],
  ["제목", "제목"],
  ["부서명", "부서"],
  ["신청자명", "신청자"],
  ["사용목적", "사용목적"],
  ["사용기간", "사용기간"],
  ["시작일자", "시작일자"],
  ["종료일자", "종료일자"],
  ["상세사유", "상세사유"],
]);

const APPROVAL_SECTION_TITLES = new Set([
  "안내사항",
  "사용자 정보",
  "신청 정보",
  "신청내용",
  "신청 내용",
  "요청 정보",
  "서비스 정보",
  "구매 정보",
  "계정 정보",
  "사용목적 및 사용기간",
  "점검대상",
  "점검 대상",
  "목적",
  "상세 내용",
]);

const APPROVAL_NUMBERED_SECTION_TITLES = new Set([
  "개요",
  "배경",
  "목적",
  "내용",
  "요청",
  "요청사항",
  "근거",
  "일정",
  "예산",
  "결론",
  "기대효과",
]);

const APPROVAL_NOISE_LINES = new Set([
  "결재",
  "합의",
  "팀원",
  "팀장",
  "실장",
  "본부장",
  "대표",
  "의장",
]);

function cleanApprovalLine(line) {
  return String(line || "").replace(/\s+/g, " ").trim();
}

function normalizeApprovalLines(raw) {
  return String(raw || "")
    .replace(/\r\n?/g, "\n")
    .split("\n")
    .map(cleanApprovalLine)
    .filter(Boolean);
}

function isApprovalHelperLine(line) {
  return /^\(?본부\/실\/팀\/파트\)?$/.test(line) || /^외 \d+명$/.test(line);
}

function isApprovalNoiseLine(line) {
  return APPROVAL_NOISE_LINES.has(line) || /^\d+$/.test(line) || isApprovalHelperLine(line);
}

function approvalSummaryLabel(line) {
  return APPROVAL_SUMMARY_LABELS.get(line) || "";
}

function numberedApprovalSectionTitle(line) {
  const match = String(line || "").match(/^\d{1,2}\.\s*(.+)$/);
  if (!match) return "";
  return cleanApprovalLine(match[1]).replace(/[:：]$/, "");
}

function isApprovalSectionHeading(line) {
  if (APPROVAL_SECTION_TITLES.has(line)) return true;
  const numberedTitle = numberedApprovalSectionTitle(line);
  return APPROVAL_SECTION_TITLES.has(numberedTitle) || APPROVAL_NUMBERED_SECTION_TITLES.has(numberedTitle);
}

function cleanApprovalSectionTitle(line) {
  const numberedTitle = numberedApprovalSectionTitle(line);
  return numberedTitle || cleanApprovalLine(line);
}

function nextApprovalValueIndex(lines, start) {
  for (let idx = start; idx < lines.length; idx += 1) {
    const candidate = lines[idx];
    if (isApprovalNoiseLine(candidate)) continue;
    if (approvalSummaryLabel(candidate) || isApprovalSectionHeading(candidate)) return -1;
    return idx;
  }
  return -1;
}

const APPROVAL_FOCUS_LABELS = new Map([
  ["부서명", "부서"],
  ["부서", "부서"],
  ["신청자명", "신청자"],
  ["신청자", "신청자"],
  ["사용용도", "사용용도"],
  ["사용목적", "사용용도"],
  ["사용기한", "사용기한"],
  ["사용기간", "사용기한"],
]);

const APPROVAL_REQUEST_SECTIONS = new Set([
  "신청내용",
  "신청 내용",
  "신청 정보",
  "요청 정보",
  "요청사항",
]);

const APPROVAL_REQUEST_NOISE = new Set(["신청", "대상"]);

function approvalFocusLabel(line) {
  return APPROVAL_FOCUS_LABELS.get(line) || "";
}

function isApprovalPlaceholderValue(line) {
  const text = String(line || "").trim();
  if (!text) return true;
  if (/^[.\s·ㆍ~∼\-]+$/.test(text)) return true;
  if (/까지$/.test(text) && !/\d/.test(text)) return true;
  return false;
}

function extractCheckedApprovalLabelsFromHtml(rawHtml) {
  const source = String(rawHtml || "");
  const inputRe = /<input\b[^>]*>/gi;
  const inputs = [];
  let match;
  while ((match = inputRe.exec(source))) {
    inputs.push({ tag: match[0], end: inputRe.lastIndex, start: match.index });
  }
  const labels = [];
  inputs.forEach((input, idx) => {
    const isCheckbox = /type\s*=\s*['"]?checkbox/i.test(input.tag);
    const isChecked = /\bchecked\b/i.test(input.tag);
    if (!isCheckbox || !isChecked) return;
    const sliceEnd = idx + 1 < inputs.length ? inputs[idx + 1].start : source.length;
    const text = source
      .slice(input.end, sliceEnd)
      .replace(/<[^>]*>/g, " ")
      .replace(/&nbsp;/gi, " ")
      .replace(/\s+/g, " ")
      .trim();
    if (text) labels.push(text);
  });
  return labels;
}

function extractApprovalHighlights(lines, options) {
  const opts = options || {};
  const summary = [];
  const seen = new Set();
  for (let i = 0; i < lines.length; i += 1) {
    const display = approvalFocusLabel(lines[i]);
    if (!display || seen.has(display)) continue;
    let value = "";
    for (let j = i + 1; j < lines.length; j += 1) {
      const cand = lines[j];
      if (approvalFocusLabel(cand) || approvalSummaryLabel(cand) || isApprovalSectionHeading(cand)) break;
      if (isApprovalNoiseLine(cand) || isApprovalPlaceholderValue(cand)) continue;
      value = cand;
      break;
    }
    if (value) {
      summary.push({ label: display, value });
      seen.add(display);
    }
  }

  const checked = Array.isArray(opts.checkedLabels) ? opts.checkedLabels.filter(Boolean) : [];
  let requests = [];
  if (checked.length) {
    requests = checked.map((label) => ({ category: "선택 항목", value: String(label).trim() }));
  } else {
    const startIdx = lines.findIndex((line) => APPROVAL_REQUEST_SECTIONS.has(line));
    if (startIdx >= 0) {
      const content = [];
      for (let k = startIdx + 1; k < lines.length; k += 1) {
        const cand = lines[k];
        if (approvalFocusLabel(cand) || approvalSummaryLabel(cand) || isApprovalSectionHeading(cand)) break;
        if (isApprovalNoiseLine(cand) || APPROVAL_REQUEST_NOISE.has(cand)) continue;
        content.push(cand);
      }
      for (let p = 0; p + 1 < content.length; p += 2) {
        requests.push({ category: content[p], value: content[p + 1] });
      }
    }
  }
  return { summary, requests };
}

function renderApprovalFocusBlock(highlights) {
  const data = highlights || { summary: [], requests: [] };
  const summary = Array.isArray(data.summary) ? data.summary : [];
  const requests = Array.isArray(data.requests) ? data.requests : [];
  if (!summary.length && !requests.length) return "";
  const summaryCard = summary.length ? `
    <section class="approval-focus-card">
      <h4>신청자 정보</h4>
      <dl class="approval-focus-list">
        ${summary.map((item) => `<div><dt>${html(item.label)}</dt><dd>${html(item.value)}</dd></div>`).join("")}
      </dl>
    </section>
  ` : "";
  const requestCard = requests.length ? `
    <section class="approval-focus-card">
      <h4>신청 항목</h4>
      <ul class="approval-focus-requests">
        ${requests.map((item) => `<li><span>${html(item.category)}</span><strong>${html(item.value)}</strong></li>`).join("")}
      </ul>
    </section>
  ` : "";
  return `
    <div class="approval-readable-focus">
      ${summaryCard}
      ${requestCard}
    </div>
  `;
}

function formatApprovalContent(raw, options) {
  const rawText = String(raw || "").trim();
  const lines = normalizeApprovalLines(rawText);
  const used = new Set();
  const seenLabels = new Set();
  const pairs = [];

  lines.forEach((line, index) => {
    const label = approvalSummaryLabel(line);
    if (!label || seenLabels.has(label)) return;
    const valueIndex = nextApprovalValueIndex(lines, index + 1);
    if (valueIndex < 0) return;
    const value = lines[valueIndex];
    if (!value) return;
    const clippedValue = value.length > 360 ? `${value.slice(0, 357)}...` : value;
    pairs.push({ label, value: clippedValue });
    seenLabels.add(label);
    used.add(index);
    used.add(valueIndex);
  });

  const sections = [];
  let current = { title: "본문", lines: [] };
  const pushCurrent = () => {
    if (current.lines.length) sections.push(current);
  };

  lines.forEach((line, index) => {
    if (used.has(index) || isApprovalNoiseLine(line)) return;
    if (approvalSummaryLabel(line)) return;
    if (isApprovalSectionHeading(line)) {
      pushCurrent();
      current = { title: cleanApprovalSectionTitle(line), lines: [] };
      return;
    }
    const bullet = /^[-*•]\s*/.test(line);
    current.lines.push({
      type: bullet ? "bullet" : "text",
      text: bullet ? line.replace(/^[-*•]\s*/, "") : line,
    });
  });
  pushCurrent();

  const highlights = extractApprovalHighlights(lines, options);
  return {
    raw: rawText,
    pairs: pairs.slice(0, 12),
    sections: sections.slice(0, 8),
    highlights,
  };
}

function renderApprovalContent(raw, options) {
  const formatted = formatApprovalContent(raw, options);
  if (!formatted.raw) {
    return `<div class="approval-content-empty">원천 상세 본문이 아직 캐시에 없습니다. 원문 열기로 Amaranth 문서를 확인하세요.</div>`;
  }
  const summary = formatted.pairs.length ? `
    <div class="approval-summary-grid">
      ${formatted.pairs.map((pair) => `
        <div class="approval-summary-item">
          <span>${html(pair.label)}</span>
          <strong>${html(pair.value)}</strong>
        </div>
      `).join("")}
    </div>
  ` : "";
  const sections = formatted.sections.length ? formatted.sections.map((section) => `
    <section class="approval-section">
      <h4>${html(section.title)}</h4>
      <ul class="approval-line-list">
        ${section.lines.map((line) => `<li class="approval-line ${line.type === "bullet" ? "bullet" : "text"}">${html(line.text)}</li>`).join("")}
      </ul>
    </section>
  `).join("") : "";
  const focusBlock = renderApprovalFocusBlock(formatted.highlights);
  return `
    <div class="approval-content-view">
      ${focusBlock}
      ${summary}
      ${sections || `<pre class="approval-content">${html(formatted.raw)}</pre>`}
      <details class="approval-raw">
        <summary>원문 텍스트</summary>
        <pre class="approval-content raw">${html(formatted.raw)}</pre>
      </details>
    </div>
  `;
}

function captureScroll() {
  return {
    windowY: window.scrollY,
    workspace: document.querySelector(".workspace")?.scrollTop || 0,
    table: document.querySelector(".content-body")?.scrollTop || 0,
    detail: document.querySelector(".detail")?.scrollTop || 0,
  };
}

function restoreScroll(position) {
  if (!position) return;
  requestAnimationFrame(() => {
    window.scrollTo(0, position.windowY || 0);
    const workspace = document.querySelector(".workspace");
    const table = document.querySelector(".content-body");
    const detail = document.querySelector(".detail");
    if (workspace) workspace.scrollTop = position.workspace || 0;
    if (table) table.scrollTop = position.table || 0;
    if (detail) detail.scrollTop = position.detail || 0;
  });
}

function writeApprovalUrl(docId) {
  syncUrlState({ docId });
}

function syncUrlState({ docId } = {}) {
  const url = new URL(window.location.href);
  if (state.mode && state.mode !== "dashboard") {
    url.searchParams.set("mode", state.mode);
  } else {
    url.searchParams.delete("mode");
  }
  if (state.mode === "approvals" && docId) {
    url.searchParams.set("docId", docId);
  } else if (state.mode !== "approvals") {
    url.searchParams.delete("docId");
  }
  if (state.assetPrefix) url.searchParams.set("prefix", state.assetPrefix);
  else url.searchParams.delete("prefix");
  if (state.ownerFilter) url.searchParams.set("owner", state.ownerFilter);
  else url.searchParams.delete("owner");
  if (state.assetKind) url.searchParams.set("kind", state.assetKind);
  else url.searchParams.delete("kind");
  if (state.assetClass) url.searchParams.set("class", state.assetClass);
  else url.searchParams.delete("class");
  if (state.query) url.searchParams.set("q", state.query);
  else url.searchParams.delete("q");
  if (state.dupTagOnly) url.searchParams.set("dup_tag", "1");
  else url.searchParams.delete("dup_tag");
  if (state.dupSerialOnly) url.searchParams.set("dup_serial", "1");
  else url.searchParams.delete("dup_serial");
  if (state.mode === "sources" && state.sourceIncludeHidden) url.searchParams.set("include_hidden", "1");
  else url.searchParams.delete("include_hidden");
  if (state.mode === "changes" && state.changeAsset) url.searchParams.set("asset_tag", state.changeAsset);
  else url.searchParams.delete("asset_tag");
  if (state.mode === "changes" && state.changeSource) url.searchParams.set("source", state.changeSource);
  else url.searchParams.delete("source");
  if (state.mode === "changes" && state.changeField) url.searchParams.set("field", state.changeField);
  else url.searchParams.delete("field");
  const nextPath = `${url.pathname}${url.search}`;
  const currentPath = `${window.location.pathname}${window.location.search}`;
  if (nextPath !== currentPath) window.history.pushState(window.history.state, "", nextPath);
}

async function api(path, options = {}) {
  const response = await fetch(path, options);
  if (!response.ok) throw new Error(`${path} ${response.status}`);
  return response.json();
}

function badge(value) {
  const text = value || "확인필요";
  const cls = text === "사용중" || text === "정상" || text === "up" ? "ok" : text === "미사용" || text === "종료/제외" || text === "down" ? "idle" : "warn";
  return `<span class="badge ${cls}">${html(text)}</span>`;
}

function softwareRiskBadge(value) {
  const text = value || "정상";
  const cls = text.startsWith("라이선스 확인") || text.startsWith("초과") ? "warn" : "ok";
  return `<span class="badge ${cls}">${html(text)}</span>`;
}

function softwareLicenseBadge(value) {
  const text = value || "확인필요";
  const cls = text === "정상" || text === "허가" ? "ok" : text === "라이선스 확인" ? "warn" : "idle";
  return `<span class="badge ${cls}">${html(text)}</span>`;
}

function ownerDisplay(row) {
  return row.owner || "-";
}

function ownerCell(row) {
  const status = row.owner_status && row.owner_status !== "active" ? row.owner_status_label || "재직 확인필요" : "";
  return `
    <span>${html(ownerDisplay(row))}</span>
    ${status ? `<br><span class="muted owner-status">${html(status)}</span>` : ""}
  `;
}

function assetTableColumns() {
  if (state.mode === "notebook_refresh") return notebookRefreshColumns();
  const lifecycleColumns = state.mode === "notebook_refresh" ? [
    { key: "lifecycle_label", label: "대상", value: (row) => row.lifecycle_label || "-", render: (row) => badge(row.lifecycle_label || "-") },
    { key: "notebook_age_years", label: "연식", className: "mono", value: (row) => row.notebook_age_years || "-", render: (row) => row.notebook_age_years ? `${html(row.notebook_age_years)}년` : "" },
    { key: "notebook_acquired_date", label: "취득/추정", className: "mono", value: (row) => `${row.notebook_acquired_date || "-"} ${row.notebook_age_basis || ""}`, render: (row) => `${html(row.notebook_acquired_date || "")}<br><span class="muted">${html(row.notebook_age_basis || "")}</span>` },
  ] : [];
  return [
    { key: "asset_tag", label: "자산", className: "mono", value: (row) => row.asset_tag, render: (row) => html(row.asset_tag) },
    { key: "asset_number_class_label", label: "분류", value: (row) => row.asset_number_class_label || row.asset_kind_label || row.category || "-", render: (row) => html(row.asset_number_class_label || row.asset_kind_label || row.category || "-") },
    { key: "asset_group", label: "번호대", className: "mono", value: (row) => row.asset_group, render: (row) => html(row.asset_group) },
    { key: "usage_status", label: "사용", value: (row) => row.usage_status || "확인필요", render: (row) => badge(row.usage_status) },
    ...lifecycleColumns,
    { key: "jira_lane", label: "Jira", className: "mono", value: (row) => row.jira_lane || "-", render: (row) => html(row.jira_lane || "") },
    { key: "hostname", label: "호스트", value: (row) => row.hostname || "-", render: (row) => html(row.hostname) },
    { key: "primary_ip", label: "IP", className: "mono", value: (row) => row.primary_ip || "-", render: (row) => html(row.primary_ip) },
    { key: "serial", label: "시리얼", className: "mono", value: (row) => row.serial || "-", render: (row) => html(row.serial) },
    { key: "model_label", label: "모델", value: (row) => [row.manufacturer, row.model].filter(Boolean).join(" ") || "-", render: (row) => html([row.manufacturer, row.model].filter(Boolean).join(" ")) },
    { key: "owner", label: "담당", value: ownerDisplay, render: ownerCell },
    { key: "department", label: "부서", value: (row) => row.department || "-", render: (row) => html(row.department) },
    { key: "location", label: "위치", value: (row) => [row.room, row.rack, row.rack_unit].filter(Boolean).join(" / ") || row.location || "-", render: (row) => html([row.room, row.rack, row.rack_unit].filter(Boolean).join(" / ") || row.location) },
    { key: "usage_basis", label: "사용근거", value: (row) => row.usage_basis || row.usage_reason || "-", render: (row) => html(row.usage_basis || row.usage_reason) },
  ];
}

function notebookRefreshColumns() {
  return [
    {
      key: "notebook_refresh_priority",
      label: "판단",
      value: (row) => `${row.notebook_refresh_priority || "-"} ${row.notebook_refresh_decision || ""} ${row.notebook_refresh_flags || ""}`,
      render: (row) => `
        <div class="decision-cell">
          ${badge(row.notebook_refresh_priority || "정보 확인")}
          <strong>${html(row.notebook_refresh_decision || "-")}</strong>
          <span>${html(row.notebook_refresh_flags || "")}</span>
        </div>
      `,
    },
    {
      key: "asset_tag",
      label: "자산",
      className: "mono",
      value: (row) => `${row.asset_tag || ""} ${row.hostname || ""} ${row.manufacturer || ""} ${row.model || ""}`,
      render: (row) => `
        <strong>${html(row.asset_tag || "-")}</strong><br>
        <span class="muted">${html([row.hostname, row.manufacturer, row.model].filter(Boolean).join(" ") || "-")}</span>
      `,
    },
    {
      key: "owner",
      label: "담당/부서",
      value: (row) => `${ownerDisplay(row)} ${row.department || ""} ${row.owner_status_label || ""}`,
      render: (row) => `${ownerCell(row)}<br><span class="muted">${html(row.department || "-")}</span>`,
    },
    {
      key: "usage_status",
      label: "사용",
      value: (row) => row.usage_status || "확인필요",
      render: (row) => badge(row.usage_status || "확인필요"),
    },
    {
      key: "notebook_refresh_reason",
      label: "근거",
      value: (row) => `${row.notebook_refresh_reason || ""} ${row.notebook_acquired_date || ""}`,
      render: (row) => `
        <strong>${html(row.notebook_refresh_reason || "-")}</strong><br>
        <span class="muted mono">${html(row.notebook_acquired_date || "-")}</span>
      `,
    },
    {
      key: "location",
      label: "위치",
      value: (row) => [row.room, row.rack, row.rack_unit].filter(Boolean).join(" / ") || row.location || "-",
      render: (row) => html([row.room, row.rack, row.rack_unit].filter(Boolean).join(" / ") || row.location || "-"),
    },
    {
      key: "usage_basis",
      label: "운영근거",
      value: (row) => `${row.usage_basis || row.usage_reason || ""} ${row.jira_lane || ""} ${row.jira_issue || ""}`,
      render: (row) => `
        <span>${html(row.usage_basis || row.usage_reason || "-")}</span><br>
        <span class="muted mono">${html([row.jira_lane, row.jira_issue].filter(Boolean).join(" / "))}</span>
      `,
    },
  ];
}

function simpleColumns(pairs) {
  return pairs.map(([key, label]) => ({
    key,
    label,
    className: key.includes("ip") || key.includes("at") || key.includes("date") || key.includes("days") ? "mono" : "",
    value: (row) => compact(row[key]) || "-",
    render: (row) => html(compact(row[key])),
  }));
}

function tableColumnsForMode(mode) {
  if (assetScopedModes.includes(mode) || mode === "locations") return assetTableColumns();
  if (mode === "asset_groups") return [
    { key: "asset_group", label: "자산번호대", className: "mono", value: (row) => row.asset_group || "-", render: (row) => `<strong>${html(row.asset_group)}</strong>` },
    { key: "assets", label: "전체", className: "mono", value: (row) => number(row.assets), render: (row) => number(row.assets) },
    { key: "in_use", label: "사용중", className: "mono", value: (row) => number(row.in_use), render: (row) => number(row.in_use) },
    { key: "unused", label: "미사용", className: "mono", value: (row) => number(row.unused), render: (row) => number(row.unused) },
    { key: "retired", label: "종료/제외", className: "mono", value: (row) => number(row.retired), render: (row) => number(row.retired) },
    { key: "needs_review", label: "확인필요", className: "mono", value: (row) => number(row.needs_review), render: (row) => number(row.needs_review) },
    { key: "servers", label: "서버", className: "mono", value: (row) => number(row.servers), render: (row) => number(row.servers) },
    { key: "network", label: "네트워크", className: "mono", value: (row) => number(row.network), render: (row) => number(row.network) },
    { key: "owners", label: "담당자", className: "mono", value: (row) => number(row.owners), render: (row) => number(row.owners) },
    { key: "sample_assets", label: "샘플", className: "mono", value: (row) => row.sample_assets || "-", render: (row) => html(row.sample_assets) },
    { key: "latest_updated", label: "갱신", className: "mono", value: (row) => compact(row.latest_updated) || "-", render: (row) => html(compact(row.latest_updated)) },
  ];
  if (mode === "owners") return [
    { key: "owner", label: "담당자", value: (row) => `${row.owner || "-"} ${row.owner_email || ""}`, render: (row) => `<strong>${html(row.owner)}</strong><br><span class="muted">${html(row.owner_email || "")}</span>` },
    { key: "department", label: "부서", value: (row) => row.department || "-", render: (row) => html(row.department || "-") },
    { key: "assets", label: "전체", value: (row) => number(row.assets), render: (row) => `<button type="button" class="owner-total" data-owner-assets="${html(row.owner)}" data-class="" data-kind="">${number(row.assets)}개</button>` },
    { key: "category_summary", label: "분류별 자산", value: (row) => row.category_summary || "-", render: ownerCategoryButtons },
    { key: "usage_summary", label: "사용 상태", value: (row) => `사용중 ${number(row.in_use)} 미사용 ${number(row.unused)} 종료/제외 ${number(row.retired)} 확인필요 ${number(row.needs_review)}`, render: (row) => `<div class="owner-usage"><span>사용중 ${number(row.in_use)}</span><span>미사용 ${number(row.unused)}</span><span>종료/제외 ${number(row.retired)}</span><span>확인필요 ${number(row.needs_review)}</span></div>` },
    { key: "updated_at", label: "갱신", className: "mono", value: (row) => compact(row.updated_at) || "-", render: (row) => html(compact(row.updated_at || "")) },
    { key: "asset_tags", label: "샘플", value: (row) => row.asset_tags || "-", render: (row) => `<span class="muted">${html(String(row.asset_tags || "").split(", ").slice(0, 4).join(", "))}</span>` },
  ];
  if (mode === "sources") return [
    { key: "freshness", label: "상태", value: (row) => sourceQuality(row).stale ? "stale" : row.last_sync_at ? "동기화" : "미설정", render: sourceFreshness },
    { key: "name", label: "이름", value: (row) => row.name || "-", render: (row) => `<strong>${html(row.name || "-")}</strong>` },
    { key: "kind", label: "종류", value: (row) => row.kind || "-", render: (row) => html(row.kind || "-") },
    { key: "status", label: "연동 상태", value: (row) => row.status || "확인필요", render: (row) => sourceStatusBadge(row.status || "확인필요") },
    { key: "record_count", label: "건수", className: "mono", value: (row) => row.record_count === null || row.record_count === undefined ? "-" : number(row.record_count), render: (row) => row.record_count === null || row.record_count === undefined ? "-" : number(row.record_count) },
    { key: "last_sync_at", label: "마지막 동기화", className: "mono", value: (row) => compact(row.last_sync_at) || "-", render: (row) => html(compact(row.last_sync_at || "")) },
    { key: "endpoint", label: "경로", className: "mono", value: (row) => row.endpoint || "-", render: (row) => html(row.endpoint || "") },
  ];
  if (mode === "approvals") return [
    { key: "bucket", label: "분류", value: (row) => row.bucket || "확인필요", render: (row) => approvalBadge(row.bucket) },
    { key: "sourceLabel", label: "구분", value: (row) => row.sourceLabel || row.theme || "-", render: (row) => html(row.sourceLabel || row.theme || "-") },
    { key: "statusLabel", label: "원천 상태", value: (row) => row.sourceStatus?.label || row.statusLabel || "-", render: approvalSourceStatus },
    { key: "docNo", label: "문서번호", className: "mono", value: (row) => row.docNo || row.docId || "-", render: (row) => html(row.docNo || row.docId) },
    { key: "docTitle", label: "제목", value: (row) => `${row.docTitle || ""} ${row.formName || ""}` || "-", render: (row) => `<strong>${html(row.docTitle || "(제목 없음)")}</strong><br><span class="muted">${html(row.formName || "")}</span>` },
    { key: "createdBy", label: "기안자", value: (row) => row.createdBy || "-", render: (row) => html(row.createdBy || "-") },
    { key: "deptName", label: "부서", value: (row) => row.deptName || "-", render: (row) => html(row.deptName || "-") },
    { key: "repDt", label: "최근시각", className: "mono", value: (row) => compactApprovalTime(row.repDt) || "-", render: (row) => html(compactApprovalTime(row.repDt)) },
    { key: "localStatus", label: "내부상태", value: (row) => row.localStatus || "-", render: (row) => approvalLocalStatus(row.localStatus, row.sourceStatus) },
  ];
  if (mode === "maintenance") return simpleColumns([
    ["event_date", "일자"], ["asset_tag", "자산"], ["hostname", "호스트"], ["event_type", "유형"], ["status", "상태"], ["note", "내용"],
  ]);
  if (mode === "software_expirations") return simpleColumns([
    ["expiry_status", "상태"], ["software_name", "소프트웨어"], ["expiration_date", "만료일"], ["days_left", "남은일"], ["requester", "신청자"], ["department", "부서"], ["docTitle", "문서"],
  ]);
  if (mode === "software_inventory") return [
    { key: "risk_label", label: "상태", value: (row) => row.risk_label || "정상", render: (row) => softwareRiskBadge(row.risk_label) },
    { key: "software_name", label: "소프트웨어", value: (row) => row.software_name || "-", render: (row) => `<strong>${html(row.software_name || "-")}</strong><br><span class="muted mono">${html(row.sw_id || "")}</span>` },
    { key: "install_count", label: "설치", className: "mono", value: (row) => number(row.install_count), render: (row) => number(row.install_count) },
    { key: "illegal_install_count", label: "라이선스 확인", className: "mono", value: (row) => number(row.illegal_install_count), render: (row) => number(row.illegal_install_count) },
    { key: "license_amount", label: "라이선스", className: "mono", value: (row) => number(row.license_amount), render: (row) => number(row.license_amount) },
    { key: "collected_at", label: "수집", className: "mono", value: (row) => compact(row.collected_at) || "-", render: (row) => html(compact(row.collected_at || "")) },
  ];
  if (mode === "software_installs") return [
    { key: "license_status_label", label: "상태", value: (row) => row.license_status_label || "확인필요", render: (row) => softwareLicenseBadge(row.license_status_label) },
    { key: "software_name", label: "소프트웨어", value: (row) => row.software_name || "-", render: (row) => `<strong>${html(row.software_name || "-")}</strong><br><span class="muted mono">${html(row.sw_id || "")}</span>` },
    { key: "user_name", label: "사용자", value: (row) => `${row.user_name || row.user_key || "-"} ${row.user_key || ""}`, render: (row) => `<strong>${html(row.user_name || row.user_key || "-")}</strong><br><span class="muted mono">${html(row.user_key || "")}</span>` },
    { key: "dept_name", label: "부서", value: (row) => row.dept_name || "-", render: (row) => html(row.dept_name || "-") },
    { key: "asset_tag", label: "자산", className: "mono", value: (row) => row.asset_tag || row.equip_name || row.equip_code || "-", render: (row) => html(row.asset_tag || row.equip_name || row.equip_code || "-") },
    { key: "install_date", label: "설치일", className: "mono", value: (row) => compact(row.install_date || row.report_date || row.collected_at) || "-", render: (row) => html(compact(row.install_date || row.report_date || row.collected_at || "")) },
  ];
  if (mode === "software_users") return [
    { key: "unlicensed_count", label: "라이선스 확인", className: "mono", value: (row) => number(row.unlicensed_count), render: (row) => row.unlicensed_count ? softwareRiskBadge(`라이선스 확인 ${number(row.unlicensed_count)}`) : softwareRiskBadge("정상") },
    { key: "user_name", label: "사용자", value: (row) => `${row.user_name || row.user_key || "-"} ${row.user_key || ""}`, render: (row) => `<strong>${html(row.user_name || row.user_key || "-")}</strong><br><span class="muted mono">${html(row.user_key || "")}</span>` },
    { key: "dept_name", label: "부서", value: (row) => row.dept_name || "-", render: (row) => html(row.dept_name || "-") },
    { key: "install_count", label: "설치", className: "mono", value: (row) => number(row.install_count), render: (row) => number(row.install_count) },
    { key: "software_count", label: "SW", className: "mono", value: (row) => number(row.software_count), render: (row) => number(row.software_count) },
    { key: "asset_count", label: "자산", className: "mono", value: (row) => number(row.asset_count), render: (row) => number(row.asset_count) },
    { key: "latest_seen_at", label: "최근", className: "mono", value: (row) => compact(row.latest_seen_at) || "-", render: (row) => html(compact(row.latest_seen_at || "")) },
  ];
  if (mode === "changes") return simpleColumns([
    ["changed_at", "일시"], ["asset_tag", "자산"], ["action", "작업"], ["field", "항목"], ["old_value", "이전"], ["new_value", "변경"], ["actor", "작업자"],
  ]);
  if (mode === "jira_missing") return simpleColumns([
    ["asset_tag", "자산"], ["jira_issue", "Jira"], ["jira_lane_label", "Jira 상태"], ["jira_owner", "Jira 담당"], ["source_status", "소스"], ["last_sync_at", "동기화"],
  ]);
  return [];
}

function filterValue(value) {
  const text = String(value ?? "").trim();
  return text || "-";
}

function normalizeText(value) {
  return String(value ?? "").toLocaleLowerCase("ko-KR").replace(/\s+/g, " ").trim();
}

function tableFilterValues(rows, column) {
  const counts = new Map();
  rows.forEach((row) => {
    const value = filterValue(column.value(row));
    counts.set(value, (counts.get(value) || 0) + 1);
  });
  return [...counts.entries()]
    .map(([value, count]) => ({ value, count }))
    .sort((a, b) => a.value.localeCompare(b.value, "ko"));
}

function activeTableFilterSet(mode, key) {
  return new Set(state.tableFilters?.[mode]?.[key] || []);
}

function isTableFilterActive(mode, key) {
  return activeTableFilterSet(mode, key).size > 0;
}

function filterRowsByTableFilters(rows, columns, mode = state.mode) {
  const filters = state.tableFilters?.[mode] || {};
  const activeColumns = columns.filter((column) => Array.isArray(filters[column.key]) && filters[column.key].length);
  if (!activeColumns.length) return rows;
  return rows.filter((row) => activeColumns.every((column) => {
    const selected = new Set(filters[column.key]);
    return selected.has(filterValue(column.value(row)));
  }));
}

function filterRowsByQuery(rows, columns) {
  const query = normalizeText(state.query);
  if (!query) return rows;
  return rows.filter((row) => columns.some((column) => normalizeText(column.value(row)).includes(query)));
}

function filterBaseRowsForMode(rows, mode = state.mode) {
  const columns = tableColumnsForMode(mode);
  return rows;
}

function visibleRowsForMode(rows, mode = state.mode) {
  const columns = tableColumnsForMode(mode);
  const queryRows = filterBaseRowsForMode(rows, mode);
  if (!filterableTableModes.has(mode) || !columns.length) return queryRows;
  return filterRowsByTableFilters(queryRows, columns, mode);
}

function tableHeader(columns, mode) {
  return `
    <tr>
      ${columns.map((column) => `
        <th scope="col">
          <button type="button" class="table-filter-trigger ${isTableFilterActive(mode, column.key) ? "active" : ""}" data-filter-mode="${html(mode)}" data-table-filter="${html(column.key)}">
            <span>${html(column.label)}</span><small>▾</small>
          </button>
        </th>
      `).join("")}
    </tr>
  `;
}

function tableFilterPanel(rows, columns, mode) {
  const open = state.openTableFilter;
  if (!open || open.mode !== mode) return "";
  const column = columns.find((item) => item.key === open.key);
  if (!column) return "";
  const options = tableFilterValues(rows, column);
  const selected = activeTableFilterSet(mode, column.key);
  const isAll = selected.size === 0;
  return `
    <div class="table-filter-popover" role="dialog" aria-label="${html(column.label)} 필터">
      <div class="table-filter-head">
        <strong>${html(column.label)} 필터</strong>
        <button type="button" class="icon-button" data-filter-close title="닫기">×</button>
      </div>
      <div class="table-filter-actions">
        <button type="button" class="btn subtle" data-filter-clear data-filter-mode="${html(mode)}" data-filter-key="${html(column.key)}">전체 선택</button>
        <span>${number(options.length)}개 값</span>
      </div>
      <div class="table-filter-options">
        ${options.map((option) => `
          <label>
            <input type="checkbox" ${isAll || selected.has(option.value) ? "checked" : ""} data-filter-toggle data-filter-mode="${html(mode)}" data-filter-key="${html(column.key)}" data-filter-value="${html(option.value)}" />
            <span>${html(option.value)}</span>
            <em>${number(option.count)}</em>
          </label>
        `).join("")}
      </div>
    </div>
  `;
}

function toggleTableFilter(mode, key, value) {
  const columns = tableColumnsForMode(mode);
  const column = columns.find((item) => item.key === key);
  if (!column) return;
  const allValues = tableFilterValues(filterBaseRowsForMode(state.rows, mode), column).map((item) => item.value);
  const nextModeFilters = { ...(state.tableFilters[mode] || {}) };
  let selected = new Set(nextModeFilters[key] || []);
  if (!selected.size) {
    selected = new Set(allValues);
  }
  if (selected.has(value)) selected.delete(value);
  else selected.add(value);
  if (!selected.size || selected.size === allValues.length) delete nextModeFilters[key];
  else nextModeFilters[key] = [...selected];
  state.tableFilters = { ...state.tableFilters, [mode]: nextModeFilters };
}

function clearTableFilter(mode, key) {
  const nextModeFilters = { ...(state.tableFilters[mode] || {}) };
  delete nextModeFilters[key];
  state.tableFilters = { ...state.tableFilters, [mode]: nextModeFilters };
}

function endpointForMode() {
  const params = new URLSearchParams();
  if (state.query) params.set("q", state.query);
  const modeKind = modeAssetKinds[state.mode] || "";
  const effectiveKind = state.assetKind || modeKind;
  if (state.assetClass && assetScopedModes.includes(state.mode)) params.set("class", state.assetClass);
  if (effectiveKind && assetScopedModes.includes(state.mode)) params.set("kind", effectiveKind);
  if (state.ownerFilter && assetScopedModes.includes(state.mode)) params.set("owner", state.ownerFilter);
  if (state.assetPrefix && assetScopedModes.includes(state.mode)) {
    params.set("asset_prefix", state.assetPrefix);
  }
  if (state.dupTagOnly && assetScopedModes.includes(state.mode)) params.set("dup_tag", "1");
  if (state.dupSerialOnly && assetScopedModes.includes(state.mode)) params.set("dup_serial", "1");
  if (state.mode === "sources" && state.sourceIncludeHidden) params.set("include_hidden", "1");
  if (state.mode === "changes") {
    if (state.changeAsset) params.set("asset_tag", state.changeAsset);
    if (state.changeSource) params.set("source", state.changeSource);
    if (state.changeField) params.set("field", state.changeField);
  }
  if (state.mode === "in_use") {
    params.set("usage", "사용중");
    return `/api/assets?${params.toString()}`;
  }
  if (state.mode === "unused") {
    params.set("usage", "미사용");
    return `/api/assets?${params.toString()}`;
  }
  if (state.mode === "retired") {
    params.set("usage", "종료/제외");
    return `/api/assets?${params.toString()}`;
  }
  if (state.mode === "notebook_refresh") {
    params.set("lifecycle", "notebook_refresh");
    return `/api/assets?${params.toString()}`;
  }
  if (["servers", "network", "notebooks", "other_assets"].includes(state.mode)) {
    params.set("usage", "운영대상");
  }
  if (state.mode === "needs_review") {
    params.set("usage", "확인필요");
    return `/api/assets?${params.toString()}`;
  }
  if (state.mode === "approvals") {
    const approvalParams = new URLSearchParams();
    approvalParams.set("filter", state.approvalFilter || "my");
    if (state.query) approvalParams.set("q", state.query);
    return `/api/approvals?${approvalParams.toString()}`;
  }
  const map = {
    assets: "/api/assets",
    servers: "/api/assets",
    network: "/api/assets",
    notebooks: "/api/assets",
    notebook_refresh: "/api/assets",
    other_assets: "/api/assets",
    asset_groups: "/api/asset-groups",
    owners: "/api/owners",
    locations: "/api/locations",
    maintenance: "/api/maintenance",
    software_inventory: "/api/software-inventory",
    software_installs: "/api/software-installs",
    software_users: "/api/software-users",
    software_expirations: "/api/software-expirations",
    changes: "/api/changes",
    sources: "/api/sources",
    jira_missing: "/api/jira-missing-assets",
  };
  const base = map[state.mode] || "/api/assets";
  return `${base}${params.toString() ? `?${params.toString()}` : ""}`;
}

async function loadRows(opts = {}) {
  try {
    return await _loadRowsImpl(opts);
  } finally {
    state.loading = false;
  }
}

async function _loadRowsImpl({ renderLoading = true } = {}) {
  state.loading = true;
  if (renderLoading) render();
  if (state.mode === "dashboard") {
    const [changes, approvals] = await Promise.all([
      api("/api/changes"),
      api("/api/approvals?filter=all"),
    ]);
    state.rows = Array.isArray(changes) ? changes : [];
    state.dashboardApprovals = approvals || null;
    state.approvalMeta = approvals || null;
    state.detail = null;
    state.loading = false;
    return;
  }
  const payload = await api(endpointForMode());
  state.approvalMeta = state.mode === "approvals" ? payload : null;
  state.rows = state.mode === "approvals" ? payload.items || [] : payload;
  if (await fallbackToJiraMissing()) {
    state.loading = false;
    return;
  }
  if (state.mode === "approvals") {
    const visibleRows = visibleRowsForMode(state.rows, "approvals");
    const firstApproval = visibleRows.find((row) => row.docId)?.docId || "";
    const selectedInRows = visibleRows.some((row) => row.docId && row.docId === state.selectedApproval);
    if (firstApproval && (!state.selectedApproval || !selectedInRows)) state.selectedApproval = firstApproval;
    if (!firstApproval) state.selectedApproval = "";
    state.loading = false;
    return;
  }
  if (state.mode === "software_inventory") {
    const visibleRows = visibleRowsForMode(state.rows, "software_inventory");
    const firstSoftware = visibleRows.find((row) => row.sw_id)?.sw_id || "";
    const selectedInRows = visibleRows.some((row) => row.sw_id && row.sw_id === state.selectedSoftware);
    if (firstSoftware && (!state.selectedSoftware || !selectedInRows)) state.selectedSoftware = firstSoftware;
    if (!firstSoftware) state.selectedSoftware = "";
    state.loading = false;
    return;
  }
  if (state.mode === "software_users") {
    const visibleRows = visibleRowsForMode(state.rows, "software_users");
    const firstUser = visibleRows.find((row) => row.user_key)?.user_key || "";
    const selectedInRows = visibleRows.some((row) => row.user_key && row.user_key === state.selectedSoftwareUser);
    if (firstUser && (!state.selectedSoftwareUser || !selectedInRows)) state.selectedSoftwareUser = firstUser;
    if (!firstUser) state.selectedSoftwareUser = "";
    state.loading = false;
    return;
  }
  const visibleRows = visibleRowsForMode(state.rows, state.mode);
  const firstAsset = visibleRows.find((row) => row.asset_tag)?.asset_tag || "";
  const selectedInRows = visibleRows.some((row) => row.asset_tag && row.asset_tag === state.selectedAsset);
  if (firstAsset && (!state.selectedAsset || !selectedInRows)) state.selectedAsset = firstAsset;
  if (!firstAsset) state.selectedAsset = "";
  state.loading = false;
}

async function fallbackToJiraMissing() {
  if (!state.query || !assetScopedModes.includes(state.mode) || state.rows.length) return false;
  const rows = await api(`/api/jira-missing-assets?q=${encodeURIComponent(state.query)}`);
  if (!Array.isArray(rows) || !rows.length) return false;
  state.mode = "jira_missing";
  state.assetKind = "";
  state.assetClass = "";
  state.assetPrefix = "";
  state.ownerFilter = "";
  state.dupTagOnly = false;
  state.dupSerialOnly = false;
  state.rows = rows;
  state.selectedAsset = rows.find((row) => row.asset_tag)?.asset_tag || "";
  state.detail = null;
  return true;
}

async function loadDetail(assetTag = state.selectedAsset) {
  if (state.mode === "dashboard") {
    state.detail = null;
    return;
  }
  if (state.mode === "approvals") {
    return loadApprovalDetail(state.selectedApproval);
  }
  if (state.mode === "jira_missing") {
    const record = state.rows.find((row) => row.asset_tag && row.asset_tag === assetTag) || state.rows[0] || null;
    state.selectedAsset = record?.asset_tag || "";
    state.detail = record ? { jira_missing: record } : null;
    return;
  }
  if (state.mode === "software_inventory") {
    const record = state.rows.find((row) => row.sw_id && row.sw_id === state.selectedSoftware) || state.rows[0] || null;
    state.selectedSoftware = record?.sw_id || "";
    if (!record) {
      state.detail = null;
      return;
    }
    const payload = await api(`/api/software-installs?sw_id=${encodeURIComponent(record.sw_id)}&limit=500`);
    state.detail = { software: record, installs: Array.isArray(payload) ? payload : [] };
    return;
  }
  if (state.mode === "software_users") {
    const record = state.rows.find((row) => row.user_key && row.user_key === state.selectedSoftwareUser) || state.rows[0] || null;
    state.selectedSoftwareUser = record?.user_key || "";
    if (!record) {
      state.detail = null;
      return;
    }
    const payload = await api(`/api/software-installs?user=${encodeURIComponent(record.user_key)}&limit=500`);
    state.detail = { softwareUser: record, installs: Array.isArray(payload) ? payload : [] };
    return;
  }
  if (!assetTag) {
    state.detail = null;
    return;
  }
  state.selectedAsset = assetTag;
  state.detail = await api(`/api/asset?asset_tag=${encodeURIComponent(assetTag)}`);
}

async function loadApprovalDetail(docId = state.selectedApproval) {
  if (!docId) {
    state.detail = null;
    return;
  }
  state.selectedApproval = docId;
  writeApprovalUrl(docId);
  state.detail = await api(`/api/approval-workflow?doc_id=${encodeURIComponent(docId)}`);
}

async function syncSelectionToVisibleRows(mode = state.mode) {
  if (mode !== state.mode) return;
  const visibleRows = visibleRowsForMode(state.rows, mode);
  if (mode === "approvals") {
    const firstApproval = visibleRows.find((row) => row.docId)?.docId || "";
    const selectedInRows = visibleRows.some((row) => row.docId && row.docId === state.selectedApproval);
    if (firstApproval && (!state.selectedApproval || !selectedInRows)) await loadApprovalDetail(firstApproval);
    if (!firstApproval) {
      state.selectedApproval = "";
      state.detail = null;
    }
    return;
  }
  if (mode === "software_inventory") {
    const firstSoftware = visibleRows.find((row) => row.sw_id)?.sw_id || "";
    const selectedInRows = visibleRows.some((row) => row.sw_id && row.sw_id === state.selectedSoftware);
    if (firstSoftware && (!state.selectedSoftware || !selectedInRows)) {
      state.selectedSoftware = firstSoftware;
      await loadDetail();
    }
    if (!firstSoftware) {
      state.selectedSoftware = "";
      state.detail = null;
    }
    return;
  }
  if (mode === "software_users") {
    const firstUser = visibleRows.find((row) => row.user_key)?.user_key || "";
    const selectedInRows = visibleRows.some((row) => row.user_key && row.user_key === state.selectedSoftwareUser);
    if (firstUser && (!state.selectedSoftwareUser || !selectedInRows)) {
      state.selectedSoftwareUser = firstUser;
      await loadDetail();
    }
    if (!firstUser) {
      state.selectedSoftwareUser = "";
      state.detail = null;
    }
    return;
  }
  const firstAsset = visibleRows.find((row) => row.asset_tag)?.asset_tag || "";
  const selectedInRows = visibleRows.some((row) => row.asset_tag && row.asset_tag === state.selectedAsset);
  if (firstAsset && (!state.selectedAsset || !selectedInRows)) await loadDetail(firstAsset);
  if (!firstAsset && visibleRows.some((row) => Object.prototype.hasOwnProperty.call(row, "asset_tag"))) {
    state.selectedAsset = "";
    state.detail = null;
  }
}

async function refreshQuality() {
  const dataQuality = await api("/api/data-quality");
  state.dataQuality = dataQuality || { cards: [], triage: [], counts: {}, sources: [] };
  render({ preserveScroll: true });
}

async function refreshAll() {
  try {
    state.loading = true;
    render();
    const insightsPromise = api("/api/insights");
    const rowsPromise = loadRows({ renderLoading: false });
    const insights = await insightsPromise;
    await rowsPromise;
    state.summary = (insights && insights.summary) || {};
    state.insights = insights || {};
    state.lastUpdated = new Date().toLocaleString("ko-KR", { hour12: false, timeZone: "Asia/Seoul" });
    await loadDetail();
    render();
    refreshQuality();
  } catch (error) {
    document.getElementById("app").innerHTML = `<div class="error">로드 실패: ${html(error.message)}</div>`;
  }
}

function sidebar() {
  return `
    <aside class="sidebar">
      <button type="button" class="brand brand-home" data-mode="dashboard" aria-label="대시보드 홈">
        <div class="brand-menu">☰</div>
        <div>
          <strong>ACME</strong>
          <span>IT 자산 통합관리</span>
        </div>
      </button>
      <nav class="nav" aria-label="주요 메뉴">
        ${nav.map((section) => `
          <div class="nav-section">
            <div class="nav-title">${html(section.title)}</div>
            ${section.items.map(([id, label, code]) => `
              <button type="button" data-mode="${id}" class="${state.mode === id ? "active" : ""}">
                <small>${html(code)}</small>
                <span>${html(label)}</span>
                ${navCount(id)}
              </button>
            `).join("")}
          </div>
        `).join("")}
      </nav>
      <div class="sidebar-footer">
        <div class="security-card">
          <span class="security-icon">◇</span>
          <div>
            <strong>보안 & 인증</strong>
            <small>${html(integrationLabel("oidc"))}</small>
          </div>
          <span class="security-info">i</span>
        </div>
      </div>
    </aside>
  `;
}

function navCount(id) {
  const map = {
    dashboard: "assets",
    assets: "assets",
    asset_groups: "asset_groups",
    servers: "asset_categories.server.operational",
    network: "asset_categories.network.operational",
    notebooks: "asset_categories.notebook.operational",
    notebook_refresh: "notebook_replacement_due",
    other_assets: "asset_categories.other.operational",
    in_use: "in_use",
    unused: "unused",
    needs_review: "needs_review",
    owners: "owners",
    locations: "locations",
    changes: "changes",
    sources: "sources",
    jira_missing: "jira_missing_assets",
    software_expirations: "software_expirations",
    software_inventory: "software_inventory",
    software_installs: "software_inventory_installs",
    software_users: "software_users",
    approvals: "approvals",
  };
  if (id === "jira_missing") return `<small>${number(state.dataQuality?.counts?.jira_missing_assets || 0)}</small>`;
  if (id === "software_expirations") return `<small>${number(state.dataQuality?.counts?.software_expiring_soon || 0)}</small>`;
  if (id === "software_inventory") return `<small>${number(state.dataQuality?.counts?.software_inventory || state.summary.software_inventory || 0)}</small>`;
  if (id === "software_installs") return `<small>${number(state.summary.software_inventory_installs || state.dataQuality?.counts?.software_inventory_installs || 0)}</small>`;
  if (id === "software_users") return `<small>${number(state.dataQuality?.counts?.software_users || state.summary.software_users || 0)}</small>`;
  const key = map[id];
  if (!key) return "";
  const value = key.includes(".") ? key.split(".").reduce((acc, part) => acc?.[part], state.summary) : state.summary[key];
  return `<small>${number(value)}</small>`;
}

function sourceOk() {
  const rows = state.insights.sources || [];
  return rows.length > 0 && rows.every((item) => ["imported", "read-only", "ok", "configured", "local-outbox"].includes(item.status));
}

function integrationSource(id) {
  return (state.insights.sources || []).find((item) => item.id === id)
    || (state.dataQuality?.sources || []).find((item) => item.id === id)
    || {};
}

function integrationLabel(id) {
  const source = integrationSource(id);
  if (source.status === "configured") return "연동됨";
  if (source.status === "local-outbox") return "내부 outbox (미전송)";
  if (source.status === "not_configured") return "설정 필요";
  return source.status || "확인";
}

function alertCount() {
  const counts = state.dashboardApprovals?.summary?.counts || {};
  return Number(counts.dispatch_pending || 0) + Number(counts.reference_unread || 0) + Number(state.summary.needs_review || 0);
}

function qualityStrip() {
  const cards = Array.isArray(state.dataQuality?.cards) ? state.dataQuality.cards : [];
  const triage = Array.isArray(state.dataQuality?.triage) ? state.dataQuality.triage : [];
  if (!cards.length && !triage.length) return "";
  return `
    <section class="quality-strip" aria-label="운영 품질 신호">
      <div class="quality-head">
        <div>
          <p class="eyebrow">DATA QUALITY</p>
          <h2>운영 품질</h2>
        </div>
        <span>${html(compact(state.dataQuality?.generated_at || state.lastUpdated))}</span>
      </div>
      <div class="quality-card-grid">
        ${cards.map((card) => `
          <button type="button" class="quality-card ${html(card.tone || "ok")}" data-mode="${html(card.target_mode || "dashboard")}" data-filter='${html(JSON.stringify(card.target_filter || {}))}'>
            <span>${html(card.label || "")}</span>
            <strong>${html(card.value || "-")}</strong>
            <small>${html(card.detail || "")}</small>
          </button>
        `).join("")}
      </div>
      <div class="quality-triage">
        ${triage.map((item) => `
          <button type="button" class="triage-chip ${html(item.tone || "ok")}" data-mode="${html(item.target_mode || "dashboard")}" data-filter='${html(JSON.stringify(item.target_filter || {}))}' title="${html(item.description || "")}">
            <span>${html(item.label || "")}</span>
            <strong>${number(item.count)}</strong>
          </button>
        `).join("")}
      </div>
    </section>
  `;
}

function modeNote() {
  const counts = state.dataQuality?.counts || {};
  if (state.mode === "assets" && Number(counts.assets || 0) > Number(counts.asset_api_limit || 50000)) {
    return ` · 기본 ${number(counts.asset_api_limit || 50000)}건 표시 / 전체 ${number(counts.assets)}건`;
  }
  if (state.mode === "changes" && Number(counts.changes || 0) > Number(counts.change_api_limit || 1000)) {
    return ` · 최신 ${number(counts.change_api_limit || 1000)}건 표시 / 전체 ${number(counts.changes)}건`;
  }
  if (state.mode === "in_use" && Number(state.summary.jira_only_in_use || 0) > 0) {
    return ` · Jira-only 사용중 ${number(state.summary.jira_only_in_use)}건 포함`;
  }
  if (state.mode === "maintenance") return " · 현재 유지보수 데이터 소스 없음";
  if (state.mode === "software_inventory") {
    const swTotal = Number(counts.software_inventory || 0);
    const swLimit = Number(counts.software_api_limit || 0);
    const base = ` · 라이선스 확인 ${number(counts.software_unlicensed_installs || 0)}건`;
    return swLimit > 0 && swTotal > swLimit ? `${base} · 기본 ${number(swLimit)}건 표시 / 전체 ${number(swTotal)}건` : base;
  }
  if (state.mode === "software_users") return ` · 사용자 ${number(counts.software_users || state.summary.software_users || 0)}명`;
  if (state.mode === "software_expirations") return ` · 만료/임박 ${number(counts.software_expiring_soon || 0)}개`;
  if (state.mode === "sources") return state.sourceIncludeHidden ? ` · 감사용 전체 · stale ${number(counts.stale_sources || 0)}개` : ` · 현재 유효한 연동만`;
  if (state.mode === "jira_missing") return " · Jira에는 있으나 자산 DB에는 없음";
  return "";
}

function topbar() {
  const mode = modeLabels[state.mode] || "운영 콘솔";
  const assetScopeActive = state.assetPrefix && assetScopedModes.includes(state.mode);
  const ownerScopeActive = state.ownerFilter && assetScopedModes.includes(state.mode);
  const kindScopeActive = state.assetKind && state.mode === "assets";
  const classScopeActive = state.assetClass && assetScopedModes.includes(state.mode);
  const alerts = alertCount();
  return `
    <header class="topbar">
      <h1 class="sr-only">${html(mode)}</h1>
      <div class="top-actions">
        <label for="search" class="sr-only">자산, 사용자, IP, 랙 검색</label>
        <input id="search" class="search" type="search" autocomplete="off" value="${html(state.query)}" placeholder="자산 · 사용자 · IP · 랙 검색" aria-label="자산, 사용자, IP, 랙 검색" />
        ${assetScopeActive ? `<button type="button" class="btn subtle" data-action="clear-prefix">번호대 해제</button>` : ""}
        ${ownerScopeActive ? `<button type="button" class="btn subtle" data-action="clear-owner-filter">${html(state.ownerFilter)} 해제</button>` : ""}
        ${kindScopeActive ? `<button type="button" class="btn subtle" data-action="clear-kind-filter">${html(assetKindLabels[state.assetKind] || state.assetKind)} 해제</button>` : ""}
        ${classScopeActive ? `<button type="button" class="btn subtle" data-action="clear-class-filter">${html(assetClassLabels[state.assetClass] || state.assetClass)} 해제</button>` : ""}
        ${state.dupTagOnly && assetScopedModes.includes(state.mode) ? `<button type="button" class="btn subtle" data-action="clear-dup-tag">중복 자산번호 해제</button>` : ""}
        ${state.dupSerialOnly && assetScopedModes.includes(state.mode) ? `<button type="button" class="btn subtle" data-action="clear-dup-serial">중복 시리얼 해제</button>` : ""}
      </div>
      <div class="top-profile">
        ${themeSelector()}
        <button type="button" class="notice-bell" data-action="show-alerts" aria-label="알림 ${number(alerts)}건">⌕${alerts ? `<sup>${number(alerts)}</sup>` : ""}</button>
        <button type="button" class="profile-avatar" data-action="show-owners" aria-label="담당자 화면">운</button>
        <div class="profile-copy"><strong>운영자</strong><span>IT 자산 콘솔</span></div>
        <button type="button" class="profile-caret" data-action="show-owners" aria-label="담당자 화면">⌄</button>
      </div>
    </header>
  `;
}

function categoryCards() {
  const categories = state.summary.asset_categories || {};
  const cards = [
    { mode: "servers", label: "서버", icon: "▤", data: categories.server || {} },
    { mode: "network", label: "네트워크", icon: "⌘", data: categories.network || {} },
    { mode: "notebooks", label: "노트북", icon: "▭", data: categories.notebook || {} },
    { mode: "other_assets", label: "기타자산", icon: "◇", data: categories.other || {} },
  ];
  return `
    <section class="dashboard-cards">
      ${cards.map((card) => `
        <button type="button" class="dash-card" data-mode="${card.mode}">
          <span class="dash-icon">${html(card.icon)}</span>
          <span class="dash-chevron">›</span>
          <span class="dash-label">${html(card.label)}</span>
          <strong>${number(card.data.operational)}<small>대</small></strong>
          <span class="dash-status"><i class="dot ok-dot"></i>${number(card.data.active)} Active</span>
          <span class="dash-status"><i class="dot idle-dot"></i>${number(card.data.idle)} Idle</span>
          <span class="dash-status"><i class="dot issue-dot"></i>${number(card.data.issue)} Issue</span>
          ${card.mode === "notebooks" && Number(state.summary.notebook_replacement_due || 0) ? `<span class="dash-status"><i class="dot issue-dot"></i>${number(state.summary.notebook_replacement_due)} 교체/매각</span>` : ""}
          ${Number(card.data.retired || 0) ? `<span class="dash-status"><i class="dot idle-dot"></i>${number(card.data.retired)} 종료/제외</span>` : ""}
        </button>
      `).join("")}
    </section>
  `;
}

function usageBar(label, value, total, tone) {
  const percent = Math.min(Math.round((Number(value || 0) / Math.max(Number(total || 0), 1)) * 100), 100);
  return `
    <div class="usage-row">
      <div><span>${html(label)}</span><strong>${number(value)} · ${percent}%</strong></div>
      <i class="${html(tone)}" style="width:${percent}%"></i>
    </div>
  `;
}

function qualityOverviewCard(total) {
  const counts = state.dataQuality?.counts || {};
  const jiraOnly = Number(state.summary.jira_only_in_use || 0);
  const dbInUse = Math.max(Number(state.summary.in_use || 0) - jiraOnly, 0);
  return `
    <div class="trend-card usage-card">
      <div class="trend-head">
        <h3>사용 판정 및 데이터 품질</h3>
        <span>전체 ${number(total)}대</span>
      </div>
      <div class="usage-bars">
        ${usageBar("DB 사용중", dbInUse, total, "ok")}
        ${usageBar("미사용", state.summary.unused, total, "idle")}
        ${usageBar("종료/제외", state.summary.retired, total, "idle")}
        ${usageBar("확인필요", state.summary.needs_review, total, "warn")}
      </div>
      ${jiraOnly ? `<p class="muted">Jira-only 사용중 ${number(jiraOnly)}건은 자산 DB 밖 항목이라 합계에서 분리됩니다.</p>` : ""}
      <div class="quality-summary-grid">
        <button type="button" data-mode="needs_review"><span>Jira/NAC 충돌</span><strong>${number(counts.jira_nac_conflicts || 0)}</strong></button>
        <button type="button" data-mode="jira_missing"><span>Jira-only</span><strong>${number(counts.jira_missing_assets || 0)}</strong></button>
        <button type="button" data-mode="sources" data-filter='${html(JSON.stringify({ include_hidden: "1" }))}'><span>지연 소스</span><strong>${number(counts.stale_sources || 0)}</strong></button>
        <button type="button" data-mode="software_expirations"><span>SW 만료</span><strong>${number(counts.software_expiring_soon || 0)}</strong></button>
        <button type="button" data-mode="assets" data-filter='${html(JSON.stringify({ dup_tag: "1" }))}'><span>중복 자산번호</span><strong>${number(counts.duplicate_asset_tags || 0)}</strong></button>
        <button type="button" data-mode="changes"><span>반복 변경</span><strong>${number(counts.repeated_jira_today || 0)}</strong></button>
      </div>
    </div>
  `;
}

function dashboardOverview() {
  const total = Number(state.summary.operational_assets || 0);
  const categories = state.summary.asset_categories || {};
  const servers = Number(categories.server?.operational || 0);
  const network = Number(categories.network?.operational || 0);
  const notebooks = Number(categories.notebook?.operational || 0);
  const others = Number(categories.other?.operational || 0);
  const approvalSummary = state.dashboardApprovals?.summary?.counts || {};
  const approvalProgress = state.summary.approval_progress || {};
  const pendingApprovals = (state.dashboardApprovals?.items || []).filter((item) => !item.sourceStatus?.done);
  const reviewAssets = state.insights.review_assets || [];
  const reviewTotal = Number(state.summary.needs_review || 0);
  const qualityCounts = state.dataQuality?.counts || {};
  const progress = Number(approvalProgress.percent || 0);
  return `
    <section class="dashboard-main-grid">
      <section class="dashboard-primary">
        <article class="dash-panel asset-overview-panel">
          <div class="dash-panel-head">
            <h2>자산 현황 개요</h2>
            <button type="button" class="period-button" data-mode="assets">전체 자산</button>
          </div>
          <div class="asset-overview-body">
            <div class="donut-wrap">
              <div class="donut-chart" style="--server:${Math.round((servers / Math.max(total, 1)) * 100)}%;--network:${Math.round((network / Math.max(total, 1)) * 100)}%;--notebook:${Math.round((notebooks / Math.max(total, 1)) * 100)}%;">
                <span>${number(total)}<small>운영 자산</small></span>
              </div>
              <div class="donut-legend">
                <div><i class="legend-server"></i><span>서버</span><strong>${number(servers)} (${Math.round((servers / Math.max(total, 1)) * 100)}%)</strong></div>
                <div><i class="legend-network"></i><span>네트워크</span><strong>${number(network)} (${Math.round((network / Math.max(total, 1)) * 100)}%)</strong></div>
                <div><i class="legend-notebook"></i><span>노트북</span><strong>${number(notebooks)} (${Math.round((notebooks / Math.max(total, 1)) * 100)}%)</strong></div>
                <div><i class="legend-other"></i><span>기타자산</span><strong>${number(others)} (${Math.round((others / Math.max(total, 1)) * 100)}%)</strong></div>
              </div>
            </div>
            ${qualityOverviewCard(total)}
          </div>
        </article>
        <article class="dash-panel recent-panel">
        <div class="dash-panel-head"><h2>최근 변경/등록 자산</h2><button type="button" class="link-button" data-mode="changes">전체 보기 ›</button></div>
          ${recentChangesTable(state.rows.slice(0, 5))}
        </article>
      </section>

      <aside class="dashboard-side">
        ${statusMiniPanel("전자결재 현황", [
          [number(approvalSummary.reference_unread || 0), "미열람", "blue"],
          [number(approvalSummary.dispatch_pending || 0), "미시행", "orange"],
          [number(approvalSummary.source_completed || 0), "열람/시행 완료", "green"],
          [number(approvalSummary.total || state.summary.approvals || 0), "전체", "slate"],
        ], "approvals")}
        ${operatorQueuePanel(qualityCounts, reviewTotal)}
        <article class="dash-panel progress-panel">
          <h2>내 업무 진행률</h2>
          <div class="progress-body">
            <div class="progress-ring" style="--value:${progress}%"><strong>${progress}%</strong><span>완료율</span></div>
            <div class="progress-list">
              <div><span>완료</span><strong>${number(approvalProgress.completed)}</strong></div>
              <div><span>진행중</span><strong>${number(approvalProgress.in_progress)}</strong></div>
              <div><span>대기</span><strong>${number(approvalProgress.waiting)}</strong></div>
              <div><span>전체</span><strong>${number(approvalProgress.total)}</strong></div>
            </div>
          </div>
        </article>
      </aside>
    </section>

    <section class="dashboard-lower-grid">
      <article class="dash-panel todo-panel">
        <div class="dash-panel-head"><h2>전자결재 처리 큐 <em>${number(pendingApprovals.length)}</em></h2><button type="button" class="link-button" data-mode="approvals">전체 보기 ›</button></div>
        ${approvalTodoTable(pendingApprovals.slice(0, 5))}
      </article>
      <article class="dash-panel todo-panel">
        <div class="dash-panel-head"><h2>확인필요 자산 큐 <em>${number(reviewTotal)}</em></h2><button type="button" class="btn-add" data-mode="needs_review">열기</button></div>
        ${reviewTodoTable(reviewAssets.slice(0, 3))}
      </article>
    </section>
  `;
}

function statusMiniPanel(title, items, targetMode) {
  return `
    <article class="dash-panel mini-status-panel">
      <div class="mini-status-head"><h2>${html(title)}</h2><button type="button" class="link-button" data-mode="${targetMode}">전체 ›</button></div>
      <div class="mini-status-grid">
        ${items.map(([value, label, tone]) => `
          <button type="button" class="mini-status-item ${tone}" data-mode="${targetMode}">
            <strong>${html(value)}</strong>
            <span>${html(label)}</span>
          </button>
        `).join("")}
      </div>
    </article>
  `;
}

function operatorQueuePanel(counts, reviewTotal) {
  const items = [
    { value: reviewTotal, label: "확인필요", tone: "blue", mode: "needs_review" },
    { value: counts.jira_nac_conflicts || 0, label: "Jira/NAC", tone: "orange", mode: "needs_review" },
    { value: counts.jira_missing_assets || 0, label: "Jira-only", tone: "red", mode: "jira_missing" },
    { value: counts.stale_sources || 0, label: "지연 소스", tone: "cyan", mode: "sources", filter: { include_hidden: "1" } },
    { value: counts.software_expiring_soon || 0, label: "SW 만료", tone: "slate", mode: "software_expirations" },
  ];
  return `
    <article class="dash-panel mini-status-panel operator-queue-panel">
      <div class="mini-status-head"><h2>운영 큐</h2><button type="button" class="link-button" data-mode="needs_review">확인 ›</button></div>
      <div class="mini-status-grid">
        ${items.map((item) => `
          <button type="button" class="mini-status-item ${html(item.tone)}" data-mode="${html(item.mode)}" data-filter='${html(JSON.stringify(item.filter || {}))}'>
            <strong>${number(item.value)}</strong>
            <span>${html(item.label)}</span>
          </button>
        `).join("")}
      </div>
    </article>
  `;
}

function recentChangesTable(rows) {
  if (!rows.length) return `<div class="empty">최근 변경 이력이 없습니다.</div>`;
  return `
    <table class="dashboard-table">
      <thead><tr><th scope="col">자산</th><th scope="col">변경 항목</th><th scope="col">이전 값</th><th scope="col">변경 값</th><th scope="col">작업자</th><th scope="col">변경일시</th></tr></thead>
      <tbody>
        ${rows.map((row) => `
          <tr data-asset="${html(row.asset_tag || "")}">
            <td class="asset-link">${html(row.asset_tag || "-")}</td>
            <td>${html(row.field || row.action || "-")}</td>
            <td>${html(row.old_value || "-")}</td>
            <td>${html(row.new_value || "-")}</td>
            <td>${html(row.actor || "-")}</td>
            <td>${html(compact(row.changed_at || row.updated_at || ""))}</td>
          </tr>
        `).join("")}
      </tbody>
    </table>
  `;
}

function approvalTodoTable(rows) {
  if (!rows.length) return `<div class="empty">대기 중인 전자결재 항목이 없습니다.</div>`;
  return `
    <table class="dashboard-table todo-table">
      <thead><tr><th scope="col">결재 문서</th><th scope="col">요청자</th><th scope="col">업무 내용</th><th scope="col">마감일</th><th scope="col">우선순위</th><th scope="col">상태</th></tr></thead>
      <tbody>
        ${rows.map((row) => `
          <tr data-approval="${html(row.docId || "")}">
            <td class="asset-link">${html(row.docTitle || row.docNo || "-")}</td>
            <td>${html(row.createdBy || "-")}</td>
            <td>${html(row.formName || row.theme || "전자결재 확인")}</td>
            <td>${html(compactApprovalTime(row.repDt || ""))}</td>
            <td><span class="priority high">높음</span></td>
            <td>${approvalSourceStatus(row)}</td>
          </tr>
        `).join("")}
      </tbody>
    </table>
  `;
}

function reviewTodoTable(rows) {
  if (!rows.length) return `<div class="empty">확인필요 자산이 없습니다.</div>`;
  const list = rows;
  return `
    <table class="dashboard-table todo-table">
      <thead><tr><th scope="col">업무 내용</th><th scope="col">갱신</th><th scope="col">우선순위</th><th scope="col">상태</th></tr></thead>
      <tbody>
        ${list.map((row) => `
          <tr data-asset="${html(row.asset_tag || "")}">
            <td>${html(row.asset_tag || row.hostname || "-")}</td>
            <td>${html(compact(row.updated_at || "").slice(0, 10) || "-")}</td>
            <td><span class="priority ${row.is_server ? "high" : "mid"}">${row.is_server ? "높음" : "보통"}</span></td>
            <td>${badge(row.usage_status || "확인필요")}</td>
          </tr>
        `).join("")}
      </tbody>
    </table>
  `;
}

function metrics() {
  const jiraOnly = Number(state.summary.jira_only_in_use || 0);
  const items = [
    ["assets", "전체", state.summary.assets, ""],
    ["in_use", jiraOnly ? "사용중(+Jira)" : "사용중", state.summary.in_use, "ok"],
    ["unused", "미사용", state.summary.unused, ""],
    ["retired", "종료/제외", state.summary.retired, ""],
    ["notebook_refresh", "교체/매각", state.summary.notebook_replacement_due, "warn"],
    ["servers", "서버", state.summary.servers, ""],
    ["needs_review", "확인필요", state.summary.needs_review, "warn"],
    ["sources", "연동", state.summary.sources, ""],
  ];
  return `<section class="metrics">${items.map(([mode, label, value, tone]) => `
    <button type="button" class="metric ${tone}" data-mode="${mode}">
      <strong>${number(value)}</strong>
      <span>${html(label)}</span>
    </button>
  `).join("")}</section>`;
}

function renderInsights() {
  const computerUse = state.insights.computer_use || {};
  const counts = state.dataQuality?.counts || {};
  const usage_reasons = state.insights.usage_reasons || [];
  const reviewCount = Number(state.summary.needs_review || 0);
  return `
    <section id="insights" class="panel decision decision-band">
      <div>
        <p class="eyebrow">QUEUE</p>
        <h2>${reviewCount ? `${number(reviewCount)} 확인필요` : "0 확인필요"}</h2>
      </div>
      <div class="decision-pills">
        <div class="pill ${computerUse.passed ? "ok" : "warn"}"><span>GUI</span><strong>${computerUse.passed ? "ACCEPT" : "WAIT"}</strong></div>
        <div class="pill ${sourceOk() ? "ok" : "warn"}"><span>연동</span><strong>${sourceOk() ? "정상" : "확인"}</strong></div>
        <div class="pill ${Number(counts.stale_sources) ? "warn" : "ok"}"><span>지연</span><strong>${number(counts.stale_sources)}</strong></div>
        <div class="pill ${Number(counts.duplicate_asset_tags) ? "warn" : "ok"}"><span>중복</span><strong>${number(counts.duplicate_asset_tags)}</strong></div>
      </div>
      <button type="button" class="btn primary" data-mode="needs_review">확인필요 열기</button>
      <div class="review-list" hidden>${usage_reasons.map((item) => html(item.reason || item.usage_reason || "")).join(" ")}</div>
    </section>
  `;
}

function mainContent() {
  if (state.loading) return `<div class="panel"><div class="empty">로드 중</div></div>`;
  if (state.mode === "asset_groups") return assetGroupView();
  if (state.mode === "locations") return rackMap();
  if (state.mode === "approvals") return approvalView();
  const title = panelTitle();
  return `
    <section class="panel">
      <div class="panel-head">
        <div>
          <h2>${html(title)}</h2>
          <span>${number(visibleRowsForMode(state.rows, state.mode).length)}건${state.ownerFilter ? ` · ${html(state.ownerFilter)}` : ""}${modeNote()}</span>
        </div>
        <div class="panel-actions">
          ${panelControls()}
          <button type="button" class="btn subtle" data-action="refresh-list">목록 새로고침</button>
        </div>
      </div>
      <div class="content-body">
        ${state.mode === "notebook_refresh" ? notebookRefreshOverview(visibleRowsForMode(state.rows, state.mode)) : ""}
        ${tableForMode()}
      </div>
    </section>
  `;
}

function panelControls() {
  if (state.mode === "sources") {
    return `
      <div class="segmented-control" role="group" aria-label="연동 표시 범위">
        <button type="button" class="${state.sourceIncludeHidden ? "subtle" : "primary"}" data-source-scope="active">현재</button>
        <button type="button" class="${state.sourceIncludeHidden ? "primary" : "subtle"}" data-source-scope="audit">감사용 전체</button>
      </div>
    `;
  }
  if (state.mode === "changes") {
    return `
      <form class="change-filter-bar" id="change-filter-form">
        <input name="asset_tag" value="${html(state.changeAsset)}" placeholder="자산번호" />
        <input name="source" value="${html(state.changeSource)}" placeholder="source" />
        <input name="field" value="${html(state.changeField)}" placeholder="항목" />
        <button class="btn primary" type="submit">필터</button>
        <button class="btn subtle" type="button" data-action="clear-change-filter">해제</button>
      </form>
    `;
  }
  return "";
}

function panelTitle() {
  const mode = modeLabels[state.mode] || "운영 콘솔";
  const parts = [mode];
  if (state.ownerFilter) parts.push(state.ownerFilter);
  if (state.assetClass) parts.push(assetClassLabels[state.assetClass] || state.assetClass);
  if (state.assetKind && state.mode === "assets") parts.push(assetKindLabels[state.assetKind] || state.assetKind);
  if (state.assetPrefix) parts.push(state.assetPrefix);
  return parts.join(" · ");
}

function tableForMode() {
  if (!state.rows.length) return emptyForMode();
  if (state.mode === "approvals") return approvalTable(state.rows);
  if (state.mode === "maintenance") return simpleTable(state.rows, [
    ["event_date", "일자"], ["asset_tag", "자산"], ["hostname", "호스트"], ["event_type", "유형"], ["status", "상태"], ["note", "내용"],
  ]);
  if (state.mode === "software_expirations") return simpleTable(state.rows, [
    ["expiry_status", "상태"], ["software_name", "소프트웨어"], ["expiration_date", "만료일"], ["days_left", "남은일"], ["requester", "신청자"], ["department", "부서"], ["docTitle", "문서"],
  ], false);
  if (state.mode === "software_inventory") return softwareInventoryTable(state.rows);
  if (state.mode === "software_installs") return filterableTable(state.rows, tableColumnsForMode("software_installs"), "software_installs", {
    className: "software-installs-table",
  });
  if (state.mode === "software_users") return softwareUsersTable(state.rows);
  if (state.mode === "changes") return simpleTable(state.rows, [
    ["changed_at", "일시"], ["asset_tag", "자산"], ["action", "작업"], ["field", "항목"], ["old_value", "이전"], ["new_value", "변경"], ["actor", "작업자"],
  ]);
  if (state.mode === "sources") return sourceTable(state.rows);
  if (state.mode === "jira_missing") return simpleTable(state.rows, [
    ["asset_tag", "자산"], ["jira_issue", "Jira"], ["jira_lane_label", "Jira 상태"], ["jira_owner", "Jira 담당"], ["source_status", "소스"], ["last_sync_at", "동기화"],
  ]);
  if (state.mode === "owners") return ownerTable(state.rows);
  return assetTable(state.rows);
}

function notebookRefreshOverview(rows) {
  const total = rows.length;
  const immediate = rows.filter((row) => row.notebook_refresh_priority === "즉시 검토").length;
  const planned = rows.filter((row) => row.notebook_refresh_priority === "교체 계획").length;
  const regular = rows.filter((row) => row.notebook_refresh_priority === "정기 교체").length;
  const info = rows.filter((row) => row.notebook_refresh_decision === "정보 확인" || String(row.notebook_refresh_flags || "").includes("확인")).length;
  const sale = rows.filter((row) => row.notebook_refresh_decision === "매각 검토").length;
  const estimated = rows.filter((row) => row.notebook_age_basis === "자산번호연도").length;
  return `
    <div class="notebook-refresh-overview" aria-label="노트북 교체 매각 판단 큐">
      <div class="refresh-overview-head">
        <div>
          <h3>판단 큐</h3>
          <span>4년 초과 노트북 ${number(total)}대 · 종료/제외 제외</span>
        </div>
        <strong>우선순위 ${number(immediate + planned + regular)}대</strong>
      </div>
      <div class="refresh-decision-sections">
        <div>
          <h4>우선순위</h4>
          <div class="refresh-decision-grid priority">
            <button type="button" class="refresh-decision-card urgent" data-table-filter="notebook_refresh_priority" data-filter-mode="notebook_refresh">
              <span>즉시 검토</span><strong>${number(immediate)}</strong>
            </button>
            <button type="button" class="refresh-decision-card plan" data-table-filter="notebook_refresh_priority" data-filter-mode="notebook_refresh">
              <span>교체 계획</span><strong>${number(planned)}</strong>
            </button>
            <button type="button" class="refresh-decision-card regular" data-table-filter="notebook_refresh_priority" data-filter-mode="notebook_refresh">
              <span>정기 교체</span><strong>${number(regular)}</strong>
            </button>
          </div>
        </div>
        <div>
          <h4>판단 보조</h4>
          <div class="refresh-decision-grid support">
            <button type="button" class="refresh-decision-card info" data-table-filter="notebook_refresh_decision" data-filter-mode="notebook_refresh">
              <span>정보 확인</span><strong>${number(info)}</strong>
            </button>
            <button type="button" class="refresh-decision-card sale" data-table-filter="notebook_refresh_decision" data-filter-mode="notebook_refresh">
              <span>매각 검토</span><strong>${number(sale)}</strong>
            </button>
            <button type="button" class="refresh-decision-card estimate" data-table-filter="notebook_age_basis" data-filter-mode="notebook_refresh">
              <span>취득일 추정</span><strong>${number(estimated)}</strong>
            </button>
          </div>
        </div>
      </div>
    </div>
  `;
}

function emptyForMode() {
  if (state.mode === "maintenance") {
    return `
      <div class="empty empty-feature">
        <strong>유지보수 데이터가 아직 없습니다.</strong>
        <span>현재 maintenance_events와 자산의 유지보수일 필드가 모두 비어 있어 운영 탭에서는 숨기거나 보증/점검 소스를 먼저 연결하는 편이 좋습니다.</span>
      </div>
    `;
  }
  if (state.mode === "software_expirations") {
    return `<div class="empty empty-feature"><strong>소프트웨어 만료일 대상이 없습니다.</strong><span>Amaranth 승인 상세에서 사용기한이 확인되는 소프트웨어/구독 신청이 여기에 표시됩니다.</span></div>`;
  }
  if (state.mode === "software_inventory") {
    return `<div class="empty empty-feature"><strong>소프트웨어 요약 데이터가 아직 없습니다.</strong><span>10.0.0.19 Sweeper 스냅샷이 들어오면 SW 요약에 표시됩니다.</span></div>`;
  }
  if (state.mode === "software_installs") {
    return `<div class="empty empty-feature"><strong>전체 설치 현황 데이터가 아직 없습니다.</strong><span>10.0.0.19 Sweeper 설치 스냅샷이 들어오면 SW 전체설치에 표시됩니다.</span></div>`;
  }
  if (state.mode === "software_users") {
    return `<div class="empty empty-feature"><strong>소프트웨어 사용현황 데이터가 아직 없습니다.</strong><span>10.0.0.19 Sweeper 설치 스냅샷이 들어오면 SW 사용현황에 표시됩니다.</span></div>`;
  }
  return `<div class="empty">데이터 없음</div>`;
}

function sourceQuality(row) {
  const sources = Array.isArray(state.dataQuality?.sources) ? state.dataQuality.sources : [];
  return sources.find((item) => item.id && item.id === row.id) || sources.find((item) => item.name === row.name) || {};
}

function sourceFreshness(row) {
  const quality = sourceQuality(row);
  if (!row.last_sync_at) return `<span class="freshness idle">미설정</span>`;
  if (quality.stale) return `<span class="freshness warn">${number(quality.age_hours)}h stale</span>`;
  return `<span class="freshness ok">${quality.age_hours === null || quality.age_hours === undefined ? "동기화" : `${number(quality.age_hours)}h`}</span>`;
}

function sourceStatusBadge(value) {
  const text = value || "확인필요";
  const okValues = new Set(["imported", "read-only", "configured", "ok", "local-outbox"]);
  const cls = okValues.has(text) ? "ok" : text === "not_configured" ? "warn" : "idle";
  const label = text === "local-outbox" ? "내부 outbox (미전송)" : text;
  return `<span class="badge ${cls}">${html(label)}</span>`;
}

function filterableTable(rows, columns, mode, options = {}) {
  const baseRows = filterBaseRowsForMode(rows, mode);
  const filteredRows = filterRowsByTableFilters(baseRows, columns, mode);
  const className = options.className || "";
  const empty = options.empty || `<div class="empty">데이터 없음</div>`;
  if (!rows.length) return empty;
  return `
    ${tableFilterPanel(baseRows, columns, mode)}
    <div class="table-filter-status">
      <span>필터 결과 ${number(filteredRows.length)} / ${number(rows.length)}건</span>
      ${Object.keys(state.tableFilters[mode] || {}).length ? `<button type="button" class="btn subtle" data-filter-clear data-filter-mode="${html(mode)}" data-filter-key="__all">전체 필터 해제</button>` : ""}
    </div>
    <table class="data-table ${html(className)}">
      <thead>
        ${tableHeader(columns, mode)}
      </thead>
      <tbody>
        ${filteredRows.length ? filteredRows.map((row) => `
          <tr ${options.rowAttributes ? options.rowAttributes(row) : ""} class="${html(options.rowClass ? options.rowClass(row) : "")}">
            ${columns.map((column) => `<td class="${html(column.className || "")}">${column.render(row)}</td>`).join("")}
          </tr>
        `).join("") : `<tr><td colspan="${columns.length}"><div class="empty">조건에 맞는 결과가 없습니다 · 필터를 조정하세요</div></td></tr>`}
      </tbody>
    </table>
  `;
}

function sourceTable(rows) {
  return filterableTable(rows, tableColumnsForMode("sources"), "sources", {
    className: "source-table",
    rowClass: (row) => sourceQuality(row).stale ? "row-stale" : "",
  });
}

function approvalView() {
  const summary = state.approvalMeta?.summary?.counts || {};
  const filters = [
    ["my", "내 업무", (summary.my_work || 0) + (summary.first_review || 0)],
    ["pending", "미시행", summary.dispatch_pending],
    ["unread", "미열람", summary.reference_unread],
    ["review", "확인필요", summary.first_review],
    ["not_my_scope", "내 업무 아님", summary.not_my_scope],
    ["all", "전체", summary.total],
  ];
  return `
    <section class="panel">
      <div class="panel-head">
        <div>
          <h2>전자결재</h2>
          <span>${number(state.rows.length)}건 · 8000 기준</span>
        </div>
        <div class="approval-filter">
          ${filters.map(([id, label, count]) => `
            <button type="button" class="btn ${state.approvalFilter === id ? "primary" : "subtle"}" data-approval-filter="${id}">
              ${html(label)} ${number(count)}
            </button>
          `).join("")}
        </div>
      </div>
      <div class="content-body">${approvalTable(state.rows)}</div>
    </section>
  `;
}

function approvalTable(rows) {
  return filterableTable(rows, tableColumnsForMode("approvals"), "approvals", {
    className: "approval-table",
    rowAttributes: (row) => `data-approval="${html(row.docId)}"`,
    rowClass: (row) => row.docId === state.selectedApproval ? "selected" : "",
  });
}

function compactApprovalTime(value) {
  const raw = String(value || "").trim();
  if (/^\d{14}$/.test(raw)) {
    return `${raw.slice(0, 4)}-${raw.slice(4, 6)}-${raw.slice(6, 8)} ${raw.slice(8, 10)}:${raw.slice(10, 12)}`;
  }
  return compact(raw);
}

function approvalBadge(value) {
  const text = value || "확인필요";
  const cls = text === "내 업무 아님" ? "idle" : text === "내가 처리" ? "ok" : "warn";
  return `<span class="badge ${cls}">${html(text)}</span>`;
}

function approvalSourceStatus(row) {
  const status = row.sourceStatus || {};
  const label = status.label || row.statusLabel || "-";
  const type = status.done ? "ok" : status.touched ? "warn" : "idle";
  const basis = status.basis ? `<small>${html(status.basis)}</small>` : "";
  return `<span class="source-status ${type}"><strong>${html(label)}</strong>${basis}</span>`;
}

function approvalLocalStatus(value, sourceStatus = null) {
  if (value === "done") return `<span class="badge ok">내부 시행 표시</span>${sourceStatus?.done ? "" : `<span class="badge warn">원천 미반영</span>`}`;
  if (value === "blocked") return `<span class="badge warn">보류</span>`;
  if (value === "draft") return `<span class="badge idle">초안</span>`;
  return `<span class="muted">-</span>`;
}

function assetGroupView() {
  return `
    <section class="panel">
      <div class="panel-head">
        <div>
          <h2>자산관리번호</h2>
          <span>번호대 클릭 시 해당 자산만 표시</span>
        </div>
        <button type="button" class="btn subtle" data-mode="assets">전체 자산</button>
      </div>
      <div class="content-body">${assetGroupTable(state.rows)}</div>
    </section>
  `;
}

function assetGroupTable(rows) {
  return filterableTable(rows, tableColumnsForMode("asset_groups"), "asset_groups", {
    className: "asset-group-table",
    rowAttributes: (row) => `data-asset-group="${html(row.asset_group)}"`,
    rowClass: (row) => row.asset_group === state.assetPrefix ? "selected" : "",
  });
}

function ownerTable(rows) {
  return filterableTable(rows, tableColumnsForMode("owners"), "owners", {
    className: "owner-table",
  });
}

function ownerCategoryButtons(row) {
  const items = Array.isArray(row.category_breakdown) ? row.category_breakdown : [];
  if (!items.length) return `<span class="muted">분류 없음</span>`;
  return `
    <div class="owner-kind-list">
      ${items.map((item) => `
        <button type="button" class="owner-kind-button" data-owner-assets="${html(row.owner)}" data-class="${html(item.kind || "")}">
          <span>${html(item.label || assetClassLabels[item.kind] || item.kind || "-")}</span><strong>${number(item.count)}</strong>
        </button>
      `).join("")}
    </div>
  `;
}

function assetTable(rows) {
  const mode = assetScopedModes.includes(state.mode) ? state.mode : "assets";
  return filterableTable(rows, tableColumnsForMode(mode), mode, {
    className: "asset-table",
    rowAttributes: (row) => `data-asset="${html(row.asset_tag)}"`,
    rowClass: (row) => row.asset_tag === state.selectedAsset ? "selected" : "",
  });
}

function softwareInventoryTable(rows) {
  return filterableTable(rows, tableColumnsForMode("software_inventory"), "software_inventory", {
    className: "software-inventory-table",
    rowAttributes: (row) => `data-software="${html(row.sw_id)}"`,
    rowClass: (row) => row.sw_id === state.selectedSoftware ? "selected" : "",
  });
}

function softwareUsersTable(rows) {
  return filterableTable(rows, tableColumnsForMode("software_users"), "software_users", {
    className: "software-users-table",
    rowAttributes: (row) => `data-software-user="${html(row.user_key)}"`,
    rowClass: (row) => row.user_key === state.selectedSoftwareUser ? "selected" : "",
  });
}

function simpleTable(rows, columns, selectable = true, mode = state.mode) {
  const tableColumns = tableColumnsForMode(mode).length ? tableColumnsForMode(mode) : simpleColumns(columns);
  return filterableTable(rows, tableColumns, mode, {
    rowAttributes: (row) => selectable && row.asset_tag ? `data-asset="${html(row.asset_tag)}"` : "",
    rowClass: (row) => row.asset_tag === state.selectedAsset ? "selected" : "",
  });
}

function rackMap() {
  const rows = state.rows.filter((row) => row.room || row.rack || row.asset_tag);
  const grouped = new Map();
  rows.forEach((row) => {
    const key = `${row.room || "미지정"}|${row.rack || "미지정"}`;
    const current = grouped.get(key) || { room: row.room || "미지정", rack: row.rack || "미지정", count: 0, servers: 0, used: 0, review: 0, assets: [] };
    current.count += 1;
    current.servers += truthy(row.is_server) ? 1 : 0;
    current.used += row.usage_status === "사용중" ? 1 : 0;
    current.review += row.usage_status === "확인필요" ? 1 : 0;
    current.assets.push(row);
    grouped.set(key, current);
  });
  const racks = [...grouped.values()].sort((a, b) => `${a.room}${a.rack}`.localeCompare(`${b.room}${b.rack}`, "ko"));
  if (!state.selectedRack && racks[0]) state.selectedRack = `${racks[0].room}|${racks[0].rack}`;
  const selected = racks.find((rack) => `${rack.room}|${rack.rack}` === state.selectedRack) || racks[0];
  const selectedAssets = selected?.assets || [];
  const selectedUsed = selected?.used || 0;
  const selectedReview = selected?.review || 0;
  const selectedServers = selected?.servers || 0;
  const selectedUtil = Math.min(100, Math.round((selected?.used / Math.max(selected?.count || 0, 1)) * 100));
  const rackTotals = racks.reduce((acc, rack) => {
    acc.count += rack.count;
    acc.servers += rack.servers;
    acc.used += rack.used;
    acc.review += rack.review;
    return acc;
  }, { count: 0, servers: 0, used: 0, review: 0 });
  const rackStat = (label, value, tone = "") => `
    <div class="rack-stat ${tone}">
      <span>${html(label)}</span>
      <strong>${number(value)}</strong>
    </div>
  `;
  return `
    <section class="panel rack-panel">
      <div class="panel-head">
        <div>
          <h2>상면도</h2>
          <span>랙 클릭 시 우측에서 상세 자산 확인</span>
        </div>
      </div>
      <div class="rack-summary-strip">
        ${rackStat("랙", racks.length)}
        ${rackStat("자산", rackTotals.count)}
        ${rackStat("사용중", rackTotals.used, "ok")}
        ${rackStat("서버", rackTotals.servers)}
        ${rackStat("확인필요", rackTotals.review, rackTotals.review ? "warn" : "")}
      </div>
      <div class="rack-workspace">
        <div class="rack-picker">
          <div class="rack-picker-head">
            <strong>랙 선택</strong>
            <span>${number(racks.length)}개 랙</span>
          </div>
          <div class="rack-map" role="list" aria-label="상면도 랙 목록">
            ${racks.map((rack) => {
              const key = `${rack.room}|${rack.rack}`;
              const util = Math.min(100, Math.round((rack.used / Math.max(rack.count, 1)) * 100));
              const tone = rack.review ? "needs-review" : rack.used ? "active" : "idle";
              return `
                <button type="button" class="rack-card ${tone} ${key === state.selectedRack ? "selected" : ""}" data-rack="${html(key)}" aria-pressed="${key === state.selectedRack ? "true" : "false"}">
                  <span class="rack-card-kicker">${html(rack.room)}</span>
                  <strong>${html(rack.room)} · ${html(rack.rack)}</strong>
                  <div class="rack-card-stats">
                    <span>${number(rack.count)}대</span>
                    <span>서버 ${number(rack.servers)}</span>
                    <span class="${rack.review ? "warn" : ""}">확인 ${number(rack.review)}</span>
                  </div>
                  <div class="rack-util" aria-label="사용중 비율 ${util}%"><i style="width:${util}%"></i></div>
                </button>
              `;
            }).join("")}
          </div>
        </div>
        <div class="rack-detail-panel">
          <div class="rack-detail-head">
            <div>
              <span>선택 랙</span>
              <h3>${html([selected?.room, selected?.rack].filter(Boolean).join(" · ") || "미지정")}</h3>
            </div>
            ${selectedReview ? badge("확인필요") : badge("정상")}
          </div>
          <div class="rack-stat-grid">
            ${rackStat("자산", selected?.count || 0)}
            ${rackStat("사용중", selectedUsed, "ok")}
            ${rackStat("서버", selectedServers)}
            ${rackStat("확인필요", selectedReview, selectedReview ? "warn" : "")}
          </div>
          <div class="rack-util large"><i style="width:${selectedUtil}%"></i></div>
          <div class="rack-selected-assets">
            <div class="rack-selected-head">
              <h3>선택 랙 자산</h3>
              <span>${number(selectedAssets.length)}개</span>
            </div>
            <div class="content-body rack-asset-table">${assetTable(selectedAssets)}</div>
          </div>
        </div>
      </div>
    </section>
  `;
}

function detailPanel() {
  if (state.mode === "approvals") return approvalDetailPanel();
  if (state.mode === "software_inventory") return softwareDetailPanel();
  if (state.mode === "software_users") return softwareUserDetailPanel();
  if (state.mode === "jira_missing" || state.detail?.jira_missing) return jiraMissingDetailPanel();
  const detail = state.detail;
  if (!detail?.asset) return `<aside class="detail"><section class="detail-card empty">자산 선택</section></aside>`;
  const asset = detail.asset;
  const endpointProfile = detail.endpoint_profile || {};
  return `
    <aside class="detail">
      <section class="detail-card detail-hero">
        <div class="detail-title">
          <div>
            <h2>${html(asset.hostname || asset.asset_tag)}</h2>
            <p class="mono">${html(asset.asset_tag)}</p>
          </div>
          ${badge(asset.usage_status)}
        </div>
        <div class="detail-grid">
          ${kv("시리얼", asset.serial, true)}
          ${kv("모델", [asset.manufacturer, asset.model].filter(Boolean).join(" "), false)}
          ${kv("IP", asset.primary_ip, true)}
          ${kv("위치", [asset.room, asset.rack, asset.rack_unit].filter(Boolean).join(" / ") || asset.location, false)}
        </div>
      </section>

      <section class="detail-card">
        <h3>판정</h3>
        <div class="reason-stack">
          <div class="reason"><span>판정</span><div>${badge(asset.usage_status)}</div></div>
          <div class="reason"><span>근거</span><strong>${html(asset.usage_basis || asset.usage_reason || "근거 없음")}</strong></div>
          <div class="reason"><span>증적</span><strong>${html(asset.usage_evidence || "-")}</strong></div>
          ${asset.notebook_refresh_decision ? `<div class="reason"><span>관리자 판단</span><strong>${html(`${asset.notebook_refresh_priority || "-"} · ${asset.notebook_refresh_decision}`)}</strong></div>` : ""}
          ${asset.notebook_refresh_reason ? `<div class="reason"><span>판단 근거</span><strong>${html(`${asset.notebook_refresh_reason} · ${asset.notebook_refresh_flags || ""}`)}</strong></div>` : ""}
          ${asset.lifecycle_label ? `<div class="reason"><span>교체/매각</span><strong>${html(`${asset.lifecycle_label} · ${asset.notebook_age_years || "-"}년 · ${asset.notebook_acquired_date || "-"} (${asset.notebook_age_basis || "기준 없음"})`)}</strong></div>` : ""}
          <div class="reason"><span>Jira</span><strong>${html([asset.jira_lane, asset.jira_issue].filter(Boolean).join(" / ") || "-")}</strong></div>
          <div class="reason"><span>담당</span><strong>${html(asset.owner || "미지정")}</strong></div>
          ${asset.owner_status && asset.owner_status !== "active" ? `<div class="reason"><span>담당 상태</span><strong>${html(asset.owner_status_label || "재직 확인필요")}</strong></div>` : ""}
          <div class="meter"><i style="width:${asset.usage_status === "확인필요" ? 38 : 86}%"></i></div>
        </div>
        <div style="display:flex; gap:8px; margin-top:12px; flex-wrap:wrap">
          <button type="button" class="btn subtle" data-usage="사용중">사용중</button>
          <button type="button" class="btn subtle" data-usage="미사용">미사용</button>
          <button type="button" class="btn subtle" data-usage="종료/제외">종료/제외</button>
          <button type="button" class="btn subtle" data-usage="확인필요">확인필요</button>
        </div>
      </section>

      ${endpointProfilePanel(endpointProfile)}
      ${installedSoftwarePanel(detail.installed_software || [])}

      <section class="detail-card">
        <h3>수정</h3>
        <form class="edit-form" id="edit-form">
          ${["hostname", "serial", "manufacturer", "model", "owner", "department", "primary_ip", "ports", "os", "location", "room", "rack", "rack_unit", "maintenance_date", "status", "purpose"].map((key) => `
            <label>${html(fieldLabel(key))}
              <input name="${html(key)}" value="${html(asset[key])}" />
            </label>
          `).join("")}
          <button class="btn primary" type="submit">저장</button>
        </form>
      </section>

      <section class="detail-card">
        <h3>변경 이력</h3>
        <div class="source-grid">
          ${(detail.changes || []).slice(0, 8).map((item) => `
            <div class="timeline-item">
              <time>${html(compact(item.changed_at))}</time>
              <div><strong>${html(item.field || item.action)}</strong><br><span class="muted">${html(item.old_value)} → ${html(item.new_value)}</span></div>
            </div>
          `).join("") || `<div class="empty">변경 이력 없음</div>`}
        </div>
      </section>

      <section class="detail-card">
        <h3>원본</h3>
        <div class="source-grid">
          ${kv("소스", asset.source)}
          ${kv("갱신", compact(asset.updated_at), true)}
          ${kv("관리번호 분류", asset.asset_number_class_label || asset.asset_kind_label)}
          ${kv("원본 분류", asset.category)}
          ${kv("목적", asset.purpose)}
        </div>
      </section>
    </aside>
  `;
}

const endpointProfilePriorityLabels = ["CPU", "RAM", "저장장치", "Wi-Fi MAC", "NAC 최근 접속"];

function profileSection(title, items) {
  if (!Array.isArray(items) || !items.length) return "";
  const rank = new Map(endpointProfilePriorityLabels.map((label, index) => [label, index]));
  const sorted = items.slice().sort((a, b) => (rank.get(a.label) ?? 99) - (rank.get(b.label) ?? 99));
  return `
    <div class="profile-group">
      <h4>${html(title)}</h4>
      <div class="profile-grid">
        ${sorted.map((item) => `
          <div class="profile-item">
            <span>${html(item.label)}</span>
            <strong class="${String(item.value || "").length > 28 ? "mono" : ""}">${html(item.value || "-")}</strong>
            ${item.source ? `<em>${html(item.source)}</em>` : ""}
          </div>
        `).join("")}
      </div>
    </div>
  `;
}

function endpointProfilePanel(profile) {
  const sections = [
    profileSection("식별", profile.identity),
    profileSection("자원", profile.hardware),
    profileSection("네트워크", profile.network),
    profileSection("관리", profile.management),
  ].filter(Boolean).join("");
  if (!sections) return "";
  return `
    <section class="detail-card">
      <h3>하드웨어/네트워크</h3>
      <div class="profile-stack">${sections}</div>
    </section>
  `;
}

function installedSoftwarePanel(items) {
  if (!Array.isArray(items) || !items.length) return "";
  return `
    <section class="detail-card">
      <h3>설치된 소프트웨어</h3>
      <div class="source-grid">
        ${items.slice(0, 12).map((item) => `
          <div class="source-item">
            <strong>${html(item.software_name || "-")}</strong>
            <span>${softwareLicenseBadge(item.license_status_label)}</span>
            <small class="mono">${html(compact(item.install_date || item.report_date || item.collected_at || ""))}</small>
          </div>
        `).join("")}
      </div>
      ${items.length > 12 ? `<p class="muted">외 ${number(items.length - 12)}개</p>` : ""}
    </section>
  `;
}

function softwareDetailPanel() {
  const detail = state.detail || {};
  const software = detail.software || state.rows.find((row) => row.sw_id === state.selectedSoftware) || null;
  const installs = Array.isArray(detail.installs) ? detail.installs : [];
  if (!software) return `<aside class="detail"><section class="detail-card empty">소프트웨어 선택</section></aside>`;
  return `
    <aside class="detail">
      <section class="detail-card detail-hero">
        <div class="detail-title">
          <div>
            <h2>${html(software.software_name || "-")}</h2>
            <p class="mono">${html(software.sw_id || "")}</p>
          </div>
          ${softwareRiskBadge(software.risk_label)}
        </div>
        <div class="detail-grid">
          ${kv("설치", number(software.install_count), true)}
          ${kv("허가", software.legal_install_count == null ? "—" : number(software.legal_install_count), true)}
          ${kv("라이선스 확인", number(software.illegal_install_count), true)}
          ${kv("라이선스", number(software.license_amount), true)}
          ${kv("수집", compact(software.collected_at), true)}
          ${kv("반영", compact(software.imported_at), true)}
        </div>
      </section>

      <section class="detail-card">
        <h3>SW 설치 상세</h3>
        <div class="software-install-list">
          ${installs.slice(0, 80).map((item) => `
            <div class="software-install-row">
              <div>
                <strong>${html(item.asset_tag || item.equip_name || item.equip_code || "-")}</strong>
                <span>${html(item.user_key || item.user_name || "사용자 미확인")}</span>
              </div>
              <div>
                ${softwareLicenseBadge(item.license_status_label)}
                <small>${html(compact(item.install_date || item.report_date || item.collected_at || ""))}</small>
              </div>
            </div>
          `).join("") || `<div class="empty">설치 이력 없음</div>`}
        </div>
      </section>
    </aside>
  `;
}

function softwareUserDetailPanel() {
  const detail = state.detail || {};
  const user = detail.softwareUser || state.rows.find((row) => row.user_key === state.selectedSoftwareUser) || null;
  const installs = Array.isArray(detail.installs) ? detail.installs : [];
  if (!user) return `<aside class="detail"><section class="detail-card empty">사용자 선택</section></aside>`;
  return `
    <aside class="detail">
      <section class="detail-card detail-hero">
        <div class="detail-title">
          <div>
            <h2>${html(user.user_name || user.user_key || "사용자 미확인")}</h2>
            <p class="mono">${html(user.user_key || "")}</p>
          </div>
          ${user.unlicensed_count ? softwareRiskBadge(`라이선스 확인 ${number(user.unlicensed_count)}`) : softwareRiskBadge("정상")}
        </div>
        <div class="detail-grid">
          ${kv("부서", user.dept_name)}
          ${kv("설치", number(user.install_count), true)}
          ${kv("소프트웨어", number(user.software_count), true)}
          ${kv("자산", number(user.asset_count), true)}
          ${kv("라이선스 확인", number(user.unlicensed_count), true)}
          ${kv("최근", compact(user.latest_seen_at), true)}
        </div>
      </section>

      <section class="detail-card">
        <h3>SW 사용자 상세</h3>
        <div class="software-install-list">
          ${installs.slice(0, 120).map((item) => `
            <div class="software-install-row">
              <div>
                <strong>${html(item.software_name || "-")}</strong>
                <span>${html(item.asset_tag || item.equip_name || item.equip_code || "-")}</span>
              </div>
              <div>
                ${softwareLicenseBadge(item.license_status_label)}
                <small>${html(compact(item.install_date || item.report_date || item.collected_at || ""))}</small>
              </div>
            </div>
          `).join("") || `<div class="empty">설치 이력 없음</div>`}
        </div>
      </section>
    </aside>
  `;
}

function jiraMissingDetailPanel() {
  const record = state.detail?.jira_missing || state.rows.find((row) => row.asset_tag === state.selectedAsset) || null;
  if (!record) return `<aside class="detail"><section class="detail-card empty">Jira-only 선택</section></aside>`;
  return `
    <aside class="detail">
      <section class="detail-card detail-hero">
        <div class="detail-title">
          <div>
            <h2>${html(record.asset_tag)}</h2>
            <p class="mono">${html(record.jira_issue || "Jira-only")}</p>
          </div>
          ${sourceStatusBadge(record.source_status || "read-only")}
        </div>
        <div class="detail-grid">
          ${kv("Jira", record.jira_issue, true)}
          ${kv("Jira 상태", record.jira_lane_label || record.jira_lane)}
          ${kv("Jira 담당", record.jira_owner)}
          ${kv("제조사", record.manufacturer)}
          ${kv("모델", record.model)}
          ${kv("스펙", record.specification)}
          ${kv("동기화", compact(record.last_sync_at), true)}
        </div>
      </section>

      <section class="detail-card">
        <h3>판정</h3>
        <div class="reason-stack">
          <div class="reason"><span>자산 DB</span><strong>미등록</strong></div>
          <div class="reason"><span>처리 상태</span><strong>${html(record.workflow_label || "미검토")}</strong></div>
          <div class="reason"><span>다음 조치</span><strong>${html(record.next_action || "확인 필요")}</strong></div>
          <div class="reason"><span>원천</span><strong>${html(record.source_file || "Jira REST API")}</strong></div>
          <div class="reason"><span>소스 상태</span><div>${sourceStatusBadge(record.source_status || "read-only")}</div></div>
          <div class="reason"><span>Jira lane</span><strong>${html(record.jira_lane || "-")}</strong></div>
        </div>
      </section>

      <section class="detail-card">
        <h3>처리 큐</h3>
        <div class="jira-action-grid">
          <button type="button" class="btn primary" data-jira-action="register_needed">자산 등록 필요</button>
          <button type="button" class="btn subtle" data-jira-action="jira_stale">Jira stale</button>
          <button type="button" class="btn subtle" data-jira-action="blocked">보류</button>
          <button type="button" class="btn subtle" data-jira-action="resolved">처리 완료</button>
        </div>
        <div class="source-grid">
          ${kv("메모", record.workflow_note)}
          ${kv("갱신", compact(record.workflow_updated_at), true)}
        </div>
      </section>
    </aside>
  `;
}

function approvalDetailPanel() {
  const detail = state.detail;
  if (!detail?.item) return `<aside class="detail"><section class="detail-card empty">문서 선택</section></aside>`;
  const item = detail.item;
  const record = detail.record || {};
  const sourceDetail = detail.sourceDetail || {};
  const comments = Array.isArray(record.comments) ? record.comments : [];
  const contentsText = sourceDetail.contentsText || sourceDetail.contentText || "";
  const checkedLabels = Array.isArray(sourceDetail.checkedLabels) ? sourceDetail.checkedLabels : [];
  const fileList = Array.isArray(sourceDetail.fileList) ? sourceDetail.fileList : [];
  const canDispatch = item.sourceBoxCode === "120" && item.operYn !== "Y";
  const externalUrl = safeExternalUrl(detail.externalUrl || item.externalUrl || detail.homeUrl || "");
  return `
    <aside class="detail">
      <section class="detail-card detail-hero">
        <div class="detail-title">
          <div>
            <h2>${html(item.docTitle || "(제목 없음)")}</h2>
            <p class="mono">${html(item.docNo || item.docId)}</p>
          </div>
          ${approvalBadge(item.bucket)}
        </div>
        <div class="detail-grid">
          ${kv("테마", item.theme)}
          ${kv("원천 상태", item.statusLabel)}
          ${kv("기안자", item.createdBy)}
          ${kv("부서", item.deptName)}
        </div>
        <div class="approval-source-strip">
          ${approvalSourceStatus(item)}
          ${externalUrl ? `<a class="btn primary" href="${html(externalUrl)}" target="_blank" rel="noopener noreferrer">원문 열기</a>` : `<span class="muted">원문 링크 없음</span>`}
        </div>
      </section>

      <section class="detail-card">
        <h3>상세 내용</h3>
        <div class="source-grid">
          ${kv("문서 제목", item.docTitle)}
          ${kv("문서 번호", item.docNo || item.docId, true)}
          ${kv("양식", item.formName)}
          ${kv("함", item.sourceLabel)}
          ${kv("최근 시각", compactApprovalTime(item.repDt), true)}
          ${kv("판단 근거", item.sourceStatus?.basis || item.needsActionReason)}
          ${kv("원천 상세", contentsText ? "Amaranth 상세 수신됨" : "상세 캐시 없음")}
        </div>
      </section>

      <section class="detail-card">
        <h3>원문 상세</h3>
        ${checkedLabels.length ? `
          <div class="approval-chip-list">
            ${checkedLabels.map((label) => `<span>${html(label)}</span>`).join("")}
          </div>
        ` : ""}
        ${renderApprovalContent(contentsText, { checkedLabels })}
        ${fileList.length ? `
          <div class="approval-files">
            ${fileList.map((file) => `<span>${html(file.fileName || file.name || file)}</span>`).join("")}
          </div>
        ` : ""}
      </section>

      <section class="detail-card">
        <h3>처리</h3>
        <form class="edit-form" id="approval-form">
          <label>시행 메모
            <textarea name="dispatchMemo" rows="5" maxlength="4000">${html(record.dispatchMemo || "")}</textarea>
          </label>
          <label>댓글
            <textarea name="comment" rows="5" maxlength="4000" placeholder="상세 화면에서 바로 남길 댓글"></textarea>
          </label>
          <label>내부 상태
            <select name="localStatus">
              <option value="draft" ${record.localStatus === "draft" || !record.localStatus ? "selected" : ""}>초안</option>
              <option value="done" ${record.localStatus === "done" ? "selected" : ""}>내부 시행 표시</option>
              <option value="blocked" ${record.localStatus === "blocked" ? "selected" : ""}>보류</option>
            </select>
          </label>
          <button class="btn primary" type="submit">초안 저장</button>
          <button class="btn primary" type="button" data-approval-action="dispatch_done" ${canDispatch ? "" : "disabled"} title="${canDispatch ? "" : "시행함 문서에서만 사용할 수 있습니다."}">내부 시행 표시 (원천 미반영)</button>
          <button class="btn subtle" type="button" data-approval-action="add_comment">댓글 등록</button>
          <button class="btn subtle" type="button" data-approval-action="request_writeback" title="${html(detail.writeback?.reason || "")}">${html(detail.writeback?.buttonLabel || "원문 반영 요청 등록")}</button>
        </form>
      </section>

      <section class="detail-card">
        <h3>댓글</h3>
        <div class="source-grid">
          ${comments.slice().reverse().slice(0, 20).map((comment) => `
            <div class="timeline-item">
              <time>${html(compact(comment.at))}</time>
              <div><strong>${html(comment.actor || "operator-ui")}</strong><br><span class="muted">${html(comment.body || "")}</span></div>
            </div>
          `).join("") || `<div class="empty">댓글 없음</div>`}
        </div>
      </section>

      <section class="detail-card">
        <h3>분류</h3>
        <div class="reason-stack">
          <div class="reason"><span>처리 구분</span><strong>${html(item.action || "-")}</strong></div>
          <div class="reason"><span>자동 판단</span><strong>${html(item.reason || "-")}</strong></div>
          <div class="reason"><span>함</span><strong>${html(item.sourceLabel || "-")}</strong></div>
          <div class="reason"><span>양식</span><strong>${html(item.formName || "-")}</strong></div>
        </div>
      </section>

      <section class="detail-card">
        <h3>이력</h3>
        <div class="source-grid">
          ${(record.history || []).slice().reverse().slice(0, 8).map((history) => `
            <div class="timeline-item">
              <time>${html(compact(history.at))}</time>
              <div><strong>${html(history.summary || history.localStatus || "-")}</strong><br><span class="muted">${html(history.actor || "")}</span></div>
            </div>
          `).join("") || `<div class="empty">처리 이력 없음</div>`}
        </div>
      </section>
    </aside>
  `;
}

function kv(label, value, mono = false) {
  return `<div class="kv"><span>${html(label)}</span><strong class="${mono ? "mono" : ""}">${html(value || "-")}</strong></div>`;
}

function fieldLabel(key) {
  return {
    hostname: "호스트", serial: "시리얼", manufacturer: "제조사", model: "모델", owner: "담당자", department: "부서",
    primary_ip: "IP", ports: "포트", os: "OS", location: "위치", room: "실", rack: "랙", rack_unit: "U",
    maintenance_date: "유지보수", status: "상태", purpose: "목적",
  }[key] || key;
}

function render(options = {}) {
  const scrollPosition = options.preserveScroll ? captureScroll() : null;
  document.getElementById("app").innerHTML = `
    <div class="app-shell">
      ${sidebar()}
      <section class="workspace">
        ${topbar()}
        <main class="workspace-main">
          ${state.mode === "dashboard" ? `
            <div class="dashboard-toolbar">
              <button type="button" class="auth-pill" data-mode="sources">SSO/OIDC</button>
              <span>${html(integrationLabel("oidc"))}</span>
            </div>
            ${qualityStrip()}
            ${categoryCards()}
            ${dashboardOverview()}
          ` : `
            ${qualityStrip()}
            <div class="hero-grid">
              ${metrics()}
              ${renderInsights()}
            </div>
            <div class="split">
              ${mainContent()}
              ${detailPanel()}
            </div>
          `}
        </main>
      </section>
      <div id="toast"></div>
    </div>
  `;
  bindEvents();
  restoreScroll(scrollPosition);
}

async function switchMode(nextMode, filter = null) {
  if (!nextMode) return;
  if (!visibleModes.has(nextMode)) nextMode = "dashboard";
  state.mode = nextMode;
  state.assetKind = modeAssetKinds[nextMode] || "";
  state.assetClass = "";
  if (nextMode === "assets") state.assetKind = "";
  if (state.mode === "asset_groups" || !assetScopedModes.includes(state.mode)) state.assetPrefix = "";
  if (nextMode !== "assets") state.ownerFilter = "";
  state.dupTagOnly = !!(filter && (filter.dup_tag === "1" || filter.dup_tag === 1 || filter.dup_tag === true));
  state.dupSerialOnly = !!(filter && (filter.dup_serial === "1" || filter.dup_serial === 1 || filter.dup_serial === true));
  state.sourceIncludeHidden = nextMode === "sources" && !!(filter && (filter.include_hidden === "1" || filter.include_hidden === 1 || filter.include_hidden === true));
  state.changeAsset = nextMode === "changes" && filter?.asset_tag ? String(filter.asset_tag) : "";
  state.changeSource = nextMode === "changes" && filter?.source ? String(filter.source) : "";
  state.changeField = nextMode === "changes" && filter?.field ? String(filter.field) : "";
  if (nextMode !== "software_inventory") state.selectedSoftware = "";
  if (nextMode !== "software_users") state.selectedSoftwareUser = "";
  state.selectedRack = "";
  state.openTableFilter = null;
  await loadRows();
  await loadDetail();
  syncUrlState({ docId: state.mode === "approvals" ? state.selectedApproval : "" });
  render();
}

async function showOwnerAssets(owner, classKey = "", kind = "") {
  state.mode = "assets";
  state.ownerFilter = owner;
  state.assetKind = kind;
  state.assetClass = classKey;
  state.assetPrefix = "";
  state.query = "";
  state.selectedAsset = "";
  writeApprovalUrl("");
  await loadRows();
  await loadDetail();
  render();
}

function bindEvents() {
  document.querySelectorAll("[data-mode]").forEach((button) => {
    button.addEventListener("click", async () => {
      let filter = null;
      const raw = button.dataset.filter;
      if (raw && raw !== "{}") {
        try { filter = JSON.parse(raw); } catch (_) { filter = null; }
      }
      await switchMode(button.dataset.mode, filter);
    });
  });
  document.querySelectorAll("[data-table-filter]").forEach((button) => {
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      const mode = button.dataset.filterMode || state.mode;
      const key = button.dataset.tableFilter || "";
      state.openTableFilter = state.openTableFilter?.mode === mode && state.openTableFilter?.key === key ? null : { mode, key };
      render({ preserveScroll: true });
    });
  });
  document.querySelectorAll("[data-filter-toggle]").forEach((input) => {
    input.addEventListener("click", async (event) => {
      event.stopPropagation();
      const mode = input.dataset.filterMode || state.mode;
      toggleTableFilter(mode, input.dataset.filterKey || "", input.dataset.filterValue || "");
      await syncSelectionToVisibleRows(mode);
      render({ preserveScroll: true });
    });
  });
  document.querySelectorAll("[data-filter-clear]").forEach((button) => {
    button.addEventListener("click", async (event) => {
      event.stopPropagation();
      const mode = button.dataset.filterMode || state.mode;
      const key = button.dataset.filterKey || "";
      if (key === "__all") {
        state.tableFilters = { ...state.tableFilters, [mode]: {} };
      } else {
        clearTableFilter(mode, key);
      }
      await syncSelectionToVisibleRows(mode);
      render({ preserveScroll: true });
    });
  });
  document.querySelectorAll("[data-filter-close]").forEach((button) => {
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      state.openTableFilter = null;
      render({ preserveScroll: true });
    });
  });
  document.querySelectorAll("[data-asset]").forEach((item) => {
    item.addEventListener("click", async () => {
      const tag = item.dataset.asset;
      if (!tag) return;
      await loadDetail(tag);
      render({ preserveScroll: true });
    });
  });
  document.querySelectorAll("[data-software]").forEach((item) => {
    item.addEventListener("click", async () => {
      const swId = item.dataset.software;
      if (!swId) return;
      state.selectedSoftware = swId;
      await loadDetail();
      render({ preserveScroll: true });
    });
  });
  document.querySelectorAll("[data-software-user]").forEach((item) => {
    item.addEventListener("click", async () => {
      const userKey = item.dataset.softwareUser;
      if (!userKey) return;
      state.selectedSoftwareUser = userKey;
      await loadDetail();
      render({ preserveScroll: true });
    });
  });
  document.querySelectorAll("[data-approval]").forEach((item) => {
    item.addEventListener("click", async () => {
      const docId = item.dataset.approval;
      if (!docId) return;
      await loadApprovalDetail(docId);
      render({ preserveScroll: true });
    });
  });
  document.querySelectorAll("[data-approval-filter]").forEach((button) => {
    button.addEventListener("click", async () => {
      state.approvalFilter = button.dataset.approvalFilter || "my";
      state.selectedApproval = "";
      await loadRows();
      await loadDetail();
      render();
    });
  });
  document.querySelectorAll("[data-source-scope]").forEach((button) => {
    button.addEventListener("click", async () => {
      state.sourceIncludeHidden = button.dataset.sourceScope === "audit";
      await loadRows();
      syncUrlState();
      render({ preserveScroll: true });
    });
  });
  const changeFilterForm = document.getElementById("change-filter-form");
  if (changeFilterForm) {
    changeFilterForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const values = Object.fromEntries(new FormData(changeFilterForm).entries());
      state.changeAsset = String(values.asset_tag || "").trim();
      state.changeSource = String(values.source || "").trim();
      state.changeField = String(values.field || "").trim();
      await loadRows();
      syncUrlState();
      render();
    });
  }
  document.querySelectorAll("[data-action='clear-change-filter']").forEach((button) => {
    button.addEventListener("click", async () => {
      state.changeAsset = "";
      state.changeSource = "";
      state.changeField = "";
      await loadRows();
      syncUrlState();
      render();
    });
  });
  document.querySelectorAll("[data-asset-group]").forEach((item) => {
    item.addEventListener("click", async () => {
      const group = item.dataset.assetGroup;
      if (!group) return;
      state.assetPrefix = group;
      state.mode = "assets";
      state.ownerFilter = "";
      state.assetKind = "";
      state.assetClass = "";
      state.selectedAsset = "";
      await loadRows();
      await loadDetail();
      render();
    });
  });
  document.querySelectorAll("[data-owner-assets]").forEach((button) => {
    button.addEventListener("click", async () => {
      await showOwnerAssets(button.dataset.ownerAssets || "", button.dataset.class || "", button.dataset.kind || "");
    });
  });
  document.querySelectorAll("[data-rack]").forEach((item) => {
    item.addEventListener("click", () => {
      state.selectedRack = item.dataset.rack;
      render({ preserveScroll: true });
    });
  });
  document.querySelectorAll("[data-copy]").forEach((button) => {
    button.addEventListener("click", async (event) => {
      event.stopPropagation();
      const value = button.dataset.copy || "";
      if (navigator.clipboard && navigator.clipboard.writeText) {
        try {
          await navigator.clipboard.writeText(value);
          toast("복사 완료");
          return;
        } catch (err) {
          // fall through to legacy path
        }
      }
      const ta = document.createElement("textarea");
      ta.value = value;
      ta.setAttribute("readonly", "");
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      let ok = false;
      try { ok = document.execCommand("copy"); } catch (_) { ok = false; }
      ta.remove();
      toast(ok ? "복사 완료" : "복사 실패 — 수동 복사 필요");
    });
  });
  document.querySelectorAll("[data-action='refresh-list']").forEach((button) => {
    button.addEventListener("click", refreshAll);
  });
  document.querySelectorAll("[data-action='show-alerts']").forEach((button) => {
    button.addEventListener("click", async () => {
      if (Number(state.summary.needs_review || 0) > 0) {
        await switchMode("needs_review");
      } else {
        state.approvalFilter = "pending";
        await switchMode("approvals");
      }
    });
  });
  document.querySelectorAll("[data-action='show-owners']").forEach((button) => {
    button.addEventListener("click", async () => {
      await switchMode("owners");
    });
  });
  document.querySelectorAll("[data-theme-choice]").forEach((button) => {
    button.addEventListener("click", () => {
      const next = setTheme(button.dataset.themeChoice || "system");
      render({ preserveScroll: true });
      toast(next === "dark" ? "다크 모드 적용" : next === "light" ? "일반 모드 적용" : "시스템 설정 적용");
    });
  });
  document.querySelectorAll("[data-action='clear-dup-tag']").forEach((button) => {
    button.addEventListener("click", async () => {
      state.dupTagOnly = false;
      await loadRows();
      await loadDetail();
      syncUrlState({ docId: state.mode === "approvals" ? state.selectedApproval : "" });
      render();
    });
  });
  document.querySelectorAll("[data-action='clear-dup-serial']").forEach((button) => {
    button.addEventListener("click", async () => {
      state.dupSerialOnly = false;
      await loadRows();
      await loadDetail();
      syncUrlState({ docId: state.mode === "approvals" ? state.selectedApproval : "" });
      render();
    });
  });
  document.querySelectorAll("[data-action='clear-prefix']").forEach((button) => {
    button.addEventListener("click", async () => {
      state.assetPrefix = "";
      await loadRows();
      await loadDetail();
      render();
    });
  });
  document.querySelectorAll("[data-action='clear-owner-filter']").forEach((button) => {
    button.addEventListener("click", async () => {
      state.ownerFilter = "";
      await loadRows();
      await loadDetail();
      render();
    });
  });
  document.querySelectorAll("[data-action='clear-kind-filter']").forEach((button) => {
    button.addEventListener("click", async () => {
      state.assetKind = "";
      await loadRows();
      await loadDetail();
      render();
    });
  });
  document.querySelectorAll("[data-action='clear-class-filter']").forEach((button) => {
    button.addEventListener("click", async () => {
      state.assetClass = "";
      await loadRows();
      await loadDetail();
      render();
    });
  });
  document.querySelectorAll("[data-usage]").forEach((button) => {
    button.addEventListener("click", async () => {
      await updateAsset({ usage_status: button.dataset.usage, usage_reason: "운영자 수동 판정" });
      toast("사용 판정 저장 완료");
    });
  });
  document.querySelectorAll("[data-jira-action]").forEach((button) => {
    button.addEventListener("click", async () => {
      await updateJiraMissingWorkflow(button.dataset.jiraAction || "unreviewed");
      toast("Jira-only 처리 상태 저장 완료");
    });
  });
  const search = document.getElementById("search");
  if (search) {
    search.addEventListener("input", (event) => {
      scheduleSearch(event.currentTarget.value);
    });
    search.addEventListener("keydown", async (event) => {
      if (event.key !== "Enter") return;
      event.preventDefault();
      await commitSearch(event.currentTarget.value);
    });
    search.addEventListener("change", async (event) => {
      await commitSearch(event.currentTarget.value);
    });
  }
  const form = document.getElementById("edit-form");
  if (form) {
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const updates = Object.fromEntries(new FormData(form).entries());
      await updateAsset(updates);
      toast("자산 정보 저장 완료");
    });
  }
  const approvalForm = document.getElementById("approval-form");
  if (approvalForm) {
    approvalForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const values = Object.fromEntries(new FormData(approvalForm).entries());
      await updateApprovalWorkflow({ ...values, action: "save_draft", summary: "8000 초안 저장" });
      toast("전자결재 초안 저장 완료");
    });
  }
  document.querySelectorAll("[data-approval-action]").forEach((button) => {
    button.addEventListener("click", async () => {
      const approvalForm = document.getElementById("approval-form");
      if (!approvalForm) return;
      const values = Object.fromEntries(new FormData(approvalForm).entries());
      const action = button.dataset.approvalAction;
      if (action === "add_comment" && !String(values.comment || "").trim()) {
        toast("댓글을 입력해 주세요");
        return;
      }
      if (action === "request_writeback" && !String(values.comment || values.dispatchMemo || "").trim()) {
        toast("시행 메모나 댓글을 입력해 주세요");
        return;
      }
      await updateApprovalWorkflow({
        ...values,
        action,
        localStatus: action === "dispatch_done" ? "done" : values.localStatus,
        summary: action === "dispatch_done" ? "내부 시행 표시 (원천 미반영)로 저장" : action === "request_writeback" ? "Amaranth 원문 반영 요청" : "댓글 등록",
      });
      toast(action === "dispatch_done" ? "내부 시행 표시 (원천 미반영)로 저장됨" : action === "request_writeback" ? "원문 반영 요청 등록됨" : "댓글 등록됨");
    });
  });
}

function scheduleSearch(value) {
  state.query = String(value || "").trim();
  if (searchTimer) window.clearTimeout(searchTimer);
  searchTimer = window.setTimeout(() => {
    searchTimer = null;
    commitSearch(state.query);
  }, 350);
}

async function commitSearch(value) {
  state.query = String(value || "").trim();
  if (searchTimer) {
    window.clearTimeout(searchTimer);
    searchTimer = null;
  }
  await loadRows();
  await loadDetail();
  syncUrlState({ docId: state.mode === "approvals" ? state.selectedApproval : "" });
  render();
}

async function updateAsset(updates) {
  if (!state.selectedAsset) return;
  try {
    await api("/api/assets/update", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        asset_tag: state.selectedAsset,
        actor: "operator-ui",
        reason: "UI update",
        updates,
      }),
    });
    await refreshAll();
    toast("저장되었습니다");
  } catch (error) {
    state.loading = false;
    render();
    toast(`저장 실패 — ${error.message}`, "error");
  }
}

async function updateApprovalWorkflow(updates) {
  if (!state.selectedApproval) return;
  try {
    await api("/api/approval-workflow", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        docId: state.selectedApproval,
        actor: "operator-ui",
        summary: "8000 내부 저장",
        ...updates,
      }),
    });
    await loadRows();
    await loadDetail();
    render();
    toast("저장되었습니다");
  } catch (error) {
    state.loading = false;
    render();
    toast(`저장 실패 — ${error.message}`, "error");
  }
}

async function updateJiraMissingWorkflow(status) {
  if (!state.selectedAsset) return;
  try {
    await api("/api/jira-missing-workflow", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        asset_tag: state.selectedAsset,
        status,
        note: status === "register_needed" ? "실자산 등록 필요" : status === "jira_stale" ? "Jira 항목 stale 정리 필요" : "",
        actor: "operator-ui",
      }),
    });
    await loadRows();
    await loadDetail();
    render({ preserveScroll: true });
    toast("저장되었습니다");
  } catch (error) {
    state.loading = false;
    render({ preserveScroll: true });
    toast(`저장 실패 — ${error.message}`, "error");
  }
}

function toast(message, kind = "info") {
  const node = document.getElementById("toast");
  if (!node) return;
  node.setAttribute("role", "status");
  node.setAttribute("aria-live", kind === "error" ? "assertive" : "polite");
  node.innerHTML = `<div class="toast${kind === "error" ? " toast-error" : ""}">${html(message)}</div>`;
  setTimeout(() => {
    const current = document.getElementById("toast");
    if (current) current.innerHTML = "";
  }, kind === "error" ? 4200 : 1800);
}

window.addEventListener("popstate", async () => {
  const params = new URLSearchParams(window.location.search);
  const nextMode = params.get("docId") || params.get("mode") === "approvals"
    ? "approvals"
    : (visibleModes.has(params.get("mode") || "") ? params.get("mode") : "dashboard");
  state.mode = nextMode;
  state.assetPrefix = params.get("prefix") || "";
  state.ownerFilter = params.get("owner") || "";
  state.assetKind = params.get("kind") || "";
  state.assetClass = params.get("class") || "";
  state.dupTagOnly = params.get("dup_tag") === "1";
  state.dupSerialOnly = params.get("dup_serial") === "1";
  state.sourceIncludeHidden = params.get("include_hidden") === "1";
  state.changeAsset = params.get("asset_tag") || "";
  state.changeSource = params.get("source") || "";
  state.changeField = params.get("field") || "";
  state.query = params.get("q") || "";
  state.selectedApproval = params.get("docId") || "";
  state.selectedAsset = "";
  await loadRows();
  await loadDetail();
  render();
});

refreshAll();
