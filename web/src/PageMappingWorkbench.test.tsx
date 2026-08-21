import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { App } from "./App";

const bootstrap = {
  actor: { actor_id: "operator-li", display_name: "操作者 operator-li" },
  runways: [
    { id: "pending", label: "待处理", documents: [] },
    { id: "processing", label: "处理中", documents: [] },
    { id: "curatable", label: "可策展", documents: [] },
  ],
};

const candidateA = {
  page_id: "page-history-a",
  chunk_id: "chunk-history-a",
  version_id: "version-old",
  page_number: 4,
  slide_id: 260,
  review_status: "approved",
  fingerprint_relation: "same",
  adjacent_confirmed: {
    before: { source_page_number: 3, page_id: "page-before" },
    after: { source_page_number: 5, page_id: "page-after" },
  },
  relative_order: { source_page_number: 4, candidate_page_number: 4, delta: 0 },
  occupied_by_case_id: null,
  standard_render: { url: "/renders/history-a.png" },
};

const baseWorkspace = {
  document_id: "document-1",
  version_id: "version-new",
  source_filename: "季度复盘-v2.pptx",
  status: "awaiting_mapping",
  revision: 0,
  remaining_cases: 2,
  current_version: { version_id: "version-old", still_serving: true },
  can_confirm: false,
  confirmed_at: null,
  confirmed_by: null,
  impact_summary: {
    reused_unchanged: 0,
    reused_changed: 0,
    created_new: 0,
    soft_deleted: 0,
    unresolved: 2,
    save_conflicts: 0,
    evidence_errors: 0,
  },
  cases: [
    {
      case_id: "case-1",
      kind: "duplicate_fingerprint",
      status: "unresolved",
      source_page: {
        page_number: 4,
        slide_id: 260,
        fingerprint: { version: 1, sha256: "abc123456789" },
        standard_render: { url: "/renders/source-4.png" },
      },
      candidates: [candidateA],
      decision: null,
      decided_by: null,
      decided_at: null,
    },
    {
      case_id: "case-2",
      kind: "multiple_candidates",
      status: "unresolved",
      source_page: {
        page_number: 9,
        slide_id: 267,
        fingerprint: { version: 1, sha256: "def123456789" },
        standard_render: { url: "/renders/source-9.png" },
      },
      candidates: [
        {
          ...candidateA,
          page_id: "page-history-b",
          chunk_id: "chunk-history-b",
          page_number: 8,
          slide_id: 267,
          fingerprint_relation: "changed",
          standard_render: { url: "/renders/history-b.png" },
        },
      ],
      decision: null,
      decided_by: null,
      decided_at: null,
    },
  ],
};

const readyToConfirmWorkspace = {
  ...baseWorkspace,
  revision: 2,
  remaining_cases: 0,
  can_confirm: true,
  cases: [
    {
      ...baseWorkspace.cases[0],
      status: "saved",
      decision: { kind: "reuse", page_id: "page-history-a" },
      decided_by: "operator-li",
    },
    {
      ...baseWorkspace.cases[1],
      status: "saved",
      decision: { kind: "new", page_id: null },
      decided_by: "operator-li",
    },
  ],
  impact_summary: {
    ...baseWorkspace.impact_summary,
    reused_unchanged: 1,
    created_new: 1,
    unresolved: 0,
  },
};

beforeEach(() => {
  window.history.replaceState(
    null,
    "",
    "/documents/document-1/versions/version-new/page-mapping",
  );
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("页对应工作面", () => {
  it("并置新旧证据，保存决定后前进且最终确认保持独立", async () => {
    const savedWorkspace = {
      ...baseWorkspace,
      revision: 1,
      remaining_cases: 1,
      cases: [
        {
          ...baseWorkspace.cases[0],
          status: "saved",
          decision: { kind: "reuse", page_id: "page-history-a" },
          decided_by: "operator-li",
        },
        baseWorkspace.cases[1],
      ],
      impact_summary: {
        ...baseWorkspace.impact_summary,
        reused_unchanged: 1,
        unresolved: 1,
      },
    };
    const completeWorkspace = {
      ...savedWorkspace,
      revision: 2,
      remaining_cases: 0,
      can_confirm: true,
      cases: [
        savedWorkspace.cases[0],
        {
          ...baseWorkspace.cases[1],
          status: "saved",
          decision: { kind: "new", page_id: null },
          decided_by: "operator-li",
        },
      ],
      impact_summary: {
        ...baseWorkspace.impact_summary,
        reused_unchanged: 1,
        created_new: 1,
        soft_deleted: 1,
        unresolved: 0,
      },
    };
    let putCount = 0;
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
      const url = String(input);
      if (url === "/api/v1/app/bootstrap") {
        return Promise.resolve(new Response(JSON.stringify(bootstrap), { status: 200 }));
      }
      if (url.endsWith("/page-mapping") && (!init?.method || init.method === "GET")) {
        return Promise.resolve(
          new Response(JSON.stringify(baseWorkspace), {
            status: 200,
            headers: { ETag: '"mapping-version-new-0"' },
          }),
        );
      }
      if (url.includes("/page-mapping/cases/") && init?.method === "PUT") {
        putCount += 1;
        expect((init.headers as Record<string, string>)["If-Match"]).toBe(
          putCount === 1 ? '"mapping-version-new-0"' : '"mapping-version-new-1"',
        );
        return Promise.resolve(
          new Response(JSON.stringify(putCount === 1 ? savedWorkspace : completeWorkspace), {
            status: 200,
            headers: { ETag: `"mapping-version-new-${putCount}"` },
          }),
        );
      }
      if (url.endsWith("/page-mapping/confirm") && init?.method === "POST") {
        return Promise.resolve(
          new Response(
            JSON.stringify({
              document_id: "document-1",
              version_id: "version-new",
              status: "ready",
              summary: completeWorkspace.impact_summary,
            }),
            { status: 200, headers: { ETag: '"mapping-version-new-3"' } },
          ),
        );
      }
      throw new Error(`未覆盖的请求：${url}`);
    });

    render(<App />);

    expect(await screen.findByRole("heading", { name: "页对应" })).toBeInTheDocument();
    expect(screen.getByText("季度复盘-v2.pptx")).toBeInTheDocument();
    expect(screen.getByText("旧版本仍在服务")).toBeInTheDocument();
    expect(screen.getByText("剩余 2 项")).toBeInTheDocument();
    expect(screen.getByAltText("新版本第 4 页标准页渲染结果")).toHaveAttribute(
      "src",
      "/renders/source-4.png",
    );
    expect(screen.getByAltText("历史候选第 4 页标准页渲染结果")).toHaveAttribute(
      "src",
      "/renders/history-a.png",
    );
    expect(screen.getByText("SlideID 260")).toBeInTheDocument();
    expect(screen.getAllByText("前邻第 3 页 · 后邻第 5 页")).toHaveLength(2);

    await userEvent.click(
      screen.getByRole("radio", { name: /沿用历史页 page-history-a/ }),
    );
    expect(screen.getByText(/保留 page_id 与 chunk_id/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "保存决定并查看下一项" }));
    expect(await screen.findByText("新版本源页 9")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("radio", { name: "创建新页" }));
    expect(screen.getByText(/不会继承历史审核/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "保存决定并查看下一项" }));
    expect(await screen.findByRole("button", { name: "确认全部对应并启用版本" })).toBeEnabled();

    await userEvent.click(screen.getByRole("button", { name: "确认全部对应并启用版本" }));
    const dialog = screen.getByRole("dialog", { name: "确认全部对应并启用版本" });
    expect(dialog).toHaveTextContent(/沿用且内容未变\s*1 页/);
    expect(dialog).toHaveTextContent(/创建新身份\s*1 页/);
    expect(dialog).toHaveTextContent(/缺席并软删除\s*1 页/);
    expect(dialog).toHaveTextContent("冻结后不能直接修改");
    await userEvent.click(screen.getByRole("button", { name: "确认并启用" }));
    expect(await screen.findByText("新版本已启用")).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalled();
  });

  it("遇到 412 时保留未保存选择并并排说明服务器决定", async () => {
    const serverWorkspace = {
      ...readyToConfirmWorkspace,
      revision: 1,
      cases: [
        {
          ...baseWorkspace.cases[0],
          status: "saved",
          decision: { kind: "reuse", page_id: "page-history-a" },
          decided_by: "operator-wang",
        },
        readyToConfirmWorkspace.cases[1],
      ],
    };
    let mappingReads = 0;
    vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
      const url = String(input);
      if (url === "/api/v1/app/bootstrap") {
        return Promise.resolve(new Response(JSON.stringify(bootstrap), { status: 200 }));
      }
      if (url.endsWith("/page-mapping") && (!init?.method || init.method === "GET")) {
        mappingReads += 1;
        return Promise.resolve(
          new Response(JSON.stringify(mappingReads === 1 ? baseWorkspace : serverWorkspace), {
            status: 200,
            headers: { ETag: `"mapping-version-new-${mappingReads - 1}"` },
          }),
        );
      }
      if (url.includes("/page-mapping/cases/") && init?.method === "PUT") {
        return Promise.resolve(
          new Response(
            JSON.stringify({
              error: {
                code: "mapping_precondition_failed",
                message: "页对应决定已被其他会话更新，请比较后重新确认。",
              },
            }),
            { status: 412, headers: { ETag: '"mapping-version-new-1"' } },
          ),
        );
      }
      throw new Error(`未覆盖的请求：${url}`);
    });

    render(<App />);
    await screen.findByRole("heading", { name: "页对应" });
    await userEvent.click(screen.getByRole("radio", { name: "创建新页" }));
    await userEvent.click(screen.getByRole("button", { name: "保存决定并查看下一项" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("其他会话更新了这一工作面");
    expect(screen.getByText("服务器当前决定")).toBeInTheDocument();
    expect(screen.getByText("沿用 page-history-a")).toBeInTheDocument();
    expect(screen.getByText("你的未保存选择")).toBeInTheDocument();
    expect(screen.getByText("创建新页", { selector: ".mapping-conflict-choice" })).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "创建新页" })).toBeChecked();
    expect(screen.getByRole("button", { name: "确认全部对应并启用版本" })).toBeDisabled();
    await waitFor(() => expect(mappingReads).toBe(2));
  });

  it("有本地草稿时阻止最终确认和 R 键刷新", async () => {
    let mappingReads = 0;
    vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
      const url = String(input);
      if (url === "/api/v1/app/bootstrap") {
        return Promise.resolve(new Response(JSON.stringify(bootstrap), { status: 200 }));
      }
      if (url.endsWith("/page-mapping")) {
        mappingReads += 1;
        return Promise.resolve(
          new Response(JSON.stringify(readyToConfirmWorkspace), {
            status: 200,
            headers: { ETag: '"mapping-version-new-2"' },
          }),
        );
      }
      throw new Error(`未覆盖的请求：${url}`);
    });

    render(<App />);
    await screen.findByRole("heading", { name: "页对应" });
    const newPage = screen.getByRole("radio", { name: "创建新页" });
    await userEvent.click(newPage);

    expect(screen.getByRole("button", { name: "确认全部对应并启用版本" })).toBeDisabled();
    newPage.blur();
    await userEvent.keyboard("r");
    expect(mappingReads).toBe(1);
    expect(screen.getByText("存在未保存选择，请先保存或切换后再刷新。")).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "创建新页" })).toBeChecked();
  });

  it("证据失败时提供原位重试并关闭最终门禁", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
      const url = String(input);
      if (url === "/api/v1/app/bootstrap") {
        return Promise.resolve(new Response(JSON.stringify(bootstrap), { status: 200 }));
      }
      if (url.endsWith("/page-mapping")) {
        return Promise.resolve(
          new Response(JSON.stringify(readyToConfirmWorkspace), {
            status: 200,
            headers: { ETag: '"mapping-version-new-2"' },
          }),
        );
      }
      throw new Error(`未覆盖的请求：${url}`);
    });

    render(<App />);
    const source = await screen.findByAltText("新版本第 4 页标准页渲染结果");
    fireEvent.error(source);

    expect(screen.getByRole("alert")).toHaveTextContent("标准页渲染加载失败");
    expect(screen.getByRole("button", { name: "确认全部对应并启用版本" })).toBeDisabled();
    await userEvent.click(screen.getByRole("button", { name: "重新加载此证据" }));
    expect(screen.getByAltText("新版本第 4 页标准页渲染结果")).toHaveAttribute(
      "src",
      "/renders/source-4.png?evidence_retry=1",
    );
  });

  it("可从被占用候选跳到占用项且不丢失草稿", async () => {
    const occupiedWorkspace = {
      ...baseWorkspace,
      cases: [
        {
          ...baseWorkspace.cases[0],
          candidates: [{ ...candidateA, occupied_by_case_id: "case-2" }],
        },
        baseWorkspace.cases[1],
      ],
    };
    vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
      const url = String(input);
      if (url === "/api/v1/app/bootstrap") {
        return Promise.resolve(new Response(JSON.stringify(bootstrap), { status: 200 }));
      }
      if (url.endsWith("/page-mapping")) {
        return Promise.resolve(
          new Response(JSON.stringify(occupiedWorkspace), {
            status: 200,
            headers: { ETag: '"mapping-version-new-0"' },
          }),
        );
      }
      throw new Error(`未覆盖的请求：${url}`);
    });

    render(<App />);
    await screen.findByRole("heading", { name: "页对应" });
    await userEvent.click(screen.getByRole("radio", { name: "创建新页" }));
    await userEvent.click(screen.getByRole("button", { name: "前往占用项" }));
    expect(screen.getByText("新版本源页 9")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /重复指纹/ }));
    expect(screen.getByRole("radio", { name: "创建新页" })).toBeChecked();
  });

  it("最终确认对话框圈定焦点并在关闭后恢复触发点", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
      const url = String(input);
      if (url === "/api/v1/app/bootstrap") {
        return Promise.resolve(new Response(JSON.stringify(bootstrap), { status: 200 }));
      }
      if (url.endsWith("/page-mapping")) {
        return Promise.resolve(
          new Response(JSON.stringify(readyToConfirmWorkspace), {
            status: 200,
            headers: { ETag: '"mapping-version-new-2"' },
          }),
        );
      }
      throw new Error(`未覆盖的请求：${url}`);
    });

    render(<App />);
    const trigger = await screen.findByRole("button", {
      name: "确认全部对应并启用版本",
    });
    await userEvent.click(trigger);
    const confirm = screen.getByRole("button", { name: "确认并启用" });
    const back = screen.getByRole("button", { name: "返回检查" });
    expect(confirm).toHaveFocus();
    await userEvent.tab();
    expect(back).toHaveFocus();
    await userEvent.tab({ shift: true });
    expect(confirm).toHaveFocus();
    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
  });
});
