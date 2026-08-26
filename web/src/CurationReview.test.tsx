import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
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
    image_sources: { total: 0, unresolved: 0, items: [] },
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

    const saveEdit = screen.getAllByRole("button", { name: "保存修改" })
      .find((button) => !(button as HTMLButtonElement).disabled);
    expect(saveEdit).toBeDefined();
    await userEvent.click(saveEdit!);
    const confirm = await screen.findByRole("button", { name: "确认文字来源" });
    await waitFor(() => expect(confirm).toHaveFocus());
    await userEvent.click(confirm);
    const review = await screen.findByRole("button", { name: "完成来源审核" });
    await waitFor(() => expect(review).toHaveFocus());
    await userEvent.click(review);
    expect(await screen.findByText("等待来源完整性选择")).toBeInTheDocument();

    expect(screen.queryByText(/overview/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/VLM/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/自动批准|自动补全/)).not.toBeInTheDocument();
    expect(screen.queryByText(/自动候选框/)).not.toBeInTheDocument();
    await userEvent.click(
      screen.getByRole("button", { name: "来源完整，直接审核" }),
    );
    expect(await screen.findByText("来源完整 · 无需截图")).toBeInTheDocument();
    await waitFor(() => expect(
      screen.getByRole("button", { name: "批准并转到下一待处理页" }),
    ).toHaveFocus());

    await userEvent.click(
      screen.getByRole("button", { name: "批准并转到下一待处理页" }),
    );
    expect(await screen.findByText("上一页已批准。已转到下一待处理页。")).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "标题来源 1 当前编辑值" }))
      .toHaveValue("第二页来源标题");
  });

  it("来源有缺口时框选首个视觉对象、校验 summary 并返回批准动作", async () => {
    const reviewedSnapshot = {
      snapshot_id: "snapshot-reviewed",
      source_content: originalSource,
      source_confirmation: {
        actor_id: "operator-zhang",
        confirmed_at: "2026-08-24T18:00:00+00:00",
      },
      source_review: {
        actor_id: "operator-zhang",
        completed_at: "2026-08-24T18:01:00+00:00",
      },
    };
    const capturedSnapshot = {
      ...reviewedSnapshot,
      snapshot_id: "snapshot-captured",
    };
    let captured = false;
    let failCapturedDetailOnce = true;
    let savedBody: Record<string, unknown> | null = null;
    const detail = () => ({
      page_id: "page-1",
      page_number: 1,
      review_status: "pending",
      source_content: originalSource,
      curation: curationState(captured ? capturedSnapshot : reviewedSnapshot),
      annotation: captured
        ? {
            snapshot_id: "snapshot-captured",
            visuals: [
              {
                visual_ref: "opaque-visual-ref",
                position: 0,
                source_kind: "capture",
                disposition: "included",
                summary: "折线展示公开指标随月份稳步上升。",
                visual_type: "chart",
                bounds: { left: 0.1, top: 0.2, width: 0.4, height: 0.4 },
                source_visual_ref: null,
                confirmed: true,
                asset: {
                  sha256: "a".repeat(64),
                  media_type: "image/png",
                  size_bytes: 512,
                  width_px: 336,
                  height_px: 196,
                  byte_contract: "standard_render_crop",
                },
              },
            ],
          }
        : { snapshot_id: "snapshot-reviewed", visuals: [] },
      standard_render: {
        sha256: "b".repeat(64),
        media_type: "image/png",
        dpi: 144,
        width_px: 1600,
        height_px: 900,
        url: "/api/v1/pages/page-1/render",
      },
    });
    vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
      const url = String(input);
      if (url === "/api/v1/app/bootstrap") {
        return Promise.resolve(new Response(JSON.stringify(bootstrap), { status: 200 }));
      }
      if (url.endsWith("/api/v1/curation/pages?review_status=pending")) {
        return Promise.resolve(
          new Response(JSON.stringify({ pages: [pendingPage] }), { status: 200 }),
        );
      }
      if (url === "/api/v1/pages/page-1" && !init?.method) {
        if (captured && failCapturedDetailOnce) {
          failCapturedDetailOnce = false;
          return Promise.resolve(
            new Response(JSON.stringify({ error: { message: "详情暂不可用" } }), {
              status: 503,
            }),
          );
        }
        return Promise.resolve(new Response(JSON.stringify(detail()), { status: 200 }));
      }
      if (url.endsWith("/curation/visuals") && init?.method === "POST") {
        savedBody = JSON.parse(String(init.body));
        captured = true;
        return Promise.resolve(
          new Response(
            JSON.stringify({ curation: curationState(capturedSnapshot) }),
            { status: 201 },
          ),
        );
      }
      throw new Error(`未覆盖的请求：${url}`);
    });

    render(<App />);
    const gapButton = await screen.findByRole("button", {
      name: "有缺口，在页面上框选",
    });
    expect(screen.queryByText("视觉对象 01")).not.toBeInTheDocument();
    await userEvent.click(gapButton);

    await userEvent.keyboard("{Escape}");
    const gapButtonAfterSelectionCancel = screen.getByRole("button", {
      name: "有缺口，在页面上框选",
    });
    await waitFor(() => expect(gapButtonAfterSelectionCancel).toHaveFocus());
    await userEvent.click(gapButtonAfterSelectionCancel);

    const image = screen.getByAltText("第 1 页标准页渲染结果");
    vi.spyOn(image, "getBoundingClientRect").mockReturnValue({
      left: 100,
      top: 100,
      right: 900,
      bottom: 550,
      width: 800,
      height: 450,
      x: 100,
      y: 100,
      toJSON: () => ({}),
    });
    fireEvent.pointerDown(image, { pointerId: 1, clientX: 180, clientY: 190 });
    fireEvent.pointerMove(image, { pointerId: 1, clientX: 500, clientY: 370 });
    fireEvent.pointerUp(image, { pointerId: 1, clientX: 500, clientY: 370 });

    let summary = await screen.findByRole("textbox", { name: "视觉对象 01 summary" });
    await waitFor(() => expect(summary).toHaveFocus());
    await userEvent.keyboard("{Escape}");
    const gapButtonAfterEditorCancel = screen.getByRole("button", {
      name: "有缺口，在页面上框选",
    });
    await waitFor(() => expect(gapButtonAfterEditorCancel).toHaveFocus());
    await userEvent.click(gapButtonAfterEditorCancel);
    fireEvent.pointerDown(image, { pointerId: 2, clientX: 180, clientY: 190 });
    fireEvent.pointerMove(image, { pointerId: 2, clientX: 500, clientY: 370 });
    fireEvent.pointerUp(image, { pointerId: 2, clientX: 500, clientY: 370 });

    summary = await screen.findByRole("textbox", { name: "视觉对象 01 summary" });
    await waitFor(() => expect(summary).toHaveFocus());
    await userEvent.click(screen.getByRole("button", { name: "保存并返回审核" }));
    expect(await screen.findByText("summary 不能为空，请写成可独立理解的结论。")).toBeInTheDocument();
    await waitFor(() => expect(summary).toHaveFocus());

    await userEvent.type(summary, "折线展示公开指标随月份稳步上升。");
    await userEvent.selectOptions(
      screen.getByRole("combobox", { name: "视觉对象 01 类型" }),
      "chart",
    );
    await userEvent.click(screen.getByRole("button", { name: "保存并返回审核" }));

    expect(await screen.findByText("折线展示公开指标随月份稳步上升。")).toBeInTheDocument();
    expect(screen.getByText("视觉对象 01 已保存；资产详情暂未刷新，可稍后刷新工作位。")).toBeInTheDocument();
    await waitFor(() => expect(
      screen.getByRole("button", { name: "批准并转到下一待处理页" }),
    ).toHaveFocus());
    expect(savedBody).toEqual({
      base_snapshot_id: "snapshot-reviewed",
      summary: "折线展示公开指标随月份稳步上升。",
      visual_type: "chart",
      bounds: { left: 0.1, top: 0.2, width: 0.4, height: 0.4 },
    });
    expect(screen.queryByText("opaque-visual-ref")).not.toBeInTheDocument();
  });

  it("以同一编号编辑、排序和确认删除多个人工截图视觉对象", async () => {
    const reviewedSnapshot = {
      snapshot_id: "snapshot-2",
      source_content: originalSource,
      source_confirmation: {
        actor_id: "operator-zhang",
        confirmed_at: "2026-08-24T18:00:00+00:00",
      },
      source_review: {
        actor_id: "operator-zhang",
        completed_at: "2026-08-24T18:01:00+00:00",
      },
    };
    const visual = (
      visualRef: string,
      position: number,
      summary: string,
      bounds: { left: number; top: number; width: number; height: number },
    ) => ({
      visual_ref: visualRef,
      position,
      source_kind: "capture",
      disposition: "included",
      summary,
      visual_type: "chart",
      bounds,
      source_visual_ref: null,
      confirmed: true,
    });
    let visuals = [
      visual("visual-a", 0, "第一幅公开趋势图。", {
        left: 0.08, top: 0.12, width: 0.32, height: 0.3,
      }),
      visual("visual-b", 1, "第二幅公开分布图。", {
        left: 0.5, top: 0.24, width: 0.34, height: 0.38,
      }),
    ];
    let snapshotId = "snapshot-2";
    let mutationCount = 0;
    let editedBody: Record<string, unknown> | null = null;
    vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
      const url = String(input);
      if (url === "/api/v1/app/bootstrap") {
        return Promise.resolve(new Response(JSON.stringify(bootstrap), { status: 200 }));
      }
      if (url.endsWith("/api/v1/curation/pages?review_status=pending")) {
        return Promise.resolve(
          new Response(JSON.stringify({ pages: [pendingPage] }), { status: 200 }),
        );
      }
      if (url === "/api/v1/pages/page-1" && !init?.method) {
        return Promise.resolve(new Response(JSON.stringify({
          page_id: "page-1",
          page_number: 1,
          review_status: "pending",
          source_content: originalSource,
          curation: curationState({ ...reviewedSnapshot, snapshot_id: snapshotId }),
          annotation: { snapshot_id: snapshotId, visuals },
          standard_render: {
            sha256: "b".repeat(64),
            media_type: "image/png",
            dpi: 144,
            width_px: 1600,
            height_px: 900,
            url: "/api/v1/pages/page-1/render",
          },
        }), { status: 200 }));
      }
      if (url.endsWith("/visual-b/move") && init?.method === "POST") {
        mutationCount += 1;
        snapshotId = `snapshot-move-${mutationCount}`;
        visuals = [
          { ...visuals[1], position: 0 },
          { ...visuals[0], position: 1 },
        ];
        return Promise.resolve(new Response(JSON.stringify({
          curation: curationState({ ...reviewedSnapshot, snapshot_id: snapshotId }),
          annotation: { snapshot_id: snapshotId, visuals },
        }), { status: 201 }));
      }
      if (url.endsWith("/visual-b") && init?.method === "PATCH") {
        editedBody = JSON.parse(String(init.body));
        mutationCount += 1;
        snapshotId = `snapshot-edit-${mutationCount}`;
        visuals = visuals.map((item) => item.visual_ref === "visual-b"
          ? {
              ...item,
              summary: String(editedBody?.summary),
              bounds: editedBody?.bounds as typeof item.bounds,
            }
          : item);
        return Promise.resolve(new Response(JSON.stringify({
          curation: curationState({ ...reviewedSnapshot, snapshot_id: snapshotId }),
          annotation: { snapshot_id: snapshotId, visuals },
        }), { status: 201 }));
      }
      if ((url.endsWith("/visual-b") || url.endsWith("/visual-a")) && init?.method === "DELETE") {
        const deletedRef = url.endsWith("/visual-b") ? "visual-b" : "visual-a";
        mutationCount += 1;
        snapshotId = `snapshot-delete-${mutationCount}`;
        visuals = visuals
          .filter((item) => item.visual_ref !== deletedRef)
          .map((item, position) => ({ ...item, position }));
        const nextCuration = curationState({ ...reviewedSnapshot, snapshot_id: snapshotId });
        if (visuals.length === 0) {
          nextCuration.blockers.push({
            code: "capture_required",
            message: "来源仍有缺口：请重新框选视觉对象，或明确改选来源完整。",
          });
          nextCuration.can_approve = false;
        }
        return Promise.resolve(new Response(JSON.stringify({
          curation: nextCuration,
          annotation: { snapshot_id: snapshotId, visuals },
        }), { status: 201 }));
      }
      throw new Error(`未覆盖的请求：${url} ${init?.method ?? "GET"}`);
    });

    render(<App />);
    const editSecond = await screen.findByRole("button", { name: "编辑视觉对象 02" });
    expect(screen.getByRole("button", { name: /视觉对象 01 框选范围/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /视觉对象 02 框选范围/ })).toBeInTheDocument();

    await userEvent.click(editSecond);
    const summary = await screen.findByRole("textbox", { name: "视觉对象 02 summary" });
    expect(summary).toHaveValue("第二幅公开分布图。");
    expect(screen.getByRole("button", { name: /视觉对象 01 框选范围/ })).toBeDisabled();
    expect(screen.getByRole("button", { name: /视觉对象 02 框选范围/ })).toHaveAttribute("tabindex", "-1");
    screen.getByRole("button", { name: /视觉对象 01 框选范围/ }).focus();
    await waitFor(() => expect(summary).toHaveFocus());
    await userEvent.clear(summary);
    await userEvent.type(summary, "未保存的本地修改。");
    await userEvent.click(screen.getByRole("button", { name: "放弃修改" }));
    await waitFor(() => expect(editSecond).toHaveFocus());
    expect(globalThis.fetch).not.toHaveBeenCalledWith(
      expect.stringContaining("/visual-b"),
      expect.objectContaining({ method: "PATCH" }),
    );

    await userEvent.click(editSecond);
    const restoredSummary = await screen.findByRole("textbox", { name: "视觉对象 02 summary" });
    expect(restoredSummary).toHaveValue("第二幅公开分布图。");
    await userEvent.clear(restoredSummary);
    await userEvent.type(restoredSummary, "第二幅公开分布图已复核。");
    const nudgeRight = screen.getByRole("button", { name: "右移" });
    nudgeRight.focus();
    await userEvent.keyboard("{Enter}");
    const saveVisualEdit = screen.getAllByRole("button", { name: "保存修改" })
      .find((button) => !(button as HTMLButtonElement).disabled);
    expect(saveVisualEdit).toBeDefined();
    await userEvent.click(saveVisualEdit!);
    await waitFor(() => expect(editedBody).toEqual(expect.objectContaining({
      base_snapshot_id: "snapshot-2",
      summary: "第二幅公开分布图已复核。",
      bounds: { left: 0.501, top: 0.24, width: 0.34, height: 0.38 },
    })));

    await userEvent.click(screen.getByRole("button", { name: "视觉对象 01 上移" }));
    await userEvent.click(screen.getByRole("button", { name: "视觉对象 02 上移" }));
    await waitFor(() => expect(
      screen.getByRole("button", { name: "编辑视觉对象 01" }),
    ).toHaveTextContent("第二幅公开分布图已复核。"));
    expect(screen.getByRole("button", { name: /视觉对象 01 框选范围/ }))
      .toHaveAttribute("data-visual-ref", "visual-b");
    expect(await screen.findByText("视觉对象 02 已上移到第 1 位。")).toBeInTheDocument();

    const deleteButton = screen.getByRole("button", { name: "删除视觉对象 01" });
    await userEvent.click(deleteButton);
    const confirm = await screen.findByRole("dialog", { name: "删除视觉对象 01？" });
    expect(confirm).toBeInTheDocument();
    await waitFor(() => expect(
      screen.getByRole("button", { name: "确认删除视觉对象 01" }),
    ).toHaveFocus());
    expect(screen.getAllByRole("button", { name: /第 1 页/ })[0]).toBeDisabled();
    await userEvent.keyboard("{Escape}");
    await waitFor(() => expect(deleteButton).toHaveFocus());
    expect(screen.queryByRole("dialog", { name: "删除视觉对象 01？" })).not.toBeInTheDocument();
    await userEvent.click(deleteButton);
    await userEvent.click(await screen.findByRole("button", { name: "确认删除视觉对象 01" }));
    expect(await screen.findByText("视觉对象 01 已删除；其余对象已重新编号。")).toBeInTheDocument();
    await waitFor(() => expect(
      screen.getByRole("button", { name: "批准并转到下一待处理页" }),
    ).toHaveFocus());
    expect(screen.getAllByRole("button", { name: /视觉对象 01 框选范围/ })).toHaveLength(1);

    await userEvent.click(screen.getByRole("button", { name: "删除视觉对象 01" }));
    await userEvent.click(await screen.findByRole("button", { name: "确认删除视觉对象 01" }));
    expect(await screen.findByText("最后一个视觉对象已删除；来源仍有缺口，请重新框选或改选来源完整。")).toBeInTheDocument();
    await waitFor(() => expect(
      screen.getByRole("button", { name: "有缺口，在页面上框选" }),
    ).toHaveFocus());
    expect(screen.getByText("来源仍有缺口：请重新框选视觉对象，或明确改选来源完整。")).toBeInTheDocument();
  });

  it("视觉对象变更失败时保留表单、范围、顺序、编号和焦点", async () => {
    const reviewedSnapshot = {
      snapshot_id: "snapshot-failure",
      source_content: originalSource,
      source_confirmation: {
        actor_id: "operator-zhang",
        confirmed_at: "2026-08-24T18:00:00+00:00",
      },
      source_review: {
        actor_id: "operator-zhang",
        completed_at: "2026-08-24T18:01:00+00:00",
      },
    };
    const visuals = [
      {
        visual_ref: "visual-a",
        position: 0,
        source_kind: "capture",
        disposition: "included",
        summary: "第一幅原始图。",
        visual_type: "chart",
        bounds: { left: 0.08, top: 0.12, width: 0.32, height: 0.3 },
        source_visual_ref: null,
        confirmed: true,
      },
      {
        visual_ref: "visual-b",
        position: 1,
        source_kind: "capture",
        disposition: "included",
        summary: "第二幅原始图。",
        visual_type: "map",
        bounds: { left: 0.5, top: 0.24, width: 0.34, height: 0.38 },
        source_visual_ref: null,
        confirmed: true,
      },
    ];
    vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
      const url = String(input);
      if (url === "/api/v1/app/bootstrap") {
        return Promise.resolve(new Response(JSON.stringify(bootstrap), { status: 200 }));
      }
      if (url.endsWith("/api/v1/curation/pages?review_status=pending")) {
        return Promise.resolve(new Response(JSON.stringify({ pages: [pendingPage] }), { status: 200 }));
      }
      if (url === "/api/v1/pages/page-1" && !init?.method) {
        return Promise.resolve(new Response(JSON.stringify({
          page_id: "page-1",
          page_number: 1,
          review_status: "pending",
          source_content: originalSource,
          curation: curationState(reviewedSnapshot),
          annotation: { snapshot_id: reviewedSnapshot.snapshot_id, visuals },
          standard_render: {
            sha256: "c".repeat(64),
            media_type: "image/png",
            dpi: 144,
            width_px: 1600,
            height_px: 900,
            url: "/api/v1/pages/page-1/render",
          },
        }), { status: 200 }));
      }
      const error = (message: string) => Promise.resolve(new Response(JSON.stringify({
        error: { code: "simulated_failure", message },
      }), { status: 409 }));
      if (url.endsWith("/visual-a") && init?.method === "PATCH") {
        return error("模拟编辑失败。");
      }
      if (url.endsWith("/visual-b/move") && init?.method === "POST") {
        return error("模拟排序失败。");
      }
      if (url.endsWith("/visual-a") && init?.method === "DELETE") {
        return error("模拟删除失败。");
      }
      throw new Error(`未覆盖的请求：${url} ${init?.method ?? "GET"}`);
    });

    render(<App />);
    await userEvent.click(await screen.findByRole("button", { name: "编辑视觉对象 01" }));
    const summary = await screen.findByRole("textbox", { name: "视觉对象 01 summary" });
    await userEvent.clear(summary);
    await userEvent.type(summary, "失败后必须保留的本地表单。");
    await userEvent.click(screen.getByRole("button", { name: "右移" }));
    const saveEdit = screen.getAllByRole("button", { name: "保存修改" })
      .find((button) => !(button as HTMLButtonElement).disabled);
    expect(saveEdit).toBeDefined();
    await userEvent.click(saveEdit!);
    expect(await screen.findByText(/模拟编辑失败。 当前范围和表单内容仍保留/)).toBeInTheDocument();
    expect(summary).toHaveValue("失败后必须保留的本地表单。");
    expect(screen.getByRole("button", { name: /视觉对象 01 框选范围/ }))
      .toHaveStyle({ left: "8.1%" });

    await userEvent.click(screen.getByRole("button", { name: "放弃修改" }));
    const moveButton = screen.getByRole("button", { name: "视觉对象 02 上移" });
    await userEvent.click(moveButton);
    expect(await screen.findByText(/模拟排序失败。 原顺序与原编号仍保留/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "编辑视觉对象 01" })).toHaveTextContent("第一幅原始图。");
    expect(screen.getByRole("button", { name: "编辑视觉对象 02" })).toHaveTextContent("第二幅原始图。");
    await waitFor(() => expect(moveButton).toHaveFocus());

    await userEvent.click(screen.getByRole("button", { name: "删除视觉对象 01" }));
    const confirmDelete = await screen.findByRole("button", { name: "确认删除视觉对象 01" });
    await userEvent.click(confirmDelete);
    expect(await screen.findByText(/模拟删除失败。 原对象与原编号仍保留/)).toBeInTheDocument();
    await waitFor(() => expect(confirmDelete).toHaveFocus());
    expect(document.querySelectorAll("[data-visual-ref]")).toHaveLength(2);
    expect(document.querySelector("[data-visual-ref='visual-a']")).not.toBeNull();
    expect(document.querySelector("[data-visual-ref='visual-b']")).not.toBeNull();
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

  it("按 AnyDoc 顺序逐项保留或忽略图片来源并显式完成来源审核", async () => {
    const source = {
      ...originalSource,
      images: [
        {
          reference_index: 0,
          alt_text: "公开流程图",
          media_type: "image/png",
          origin_part: "ppt/media/shared.png",
        },
        {
          reference_index: 1,
          alt_text: "公开装饰图",
          media_type: "image/png",
          origin_part: "ppt/media/shared.png",
        },
      ],
    };
    type Decision = "included" | "ignored" | null;
    let snapshotId: string | null = null;
    let confirmed = false;
    let reviewed = false;
    let decisions: Decision[] = [null, null];
    let summaries = ["", ""];
    let reasons: Array<string | null> = [null, null];
    let notes = ["", ""];
    const imageItems = () => source.images.map((image, index) => ({
      ...image,
      source_ref: `source-${index + 1}`,
      position: index,
      object_sha256: "a".repeat(64),
      size_bytes: 2048 + index,
      integrity: "verified" as const,
      duplicate_object: true,
      preview_url: `/api/v1/pages/page-1/source-images/source-${index + 1}`,
      disposition: decisions[index],
      summary: summaries[index] || null,
      ignore_reason: reasons[index],
      ignore_note: notes[index] || null,
      visual_ref: decisions[index] === "included" ? "visual-1" : null,
      decided_by: decisions[index] ? "operator-zhang" : null,
      decided_at: decisions[index] ? "2026-08-24T18:00:00+00:00" : null,
    }));
    const state = () => {
      const imageBlockers = imageItems().flatMap((item) => {
        const number = String(item.position + 1).padStart(2, "0");
        if (!item.disposition) return [{
          code: "image_disposition_required",
          message: `图片来源 ${number}：尚未选择保留或忽略。`,
          source_ref: item.source_ref,
        }];
        if (item.disposition === "included" && !item.summary) return [{
          code: "image_summary_required",
          message: `图片来源 ${number}：保留项缺少 summary。`,
          source_ref: item.source_ref,
        }];
        if (item.disposition === "ignored" && !item.ignore_reason) return [{
          code: "image_reason_required",
          message: `图片来源 ${number}：忽略项缺少原因。`,
          source_ref: item.source_ref,
        }];
        if (item.ignore_reason === "other" && !item.ignore_note) return [{
          code: "image_other_note_required",
          message: `图片来源 ${number}：“其他”原因缺少说明。`,
          source_ref: item.source_ref,
        }];
        return [];
      });
      const blockers = [
        ...(!snapshotId ? [{ code: "source_unsaved", message: "文字修改尚未保存。" }] : []),
        ...(!confirmed ? [{ code: "source_unconfirmed", message: "文字来源尚未确认。" }] : []),
        ...(!reviewed ? [{ code: "source_review_incomplete", message: "来源审核尚未完成。" }] : []),
        ...imageBlockers,
      ];
      return {
        current_snapshot: snapshotId ? {
          snapshot_id: snapshotId,
          source_snapshot_id: null,
          source_content: source,
          created_by: "operator-zhang",
          created_at: "2026-08-24T18:00:00+00:00",
          source_confirmation: confirmed ? {
            actor_id: "operator-zhang",
            confirmed_at: "2026-08-24T18:00:00+00:00",
          } : null,
          source_review: reviewed ? {
            actor_id: "operator-zhang",
            completed_at: "2026-08-24T18:02:00+00:00",
          } : null,
          image_source_decisions: [],
        } : null,
        image_sources: {
          total: 2,
          unresolved: imageBlockers.length,
          items: imageItems(),
        },
        chunk_body: { nonempty: true },
        blockers,
        can_confirm_source: Boolean(snapshotId),
        can_complete_source_review: confirmed && imageBlockers.length === 0,
        can_approve: reviewed && blockers.length === 0,
      };
    };

    vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
      const url = String(input);
      if (url === "/api/v1/app/bootstrap") {
        return Promise.resolve(new Response(JSON.stringify(bootstrap), { status: 200 }));
      }
      if (url.includes("/api/v1/curation/pages")) {
        return Promise.resolve(new Response(JSON.stringify({ pages: [pendingPage] }), { status: 200 }));
      }
      if (url === "/api/v1/pages/page-1" && !init?.method) {
        return Promise.resolve(new Response(JSON.stringify({
          page_id: "page-1",
          page_number: 1,
          review_status: "pending",
          source_content: source,
          curation: state(),
        }), { status: 200 }));
      }
      if (url.endsWith("/curation/snapshots") && init?.method === "POST") {
        const request = JSON.parse(String(init.body));
        source.titles = request.titles;
        source.body = request.body;
        snapshotId = "snapshot-text";
        confirmed = false;
        reviewed = false;
        return Promise.resolve(new Response(JSON.stringify({ curation: state() }), { status: 201 }));
      }
      if (url.endsWith("/curation/source-confirmation") && init?.method === "POST") {
        confirmed = true;
        return Promise.resolve(new Response(JSON.stringify({ curation: state() }), { status: 200 }));
      }
      if (url.includes("/curation/image-sources/") && init?.method === "POST") {
        const index = url.endsWith("source-1") ? 0 : 1;
        const body = JSON.parse(String(init.body));
        snapshotId = `snapshot-image-${index + 1}`;
        reviewed = false;
        decisions[index] = body.disposition;
        summaries[index] = body.summary ?? "";
        reasons[index] = body.ignore_reason ?? null;
        notes[index] = body.ignore_note ?? "";
        return Promise.resolve(new Response(JSON.stringify({ curation: state() }), { status: 201 }));
      }
      if (url.endsWith("/curation/source-review") && init?.method === "POST") {
        reviewed = true;
        return Promise.resolve(new Response(JSON.stringify({ curation: state() }), { status: 200 }));
      }
      throw new Error(`未覆盖的请求：${url}`);
    });

    render(<App />);
    await userEvent.click(await screen.findByRole("button", { name: "保存修改" }));
    await userEvent.click(await screen.findByRole("button", { name: "确认文字来源" }));

    expect(await screen.findByRole("heading", { name: "图片来源" })).toBeInTheDocument();
    expect(screen.getByText("0 / 2 已处置")).toBeInTheDocument();
    expect(screen.getAllByText("重复对象")).toHaveLength(2);
    expect(screen.queryByRole("button", { name: /上移|下移|排序/ })).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole("radio", { name: "保留原始图片" }));
    expect(screen.getByText("将以原始字节与媒体类型进入产物")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "保存并处理下一项" })).toBeDisabled();
    await userEvent.type(
      screen.getByRole("textbox", { name: "图片来源 01 summary" }),
      "公开流程图展示录入、核验和发布三个连续阶段。",
    );
    await userEvent.type(
      screen.getByRole("textbox", { name: "标题来源 1 当前编辑值" }),
      "（文字修订）",
    );
    expect(screen.queryByRole("button", { name: "保存并处理下一项" })).not.toBeInTheDocument();
    const saveText = screen.getByRole("button", { name: "保存修改" });
    expect(saveText).toBeEnabled();
    await userEvent.click(saveText);
    const confirmText = screen.getByRole("button", { name: "确认文字来源" });
    expect(confirmText).toBeEnabled();
    await userEvent.click(confirmText);
    expect(screen.getByRole("textbox", { name: "图片来源 01 summary" })).toHaveValue(
      "公开流程图展示录入、核验和发布三个连续阶段。",
    );
    await userEvent.click(screen.getByRole("button", { name: "保存并处理下一项" }));
    await waitFor(() => expect(
      screen.getByRole("radio", { name: "保留原始图片" }),
    ).toHaveFocus());

    await userEvent.click(screen.getByRole("radio", { name: "忽略此来源" }));
    expect(screen.getByRole("button", { name: "保存并处理下一项" })).toBeDisabled();
    await userEvent.selectOptions(screen.getByRole("combobox", { name: "图片来源 02 忽略原因" }), "other");
    expect(screen.getByRole("button", { name: "保存并处理下一项" })).toBeDisabled();
    await userEvent.type(
      screen.getByRole("textbox", { name: "图片来源 02 其他原因说明" }),
      "该图片只包含无语义的版式占位。",
    );
    expect(screen.getByRole("button", { name: "保存并处理下一项" })).toBeEnabled();
    await userEvent.click(screen.getByRole("button", { name: "保存并处理下一项" }));
    const review = screen.getByRole("button", { name: "完成来源审核" });
    await waitFor(() => expect(review).toHaveFocus());
    await userEvent.click(review);
    expect(await screen.findByText("等待来源完整性选择")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: /图片来源 01/ }));
    await userEvent.type(
      screen.getByRole("textbox", { name: "图片来源 01 summary" }),
      "（修订）",
    );
    expect(screen.getByText("来源审核确认已失效")).toBeInTheDocument();
  });
});
