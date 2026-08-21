import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  confirmPageMapping,
  loadPageMapping,
  OperatorError,
  PageMappingConflictError,
  type PageMappingCase,
  type PageMappingDraft,
  type PageMappingWorkspace,
  savePageMappingDecision,
} from "./api";

interface PageMappingWorkbenchProps {
  documentId: string;
  versionId: string;
}

type WorkbenchState =
  | { kind: "loading" }
  | { kind: "error"; message: string }
  | { kind: "ready"; data: PageMappingWorkspace; etag: string };

interface MappingConflict {
  server: PageMappingCase["decision"];
  local: PageMappingDraft;
}

const caseLabels: Record<PageMappingCase["kind"], string> = {
  duplicate_fingerprint: "重复指纹",
  slide_id_conflict: "SlideID 冲突",
  multiple_candidates: "多候选",
};

function decisionCopy(decision: PageMappingCase["decision"] | PageMappingDraft | null): string {
  if (!decision) return "尚未决定";
  if (decision.kind === "new") return "创建新页";
  return `沿用 ${decision.page_id}`;
}

function neighborCopy(candidate: PageMappingCase["candidates"][number]): string {
  const before = candidate.adjacent_confirmed.before;
  const after = candidate.adjacent_confirmed.after;
  if (!before && !after) return "当前没有相邻已确认页";
  return [before ? `前邻第 ${before.source_page_number} 页` : "前邻无", after ? `后邻第 ${after.source_page_number} 页` : "后邻无"].join(" · ");
}

function EvidenceImage({
  url,
  alt,
  onErrorChange,
}: {
  url: string;
  alt: string;
  onErrorChange: (url: string, failed: boolean) => void;
}) {
  const [failed, setFailed] = useState(false);
  const [retry, setRetry] = useState(0);
  useEffect(() => () => onErrorChange(url, false), [onErrorChange, url]);

  if (failed) {
    return (
      <div className="mapping-evidence-error" role="alert">
        <strong>标准页渲染加载失败</strong>
        <span>证据恢复前不能启用版本。</span>
        <button
          type="button"
          onClick={() => {
            setFailed(false);
            setRetry((current) => current + 1);
            onErrorChange(url, false);
          }}
        >
          重新加载此证据
        </button>
      </div>
    );
  }
  const separator = url.includes("?") ? "&" : "?";
  return (
    <img
      src={retry === 0 ? url : `${url}${separator}evidence_retry=${retry}`}
      alt={alt}
      onLoad={() => onErrorChange(url, false)}
      onError={() => {
        setFailed(true);
        onErrorChange(url, true);
      }}
    />
  );
}

export function PageMappingWorkbench({ documentId, versionId }: PageMappingWorkbenchProps) {
  const [state, setState] = useState<WorkbenchState>({ kind: "loading" });
  const [selectedCaseId, setSelectedCaseId] = useState<string | null>(null);
  const [drafts, setDrafts] = useState<Record<string, PageMappingDraft>>({});
  const [saving, setSaving] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [showConfirmation, setShowConfirmation] = useState(false);
  const [conflict, setConflict] = useState<MappingConflict | null>(null);
  const [success, setSuccess] = useState(false);
  const [statusMessage, setStatusMessage] = useState("");
  const [caseFilter, setCaseFilter] = useState<"unresolved" | "all">("unresolved");
  const [evidenceErrors, setEvidenceErrors] = useState<Set<string>>(new Set());
  const confirmAction = useRef<HTMLButtonElement>(null);
  const confirmTrigger = useRef<HTMLButtonElement>(null);
  const dialog = useRef<HTMLElement>(null);

  const load = useCallback(
    async (signal?: AbortSignal) => {
      setState({ kind: "loading" });
      try {
        const result = await loadPageMapping(documentId, versionId, signal);
        setState({ kind: "ready", ...result });
        setSelectedCaseId(
          (current) =>
            current ?? result.data.cases.find((item) => item.status === "unresolved")?.case_id ?? result.data.cases[0]?.case_id ?? null,
        );
      } catch (error) {
        if (error instanceof DOMException && error.name === "AbortError") return;
        setState({
          kind: "error",
          message: error instanceof OperatorError ? error.message : "页对应工作面加载失败。",
        });
      }
    },
    [documentId, versionId],
  );

  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load]);

  const selectedCase =
    state.kind === "ready"
      ? state.data.cases.find((item) => item.case_id === selectedCaseId) ?? state.data.cases[0]
      : undefined;
  const draft = selectedCase
    ? drafts[selectedCase.case_id] ??
      (selectedCase.decision
        ? selectedCase.decision.kind === "reuse" && selectedCase.decision.page_id
          ? { kind: "reuse" as const, page_id: selectedCase.decision.page_id }
          : { kind: "new" as const }
        : undefined)
    : undefined;
  const displayedCandidate = useMemo(() => {
    if (!selectedCase) return undefined;
    if (draft?.kind === "reuse") {
      return selectedCase.candidates.find((candidate) => candidate.page_id === draft.page_id);
    }
    return selectedCase.candidates[0];
  }, [draft, selectedCase]);
  const updateEvidenceError = useCallback((url: string, failed: boolean) => {
    setEvidenceErrors((current) => {
      const next = new Set(current);
      if (failed) next.add(url);
      else next.delete(url);
      return next;
    });
  }, []);
  const hasUnsavedDrafts = Object.keys(drafts).length > 0;
  const canFinalize =
    state.kind === "ready" &&
    state.data.can_confirm &&
    !hasUnsavedDrafts &&
    conflict === null &&
    evidenceErrors.size === 0;

  const save = useCallback(async () => {
    if (state.kind !== "ready" || !selectedCase || !draft || saving) return;
    setSaving(true);
    setConflict(null);
    setStatusMessage("正在保存页对应决定。");
    try {
      const result = await savePageMappingDecision(
        state.data,
        selectedCase.case_id,
        draft,
        state.etag,
      );
      setState({ kind: "ready", ...result });
      setDrafts((current) => {
        const next = { ...current };
        delete next[selectedCase.case_id];
        return next;
      });
      const currentIndex = result.data.cases.findIndex(
        (item) => item.case_id === selectedCase.case_id,
      );
      const nextCase = [
        ...result.data.cases.slice(currentIndex + 1),
        ...result.data.cases.slice(0, currentIndex + 1),
      ].find((item) => item.status === "unresolved");
      setSelectedCaseId(nextCase?.case_id ?? selectedCase.case_id);
      setStatusMessage(
        result.data.remaining_cases === 0
          ? "全部决定已保存，请执行最终复核。"
          : "决定已保存，已前往下一项。",
      );
    } catch (error) {
      if (error instanceof PageMappingConflictError) {
        try {
          const latest = await loadPageMapping(documentId, versionId);
          const serverCase = latest.data.cases.find(
            (item) => item.case_id === selectedCase.case_id,
          );
          setState({ kind: "ready", ...latest });
          setSelectedCaseId(selectedCase.case_id);
          setConflict({ server: serverCase?.decision ?? null, local: draft });
          setStatusMessage("检测到并发更新，需要重新确认当前选择。");
        } catch (reloadError) {
          setStatusMessage(
            reloadError instanceof OperatorError
              ? reloadError.message
              : "并发更新后无法重新加载，请刷新工作面。",
          );
        }
      } else {
        setStatusMessage(error instanceof OperatorError ? error.message : "决定未保存，请重试。");
      }
    } finally {
      setSaving(false);
    }
  }, [documentId, draft, saving, selectedCase, state, versionId]);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      if (target?.matches("input, textarea, select, [contenteditable='true']")) return;
      if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
        event.preventDefault();
        void save();
      } else if (!event.ctrlKey && !event.metaKey && event.key.toLowerCase() === "r") {
        event.preventDefault();
        if (hasUnsavedDrafts || conflict) {
          setStatusMessage("存在未保存选择，请先保存或切换后再刷新。");
        } else {
          void load();
        }
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [conflict, hasUnsavedDrafts, load, save]);

  useEffect(() => {
    const onBeforeUnload = (event: BeforeUnloadEvent) => {
      if (Object.keys(drafts).length === 0) return;
      event.preventDefault();
    };
    window.addEventListener("beforeunload", onBeforeUnload);
    return () => window.removeEventListener("beforeunload", onBeforeUnload);
  }, [drafts]);

  useEffect(() => {
    if (!showConfirmation) return;
    confirmAction.current?.focus();
    const background = Array.from(
      document.querySelectorAll<HTMLElement>(
        ".app-shell > .topbar, .app-shell > .command-strip, .mapping-header, .mapping-body, .mapping-command-zone",
      ),
    );
    const existingInert = background.map((element) => element.hasAttribute("inert"));
    background.forEach((element) => element.setAttribute("inert", ""));
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !confirmAction.current?.disabled) {
        setShowConfirmation(false);
        return;
      }
      if (event.key !== "Tab" || !dialog.current) return;
      const focusable = Array.from(
        dialog.current.querySelectorAll<HTMLElement>("button:not(:disabled), [href]"),
      );
      if (focusable.length === 0) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => {
      window.removeEventListener("keydown", onKeyDown);
      background.forEach((element, index) => {
        if (!existingInert[index]) element.removeAttribute("inert");
      });
      confirmTrigger.current?.focus();
    };
  }, [showConfirmation]);

  const confirm = async () => {
    if (state.kind !== "ready" || !canFinalize || confirming) return;
    setConfirming(true);
    setStatusMessage("正在执行最终原子检查并启用版本。");
    try {
      await confirmPageMapping(state.data, state.etag);
      setShowConfirmation(false);
      setSuccess(true);
      setStatusMessage("新版本已启用");
    } catch (error) {
      setShowConfirmation(false);
      setStatusMessage(error instanceof OperatorError ? error.message : "最终确认未完成，请重试。");
    } finally {
      setConfirming(false);
    }
  };

  if (state.kind === "loading") {
    return (
      <main className="mapping-loading" aria-busy="true" aria-live="polite">
        <span className="mapping-loading-line shimmer" />
        <p>正在装载页对应证据…</p>
      </main>
    );
  }

  if (state.kind === "error") {
    return (
      <main className="mapping-error">
        <section role="alert">
          <h1>页对应工作面未能加载</h1>
          <p>{state.message}</p>
          <button type="button" onClick={() => void load()}>
            重新加载
          </button>
        </section>
      </main>
    );
  }

  if (!selectedCase) {
    return (
      <main className="mapping-error">
        <section>
          <h1>页对应</h1>
          <p>该版本没有需要人工处理的页对应项。</p>
        </section>
      </main>
    );
  }

  const impact = state.data.impact_summary;
  const visibleCases = state.data.cases.filter(
    (item) => caseFilter === "all" || item.status === "unresolved",
  );
  return (
    <main className="mapping-workbench">
      <header className="mapping-header">
        <div>
          <a href="/documents">返回文档</a>
          <h1>页对应</h1>
          <span className="mapping-file">{state.data.source_filename}</span>
        </div>
        <div className="mapping-version-gate">
          <span className="mapping-remaining">剩余 {state.data.remaining_cases} 项</span>
          {state.data.current_version.still_serving ? (
            <a
              className="mapping-old-version"
              href={`/curation?version=${state.data.current_version.version_id}`}
            >
              旧版本仍在服务
            </a>
          ) : null}
        </div>
      </header>

      <section className="mapping-body">
        <aside className="mapping-case-rail" aria-label="页对应项">
          <div className="mapping-rail-heading">
            <strong>对应队列</strong>
            <div className="mapping-case-filters" aria-label="筛选对应项">
              <button
                type="button"
                aria-pressed={caseFilter === "unresolved"}
                onClick={() => setCaseFilter("unresolved")}
              >
                未决
              </button>
              <button
                type="button"
                aria-pressed={caseFilter === "all"}
                onClick={() => setCaseFilter("all")}
              >
                全部
              </button>
            </div>
          </div>
          <div className="mapping-case-list" role="list">
            {visibleCases.map((item) => (
              <div role="listitem" key={item.case_id}>
                <button
                  type="button"
                  className={`mapping-case-row${item.case_id === selectedCase.case_id ? " is-current" : ""}`}
                  onClick={() => {
                    setSelectedCaseId(item.case_id);
                    setConflict(null);
                  }}
                >
                  <span className="mapping-case-number">{item.source_page.page_number}</span>
                  <span>
                    <strong>{caseLabels[item.kind]}</strong>
                    <small>{item.status === "saved" ? decisionCopy(item.decision) : "等待决定"}</small>
                  </span>
                  <span className={`mapping-case-state mapping-case-state--${item.status}`}>
                    {item.status === "saved" ? "已保存" : "未决定"}
                  </span>
                </button>
              </div>
            ))}
            {visibleCases.length === 0 ? (
              <div className="mapping-case-empty">
                <span>没有未决项</span>
                <button type="button" onClick={() => setCaseFilter("all")}>查看全部</button>
              </div>
            ) : null}
          </div>
        </aside>

        <section className="mapping-evidence" aria-label="同步证据比较">
          <div className="mapping-evidence-context">
            <span>新版本源页 {selectedCase.source_page.page_number}</span>
            <span>SlideID {selectedCase.source_page.slide_id}</span>
            <span>指纹 {selectedCase.source_page.fingerprint.sha256.slice(0, 10)}</span>
          </div>
          <div className="mapping-compare-stage">
            <figure>
              <figcaption>
                <strong>新版本源页</strong>
                <span>第 {selectedCase.source_page.page_number} 页</span>
              </figcaption>
              <div className="mapping-render-frame">
                <EvidenceImage
                  key={selectedCase.source_page.standard_render.url}
                  url={selectedCase.source_page.standard_render.url}
                  alt={`新版本第 ${selectedCase.source_page.page_number} 页标准页渲染结果`}
                  onErrorChange={updateEvidenceError}
                />
              </div>
            </figure>
            <figure>
              <figcaption>
                <strong>历史候选</strong>
                <span>{displayedCandidate ? `第 ${displayedCandidate.page_number} 页` : "无候选"}</span>
              </figcaption>
              <div className="mapping-render-frame">
                {displayedCandidate ? (
                  <EvidenceImage
                    key={displayedCandidate.standard_render.url}
                    url={displayedCandidate.standard_render.url}
                    alt={`历史候选第 ${displayedCandidate.page_number} 页标准页渲染结果`}
                    onErrorChange={updateEvidenceError}
                  />
                ) : (
                  <div className="mapping-no-candidate">
                    <strong>没有历史候选</strong>
                    <span>保守选择是创建新页，不继承任何历史审核。</span>
                  </div>
                )}
              </div>
            </figure>
          </div>
          {displayedCandidate ? (
            <div className="mapping-neighbor-strip">
              <span>{neighborCopy(displayedCandidate)}</span>
              <span>
                相对顺序差 {displayedCandidate.relative_order.delta > 0 ? "+" : ""}
                {displayedCandidate.relative_order.delta}
              </span>
            </div>
          ) : null}
        </section>

        <aside className="mapping-decision-panel">
          <div className="mapping-decision-heading">
            <div>
              <strong>选择稳定身份</strong>
              <span>{caseLabels[selectedCase.kind]}</span>
            </div>
            <span>第 {selectedCase.source_page.page_number} 页</span>
          </div>

          {conflict ? (
            <section className="mapping-conflict" role="alert">
              <strong>其他会话更新了这一工作面</strong>
              <p>系统没有覆盖你的选择。请比较后重新保存。</p>
              <div>
                <span>服务器当前决定</span>
                <b>{decisionCopy(conflict.server)}</b>
              </div>
              <div>
                <span>你的未保存选择</span>
                <b className="mapping-conflict-choice">{decisionCopy(conflict.local)}</b>
              </div>
            </section>
          ) : null}

          <fieldset className="mapping-options">
            <legend>历史候选与新身份</legend>
            {selectedCase.candidates.map((candidate) => {
              const occupied =
                candidate.occupied_by_case_id !== null &&
                candidate.occupied_by_case_id !== selectedCase.case_id;
              const occupyingCase = state.data.cases.find(
                (item) => item.case_id === candidate.occupied_by_case_id,
              );
              return (
                <div key={candidate.page_id} className="mapping-candidate-option">
                  <label className={occupied ? "is-occupied" : ""}>
                    <input
                      type="radio"
                      name={`mapping-${selectedCase.case_id}`}
                      checked={draft?.kind === "reuse" && draft.page_id === candidate.page_id}
                      disabled={occupied}
                      onChange={() => {
                        setDrafts((current) => ({
                          ...current,
                          [selectedCase.case_id]: { kind: "reuse", page_id: candidate.page_id },
                        }));
                        setConflict(null);
                      }}
                      aria-label={`沿用历史页 ${candidate.page_id}`}
                    />
                    <span className="mapping-option-copy">
                      <strong>沿用历史页</strong>
                      <code>{candidate.page_id}</code>
                      <small>
                        原第 {candidate.page_number} 页 · SlideID {candidate.slide_id} · {candidate.review_status}
                      </small>
                      <small>{occupied ? (occupyingCase ? `已被第 ${occupyingCase.source_page.page_number} 页占用` : "已被确定性对应占用") : neighborCopy(candidate)}</small>
                    </span>
                    <span className={`mapping-relation mapping-relation--${candidate.fingerprint_relation}`}>
                      {candidate.fingerprint_relation === "same" ? "内容未变" : "内容变化"}
                    </span>
                  </label>
                  {occupied && occupyingCase ? (
                    <button
                      type="button"
                      className="mapping-occupied-jump"
                      onClick={() => {
                        setSelectedCaseId(occupyingCase.case_id);
                        setConflict(null);
                      }}
                    >
                      前往占用项
                    </button>
                  ) : null}
                </div>
              );
            })}
            <label className="mapping-new-option">
              <input
                type="radio"
                name={`mapping-${selectedCase.case_id}`}
                checked={draft?.kind === "new"}
                onChange={() => {
                  setDrafts((current) => ({
                    ...current,
                    [selectedCase.case_id]: { kind: "new" },
                  }));
                  setConflict(null);
                }}
                aria-label="创建新页"
              />
              <span className="mapping-option-copy">
                <strong>创建新页</strong>
                <small>分配新的 page_id 与 chunk_id</small>
              </span>
              <span className="mapping-relation">保守</span>
            </label>
          </fieldset>

          <div className="mapping-consequence" aria-live="polite">
            {!draft ? (
              <span>选择一项后，这里会说明身份与审核影响。</span>
            ) : draft.kind === "new" ? (
              <span>创建独立身份；不会继承历史审核，页面进入 pending。</span>
            ) : displayedCandidate?.fingerprint_relation === "same" ? (
              <span>保留 page_id 与 chunk_id，并继承该页最近的相同内容审核。</span>
            ) : (
              <span>保留 page_id 与 chunk_id；内容变化，页面回到 pending，仅提供待确认预填。</span>
            )}
          </div>

          <button
            type="button"
            className="mapping-save-button"
            disabled={!draft || saving || success}
            onClick={() => void save()}
          >
            {saving ? "正在保存…" : "保存决定并查看下一项"}
          </button>
        </aside>
      </section>

      <section className="mapping-command-zone" aria-label="页对应门禁">
        <div aria-live="polite">
          <span className={success ? "is-success" : ""}>
            {success ? "新版本已启用" : statusMessage || "决定只在明确保存后写入。"}
          </span>
          <small>
            <kbd>Ctrl</kbd> / <kbd>⌘</kbd> + <kbd>Enter</kbd> 保存并前进
          </small>
        </div>
        {state.data.remaining_cases === 0 ? (
          <button
            ref={confirmTrigger}
            type="button"
            className="mapping-confirm-button"
            disabled={!canFinalize || confirming || success}
            onClick={() => setShowConfirmation(true)}
          >
            确认全部对应并启用版本
          </button>
        ) : (
          <span className="mapping-gate-copy">全部 case 保存后开放最终确认</span>
        )}
      </section>

      {showConfirmation ? (
        <div className="mapping-dialog-backdrop">
          <section ref={dialog} className="mapping-confirm-dialog" role="dialog" aria-modal="true" aria-labelledby="mapping-confirm-title">
            <h2 id="mapping-confirm-title">确认全部对应并启用版本</h2>
            <p>系统将再次原子检查全部决定，并只在检查成功后切换当前版本。</p>
            <dl>
              <div><dt>沿用且内容未变</dt><dd>{impact.reused_unchanged} 页</dd></div>
              <div><dt>沿用但内容变化</dt><dd>{impact.reused_changed} 页</dd></div>
              <div><dt>创建新身份</dt><dd>{impact.created_new} 页</dd></div>
              <div><dt>缺席并软删除</dt><dd>{impact.soft_deleted} 页</dd></div>
              <div><dt>未决 / 冲突 / 证据失败</dt><dd>{impact.unresolved + impact.save_conflicts + impact.evidence_errors + evidenceErrors.size} 项</dd></div>
            </dl>
            <div className="mapping-freeze-warning">
              <strong>对应关系将被冻结</strong>
              <span>冻结后不能直接修改；纠错必须作废版本并重新摄取。</span>
            </div>
            <div className="mapping-dialog-actions">
              <button type="button" onClick={() => setShowConfirmation(false)}>返回检查</button>
              <button
                ref={confirmAction}
                type="button"
                className="mapping-confirm-button"
                disabled={confirming}
                onClick={() => void confirm()}
              >
                {confirming ? "确认中…" : "确认并启用"}
              </button>
            </div>
          </section>
        </div>
      ) : null}
    </main>
  );
}
