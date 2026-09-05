import { chromium, expect } from "playwright/test";

const [baseUrl, route, pageId] = process.argv.slice(2);
const browser = await chromium.launch({ headless: true, args: ["--no-sandbox"] });
try {
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const detail = async () => (await page.request.get(`${baseUrl}/api/v1/pages/${pageId}`)).json();
  await page.goto(`${baseUrl}${route}`);
  const original = (await detail()).source_content;
  expect(original.body).toEqual(["公开待清空正文"]);
  const edit = async () => {
    const expand = page.getByRole("button", { name: "展开文字核对" });
    if (await expand.count()) await expand.click();
    const modify = page.getByRole("button", { name: "修改文字", exact: true });
    if (await modify.count()) await modify.click();
    await page.getByRole("button", { name: "从正文 01 打开放大视图" }).click();
    return page.getByRole("textbox", { name: "正文 01 当前编辑值" });
  };
  const approve = async () => {
    await page.getByRole("button", { name: "来源完整，直接审核" }).click();
    await page.getByRole("button", { name: "批准并转到下一待处理页" }).click();
    await expect.poll(async () => (await detail()).review_status).toBe("approved");
    await page.getByRole("button", { name: "全部", exact: true }).click();
    await page.getByRole("button", { name: /公开清空闭环，已批准/ }).click();
    await page.getByRole("button", { name: "展开文字核对" }).click();
  };
  await (await edit()).fill("");
  await page.getByRole("button", { name: "正文 01，当前值为空，已修改，打开来源审计" }).click();
  await expect(page.getByRole("region", { name: "正文 01 当前值", exact: true })).toContainText("当前值为空");
  await page.keyboard.press("Escape");
  await page.getByRole("button", { name: "保存并确认修改" }).click();
  await expect.poll(async () => (await detail()).curation.current_snapshot.source_content.body).toEqual([""]);
  const clearedSnapshot = (await detail()).curation.current_snapshot;
  await page.reload();
  await page.getByRole("button", { name: "展开文字核对" }).click();
  await expect(page.getByRole("button", { name: "正文 01，当前值为空，已修改，打开来源审计" })).toBeVisible();
  await approve();
  await expect(page.getByRole("region", { name: "正文整稿预览" })).toHaveText("无保留正文，1 段来源可审计");
  await page.getByRole("button", { name: "隐藏来源 · 1", exact: true }).click();
  await expect(page.getByRole("region", { name: "正文 01 AnyDoc 原文", exact: true })).toContainText("公开待清空正文");
  await expect(page.getByRole("dialog", { name: "正文 01 · 来源审计" })).toContainText("blackbox-operator");
  await page.keyboard.press("Escape");
  await page.getByRole("button", { name: "放大查看正文", exact: true }).click();
  await expect(page.getByRole("region", { name: "连续正文编辑面" })).toHaveText("无保留正文，1 段来源可审计");
  await expect(page.getByRole("textbox")).toHaveCount(0);
  await page.keyboard.press("Escape");
  const refused = await page.request.post(`${baseUrl}/api/v1/pages/${pageId}/curation/text-review`, { data: { titles: original.titles, body: ["绕过批准修改"] } });
  expect(refused.status()).toBe(409);
  await page.getByRole("button", { name: "重新打开此页" }).click();
  await page.getByRole("button", { name: "确认重新打开" }).click();
  await expect(page.getByText("页面已重新打开，恢复为待处理并解锁编辑。")).toBeVisible();
  await (await edit()).fill("恢复后正文");
  await page.getByRole("button", { name: "保存并确认修改" }).click();
  await expect.poll(async () => (await detail()).curation.current_snapshot.source_content.body).toEqual(["恢复后正文"]);
  await approve();
  await expect(page.getByRole("button", { name: /隐藏来源/ })).toHaveCount(0);
  await expect(page.getByRole("region", { name: "正文整稿预览" })).toContainText("恢复后正文");
  await page.getByRole("button", { name: "重新打开此页" }).click();
  await page.getByRole("button", { name: "确认重新打开" }).click();
  await expect(page.getByText("页面已重新打开，恢复为待处理并解锁编辑。")).toBeVisible();
  await (await edit()).fill(" \n ");
  await page.getByRole("button", { name: "保存并确认修改" }).click();
  await expect.poll(async () => (await detail()).curation.current_snapshot.source_content.body).toEqual([" \n "]);
  await approve();
  await expect(page.getByRole("button", { name: "隐藏来源 · 1", exact: true })).toBeVisible();
  console.log(JSON.stringify({ original, clearedSnapshot, finalDetail: await detail() }));
} finally {
  await browser.close();
}
