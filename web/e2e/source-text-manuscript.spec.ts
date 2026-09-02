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

async function mockCurationApi(page: Page, onSubmit?: (payload: unknown) => void) {
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
        source_content: { ...source, ...payload },
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
  await expect(page.getByRole("textbox", { name: "正文 11 当前编辑值" })).toBeFocused();
});
