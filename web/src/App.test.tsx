import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { App } from "./App";

const bootstrap = {
  actor: { actor_id: "operator-zhang", display_name: "操作者 operator-zhang" },
  runways: [
    { id: "pending", label: "待处理", documents: [] },
    { id: "processing", label: "处理中", documents: [] },
    { id: "curatable", label: "可策展", documents: [] },
  ],
};

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
