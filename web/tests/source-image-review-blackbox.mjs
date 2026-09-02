import { mkdir } from "node:fs/promises";
import { resolve } from "node:path";
import { chromium } from "playwright";

const baseUrl = process.argv[2];
const routePrefix = process.argv[3];
if (!baseUrl || !routePrefix) throw new Error("缺少 base URL 或图片策展路由");

const browser = await chromium.launch({
  ...(process.env.PPTEXTRACT_CHROME
    ? { executablePath: process.env.PPTEXTRACT_CHROME }
    : {}),
  headless: true,
  args: ["--no-sandbox", "--disable-dev-shm-usage"],
});

const checks = [];
const routeFor = (pageNumber) => `${baseUrl}${routePrefix}&page=${pageNumber}`;
const captureRoot = process.env.PPTEXTRACT_CAPTURE_DIR
  ? resolve(process.env.PPTEXTRACT_CAPTURE_DIR)
  : null;
if (captureRoot) await mkdir(captureRoot, { recursive: true });

async function saveAndConfirmText(page) {
  const reviewText = page.getByRole("button", {
    name: /^(文字一致，确认|保存并确认修改|确认无标题\/正文来源)$/,
  });
  if (await reviewText.isEnabled()) await reviewText.click();
  await page.getByRole("heading", { name: "图片来源" }).waitFor();
  await page.getByRole("radio", { name: "保留原始图片" }).waitFor();
}

try {
  const compact = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  compact.setDefaultTimeout(30_000);
  await compact.goto(routeFor(3), { waitUntil: "domcontentloaded", timeout: 10_000 });
  await compact.getByRole("heading", { name: "来源日志" }).waitFor();
  if (await compact.evaluate(() => document.documentElement.scrollWidth > window.innerWidth)) {
    throw new Error("1280px 图片来源工作台存在页面级横向溢出");
  }
  for (const selector of [".page-rail", ".evidence-panel", ".inspector-panel"]) {
    if (!(await compact.locator(selector).isVisible())) {
      throw new Error(`1280px 缺少三栏区域：${selector}`);
    }
  }
  await saveAndConfirmText(compact);
  if (captureRoot) {
    await compact.screenshot({ path: resolve(captureRoot, "user-1280.png"), fullPage: true });
  }
  checks.push("image-viewport-1280-three-columns");
  await compact.close();

  const keep = await browser.newPage({ viewport: { width: 1440, height: 1024 } });
  keep.setDefaultTimeout(30_000);
  await keep.goto(routeFor(1), { waitUntil: "domcontentloaded", timeout: 10_000 });
  await saveAndConfirmText(keep);
  await keep.getByRole("radio", { name: "保留原始图片" }).check();
  await keep.getByRole("textbox", { name: "图片来源 01 summary" }).fill(
    "公开蓝色来源图用于验证单项保留的原始资产路径。",
  );
  await keep.getByRole("button", { name: "保存并处理下一项" }).click();
  const keepReview = keep.getByRole("button", { name: "完成来源审核" });
  await keepReview.waitFor();
  await keepReview.click();
  await keep.getByRole("button", { name: "来源完整，直接审核" }).waitFor();
  checks.push("single-image-included");
  await keep.close();

  const ignore = await browser.newPage({ viewport: { width: 1440, height: 1024 } });
  ignore.setDefaultTimeout(30_000);
  await ignore.goto(routeFor(2), { waitUntil: "domcontentloaded", timeout: 10_000 });
  await saveAndConfirmText(ignore);
  await ignore.getByRole("radio", { name: "忽略此来源" }).check();
  await ignore.getByRole("combobox", { name: "图片来源 01 忽略原因" }).selectOption(
    "decorative",
  );
  await ignore.getByRole("button", { name: "保存并处理下一项" }).click();
  await ignore.getByRole("button", { name: "完成来源审核" }).click();
  await ignore.getByRole("button", { name: "来源完整，直接审核" }).waitFor();
  checks.push("single-image-ignored");
  await ignore.close();

  const mixed = await browser.newPage({ viewport: { width: 1440, height: 1024 } });
  mixed.setDefaultTimeout(30_000);
  let previewFailed = true;
  await mixed.route("**/source-images/**", async (route) => {
    if (previewFailed) {
      previewFailed = false;
      await route.fulfill({ status: 503, body: "preview unavailable" });
      return;
    }
    await route.continue();
  });
  let saveFailed = true;
  await mixed.route("**/curation/image-sources/**", async (route) => {
    if (saveFailed) {
      saveFailed = false;
      await route.fulfill({
        status: 503,
        contentType: "application/json",
        body: JSON.stringify({ error: { message: "图片处置暂时无法保存。" } }),
      });
      return;
    }
    await route.continue();
  });
  await mixed.goto(routeFor(3), { waitUntil: "domcontentloaded", timeout: 10_000 });
  await saveAndConfirmText(mixed);
  await mixed.getByText("原始预览加载失败").waitFor();
  await mixed.getByRole("button", { name: "重试原始预览" }).click();
  await mixed.getByRole("img", { name: /图片来源 01 原始预览/ }).waitFor();
  if ((await mixed.locator(".duplicate-object-chip").count()) !== 2) {
    throw new Error("同页重复引用未显示两个独立的重复对象提示");
  }
  if (captureRoot) {
    await mixed.screenshot({ path: resolve(captureRoot, "desktop.png"), fullPage: true });
  }
  await mixed.getByRole("radio", { name: "保留原始图片" }).check();
  const summary = mixed.getByRole("textbox", { name: "图片来源 01 summary" });
  if (await mixed.getByRole("button", { name: "保存并处理下一项" }).isEnabled()) {
    throw new Error("保留项缺少 summary 时保存动作仍可用");
  }
  await summary.fill("公开橙色来源图以两次独立引用验证混合处置。 ");
  await mixed.getByRole("button", { name: "保存并处理下一项" }).click();
  await mixed.getByText(/图片处置暂时无法保存.*本地图片处置仍保留/).waitFor();
  if (!(await summary.inputValue()).includes("两次独立引用")) {
    throw new Error("图片保存失败后本地 summary 丢失");
  }
  await mixed.getByRole("button", { name: "保存并处理下一项" }).click();
  const secondKeep = mixed.getByRole("radio", { name: "保留原始图片" });
  await secondKeep.waitFor();
  await mixed.waitForFunction(() => (
    document.activeElement?.getAttribute("aria-label") === "保留原始图片"
  ));
  if (!(await secondKeep.evaluate((element) => element === document.activeElement))) {
    throw new Error("保存后焦点未移动到下一项图片来源");
  }
  const secondIgnore = mixed.getByRole("radio", { name: "忽略此来源" });
  await secondIgnore.check();
  await mixed.getByRole("combobox", { name: "图片来源 02 忽略原因" }).selectOption(
    "duplicate_source",
  );
  await mixed.getByRole("button", { name: "保存并处理下一项" }).click();
  const mixedReview = mixed.getByRole("button", { name: "完成来源审核" });
  const mixedReviewHandle = await mixedReview.elementHandle();
  await mixed.waitForFunction(
    (element) => document.activeElement === element,
    mixedReviewHandle,
  );
  if (!(await mixedReview.evaluate((element) => element === document.activeElement))) {
    throw new Error("全部图片完成后焦点未移动到来源审核动作");
  }
  await mixedReview.click();
  await mixed.getByRole("button", { name: "来源完整，直接审核" }).waitFor();
  await mixed.getByRole("button", { name: /图片来源 01/ }).click();
  await mixed.getByRole("textbox", { name: "图片来源 01 summary" }).fill(
    "修改后的公开 summary 会撤销来源审核确认。",
  );
  await mixed.getByText("来源审核确认已失效").waitFor();
  let leavePrompted = false;
  mixed.once("dialog", async (dialog) => {
    leavePrompted = true;
    await dialog.accept();
  });
  await mixed.getByRole("button", { name: /第 1 页，公开单项保留页/ }).click();
  if (!leavePrompted) throw new Error("离开含未保存图片修改的页面前未明确提示");
  checks.push("mixed-duplicate-dispositions");
  checks.push("preview-and-save-recovery");
  checks.push("review-invalidated-and-leave-warning");
  checks.push("image-viewport-1440");
  await mixed.close();

  const loadFailure = await browser.newPage({ viewport: { width: 1440, height: 1024 } });
  loadFailure.setDefaultTimeout(30_000);
  await loadFailure.route("**/api/v1/pages/*", async (route) => {
    const pathname = new URL(route.request().url()).pathname;
    if (/\/api\/v1\/pages\/[^/]+$/.test(pathname)) {
      await route.fulfill({
        status: 503,
        contentType: "application/json",
        body: JSON.stringify({ error: { message: "AnyDoc 来源暂时不可用。" } }),
      });
      return;
    }
    await route.continue();
  });
  await loadFailure.goto(routeFor(3), { waitUntil: "domcontentloaded", timeout: 10_000 });
  await loadFailure.getByRole("heading", { name: "来源日志连接中断" }).waitFor();
  await loadFailure.getByRole("button", { name: "重新加载来源" }).waitFor();
  checks.push("source-load-recovery");
  await loadFailure.close();

  process.stdout.write(JSON.stringify({ ok: true, checks }));
} finally {
  await browser.close();
}
