import { mkdir } from "node:fs/promises";
import { resolve } from "node:path";
import { chromium } from "playwright";

const baseUrl = process.argv[2];
const routes = process.argv.slice(3);
if (!baseUrl || routes.length !== 2) {
  throw new Error("缺少 base URL 或两条策展路由");
}
const captureRoot = process.env.PPTEXTRACT_CAPTURE_DIR
  ? resolve(process.env.PPTEXTRACT_CAPTURE_DIR)
  : null;
if (captureRoot) await mkdir(captureRoot, { recursive: true });

const browser = await chromium.launch({
  ...(process.env.PPTEXTRACT_CHROME
    ? { executablePath: process.env.PPTEXTRACT_CHROME }
    : {}),
  headless: true,
  args: ["--no-sandbox", "--disable-dev-shm-usage"],
});

const checks = [];
const savedBounds = [];
const mutationSnapshots = [];

async function prepareReviewedPage(page) {
  await page.getByRole("heading", { name: "来源日志" }).waitFor();
  await page.getByRole("button", { name: "文字一致，确认" }).click();
  await page.getByRole("button", { name: "有缺口，在页面上框选" }).waitFor();
  if ((await page.locator(".capture-range").count()) !== 0) {
    throw new Error("来源审核完成后自动显示了候选框");
  }
}

async function exercise(route, viewport) {
  let stage = "打开工作位";
  try {
  const page = await browser.newPage({ viewport });
  page.setDefaultTimeout(30_000);
  await page.goto(`${baseUrl}${route}`, { waitUntil: "domcontentloaded", timeout: 10_000 });
  await prepareReviewedPage(page);
  await page.getByRole("button", { name: "有缺口，在页面上框选" }).click();

  const render = page.getByRole("img", { name: /标准页渲染结果/ });
  const imageBox = await render.boundingBox();
  if (!imageBox) throw new Error(`${viewport.width}px 标准页渲染结果没有可框选尺寸`);
  const start = {
    x: imageBox.x + imageBox.width * 0.18,
    y: imageBox.y + imageBox.height * 0.22,
  };
  const end = {
    x: imageBox.x + imageBox.width * 0.62,
    y: imageBox.y + imageBox.height * 0.66,
  };
  await page.mouse.move(start.x, start.y);
  await page.mouse.down();
  await page.mouse.move(end.x, end.y, { steps: 5 });
  await page.mouse.up();

  const editor = page.getByRole("dialog", { name: "视觉对象 01" });
  await editor.waitFor();
  await page.waitForFunction(() => (
    document.querySelector(".capture-editor")?.getAttribute("data-positioned") === "true"
  ));
  const summary = page.getByRole("textbox", { name: "视觉对象 01 summary" });
  await page.waitForFunction(() => (
    document.activeElement?.getAttribute("aria-label") === "视觉对象 01 summary"
  ));
  const editorBox = await editor.boundingBox();
  const rangeBox = await page.locator(".capture-range").boundingBox();
  if (!editorBox || !rangeBox) throw new Error(`${viewport.width}px 缺少浮窗或框选范围`);
  const overlaps = !(
    editorBox.x + editorBox.width <= rangeBox.x ||
    rangeBox.x + rangeBox.width <= editorBox.x ||
    editorBox.y + editorBox.height <= rangeBox.y ||
    rangeBox.y + rangeBox.height <= editorBox.y
  );
  if (overlaps) {
    throw new Error(`${viewport.width}px 浮窗遮挡当前框选范围：editor=${JSON.stringify(editorBox)} range=${JSON.stringify(rangeBox)}`);
  }
  if (
    editorBox.x < 0 || editorBox.y < 0 ||
    editorBox.x + editorBox.width > viewport.width ||
    editorBox.y + editorBox.height > viewport.height
  ) {
    throw new Error(`${viewport.width}px 浮窗超出可视区域`);
  }
  const placement = await editor.getAttribute("data-placement");
  if (placement !== "right") {
    throw new Error(`${viewport.width}px 中央范围应优先把浮窗放在右侧，实际为 ${placement ?? "未标记"}`);
  }
  const evidenceStage = page.locator(".evidence-stage");
  await evidenceStage.evaluate((element) => {
    element.style.alignItems = "start";
    element.style.paddingBlock = "180px";
    element.dispatchEvent(new Event("scroll"));
  });
  await page.waitForTimeout(60);
  const rangeBeforeScroll = await page.locator(".capture-range").boundingBox();
  const editorBeforeScroll = await editor.boundingBox();
  await evidenceStage.evaluate((element) => {
    element.scrollTop = 80;
  });
  await page.waitForTimeout(60);
  const rangeAfterScroll = await page.locator(".capture-range").boundingBox();
  const editorAfterScroll = await editor.boundingBox();
  if (!rangeBeforeScroll || !editorBeforeScroll || !rangeAfterScroll || !editorAfterScroll) {
    throw new Error(`${viewport.width}px 内部滚动后缺少浮窗或范围尺寸`);
  }
  const rangeDelta = rangeAfterScroll.y - rangeBeforeScroll.y;
  const editorDelta = editorAfterScroll.y - editorBeforeScroll.y;
  if (Math.abs(rangeDelta - editorDelta) > 2) {
    throw new Error(`${viewport.width}px 内部滚动后浮窗未保持范围关联：range=${rangeDelta} editor=${editorDelta}`);
  }
  if ((await page.locator(".inspector-panel").getAttribute("inert")) === null) {
    throw new Error(`${viewport.width}px 浮窗打开时右栏未暂停全局动作`);
  }
  if (!(await page.locator(".page-row").first().isDisabled())) {
    throw new Error(`${viewport.width}px 浮窗打开时页面切换仍可用`);
  }

  await page.getByRole("button", { name: "保存并返回审核" }).click();
  await page.getByText("summary 不能为空，请写成可独立理解的结论。").waitFor();
  await page.waitForFunction(() => (
    document.activeElement?.getAttribute("aria-label") === "视觉对象 01 summary"
  ));
  if (!(await summary.evaluate((element) => element === document.activeElement))) {
    throw new Error(`${viewport.width}px 空 summary 后焦点未返回字段`);
  }
  const errorEditorBox = await editor.boundingBox();
  const errorRangeBox = await page.locator(".capture-range").boundingBox();
  if (!errorEditorBox || !errorRangeBox) {
    throw new Error(`${viewport.width}px 校验错误展开后缺少浮窗或范围尺寸`);
  }
  const errorOverlaps = !(
    errorEditorBox.x + errorEditorBox.width <= errorRangeBox.x ||
    errorRangeBox.x + errorRangeBox.width <= errorEditorBox.x ||
    errorEditorBox.y + errorEditorBox.height <= errorRangeBox.y ||
    errorRangeBox.y + errorRangeBox.height <= errorEditorBox.y
  );
  if (errorOverlaps) {
    throw new Error(`${viewport.width}px 校验错误展开后浮窗遮挡当前框选范围`);
  }

  await evidenceStage.evaluate((element) => {
    element.style.alignItems = "";
    element.style.paddingBlock = "";
    element.scrollTop = 0;
  });
  await editor.evaluate((element) => {
    element.style.width = "900px";
    element.style.maxHeight = "240px";
  });
  await page.waitForFunction(() => (
    document.querySelector(".capture-editor")?.getAttribute("data-placement") === "bottom"
  ));
  const bottomRangeBox = await page.locator(".capture-range").boundingBox();
  if (!bottomRangeBox) throw new Error(`${viewport.width}px 下方定位探测缺少范围尺寸`);
  const desiredRangeTop = viewport.height - bottomRangeBox.height - 48;
  await page.locator(".capture-range").evaluate((element, top) => {
    const range = element.getBoundingClientRect();
    element.style.transform = `translateY(${top - range.top}px)`;
    window.dispatchEvent(new Event("resize"));
  }, desiredRangeTop);
  await page.waitForFunction(() => (
    document.querySelector(".capture-editor")?.getAttribute("data-placement") === "top"
  ));
  await editor.evaluate((element) => {
    element.style.width = "";
    element.style.maxHeight = "";
  });
  await page.locator(".capture-range").evaluate((element) => {
    element.style.transform = "";
    window.dispatchEvent(new Event("resize"));
  });
  await evidenceStage.evaluate((element) => {
    element.style.alignItems = "";
    element.scrollTop = 0;
  });
  await summary.fill(`公开折线展示 ${viewport.width}px 验收视口中的稳定增长趋势。`);
  await page.getByRole("combobox", { name: "视觉对象 01 类型" }).selectOption("chart");

  if (captureRoot) {
    await page.screenshot({
      path: resolve(captureRoot, `capture-${viewport.width}.png`),
      fullPage: true,
    });
  }
  const requestPromise = page.waitForRequest((request) => (
    request.url().includes("/curation/visuals") && request.method() === "POST"
  ));
  const firstSaveResponse = page.waitForResponse((response) => (
    response.url().includes("/curation/visuals") &&
    response.request().method() === "POST" && response.status() === 201
  ));
  await page.getByRole("button", { name: "保存并返回审核" }).click();
  const request = await requestPromise;
  mutationSnapshots.push((await (await firstSaveResponse).json()).curation.current_snapshot.snapshot_id);
  const payload = request.postDataJSON();
  savedBounds.push(payload.bounds);
  for (const [field, expected] of Object.entries({
    left: 0.18,
    top: 0.22,
    width: 0.44,
    height: 0.44,
  })) {
    if (Math.abs(payload.bounds[field] - expected) > 0.002) {
      throw new Error(`${viewport.width}px ${field} 未按整页显示范围归一化`);
    }
  }
  await page.getByRole("heading", { name: "人工截图" }).waitFor();
  await page.getByText(new RegExp(`${viewport.width}px 验收视口`)).waitFor();

  stage = "追加第二个视觉对象";
  await page.getByRole("button", { name: "再截一个" }).click();
  await page.locator(".capture-mode-instruction").waitFor();
  const secondImageBox = await render.boundingBox();
  if (!secondImageBox) throw new Error(`${viewport.width}px 追加框选时标准页没有尺寸`);
  const secondStart = {
    x: secondImageBox.x + secondImageBox.width * 0.56,
    y: secondImageBox.y + secondImageBox.height * 0.16,
  };
  const secondEnd = {
    x: secondImageBox.x + secondImageBox.width * 0.84,
    y: secondImageBox.y + secondImageBox.height * 0.46,
  };
  await page.mouse.move(secondStart.x, secondStart.y);
  await page.mouse.down();
  await page.mouse.move(secondEnd.x, secondEnd.y, { steps: 4 });
  await page.mouse.up();
  const secondEditor = page.getByRole("dialog", { name: "视觉对象 02" });
  await secondEditor.waitFor();
  const secondRangeBox = await page.locator(".capture-range.is-active").boundingBox();
  if (!secondRangeBox) throw new Error(`${viewport.width}px 左侧定位探测缺少活动范围尺寸`);
  const leftSpace = secondRangeBox.x - 26;
  const rightSpace = viewport.width - secondRangeBox.x - secondRangeBox.width - 26;
  if (leftSpace <= rightSpace) {
    throw new Error(`${viewport.width}px 左侧定位探测的范围没有位于视口右半部`);
  }
  const leftProbeWidth = Math.floor((leftSpace + rightSpace) / 2);
  await secondEditor.evaluate((element, width) => {
    element.style.width = `${width}px`;
  }, leftProbeWidth);
  await page.waitForFunction(() => (
    document.querySelector(".capture-editor")?.getAttribute("data-placement") === "left"
  ));
  await secondEditor.evaluate((element) => {
    element.style.width = "";
  });
  await page.getByRole("textbox", { name: "视觉对象 02 summary" })
    .fill(`公开分布图展示 ${viewport.width}px 视口中的地区差异。`);
  await page.getByRole("combobox", { name: "视觉对象 02 类型" }).selectOption("map");
  if (captureRoot) {
    await page.screenshot({
      path: resolve(captureRoot, `capture-multiple-${viewport.width}.png`),
      fullPage: true,
    });
  }
  const secondSaveResponse = page.waitForResponse((response) => (
    response.url().endsWith("/curation/visuals") &&
    response.request().method() === "POST" && response.status() === 201
  ));
  await secondEditor.getByRole("button", { name: "保存并返回审核" }).click();
  mutationSnapshots.push((await (await secondSaveResponse).json()).curation.current_snapshot.snapshot_id);
  await page.getByRole("button", { name: "编辑视觉对象 02" }).waitFor();
  if ((await page.locator(".capture-range").count()) !== 2) {
    throw new Error(`${viewport.width}px 保存第二个对象后未同时显示两个范围`);
  }

  stage = "编辑第二个视觉对象";
  await page.getByRole("button", { name: "编辑视觉对象 02" }).click();
  const editDialog = page.getByRole("dialog", { name: "视觉对象 02" });
  await editDialog.waitFor();
  const editedSummary = page.getByRole("textbox", { name: "视觉对象 02 summary" });
  await editedSummary.fill(`公开分布图展示 ${viewport.width}px 视口中的地区差异，已人工复核。`);
  const activeRange = page.getByRole("button", { name: /视觉对象 02 框选范围/ });
  const widthBeforePointerResize = await activeRange.evaluate(
    (element) => Number.parseFloat(element.style.width),
  );
  const southeastHandle = page.locator(".capture-resize-handle.is-se");
  const handleBox = await southeastHandle.boundingBox();
  if (!handleBox) throw new Error(`${viewport.width}px 缩放控制点不可见`);
  await page.mouse.move(handleBox.x + handleBox.width / 2, handleBox.y + handleBox.height / 2);
  await page.mouse.down();
  await page.mouse.move(handleBox.x + handleBox.width / 2 + 8, handleBox.y + handleBox.height / 2 + 8);
  await page.mouse.up();
  const widthAfterPointerResize = await activeRange.evaluate(
    (element) => Number.parseFloat(element.style.width),
  );
  if (widthAfterPointerResize <= widthBeforePointerResize) {
    throw new Error(`${viewport.width}px 拖动缩放控制点未调整范围`);
  }
  const nudge = editDialog.getByRole("button", { name: "右移" });
  await nudge.focus();
  await page.keyboard.press("Enter");
  stage = "验证编辑失败保留范围与表单";
  const editFailurePattern = "**/api/v1/pages/*/curation/visuals/*";
  const rejectEdit = async (route) => {
    if (route.request().method() !== "PATCH") return route.fallback();
    return route.fulfill({
      status: 409,
      contentType: "application/json",
      body: JSON.stringify({ error: { code: "simulated_failure", message: "模拟编辑失败。" } }),
    });
  };
  await page.route(editFailurePattern, rejectEdit);
  await editDialog.getByRole("button", { name: "保存修改" }).click();
  await page.getByText(/模拟编辑失败。 当前范围和表单内容仍保留/).waitFor();
  const failedDraftSummary = await editedSummary.inputValue();
  if (failedDraftSummary !== `公开分布图展示 ${viewport.width}px 视口中的地区差异，已人工复核。`) {
    throw new Error(`${viewport.width}px 编辑失败后表单内容发生变化：${failedDraftSummary}`);
  }
  const failedDraftLeft = await page.getByRole("button", {
    name: /视觉对象 02 框选范围/,
  }).evaluate((element) => Number.parseFloat(element.style.left));
  if (Math.abs(failedDraftLeft - 56.1) > 0.01) {
    throw new Error(`${viewport.width}px 编辑失败后键盘微调范围未保留`);
  }
  await page.unroute(editFailurePattern, rejectEdit);

  stage = "保存第二个视觉对象的编辑";
  const editResponse = page.waitForResponse((response) => (
    response.url().includes("/curation/visuals/") &&
    response.request().method() === "PATCH" && response.status() === 201
  ));
  await editDialog.getByRole("button", { name: "保存修改" }).click();
  mutationSnapshots.push((await (await editResponse).json()).curation.current_snapshot.snapshot_id);
  await page.getByText(/已人工复核/).waitFor();

  stage = "上移第二个视觉对象";
  const moveButton = page.getByRole("button", { name: "视觉对象 02 上移" });
  const secondRefBeforeFailedMove = await page.getByRole("button", {
    name: /视觉对象 02 框选范围/,
  }).getAttribute("data-visual-ref");
  const moveFailurePattern = "**/api/v1/pages/*/curation/visuals/*/move";
  const rejectMove = (route) => route.fulfill({
    status: 409,
    contentType: "application/json",
    body: JSON.stringify({ error: { code: "simulated_failure", message: "模拟排序失败。" } }),
  });
  await page.route(moveFailurePattern, rejectMove);
  await moveButton.click();
  await page.getByText(/模拟排序失败。 原顺序与原编号仍保留/).waitFor();
  await page.waitForFunction(() => document.activeElement?.getAttribute("aria-label") === "视觉对象 02 上移");
  const secondRefAfterFailedMove = await page.getByRole("button", {
    name: /视觉对象 02 框选范围/,
  }).getAttribute("data-visual-ref");
  if (!secondRefBeforeFailedMove || secondRefAfterFailedMove !== secondRefBeforeFailedMove) {
    throw new Error(`${viewport.width}px 排序失败后可见编号发生变化`);
  }
  await page.unroute(moveFailurePattern, rejectMove);

  const moveResponse = page.waitForResponse((response) => (
    response.url().endsWith("/move") && response.status() === 201
  ));
  await moveButton.focus();
  await page.keyboard.press("Enter");
  mutationSnapshots.push((await (await moveResponse).json()).curation.current_snapshot.snapshot_id);
  await page.getByText("视觉对象 02 已上移到第 1 位。").waitFor();
  const firstSummary = page.getByRole("button", { name: "编辑视觉对象 01" });
  if (!(await firstSummary.textContent()).includes("已人工复核")) {
    throw new Error(`${viewport.width}px 排序成功后右栏编号未原子更新`);
  }
  const firstCentralRef = await page.getByRole("button", {
    name: /视觉对象 01 框选范围/,
  }).getAttribute("data-visual-ref");
  if (!firstCentralRef) throw new Error(`${viewport.width}px 中央排序编号缺少稳定对象身份`);

  stage = "取消并确认删除排序后的第一个视觉对象";
  const deleteButton = page.getByRole("button", { name: "删除视觉对象 01", exact: true });
  stage = "打开删除确认弹窗";
  await deleteButton.click();
  const deleteDialog = page.getByRole("dialog", { name: "删除视觉对象 01？" });
  await deleteDialog.waitFor();
  stage = "按 Escape 取消删除";
  await page.keyboard.press("Escape");
  await deleteDialog.waitFor({ state: "hidden" });
  await page.waitForFunction(() => document.activeElement?.getAttribute("aria-label") === "删除视觉对象 01");
  stage = "再次打开删除确认弹窗";
  await deleteButton.click();
  await deleteDialog.waitFor();
  stage = "确认删除排序后的第一个视觉对象";
  const deleteFailurePattern = "**/api/v1/pages/*/curation/visuals/*";
  const rejectDelete = async (route) => {
    if (route.request().method() !== "DELETE") return route.fallback();
    return route.fulfill({
      status: 409,
      contentType: "application/json",
      body: JSON.stringify({ error: { code: "simulated_failure", message: "模拟删除失败。" } }),
    });
  };
  await page.route(deleteFailurePattern, rejectDelete);
  const confirmDelete = page.getByRole("button", { name: "确认删除视觉对象 01" });
  await page.waitForFunction(() => document.activeElement?.textContent?.trim() === "确认删除视觉对象 01");
  await page.keyboard.press("Enter");
  await page.getByText(/模拟删除失败。 原对象与原编号仍保留/).waitFor();
  await page.waitForFunction(() => document.activeElement?.textContent?.trim() === "确认删除视觉对象 01");
  if ((await page.locator(".capture-range").count()) !== 2 || !(await deleteDialog.isVisible())) {
    throw new Error(`${viewport.width}px 删除失败后对象或确认上下文未保留`);
  }
  await page.unroute(deleteFailurePattern, rejectDelete);

  stage = "删除排序后的第一个视觉对象";
  const deleteResponse = page.waitForResponse((response) => (
    response.url().includes("/curation/visuals/") &&
    response.request().method() === "DELETE"
  ));
  await confirmDelete.click();
  const completedDeleteResponse = await deleteResponse;
  if (completedDeleteResponse.status() !== 201) {
    throw new Error(
      `${viewport.width}px 删除请求返回 ${completedDeleteResponse.status()}：${await completedDeleteResponse.text()}`,
    );
  }
  mutationSnapshots.push((await completedDeleteResponse.json()).curation.current_snapshot.snapshot_id);
  stage = "等待删除后的重新编号公告";
  await page.getByText(/已删除；其余对象已重新编号/).waitFor();
  stage = "核对删除后的对象身份与焦点";
  if ((await page.locator(".capture-range").count()) !== 1) {
    throw new Error(`${viewport.width}px 删除后未保留并重新编号剩余对象`);
  }
  if ((await page.getByRole("button", { name: "编辑视觉对象 01" }).textContent()).includes("已人工复核")) {
    throw new Error(`${viewport.width}px 删除目标错误或排序后的对象身份未保持`);
  }

  stage = "从已有人工截图的确认摘要续接审批门禁";
  const textSummary = page.getByRole("status", { name: "文字核对摘要" });
  const textSummaryCopy = (await textSummary.textContent())?.replace(/\s+/g, "") ?? "";
  for (const expected of ["文字已确认", "标题1", "正文1", "表格0"]) {
    if (!textSummaryCopy.includes(expected)) {
      throw new Error(`${viewport.width}px 文字核对摘要缺少 ${expected}：${textSummaryCopy}`);
    }
  }
  await page.getByRole("button", { name: "展开文字核对" }).click();
  await page.getByRole("button", { name: "修改文字" }).click();
  await page.getByRole("button", { name: "编辑正文 01" }).click();
  const revisedBody = page.getByRole("textbox", { name: "正文 01 当前编辑值" });
  await revisedBody.fill(`${await revisedBody.inputValue()}（保留既有人工截图）`);
  await page.getByRole("button", { name: "保存并确认修改" }).click();
  await page.getByRole("status", { name: "文字核对摘要" }).waitFor();
  if ((await page.locator(".capture-range").count()) !== 1) {
    throw new Error(`${viewport.width}px 新文字快照没有保留既有人工截图`);
  }
  await page.waitForFunction(() => (
    document.activeElement?.textContent?.trim() === "批准并转到下一待处理页"
  ));
  await page.getByRole("button", { name: "展开文字核对" }).click();
  if (
    await page.getByRole("button", { name: /^(编辑标题|编辑正文)/ }).count() ||
    await page.getByRole("textbox", { name: /当前编辑值/ }).count() ||
    await page.getByRole("button", { name: /文字一致，确认|完成来源审核/ }).count()
  ) {
    throw new Error(`${viewport.width}px 新确认稿未保持只读，或重复暴露确认动作`);
  }
  await page.getByRole("button", { name: "折叠文字核对" }).click();

  const approve = page.getByRole("button", { name: "批准并转到下一待处理页" });
  if (!(await approve.isEnabled())) throw new Error(`${viewport.width}px 保存后批准动作未开放`);
  if (await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth)) {
    throw new Error(`${viewport.width}px 存在页面级横向溢出`);
  }
  const rightScroll = await page.locator(".source-review-scroll").evaluate(
    (element) => getComputedStyle(element).overflowY,
  );
  if (!["auto", "scroll"].includes(rightScroll)) {
    throw new Error(`${viewport.width}px 右栏不是独立滚动区域`);
  }
  stage = "验证完整键盘审核流";
  await approve.focus();
  await page.keyboard.press("a");
  await page.getByText(/上一页已批准|待处理队列已清空/).waitFor();

  const targetTitle = viewport.width === 1280 ? "公开框选验收页 1" : "公开框选验收页 2";
  await page.getByRole("button", { name: "全部", exact: true }).click();
  const approvedRow = page.getByRole("button", {
    name: new RegExp(`${targetTitle}，已批准`),
  });
  await approvedRow.waitFor();
  await approvedRow.click();
  await page.getByText("批准结论已冻结").waitFor();
  const reopen = page.getByRole("button", { name: "重新打开此页" });
  await reopen.focus();
  await page.keyboard.press("r");
  const reopenDialog = page.getByRole("dialog", { name: "重新打开第 1 页？" });
  await reopenDialog.waitFor();
  await page.keyboard.press("Escape");
  await reopenDialog.waitFor({ state: "hidden" });
  await page.keyboard.press("r");
  await page.getByRole("button", { name: "确认重新打开" }).click();
  await page.getByText("页面已重新打开，恢复为待处理并解锁编辑。").waitFor();

  const rows = page.locator(".page-row");
  const currentText = await page.locator('.page-row[aria-current="true"]').textContent();
  const currentIndex = await rows.evaluateAll((elements) => (
    elements.findIndex((element) => element.getAttribute("aria-current") === "true")
  ));
  const rowCount = await rows.count();
  const navigationKey = currentIndex < rowCount - 1 ? "ArrowRight" : "ArrowLeft";
  await page.keyboard.press(navigationKey);
  if (await page.locator('.page-row[aria-current="true"]').textContent() === currentText) {
    throw new Error(`${viewport.width}px ${navigationKey} 未移动页面选择`);
  }
  const reopenedRow = page.getByRole("button", {
    name: new RegExp(`${targetTitle}，待处理`),
  });
  await reopenedRow.click();
  await page.locator(".review-gate").click({ position: { x: 8, y: 8 } });
  await page.keyboard.press("x");
  const exclusionReason = page.getByRole("combobox", { name: "整页排除原因" });
  if (!(await exclusionReason.evaluate((element) => element === document.activeElement))) {
    throw new Error(`${viewport.width}px X 未把焦点送到整页排除原因`);
  }
  const selectedBeforeInputKeys = await page.locator('.page-row[aria-current="true"]').textContent();
  await page.keyboard.press("ArrowRight");
  await page.keyboard.press("a");
  await page.keyboard.press("r");
  if (await page.locator('.page-row[aria-current="true"]').textContent() !== selectedBeforeInputKeys) {
    throw new Error(`${viewport.width}px 输入控件未抑制全局页面快捷键`);
  }
  if (await reopenDialog.isVisible()) {
    throw new Error(`${viewport.width}px 输入控件中的 R 意外打开了重开确认框`);
  }
  await exclusionReason.selectOption("irrelevant");
  await page.getByRole("button", { name: "排除并转到下一待处理页" }).click();
  await page.getByText(/上一页已排除|待处理队列已清空/).waitFor();
  checks.push(`capture-viewport-${viewport.width}`);
  checks.push(`capture-next-gate-${viewport.width}`);
  checks.push(`keyboard-flow-${viewport.width}`);
  await page.close();
  } catch (error) {
    process.stderr.write(`BLACKBOX_STAGE=${viewport.width}:${stage}\n`);
    error.message = `${viewport.width}px ${stage}：${error.message}`;
    throw error;
  }
}

try {
  await exercise(routes[0], { width: 1280, height: 900 });
  await exercise(routes[1], { width: 1440, height: 1024 });
  process.stdout.write(JSON.stringify({ ok: true, checks, savedBounds, mutationSnapshots }));
} finally {
  await browser.close();
}
