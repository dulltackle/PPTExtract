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
  for (const width of [1440, 1280]) {
    const page = await browser.newPage({ viewport: { width, height: 900 } });
    page.setDefaultTimeout(5_000);
    await page.goto(`${baseUrl}/documents`, { waitUntil: "domcontentloaded", timeout: 10_000 });
    await page.getByRole("heading", { name: "待处理" }).waitFor();
    const bodyText = await page.locator("body").innerText();
    for (const expected of [
      "操作者 blackbox-operator",
      "还没有待处理文档",
      "当前没有处理中的文档",
      "还没有可策展文档",
    ]) {
      if (!bodyText.includes(expected)) throw new Error(`${width}px 缺少文本：${expected}`);
    }
    for (const forbidden of ["SQLite", "对象目录健康", "区域经营分析"] ) {
      if (bodyText.includes(forbidden)) throw new Error(`${width}px 泄漏或虚构内容：${forbidden}`);
    }
    const hasOverflow = await page.evaluate(
      () => document.documentElement.scrollWidth > window.innerWidth,
    );
    if (hasOverflow) throw new Error(`${width}px 存在页面级横向溢出`);

    const upload = page.getByRole("button", { name: /上传 PPTX/ });
    await upload.focus();
    if (!(await upload.evaluate((element) => element === document.activeElement))) {
      throw new Error(`${width}px 上传入口无法获得键盘焦点`);
    }
    await upload.click();
    await page.getByRole("status").getByText("上传流程将在 #20 接入", { exact: false }).waitFor();
    checks.push(`viewport-${width}`);
    await page.close();
  }

  const errorPage = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  errorPage.setDefaultTimeout(5_000);
  await errorPage.route("**/api/v1/app/bootstrap", async (route) => {
    await route.fulfill({
      status: 503,
      contentType: "application/json",
      body: JSON.stringify({
        error: { code: "bootstrap_unavailable", message: "文档入口暂时不可用。" },
      }),
    });
  });
  await errorPage.goto(`${baseUrl}/documents`, {
    waitUntil: "domcontentloaded",
    timeout: 10_000,
  });
  const alert = errorPage.getByRole("alert");
  await alert.getByText("文档入口暂时不可用。").waitFor();
  await errorPage.getByRole("button", { name: "重新连接" }).waitFor();
  checks.push("operator-safe-error");
  await errorPage.close();

  process.stdout.write(JSON.stringify({ ok: true, checks }));
} finally {
  await browser.close();
}
