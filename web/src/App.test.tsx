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

  it("在文档行持续显示渲染风险汇总并只提供策展入口", async () => {
    const withWarnings = {
      ...bootstrap,
      runways: bootstrap.runways.map((runway) =>
        runway.id === "curatable"
          ? {
              ...runway,
              documents: [
                {
                  document_id: "document-1",
                  version_id: "version-1",
                  title: "公开风险文档.pptx",
                  status: "ready",
                  status_label: "可策展",
                  rendering_warnings: {
                    total: 5,
                    pages: 3,
                    unconfirmed: 5,
                    unconfirmed_pages: 3,
                  },
                  action: {
                    label: "进入策展",
                    href: "/curation?filter=rendering-warnings",
                  },
                },
              ],
            }
          : runway,
      ),
    };
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify(withWarnings), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );

    render(<App />);

    expect(await screen.findByText("公开风险文档.pptx")).toBeInTheDocument();
    expect(screen.getByText("渲染风险 · 3 页 / 5 条未确认")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "进入策展" })).toHaveAttribute(
      "href",
      "/curation?filter=rendering-warnings",
    );
    expect(screen.queryByRole("button", { name: /确认.*警告/ })).not.toBeInTheDocument();
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

describe("渲染警告工作流", () => {
  it("从深链进入警告模式、移动焦点并支持单条和整版确认", async () => {
    window.history.replaceState(
      null,
      "",
      "/curation?filter=rendering-warnings&warning=warning-font",
    );
    const warningSummary = {
      total: 2,
      pages: 1,
      unconfirmed: 2,
      unconfirmed_pages: 1,
    };
    const warnings = [
      {
        warning_id: "warning-font",
        page_number: 1,
        code: "missing_font",
        details: {
          requested_font: "PPTExtract Missing Contract Font",
          replacement_font: "Noto Sans",
        },
        render_config_version: "render-config-1",
        observed_at: "2026-08-22T10:00:00+00:00",
        status: "unconfirmed",
        confirmed_by: null,
        confirmed_at: null,
      },
      {
        warning_id: "warning-animation",
        page_number: 1,
        code: "animation_flattened",
        details: { timeline_count: 1 },
        render_config_version: "render-config-1",
        observed_at: "2026-08-22T10:00:00+00:00",
        status: "unconfirmed",
        confirmed_by: null,
        confirmed_at: null,
      },
    ];
    const warningPage = {
      ...visiblePage,
      rendering_warnings: warningSummary,
      version_rendering_warnings: warningSummary,
    };
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
      const url = String(input);
      if (url === "/api/v1/app/bootstrap") {
        return Promise.resolve(new Response(JSON.stringify(bootstrap), { status: 200 }));
      }
      if (url.includes("/api/v1/curation/pages")) {
        return Promise.resolve(
          new Response(JSON.stringify({ pages: [warningPage] }), { status: 200 }),
        );
      }
      if (
        url ===
        "/api/v1/documents/document-1/versions/version-1/rendering-warnings"
      ) {
        return Promise.resolve(
          new Response(
            JSON.stringify({
              document_id: "document-1",
              version_id: "version-1",
              render_config_version: "render-config-1",
              summary: warningSummary,
              warnings,
            }),
            { status: 200 },
          ),
        );
      }
      if (url.endsWith("/warning-font/confirm") && init?.method === "POST") {
        return Promise.resolve(
          new Response(
            JSON.stringify({
              ...warnings[0],
              status: "confirmed",
              confirmed_by: "operator-zhang",
              confirmed_at: "2026-08-22T10:05:00+00:00",
            }),
            { status: 200 },
          ),
        );
      }
      if (url.endsWith("/rendering-warnings/confirm-all") && init?.method === "POST") {
        return Promise.resolve(
          new Response(
            JSON.stringify({
              confirmed_count: 1,
              summary: { ...warningSummary, unconfirmed: 0, unconfirmed_pages: 0 },
              render_config_version: "render-config-1",
              warnings: warnings.map((warning) => ({
                ...warning,
                status: "confirmed",
                confirmed_by:
                  warning.warning_id === "warning-font" ? "operator-zhang" : "operator-li",
                confirmed_at: "2026-08-22T10:06:00+00:00",
              })),
            }),
            { status: 200 },
          ),
        );
      }
      throw new Error(`未覆盖的请求：${url}`);
    });

    render(<App />);

    const heading = await screen.findByRole("heading", { name: "渲染警告" });
    await waitFor(() => expect(heading).toHaveFocus());
    expect(screen.getByText("字体缺失或替代")).toBeInTheDocument();
    expect(screen.getByText(/PPTExtract Missing Contract Font → Noto Sans/)).toBeInTheDocument();
    expect(screen.getByText("动画时间线已静态扁平化")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "确认字体缺失或替代警告" })).toBeEnabled();
    expect(screen.getByRole("button", { name: /渲染警告 2\/2 未确认/ })).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "确认字体缺失或替代警告" }));
    expect(await screen.findByText(/operator-zhang.*已确认/)).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("已确认 1 条渲染警告");

    await userEvent.click(screen.getByRole("button", { name: "确认当前版本全部警告" }));
    const dialog = screen.getByRole("dialog", { name: "确认当前版本全部警告" });
    expect(dialog).toHaveTextContent("1 页 / 1 条未确认");
    expect(dialog).toHaveTextContent("render-config-1");
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "确认全部 1 条警告" })).toHaveFocus(),
    );
    await userEvent.tab();
    expect(screen.getByRole("button", { name: "返回检查" })).toHaveFocus();
    await userEvent.tab({ shift: true });
    expect(screen.getByRole("button", { name: "确认全部 1 条警告" })).toHaveFocus();
    await userEvent.click(screen.getByRole("button", { name: "确认全部 1 条警告" }));
    expect(await screen.findByText("当前版本 2 条渲染警告均已确认")).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalled();
  });

  it("在发布表面独立显示硬阻塞并提供警告深链", async () => {
    window.history.replaceState(null, "", "/publication");
    vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
      const url = String(input);
      if (url === "/api/v1/app/bootstrap") {
        return Promise.resolve(new Response(JSON.stringify(bootstrap), { status: 200 }));
      }
      if (url === "/api/v1/publications") {
        return Promise.resolve(
          new Response(
            JSON.stringify({
              preflight: {
                can_publish: false,
                summary: { total: 5, pages: 3, unconfirmed: 5, unconfirmed_pages: 3 },
                stale_render_versions: 0,
                href: "/curation?filter=rendering-warnings&document=doc-1&version=version-1&page=2&warning=warning-1",
              },
              current: null,
              candidate: null,
              task: null,
            }),
            { status: 200 },
          ),
        );
      }
      throw new Error(`未覆盖的请求：${url}`);
    });

    render(<App />);

    expect(await screen.findByText("发布被阻止")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "发布前置校验" })).toBeInTheDocument();
    expect(screen.getByText("3 页 / 5 条未确认")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "创建发布候选" })).toBeDisabled();
    expect(screen.getByRole("link", { name: "前往确认渲染警告" })).toHaveAttribute(
      "href",
      "/curation?filter=rendering-warnings&document=doc-1&version=version-1&page=2&warning=warning-1",
    );
  });

  it("发布确认遇到并发状态变化后重新加载并恢复硬阻塞", async () => {
    window.history.replaceState(null, "", "/publication");
    let preflightReads = 0;
    vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
      const url = String(input);
      if (url === "/api/v1/app/bootstrap") {
        return Promise.resolve(new Response(JSON.stringify(bootstrap), { status: 200 }));
      }
      if (url === "/api/v1/publications/candidates" && init?.method === "POST") {
        return Promise.resolve(
          new Response(
            JSON.stringify({
              error: { code: "rendering_warnings_unconfirmed", message: "出现新的未确认警告。" },
            }),
            { status: 409 },
          ),
        );
      }
      if (url === "/api/v1/publications") {
        preflightReads += 1;
        const blocked = preflightReads > 1;
        return Promise.resolve(
          new Response(
            JSON.stringify({
              preflight: {
                can_publish: !blocked,
                stale_render_versions: 0,
                href: blocked ? "/curation?filter=rendering-warnings" : null,
                summary: blocked
                  ? { total: 1, pages: 1, unconfirmed: 1, unconfirmed_pages: 1 }
                  : { total: 0, pages: 0, unconfirmed: 0, unconfirmed_pages: 0 },
              },
              current: null,
              candidate: null,
              task: null,
            }),
          ),
        );
      }
      throw new Error(`未覆盖的请求：${url}`);
    });

    render(<App />);
    const create = await screen.findByRole("button", { name: "创建发布候选" });
    expect(create).toBeEnabled();
    await userEvent.click(create);

    expect(await screen.findByText("发布被阻止")).toBeInTheDocument();
    expect(screen.getByText("1 页 / 1 条未确认")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "创建发布候选" })).toBeDisabled();
  });

  it("从文档级渲染警告深链进入对应文档，而不是全局第一项", async () => {
    window.history.replaceState(
      null,
      "",
      "/curation?filter=rendering-warnings&document=document-2&version=version-2",
    );
    const summary = { total: 1, pages: 1, unconfirmed: 1, unconfirmed_pages: 1 };
    const first = {
      ...visiblePage,
      rendering_warnings: summary,
      version_rendering_warnings: summary,
    };
    const second = {
      ...visiblePage,
      document_id: "document-2",
      version_id: "version-2",
      page_id: "page-2",
      page_number: 2,
      title: "文档乙风险页",
      rendering_warnings: summary,
      version_rendering_warnings: summary,
    };
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
      const url = String(input);
      if (url === "/api/v1/app/bootstrap") {
        return Promise.resolve(new Response(JSON.stringify(bootstrap), { status: 200 }));
      }
      if (url.includes("/api/v1/curation/pages")) {
        return Promise.resolve(new Response(JSON.stringify({ pages: [first, second] })));
      }
      if (url === "/api/v1/documents/document-2/versions/version-2/rendering-warnings") {
        return Promise.resolve(
          new Response(
            JSON.stringify({
              document_id: "document-2",
              version_id: "version-2",
              render_config_version: "render-config-1",
              summary,
              warnings: [
                {
                  warning_id: "warning-document-2",
                  page_number: 2,
                  code: "animation_flattened",
                  details: { timeline_count: 1 },
                  render_config_version: "render-config-1",
                  observed_at: "2026-08-22T10:00:00+00:00",
                  status: "unconfirmed",
                  confirmed_by: null,
                  confirmed_at: null,
                },
              ],
            }),
          ),
        );
      }
      throw new Error(`未覆盖的请求：${url}`);
    });

    render(<App />);

    const evidence = await screen.findByAltText("第 2 页标准页渲染结果");
    expect(evidence).toHaveAttribute("src", "/api/v1/pages/page-2/render");
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/documents/document-2/versions/version-2/rendering-warnings",
      expect.any(Object),
    );
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
    expect(
      await screen.findByRole("textbox", { name: "正文来源 1 当前编辑值" }),
    ).toHaveValue("隐藏页真实正文");
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
