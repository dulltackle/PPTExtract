import { expect, test, type Locator, type Page } from "playwright/test";

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
  initialSnapshot: null | Record<string, unknown> = null,
  reviewStatus: "pending" | "approved" = "pending",
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
        json: {
          pages: [{
            ...pageSummary,
            review_status: reviewStatus,
            title: sourceContent.titles[0] ?? null,
          }],
        },
      });
      return;
    }
    if (path === "/api/v1/pages/page-browser" && request.method() === "GET") {
      await route.fulfill({
        json: {
          page_id: "page-browser",
          page_number: 1,
          review_status: reviewStatus,
          source_content: sourceContent,
          curation: curation(initialSnapshot),
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

async function mockRepeatedFooterNoiseApi(page: Page, candidateGate?: Promise<void>) {
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
      affected_pages: [1, 2, 3].map((number) => ({
        page_id: `page-${number}`,
        page_version_id: `pv-${number}`,
        page_number: number,
        review_status: "pending",
      })),
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
      await candidateGate;
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

async function selectBodyText(
  page: Page,
  startIndex: number,
  startOffset: number,
  endIndex: number,
  endOffset: number,
) {
  await page.evaluate(({ startIndex, startOffset, endIndex, endOffset }) => {
    const start = document.querySelector<HTMLElement>(
      `[data-body-editable-index="${startIndex}"]`,
    );
    const end = document.querySelector<HTMLElement>(
      `[data-body-editable-index="${endIndex}"]`,
    );
    if (!start || !end) throw new Error("正文连续编辑面尚未呈现来源段落");
    const pointAt = (root: HTMLElement, logicalOffset: number) => {
      const lines = Array.from(root.querySelectorAll<HTMLElement>(":scope > [data-body-line]"));
      let remaining = logicalOffset;
      let target = lines[lines.length - 1] ?? root;
      for (const [index, line] of lines.entries()) {
        const length = line.textContent?.length ?? 0;
        if (remaining <= length) {
          target = line;
          break;
        }
        remaining -= length;
        if (index < lines.length - 1) remaining -= 1;
      }
      const text = Array.from(target.childNodes).find((node) => node.nodeType === Node.TEXT_NODE);
      return text
        ? { node: text, offset: Math.min(remaining, text.textContent?.length ?? 0) }
        : { node: target, offset: 0 };
    };
    const startPoint = pointAt(start, startOffset);
    const endPoint = pointAt(end, endOffset);
    start.focus();
    const range = document.createRange();
    range.setStart(startPoint.node, startPoint.offset);
    range.setEnd(endPoint.node, endPoint.offset);
    const selection = window.getSelection();
    selection?.removeAllRanges();
    selection?.addRange(range);
  }, { startIndex, startOffset, endIndex, endOffset });
}

async function pasteIntoFocusedBody(page: Page, text: string) {
  await page.evaluate((value) => {
    const clipboardData = new DataTransfer();
    clipboardData.setData("text/plain", value);
    document.activeElement?.dispatchEvent(new ClipboardEvent("paste", {
      bubbles: true,
      cancelable: true,
      clipboardData,
    }));
  }, text);
}

async function expectBodyText(editor: Locator, expected: string) {
  await expect.poll(() => editor.evaluate((element) => (element as HTMLElement).innerText))
    .toBe(expected);
}

test("正文整稿预览从来源日志侧展开、保留草稿并恢复触发焦点", async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  let mutationRequests = 0;
  page.on("request", (request) => {
    if (request.method() !== "GET") mutationRequests += 1;
  });
  await mockCurationApi(page);
  await page.goto("/curation");

  const preview = page.getByRole("region", { name: "正文整稿预览" });
  await expect(preview).toBeVisible();
  await expect(preview).toContainText("正文 01");
  await expect(page.getByText("还有正文内容，请在放大视图中继续查看。")).toBeVisible();
  await expect(page.getByRole("button", { name: /^编辑正文/ })).toHaveCount(0);
  await page.screenshot({ path: testInfo.outputPath("body-preview-wide.png") });

  const thirdParagraph = preview.getByRole("button", {
    name: "从正文 03 打开放大视图",
  });
  await thirdParagraph.click();

  const expanded = page.getByRole("dialog", { name: "正文放大视图" });
  await expect(expanded).toBeVisible();
  const longestEditor = expanded.getByRole("textbox", { name: "正文 11 当前编辑值" });
  expect(await longestEditor.evaluate((element) => element.scrollHeight))
    .toBeLessThanOrEqual(await longestEditor.evaluate((element) => element.clientHeight));
  const thirdEditor = expanded.getByRole("textbox", { name: "正文 03 当前编辑值" });
  await expect(thirdEditor).toBeFocused();
  await thirdEditor.fill("只保存在本地的正文草稿");
  await expect(page.getByRole("heading", { name: "标准页渲染" })).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath("body-expanded-wide.png") });

  await page.keyboard.press("Escape");
  await expect(expanded).toHaveCount(0);
  await expect(thirdParagraph).toBeFocused();
  await expect(preview).toContainText("只保存在本地的正文草稿");
  expect(mutationRequests).toBe(0);

  await page.getByRole("button", { name: "放大编辑正文" }).click();
  await expectBodyText(
    page.getByRole("textbox", { name: "正文 03 当前编辑值" }),
    "只保存在本地的正文草稿",
  );
});

test("来源段号以键盘打开受约束审计面板并保留未保存整稿草稿", async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 1280, height: 900 });
  let mutationRequests = 0;
  page.on("request", (request) => {
    if (request.method() !== "GET") mutationRequests += 1;
  });
  await mockCurationApi(page);
  await page.goto("/curation");

  const preview = page.getByRole("region", { name: "正文整稿预览" });
  const previewNumbers = preview.getByRole("button", { name: /正文 \d+，.*，打开来源审计/ });
  await expect(previewNumbers).toHaveCount(11);
  const thirdPreviewNumber = preview.getByRole("button", {
    name: "正文 03，未修改，打开来源审计",
  });
  await thirdPreviewNumber.focus();
  await page.keyboard.press("Enter");

  let audit = page.getByRole("dialog", { name: "正文 03 · 来源审计" });
  await expect(audit.getByRole("button", { name: "关闭正文 03 来源审计" })).toBeFocused();
  await expect(audit.getByRole("status", { name: "正文 03 修改状态" }))
    .toContainText("当前值与 AnyDoc 原文一致");
  await expect(audit.getByRole("region", { name: "正文 03 当前值" }))
    .toContainText(source.body[2]);
  await expect(audit.getByRole("region", { name: "正文 03 AnyDoc 原文" }))
    .toContainText(source.body[2]);
  await expect(page.getByRole("heading", { name: "标准页渲染" })).toBeVisible();
  await expect(page.locator(".source-review-log")).toHaveAttribute("inert", "");
  const currentPage = page.getByRole("button", {
    name: /第 1 页，公开长标题：用于核对整页来源文字的稳定身份与阅读顺序，待处理/,
  });
  await expect(currentPage).toBeDisabled();
  const comparisonColumns = await audit.locator(".source-body-audit-compare").evaluate(
    (element) => getComputedStyle(element).gridTemplateColumns.trim().split(/\s+/),
  );
  expect(comparisonColumns).toHaveLength(1);
  await page.screenshot({ path: testInfo.outputPath("source-audit-panel-1280.png") });

  const returnButton = audit.getByRole("button", { name: "返回正文" });
  await page.keyboard.press("Shift+Tab");
  await expect(returnButton).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(audit.getByRole("button", { name: "关闭正文 03 来源审计" })).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(audit).toHaveCount(0);
  await expect(thirdPreviewNumber).toBeFocused();
  await expect(currentPage).toBeEnabled();
  expect(mutationRequests).toBe(0);

  await preview.getByRole("button", { name: "从正文 03 打开放大视图" }).click();
  const expanded = page.getByRole("dialog", { name: "正文放大视图" });
  const editor = expanded.getByRole("textbox", { name: "正文 03 当前编辑值" });
  await editor.fill("只存在于内存中的审计草稿");
  const expandedNumber = expanded.getByRole("button", {
    name: "正文 03，已修改，打开来源审计",
  });
  await expandedNumber.press("Space");
  audit = page.getByRole("dialog", { name: "正文 03 · 来源审计" });
  const expandedComparisonColumns = await audit.locator(".source-body-audit-compare").evaluate(
    (element) => getComputedStyle(element).gridTemplateColumns.trim().split(/\s+/),
  );
  expect(expandedComparisonColumns).toHaveLength(2);
  await expect(audit.getByRole("status", { name: "正文 03 修改状态" }))
    .toContainText("当前值有未保存修改");
  await expect(audit.getByRole("region", { name: "正文 03 当前值" }))
    .toContainText("只存在于内存中的审计草稿");
  await expect(audit.getByRole("region", { name: "正文 03 AnyDoc 原文" }))
    .toContainText(source.body[2]);
  await page.keyboard.press("Escape");
  await expect(expandedNumber).toBeFocused();
  await expectBodyText(editor, "只存在于内存中的审计草稿");
  expect(mutationRequests).toBe(0);

  await editor.fill("");
  const emptyNumber = expanded.getByRole("button", {
    name: "正文 03，当前值为空，已修改，打开来源审计",
  });
  await emptyNumber.click();
  audit = page.getByRole("dialog", { name: "正文 03 · 来源审计" });
  await expect(audit.getByRole("status", { name: "正文 03 修改状态" }))
    .toContainText("当前值为空");
  await page.keyboard.press("Escape");
  await expect(emptyNumber).toBeFocused();
  expect(mutationRequests).toBe(0);
});

test("单段短正文保留固定预览与键盘放大入口", async ({ page }) => {
  const shortSource = {
    ...source,
    body: ["单段短正文。"],
  };
  await mockCurationApi(page, undefined, shortSource);
  await page.goto("/curation");

  const preview = page.getByRole("region", { name: "正文整稿预览" });
  await expect(preview).toContainText("单段短正文。");
  expect(await preview.evaluate((element) => element.clientHeight)).toBe(224);
  await expect(page.getByText("正文已完整显示。")).toBeVisible();
  await expect(page.getByText(/还有正文内容/)).toHaveCount(0);

  const expand = page.getByRole("button", { name: "放大编辑正文" });
  await expand.focus();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("textbox", { name: "正文 01 当前编辑值" })).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(expand).toBeFocused();
});

test("已批准正文保持只读且只能通过既有重新打开流程修改", async ({ page }) => {
  const confirmedSource = {
    ...source,
    body: ["已经确认并冻结的公开正文。"],
  };
  const snapshot = {
    snapshot_id: "snapshot-confirmed-browser",
    source_snapshot_id: null,
    source_content: confirmedSource,
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
  await mockCurationApi(page, undefined, confirmedSource, snapshot, "approved");
  await page.goto("/curation");

  await expect(page.getByRole("button", { name: "重新打开此页" })).toBeVisible();
  await page.getByRole("button", { name: "展开文字核对" }).click();
  const expand = page.getByRole("button", { name: "放大查看正文" });
  await expand.click();
  const expanded = page.getByRole("dialog", { name: "正文放大视图" });
  await expect(expanded.getByRole("button", { name: /关闭正文放大视图/ })).toBeFocused();
  const readonlyBody = expanded.getByRole("textbox", { name: "正文 01 当前只读值" });
  await expect(readonlyBody).toHaveAttribute("aria-readonly", "true");
  await expect(readonlyBody).toHaveAttribute("contenteditable", "false");
  await expectBodyText(readonlyBody, "已经确认并冻结的公开正文。");
  await selectBodyText(page, 0, 12, 0, 12);
  await page.keyboard.press("Enter");
  await selectBodyText(page, 0, 0, 0, 2);
  await pasteIntoFocusedBody(page, "不得写入");
  await selectBodyText(page, 0, 0, 0, 2);
  await page.keyboard.press("Control+x");
  await expectBodyText(readonlyBody, "已经确认并冻结的公开正文。");
  await expect(expanded.getByRole("button", { name: /保存|确认修改/ })).toHaveCount(0);
});

test("长页核对稿支持完整阅读、原位多块草稿与组合提交", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 800 });
  let submitted: unknown = null;
  await mockCurationApi(page, (payload) => { submitted = payload; });
  await page.goto("/curation");

  const manuscript = page.getByRole("region", { name: "标题与正文核对稿" });
  await expect(manuscript).toBeVisible();
  const bodyGroup = manuscript.getByRole("region", { name: "正文", exact: true });
  await expect(bodyGroup).toContainText("11 段 · 保留原始段落边界");
  const blocks = manuscript.locator("[data-source-text-block]");
  await expect(blocks).toHaveCount(12);
  await expect(bodyGroup.locator('[data-source-text-block^="body-"]')).toHaveCount(11);
  await expect(blocks.nth(0).locator(".source-manuscript-text")).toHaveText(source.titles[0]);
  for (const [index, value] of body.entries()) {
    await expect(blocks.nth(index + 1).locator(".source-body-preview-text"))
      .toHaveText(value || "空块");
  }
  await expect(page.getByRole("textbox", { name: /当前编辑值/ })).toHaveCount(0);

  const workspaceColumns = await page.locator(".curation-workspace").evaluate(
    (element) => getComputedStyle(element).gridTemplateColumns.trim().split(/\s+/),
  );
  expect(workspaceColumns).toHaveLength(3);

  const longBlock = blocks.nth(10).locator(".source-body-preview-text");
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

  await page.getByRole("button", { name: "从正文 02 打开放大视图" }).click();
  await expect(page.getByRole("dialog", { name: "正文放大视图" })).toBeVisible();

  const secondBodyEditor = page.getByRole("textbox", { name: "正文 02 当前编辑值" });
  await selectBodyText(page, 1, 0, 1, 0);
  await secondBodyEditor.press("Backspace");
  await expectBodyText(page.getByRole("textbox", { name: "正文 01 当前编辑值" }), body[0]);
  await expect(page.getByRole("textbox", { name: /正文 \d+ 当前编辑值/ })).toHaveCount(11);
  await secondBodyEditor.fill("会由 Escape 保留的值");
  await secondBodyEditor.press("Escape");
  await expect(secondBodyEditor).toHaveCount(0);
  await expect(blocks.nth(2)).toContainText("会由 Escape 保留的值");
  await expect(page.getByText("修订后的公开长标题", { exact: true })).toBeVisible();
  await expect(manuscript.getByRole("group", { name: "标题" }).getByText("已修改"))
    .toBeVisible();
  await expect(page.getByText("查看标题 1的原始提取", { exact: true })).toBeVisible();

  await page.getByRole("button", { name: "从正文 01 打开放大视图" }).click();
  const firstBodyEditor = page.getByRole("textbox", { name: "正文 01 当前编辑值" });
  await firstBodyEditor.fill("第一行\n第二行");
  await page.getByRole("button", { name: "返回来源日志" }).click();
  await page.getByRole("button", { name: "保存并确认修改" }).click();
  await expect.poll(() => submitted).toMatchObject({
    titles: ["修订后的公开长标题"],
    body: ["第一行\n第二行", "会由 Escape 保留的值", ...body.slice(2)],
  });
});

test("连续正文编辑面允许段内换行与粘贴并拒绝跨来源段落修改", async ({ page }) => {
  const boundarySource = {
    ...source,
    titles: ["来源边界验收标题"],
    body: ["第一来源段", "第二来源段", "第三\u200B来源段"],
  };
  let submitted: { titles: string[]; body: string[] } | null = null;
  await mockCurationApi(
    page,
    (payload) => { submitted = payload as { titles: string[]; body: string[] }; },
    boundarySource,
  );
  await page.goto("/curation");

  await page.getByRole("button", { name: "从正文 01 打开放大视图" }).click();
  const expanded = page.getByRole("dialog", { name: "正文放大视图" });
  const editingSurface = expanded.getByRole("region", { name: "连续正文编辑面" });
  await expect(editingSurface).toBeVisible();
  await expect(editingSurface.locator(".source-body-paragraph")).toHaveCount(3);

  const first = expanded.getByRole("textbox", { name: "正文 01 当前编辑值" });
  const second = expanded.getByRole("textbox", { name: "正文 02 当前编辑值" });
  await expect(first).toBeFocused();
  await selectBodyText(page, 0, 5, 0, 5);
  await page.keyboard.press("Enter");
  await page.keyboard.type("追加行");
  await selectBodyText(page, 0, 9, 0, 9);
  await pasteIntoFocusedBody(page, "\n粘贴甲\n粘贴乙");
  const editedFirst = "第一来源段\n追加行\n粘贴甲\n粘贴乙";
  await expectBodyText(first, editedFirst);

  await selectBodyText(page, 0, editedFirst.length, 0, editedFirst.length);
  const imeEnterPrevented = await first.evaluate((element) => {
    const event = new KeyboardEvent("keydown", {
      bubbles: true,
      cancelable: true,
      code: "Enter",
      isComposing: true,
      key: "Enter",
      keyCode: 229,
    });
    element.dispatchEvent(event);
    return event.defaultPrevented;
  });
  expect(imeEnterPrevented).toBe(false);
  await expectBodyText(first, editedFirst);

  const imeEscapePrevented = await first.evaluate((element) => {
    const event = new KeyboardEvent("keydown", {
      bubbles: true,
      cancelable: true,
      code: "Escape",
      isComposing: true,
      key: "Escape",
      keyCode: 229,
    });
    element.dispatchEvent(event);
    return event.defaultPrevented;
  });
  expect(imeEscapePrevented).toBe(false);
  await expect(expanded).toBeVisible();

  await selectBodyText(page, 0, editedFirst.length, 0, editedFirst.length);
  await page.keyboard.press("Delete");
  await expectBodyText(second, "第二来源段");
  await expect(expanded.getByRole("status"))
    .toContainText("已到达来源段落边界。请在当前来源文字块内编辑。");

  await selectBodyText(page, 1, 0, 1, 0);
  await page.keyboard.press("Backspace");
  await expectBodyText(first, editedFirst);
  await expectBodyText(second, "第二来源段");

  await selectBodyText(page, 0, 2, 1, 2);
  const copied = await page.evaluate(() => {
    const clipboardData = new DataTransfer();
    document.activeElement?.dispatchEvent(new ClipboardEvent("copy", {
      bubbles: true,
      cancelable: true,
      clipboardData,
    }));
    return clipboardData.getData("text/plain");
  });
  expect(copied).toBe(`${editedFirst.slice(2)}\n\n${boundarySource.body[1].slice(0, 2)}`);

  for (const action of [
    "type",
    "paste",
    "cut",
    "composition",
    "Backspace",
    "Delete",
  ] as const) {
    await selectBodyText(page, 0, 2, 1, 2);
    const selectionBefore = await page.evaluate(() => window.getSelection()?.toString());
    if (action === "type") await page.keyboard.type("跨段改写");
    else if (action === "paste") await pasteIntoFocusedBody(page, "跨段粘贴\n仍应拒绝");
    else if (action === "cut") await page.keyboard.press("Control+x");
    else if (action === "composition") {
      const compositionPrevented = await first.evaluate((element) => {
        const event = new InputEvent("beforeinput", {
          bubbles: true,
          cancelable: true,
          data: "候",
          inputType: "insertCompositionText",
          isComposing: true,
        });
        element.dispatchEvent(event);
        return event.defaultPrevented;
      });
      expect(compositionPrevented).toBe(true);
    }
    else await page.keyboard.press(action);
    await expectBodyText(first, editedFirst);
    await expectBodyText(second, "第二来源段");
    expect(await page.evaluate(() => window.getSelection()?.toString())).toBe(selectionBefore);
    await expect(first).toBeFocused();
  }

  await second.focus();
  await second.blur();
  await expectBodyText(second, "第二来源段");
  await page.keyboard.press("Escape");
  await expect(expanded).toHaveCount(0);
  await page.getByRole("button", { name: "放大编辑正文" }).click();
  await expectBodyText(first, editedFirst);
  await expectBodyText(second, "第二来源段");

  const third = expanded.getByRole("textbox", { name: "正文 03 当前编辑值" });
  await selectBodyText(page, 2, boundarySource.body[2].length, 2, boundarySource.body[2].length);
  await page.keyboard.type("尾");
  await expectBodyText(third, "第三\u200B来源段尾");

  await expanded.getByRole("button", { name: "保存并确认修改" }).click();
  await expect.poll(() => submitted?.body)
    .toEqual([editedFirst, "第二来源段", "第三\u200B来源段尾"]);
  expect(submitted?.titles).toEqual(boundarySource.titles);
});

test("重复页脚检查迁入来源审计面板并保留确认与撤销证据", async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 1280, height: 900 });
  const api = await mockRepeatedFooterNoiseApi(page);
  await page.goto("/curation");

  const manuscript = page.getByRole("region", { name: "标题与正文核对稿" });
  const footerBlock = manuscript.locator('[data-source-text-block="body-1"]');
  await expect(manuscript.getByRole("button", { name: /次级动作/ }))
    .toHaveCount(0);
  await expect(page.getByRole("button", { name: "检查是否为重复页脚噪声" }))
    .toHaveCount(0);
  await expect(page.getByRole("button", { name: /检查正文来源 .* 的跨页重复/ }))
    .toHaveCount(0);

  const auditTrigger = footerBlock.getByRole("button", {
    name: "正文 02，未修改，打开来源审计",
  });
  await auditTrigger.click();
  let audit = page.getByRole("dialog", { name: "正文 02 · 来源审计" });
  let checkRepeated = audit.getByRole("button", { name: "检查是否为重复页脚噪声" });
  await expect(checkRepeated).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath("footer-noise-audit-action-1280.png") });
  await page.keyboard.press("Escape");
  await expect(audit).toHaveCount(0);
  await expect(auditTrigger).toBeFocused();

  await page.keyboard.press("Enter");
  audit = page.getByRole("dialog", { name: "正文 02 · 来源审计" });
  checkRepeated = audit.getByRole("button", { name: "检查是否为重复页脚噪声" });
  await checkRepeated.click();
  const dialog = page.getByRole("dialog", { name: "确认排除重复页脚噪声" });
  await expect(dialog).toBeVisible();
  await expect(dialog.getByRole("checkbox", { name: "我已核对全部受影响页" }))
    .toBeFocused();
  await expect.poll(api.candidateRequests).toBe(1);
  expect(api.mutationRequests()).toBe(0);
  await expect(footerBlock.locator(".source-body-preview-text")).toHaveText(source.body[1]);
  await expect(dialog.getByRole("link", { name: "查看第 2 页标准页渲染" }))
    .toHaveAttribute("href", "/api/v1/pages/page-browser-2/render");

  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);
  await expect(checkRepeated).toBeFocused();
  await expect(audit).toBeVisible();

  await checkRepeated.click();
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

  const activeState = audit.locator(".footer-noise-source-state");
  await expect(activeState).toContainText("已从 Chunk 正文排除");
  await expect(activeState).toContainText("operator-browser");
  await expect(activeState).toContainText("规则 manual-exact-text-v1");
  await expect(audit.getByRole("button", { name: /次级动作/ })).toHaveCount(0);
  const revoke = audit.getByRole("button", { name: "撤销正文来源 2 的重复页脚排除" });
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

  const revokedAudit = audit.locator(".footer-noise-revoked-audit");
  await expect(revokedAudit).toContainText("最近一次排除已撤销");
  await expect(revokedAudit).toContainText("operator-browser");
  await expect(revokedAudit).toContainText("规则 manual-exact-text-v1");
  await expect(revokedAudit).toContainText("从策展工作台撤销并恢复正文。");
  const restoredCheck = audit.getByRole("button", {
    name: "再次检查是否为重复页脚噪声",
  });
  await expect(restoredCheck).toBeVisible();
  await expect(restoredCheck).toBeFocused();
  await expect(audit.getByRole("button", { name: /次级动作/ })).toHaveCount(0);
});

test("关闭来源审计会取消尚未返回的重复页脚候选请求", async ({ page }) => {
  let releaseCandidate!: () => void;
  const candidateGate = new Promise<void>((resolve) => {
    releaseCandidate = resolve;
  });
  const api = await mockRepeatedFooterNoiseApi(page, candidateGate);
  await page.goto("/curation");

  const manuscript = page.getByRole("region", { name: "标题与正文核对稿" });
  const auditTrigger = manuscript.getByRole("button", {
    name: "正文 02，未修改，打开来源审计",
  });
  await auditTrigger.click();
  const audit = page.getByRole("dialog", { name: "正文 02 · 来源审计" });
  const candidateSettled = Promise.race([
    page.waitForEvent("requestfinished", {
      predicate: (request) => request.url().includes("repeated-footer-noise/candidates"),
    }),
    page.waitForEvent("requestfailed", {
      predicate: (request) => request.url().includes("repeated-footer-noise/candidates"),
    }),
  ]);
  await audit.getByRole("button", { name: "检查是否为重复页脚噪声" }).click();
  await expect.poll(api.candidateRequests).toBe(1);

  await page.keyboard.press("Escape");
  await expect(audit).toHaveCount(0);
  await expect(auditTrigger).toBeFocused();
  releaseCandidate();
  await candidateSettled;
  await page.waitForTimeout(50);

  await expect(page.getByRole("dialog", { name: "确认排除重复页脚噪声" })).toHaveCount(0);
  await expect(page.getByText("已关闭来源审计；重复页脚检查已取消，整稿草稿保持不变。"))
    .toBeVisible();
  expect(api.mutationRequests()).toBe(0);
});

test("正文放大视图让顶层重复页脚对话框优先处理 Escape", async ({ page }) => {
  await mockRepeatedFooterNoiseApi(page);
  await page.goto("/curation");

  await page.getByRole("button", { name: "从正文 02 打开放大视图" }).click();
  const expanded = page.getByRole("dialog", { name: "正文放大视图" });
  const footerBlock = expanded.locator('[data-source-text-block="body-1"]');
  await footerBlock.getByRole("button", {
    name: "正文 02，未修改，打开来源审计",
  }).click();
  const audit = page.getByRole("dialog", { name: "正文 02 · 来源审计" });
  await audit.getByRole("button", { name: "检查是否为重复页脚噪声" }).click();
  const dialog = page.getByRole("dialog", { name: "确认排除重复页脚噪声" });
  await expect(dialog).toBeVisible();

  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);
  await expect(audit).toBeVisible();
  await expect(expanded).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(audit).toHaveCount(0);
  await expect(expanded).toBeVisible();
});

test("正文放大视图让顶层导航保护对话框优先处理 Escape", async ({ page }) => {
  await mockNavigationGuardApi(page);
  await page.goto("/curation");

  await page.getByRole("button", { name: "从正文 01 打开放大视图" }).click();
  const expanded = page.getByRole("dialog", { name: "正文放大视图" });
  const editor = expanded.getByRole("textbox", { name: "正文 01 当前编辑值" });
  await editor.fill("只存在于正文放大视图的本地草稿");
  await page.getByRole("button", { name: /第 2 页，第 2 页持久标题，待处理/ }).click();
  const dialog = page.getByRole("dialog", { name: "放弃当前页的文字修改？" });
  await expect(dialog).toBeVisible();

  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);
  await expect(expanded).toBeVisible();
  await expectBodyText(editor, "只存在于正文放大视图的本地草稿");
});

test("现有还原流程丢弃正文放大视图草稿并恢复持久正文", async ({ page }) => {
  await mockNavigationGuardApi(page);
  await page.goto("/curation");

  await page.getByRole("button", { name: "从正文 01 打开放大视图" }).click();
  const editor = page.getByRole("textbox", { name: "正文 01 当前编辑值" });
  await expect(editor).toBeFocused();
  await editor.fill("应由现有还原流程丢弃的正文草稿");
  await page.getByRole("button", { name: /第 2 页，第 2 页持久标题，待处理/ }).click();
  const dialog = page.getByRole("dialog", { name: "放弃当前页的文字修改？" });
  await dialog.getByRole("button", { name: "放弃修改并离开" }).click();
  await expect(page.getByRole("region", { name: "标题与正文核对稿" }))
    .toContainText("第 2 页持久正文");

  await page.getByRole("button", { name: /第 1 页，第 1 页持久标题，待处理/ }).click();
  await page.getByRole("button", { name: "从正文 01 打开放大视图" }).click();
  await expectBodyText(
    page.getByRole("textbox", { name: "正文 01 当前编辑值" }),
    "第 1 页持久正文",
  );
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

test("1280 屏幕的 200% 缩放下转为单列且无页面级横向溢出", async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 640, height: 900 });
  await mockCurationApi(page);
  await page.goto("/curation");

  const manuscript = page.getByRole("region", { name: "标题与正文核对稿" });
  await expect(manuscript).toBeVisible();
  const lastTrigger = page.getByRole("button", { name: "从正文 11 打开放大视图" });
  await lastTrigger.click();
  const expanded = page.getByRole("dialog", { name: "正文放大视图" });
  await expect(expanded).toBeVisible();
  await expect(page.locator(".evidence-panel")).toBeHidden();
  const overflow = await page.evaluate(() => ({
    clientWidth: document.documentElement.clientWidth,
    scrollWidth: document.documentElement.scrollWidth,
  }));
  expect(overflow.scrollWidth).toBeLessThanOrEqual(overflow.clientWidth);
  await page.screenshot({ path: testInfo.outputPath("body-expanded-narrow.png") });

  const lastEditor = page.getByRole("textbox", { name: "正文 11 当前编辑值" });
  await expect(lastEditor).toBeFocused();
  expect(await lastEditor.evaluate((element) => element.scrollHeight))
    .toBeLessThanOrEqual(await lastEditor.evaluate((element) => element.clientHeight));
  await lastEditor.fill("200% 缩放下仍受保护的本地草稿");
  const saveButton = page.getByRole("button", { name: "保存并确认修改" });
  await saveButton.focus();
  await page.keyboard.press("Tab");
  await expect(page.getByRole("button", { name: /关闭正文放大视图/ })).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(lastTrigger).toBeFocused();
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
