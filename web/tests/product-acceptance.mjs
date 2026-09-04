import { chromium } from "playwright";

const baseUrl = process.argv[2];
const mode = process.argv[3];
const args = process.argv.slice(4);
if (!baseUrl || !["curate", "map", "publish"].includes(mode)) {
  throw new Error("缺少 base URL 或产品验收模式");
}

const browser = await chromium.launch({
  ...(process.env.PPTEXTRACT_CHROME
    ? { executablePath: process.env.PPTEXTRACT_CHROME }
    : {}),
  headless: true,
  args: ["--no-sandbox", "--disable-dev-shm-usage"],
});

async function openPage(route, pageNumber, viewport = { width: 1440, height: 1024 }) {
  const page = await browser.newPage({ viewport });
  page.setDefaultTimeout(45_000);
  const separator = route.includes("?") ? "&" : "?";
  await page.goto(`${baseUrl}${route}${separator}page=${pageNumber}`, {
    waitUntil: "domcontentloaded",
    timeout: 15_000,
  });
  await page.getByRole("heading", { name: "来源日志" }).waitFor();
  return page;
}

async function confirmText(page) {
  const reviewText = page.getByRole("button", {
    name: /^(文字一致，确认|保存并确认修改|确认无标题\/正文来源)$/,
  });
  if (await reviewText.isEnabled()) await reviewText.click();
}

async function finishSourceReview(page) {
  const review = page.getByRole("button", { name: "完成来源审核" });
  await review.waitFor();
  await review.click();
  await page.getByRole("button", { name: "来源完整，直接审核" }).waitFor();
}

async function approveWithCompleteSource(page) {
  await page.getByRole("button", { name: "来源完整，直接审核" }).click();
  await page.getByText("来源完整 · 无需截图").waitFor();
  await page.getByRole("button", { name: "批准并转到下一待处理页" }).click();
  await page.getByText(/上一页已批准|待处理队列已清空/).waitFor();
}

async function curate(route) {
  const checks = [];

  const zeroCapture = await openPage(route, 1, { width: 1280, height: 900 });
  for (const selector of [".page-rail", ".evidence-panel", ".inspector-panel"]) {
    if (!(await zeroCapture.locator(selector).isVisible())) {
      throw new Error(`1280px 缺少产品策展区域：${selector}`);
    }
  }
  if (await zeroCapture.evaluate(() => document.documentElement.scrollWidth > window.innerWidth)) {
    throw new Error("1280px 产品快乐路径存在页面级横向溢出");
  }
  await confirmText(zeroCapture);
  await zeroCapture.getByRole("button", { name: "来源完整，直接审核" }).waitFor();
  const bodyText = await zeroCapture.locator("body").innerText();
  for (const forbidden of [
    "VLM",
    "自动批准",
    "批量批准",
    "批量确认来源",
    "跨页复制",
    "事实性标注模板",
  ]) {
    if (bodyText.includes(forbidden)) throw new Error(`产品出现范围外入口：${forbidden}`);
  }
  await approveWithCompleteSource(zeroCapture);
  checks.push("zero-capture-approved");
  checks.push("forbidden-actions-absent");
  await zeroCapture.close();

  const sourceImage = await openPage(route, 2);
  if (!(await sourceImage.locator(".curation-workspace").isVisible())) {
    throw new Error("1440px 产品策展工作台不可见");
  }
  await confirmText(sourceImage);
  await sourceImage.getByRole("radio", { name: "保留原始图片" }).check();
  await sourceImage.getByRole("textbox", { name: "图片来源 01 summary" }).fill(
    "公开蓝色来源图片证明 AnyDoc 原始字节已由策展人员明确纳入。",
  );
  await sourceImage.getByRole("button", { name: "保存并处理下一项" }).click();
  await finishSourceReview(sourceImage);
  await approveWithCompleteSource(sourceImage);
  checks.push("source-image-included");
  await sourceImage.close();

  const capture = await openPage(route, 3);
  await confirmText(capture);
  await capture.getByRole("button", { name: "有缺口，在页面上框选" }).waitFor();
  await capture.getByRole("button", { name: "有缺口，在页面上框选" }).click();
  const render = capture.getByRole("img", { name: /标准页渲染结果/ });
  const renderBox = await render.boundingBox();
  if (!renderBox) throw new Error("标准页渲染结果没有可框选范围");
  await capture.mouse.move(
    renderBox.x + renderBox.width * 0.2,
    renderBox.y + renderBox.height * 0.24,
  );
  await capture.mouse.down();
  await capture.mouse.move(
    renderBox.x + renderBox.width * 0.64,
    renderBox.y + renderBox.height * 0.68,
    { steps: 5 },
  );
  await capture.mouse.up();
  const editor = capture.getByRole("dialog", { name: "视觉对象 01" });
  await editor.waitFor();
  await capture.getByRole("textbox", { name: "视觉对象 01 summary" }).fill(
    "公开人工框选区域展示由策展人员确认的稳定视觉结论。",
  );
  await capture.getByRole("combobox", { name: "视觉对象 01 类型" }).selectOption("diagram");
  await editor.getByRole("button", { name: "保存并返回审核" }).click();
  await capture.getByRole("heading", { name: "人工截图" }).waitFor();
  await capture.getByRole("button", { name: "批准并转到下一待处理页" }).click();
  await capture.getByText(/上一页已批准|待处理队列已清空/).waitFor();
  checks.push("capture-approved");
  await capture.close();

  const excluded = await openPage(route, 4);
  await excluded.getByRole("button", { name: "排除此页" }).click();
  await excluded.getByRole("combobox", { name: "整页排除原因" }).selectOption("irrelevant");
  await excluded.getByRole("button", { name: "排除并转到下一待处理页" }).click();
  await excluded.getByText(/上一页已排除|待处理队列已清空/).waitFor();
  checks.push("page-excluded");
  await excluded.close();

  return {
    ok: true,
    checks: [
      "viewport-1280",
      "viewport-1440",
      "zero-capture-approved",
      "source-image-included",
      "capture-approved",
      "page-excluded",
      "forbidden-actions-absent",
    ],
  };
}

async function mapPages(route, expectedJson) {
  const expected = JSON.parse(expectedJson);
  const page = await browser.newPage({ viewport: { width: 1440, height: 1024 } });
  page.setDefaultTimeout(45_000);
  await page.goto(`${baseUrl}${route}`, { waitUntil: "domcontentloaded", timeout: 15_000 });
  await page.getByRole("heading", { name: "页对应" }).waitFor();
  await page.getByText("旧版本仍在服务").waitFor();

  for (let index = 0; index < Object.keys(expected).length; index += 1) {
    const pageCopy = await page.locator(".mapping-decision-heading > span").innerText();
    const pageNumber = pageCopy.match(/\d+/)?.[0];
    const pageId = pageNumber ? expected[pageNumber] : null;
    if (!pageId) throw new Error(`未定义 ${pageCopy} 的产品验收对应决定`);
    await page.getByRole("radio", { name: `沿用历史页 ${pageId}` }).check();
    await page.getByRole("button", { name: "保存决定并查看下一项" }).click();
    await page.getByText(/决定已保存|全部决定已保存/).waitFor();
  }

  await page.getByRole("button", { name: "确认全部对应并启用版本" }).click();
  const dialog = page.getByRole("dialog", { name: "确认全部对应并启用版本" });
  await dialog.waitFor();
  await dialog.getByRole("button", { name: "确认并启用" }).click();
  await page.getByText("新版本已启用", { exact: true }).waitFor();
  await page.close();
  return {
    ok: true,
    checks: [
      "old-version-served-during-mapping",
      "duplicate-pages-mapped",
      "mapping-confirmed",
    ],
  };
}

async function publish() {
  const page = await browser.newPage({ viewport: { width: 1440, height: 1024 } });
  page.setDefaultTimeout(45_000);
  await page.goto(`${baseUrl}/publication`, { waitUntil: "domcontentloaded", timeout: 15_000 });
  await page.getByRole("heading", { name: "首次发布尚未建立" }).waitFor();
  await page.getByRole("button", { name: "创建发布候选" }).click();
  await page.getByText("新增 3").waitFor();
  await page.getByRole("button", { name: "确认发布" }).click();
  const gate = page.getByRole("region", { name: "最终确认" });
  await gate.getByText("3 个 Chunk · 2 个视觉资产").waitFor();
  await gate.getByRole("button", { name: "确认并开始构建" }).click();

  const deadline = Date.now() + 90_000;
  while (Date.now() < deadline) {
    if (await page.getByRole("heading", { name: "发布完成" }).count()) break;
    const refresh = page.getByRole("button", { name: "刷新活动状态" });
    if (await refresh.count()) await refresh.click();
    await page.waitForTimeout(250);
  }
  await page.getByRole("heading", { name: "发布完成" }).waitFor();
  await page.getByRole("heading", { name: "当前产物" }).waitFor();
  await page.close();
  return {
    ok: true,
    checks: ["candidate-reviewed", "publication-confirmed", "artifact-published"],
  };
}

try {
  const report = mode === "curate"
    ? await curate(args[0])
    : mode === "map"
      ? await mapPages(args[0], args[1])
      : await publish();
  process.stdout.write(JSON.stringify(report));
} finally {
  await browser.close();
}
