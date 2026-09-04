import { chromium } from "playwright";

const baseUrl = process.argv[2];
if (!baseUrl) throw new Error("缺少 base URL");

const browser = await chromium.launch({
  ...(process.env.PPTEXTRACT_CHROME
    ? { executablePath: process.env.PPTEXTRACT_CHROME }
    : {}),
  headless: true,
  args: ["--no-sandbox", "--disable-dev-shm-usage"],
});

const checks = [];
try {
  for (const viewport of [
    { width: 1440, height: 1024 },
    { width: 1280, height: 900 },
  ]) {
    const page = await browser.newPage({ viewport });
    page.setDefaultTimeout(45_000);
    await page.goto(`${baseUrl}/curation`, { waitUntil: "domcontentloaded", timeout: 10_000 });
    await page.getByRole("heading", { name: "逐页策展" }).waitFor();
    if ((await page.getByText("隐藏页 · 未处理").count()) !== 0) {
      throw new Error("默认待处理视图不应显示隐藏页");
    }
    if (viewport.width === 1440) checks.push("pending-excludes-hidden");

    await page.getByRole("button", { name: "全部" }).click();
    const hiddenRow = page.getByRole("button", {
      name: /第 2 页，隐藏页 · 未处理，默认跳过/,
    });
    await hiddenRow.waitFor();
    await hiddenRow.click();
    await page.getByText("此页尚未生成标准渲染").waitFor();
    await page.getByRole("heading", { name: "源页登记" }).waitFor();
    await page.getByText("ppt/slides/slide2.xml").waitFor();
    if ((await page.getByText("AnyDoc 来源").count()) !== 0) {
      throw new Error("未启用隐藏页不应伪造 AnyDoc 来源");
    }
    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth > window.innerWidth,
    );
    if (overflow) throw new Error(`${viewport.width}px 存在页面级横向溢出`);
    checks.push(`viewport-${viewport.width}x${viewport.height}`);

    if (viewport.width === 1280) {
      const enable = page.getByRole("button", { name: "启用并处理此页" });
      await enable.dblclick();
      const taskStatus = page.getByRole("status");
      await taskStatus.waitFor();
      if (!(await taskStatus.evaluate((element) => element === document.activeElement))) {
        throw new Error("提交后焦点未移动到任务状态通知");
      }
      await page.getByText("处理完成，页面已进入待处理队列。").waitFor();
      await page.getByRole("img", { name: "第 2 页标准页渲染结果" }).waitFor();
      await page.getByText(/普通策展/).waitFor();
      checks.push("persistent-enable-to-pending");
    }
    await page.close();
  }

  process.stdout.write(JSON.stringify({ ok: true, checks }));
} finally {
  await browser.close();
}
