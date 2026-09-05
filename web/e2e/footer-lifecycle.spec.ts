import { expect, test } from "playwright/test";

for (const width of [1280, 1440]) {
  test(`重复页脚原位核对、隐藏审计与整组撤销 ${width}`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 900 });
    const source = { titles: ["公开材料"], body: ["清空来源", "保留正文", "公开重复页脚", "清空且排除"], tables: [], images: [], speaker_notes: [] };
    let status = "pending";
    let otherStatus = "approved";
    let active = true;
    let allHidden = false;
    let mutations = 0;
    const detail = () => ({
      page_id: "p1", document_id: "doc", version_id: "v1", page_number: 1, review_status: status,
      source_content: source,
      curation: {
        current_snapshot: { snapshot_id: "snap", source_content: { ...source, body: ["", allHidden ? "" : "保留正文", "公开重复页脚", ""] }, created_by: "确认人", created_at: "2026-09-02T08:00:00Z", source_confirmation: { actor_id: "确认人", confirmed_at: "2026-09-02T08:00:00Z" } },
        image_sources: { total: 0, unresolved: 0, items: [] }, blockers: [], can_approve: false,
        chunk_body: { nonempty: true, preview: active ? "公开材料\n\n保留正文" : "公开材料\n\n保留正文\n\n公开重复页脚" },
        repeated_footer_noise: {
          sources: source.body.map((text, index) => ({ source_ref: `s${index}`, source_kind: "body", source_index: index, text, active_confirmation_id: active && index >= 2 ? "group" : null })),
          history: [2, 3].map((index) => ({
            confirmation_id: "group", source_ref: `s${index}`, source_text: source.body[index], rule_version: "manual-exact-text-v1", confirmed_by: "确认人", confirmed_at: "2026-09-02T08:00:00Z", confirmation_note: "已逐页核对", status: active ? "active" : "revoked",
            revoked_by: active ? null : "撤销人", revoked_at: active ? null : "2026-09-02T09:00:00Z", revoke_note: active ? null : "范围调整",
            affected_pages: [{ page_id: "p1", page_version_id: "pv1", page_number: 1, review_status: status }, { page_id: "p2", page_version_id: "pv2", page_number: 2, review_status: otherStatus }],
          })),
        },
        chunk_metadata: { excluded_repeated_footer_noise: active ? [2, 3].map((index) => ({ confirmation_id: "group", source_ref: `s${index}`, source_text: source.body[index], rule_version: "manual-exact-text-v1", confirmed_by: "确认人", confirmed_at: "2026-09-02T08:00:00Z" })) : [] },
      },
    });
    await page.route("**/api/v1/**", async (route) => {
      const path = new URL(route.request().url()).pathname;
      if (route.request().method() === "POST" && !path.includes("runtime-facts")) mutations++;
      if (path.endsWith("/bootstrap")) return route.fulfill({ json: { actor: { actor_id: "确认人" }, runways: [] } });
      if (path.endsWith("/curation/pages")) return route.fulfill({ json: { pages: [{ ...detail(), title: "公开材料", hidden: false, enabled: true }] } });
      if (path.endsWith("/pages/p1")) return route.fulfill({ json: detail() });
      if (path.endsWith("/reopen")) { status = "pending"; return route.fulfill({ json: { review: { status }, curation: detail().curation } }); }
      if (path.endsWith("/revoke")) { active = false; return route.fulfill({ json: { confirmation: { status: "revoked" } } }); }
      return route.fulfill({ status: 404, json: {} });
    });
    await page.goto("/curation");
    await page.getByRole("button", { name: "展开文字核对" }).click();
    const preview = page.getByRole("region", { name: "正文整稿预览" });
    await expect(preview.locator("article")).toHaveCount(4);
    const trigger = preview.getByRole("button", { name: "正文 03，已排除重复页脚，未修改，打开来源审计" });
    await expect(trigger).toContainText("已排除");
    await expect(preview.locator('[data-source-text-block="body-2"]')).toContainText("公开重复页脚");
    await page.screenshot({ path: testInfo.outputPath("pending-preview.png") });
    await trigger.focus();
    await page.keyboard.press("Enter");
    let audit = page.getByRole("dialog", { name: "正文 03 · 来源审计" });
    const revoke = () => audit.getByRole("button", { name: "撤销正文来源 3 的重复页脚排除" });
    await expect(revoke()).toBeDisabled();
    await expect(audit).toContainText("第 2 页 · 已批准，需重新打开");
    await expect(audit.getByRole("link", { name: /定位第 2 页/ })).toHaveAttribute("href", "/curation?filter=all&document=doc&page=2");
    await page.keyboard.press("Escape");
    await expect(trigger).toBeFocused();
    await page.getByRole("button", { name: "放大查看正文", exact: true }).click();
    await expect(page.getByRole("region", { name: "连续正文编辑面" }).locator("article")).toHaveCount(4);
    await expect(page.getByRole("button", { name: "正文 03，已排除重复页脚，未修改，打开来源审计" })).toContainText("已排除");
    await page.screenshot({ path: testInfo.outputPath("pending-expanded.png") });
    await page.keyboard.press("Escape");
    await page.getByRole("button", { name: "修改文字", exact: true }).click();
    await preview.getByRole("button", { name: "从正文 03 打开放大视图", exact: true }).click();
    const editor = page.getByRole("textbox", { name: "正文 03 当前编辑值", exact: true });
    await expect(editor).toBeFocused();
    await editor.fill("页脚本地修订");
    const draftTrigger = page.getByRole("button", { name: "正文 03，已排除重复页脚，已修改，打开来源审计" });
    await draftTrigger.click();
    audit = page.getByRole("dialog", { name: "正文 03 · 来源审计" });
    await expect(audit.getByRole("region", { name: "正文 03 当前值", exact: true })).toContainText("页脚本地修订");
    await expect(audit.getByRole("button", { name: "刷新整组审核状态" })).toBeDisabled();
    await expect(revoke()).toBeDisabled();
    await page.keyboard.press("Escape");
    await expect(draftTrigger).toBeFocused();
    await expect(editor).toHaveText("页脚本地修订");
    await editor.fill("公开重复页脚");
    await page.keyboard.press("Escape");

    status = "approved";
    await page.reload();
    await page.getByRole("button", { name: "展开文字核对" }).click();
    await expect(preview.locator("article")).toHaveCount(1);
    await expect(preview).toContainText("正文 02");
    for (const mode of ["preview", "expanded"]) {
      if (mode === "expanded") {
        await page.getByRole("button", { name: "放大查看正文", exact: true }).click();
        await expect(page.getByRole("button", { name: /关闭正文放大视图/ })).toBeFocused();
      }
      const hidden = page.getByRole("button", { name: "隐藏来源 · 3", exact: true });
      await hidden.focus();
      await page.keyboard.press("Enter");
      await page.getByRole("button", { name: "正文 03，已排除重复页脚", exact: true }).focus();
      await page.keyboard.press("Enter");
      audit = page.getByRole("dialog", { name: "正文 03 · 来源审计" });
      await expect(audit.getByRole("region", { name: "正文 03 AnyDoc 原文", exact: true })).toContainText("公开重复页脚");
      await expect(audit).toContainText("manual-exact-text-v1");
      await expect(audit).toContainText("确认人");
      await expect(audit).toContainText("已逐页核对");
      await expect(revoke()).toBeDisabled();
      await page.screenshot({ path: testInfo.outputPath(`approved-audit-${mode}.png`) });
      await page.keyboard.press("Escape");
      await expect(hidden).toBeFocused();
    }
    await expect(page.getByRole("region", { name: "连续正文编辑面" }).locator("article")).toHaveCount(1);
    expect(mutations).toBe(0);
    allHidden = true;
    await page.reload();
    await page.getByRole("button", { name: "展开文字核对" }).click();
    await expect(preview).toContainText("无保留正文，4 段来源可审计");
    await expect(preview.locator("article")).toHaveCount(0);
    await page.getByRole("button", { name: "放大查看正文", exact: true }).click();
    await expect(page.getByRole("region", { name: "连续正文编辑面" })).toContainText("无保留正文，4 段来源可审计");
    allHidden = false;
    await page.reload();
    await page.getByRole("button", { name: "展开文字核对" }).click();
    await page.getByRole("button", { name: "重新打开此页", exact: true }).click();
    await page.getByRole("button", { name: "确认重新打开", exact: true }).click();
    await page.getByRole("button", { name: "正文 03，已排除重复页脚，未修改，打开来源审计" }).click();
    audit = page.getByRole("dialog", { name: "正文 03 · 来源审计" });
    await expect(revoke()).toBeDisabled();
    otherStatus = "pending";
    await audit.getByRole("button", { name: "刷新整组审核状态" }).click();
    await expect(revoke()).toBeEnabled();
    await revoke().click();
    await expect(audit).toContainText("历史排除已撤销");
    await expect(audit).toContainText("撤销人");
    await expect(audit).toContainText("范围调整");
    await page.keyboard.press("Escape");
    await expect(preview.getByRole("button", { name: "正文 03，未修改，打开来源审计" })).toBeVisible();
    expect(mutations).toBe(2);
  });
}
