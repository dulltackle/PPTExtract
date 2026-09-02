import { chromium } from "playwright";
import { mkdir } from "node:fs/promises";
import { resolve } from "node:path";
import { assertWcag22AA } from "./accessibility.mjs";

const baseUrl = process.argv[2];
const route = process.argv[3];
const captureRoot = process.env.PPTEXTRACT_CAPTURE_DIR;
if (!baseUrl || !route) throw new Error("缺少 base URL 或策展路由");
if (captureRoot) await mkdir(captureRoot, { recursive: true });

const browser = await chromium.launch({
  ...(process.env.PPTEXTRACT_CHROME
    ? { executablePath: process.env.PPTEXTRACT_CHROME }
    : {}),
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
  await compact.screenshot();
  await assertWcag22AA(compact, "1280×900 策展工作台");
  checks.push("viewport-1280-three-columns");
  checks.push("wcag22aa-1280");
  await compact.close();

  for (const viewport of [
    { width: 1024, height: 720, label: "125%" },
    { width: 640, height: 450, label: "200%" },
  ]) {
    const zoomed = await browser.newPage({ viewport });
    zoomed.setDefaultTimeout(30_000);
    await zoomed.goto(`${baseUrl}${route}`, { waitUntil: "domcontentloaded", timeout: 10_000 });
    await zoomed.getByRole("heading", { name: "来源日志" }).waitFor();
    const workspace = zoomed.locator(".curation-workspace");
    const overflow = await workspace.evaluate((element) => ({
      overflowX: getComputedStyle(element).overflowX,
      scrollWidth: element.scrollWidth,
      clientWidth: element.clientWidth,
    }));
    if (overflow.overflowX !== "auto" || overflow.scrollWidth <= overflow.clientWidth) {
      throw new Error(`${viewport.label} 缩放下三栏没有提供明确的横向滚动路径`);
    }
    await workspace.evaluate((element) => {
      element.scrollLeft = element.scrollWidth - element.clientWidth;
    });
    const inspectorBox = await zoomed.locator(".inspector-panel").boundingBox();
    if (!inspectorBox || inspectorBox.x >= viewport.width || inspectorBox.x + inspectorBox.width <= 0) {
      throw new Error(`${viewport.label} 缩放下无法滚动到来源与审核动作`);
    }
    await zoomed
      .getByRole("button", { name: /文字一致，确认|保存并确认修改|确认无标题\/正文来源/ })
      .waitFor();
    checks.push(`zoom-${viewport.label}-reachable`);
    await zoomed.close();
  }

  const textZoomed = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  textZoomed.setDefaultTimeout(30_000);
  await textZoomed.goto(`${baseUrl}${route}`, { waitUntil: "domcontentloaded", timeout: 10_000 });
  await textZoomed.getByRole("heading", { name: "来源日志" }).waitFor();
  await textZoomed.evaluate(() => {
    for (const element of document.querySelectorAll("body *")) {
      if (element instanceof SVGElement) continue;
      const style = getComputedStyle(element);
      const fontSize = Number.parseFloat(style.fontSize);
      const lineHeight = Number.parseFloat(style.lineHeight);
      if (Number.isFinite(fontSize) && fontSize > 0) {
        element.style.fontSize = `${fontSize * 2}px`;
      }
      if (Number.isFinite(lineHeight) && lineHeight > 0) {
        element.style.lineHeight = `${lineHeight * 2}px`;
      }
    }
  });
  const textZoomWorkspace = textZoomed.locator(".curation-workspace");
  const textZoomOverflow = await textZoomWorkspace.evaluate((element) => ({
    overflowX: getComputedStyle(element).overflowX,
    scrollWidth: element.scrollWidth,
    clientWidth: element.clientWidth,
  }));
  if (textZoomOverflow.overflowX !== "auto") {
    throw new Error("200% 文字缩放下工作台没有显式横向滚动路径");
  }
  if (await textZoomed.evaluate(() => document.documentElement.scrollWidth > window.innerWidth)) {
    throw new Error("200% 文字缩放造成页面级横向溢出");
  }
  await textZoomWorkspace.evaluate((element) => {
    element.scrollLeft = element.scrollWidth - element.clientWidth;
  });
  await textZoomed.locator(".source-review-scroll").evaluate((element) => {
    element.scrollTop = element.scrollHeight;
  });
  const exclusionAction = textZoomed.getByRole("button", { name: "排除并转到下一待处理页" });
  await exclusionAction.scrollIntoViewIfNeeded();
  const exclusionBox = await exclusionAction.boundingBox();
  if (!exclusionBox || exclusionBox.x >= 1280 || exclusionBox.x + exclusionBox.width <= 0) {
    throw new Error("200% 文字缩放下无法到达整页审核动作");
  }
  const textCommandStrip = textZoomed.getByRole("contentinfo", { name: "工作位状态" });
  const textZoomStatus = textCommandStrip.locator(".command-status");
  await textZoomStatus.waitFor();
  if (!(await textZoomStatus.textContent())?.trim()) {
    throw new Error("200% 文字缩放下当前页状态上下文为空");
  }
  checks.push("text-zoom-200%-reachable");
  await textZoomed.close();

  const page = await browser.newPage({ viewport: { width: 1440, height: 1024 } });
  page.setDefaultTimeout(30_000);
  await page.goto(`${baseUrl}${route}`, { waitUntil: "domcontentloaded", timeout: 10_000 });
  const textReviewRequests = [];
  page.on("request", (request) => {
    if (
      request.method() === "POST" &&
      new URL(request.url()).pathname.endsWith("/curation/text-review")
    ) {
      textReviewRequests.push(new URL(request.url()).pathname);
    }
  });
  await page.getByRole("button", { name: "文字一致，确认" }).waitFor();
  const body = page.getByRole("textbox", { name: "正文来源 1 当前编辑值" });
  await body.waitFor();
  await body.fill(`${await body.inputValue()}（浏览器人工核对）`);
  await page.getByText("有本地修改").waitFor();
  await page.getByRole("button", { name: "保存并确认修改" }).waitFor();

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

  await page.getByRole("button", { name: "保存并确认修改" }).click();
  await page.getByText("文字及来源审核均已完成。").waitFor();
  await page.getByRole("button", { name: "展开文字核对" }).waitFor();
  if (textReviewRequests.length !== 1 || !textReviewRequests[0].endsWith("/curation/text-review")) {
    throw new Error(`文字核对未通过单一公开命令提交：${textReviewRequests.join(", ")}`);
  }
  if (await page.getByRole("button", { name: "确认文字来源" }).count()) {
    throw new Error("文字核对仍暴露旧确认动作");
  }
  checks.push("text-review-action-labels");
  checks.push("single-text-review-command");
  await page.waitForFunction(
    () => document.activeElement?.textContent?.trim() === "来源完整，直接审核",
  );
  await page.getByRole("button", { name: "有缺口，在页面上框选" }).waitFor();
  if ((await page.locator(".capture-range").count()) !== 0) {
    throw new Error("来源审核完成后自动显示了候选框");
  }
  await page.getByRole("button", { name: "来源完整，直接审核" }).click();
  await page.getByText("来源完整 · 无需截图").waitFor();
  if (captureRoot) {
    await page.screenshot({ path: resolve(captureRoot, "curation-1440-ready.png") });
  }
  await page.screenshot();
  await assertWcag22AA(page, "1440×1024 策展工作台");
  const approve = page.getByRole("button", { name: "批准并转到下一待处理页" });
  await page.waitForFunction(
    () => document.activeElement?.textContent?.trim() === "批准并转到下一待处理页",
  );
  await page.keyboard.press("a");
  await page.getByText(/上一页已批准|待处理队列已清空/).waitFor();
  checks.push("plain-text-zero-capture-approved");
  checks.push("keyboard-a-approved");
  checks.push("wcag22aa-1440");

  await page.getByRole("button", { name: "全部" }).click();
  const approvedRow = page.getByRole("button", {
    name: /公开浏览器策展页，已批准/,
  });
  await approvedRow.waitFor();
  await approvedRow.click();
  await page.getByText("批准结论已冻结").waitFor();
  await page.getByRole("button", { name: "重新打开此页" }).focus();
  await page.getByRole("button", { name: "快捷键" }).click();
  await page.keyboard.press("r");
  const reopenDialog = page.getByRole("dialog", { name: "重新打开第 1 页？" });
  await reopenDialog.waitFor();
  const commandPanel = page.locator(".command-help-panel");
  await commandPanel.getByText(/取消/).waitFor();
  if (await commandPanel.getByText(/重新打开/).count()) {
    throw new Error("重开确认弹窗打开时命令条仍显示 R 重新打开");
  }
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
