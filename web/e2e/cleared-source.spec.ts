import { expect, test } from "playwright/test";

for (const width of [1280, 1440, 640]) {
test(`批准后隐藏空白正文并保留原段号审计 ${width}`, async ({ page }, testInfo) => {
  await page.setViewportSize({ width, height: 900 });
  const source = { titles: ["公开测试标题"], body: ["清空的长原文。".repeat(80), "保留第二段", "第三段原文"], tables: [], images: [], speaker_notes: [] };
  await page.route("**/api/v1/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    const summary = { page_id: "page-cleared", document_id: "doc", version_id: "version", page_number: 1, review_status: "approved", title: "公开测试标题", hidden: false, enabled: true };
    if (path.endsWith("/bootstrap")) return route.fulfill({ json: { actor: { actor_id: "tester" }, runways: [] } });
    if (path.endsWith("/curation/pages")) return route.fulfill({ json: { pages: [summary] } });
    if (path.endsWith("/pages/page-cleared")) return route.fulfill({ json: {
      ...summary, source_content: source, curation: {
        current_snapshot: { snapshot_id: "snapshot", source_content: { ...source, body: ["", "保留第二段", " \n "] }, created_by: "tester", created_at: "2026-09-02T08:00:00Z", source_confirmation: { actor_id: "tester", confirmed_at: "2026-09-02T08:00:00Z" } },
        image_sources: { total: 0, unresolved: 0, items: [] }, chunk_body: { nonempty: true }, blockers: [], can_approve: false,
      },
    } });
    return route.fulfill({ status: 404, json: {} });
  });
  await page.goto("/curation");
  await page.getByRole("button", { name: "展开文字核对" }).click();
  const preview = page.getByRole("region", { name: "正文整稿预览" });
  await expect(preview.locator("article")).toHaveCount(1);
  await expect(preview).toContainText("正文 02");
  for (const mode of ["preview", "expanded"]) {
    if (mode === "expanded") {
      await page.getByRole("button", { name: "放大查看正文", exact: true }).click();
      await expect(page.getByRole("button", { name: /关闭正文放大视图/ })).toBeFocused();
    }
    const trigger = page.getByRole("button", { name: "隐藏来源 · 2", exact: true });
    await trigger.focus();
    await page.keyboard.press("Enter");
    const audit = page.getByRole("dialog", { name: "正文 01 · 来源审计" });
    await expect(audit.getByRole("region", { name: "正文 01 当前值", exact: true })).toContainText("当前值为空");
    await expect(audit.getByRole("region", { name: "正文 01 AnyDoc 原文", exact: true })).toContainText(source.body[0]);
    const originalText = audit.getByRole("region", { name: "正文 01 AnyDoc 原文", exact: true });
    await originalText.scrollIntoViewIfNeeded();
    await expect(originalText).toBeVisible();
    expect(await originalText.evaluate((element) => element.scrollHeight <= element.clientHeight)).toBe(true);
    await page.screenshot({ path: testInfo.outputPath(`hidden-long-original-${mode}.png`) });
    await expect(audit).toContainText("tester");
    await audit.getByRole("button", { name: "正文 03，仅含空白字符", exact: true }).click();
    const nextAudit = page.getByRole("dialog", { name: "正文 03 · 来源审计" });
    await expect(nextAudit.getByRole("region", { name: "正文 03 当前值", exact: true })).toContainText("仅含空白字符");
    await expect(nextAudit.getByRole("textbox")).toHaveCount(0);
    await page.screenshot({ path: testInfo.outputPath(`hidden-audit-${mode}.png`) });
    await nextAudit.getByRole("button", { name: "返回正文", exact: true }).focus();
    await page.keyboard.press("Tab");
    await expect(nextAudit.getByRole("button", { name: /关闭.*来源审计/ })).toBeFocused();
    await page.keyboard.press("Escape");
    await expect(trigger).toBeFocused();
  }
  const expanded = page.getByRole("region", { name: "连续正文编辑面" });
  await expect(expanded.getByRole("textbox")).toHaveCount(1);
  await expect(expanded.getByRole("textbox")).toHaveAttribute("aria-readonly", "true");
});

}
