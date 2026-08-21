import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { App } from "./App";

const bootstrap = {
  actor: { actor_id: "operator-zhang", display_name: "操作者 operator-zhang" },
  runways: [
    { id: "pending", label: "待处理", documents: [] },
    { id: "processing", label: "处理中", documents: [] },
    { id: "curatable", label: "可策展", documents: [] },
  ],
};

const visiblePage = {
  page_id: "page-1",
  chunk_id: "chunk-1",
  document_id: "document-1",
  version_id: "version-1",
  page_number: 1,
  review_status: "pending",
  title: "公开合成页一",
  hidden: false,
  enabled: true,
  source_reference: {
    slide_id: 256,
    relationship_id: "rId7",
    part: "ppt/slides/slide1.xml",
  },
  enablement: null,
};

const hiddenPage = {
  page_id: null,
  chunk_id: null,
  document_id: "document-1",
  version_id: "version-1",
  page_number: 2,
  review_status: null,
  title: null,
  hidden: true,
  enabled: false,
  source_reference: {
    slide_id: 257,
    relationship_id: "rId8",
    part: "ppt/slides/slide2.xml",
  },
  enablement: { status: "not_started", job_id: null, error: null },
};

const hiddenPageB = {
  ...hiddenPage,
  page_number: 3,
  source_reference: {
    slide_id: 258,
    relationship_id: "rId9",
    part: "ppt/slides/slide3.xml",
  },
};

beforeEach(() => {
  window.history.replaceState(null, "", "/documents");
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("默认文档入口", () => {
  it("由真实 bootstrap 数据呈现操作者和三条诚实空跑道", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify(bootstrap), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );

    render(<App />);

    expect(screen.getByText("正在连接文档入口…")).toBeInTheDocument();
    expect(await screen.findByText("操作者 operator-zhang")).toBeInTheDocument();
    expect(screen.getAllByRole("region")).toHaveLength(3);
    expect(screen.getByText("还没有待处理文档")).toBeInTheDocument();
    expect(screen.getByText("当前没有处理中的文档")).toBeInTheDocument();
    expect(screen.getByText("还没有可策展文档")).toBeInTheDocument();
    expect(screen.queryByText("区域经营分析")).not.toBeInTheDocument();
  });

  it("显示 API 的安全错误消息并允许恢复", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch");
    fetchMock.mockResolvedValueOnce(
      new Response(
        JSON.stringify({
          error: { code: "bootstrap_unavailable", message: "文档入口暂时不可用。" },
        }),
        { status: 503, headers: { "Content-Type": "application/json" } },
      ),
    );
    fetchMock.mockResolvedValueOnce(
      new Response(JSON.stringify(bootstrap), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );

    render(<App />);

    expect(await screen.findByRole("alert")).toHaveTextContent("文档入口暂时不可用。");
    await userEvent.click(screen.getByRole("button", { name: "重新连接" }));
    expect(await screen.findByText("操作者 operator-zhang")).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("上传入口响应操作但不发送票外请求", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify(bootstrap), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    render(<App />);
    await screen.findByText("操作者 operator-zhang");

    await userEvent.click(screen.getByRole("button", { name: "上传 PPTX（暂未开放）" }));

    expect(screen.getByRole("status")).toHaveTextContent("上传流程将在 #20 接入");
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
  });

  it("重复刷新会取消旧请求且忽略过期响应", async () => {
    const requests: Array<{
      signal: AbortSignal | null | undefined;
      resolve: (response: Response) => void;
    }> = [];
    vi.spyOn(globalThis, "fetch").mockImplementation((_input, init) => {
      return new Promise<Response>((resolve) => {
        requests.push({ signal: init?.signal, resolve });
      });
    });

    render(<App />);
    await waitFor(() => expect(requests).toHaveLength(1));
    await userEvent.keyboard("r");
    await waitFor(() => expect(requests).toHaveLength(2));

    expect(requests[0].signal?.aborted).toBe(true);
    requests[1].resolve(
      new Response(JSON.stringify(bootstrap), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    expect(await screen.findByText("操作者 operator-zhang")).toBeInTheDocument();

    requests[0].resolve(
      new Response(
        JSON.stringify({ error: { code: "stale", message: "过期请求不应覆盖界面。" } }),
        { status: 503, headers: { "Content-Type": "application/json" } },
      ),
    );
    await waitFor(() => expect(screen.queryByText("过期请求不应覆盖界面。")).not.toBeInTheDocument());
  });
});

describe("隐藏页策展工作台", () => {
  it("仅在全部视图登记隐藏页，并从持久任务恢复为普通 pending 页", async () => {
    window.history.replaceState(null, "", "/curation");
    let enabled = false;
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
      const url = String(input);
      if (url === "/api/v1/app/bootstrap") {
        return Promise.resolve(
          new Response(JSON.stringify(bootstrap), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          }),
        );
      }
      if (url.endsWith("review_status=pending")) {
        return Promise.resolve(
          new Response(JSON.stringify({ pages: [visiblePage] }), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          }),
        );
      }
      if (url.endsWith("review_status=all")) {
        const processed = enabled
          ? {
              ...hiddenPage,
              page_id: "page-2",
              chunk_id: "chunk-2",
              title: "公开合成隐藏页",
              review_status: "pending",
              enabled: true,
              enablement: { status: "succeeded", job_id: "job-1", error: null },
            }
          : hiddenPage;
        return Promise.resolve(
          new Response(JSON.stringify({ pages: [visiblePage, processed] }), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          }),
        );
      }
      if (url.endsWith("/source-pages/2/enable") && init?.method === "POST") {
        return Promise.resolve(
          new Response(
            JSON.stringify({
              document_id: "document-1",
              version_id: "version-1",
              page_number: 2,
              job_id: "job-1",
              status: "accepted",
              page_id: null,
            }),
            { status: 202, headers: { "Content-Type": "application/json" } },
          ),
        );
      }
      if (url === "/api/v1/jobs/job-1") {
        enabled = true;
        return Promise.resolve(
          new Response(
            JSON.stringify({
              job_id: "job-1",
              kind: "page.enable",
              status: "succeeded",
              attempts: 1,
              next_retry_at: null,
              progress: { phase: "activation", completed_pages: 1, total_pages: 1 },
              error: null,
            }),
            { status: 200, headers: { "Content-Type": "application/json" } },
          ),
        );
      }
      if (url === "/api/v1/pages/page-1" || url === "/api/v1/pages/page-2") {
        const second = url.endsWith("page-2");
        return Promise.resolve(
          new Response(
            JSON.stringify({
              page_id: second ? "page-2" : "page-1",
              page_number: second ? 2 : 1,
              review_status: "pending",
              source_content: {
                titles: [second ? "公开合成隐藏页" : "公开合成页一"],
                body: [second ? "隐藏页真实正文" : "普通页正文"],
                speaker_notes: [],
              },
            }),
            { status: 200, headers: { "Content-Type": "application/json" } },
          ),
        );
      }
      throw new Error(`未覆盖的请求：${url}`);
    });

    render(<App />);

    expect(await screen.findByRole("heading", { name: "逐页策展" })).toBeInTheDocument();
    expect(screen.queryByText("隐藏页 · 未处理")).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "全部" }));
    await userEvent.click(
      await screen.findByRole("button", {
        name: "第 2 页，隐藏页 · 未处理，默认跳过",
      }),
    );

    expect(screen.getByText("此页尚未生成标准渲染")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "源页登记" })).toBeInTheDocument();
    expect(screen.getByText("ppt/slides/slide2.xml")).toBeInTheDocument();
    expect(screen.queryByText("AnyDoc 来源")).not.toBeInTheDocument();

    const enable = screen.getByRole("button", { name: "启用并处理此页" });
    await userEvent.dblClick(enable);
    expect(await screen.findByText("处理完成，页面已进入待处理队列。")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("status")).toHaveFocus());
    expect(
      screen.getByRole("button", { name: "第 2 页，公开合成隐藏页，待处理" }),
    ).toBeInTheDocument();
    expect(screen.getByText("页已进入普通策展流程")).toBeInTheDocument();
    expect(await screen.findByText("隐藏页真实正文")).toBeInTheDocument();
    expect(
      fetchMock.mock.calls.filter(([input]) => String(input).endsWith("/source-pages/2/enable")),
    ).toHaveLength(1);
  });

  it("提交期间切换页面不会把忙碌状态、提示或焦点带到另一隐藏页", async () => {
    window.history.replaceState(null, "", "/curation");
    let resolveEnable: ((response: Response) => void) | undefined;
    const enableResponse = new Promise<Response>((resolve) => {
      resolveEnable = resolve;
    });
    vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
      const url = String(input);
      if (url === "/api/v1/app/bootstrap") {
        return Promise.resolve(new Response(JSON.stringify(bootstrap), { status: 200 }));
      }
      if (url.endsWith("review_status=pending")) {
        return Promise.resolve(new Response(JSON.stringify({ pages: [visiblePage] }), { status: 200 }));
      }
      if (url.endsWith("review_status=all")) {
        return Promise.resolve(
          new Response(JSON.stringify({ pages: [visiblePage, hiddenPage, hiddenPageB] }), {
            status: 200,
          }),
        );
      }
      if (url === "/api/v1/pages/page-1") {
        return Promise.resolve(
          new Response(
            JSON.stringify({
              page_id: "page-1",
              page_number: 1,
              review_status: "pending",
              source_content: { titles: ["公开合成页一"], body: [], speaker_notes: [] },
            }),
            { status: 200 },
          ),
        );
      }
      if (url.endsWith("/source-pages/2/enable") && init?.method === "POST") {
        return enableResponse;
      }
      throw new Error(`未覆盖的请求：${url}`);
    });

    render(<App />);
    await screen.findByRole("heading", { name: "逐页策展" });
    await userEvent.click(screen.getByRole("button", { name: "全部" }));
    await userEvent.click(
      await screen.findByRole("button", { name: "第 2 页，隐藏页 · 未处理，默认跳过" }),
    );
    await userEvent.click(screen.getByRole("button", { name: "启用并处理此页" }));
    expect(screen.getByText("正在提交启用请求。")).toBeInTheDocument();

    const thirdPage = screen.getByRole("button", {
      name: "第 3 页，隐藏页 · 未处理，默认跳过",
    });
    await userEvent.click(thirdPage);
    expect(screen.getByRole("button", { name: "启用并处理此页" })).toBeEnabled();
    expect(screen.queryByText("正在提交启用请求。")).not.toBeInTheDocument();

    resolveEnable?.(
      new Response(
        JSON.stringify({
          document_id: "document-1",
          version_id: "version-1",
          page_number: 2,
          job_id: "job-1",
          status: "accepted",
          page_id: null,
        }),
        { status: 202 },
      ),
    );
    await waitFor(() => expect(thirdPage).toHaveFocus());
    expect(screen.queryByText("任务已排队，等待处理。")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "启用并处理此页" })).toBeEnabled();
  });

  it("后台轮询更新 aria-live 文案时不会反复抢走操作者焦点", async () => {
    window.history.replaceState(null, "", "/curation");
    let resolveJob: ((response: Response) => void) | undefined;
    const jobResponse = new Promise<Response>((resolve) => {
      resolveJob = resolve;
    });
    const queuedHiddenPage = {
      ...hiddenPage,
      enablement: { status: "queued", job_id: "job-queued", error: null },
    };
    vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
      const url = String(input);
      if (url === "/api/v1/app/bootstrap") {
        return Promise.resolve(new Response(JSON.stringify(bootstrap), { status: 200 }));
      }
      if (url.endsWith("review_status=pending")) {
        return Promise.resolve(new Response(JSON.stringify({ pages: [visiblePage] }), { status: 200 }));
      }
      if (url.endsWith("review_status=all")) {
        return Promise.resolve(
          new Response(JSON.stringify({ pages: [visiblePage, queuedHiddenPage] }), { status: 200 }),
        );
      }
      if (url === "/api/v1/pages/page-1") {
        return Promise.resolve(
          new Response(
            JSON.stringify({
              page_id: "page-1",
              page_number: 1,
              review_status: "pending",
              source_content: { titles: ["公开合成页一"], body: [], speaker_notes: [] },
            }),
            { status: 200 },
          ),
        );
      }
      if (url === "/api/v1/jobs/job-queued") return jobResponse;
      throw new Error(`未覆盖的请求：${url}`);
    });

    render(<App />);
    await screen.findByRole("heading", { name: "逐页策展" });
    const allFilter = screen.getByRole("button", { name: "全部" });
    await userEvent.click(allFilter);
    await userEvent.click(
      await screen.findByRole("button", { name: "第 2 页，隐藏页 · 未处理，已排队" }),
    );
    allFilter.focus();

    resolveJob?.(
      new Response(
        JSON.stringify({
          job_id: "job-queued",
          kind: "page.enable",
          status: "queued",
          attempts: 0,
          next_retry_at: null,
          progress: { phase: "queued", completed_pages: 0, total_pages: 1 },
          error: null,
        }),
        { status: 200 },
      ),
    );
    expect(await screen.findByText("任务已排队，等待处理。")).toBeInTheDocument();
    expect(allFilter).toHaveFocus();
  });
});
