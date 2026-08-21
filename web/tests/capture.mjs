import { mkdir } from "node:fs/promises";
import { resolve } from "node:path";
import { chromium } from "playwright";

const baseUrl = process.argv[2] ?? "http://127.0.0.1:5173";
const outputRoot = resolve(process.argv[3] ?? "../.impeccable/review");
await mkdir(outputRoot, { recursive: true });

const browser = await chromium.launch({
  executablePath: process.env.PPTEXTRACT_CHROME ?? "/usr/bin/google-chrome",
  headless: true,
  args: ["--no-sandbox", "--disable-dev-shm-usage"],
});

try {
  for (const capture of [
    { name: "desktop.png", width: 1440, height: 900 },
    { name: "user-1280.png", width: 1280, height: 900 },
    { name: "hero-repro.png", width: 1584, height: 992 },
    { name: "mobile.png", width: 390, height: 844 },
  ]) {
    const page = await browser.newPage({ viewport: capture });
    await page.goto(`${baseUrl}/documents`, { waitUntil: "networkidle" });
    await page.getByRole("heading", { name: "待处理" }).waitFor();
    await page.screenshot({ path: resolve(outputRoot, capture.name), fullPage: true });
    await page.close();
  }

  const errorPage = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  await errorPage.route("**/api/v1/app/bootstrap", async (route) => {
    await route.fulfill({
      status: 503,
      contentType: "application/json",
      body: JSON.stringify({
        error: { code: "bootstrap_unavailable", message: "文档入口暂时不可用。" },
      }),
    });
  });
  await errorPage.goto(`${baseUrl}/documents`, { waitUntil: "networkidle" });
  await errorPage.getByRole("heading", { name: "文档入口连接中断" }).waitFor();
  await errorPage.screenshot({ path: resolve(outputRoot, "error.png"), fullPage: true });
  await errorPage.close();
} finally {
  await browser.close();
}
