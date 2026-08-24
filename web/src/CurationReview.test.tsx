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

const pendingPage = {
  page_id: "page-1",
  chunk_id: "chunk-1",
  document_id: "document-1",
  version_id: "version-1",
  page_number: 1,
  review_status: "pending",
  title: "公开来源标题",
  hidden: false,
  enabled: true,
  source_reference: {
    slide_id: 256,
    relationship_id: "rId7",
    part: "ppt/slides/slide1.xml",
  },
  enablement: null,
};

const secondPage = {
  ...pendingPage,
  page_id: "page-2",
  chunk_id: "chunk-2",
  page_number: 2,
  title: "第二页来源标题",
};

const originalSource = {
  titles: ["公开来源标题"],
  body: ["公开来源正文。"],
  tables: [],
  images: [],
  speaker_notes: ["只读演讲者备注。"],
};

function curationState(
  snapshot: null | {
    snapshot_id: string;
    source_content: typeof originalSource;
    source_confirmation: null | { actor_id: string; confirmed_at: string };
    source_review: null | { actor_id: string; completed_at: string };
  },
) {
  const confirmed = snapshot?.source_confirmation ?? null;
  const reviewed = snapshot?.source_review ?? null;
  const blockers = snapshot === null
    ? [
        { code: "source_unsaved", message: "文字修改尚未保存。" },
        { code: "source_unconfirmed", message: "文字来源尚未确认。" },
        { code: "source_review_incomplete", message: "来源审核尚未完成。" },
      ]
    : [
        ...(confirmed ? [] : [{ code: "source_unconfirmed", message: "文字来源尚未确认。" }]),
        ...(reviewed ? [] : [{ code: "source_review_incomplete", message: "来源审核尚未完成。" }]),
      ];
  return {
    current_snapshot: snapshot,
    image_sources: { total: 0, unresolved: 0 },
    chunk_body: { nonempty: true },
    blockers,
    can_confirm_source: snapshot !== null,
    can_complete_source_review: confirmed !== null,
    can_approve: snapshot !== null && blockers.length === 0,
  };
}

beforeEach(() => {
  window.history.replaceState(null, "", "/curation");
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("来源文字审核工作台", () => {
  it("以显式保存、确认和来源审核完成无 overview、零框选批准路径", async () => {
    let queueApproved = false;
    let snapshot: ReturnType<typeof curationState>["current_snapshot"] = null;
    vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
      const url = String(input);
      if (url === "/api/v1/app/bootstrap") {
        return Promise.resolve(new Response(JSON.stringify(bootstrap), { status: 200 }));
      }
      if (url.endsWith("/api/v1/curation/pages?review_status=pending")) {
        return Promise.resolve(
          new Response(JSON.stringify({ pages: queueApproved ? [secondPage] : [pendingPage] }), {
            status: 200,
          }),
        );
      }
      if (url.endsWith("/api/v1/curation/pages?review_status=all")) {
        return Promise.resolve(
          new Response(
            JSON.stringify({
              pages: queueApproved
                ? [{ ...pendingPage, review_status: "approved" }, secondPage]
                : [pendingPage, secondPage],
            }),
            { status: 200 },
          ),
        );
      }
      if ((url === "/api/v1/pages/page-1" || url === "/api/v1/pages/page-2") && !init?.method) {
        const source = url.endsWith("page-2")
          ? { ...originalSource, titles: ["第二页来源标题"] }
          : originalSource;
        return Promise.resolve(
          new Response(
            JSON.stringify({
              page_id: url.endsWith("page-2") ? "page-2" : "page-1",
              page_number: url.endsWith("page-2") ? 2 : 1,
              review_status: "pending",
              source_content: source,
              curation: curationState(url.endsWith("page-2") ? null : snapshot),
            }),
            { status: 200 },
          ),
        );
      }
      if (url.endsWith("/curation/snapshots") && init?.method === "POST") {
        const body = JSON.parse(String(init.body));
        snapshot = {
          snapshot_id: "snapshot-1",
          source_content: { ...originalSource, titles: body.titles, body: body.body },
          source_confirmation: null,
          source_review: null,
        };
        return Promise.resolve(
          new Response(JSON.stringify({ curation: curationState(snapshot) }), { status: 201 }),
        );
      }
      if (url.endsWith("/curation/source-confirmation") && init?.method === "POST") {
        snapshot = {
          ...snapshot!,
          source_confirmation: {
            actor_id: "operator-zhang",
            confirmed_at: "2026-08-24T18:00:00+00:00",
          },
        };
        return Promise.resolve(
          new Response(JSON.stringify({ curation: curationState(snapshot) }), { status: 200 }),
        );
      }
      if (url.endsWith("/curation/source-review") && init?.method === "POST") {
        snapshot = {
          ...snapshot!,
          source_review: {
            actor_id: "operator-zhang",
            completed_at: "2026-08-24T18:01:00+00:00",
          },
        };
        return Promise.resolve(
          new Response(JSON.stringify({ curation: curationState(snapshot) }), { status: 200 }),
        );
      }
      if (url.endsWith("/api/v1/pages/page-1/approve") && init?.method === "POST") {
        queueApproved = true;
        return Promise.resolve(
          new Response(
            JSON.stringify({
              review: {
                status: "approved",
                reviewed_by: "operator-zhang",
                reviewed_at: "2026-08-24T18:02:00+00:00",
                source_version_id: "version-1",
                snapshot_id: "snapshot-1",
              },
              chunk_body: "人工修订标题\n\n人工修订正文。",
            }),
            { status: 200 },
          ),
        );
      }
      throw new Error(`未覆盖的请求：${url}`);
    });

    render(<App />);

    expect(await screen.findByRole("heading", { name: "逐页策展" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "标准页渲染" })).toBeInTheDocument();
    expect(await screen.findByRole("heading", { name: "文字来源" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "全部" }));
    expect(await screen.findByRole("button", {
      name: "第 2 页，第二页来源标题，待处理",
    })).toBeInTheDocument();
    expect(screen.getByText("只读演讲者备注。")).toBeInTheDocument();
    expect(screen.getByText("文字修改尚未保存。")).toBeInTheDocument();

    const title = screen.getByRole("textbox", { name: "标题来源 1 当前编辑值" });
    const body = screen.getByRole("textbox", { name: "正文来源 1 当前编辑值" });
    await userEvent.clear(title);
    await userEvent.type(title, "人工修订标题");
    await userEvent.clear(body);
    await userEvent.type(body, "人工修订正文。");
    expect(screen.getByText("已修改，原确认失效")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "保存修改" }));
    const confirm = await screen.findByRole("button", { name: "确认文字来源" });
    await waitFor(() => expect(confirm).toHaveFocus());
    await userEvent.click(confirm);
    const review = await screen.findByRole("button", { name: "完成来源审核" });
    await waitFor(() => expect(review).toHaveFocus());
    await userEvent.click(review);
    expect(await screen.findByText("来源完整 · 无需截图")).toBeInTheDocument();

    expect(screen.queryByText(/overview/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/VLM/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/自动批准|自动补全/)).not.toBeInTheDocument();
    expect(screen.queryByText(/视觉对象|框选/)).not.toBeInTheDocument();

    await userEvent.click(
      screen.getByRole("button", { name: "批准并转到下一待处理页" }),
    );
    expect(await screen.findByText("上一页已批准。已转到下一待处理页。")).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "标题来源 1 当前编辑值" }))
      .toHaveValue("第二页来源标题");
  });

  it("切换页面前明确询问是否放弃未保存的来源修改", async () => {
    const confirm = vi.spyOn(window, "confirm")
      .mockReturnValueOnce(false)
      .mockReturnValueOnce(true);
    vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
      const url = String(input);
      if (url === "/api/v1/app/bootstrap") {
        return Promise.resolve(new Response(JSON.stringify(bootstrap), { status: 200 }));
      }
      if (url.endsWith("review_status=pending")) {
        return Promise.resolve(
          new Response(JSON.stringify({ pages: [pendingPage, secondPage] }), { status: 200 }),
        );
      }
      if (url === "/api/v1/pages/page-1" || url === "/api/v1/pages/page-2") {
        const second = url.endsWith("page-2");
        const source = {
          ...originalSource,
          titles: [second ? "第二页来源标题" : "公开来源标题"],
        };
        return Promise.resolve(
          new Response(
            JSON.stringify({
              page_id: second ? "page-2" : "page-1",
              page_number: second ? 2 : 1,
              review_status: "pending",
              source_content: source,
              curation: curationState(null),
            }),
            { status: 200 },
          ),
        );
      }
      throw new Error(`未覆盖的请求：${url}`);
    });

    render(<App />);
    const title = await screen.findByRole("textbox", { name: "标题来源 1 当前编辑值" });
    await userEvent.type(title, "（本地修改）");
    const secondRow = screen.getByRole("button", {
      name: "第 2 页，第二页来源标题，待处理",
    });

    await userEvent.click(secondRow);
    expect(confirm).toHaveBeenCalledTimes(1);
    expect(title).toHaveValue("公开来源标题（本地修改）");

    await userEvent.click(secondRow);
    expect(confirm).toHaveBeenCalledTimes(2);
    await waitFor(() => expect(
      screen.getByRole("textbox", { name: "标题来源 1 当前编辑值" }),
    ).toHaveValue("第二页来源标题"));
  });
});
