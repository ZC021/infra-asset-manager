const fs = require("fs");
const path = require("path");
const { chromium } = require("playwright");

const root = path.resolve(__dirname, "..");
const receiptPath = path.join(root, "var", "receipts", "computer-use-user-test.json");
const screenshotPath = path.join(root, "var", "receipts", "computer-use-local.png");
const csvDownloadPath = path.join(root, "var", "receipts", "computer-use-assets.csv");
const base = process.argv[2] || "http://127.0.0.1:8000";

function nowIso() {
  return new Date().toISOString();
}

async function visibleText(locator) {
  return (await locator.innerText()).trim();
}

async function main() {
  const browser = await chromium.launch({ headless: process.env.HEADLESS !== "0" });
  const page = await browser.newPage({ viewport: { width: 1440, height: 960 } });
  const observed = {
    title: "",
    summary_cards: {},
    clicked_modes: [],
    search_result: "",
    selected_asset: "",
    detail_heading: "",
    csv_header: "",
    csv_download: csvDownloadPath,
    screenshot: screenshotPath,
  };

  try {
    const health = await (await page.request.get(`${base}/api/health`)).json();
    observed.ui_build_id = health.ui_build_id;

    await page.goto(base, { waitUntil: "networkidle", timeout: 15000 });
    observed.title = await page.title();

    await page.waitForSelector("#summary .card", { timeout: 10000 });
    const cards = await page.locator("#summary .card").all();
    for (const card of cards) {
      const value = await visibleText(card.locator("strong"));
      const label = await visibleText(card.locator("span"));
      observed.summary_cards[label] = Number(value.replace(/,/g, "")) || value;
    }

    const modes = [
      "servers",
      "assets",
      "in_use",
      "unused",
      "needs_review",
      "owners",
      "locations",
      "maintenance",
      "changes",
      "sources",
    ];
    for (const mode of modes) {
      await page.locator(`button[data-mode="${mode}"]`).click();
      await page.waitForTimeout(150);
      const content = await page.locator("#content").innerText({ timeout: 10000 });
      if (!content.trim()) {
        throw new Error(`${mode} rendered empty content`);
      }
      observed.clicked_modes.push(mode);
    }

    await page.locator('button[data-mode="assets"]').click();
    await page.waitForFunction(() => document.querySelector('button[data-mode="assets"]')?.classList.contains("active"));
    await page.waitForSelector("#content table", { timeout: 10000 });
    await page.fill("#search", "");
    await page.waitForTimeout(150);
    await page.fill("#search", "ACME-A02-250379");
    await page.waitForFunction(() => {
      const content = document.querySelector("#content")?.innerText || "";
      return content.includes("ACME-A02-250379") && content.includes("사용중");
    });
    const searchText = await page.locator("#content").innerText({ timeout: 10000 });
    observed.search_result = searchText.slice(0, 1000);
    if (!searchText.includes("ACME-A02-250379") || !searchText.includes("사용중")) {
      throw new Error("ACME-A02-250379 search did not show in-use asset");
    }

    await page.locator('[data-asset="ACME-A02-250379"]').first().click();
    await page.waitForSelector("#detail h2", { timeout: 10000 });
    observed.selected_asset = "ACME-A02-250379";
    observed.detail_heading = await visibleText(page.locator("#detail h2"));
    const detailText = await page.locator("#detail").innerText();
    if (!detailText.includes("통합 인프라 사용 근거")) {
      throw new Error("asset detail did not show integrated infra usage reason");
    }

    const [download] = await Promise.all([page.waitForEvent("download"), page.locator("a.download").click()]);
    await download.saveAs(csvDownloadPath);
    const csvBody = fs.readFileSync(csvDownloadPath, "utf8");
    observed.csv_header = csvBody.split(/\r?\n/)[0] || "";
    if (!observed.csv_header.includes("usage_status") || !observed.csv_header.includes("primary_ip")) {
      throw new Error("CSV export header missing required columns");
    }

    await page.goto(base, { waitUntil: "networkidle", timeout: 15000 });
    await page.screenshot({ path: screenshotPath, fullPage: true });

    const payload = {
      passed: true,
      mode: "local-playwright-computer-use",
      app: "chromium",
      base,
      checked_at: nowIso(),
      simulated: false,
      observed,
    };
    fs.mkdirSync(path.dirname(receiptPath), { recursive: true });
    fs.writeFileSync(receiptPath, JSON.stringify(payload, null, 2), "utf8");
    console.log(JSON.stringify(payload, null, 2));
  } catch (error) {
    const payload = {
      passed: false,
      mode: "local-playwright-computer-use",
      app: "chromium",
      base,
      checked_at: nowIso(),
      simulated: false,
      observed,
      error: String(error && error.stack ? error.stack : error),
    };
    fs.mkdirSync(path.dirname(receiptPath), { recursive: true });
    fs.writeFileSync(receiptPath, JSON.stringify(payload, null, 2), "utf8");
    console.error(JSON.stringify(payload, null, 2));
    process.exitCode = 1;
  } finally {
    await browser.close();
  }
}

main();
