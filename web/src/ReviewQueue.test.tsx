import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
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
    expect(screen.getByRole("textbox", { name: "标题来源 1 当前编辑值" })).toBeEnabled();
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
    const title = await screen.findByRole("textbox", { name: "标题来源 1 当前编辑值" });
    expect(title).toHaveValue("队列页 1");
    title.blur();
    await userEvent.keyboard("{ArrowRight}");
    expect(await screen.findByRole("textbox", { name: "标题来源 1 当前编辑值" })).toHaveValue("队列页 2");
    const secondTitle = screen.getByRole("textbox", { name: "标题来源 1 当前编辑值" });
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
    expect(await screen.findByRole("textbox", { name: "标题来源 1 当前编辑值" })).toHaveValue("队列页 2");
    await userEvent.click(screen.getByRole("checkbox", { name: "选择第 2 页，队列页 2" }));
    expect(screen.getByRole("region", { name: "批量排除" })).toHaveTextContent("已选 1 页");
    await userEvent.selectOptions(
      screen.getByRole("combobox", { name: "整页排除原因" }),
      "irrelevant",
    );
    await userEvent.click(screen.getByRole("button", { name: "排除并转到下一待处理页" }));

    expect(await screen.findByRole("textbox", { name: "标题来源 1 当前编辑值" })).toHaveValue("队列页 3");
    expect(screen.queryByRole("region", { name: "批量排除" })).not.toBeInTheDocument();
  });
});
