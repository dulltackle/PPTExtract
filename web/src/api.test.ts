import { afterEach, describe, expect, it, vi } from "vitest";

import {
  recordCurationTimingSample,
  retryPendingCurationTimingSamples,
} from "./api";

afterEach(() => {
  vi.restoreAllMocks();
});

describe("策展运行事实恢复队列", () => {
  it("非成功响应保留样本，并在后续重试成功后移除", async () => {
    const bodies: Array<Record<string, unknown>> = [];
    vi.spyOn(globalThis, "fetch")
      .mockImplementationOnce((_input, init) => {
        bodies.push(JSON.parse(String(init?.body)));
        return Promise.resolve(new Response(null, { status: 503 }));
      })
      .mockImplementationOnce((_input, init) => {
        bodies.push(JSON.parse(String(init?.body)));
        return Promise.resolve(new Response(null, { status: 201 }));
      });

    await recordCurationTimingSample(
      "recoverable-sample",
      "page-1",
      "version-1",
      "page_decision",
      123.6,
    );
    expect(globalThis.localStorage.getItem("pptextract:curation-timing-samples"))
      .toContain("recoverable-sample");

    await retryPendingCurationTimingSamples();

    expect(bodies).toEqual([
      {
        sample_id: "recoverable-sample",
        page_id: "page-1",
        version_id: "version-1",
        stage: "page_decision",
        duration_ms: 124,
      },
      {
        sample_id: "recoverable-sample",
        page_id: "page-1",
        version_id: "version-1",
        stage: "page_decision",
        duration_ms: 124,
      },
    ]);
    expect(globalThis.localStorage.getItem("pptextract:curation-timing-samples")).toBeNull();
  });
});
