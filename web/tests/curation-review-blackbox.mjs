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
  for (const forbidden of ["overview", "VLM", "自动批准", "自动补全", "视觉对象", "框选"]) {
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
  await page.close();

  process.stdout.write(JSON.stringify({ ok: true, checks }));
} finally {
  await browser.close();
}
