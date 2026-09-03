import { expect, test, type Page } from "playwright/test";

const body = Array.from({ length: 11 }, (_, index) => (
  index === 5
    ? ""
    : `正文块 ${String(index + 1).padStart(2, "0")}：` +
      "这是一段用于真实浏览器核对自然换行、完整内容和连续阅读节奏的公开长文本。".repeat(index + 2)
));

const source = {
  titles: ["公开长标题：用于核对整页来源文字的稳定身份与阅读顺序"],
  body,
  tables: [],
  images: [],
  speaker_notes: [],
};

const pageSummary = {
  page_id: "page-browser",
  chunk_id: "chunk-browser",
  document_id: "document-browser",
  version_id: "version-browser",
  page_number: 1,
  review_status: "pending",
  title: source.titles[0],
  hidden: false,
  enabled: true,
  source_reference: {
    slide_id: 256,
    relationship_id: "rId7",
    part: "ppt/slides/slide1.xml",
  },
  enablement: null,
};

function curation(snapshot: null | Record<string, unknown>) {
  const confirmed = Boolean(snapshot);
  return {
    current_snapshot: snapshot,
    image_sources: { total: 0, unresolved: 0, items: [] },
    chunk_body: { nonempty: true },
    blockers: confirmed ? [] : [
      { code: "source_unsaved", message: "文字修改尚未保存。" },
      { code: "source_unconfirmed", message: "文字来源尚未确认。" },
      { code: "source_review_incomplete", message: "来源审核尚未完成。" },
    ],
    can_confirm_source: confirmed,
    can_complete_source_review: confirmed,
    can_approve: confirmed,
  };
}

async function mockCurationApi(
  page: Page,
  onSubmit?: (payload: unknown) => void,
  sourceContent = source,
) {
  await page.addInitScript(() => {
    Object.defineProperty(navigator, "sendBeacon", {
      configurable: true,
      value: () => true,
    });
  });
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path === "/api/v1/app/bootstrap") {
      await route.fulfill({
        json: {
          actor: { actor_id: "operator-browser", display_name: "操作者 operator-browser" },
          runways: [
            { id: "pending", label: "待处理", documents: [] },
            { id: "processing", label: "处理中", documents: [] },
            { id: "curatable", label: "可策展", documents: [] },
          ],
        },
      });
      return;
    }
    if (path === "/api/v1/curation/pages") {
      await route.fulfill({
        json: { pages: [{ ...pageSummary, title: sourceContent.titles[0] ?? null }] },
      });
      return;
    }
    if (path === "/api/v1/pages/page-browser" && request.method() === "GET") {
      await route.fulfill({
        json: {
          page_id: "page-browser",
          page_number: 1,
          review_status: "pending",
          source_content: sourceContent,
          curation: curation(null),
        },
      });
      return;
    }
    if (path === "/api/v1/pages/page-browser/curation/text-review") {
      const payload = request.postDataJSON();
      onSubmit?.(payload);
      const snapshot = {
        snapshot_id: "snapshot-browser",
        source_snapshot_id: null,
        source_content: { ...sourceContent, ...payload },
        created_by: "operator-browser",
        created_at: "2026-09-02T08:00:00+00:00",
        source_confirmation: {
          actor_id: "operator-browser",
          confirmed_at: "2026-09-02T08:00:00+00:00",
        },
        source_review: {
          actor_id: "operator-browser",
          completed_at: "2026-09-02T08:00:01+00:00",
        },
        image_source_decisions: [],
      };
      await route.fulfill({
        status: 201,
        json: {
          curation: curation(snapshot),
          transition: {
            snapshot: "created",
            source_saved: true,
            source_confirmed: true,
            source_review_completed: true,
          },
          next_unresolved_image: null,
        },
      });
      return;
    }
    await route.fulfill({ status: 404, json: { error: { message: `未覆盖的请求：${path}` } } });
  });
}

async function mockNavigationGuardApi(page: Page) {
  const pageSource = (pageNumber: number) => ({
    ...source,
    titles: [`第 ${pageNumber} 页持久标题`],
    body: [`第 ${pageNumber} 页持久正文`],
  });
  let pages = [1, 2].map((pageNumber) => ({
    ...pageSummary,
    page_id: `page-${pageNumber}`,
    chunk_id: `chunk-${pageNumber}`,
    page_number: pageNumber,
    title: pageSource(pageNumber).titles[0],
  }));
  let batchRequestCount = 0;

  await page.addInitScript(() => {
    Object.defineProperty(navigator, "sendBeacon", {
      configurable: true,
      value: () => true,
    });
  });
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    if (path === "/api/v1/app/bootstrap") {
      await route.fulfill({
        json: {
          actor: { actor_id: "operator-browser", display_name: "操作者 operator-browser" },
          runways: [
            { id: "pending", label: "待处理", documents: [] },
            { id: "processing", label: "处理中", documents: [] },
            { id: "curatable", label: "可策展", documents: [] },
          ],
        },
      });
      return;
    }
    if (path === "/api/v1/curation/pages") {
      const selectedFilter = url.searchParams.get("review_status");
      await route.fulfill({
        json: {
          pages: selectedFilter === "all"
            ? pages
            : pages.filter((item) => item.review_status === selectedFilter),
        },
      });
      return;
    }
    const detail = path.match(/^\/api\/v1\/pages\/page-(\d+)$/);
    if (detail && request.method() === "GET") {
      const pageNumber = Number(detail[1]);
      await route.fulfill({
        json: {
          page_id: `page-${pageNumber}`,
          page_number: pageNumber,
          review_status: "pending",
          source_content: pageSource(pageNumber),
          curation: curation(null),
        },
      });
      return;
    }
    if (path === "/api/v1/pages/batch-exclude" && request.method() === "POST") {
      batchRequestCount += 1;
      const payload = request.postDataJSON() as { page_ids: string[] };
      pages = pages.map((item) => payload.page_ids.includes(item.page_id)
        ? { ...item, review_status: "excluded" }
        : item);
      await route.fulfill({
        json: {
          requested: payload.page_ids.length,
          excluded: payload.page_ids,
          failed: [],
          complete: true,
        },
      });
      return;
    }
    await route.fulfill({ status: 404, json: { error: { message: `未覆盖的请求：${path}` } } });
  });

  return { batchRequestCount: () => batchRequestCount };
}

async function mockRepeatedFooterNoiseApi(page: Page) {
  const footerSourceRef = "footer-source-browser";
  let state: "idle" | "active" | "revoked" = "idle";
  let candidateRequests = 0;
  let mutationRequests = 0;
  let confirmationPayload: unknown = null;

  const footerCuration = () => {
    const active = state === "active";
    const history = state === "idle" ? [] : [{
      confirmation_id: "confirmation-browser",
      source_ref: footerSourceRef,
      source_text: source.body[1],
      rule_version: "manual-exact-text-v1",
      confirmation_note: "浏览器逐页核对完成。",
      confirmed_by: "operator-browser",
      confirmed_at: "2026-09-02T08:03:00+00:00",
      status: active ? "active" : "revoked",
      revoked_by: state === "revoked" ? "operator-browser" : null,
      revoked_at: state === "revoked" ? "2026-09-02T08:04:00+00:00" : null,
      revoke_note: state === "revoked" ? "从策展工作台撤销并恢复正文。" : null,
    }];
    const excluded = active ? [{
      confirmation_id: "confirmation-browser",
      source_ref: footerSourceRef,
      source_text: source.body[1],
      rule_version: "manual-exact-text-v1",
      confirmed_by: "operator-browser",
      confirmed_at: "2026-09-02T08:03:00+00:00",
    }] : [];
    return {
      ...curation(null),
      repeated_footer_noise: {
        sources: source.body.map((text, index) => ({
          source_ref: index === 1 ? footerSourceRef : `body-source-browser-${index}`,
          source_kind: "body",
          source_index: index,
          text,
          active_confirmation_id: active && index === 1 ? "confirmation-browser" : null,
        })),
        active_count: active ? 1 : 0,
        history,
      },
      chunk_body: {
        nonempty: true,
        preview: active ? source.body.filter((_, index) => index !== 1).join("\n\n") : source.body.join("\n\n"),
      },
      chunk_metadata: { excluded_repeated_footer_noise: excluded },
    };
  };

  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path === "/api/v1/app/bootstrap") {
      await route.fulfill({
        json: {
          actor: { actor_id: "operator-browser", display_name: "操作者 operator-browser" },
          runways: [
            { id: "pending", label: "待处理", documents: [] },
            { id: "processing", label: "处理中", documents: [] },
            { id: "curatable", label: "可策展", documents: [] },
          ],
        },
      });
      return;
    }
    if (path === "/api/v1/curation/pages") {
      await route.fulfill({ json: { pages: [pageSummary] } });
      return;
    }
    if (path === "/api/v1/pages/page-browser" && request.method() === "GET") {
      await route.fulfill({
        json: {
          page_id: "page-browser",
          page_number: 1,
          review_status: "pending",
          source_content: source,
          curation: footerCuration(),
        },
      });
      return;
    }
    if (
      path === `/api/v1/pages/page-browser/repeated-footer-noise/candidates/${footerSourceRef}` &&
      request.method() === "GET"
    ) {
      candidateRequests += 1;
      await route.fulfill({
        json: {
          candidate: {
            candidate_id: "a".repeat(64),
            document_id: "document-browser",
            version_id: "version-browser",
            source_text: source.body[1],
            normalized_text: source.body[1],
            rule_version: "manual-exact-text-v1",
            affected_pages: [1, 2, 3].map((pageNumber) => ({
              page_id: pageNumber === 1 ? "page-browser" : `page-browser-${pageNumber}`,
              page_version_id: `page-version-browser-${pageNumber}`,
              page_number: pageNumber,
              source_ref: pageNumber === 1 ? footerSourceRef : `footer-source-browser-${pageNumber}`,
              source_kind: "body",
              source_index: 1,
              source_text: source.body[1],
              standard_render: { url: `/api/v1/pages/page-browser-${pageNumber}/render` },
            })),
          },
        },
      });
      return;
    }
    if (
      path === "/api/v1/pages/page-browser/repeated-footer-noise/confirmations" &&
      request.method() === "POST"
    ) {
      mutationRequests += 1;
      confirmationPayload = request.postDataJSON();
      state = "active";
      await route.fulfill({ status: 201, json: { confirmation_id: "confirmation-browser" } });
      return;
    }
    if (
      path === "/api/v1/repeated-footer-noise/confirmations/confirmation-browser/revoke" &&
      request.method() === "POST"
    ) {
      mutationRequests += 1;
      state = "revoked";
      await route.fulfill({ json: { confirmation_id: "confirmation-browser", status: "revoked" } });
      return;
    }
    await route.fulfill({ status: 404, json: { error: { message: `未覆盖的请求：${path}` } } });
  });

  return {
    candidateRequests: () => candidateRequests,
    mutationRequests: () => mutationRequests,
    confirmationPayload: () => confirmationPayload,
  };
}

test("长页核对稿支持完整阅读、原位多块草稿与组合提交", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 800 });
  let submitted: unknown = null;
  await mockCurationApi(page, (payload) => { submitted = payload; });
  await page.goto("/curation");

  const manuscript = page.getByRole("region", { name: "标题与正文核对稿" });
  await expect(manuscript).toBeVisible();
  const blocks = manuscript.locator("[data-source-text-block]");
  await expect(blocks).toHaveCount(12);
  await expect(blocks.nth(0).locator(".source-manuscript-text")).toHaveText(source.titles[0]);
  for (const [index, value] of body.entries()) {
    await expect(blocks.nth(index + 1).locator(".source-manuscript-text"))
      .toHaveText(value || "空块");
  }
  await expect(page.getByRole("textbox", { name: /当前编辑值/ })).toHaveCount(0);

  const workspaceColumns = await page.locator(".curation-workspace").evaluate(
    (element) => getComputedStyle(element).gridTemplateColumns.trim().split(/\s+/),
  );
  expect(workspaceColumns).toHaveLength(3);

  const longBlock = blocks.nth(10).locator(".source-manuscript-text");
  const wrapping = await longBlock.evaluate((element) => ({
    clientWidth: element.clientWidth,
    scrollWidth: element.scrollWidth,
    clientHeight: element.clientHeight,
    lineHeight: Number.parseFloat(getComputedStyle(element).lineHeight),
    whiteSpace: getComputedStyle(element).whiteSpace,
  }));
  expect(wrapping.whiteSpace).toBe("pre-wrap");
  expect(wrapping.scrollWidth).toBeLessThanOrEqual(wrapping.clientWidth);
  expect(wrapping.clientHeight).toBeGreaterThan(wrapping.lineHeight * 2);

  const toggle = page.getByRole("button", { name: "折叠文字核对" });
  await toggle.focus();
  await page.keyboard.press("Tab");
  await expect(page.getByRole("button", { name: "编辑标题 1" })).toBeFocused();
  await page.keyboard.press("Enter");
  const titleEditor = page.getByRole("textbox", { name: "标题 1 当前编辑值" });
  await titleEditor.fill("修订后的公开长标题");

  await page.getByRole("button", { name: "编辑正文 02" }).click();
  await expect(page.getByRole("textbox", { name: /当前编辑值/ })).toHaveCount(1);
  await expect(page.getByText("修订后的公开长标题", { exact: true })).toBeVisible();
  await expect(page.getByText("已修改", { exact: true })).toBeVisible();
  await expect(page.getByText("查看标题 1的原始提取", { exact: true })).toBeVisible();

  const secondBodyEditor = page.getByRole("textbox", { name: "正文 02 当前编辑值" });
  await secondBodyEditor.fill("会由 Escape 取消的值");
  await secondBodyEditor.press("Escape");
  await expect(secondBodyEditor).toHaveCount(0);
  await expect(blocks.nth(2)).toContainText(body[1]);

  await page.getByRole("button", { name: "编辑正文 01" }).click();
  const firstBodyEditor = page.getByRole("textbox", { name: "正文 01 当前编辑值" });
  await firstBodyEditor.fill("第一行\n第二行");
  await page.getByRole("button", { name: "保存并确认修改" }).click();
  await expect.poll(() => submitted).toMatchObject({
    titles: ["修订后的公开长标题"],
    body: ["第一行\n第二行", ...body.slice(1)],
  });
});

test("重复页脚检查收纳为正文次级动作并保留确认与撤销证据", async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 1280, height: 900 });
  const api = await mockRepeatedFooterNoiseApi(page);
  await page.goto("/curation");

  const manuscript = page.getByRole("region", { name: "标题与正文核对稿" });
  const footerBlock = manuscript.locator('[data-source-text-block="body-1"]');
  await expect(manuscript.getByRole("button", { name: /正文来源 \d+ 次级动作/ }))
    .toHaveCount(source.body.length);
  await expect(page.getByRole("button", { name: "检查是否为重复页脚噪声" }))
    .toHaveCount(0);
  await expect(page.getByRole("button", { name: /检查正文来源 .* 的跨页重复/ }))
    .toHaveCount(0);

  const secondaryActions = footerBlock.getByRole("button", { name: "正文来源 2 次级动作" });
  await secondaryActions.focus();
  await page.keyboard.press("Enter");
  let checkRepeated = footerBlock.getByRole("button", { name: "检查是否为重复页脚噪声" });
  await expect(checkRepeated).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath("footer-noise-secondary-action-1280.png") });
  await page.keyboard.press("Escape");
  await expect(checkRepeated).toHaveCount(0);
  await expect(secondaryActions).toBeFocused();

  await page.keyboard.press("Enter");
  checkRepeated = footerBlock.getByRole("button", { name: "检查是否为重复页脚噪声" });
  await page.keyboard.press("Tab");
  await expect(checkRepeated).toBeFocused();
  await page.keyboard.press("Enter");
  const dialog = page.getByRole("dialog", { name: "确认排除重复页脚噪声" });
  await expect(dialog).toBeVisible();
  await expect(dialog.getByRole("checkbox", { name: "我已核对全部受影响页" }))
    .toBeFocused();
  await expect.poll(api.candidateRequests).toBe(1);
  expect(api.mutationRequests()).toBe(0);
  await expect(footerBlock.locator(".source-manuscript-text")).toHaveText(source.body[1]);
  await expect(dialog.getByRole("link", { name: "查看第 2 页标准页渲染" }))
    .toHaveAttribute("href", "/api/v1/pages/page-browser-2/render");

  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);
  await expect(secondaryActions).toBeFocused();

  await page.keyboard.press("Enter");
  await footerBlock.getByRole("button", { name: "检查是否为重复页脚噪声" }).click();
  const reopenedDialog = page.getByRole("dialog", { name: "确认排除重复页脚噪声" });
  await reopenedDialog.getByRole("checkbox", { name: "我已核对全部受影响页" }).check();
  await reopenedDialog.getByRole("textbox", { name: "确认说明（可选）" })
    .fill("浏览器逐页核对完成。");
  await reopenedDialog.getByRole("button", { name: "确认排除 3 页中的此来源" }).click();
  await expect.poll(api.mutationRequests).toBe(1);
  expect(api.confirmationPayload()).toMatchObject({
    candidate_id: "a".repeat(64),
    source_ref: "footer-source-browser",
    note: "浏览器逐页核对完成。",
  });

  const activeState = footerBlock.locator(".footer-noise-source-state");
  await expect(activeState).toContainText("已从 Chunk 正文排除");
  await expect(activeState).toContainText("operator-browser");
  await expect(activeState).toContainText("规则 manual-exact-text-v1");
  await expect(footerBlock.getByRole("button", { name: "正文来源 2 次级动作" }))
    .toHaveCount(0);
  const revoke = footerBlock.getByRole("button", { name: "撤销正文来源 2 的重复页脚排除" });
  await expect(revoke).toBeFocused();

  await page.setViewportSize({ width: 640, height: 900 });
  await revoke.scrollIntoViewIfNeeded();
  await expect(revoke).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath("footer-noise-active-200-percent.png") });
  const overflow = await page.evaluate(() => ({
    clientWidth: document.documentElement.clientWidth,
    scrollWidth: document.documentElement.scrollWidth,
  }));
  expect(overflow.scrollWidth).toBeLessThanOrEqual(overflow.clientWidth);
  await revoke.focus();
  await page.keyboard.press("Enter");
  await expect.poll(api.mutationRequests).toBe(2);

  const revokedAudit = footerBlock.locator(".footer-noise-revoked-audit");
  await expect(revokedAudit).toContainText("最近一次排除已撤销");
  await expect(revokedAudit).toContainText("operator-browser");
  await expect(revokedAudit).toContainText("规则 manual-exact-text-v1");
  await expect(revokedAudit).toContainText("从策展工作台撤销并恢复正文。");
  const restoredSecondaryActions = footerBlock.getByRole("button", {
    name: "正文来源 2 次级动作",
  });
  await expect(restoredSecondaryActions).toBeVisible();
  await expect(restoredSecondaryActions).toBeFocused();
  await expect(footerBlock.getByRole("button", { name: "检查是否为重复页脚噪声" }))
    .toHaveCount(0);
});

test("页点击、方向键、筛选与批量排除共用键盘可达的文字导航保护", async ({ page }) => {
  const api = await mockNavigationGuardApi(page);
  await page.goto("/curation");

  await page.getByRole("button", { name: "编辑标题 1" }).click();
  const editor = page.getByRole("textbox", { name: "标题 1 当前编辑值" });
  await editor.fill("只存在于第 1 页的本地草稿");
  const platformLeaveProtected = await page.evaluate(() => {
    const event = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(event);
    return event.defaultPrevented;
  });
  expect(platformLeaveProtected).toBe(true);

  await page.getByRole("button", { name: /第 2 页，第 2 页持久标题，待处理/ }).click();
  let dialog = page.getByRole("dialog", { name: "放弃当前页的文字修改？" });
  await expect(dialog).toContainText("转到第 02 页");
  await expect(dialog.getByRole("button")).toHaveCount(2);
  await expect(dialog.getByRole("button", { name: "留在当前页" })).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(editor).toHaveValue("只存在于第 1 页的本地草稿");
  await expect(editor).toBeFocused();

  await editor.blur();
  const currentRow = page.getByRole("button", { name: /第 1 页，第 1 页持久标题，待处理/ });
  await currentRow.focus();
  await page.keyboard.press("ArrowRight");
  dialog = page.getByRole("dialog", { name: "放弃当前页的文字修改？" });
  await expect(dialog).toContainText("转到第 02 页");
  await page.keyboard.press("Escape");
  await expect(editor).toHaveValue("只存在于第 1 页的本地草稿");

  await page.getByRole("button", { name: "全部" }).click();
  dialog = page.getByRole("dialog", { name: "放弃当前页的文字修改？" });
  await expect(dialog).toContainText("切换到“全部”筛选");
  await page.keyboard.press("Escape");

  await page.getByRole("checkbox", { name: "选择第 1 页，第 1 页持久标题" }).check();
  const batch = page.getByRole("region", { name: "批量排除" });
  await batch.getByRole("combobox", { name: "统一排除原因" }).selectOption("duplicate");
  await batch.getByRole("button", { name: "批量排除 1 页" }).click();
  dialog = page.getByRole("dialog", { name: "放弃当前页的文字修改？" });
  await expect(dialog).toContainText("提交批量排除并离开当前页");
  await expect.poll(api.batchRequestCount).toBe(0);
  await page.keyboard.press("Enter");
  await expect(editor).toHaveValue("只存在于第 1 页的本地草稿");

  await batch.getByRole("button", { name: "批量排除 1 页" }).click();
  await expect(page.getByRole("button", { name: "留在当前页" })).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(page.getByRole("button", { name: "放弃修改并离开" })).toBeFocused();
  await page.keyboard.press("Enter");

  await expect.poll(api.batchRequestCount).toBe(1);
  await expect(page.getByRole("region", { name: "标题与正文核对稿" }))
    .toContainText("第 2 页持久标题");
  await expect(page.getByText("只存在于第 1 页的本地草稿", { exact: true })).toHaveCount(0);

  await page.getByRole("checkbox", { name: "选择第 2 页，第 2 页持久标题" }).check();
  const remainingBatch = page.getByRole("region", { name: "批量排除" });
  await remainingBatch.getByRole("combobox", { name: "统一排除原因" }).selectOption("duplicate");
  await remainingBatch.getByRole("button", { name: "批量排除 1 页" }).click();
  await expect(page.getByText("待处理队列为空")).toBeVisible();
  await page.getByRole("button", { name: "查看全部" }).click();
  await expect(page.getByRole("button", { name: /第 1 页，第 1 页持久标题，已排除/ })).toBeVisible();
  await expect(page.getByRole("button", { name: /第 2 页，第 2 页持久标题，已排除/ })).toBeVisible();
});

test("1280 屏幕的 200% 缩放下转为单列且无页面级横向溢出", async ({ page }) => {
  await page.setViewportSize({ width: 640, height: 900 });
  await mockCurationApi(page);
  await page.goto("/curation");

  const manuscript = page.getByRole("region", { name: "标题与正文核对稿" });
  await expect(manuscript).toBeVisible();
  const firstBlock = manuscript.locator("[data-source-text-block]").first();
  const gridColumns = await firstBlock.evaluate(
    (element) => getComputedStyle(element).gridTemplateColumns.split(" ").length,
  );
  expect(gridColumns).toBe(1);
  const overflow = await page.evaluate(() => ({
    clientWidth: document.documentElement.clientWidth,
    scrollWidth: document.documentElement.scrollWidth,
  }));
  expect(overflow.scrollWidth).toBeLessThanOrEqual(overflow.clientWidth);

  const lastEdit = page.getByRole("button", { name: "编辑正文 11" });
  await lastEdit.focus();
  await page.keyboard.press("Tab");
  await expect(page.getByRole("button", { name: "文字一致，确认" })).toBeFocused();
  await page.getByRole("button", { name: "编辑正文 11" }).click();
  const lastEditor = page.getByRole("textbox", { name: "正文 11 当前编辑值" });
  await expect(lastEditor).toBeFocused();
  await lastEditor.fill("200% 缩放下仍受保护的本地草稿");
  await page.getByRole("button", { name: "全部" }).click();
  const dialog = page.getByRole("dialog", { name: "放弃当前页的文字修改？" });
  await expect(dialog).toBeVisible();
  await expect(dialog.getByRole("button", { name: "留在当前页" })).toBeVisible();
  await expect(dialog.getByRole("button", { name: "放弃修改并离开" })).toBeVisible();
  const bounds = await dialog.boundingBox();
  expect(bounds).not.toBeNull();
  expect(bounds!.x).toBeGreaterThanOrEqual(0);
  expect(bounds!.y).toBeGreaterThanOrEqual(0);
  expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(640);
  expect(bounds!.y + bounds!.height).toBeLessThanOrEqual(900);
});

test("空标题与正文需要显式确认，并在成功后留下零计数摘要", async ({ page }) => {
  const emptySource = {
    titles: [],
    body: [],
    tables: [],
    images: [],
    speaker_notes: [],
  };
  let submitted: unknown = null;
  await mockCurationApi(page, (payload) => { submitted = payload; }, emptySource);
  await page.goto("/curation");

  const emptyState = page.getByRole("status", { name: "标题和正文来源为空" });
  await expect(emptyState).toContainText("未发现标题或正文来源");
  await expect(emptyState).toContainText("对照标准页渲染");
  await expect(page.getByRole("button", { name: "确认无标题/正文来源" })).toBeEnabled();

  await page.getByRole("button", { name: "确认无标题/正文来源" }).click();
  await expect.poll(() => submitted).toMatchObject({ titles: [], body: [] });
  const summary = page.getByRole("status", { name: "文字核对摘要" });
  await expect(summary).toContainText("文字已确认");
  await expect(summary).toContainText("标题0正文0表格0");
  await expect(page.getByRole("button", { name: "来源完整，直接审核" })).toBeFocused();

  await page.getByRole("button", { name: "展开文字核对" }).click();
  await expect(page.getByRole("button", { name: "修改文字" })).toBeVisible();
  await expect(page.getByRole("button", { name: /^(编辑标题|编辑正文)/ })).toHaveCount(0);
  await expect(page.getByRole("textbox", { name: /当前编辑值/ })).toHaveCount(0);
  await expect(page.getByRole("button", { name: /文字一致，确认|完成来源审核/ })).toHaveCount(0);
});
