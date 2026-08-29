import { useCallback, useEffect, useRef, useState } from "react";

import {
  confirmPublicationCandidate,
  createPublicationCandidate,
  loadPublicationWorkspace,
  OperatorError,
  retryPublicationTask,
  type PublicationArtifact,
  type PublicationCandidate,
  type PublicationConfirmation,
  type PublicationTask,
  type PublicationWorkspace,
} from "./api";

type LoadState =
  | { kind: "loading" }
  | { kind: "ready"; data: PublicationWorkspace }
  | { kind: "error"; message: string };

type ActionKind = "create" | "confirm" | "retry" | null;

const taskStages = [
  { id: "frozen_input", label: "冻结输入已锁定", detail: "确切 Chunk、策展快照与视觉资产" },
  { id: "build", label: "构建完整 ZIP", detail: "manifest、Chunk JSONL 与引用资产" },
  { id: "validate", label: "完整性校验", detail: "Schema、哈希、ID 与资产引用" },
  { id: "switch_pointer", label: "切换当前指针", detail: "仅在全部校验成功后执行" },
] as const;

const taskPhaseState: Record<string, { index: number; activeDetail: string }> = {
  frozen_input: { index: 0, activeDetail: "正在锁定冻结清单" },
  build: { index: 1, activeDetail: "正在生成完整 ZIP" },
  validate: { index: 2, activeDetail: "正在校验 Schema、哈希、ID 与资产引用" },
  store: { index: 3, activeDetail: "正在写入不可变对象存储" },
  switch_pointer: { index: 3, activeDetail: "正在原子切换当前指针" },
};

function formatDate(value: string | null): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(date);
}

function formatBytes(value: number): string {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / 1024 / 1024).toFixed(1)} MB`;
}

function CurrentArtifactStrip({ artifact }: { artifact: PublicationArtifact | null }) {
  if (!artifact) {
    return (
      <section className="publication-current is-empty" aria-labelledby="current-artifact-title">
        <div>
          <h1 id="current-artifact-title">首次发布尚未建立</h1>
          <p>完整产物通过校验后，发布序号、哈希和下载入口会固定在这里。</p>
        </div>
        <span className="artifact-safety">当前无可下载产物</span>
      </section>
    );
  }
  return (
    <section className="publication-current" aria-labelledby="current-artifact-title">
      <div className="artifact-identity">
        <span className="artifact-seq">#{artifact.publication_seq}</span>
        <div>
          <h1 id="current-artifact-title">当前产物</h1>
          <p>已完整校验并原子切换</p>
        </div>
      </div>
      <dl className="artifact-facts">
        <div><dt>快照</dt><dd>{artifact.snapshot_id}</dd></div>
        <div><dt>发布时间</dt><dd>{formatDate(artifact.published_at)}</dd></div>
        <div><dt>内容</dt><dd>{artifact.chunk_count} Chunk · {artifact.asset_count} 资产</dd></div>
        <div><dt>ZIP</dt><dd>{formatBytes(artifact.size_bytes)}</dd></div>
        <div className="artifact-hash"><dt>SHA-256</dt><dd>{artifact.sha256}</dd></div>
      </dl>
      <a className="artifact-download" href={artifact.download_url}>下载当前 ZIP</a>
    </section>
  );
}

function CandidateLedger({ candidate }: { candidate: PublicationCandidate | null }) {
  const [expanded, setExpanded] = useState<Set<string>>(new Set());

  useEffect(() => setExpanded(new Set()), [candidate?.candidate_id]);

  if (!candidate) {
    return (
      <section className="candidate-ledger candidate-ledger--empty" aria-labelledby="candidate-title">
        <header>
          <div>
            <h2 id="candidate-title">候选范围账本</h2>
            <p>尚未捕获一致业务视图</p>
          </div>
          <span className="ledger-status">等待创建</span>
        </header>
        <div className="ledger-empty-copy">
          <strong>先通过右侧前置校验，再由人创建候选。</strong>
          <p>创建后可在这里逐层核验文档、当前版本和将进入完整产物的 approved 页。</p>
        </div>
      </section>
    );
  }

  const allExpanded = candidate.documents.length > 0 && expanded.size === candidate.documents.length;
  const setAll = (open: boolean) => {
    setExpanded(open ? new Set(candidate.documents.map((document) => document.document_id)) : new Set());
  };

  return (
    <section className="candidate-ledger" aria-labelledby="candidate-title">
      <header>
        <div>
          <h2 id="candidate-title">候选范围账本</h2>
          <p><span className="mono">{candidate.candidate_id}</span> · {formatDate(candidate.created_at)}</p>
        </div>
        <span className={`ledger-status is-${candidate.status}`}>
          {candidate.status === "ready" ? "待确认" : candidate.status === "stale" ? "已失效" :
            candidate.status === "no_change" ? "无变化" : candidate.status === "failed" ? "构建失败" :
              candidate.status === "succeeded" ? "已发布" : "已冻结"}
        </span>
      </header>

      <div className="candidate-diff" aria-label="相对当前产物的差异">
        <span><strong>{candidate.diff.added}</strong>新增 {candidate.diff.added}</span>
        <span><strong>{candidate.diff.updated}</strong>更新 {candidate.diff.updated}</span>
        <span><strong>{candidate.diff.removed}</strong>移除 {candidate.diff.removed}</span>
        <span><strong>{candidate.diff.unchanged}</strong>不变 {candidate.diff.unchanged}</span>
      </div>

      <div className="candidate-exclusions" aria-label="未纳入摘要">
        <span>pending {candidate.excluded.pending_pages} 页不纳入</span>
        <span>excluded {candidate.excluded.excluded_pages} 页</span>
        <span>隐藏未启用 {candidate.excluded.disabled_hidden_pages} 页</span>
        <span>软删文档 {candidate.excluded.soft_deleted_documents} 个</span>
      </div>

      <div className="ledger-toolbar">
        <div>
          <strong>{candidate.chunk_count} 个 Chunk</strong>
          <span>{candidate.asset_count} 个被引用视觉资产</span>
        </div>
        <button type="button" onClick={() => setAll(!allExpanded)}>
          {allExpanded ? "全部收起" : "全部展开"}
        </button>
      </div>

      <div className="document-scope-list">
        {candidate.documents.length ? candidate.documents.map((document) => {
          const open = expanded.has(document.document_id);
          return (
            <details key={document.document_id} open={open}>
              <summary onClick={(event) => {
                event.preventDefault();
                setExpanded((current) => {
                  const next = new Set(current);
                  if (next.has(document.document_id)) next.delete(document.document_id);
                  else next.add(document.document_id);
                  return next;
                });
              }}>
                <span className="scope-chevron" aria-hidden="true" />
                <span>
                  <strong>{document.title}</strong>
                  <small>{document.document_id} · 当前版本 {document.version_id}</small>
                </span>
                <span>{document.pages.length} 页</span>
              </summary>
              <div className="scope-table-wrap">
                <table>
                  <thead><tr><th>页</th><th>标题与身份</th><th>审核来源</th><th>差异</th></tr></thead>
                  <tbody>
                    {document.pages.map((page) => (
                      <tr key={page.page_id}>
                        <td className="page-number">{String(page.page_number).padStart(2, "0")}</td>
                        <td><strong>{page.title}</strong><small>{page.page_id}<br />{page.chunk_id}</small></td>
                        <td><span>{page.reviewed_by}</span><small>{formatDate(page.reviewed_at)}<br />{page.snapshot_id}</small></td>
                        <td><span className={`change-mark is-${page.change}`}>{page.change === "added" ? "新增" : page.change === "updated" ? "更新" : "不变"}</span></td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </details>
          );
        }) : <p className="scope-empty">当前候选不含 approved 页；确认后将生成空内容集合。</p>}
      </div>
    </section>
  );
}

function PreflightCheck({ workspace }: { workspace: PublicationWorkspace }) {
  const { preflight } = workspace;
  const blocked = !preflight.can_publish;
  return (
    <section className={`control-section preflight-control ${blocked ? "is-blocked" : "is-clear"}`}>
      <header><h3>发布前置校验</h3><span>{blocked ? "发布被阻止" : "通过"}</span></header>
      <div className="control-check">
        <span className="control-check-mark" aria-hidden="true" />
        <div>
          <strong>渲染警告确认</strong>
          <p>{preflight.stale_render_versions > 0
            ? `${preflight.stale_render_versions} 个版本正在重建`
            : blocked
              ? `${preflight.summary.unconfirmed_pages} 页 / ${preflight.summary.unconfirmed} 条未确认`
              : preflight.summary.total
                ? `${preflight.summary.total} 条警告均已确认`
                : "当前内容未发现渲染警告"}</p>
        </div>
      </div>
      {blocked && preflight.href ? <a href={preflight.href}>前往确认渲染警告</a> : null}
    </section>
  );
}

function TaskRail({ task }: { task: PublicationTask }) {
  const phase = task.progress?.phase ?? "frozen_input";
  const phaseState = taskPhaseState[phase];
  const currentIndex = phase === "succeeded" ? taskStages.length : phaseState?.index ?? null;
  return (
    <>
      <ol className="publication-stage-list" aria-label="发布任务阶段">
        {taskStages.map((stage, index) => {
          const complete = task.status === "succeeded" || (currentIndex !== null && index < currentIndex);
          const active = task.status !== "failed" && !complete && index === currentIndex;
          const failed = task.status === "failed" && index === currentIndex;
          return (
            <li key={stage.id} className={complete ? "is-complete" : active ? "is-active" : failed ? "is-failed" : ""}>
              <span className="stage-node" aria-hidden="true" />
              <div><strong>{stage.label}</strong><small>{failed ? "在此阶段失败" : complete ? "已完成" : active ? phaseState?.activeDetail : stage.detail}</small></div>
            </li>
          );
        })}
      </ol>
      {currentIndex === null ? <p className="unknown-task-phase" role="status">未知任务阶段：{phase}</p> : null}
    </>
  );
}

function PublicationControl({
  workspace,
  action,
  confirmOpen,
  setConfirmOpen,
  onCreate,
  onConfirm,
  onRetry,
}: {
  workspace: PublicationWorkspace;
  action: ActionKind;
  confirmOpen: boolean;
  setConfirmOpen: (open: boolean) => void;
  onCreate: () => void;
  onConfirm: () => void;
  onRetry: () => void;
}) {
  const { candidate, task, current } = workspace;
  const active = task?.status === "queued" || task?.status === "running";
  const pageCount = candidate?.documents.reduce((total, document) => total + document.pages.length, 0) ?? 0;

  return (
    <aside className="publication-control" aria-label="发布控制轨">
      <PreflightCheck workspace={workspace} />

      {!candidate || candidate.status === "stale" ? (
        <section className="control-section candidate-control">
          <header><h3>{candidate?.status === "stale" ? "候选已失效" : "创建发布候选"}</h3></header>
          <p>{candidate?.status === "stale"
            ? "业务状态已变化，旧候选不能继续确认。重新捕获当前一致视图。"
            : "由人显式捕获当前一致业务视图；策展状态变化不会自动发起发布。"}</p>
          <button type="button" disabled={!workspace.preflight.can_publish || action !== null} onClick={onCreate}>
            {action === "create" ? "正在创建候选" : candidate ? "重新创建发布候选" : "创建发布候选"}
          </button>
        </section>
      ) : null}

      {candidate && candidate.status === "ready" ? (
        <section className="control-section candidate-control">
          <header><h3>候选待确认</h3><span>范围可核验</span></header>
          <dl>
            <div><dt>候选</dt><dd>{candidate.candidate_id}</dd></div>
            <div><dt>业务状态</dt><dd>{candidate.business_state_token}</dd></div>
          </dl>
          <button type="button" onClick={() => setConfirmOpen(!confirmOpen)} aria-expanded={confirmOpen}>
            确认发布
          </button>
          {confirmOpen ? (
            <div className="inline-confirmation" role="region" aria-label="最终确认">
              <strong>冻结候选 {candidate.candidate_id}</strong>
              <p>{candidate.documents.length} 个文档 · {pageCount} 页</p>
              <p>{candidate.chunk_count} 个 Chunk · {candidate.asset_count} 个视觉资产</p>
              <p className="confirmation-baseline">{current
                ? `构建切换前继续保留当前产物 #${current.publication_seq} · SHA-256 ${current.sha256}`
                : "构建切换前仍无当前产物"}</p>
              <p>确认后输入保持只读；后续策展变化不会进入本次构建。</p>
              <div>
                <button type="button" className="quiet-action" onClick={() => setConfirmOpen(false)}>返回核验</button>
                <button type="button" disabled={action !== null} onClick={onConfirm}>
                  {action === "confirm" ? "正在冻结输入" : "确认并开始构建"}
                </button>
              </div>
            </div>
          ) : null}
        </section>
      ) : null}

      {candidate?.status === "no_change" ? (
        <section className="control-section no-change-control">
          <header><h3>内容集合无变化</h3><span>no_change</span></header>
          <p>未生成重复 ZIP，也未递增发布序号。当前产物保持不变。</p>
          <button type="button" disabled={!workspace.preflight.can_publish || action !== null} onClick={onCreate}>重新检查当前范围</button>
        </section>
      ) : null}

      {task ? (
        <section className={`control-section task-control is-${task.status}`}>
          <header>
            <h3>{task.status === "failed" ? "本次任务失败" : task.status === "succeeded" ? "发布完成" : "构建与切换"}</h3>
            <span>{candidate?.publication_seq ? `发布序号 #${candidate.publication_seq}` : task.status}</span>
          </header>
          <TaskRail task={task} />
          {task.status === "failed" ? (
            <div className="task-failure">
              <strong>{current ? `当前产物仍为 #${current.publication_seq}` : "当前仍无发布产物"}</strong>
              <p>{task.error?.message ?? "构建未完成；当前产物保持不变。"}</p>
              <button type="button" disabled={action !== null} onClick={onRetry}>
                {action === "retry" ? "正在复用原冻结输入" : "使用原冻结输入重试"}
              </button>
            </div>
          ) : active ? (
            <p className="frozen-proof">候选摘要已冻结，轮询只更新任务进度。</p>
          ) : null}
        </section>
      ) : null}
    </aside>
  );
}

export function PublicationPreflight() {
  const [state, setState] = useState<LoadState>({ kind: "loading" });
  const [action, setAction] = useState<ActionKind>(null);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [announcement, setAnnouncement] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const request = useRef<AbortController | null>(null);
  const errorRef = useRef<HTMLDivElement | null>(null);

  const load = useCallback((showLoading = true) => {
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    if (showLoading) setState({ kind: "loading" });
    loadPublicationWorkspace(controller.signal)
      .then((data) => setState({ kind: "ready", data }))
      .catch((cause) => {
        if (cause instanceof DOMException && cause.name === "AbortError") return;
        setState({
          kind: "error",
          message: cause instanceof OperatorError ? cause.message : "发布台账发生未知错误，请重试。",
        });
      });
  }, []);

  useEffect(() => {
    load();
    return () => request.current?.abort();
  }, [load]);

  useEffect(() => {
    if (actionError) errorRef.current?.focus();
  }, [actionError]);

  const workspace = state.kind === "ready" ? state.data : null;
  useEffect(() => {
    if (!workspace || (workspace.task?.status !== "queued" && workspace.task?.status !== "running")) return;
    const timer = window.setTimeout(() => load(false), 1500);
    return () => window.clearTimeout(timer);
  }, [load, workspace]);

  const applyConfirmation = (confirmation: PublicationConfirmation, retrying: boolean) => {
    if (!workspace?.candidate) return;
    if (confirmation.status === "no_change") {
      setState({ kind: "ready", data: { ...workspace, candidate: { ...workspace.candidate, status: "no_change" }, task: null } });
      setAnnouncement("内容集合无变化；未生成 ZIP，当前产物保持不变。");
      return;
    }
    const task: PublicationTask = {
      job_id: confirmation.job_id ?? "",
      status: "queued",
      progress: { phase: "frozen_input", completed_pages: 0, total_pages: workspace.candidate.chunk_count },
      error: null,
      attempts: retrying ? (workspace.task?.attempts ?? 0) + 1 : 0,
      updated_at: new Date().toISOString(),
    };
    setState({
      kind: "ready",
      data: {
        ...workspace,
        candidate: {
          ...workspace.candidate,
          status: "confirmed",
          publication_seq: confirmation.publication_seq,
          frozen_input_hash: confirmation.frozen_input_hash ?? workspace.candidate.frozen_input_hash,
        },
        task,
      },
    });
    setConfirmOpen(false);
    setAnnouncement(retrying ? "正在复用原冻结输入。" : "候选输入已冻结，构建任务已排队。");
  };

  const runAction = async (kind: Exclude<ActionKind, null>, command: () => Promise<void>) => {
    setAction(kind);
    setActionError(null);
    setAnnouncement(null);
    try {
      await command();
    } catch (cause) {
      setActionError(cause instanceof OperatorError ? cause.message : "操作未完成，请重试。");
      if (kind !== "retry") load(false);
    } finally {
      setAction(null);
    }
  };

  if (state.kind === "loading") {
    return (
      <main className="publication-workspace" aria-busy="true">
        <section className="publication-loading"><h1>冻结发布台账</h1><p>正在恢复当前产物、候选与任务…</p></section>
      </main>
    );
  }

  if (state.kind === "error") {
    return (
      <main className="publication-workspace">
        <section className="publication-loading is-error" role="alert">
          <h1>冻结发布台账</h1><strong>台账连接中断</strong><p>{state.message}</p>
          <button type="button" onClick={() => load()}>重新连接</button>
        </section>
      </main>
    );
  }

  const data = state.data;
  return (
    <main className="publication-workspace">
      <CurrentArtifactStrip artifact={data.current} />
      {actionError ? <div className="publication-action-error" role="alert" tabIndex={-1} ref={errorRef}><strong>操作未完成</strong><span>{actionError}</span></div> : null}
      <div className="publication-layout">
        <CandidateLedger candidate={data.candidate} />
        <PublicationControl
          workspace={data}
          action={action}
          confirmOpen={confirmOpen}
          setConfirmOpen={setConfirmOpen}
          onCreate={() => void runAction("create", async () => {
            const created = await createPublicationCandidate();
            setState({ kind: "ready", data: { ...data, candidate: created, task: null } });
            setAnnouncement(`发布候选 ${created.candidate_id} 已创建，请核验范围。`);
          })}
          onConfirm={() => void runAction("confirm", async () => {
            if (!data.candidate) return;
            applyConfirmation(await confirmPublicationCandidate(data.candidate.candidate_id), false);
          })}
          onRetry={() => void runAction("retry", async () => {
            if (!data.task) return;
            applyConfirmation(await retryPublicationTask(data.task.job_id), true);
          })}
        />
      </div>
      {announcement ? <p className="sr-only" role="status" aria-live="polite">{announcement}</p> : null}
    </main>
  );
}
