import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { App } from "./App";

const bootstrap = {
  actor: { actor_id: "operator-queue", display_name: "操作者 operator-queue" },
  runways: [
    { id: "pending", label: "待处理", documents: [] },
    { id: "processing", label: "处理中", documents: [] },
    { id: "curatable", label: "可策展", documents: [] },
  ],
};

const source = {
  titles: ["队列测试标题"],
  body: ["队列测试正文。"],
  tables: [],
  images: [],
  speaker_notes: [],
};

function page(pageNumber: number, status: "pending" | "approved" | "excluded") {
  return {
    page_id: `page-${pageNumber}`,
    chunk_id: `chunk-${pageNumber}`,
    document_id: "document-queue",
    version_id: "version-queue",
    page_number: pageNumber,
    review_status: status,
    title: `队列页 ${pageNumber}`,
    hidden: false,
    enabled: true,
    source_reference: {
      slide_id: 255 + pageNumber,
      relationship_id: `rId${pageNumber}`,
      part: `ppt/slides/slide${pageNumber}.xml`,
    },
    enablement: null,
    review: {
      status,
      reviewed_by: status === "pending" ? null : "curator-old",
      reviewed_at: status === "pending" ? null : "2026-08-25T10:00:00+00:00",
      source_version_id: status === "pending" ? null : "version-queue",
      inherited_from_page_version_id: pageNumber === 4 ? "page-version-old" : null,
      exclusion_reason: status === "excluded" ? "irrelevant" : null,
      exclusion_note: null,
    },
  };
}

function curation(status: "pending" | "approved" | "excluded") {
  return {
    current_snapshot: null,
    image_sources: { total: 0, unresolved: 0, items: [] },
    chunk_body: { nonempty: true },
    blockers: [
      { code: "source_unsaved", message: "文字修改尚未保存。" },
      { code: "source_unconfirmed", message: "文字来源尚未确认。" },
      { code: "source_review_incomplete", message: "来源审核尚未完成。" },
    ],
    can_confirm_source: false,
    can_complete_source_review: false,
    can_approve: false,
    review_status: status,
  };
}

beforeEach(() => {
  window.history.replaceState(null, "", "/curation");
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("审核队列、排除与重开", () => {
  it("切换页面时按当前阶段提交唯一的单调活跃计时片段", async () => {
    const pages = [page(1, "pending"), page(2, "pending")];
    const samples: Array<Record<string, unknown>> = [];
    let monotonicNow = 100;
    const performanceNow = vi.spyOn(performance, "now").mockImplementation(() => monotonicNow);
    vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
      const url = String(input);
      if (url === "/api/v1/app/bootstrap") {
        return Promise.resolve(new Response(JSON.stringify(bootstrap), { status: 200 }));
      }
      if (url.endsWith("review_status=pending")) {
        return Promise.resolve(new Response(JSON.stringify({ pages }), { status: 200 }));
      }
      const detailMatch = url.match(/^\/api\/v1\/pages\/(page-\d+)$/);
      if (detailMatch && !init?.method) {
        const selected = pages.find((item) => item.page_id === detailMatch[1])!;
        return Promise.resolve(new Response(JSON.stringify({
          page_id: selected.page_id,
          page_number: selected.page_number,
          review_status: "pending",
          review: selected.review,
          source_content: { ...source, titles: [selected.title] },
          curation: curation("pending"),
        }), { status: 200 }));
      }
      if (url === "/api/v1/curation/runtime-facts/samples" && init?.method === "POST") {
        samples.push(JSON.parse(String(init.body)));
        return Promise.resolve(new Response(JSON.stringify({ status: "recorded" }), { status: 201 }));
      }
      throw new Error(`未覆盖的请求：${url}`);
    });

    render(<App />);
    expect(await screen.findByRole("button", { name: "编辑标题 1" })).toBeInTheDocument();
    expect(screen.getAllByText("队列页 1").length).toBeGreaterThan(0);
    await waitFor(() => expect(performanceNow).toHaveBeenCalled());
    monotonicNow = 1_350;
    window.dispatchEvent(new Event("pagehide"));
    await waitFor(() => expect(samples).toHaveLength(1));
    const restored = new Event("pageshow");
    Object.defineProperty(restored, "persisted", { value: true });
    monotonicNow = 1_400;
    window.dispatchEvent(restored);
    monotonicNow = 1_900;
    window.dispatchEvent(new Event("pagehide"));
    await waitFor(() => expect(samples).toHaveLength(2));
    await userEvent.click(screen.getByRole("button", { name: /第 2 页，队列页 2，待处理/ }));

    await waitFor(() => expect(samples).toHaveLength(2));
    expect(samples[0]).toMatchObject({
      page_id: "page-1",
      version_id: "version-queue",
      stage: "source_review",
      duration_ms: 1_250,
    });
    expect(samples[0].sample_id).toEqual(expect.any(String));
    expect(samples[1]).toMatchObject({
      page_id: "page-1",
      version_id: "version-queue",
      stage: "source_review",
      duration_ms: 500,
    });
    expect(samples[1].sample_id).not.toBe(samples[0].sample_id);
  });

  it("默认聚焦待处理页，批量排除后保留筛选并明确提供恢复路径", async () => {
    let pages = [page(1, "pending"), page(2, "pending"), page(3, "approved"), page(4, "excluded")];
    vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
      const url = String(input);
      if (url === "/api/v1/app/bootstrap") {
        return Promise.resolve(new Response(JSON.stringify(bootstrap), { status: 200 }));
      }
      if (url.startsWith("/api/v1/curation/pages?review_status=")) {
        const filter = new URL(url, "http://local").searchParams.get("review_status");
        const visible = filter === "all"
          ? pages
          : filter === "inherited"
            ? pages.filter((item) => item.review.inherited_from_page_version_id)
            : pages.filter((item) => item.review_status === filter);
        return Promise.resolve(new Response(JSON.stringify({ pages: visible }), { status: 200 }));
      }
      const detailMatch = url.match(/^\/api\/v1\/pages\/(page-\d+)$/);
      if (detailMatch && !init?.method) {
        const selected = pages.find((item) => item.page_id === detailMatch[1])!;
        return Promise.resolve(new Response(JSON.stringify({
          page_id: selected.page_id,
          page_number: selected.page_number,
          review_status: selected.review_status,
          review: selected.review,
          source_content: source,
          curation: curation(selected.review_status),
        }), { status: 200 }));
      }
      if (url === "/api/v1/pages/batch-exclude" && init?.method === "POST") {
        const body = JSON.parse(String(init.body));
        pages = pages.map((item) => body.page_ids.includes(item.page_id)
          ? {
              ...item,
              review_status: "excluded" as const,
              review: {
                ...item.review,
                status: "excluded" as const,
                reviewed_by: "operator-queue",
                reviewed_at: "2026-08-26T10:00:00+00:00",
                exclusion_reason: body.reason,
              },
            }
          : item);
        return Promise.resolve(new Response(JSON.stringify({
          requested: body.page_ids.length,
          excluded: body.page_ids,
          failed: [],
          complete: true,
        }), { status: 200 }));
      }
      if (url === "/api/v1/pages/page-1/reopen" && init?.method === "POST") {
        pages = pages.map((item) => item.page_id === "page-1"
          ? {
              ...item,
              review_status: "pending" as const,
              review: {
                ...item.review,
                status: "pending" as const,
                reviewed_by: null,
                reviewed_at: null,
                source_version_id: null,
                exclusion_reason: null,
              },
            }
          : item);
        return Promise.resolve(new Response(JSON.stringify({ review: pages[0].review }), { status: 200 }));
      }
      throw new Error(`未覆盖的请求：${url}`);
    });

    render(<App />);

    expect(await screen.findByRole("heading", { name: "逐页策展" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "待处理" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: "已继承" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /队列页 3/ })).not.toBeInTheDocument();

    await userEvent.click(await screen.findByRole("checkbox", { name: "选择第 1 页，队列页 1" }));
    await userEvent.click(screen.getByRole("checkbox", { name: "选择第 2 页，队列页 2" }));
    const batch = screen.getByRole("region", { name: "批量排除" });
    expect(within(batch).getByText("已选 2 页")).toBeInTheDocument();
    expect(screen.queryByText(/批量批准|批量确认来源|跨页复制/)).not.toBeInTheDocument();
    await userEvent.selectOptions(within(batch).getByRole("combobox", { name: "统一排除原因" }), "irrelevant");
    await userEvent.click(within(batch).getByRole("button", { name: "批量排除 2 页" }));

    expect(await screen.findByText("已批量排除 2 页。每页均已分别记录审核事件。")).toBeInTheDocument();
    expect(screen.getByText("待处理队列为空")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "查看已继承" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "查看全部" }));
    expect(await screen.findByRole("button", { name: /第 1 页，队列页 1，已排除/ })).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: /第 1 页，队列页 1，已排除/ }));
    expect(await screen.findByText("排除结论已冻结")).toBeInTheDocument();
    screen.getByRole("button", { name: "重新打开此页" }).focus();
    await userEvent.keyboard("r");
    const dialog = await screen.findByRole("dialog", { name: "重新打开第 1 页？" });
    await userEvent.keyboard("{Escape}");
    await waitFor(() => expect(dialog).not.toBeInTheDocument());
    await userEvent.keyboard("r");
    await userEvent.click(await screen.findByRole("button", { name: "确认重新打开" }));
    expect(await screen.findByText("页面已重新打开，恢复为待处理并解锁编辑。")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "编辑标题 1" })).toBeEnabled();
  });

  it("批量排除部分冲突时只保留未处理页的选择与统一原因", async () => {
    let pages = [page(1, "pending"), page(2, "pending")];
    vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
      const url = String(input);
      if (url === "/api/v1/app/bootstrap") {
        return Promise.resolve(new Response(JSON.stringify(bootstrap), { status: 200 }));
      }
      if (url.endsWith("review_status=pending")) {
        return Promise.resolve(new Response(JSON.stringify({ pages }), { status: 200 }));
      }
      const detailMatch = url.match(/^\/api\/v1\/pages\/(page-\d+)$/);
      if (detailMatch && !init?.method) {
        const selected = pages.find((item) => item.page_id === detailMatch[1]) ?? page(2, "pending");
        return Promise.resolve(new Response(JSON.stringify({
          page_id: selected.page_id,
          page_number: selected.page_number,
          review_status: selected.review_status,
          review: selected.review,
          source_content: source,
          curation: curation(selected.review_status),
        }), { status: 200 }));
      }
      if (url === "/api/v1/pages/batch-exclude" && init?.method === "POST") {
        pages = [page(2, "pending")];
        return Promise.resolve(new Response(JSON.stringify({
          requested: 2,
          excluded: ["page-1"],
          failed: [{
            page_id: "page-2",
            code: "curation_state_changed",
            message: "页面状态已被其他会话改变，请重新加载。",
          }],
          complete: false,
        }), { status: 207 }));
      }
      throw new Error(`未覆盖的请求：${url}`);
    });

    render(<App />);
    await userEvent.click(await screen.findByRole("checkbox", { name: "选择第 1 页，队列页 1" }));
    await userEvent.click(screen.getByRole("checkbox", { name: "选择第 2 页，队列页 2" }));
    const batch = screen.getByRole("region", { name: "批量排除" });
    await userEvent.selectOptions(within(batch).getByRole("combobox", { name: "统一排除原因" }), "duplicate");
    await userEvent.click(within(batch).getByRole("button", { name: "批量排除 2 页" }));

    expect(await screen.findByText(/1 页未处理.*第 2 页/)).toBeInTheDocument();
    expect(screen.queryByRole("checkbox", { name: "选择第 1 页，队列页 1" })).not.toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: "选择第 2 页，队列页 2" })).toBeChecked();
    expect(screen.getByRole("combobox", { name: "统一排除原因" })).toHaveValue("duplicate");
  });

  it("方向键、筛选和包含当前页的批量排除共用文字导航保护", async () => {
    let pages = [page(1, "pending"), page(2, "pending")];
    const batchRequests: Array<Record<string, unknown>> = [];
    vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
      const url = String(input);
      if (url === "/api/v1/app/bootstrap") {
        return Promise.resolve(new Response(JSON.stringify(bootstrap), { status: 200 }));
      }
      if (url.startsWith("/api/v1/curation/pages?review_status=")) {
        const selectedFilter = new URL(url, "http://local").searchParams.get("review_status");
        const visible = selectedFilter === "all"
          ? pages
          : pages.filter((item) => item.review_status === selectedFilter);
        return Promise.resolve(new Response(JSON.stringify({ pages: visible }), { status: 200 }));
      }
      const detailMatch = url.match(/^\/api\/v1\/pages\/(page-\d+)$/);
      if (detailMatch && !init?.method) {
        const selected = pages.find((item) => item.page_id === detailMatch[1])!;
        return Promise.resolve(new Response(JSON.stringify({
          page_id: selected.page_id,
          page_number: selected.page_number,
          review_status: selected.review_status,
          review: selected.review,
          source_content: { ...source, titles: [selected.title] },
          curation: curation(selected.review_status),
        }), { status: 200 }));
      }
      if (url === "/api/v1/pages/batch-exclude" && init?.method === "POST") {
        const payload = JSON.parse(String(init.body));
        batchRequests.push(payload);
        pages = pages.map((item) => payload.page_ids.includes(item.page_id)
          ? { ...item, review_status: "excluded" as const }
          : item);
        return Promise.resolve(new Response(JSON.stringify({
          requested: payload.page_ids.length,
          excluded: payload.page_ids,
          failed: [],
          complete: true,
        }), { status: 200 }));
      }
      throw new Error(`未覆盖的请求：${url}`);
    });

    render(<App />);
    await userEvent.click(await screen.findByRole("button", { name: "编辑标题 1" }));
    const title = screen.getByRole("textbox", { name: "标题 1 当前编辑值" });
    await userEvent.clear(title);
    await userEvent.type(title, "尚未保存的队列标题");
    title.blur();

    const currentRow = screen.getByRole("button", { name: /第 1 页，队列页 1，待处理/ });
    currentRow.focus();
    fireEvent.keyDown(currentRow, { key: "ArrowRight" });
    expect(screen.getByRole("dialog", { name: "放弃当前页的文字修改？" }))
      .toHaveTextContent("转到第 02 页");
    await userEvent.keyboard("{Escape}");
    expect(title).toHaveValue("尚未保存的队列标题");

    await userEvent.click(screen.getByRole("button", { name: "全部" }));
    expect(screen.getByRole("dialog", { name: "放弃当前页的文字修改？" }))
      .toHaveTextContent("切换到“全部”筛选");
    await userEvent.keyboard("{Escape}");

    await userEvent.click(screen.getByRole("checkbox", { name: "选择第 1 页，队列页 1" }));
    const batch = screen.getByRole("region", { name: "批量排除" });
    await userEvent.selectOptions(
      within(batch).getByRole("combobox", { name: "统一排除原因" }),
      "duplicate",
    );
    await userEvent.click(within(batch).getByRole("button", { name: "批量排除 1 页" }));
    const firstBatchDialog = screen.getByRole("dialog", { name: "放弃当前页的文字修改？" });
    expect(firstBatchDialog).toHaveTextContent("提交批量排除并离开当前页");
    expect(batchRequests).toHaveLength(0);
    await userEvent.keyboard("{Enter}");
    expect(title).toHaveValue("尚未保存的队列标题");

    await userEvent.click(within(batch).getByRole("button", { name: "批量排除 1 页" }));
    await waitFor(() => expect(
      screen.getByRole("button", { name: "留在当前页" }),
    ).toHaveFocus());
    await userEvent.tab();
    expect(screen.getByRole("button", { name: "放弃修改并离开" })).toHaveFocus();
    await userEvent.keyboard("{Enter}");

    await waitFor(() => expect(batchRequests).toHaveLength(1));
    await waitFor(() => expect(within(
      screen.getByRole("region", { name: "标题与正文核对稿" }),
    ).getByText("队列页 2")).toBeInTheDocument());
    expect(screen.queryByText("尚未保存的队列标题")).not.toBeInTheDocument();
  });

  it("方向键移动页面，输入聚焦时停用全局快捷键，X 只聚焦排除原因", async () => {
    const pages = [page(1, "pending"), page(2, "pending")];
    vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
      const url = String(input);
      if (url === "/api/v1/app/bootstrap") {
        return Promise.resolve(new Response(JSON.stringify(bootstrap), { status: 200 }));
      }
      if (url.endsWith("review_status=pending")) {
        return Promise.resolve(new Response(JSON.stringify({ pages }), { status: 200 }));
      }
      const detailMatch = url.match(/^\/api\/v1\/pages\/(page-\d+)$/);
      if (detailMatch && !init?.method) {
        const selected = pages.find((item) => item.page_id === detailMatch[1])!;
        return Promise.resolve(new Response(JSON.stringify({
          page_id: selected.page_id,
          page_number: selected.page_number,
          review_status: "pending",
          review: selected.review,
          source_content: { ...source, titles: [selected.title] },
          curation: curation("pending"),
        }), { status: 200 }));
      }
      throw new Error(`未覆盖的请求：${url}`);
    });

    render(<App />);
    await userEvent.click(await screen.findByRole("button", { name: "编辑标题 1" }));
    const title = screen.getByRole("textbox", { name: "标题 1 当前编辑值" });
    expect(title).toHaveValue("队列页 1");
    title.blur();
    const selectedRow = screen.getByRole("button", { name: /第 1 页，队列页 1，待处理/ });
    selectedRow.focus();
    fireEvent.keyDown(selectedRow, { key: "ArrowRight" });
    await waitFor(() => expect(within(
      screen.getByRole("region", { name: "标题与正文核对稿" }),
    ).getByText("队列页 2")).toBeInTheDocument());
    await userEvent.click(screen.getByRole("button", { name: "编辑标题 1" }));
    const secondTitle = screen.getByRole("textbox", { name: "标题 1 当前编辑值" });
    secondTitle.focus();
    await userEvent.keyboard("{ArrowLeft}");
    expect(secondTitle).toHaveValue("队列页 2");
    secondTitle.blur();
    await userEvent.keyboard("x");
    expect(screen.getByRole("combobox", { name: "整页排除原因" })).toHaveFocus();
    expect(screen.getAllByText("待处理").length).toBeGreaterThan(0);
  });

  it("单页排除已选中的中间页后清理批量选择，并进入原顺序的下一页", async () => {
    let pages = [page(1, "pending"), page(2, "pending"), page(3, "pending")];
    window.history.replaceState(
      null,
      "",
      "/curation?document=document-queue&version=version-queue&page=2",
    );
    vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
      const url = String(input);
      if (url === "/api/v1/app/bootstrap") {
        return Promise.resolve(new Response(JSON.stringify(bootstrap), { status: 200 }));
      }
      if (url.endsWith("review_status=pending")) {
        return Promise.resolve(new Response(JSON.stringify({ pages }), { status: 200 }));
      }
      const detailMatch = url.match(/^\/api\/v1\/pages\/(page-\d+)$/);
      if (detailMatch && !init?.method) {
        const selected = pages.find((item) => item.page_id === detailMatch[1])!;
        return Promise.resolve(new Response(JSON.stringify({
          page_id: selected.page_id,
          page_number: selected.page_number,
          review_status: selected.review_status,
          review: selected.review,
          source_content: { ...source, titles: [selected.title] },
          curation: curation(selected.review_status),
        }), { status: 200 }));
      }
      if (url === "/api/v1/pages/page-2/exclude" && init?.method === "POST") {
        pages = pages.filter((item) => item.page_id !== "page-2");
        return Promise.resolve(new Response(JSON.stringify({
          review: {
            ...page(2, "excluded").review,
            reviewed_by: "operator-queue",
            reviewed_at: "2026-08-26T10:00:00+00:00",
            exclusion_reason: "irrelevant",
          },
        }), { status: 200 }));
      }
      throw new Error(`未覆盖的请求：${url}`);
    });

    render(<App />);
    const initialManuscript = await screen.findByRole("region", { name: "标题与正文核对稿" });
    expect(within(initialManuscript).getByText("队列页 2")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("checkbox", { name: "选择第 2 页，队列页 2" }));
    expect(screen.getByRole("region", { name: "批量排除" })).toHaveTextContent("已选 1 页");
    await userEvent.selectOptions(
      screen.getByRole("combobox", { name: "整页排除原因" }),
      "irrelevant",
    );
    await userEvent.click(screen.getByRole("button", { name: "排除并转到下一待处理页" }));

    await waitFor(() => expect(within(
      screen.getByRole("region", { name: "标题与正文核对稿" }),
    ).getByText("队列页 3")).toBeInTheDocument());
    expect(screen.queryByRole("region", { name: "批量排除" })).not.toBeInTheDocument();
  });
});
