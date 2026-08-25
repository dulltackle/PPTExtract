import { mkdir } from "node:fs/promises";
import { resolve } from "node:path";
import { chromium } from "playwright";

const baseUrl = process.argv[2];
const routes = process.argv.slice(3);
if (!baseUrl || routes.length !== 2) {
  throw new Error("缺少 base URL 或两条策展路由");
}
const captureRoot = process.env.PPTEXTRACT_CAPTURE_DIR
  ? resolve(process.env.PPTEXTRACT_CAPTURE_DIR)
  : null;
if (captureRoot) await mkdir(captureRoot, { recursive: true });

const browser = await chromium.launch({
  executablePath: process.env.PPTEXTRACT_CHROME ?? "/usr/bin/google-chrome",
  headless: true,
  args: ["--no-sandbox", "--disable-dev-shm-usage"],
});

const checks = [];
const savedBounds = [];

async function prepareReviewedPage(page) {
  await page.getByRole("heading", { name: "来源日志" }).waitFor();
  const save = page.getByRole("button", { name: "保存修改" });
  if (await save.isEnabled()) await save.click();
  const confirm = page.getByRole("button", { name: "确认文字来源" });
  await confirm.click();
  const review = page.getByRole("button", { name: "完成来源审核" });
  await review.click();
  await page.getByRole("button", { name: "有缺口，在页面上框选" }).waitFor();
  if ((await page.locator(".capture-range").count()) !== 0) {
    throw new Error("来源审核完成后自动显示了候选框");
  }
}

async function exercise(route, viewport) {
  const page = await browser.newPage({ viewport });
  page.setDefaultTimeout(30_000);
  await page.goto(`${baseUrl}${route}`, { waitUntil: "domcontentloaded", timeout: 10_000 });
  await prepareReviewedPage(page);
  await page.getByRole("button", { name: "有缺口，在页面上框选" }).click();

  const render = page.getByRole("img", { name: /标准页渲染结果/ });
  const imageBox = await render.boundingBox();
  if (!imageBox) throw new Error(`${viewport.width}px 标准页渲染结果没有可框选尺寸`);
  const start = {
    x: imageBox.x + imageBox.width * 0.18,
    y: imageBox.y + imageBox.height * 0.22,
  };
  const end = {
    x: imageBox.x + imageBox.width * 0.62,
    y: imageBox.y + imageBox.height * 0.66,
  };
  await page.mouse.move(start.x, start.y);
  await page.mouse.down();
  await page.mouse.move(end.x, end.y, { steps: 5 });
  await page.mouse.up();

  const editor = page.getByRole("dialog", { name: "视觉对象 01" });
  await editor.waitFor();
  const summary = page.getByRole("textbox", { name: "视觉对象 01 summary" });
  await page.waitForFunction(() => (
    document.activeElement?.getAttribute("aria-label") === "视觉对象 01 summary"
  ));
  const editorBox = await editor.boundingBox();
  const rangeBox = await page.locator(".capture-range").boundingBox();
  if (!editorBox || !rangeBox) throw new Error(`${viewport.width}px 缺少浮窗或框选范围`);
  const overlaps = !(
    editorBox.x + editorBox.width <= rangeBox.x ||
    rangeBox.x + rangeBox.width <= editorBox.x ||
    editorBox.y + editorBox.height <= rangeBox.y ||
    rangeBox.y + rangeBox.height <= editorBox.y
  );
  if (overlaps) throw new Error(`${viewport.width}px 浮窗遮挡当前框选范围`);
  if (
    editorBox.x < 0 || editorBox.y < 0 ||
    editorBox.x + editorBox.width > viewport.width ||
    editorBox.y + editorBox.height > viewport.height
  ) {
    throw new Error(`${viewport.width}px 浮窗超出可视区域`);
  }
  if ((await page.locator(".inspector-panel").getAttribute("inert")) === null) {
    throw new Error(`${viewport.width}px 浮窗打开时右栏未暂停全局动作`);
  }
  if (!(await page.locator(".page-row").first().isDisabled())) {
    throw new Error(`${viewport.width}px 浮窗打开时页面切换仍可用`);
  }

  await page.getByRole("button", { name: "保存并返回审核" }).click();
  await page.getByText("summary 不能为空，请写成可独立理解的结论。").waitFor();
  await page.waitForFunction(() => (
    document.activeElement?.getAttribute("aria-label") === "视觉对象 01 summary"
  ));
  if (!(await summary.evaluate((element) => element === document.activeElement))) {
    throw new Error(`${viewport.width}px 空 summary 后焦点未返回字段`);
  }
  await summary.fill(`公开折线展示 ${viewport.width}px 验收视口中的稳定增长趋势。`);
  await page.getByRole("combobox", { name: "视觉对象 01 类型" }).selectOption("chart");

  if (captureRoot) {
    await page.screenshot({
      path: resolve(captureRoot, `capture-${viewport.width}.png`),
      fullPage: true,
    });
  }
  const requestPromise = page.waitForRequest((request) => (
    request.url().includes("/curation/visuals") && request.method() === "POST"
  ));
  await page.getByRole("button", { name: "保存并返回审核" }).click();
  const request = await requestPromise;
  const payload = request.postDataJSON();
  savedBounds.push(payload.bounds);
  for (const [field, expected] of Object.entries({
    left: 0.18,
    top: 0.22,
    width: 0.44,
    height: 0.44,
  })) {
    if (Math.abs(payload.bounds[field] - expected) > 0.002) {
      throw new Error(`${viewport.width}px ${field} 未按整页显示范围归一化`);
    }
  }
  await page.getByRole("heading", { name: "人工截图" }).waitFor();
  await page.getByText(new RegExp(`${viewport.width}px 验收视口`)).waitFor();
  const approve = page.getByRole("button", { name: "批准并转到下一待处理页" });
  await page.waitForFunction(() => (
    document.activeElement?.textContent?.trim() === "批准并转到下一待处理页"
  ));
  if (!(await approve.isEnabled())) throw new Error(`${viewport.width}px 保存后批准动作未开放`);
  if (await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth)) {
    throw new Error(`${viewport.width}px 存在页面级横向溢出`);
  }
  const rightScroll = await page.locator(".source-review-scroll").evaluate(
    (element) => getComputedStyle(element).overflowY,
  );
  if (!["auto", "scroll"].includes(rightScroll)) {
    throw new Error(`${viewport.width}px 右栏不是独立滚动区域`);
  }
  checks.push(`capture-viewport-${viewport.width}`);
  await page.close();
}

try {
  await exercise(routes[0], { width: 1280, height: 900 });
  await exercise(routes[1], { width: 1440, height: 1024 });
  process.stdout.write(JSON.stringify({ ok: true, checks, savedBounds }));
} finally {
  await browser.close();
}
