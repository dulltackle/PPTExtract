import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { PublicationPreflight } from "./PublicationPreflight";
import type { PublicationCandidate, PublicationWorkspace } from "./api";

const candidate: PublicationCandidate = {
  candidate_id: "candidate-36",
  status: "ready",
  business_state_token: "business-state-token-36",
  content_set_hash: "content-set-hash-36",
  created_by: "publisher-1",
  created_at: "2026-08-29T06:00:00Z",
  confirmed_by: null,
  confirmed_at: null,
  publication_seq: null,
  frozen_input_hash: null,
  diff: { added: 2, updated: 1, removed: 1, unchanged: 4 },
  excluded: {
    pending_pages: 3,
    excluded_pages: 2,
    disabled_hidden_pages: 1,
    soft_deleted_documents: 1,
  },
  documents: [
    {
      document_id: "document-1",
      version_id: "version-3",
      title: "公开知识源.pptx",
      pages: [
        {
          page_number: 2,
          title: "公开页标题",
          page_id: "page-2",
          chunk_id: "chunk-2",
          snapshot_id: "snapshot-2",
          reviewed_by: "curator-1",
          reviewed_at: "2026-08-29T05:30:00Z",
          change: "updated",
        },
      ],
    },
  ],
  chunk_count: 7,
  asset_count: 3,
};

const emptyWorkspace: PublicationWorkspace = {
  preflight: {
    can_publish: true,
    summary: { total: 0, pages: 0, unconfirmed: 0, unconfirmed_pages: 0 },
    stale_render_versions: 0,
    href: null,
  },
  current: null,
  candidate: null,
  task: null,
};

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("冻结发布台账", () => {
  it("从诚实空态创建候选，并展开核验文档、版本和纳入页", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
      const url = String(input);
      if (url === "/api/v1/publications" && !init?.method) {
        return Promise.resolve(new Response(JSON.stringify(emptyWorkspace)));
      }
      if (url === "/api/v1/publications/candidates" && init?.method === "POST") {
        return Promise.resolve(new Response(JSON.stringify(candidate), { status: 201 }));
      }
      throw new Error(`未覆盖的请求：${url}`);
    });

    render(<PublicationPreflight />);

    expect(await screen.findByRole("heading", { name: "首次发布尚未建立" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "创建发布候选" }));

    expect(await screen.findAllByText("candidate-36")).toHaveLength(2);
    expect(screen.getByText("新增 2")).toBeInTheDocument();
    expect(screen.getByText("更新 1")).toBeInTheDocument();
    expect(screen.getByText("移除 1")).toBeInTheDocument();
    expect(screen.getByText("pending 3 页不纳入")).toBeInTheDocument();
    await userEvent.click(screen.getByText("公开知识源.pptx"));
    const pageRow = screen.getByText("公开页标题").closest("tr");
    expect(pageRow).toHaveTextContent("chunk-2");
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/publications/candidates",
      expect.objectContaining({ method: "POST" }),
    );
  });

  it("在控制轨内完成最终确认，并持续展示冻结阶段", async () => {
    const workspace = { ...emptyWorkspace, candidate };
    vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
      const url = String(input);
      if (url === "/api/v1/publications") {
        return Promise.resolve(new Response(JSON.stringify(workspace)));
      }
      if (
        url === "/api/v1/publications/candidates/candidate-36/confirm" &&
        init?.method === "POST"
      ) {
        return Promise.resolve(
          new Response(
            JSON.stringify({
              candidate_id: "candidate-36",
              status: "queued",
              publication_seq: 8,
              job_id: "job-8",
              frozen_input_hash: "frozen-input-36",
            }),
            { status: 202 },
          ),
        );
      }
      throw new Error(`未覆盖的请求：${url}`);
    });

    render(<PublicationPreflight />);
    await userEvent.click(await screen.findByRole("button", { name: "确认发布" }));

    const gate = screen.getByRole("region", { name: "最终确认" });
    expect(gate).toHaveTextContent("candidate-36");
    expect(gate).toHaveTextContent("1 个文档 · 1 页");
    expect(gate).toHaveTextContent("7 个 Chunk · 3 个视觉资产");
    expect(gate).toHaveTextContent("构建切换前仍无当前产物");
    await userEvent.click(screen.getByRole("button", { name: "确认并开始构建" }));

    expect(await screen.findByText("冻结输入已锁定")).toBeInTheDocument();
    expect(screen.getByText("构建完整 ZIP")).toBeInTheDocument();
    expect(screen.getByText("完整性校验")).toBeInTheDocument();
    expect(screen.getByText("切换当前指针")).toBeInTheDocument();
    expect(screen.getByText(/发布序号 #8/)).toBeInTheDocument();
  });

  it("把对象存储阶段归入切换前阶段并给出准确状态", async () => {
    const storingWorkspace: PublicationWorkspace = {
      ...emptyWorkspace,
      candidate: { ...candidate, status: "confirmed", publication_seq: 8 },
      task: {
        job_id: "job-8",
        candidate_id: "candidate-36",
        publication_seq: 8,
        status: "running",
        phase: "store",
        progress: { phase: "store", completed_pages: 0, total_pages: 7 },
        error: null,
        attempts: 1,
        updated_at: "2026-08-29T06:10:00Z",
      },
    };
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify(storingWorkspace), { status: 200 }),
    );

    render(<PublicationPreflight />);

    const storageState = await screen.findByText("正在写入不可变对象存储");
    expect(storageState.closest("li")).toHaveClass("is-active");
    expect(screen.getByText("完整性校验").closest("li")).toHaveClass("is-complete");
  });

  it("发布占用中持续展示冻结身份、活动阶段并允许手动刷新", async () => {
    const busyWorkspace: PublicationWorkspace = {
      ...emptyWorkspace,
      candidate: { ...candidate, status: "confirmed", publication_seq: 8 },
      task: {
        job_id: "job-8",
        candidate_id: "candidate-36",
        publication_seq: 8,
        status: "running",
        phase: "validate",
        progress: { phase: "validate", completed_pages: 0, total_pages: 7 },
        error: null,
        attempts: 1,
        updated_at: "2026-08-29T06:10:00Z",
      },
    };
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(() =>
      Promise.resolve(new Response(JSON.stringify(busyWorkspace), { status: 200 })),
    );

    render(<PublicationPreflight />);

    expect(await screen.findByRole("heading", { name: "发布占用中" })).toBeInTheDocument();
    const activeTask = screen.getByRole("region", { name: "活动发布任务" });
    expect(activeTask).toHaveTextContent("job-8");
    expect(activeTask).toHaveTextContent("candidate-36");
    expect(activeTask).toHaveTextContent("#8");
    expect(activeTask).toHaveTextContent("running · validate");
    expect(screen.getByText("新增 2")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "刷新活动状态" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
  });

  it("失败时证明当前产物未移动，并使用原冻结输入重试", async () => {
    const failedWorkspace: PublicationWorkspace = {
      ...emptyWorkspace,
      current: {
        publication_seq: 7,
        candidate_id: "candidate-7",
        snapshot_id: "snapshot-7",
        published_at: "2026-08-28T06:00:00Z",
        chunk_count: 5,
        asset_count: 2,
        size_bytes: 2048,
        sha256: "7".repeat(64),
        media_type: "application/zip",
        download_url: "/api/v1/publications/7/artifact",
      },
      candidate: {
        ...candidate,
        status: "failed",
        publication_seq: 8,
        frozen_input_hash: "frozen-input-36",
      },
      task: {
        job_id: "job-8",
        candidate_id: "candidate-36",
        publication_seq: 8,
        status: "failed",
        phase: "validate",
        progress: { phase: "validate", completed_pages: 0, total_pages: 7 },
        error: {
          code: "publication_build_failed",
          message: "视觉资产引用不完整",
          phase: "validate",
          retryable: true,
        },
        attempts: 1,
        updated_at: "2026-08-29T06:10:00Z",
      },
    };
    vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
      const url = String(input);
      if (url === "/api/v1/publications") {
        return Promise.resolve(new Response(JSON.stringify(failedWorkspace)));
      }
      if (url === "/api/v1/publications/tasks/job-8/retry" && init?.method === "POST") {
        return Promise.resolve(
          new Response(
            JSON.stringify({
              candidate_id: "candidate-36",
              status: "queued",
              publication_seq: 8,
              job_id: "job-8-retry",
              frozen_input_hash: "frozen-input-36",
            }),
            { status: 202 },
          ),
        );
      }
      throw new Error(`未覆盖的请求：${url}`);
    });

    render(<PublicationPreflight />);

    expect(await screen.findByText("本次任务失败")).toBeInTheDocument();
    expect(screen.getByText("当前产物仍为 #7")).toBeInTheDocument();
    expect(screen.getByText("视觉资产引用不完整")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "使用原冻结输入重试" }));
    await waitFor(() => expect(screen.getByText(/正在复用原冻结输入/)).toBeInTheDocument());
  });

  it("失败后可按最新业务状态创建新候选，并保留失败回执", async () => {
    const failedWorkspace: PublicationWorkspace = {
      ...emptyWorkspace,
      current: {
        publication_seq: 7,
        candidate_id: "candidate-7",
        snapshot_id: "snapshot-7",
        published_at: "2026-08-28T06:00:00Z",
        chunk_count: 5,
        asset_count: 2,
        size_bytes: 2048,
        sha256: "7".repeat(64),
        media_type: "application/zip",
        download_url: "/api/v1/publications/7/artifact",
      },
      candidate: {
        ...candidate,
        status: "failed",
        publication_seq: 8,
        frozen_input_hash: "frozen-input-36",
      },
      task: {
        job_id: "job-8",
        candidate_id: "candidate-36",
        publication_seq: 8,
        status: "failed",
        phase: "validate",
        progress: { phase: "validate", completed_pages: 0, total_pages: 7 },
        error: {
          code: "publication_build_failed",
          message: "视觉资产引用不完整",
          phase: "validate",
          retryable: true,
        },
        attempts: 1,
        updated_at: "2026-08-29T06:10:00Z",
      },
    };
    const latestCandidate = {
      ...candidate,
      candidate_id: "candidate-latest",
      business_state_token: "business-state-token-latest",
      created_at: "2026-08-29T06:20:00Z",
    };
    vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
      const url = String(input);
      if (url === "/api/v1/publications") {
        return Promise.resolve(new Response(JSON.stringify(failedWorkspace)));
      }
      if (url === "/api/v1/publications/candidates" && init?.method === "POST") {
        return Promise.resolve(new Response(JSON.stringify(latestCandidate), { status: 201 }));
      }
      throw new Error(`未覆盖的请求：${url}`);
    });

    render(<PublicationPreflight />);
    expect(await screen.findByText("本次任务失败")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "按最新业务状态创建新候选" }));

    expect(await screen.findAllByText("candidate-latest")).toHaveLength(2);
    expect(screen.queryByText("视觉资产引用不完整")).not.toBeInTheDocument();
  });

  it("重试遇到全局占用时刷新为活动任务，清除旧错误并恢复操作焦点", async () => {
    const failedWorkspace: PublicationWorkspace = {
      ...emptyWorkspace,
      candidate: { ...candidate, status: "failed", publication_seq: 8 },
      task: {
        job_id: "job-8",
        candidate_id: "candidate-36",
        publication_seq: 8,
        status: "failed",
        phase: "validate",
        progress: { phase: "validate", completed_pages: 0, total_pages: 7 },
        error: {
          code: "publication_build_failed",
          message: "视觉资产引用不完整",
          phase: "validate",
          retryable: true,
        },
        attempts: 1,
        updated_at: "2026-08-29T06:10:00Z",
      },
    };
    const activeCandidate = {
      ...candidate,
      candidate_id: "candidate-active",
      status: "confirmed" as const,
      publication_seq: 9,
    };
    const busyWorkspace: PublicationWorkspace = {
      ...emptyWorkspace,
      candidate: activeCandidate,
      task: {
        job_id: "job-9",
        candidate_id: "candidate-active",
        publication_seq: 9,
        status: "running",
        phase: "build",
        progress: { phase: "build", completed_pages: 0, total_pages: 7 },
        error: null,
        attempts: 1,
        updated_at: "2026-08-29T06:12:00Z",
      },
    };
    let workspaceLoads = 0;
    vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
      const url = String(input);
      if (url === "/api/v1/publications") {
        workspaceLoads += 1;
        const workspace = workspaceLoads === 1 ? failedWorkspace : busyWorkspace;
        return Promise.resolve(new Response(JSON.stringify(workspace)));
      }
      if (url === "/api/v1/publications/tasks/job-8/retry" && init?.method === "POST") {
        return Promise.resolve(new Response(JSON.stringify({
          error: {
            code: "publication_busy",
            message: "已有发布任务正在构建，请等待完成后再确认。",
            details: {
              job_id: "job-9",
              candidate_id: "candidate-active",
              publication_seq: 9,
              status: "running",
              phase: "build",
              updated_at: "2026-08-29T06:12:00Z",
            },
          },
        }), { status: 409 }));
      }
      throw new Error(`未覆盖的请求：${url}`);
    });

    render(<PublicationPreflight />);
    await userEvent.click(await screen.findByRole("button", { name: "使用原冻结输入重试" }));

    expect(await screen.findByRole("heading", { name: "发布占用中" })).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "活动发布任务" })).toHaveTextContent("job-9");
    expect(screen.queryByText("操作未完成")).not.toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByRole("button", { name: "刷新活动状态" })).toHaveFocus();
    });
  });

  it("前置校验阻塞时只提供原因和修复入口，不创建候选", async () => {
    const blocked: PublicationWorkspace = {
      ...emptyWorkspace,
      preflight: {
        can_publish: false,
        summary: { total: 2, pages: 2, unconfirmed: 2, unconfirmed_pages: 2 },
        stale_render_versions: 0,
        href: "/curation?filter=rendering-warnings",
      },
    };
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify(blocked), { status: 200 }),
    );

    render(<PublicationPreflight />);

    expect(await screen.findByText("发布被阻止")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "前往确认渲染警告" })).toHaveAttribute(
      "href",
      "/curation?filter=rendering-warnings",
    );
    expect(screen.getByRole("button", { name: "创建发布候选" })).toBeDisabled();
  });

  it("候选失效后保留旧摘要，并把主动作切换为按最新状态重建", async () => {
    const staleWorkspace: PublicationWorkspace = {
      ...emptyWorkspace,
      candidate: { ...candidate, status: "stale" },
    };
    vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
      const url = String(input);
      if (url === "/api/v1/publications") {
        return Promise.resolve(new Response(JSON.stringify(staleWorkspace)));
      }
      if (url === "/api/v1/publications/candidates" && init?.method === "POST") {
        return Promise.resolve(new Response(JSON.stringify(candidate), { status: 201 }));
      }
      throw new Error(`未覆盖的请求：${url}`);
    });

    render(<PublicationPreflight />);

    expect((await screen.findAllByText("已失效")).length).toBeGreaterThan(0);
    expect(screen.getByText("新增 2")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "重新创建发布候选" }));
    expect(await screen.findByRole("button", { name: "确认发布" })).toBeInTheDocument();
  });

  it("无变化形成独立回执，明确未创建 ZIP、任务或新序号", async () => {
    const noChangeWorkspace: PublicationWorkspace = {
      ...emptyWorkspace,
      candidate: { ...candidate, status: "no_change" },
    };
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify(noChangeWorkspace), { status: 200 }),
    );

    render(<PublicationPreflight />);

    expect(await screen.findByRole("heading", { name: "内容集合无变化" })).toBeInTheDocument();
    expect(screen.getByText(/未生成重复 ZIP，也未递增发布序号/)).toBeInTheDocument();
    expect(screen.queryByRole("list", { name: "发布任务阶段" })).not.toBeInTheDocument();
  });

  it("成功回执同时证明完整性校验通过且当前指针已切换", async () => {
    const succeededWorkspace: PublicationWorkspace = {
      ...emptyWorkspace,
      current: {
        publication_seq: 8,
        candidate_id: "candidate-36",
        snapshot_id: "candidate-36",
        published_at: "2026-08-29T06:15:00Z",
        chunk_count: 7,
        asset_count: 3,
        size_bytes: 4096,
        sha256: "8".repeat(64),
        media_type: "application/zip",
        download_url: "/api/v1/publications/8/artifact",
      },
      candidate: { ...candidate, status: "succeeded", publication_seq: 8 },
      task: {
        job_id: "job-8",
        candidate_id: "candidate-36",
        publication_seq: 8,
        status: "succeeded",
        phase: "succeeded",
        progress: { phase: "succeeded", completed_pages: 7, total_pages: 7 },
        error: null,
        attempts: 1,
        updated_at: "2026-08-29T06:15:00Z",
      },
    };
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify(succeededWorkspace), { status: 200 }),
    );

    render(<PublicationPreflight />);

    expect(await screen.findByRole("heading", { name: "发布完成" })).toBeInTheDocument();
    expect(screen.getByText("已完整校验并原子切换")).toBeInTheDocument();
    expect(screen.getByText("8".repeat(64))).toBeInTheDocument();
    expect(screen.getByText("切换当前指针").closest("li")).toHaveClass("is-complete");
  });
});
