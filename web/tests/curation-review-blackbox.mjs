import { chromium } from "playwright";
import { mkdir } from "node:fs/promises";
import { resolve } from "node:path";

const baseUrl = process.argv[2];
const route = process.argv[3];
const captureRoot = process.env.PPTEXTRACT_CAPTURE_DIR;
if (!baseUrl || !route) throw new Error("缺少 base URL 或策展路由");
if (captureRoot) await mkdir(captureRoot, { recursive: true });

const browser = await chromium.launch({
  executablePath: process.env.PPTEXTRACT_CHROME ?? "/usr/bin/google-chrome",
  headless: true,
  args: ["--no-sandbox", "--disable-dev-shm-usage"],
});

const checks = [];
try {
  const compact = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  compact.setDefaultTimeout(30_000);
  await compact.goto(`${baseUrl}${route}`, { waitUntil: "domcontentloaded", timeout: 10_000 });
  await compact.getByRole("heading", { name: "来源日志" }).waitFor();
  for (const [name, locator] of [
    ["页清单", compact.locator(".page-rail")],
    ["标准页渲染", compact.locator(".evidence-panel")],
    ["来源与策展日志", compact.locator(".inspector-panel")],
  ]) {
    if (!(await locator.isVisible())) throw new Error(`1280px 缺少工作台区域：${name}`);
  }
  const compactOverflow = await compact.evaluate(
    () => document.documentElement.scrollWidth > window.innerWidth,
  );
  if (compactOverflow) throw new Error("1280px 存在页面级横向溢出");
  const pageListOverflow = await compact.locator(".page-list").evaluate(
    (element) => getComputedStyle(element).overflowY,
  );
  if (!["auto", "scroll"].includes(pageListOverflow)) {
    throw new Error("长文档页清单无法纵向滚动");
  }
  if (captureRoot) {
    await compact.screenshot({ path: resolve(captureRoot, "curation-1280.png") });
  }
  checks.push("viewport-1280-three-columns");
  await compact.close();

  const page = await browser.newPage({ viewport: { width: 1440, height: 1024 } });
  page.setDefaultTimeout(30_000);
  await page.goto(`${baseUrl}${route}`, { waitUntil: "domcontentloaded", timeout: 10_000 });
  const body = page.getByRole("textbox", { name: "正文来源 1 当前编辑值" });
  await body.waitFor();
  await body.fill(`${await body.inputValue()}（浏览器人工核对）`);
  await page.getByText("已修改，原确认失效").waitFor();

  const bodyText = await page.locator("body").innerText();
  for (const forbidden of [
    "overview",
    "VLM",
    "自动批准",
    "自动补全",
    "自动候选框",
    "批量批准",
    "批量确认来源",
    "跨页复制",
    "事实性标注模板",
  ]) {
    if (bodyText.includes(forbidden)) throw new Error(`工作台出现范围外入口：${forbidden}`);
  }

  await page.getByRole("button", { name: "保存修改" }).click();
  const confirm = page.getByRole("button", { name: "确认文字来源" });
  await confirm.waitFor();
  await page.waitForFunction(
    () => document.activeElement?.textContent?.trim() === "确认文字来源",
  );
  await confirm.click();
  const review = page.getByRole("button", { name: "完成来源审核" });
  await page.waitForFunction(
    () => document.activeElement?.textContent?.trim() === "完成来源审核",
  );
  await review.click();
  await page.getByRole("button", { name: "有缺口，在页面上框选" }).waitFor();
  if ((await page.locator(".capture-range").count()) !== 0) {
    throw new Error("来源审核完成后自动显示了候选框");
  }
  await page.getByRole("button", { name: "来源完整，直接审核" }).click();
  await page.getByText("来源完整 · 无需截图").waitFor();
  if (captureRoot) {
    await page.screenshot({ path: resolve(captureRoot, "curation-1440-ready.png") });
  }
  const approve = page.getByRole("button", { name: "批准并转到下一待处理页" });
  await page.waitForFunction(
    () => document.activeElement?.textContent?.trim() === "批准并转到下一待处理页",
  );
  await page.keyboard.press("a");
  await page.getByText(/上一页已批准|待处理队列已清空/).waitFor();
  checks.push("plain-text-zero-capture-approved");
  checks.push("keyboard-a-approved");

  await page.getByRole("button", { name: "全部" }).click();
  const approvedRow = page.getByRole("button", {
    name: /公开浏览器策展页，已批准/,
  });
  await approvedRow.waitFor();
  await approvedRow.click();
  await page.getByText("批准结论已冻结").waitFor();
  await page.getByRole("button", { name: "重新打开此页" }).focus();
  await page.keyboard.press("r");
  const reopenDialog = page.getByRole("dialog", { name: "重新打开第 1 页？" });
  await reopenDialog.waitFor();
  await page.keyboard.press("Escape");
  await reopenDialog.waitFor({ state: "hidden" });
  await page.keyboard.press("r");
  await page.getByRole("button", { name: "确认重新打开" }).click();
  await page.getByText("页面已重新打开，恢复为待处理并解锁编辑。").waitFor();
  await page.locator(".review-gate").click({ position: { x: 8, y: 8 } });
  await page.keyboard.press("x");
  const exclusionReason = page.getByRole("combobox", { name: "整页排除原因" });
  if (!(await exclusionReason.evaluate((element) => element === document.activeElement))) {
    throw new Error("X 未把焦点送到整页排除原因");
  }
  await exclusionReason.selectOption("irrelevant");
  await page.getByRole("button", { name: "排除并转到下一待处理页" }).click();
  await page.getByText("上一页已排除。已转到下一待处理页。").waitFor();
  checks.push("keyboard-r-reopen-and-x-exclude");

  await page.getByRole("button", { name: "待处理", exact: true }).click();
  for (const title of ["公开批量策展页二", "公开批量策展页三"]) {
    await page.getByRole("checkbox", { name: new RegExp(`选择第 1 页，${title}`) }).check();
  }
  const batch = page.getByRole("region", { name: "批量排除" });
  await batch.getByRole("combobox", { name: "统一排除原因" }).selectOption("duplicate");
  await batch.getByRole("button", { name: "批量排除 2 页" }).click();
  await page.getByText("已批量排除 2 页。每页均已分别记录审核事件。").waitFor();
  for (const title of ["公开批量策展页二", "公开批量策展页三"]) {
    await page.getByRole("checkbox", { name: new RegExp(`选择第 1 页，${title}`) }).waitFor({ state: "hidden" });
  }
  checks.push("pending-only-batch-exclusion");
  checks.push("forbidden-batch-actions-absent");
  await page.close();

  process.stdout.write(JSON.stringify({ ok: true, checks }));
} finally {
  await browser.close();
}
