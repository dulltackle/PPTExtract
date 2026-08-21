import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  type CurationPage,
  enableHiddenPage,
  loadCurationPages,
  loadJob,
  loadPageDetail,
  OperatorError,
} from "./api";

type Filter = "pending" | "all";

interface PageOperation {
  submitting: boolean;
  announcement: string | null;
}

const phaseLabels: Record<string, string> = {
  queued: "等待 worker 接手",
  conversion: "正在转换源页",
  rendering: "正在生成标准页渲染",
  page_fingerprint: "正在计算页指纹",
  activation: "正在建立页版本",
};

function pageKey(page: CurationPage): string {
  return `${page.document_id}:${page.version_id}:${page.page_number}`;
}

function pageStatusLabel(page: CurationPage): string {
  if (page.hidden && !page.enabled) {
    const status = page.enablement?.status;
    if (status === "queued") return "已排队";
    if (status === "running") return "处理中";
    if (status === "failed") return "处理失败";
    return "默认跳过";
  }
  return page.review_status === "pending"
    ? "待处理"
    : page.review_status === "approved"
      ? "已批准"
      : "已排除";
}

function HiddenRenderPlaceholder() {
  return (
    <div className="hidden-render-placeholder">
      <svg viewBox="0 0 72 52" aria-hidden="true">
        <rect x="2" y="2" width="68" height="48" rx="2" />
        <path d="M17 18h38M17 26h27M17 34h20" />
        <path d="M9 43 63 9" />
      </svg>
      <strong>此页尚未生成标准渲染</strong>
      <p>源页已登记但默认跳过。启用后才会转换、渲染并进入策展。</p>
    </div>
  );
}

function PageRail({
  pages,
  filter,
  selectedKey,
  onFilter,
  onSelect,
}: {
  pages: CurationPage[];
  filter: Filter;
  selectedKey: string | null;
  onFilter: (filter: Filter) => void;
  onSelect: (key: string) => void;
}) {
  return (
    <aside className="page-rail" aria-label="页清单">
      <div className="page-rail-heading">
        <div>
          <h1>逐页策展</h1>
          <p>{pages.length} 页可见</p>
        </div>
        <div className="filter-tabs" aria-label="页清单筛选">
          {(["pending", "all"] as const).map((value) => (
            <button
              key={value}
              type="button"
              className={filter === value ? "is-current" : ""}
              aria-pressed={filter === value}
              onClick={() => onFilter(value)}
            >
              {value === "pending" ? "待处理" : "全部"}
            </button>
          ))}
        </div>
      </div>
      <div className="page-list">
        {pages.length === 0 ? (
          <div className="page-list-empty">
            <strong>{filter === "pending" ? "待处理队列为空" : "当前版本没有可显示的页"}</strong>
            <span>
              {filter === "pending" ? "切换到“全部”可查看已处理页与隐藏页登记。" : "上传并处理版本后，源页会按原始顺序出现。"}
            </span>
          </div>
        ) : null}
        {pages.map((page) => {
          const key = pageKey(page);
          const hiddenUnprocessed = page.hidden && !page.enabled;
          const title = hiddenUnprocessed ? "隐藏页 · 未处理" : page.title || `第 ${page.page_number} 页`;
          const status = pageStatusLabel(page);
          return (
            <button
              type="button"
              className={`page-row ${hiddenUnprocessed ? "page-row--hidden" : ""} ${selectedKey === key ? "is-selected" : ""}`}
              key={key}
              aria-label={`第 ${page.page_number} 页，${title}，${status}`}
              aria-current={selectedKey === key ? "true" : undefined}
              onClick={() => onSelect(key)}
            >
              <span className="page-number">{String(page.page_number).padStart(2, "0")}</span>
              <span className={`page-state-mark ${hiddenUnprocessed ? "is-hollow" : ""}`} aria-hidden="true" />
              <span className="page-row-copy">
                <strong>{title}</strong>
                <span>{status}</span>
              </span>
            </button>
          );
        })}
      </div>
    </aside>
  );
}

function EvidencePanel({ page }: { page: CurationPage | null }) {
  return (
    <section className="evidence-panel" aria-labelledby="evidence-heading">
      <header className="evidence-heading">
        <div>
          <h2 id="evidence-heading">标准页渲染</h2>
          <span>{page ? `原始页序 ${page.page_number}` : "尚未选择页"}</span>
        </div>
        {page ? <span className="evidence-status">{pageStatusLabel(page)}</span> : null}
      </header>
      <div className="evidence-stage">
        {!page ? (
          <div className="evidence-empty">从左侧选择一页以查看来源证据。</div>
        ) : page.hidden && !page.enabled ? (
          <HiddenRenderPlaceholder />
        ) : (
          <figure className="page-render">
            <img src={`/api/v1/pages/${page.page_id}/render`} alt={`第 ${page.page_number} 页标准页渲染结果`} />
            <figcaption>{page.title || `第 ${page.page_number} 页`}</figcaption>
          </figure>
        )}
      </div>
    </section>
  );
}

function SourceRegistration({
  page,
  submitting,
  announcement,
  statusRef,
  onEnable,
}: {
  page: CurationPage;
  submitting: boolean;
  announcement: string | null;
  statusRef: React.RefObject<HTMLDivElement | null>;
  onEnable: () => void;
}) {
  const enablement = page.enablement;
  const busy = submitting || enablement?.status === "queued" || enablement?.status === "running";
  const failed = enablement?.status === "failed";
  const taskLabel = submitting
    ? "正在提交启用请求"
    : enablement?.status === "queued"
      ? "任务已排队"
      : enablement?.status === "running"
        ? "任务处理中"
        : failed
          ? "任务处理失败"
          : null;
  return (
    <div className="source-registration">
      <div className="log-title-row">
        <div>
          <h2>源页登记</h2>
          <p>尚未生成策展内容</p>
        </div>
        <span className="source-hidden-chip">隐藏 · 未启用</span>
      </div>
      <dl className="source-facts">
        <div><dt>原始页序</dt><dd>{page.page_number}</dd></div>
        <div><dt>Slide ID</dt><dd>{page.source_reference.slide_id}</dd></div>
        <div><dt>关系引用</dt><dd>{page.source_reference.relationship_id}</dd></div>
        <div className="source-part"><dt>源部件</dt><dd>{page.source_reference.part}</dd></div>
        <div><dt>隐藏标记</dt><dd>是</dd></div>
        <div><dt>处理状态</dt><dd>{pageStatusLabel(page)}</dd></div>
      </dl>
      {taskLabel || announcement ? (
        <div
          className={`task-notice ${failed ? "task-notice--failed" : ""}`}
          role="status"
          aria-live="polite"
          tabIndex={-1}
          ref={statusRef}
        >
          <strong>{announcement || taskLabel}</strong>
          {failed ? (
            <span>{enablement.error?.message || "处理未完成，源页仍保持未启用。"}</span>
          ) : busy ? (
            <span>任务已持久化，可以离开此页；重新进入会恢复真实状态。</span>
          ) : null}
        </div>
      ) : null}
      <div className="enable-boundary">
        <p>启用后才会转换和渲染；成功前不会创建页版本、页指纹或审核状态。</p>
        <button type="button" className="enable-page-button" disabled={busy} onClick={onEnable}>
          {busy ? "正在处理此页" : failed ? "重试处理" : "启用并处理此页"}
        </button>
      </div>
    </div>
  );
}

function InspectorPanel({
  page,
  submitting,
  announcement,
  statusRef,
  onEnable,
}: {
  page: CurationPage | null;
  submitting: boolean;
  announcement: string | null;
  statusRef: React.RefObject<HTMLDivElement | null>;
  onEnable: () => void;
}) {
  return (
    <aside className="inspector-panel" aria-label="来源与策展日志">
      {!page ? (
        <div className="inspector-empty">选择一页后，这里会显示可追溯来源与可用动作。</div>
      ) : page.hidden && !page.enabled ? (
        <SourceRegistration
          page={page}
          submitting={submitting}
          announcement={announcement}
          statusRef={statusRef}
          onEnable={onEnable}
        />
      ) : (
        <NormalSourceRegistration
          page={page}
          announcement={announcement}
          statusRef={statusRef}
        />
      )}
    </aside>
  );
}

function NormalSourceRegistration({
  page,
  announcement,
  statusRef,
}: {
  page: CurationPage;
  announcement: string | null;
  statusRef: React.RefObject<HTMLDivElement | null>;
}) {
  const [source, setSource] = useState<{
    titles: string[];
    body: string[];
    speaker_notes: string[];
  } | null>(null);
  const [sourceError, setSourceError] = useState<string | null>(null);

  useEffect(() => {
    if (!page.page_id) return;
    const controller = new AbortController();
    setSource(null);
    setSourceError(null);
    loadPageDetail(page.page_id, controller.signal)
      .then((detail) => setSource(detail.source_content))
      .catch((cause) => {
        if (cause instanceof DOMException && cause.name === "AbortError") return;
        setSourceError(
          cause instanceof OperatorError ? cause.message : "AnyDoc 来源加载失败，请重试。",
        );
      });
    return () => controller.abort();
  }, [page.page_id]);

  return (
    <div className="source-registration">
      <div className="log-title-row">
        <div>
          <h2>AnyDoc 来源</h2>
          <p>页已进入普通策展流程</p>
        </div>
        <span className="pending-chip">{pageStatusLabel(page)}</span>
      </div>
      {announcement ? (
        <div
          className="task-notice task-notice--success"
          role="status"
          aria-live="polite"
          tabIndex={-1}
          ref={statusRef}
        >
          <strong>{announcement}</strong>
        </div>
      ) : null}
      <div className="normal-source-note" aria-busy={!source && !sourceError}>
        {sourceError ? (
          <p className="source-load-error">{sourceError}</p>
        ) : source ? (
          <>
            <strong>{source.titles[0] || page.title || `第 ${page.page_number} 页`}</strong>
            {source.body.length ? (
              <div className="source-body">
                {source.body.map((paragraph, index) => (
                  <p key={`${index}:${paragraph}`}>{paragraph}</p>
                ))}
              </div>
            ) : (
              <p>本页没有正文段落；标题、表格、图片或备注仍可作为来源。</p>
            )}
            {source.speaker_notes.length ? (
              <div className="speaker-notes">
                <span>演讲者备注</span>
                <p>{source.speaker_notes.join("\n")}</p>
              </div>
            ) : null}
          </>
        ) : (
          <p>正在加载真实 AnyDoc 来源…</p>
        )}
      </div>
    </div>
  );
}

export function CurationWorkbench() {
  const [filter, setFilter] = useState<Filter>("pending");
  const [pages, setPages] = useState<CurationPage[]>([]);
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [operations, setOperations] = useState<Record<string, PageOperation>>({});
  const request = useRef<AbortController | null>(null);
  const poll = useRef<AbortController | null>(null);
  const timer = useRef<number | null>(null);
  const statusRef = useRef<HTMLDivElement>(null);
  const selectedKeyRef = useRef<string | null>(null);
  selectedKeyRef.current = selectedKey;

  const loadPages = useCallback(async (nextFilter: Filter, preserveKey?: string | null) => {
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    setLoading(true);
    setError(null);
    try {
      const nextPages = await loadCurationPages(nextFilter, controller.signal);
      setPages(nextPages);
      setSelectedKey((current) => {
        const preferred = preserveKey ?? current;
        return nextPages.some((page) => pageKey(page) === preferred)
          ? preferred
          : nextPages[0]
            ? pageKey(nextPages[0])
            : null;
      });
    } catch (cause) {
      if (cause instanceof DOMException && cause.name === "AbortError") return;
      setError(cause instanceof OperatorError ? cause.message : "策展页清单发生未知错误。请重试。");
    } finally {
      if (!controller.signal.aborted) setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadPages(filter);
    return () => request.current?.abort();
  }, [filter, loadPages]);

  const selected = useMemo(
    () => pages.find((page) => pageKey(page) === selectedKey) ?? null,
    [pages, selectedKey],
  );
  const selectedOperation = selectedKey ? operations[selectedKey] : undefined;

  const updateOperation = useCallback(
    (targetKey: string, update: Partial<PageOperation>) => {
      setOperations((current) => ({
        ...current,
        [targetKey]: {
          submitting: current[targetKey]?.submitting ?? false,
          announcement: current[targetKey]?.announcement ?? null,
          ...update,
        },
      }));
    },
    [],
  );

  const focusStatus = useCallback((targetKey: string) => {
    window.requestAnimationFrame(() => {
      if (selectedKeyRef.current === targetKey) statusRef.current?.focus();
    });
  }, []);

  const watchJob = useCallback(
    async (jobId: string, targetKey: string) => {
      poll.current?.abort();
      const controller = new AbortController();
      poll.current = controller;
      try {
        const job = await loadJob(jobId, controller.signal);
        setPages((current) =>
          current.map((page) =>
            pageKey(page) === targetKey && page.enablement
              ? {
                  ...page,
                  enablement: { status: job.status, job_id: jobId, error: job.error },
                }
              : page,
          ),
        );
        if (job.status === "succeeded") {
          updateOperation(targetKey, {
            announcement: "处理完成，页面已进入待处理队列。",
          });
          await loadPages(filter, targetKey);
          focusStatus(targetKey);
          return;
        }
        if (job.status === "failed" || job.status === "cancelled") {
          updateOperation(targetKey, {
            announcement: "处理失败，源页仍保持隐藏且未启用。",
          });
          focusStatus(targetKey);
          return;
        }
        updateOperation(targetKey, {
          announcement:
            job.status === "queued"
              ? job.next_retry_at
                ? "等待自动重试。"
                : "任务已排队，等待处理。"
              : phaseLabels[job.progress?.phase || ""] || "正在处理隐藏页。",
        });
        timer.current = window.setTimeout(() => void watchJob(jobId, targetKey), 700);
      } catch (cause) {
        if (cause instanceof DOMException && cause.name === "AbortError") return;
        updateOperation(targetKey, {
          announcement:
            cause instanceof OperatorError
              ? `${cause.message} 刷新页面可从服务端恢复任务。`
              : "任务状态读取失败。刷新页面可从服务端恢复任务。",
        });
        focusStatus(targetKey);
      }
    },
    [filter, focusStatus, loadPages, updateOperation],
  );

  useEffect(() => {
    const enablement = selected?.enablement;
    if (
      selected &&
      !selected.enabled &&
      enablement?.job_id &&
      (enablement.status === "queued" || enablement.status === "running")
    ) {
      void watchJob(enablement.job_id, pageKey(selected));
    }
    return () => {
      poll.current?.abort();
      if (timer.current !== null) window.clearTimeout(timer.current);
    };
  }, [selected?.enablement?.job_id, selected?.enablement?.status, selected?.enabled, watchJob]);

  const handleEnable = async () => {
    if (!selected || selected.enabled) return;
    const targetKey = pageKey(selected);
    if (operations[targetKey]?.submitting) return;
    updateOperation(targetKey, {
      submitting: true,
      announcement: "正在提交启用请求。",
    });
    focusStatus(targetKey);
    try {
      const accepted = await enableHiddenPage(selected);
      if (accepted.status === "no_change") {
        updateOperation(targetKey, {
          announcement: "页面已由另一会话完成处理，正在恢复最新状态。",
        });
        await loadPages(filter, targetKey);
        focusStatus(targetKey);
      } else if (accepted.job_id) {
        setPages((current) =>
          current.map((page) =>
            pageKey(page) === targetKey && page.enablement
              ? {
                  ...page,
                  enablement: { ...page.enablement, status: "queued", job_id: accepted.job_id },
                }
              : page,
          ),
        );
        updateOperation(targetKey, {
          announcement:
            accepted.status === "coalesced"
              ? "另一会话已启动任务，已接续现有处理。"
              : "任务已排队，等待处理。",
        });
      }
    } catch (cause) {
      updateOperation(targetKey, {
        announcement:
          cause instanceof OperatorError
            ? `${cause.message} 刷新页面可确认任务是否已被接受。`
            : "启用请求未完成。刷新页面可确认任务是否已被接受。",
      });
      focusStatus(targetKey);
    } finally {
      updateOperation(targetKey, { submitting: false });
    }
  };

  if (error) {
    return (
      <main className="curation-load-state">
        <section role="alert" className="error-panel">
          <h1>策展页清单连接中断</h1>
          <p>{error}</p>
          <button type="button" className="secondary-button" onClick={() => void loadPages(filter)}>
            重新连接
          </button>
        </section>
      </main>
    );
  }

  return (
    <main className="curation-workspace" aria-busy={loading}>
      <PageRail
        pages={pages}
        filter={filter}
        selectedKey={selectedKey}
        onFilter={setFilter}
        onSelect={setSelectedKey}
      />
      <EvidencePanel page={selected} />
      <InspectorPanel
        page={selected}
        submitting={selectedOperation?.submitting ?? false}
        announcement={selectedOperation?.announcement ?? null}
        statusRef={statusRef}
        onEnable={() => void handleEnable()}
      />
    </main>
  );
}
