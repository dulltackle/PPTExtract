import { type RefObject, useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  approveCurationPage,
  type CurationBlocker,
  type CurationState,
  completeCurationSourceReview,
  confirmCurationSource,
  type CurationPage,
  loadPageDetail,
  OperatorError,
  type PageDetail,
  saveCurationSnapshot,
  type SourceContent,
} from "./api";

function normalizedSource(source: Partial<SourceContent>): SourceContent {
  return {
    titles: source.titles ?? [],
    body: source.body ?? [],
    tables: source.tables ?? [],
    images: source.images ?? [],
    speaker_notes: source.speaker_notes ?? [],
  };
}

function fallbackCuration(source: SourceContent): CurationState {
  const hasText = [
    ...source.titles,
    ...source.body,
    ...source.speaker_notes,
  ].some((value) => value.trim()) || source.tables.length > 0;
  const imageCount = source.images.length;
  return {
    current_snapshot: null,
    image_sources: { total: imageCount, unresolved: imageCount },
    chunk_body: { nonempty: hasText },
    blockers: [
      { code: "source_unsaved", message: "文字修改尚未保存。" },
      { code: "source_unconfirmed", message: "文字来源尚未确认。" },
      { code: "source_review_incomplete", message: "来源审核尚未完成。" },
      ...(imageCount
        ? [{ code: "image_sources_unresolved" as const, message: `${imageCount} 个图片来源尚待逐项处置。` }]
        : []),
      ...(!hasText
        ? [{ code: "chunk_body_empty" as const, message: "已确认来源无法生成非空 Chunk 正文。" }]
        : []),
    ],
    can_confirm_source: false,
    can_complete_source_review: false,
    can_approve: false,
  };
}

function formatTime(value: string): string {
  return new Intl.DateTimeFormat("zh-CN", {
    dateStyle: "short",
    timeStyle: "short",
  }).format(new Date(value));
}

function localBlockers(
  source: SourceContent,
  titles: string[],
  body: string[],
): CurationBlocker[] {
  const imageCount = source.images.length;
  const nonempty = [
    ...titles,
    ...body,
    ...source.speaker_notes,
  ].some((value) => value.trim()) || source.tables.length > 0;
  return [
    { code: "source_unsaved", message: "文字修改尚未保存。" },
    { code: "source_unconfirmed", message: "文字来源尚未确认。" },
    { code: "source_review_incomplete", message: "来源审核尚未完成。" },
    ...(imageCount
      ? [{ code: "image_sources_unresolved" as const, message: `${imageCount} 个图片来源尚待逐项处置。` }]
      : []),
    ...(!nonempty
      ? [{ code: "chunk_body_empty" as const, message: "已确认来源无法生成非空 Chunk 正文。" }]
      : []),
  ];
}

function PhaseStatus({ complete, children }: { complete: boolean; children: string }) {
  return (
    <span className={`source-phase-status ${complete ? "is-complete" : "is-pending"}`}>
      <span aria-hidden="true" />
      {children}
    </span>
  );
}

export function SourceReviewLog({
  page,
  arrivalAnnouncement,
  statusRef,
  onDirtyChange,
  onApproved,
}: {
  page: CurationPage;
  arrivalAnnouncement: string | null;
  statusRef: RefObject<HTMLDivElement | null>;
  onDirtyChange: (dirty: boolean) => void;
  onApproved: () => Promise<void>;
}) {
  const [detail, setDetail] = useState<PageDetail | null>(null);
  const [titles, setTitles] = useState<string[]>([]);
  const [body, setBody] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [retryRevision, setRetryRevision] = useState(0);
  const [operation, setOperation] = useState<"save" | "confirm" | "review" | "approve" | null>(null);
  const [focusTarget, setFocusTarget] = useState<"confirm" | "review" | "approve" | null>(null);
  const [announcement, setAnnouncement] = useState<string | null>(arrivalAnnouncement);
  const firstFieldRef = useRef<HTMLTextAreaElement>(null);
  const confirmRef = useRef<HTMLButtonElement>(null);
  const reviewRef = useRef<HTMLButtonElement>(null);
  const approveRef = useRef<HTMLButtonElement>(null);

  const load = useCallback(() => {
    if (!page.page_id) return () => undefined;
    const controller = new AbortController();
    setLoading(true);
    setLoadError(null);
    loadPageDetail(page.page_id, controller.signal)
      .then((payload) => {
        const original = normalizedSource(payload.source_content);
        const curation = payload.curation ?? fallbackCuration(original);
        const effective = normalizedSource(
          curation.current_snapshot?.source_content ?? original,
        );
        setDetail({ ...payload, source_content: original, curation });
        setTitles(effective.titles);
        setBody(effective.body);
        window.requestAnimationFrame(() => firstFieldRef.current?.focus());
      })
      .catch((cause) => {
        if (cause instanceof DOMException && cause.name === "AbortError") return;
        setLoadError(
          cause instanceof OperatorError ? cause.message : "AnyDoc 来源加载失败，请重试。",
        );
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [page.page_id, retryRevision]);

  useEffect(() => load(), [load]);

  useEffect(() => {
    if (!arrivalAnnouncement || loading) return;
    setAnnouncement(arrivalAnnouncement);
    window.requestAnimationFrame(() => statusRef.current?.focus());
  }, [arrivalAnnouncement, loading, statusRef]);

  const original = detail ? normalizedSource(detail.source_content) : null;
  const curation = detail?.curation ?? null;
  const savedSource = original
    ? normalizedSource(curation?.current_snapshot?.source_content ?? original)
    : null;
  const dirty = Boolean(
    savedSource &&
      (JSON.stringify(titles) !== JSON.stringify(savedSource.titles) ||
        JSON.stringify(body) !== JSON.stringify(savedSource.body)),
  );

  useEffect(() => {
    onDirtyChange(dirty);
    const warnBeforeLeave = (event: BeforeUnloadEvent) => {
      if (!dirty) return;
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", warnBeforeLeave);
    return () => {
      window.removeEventListener("beforeunload", warnBeforeLeave);
      onDirtyChange(false);
    };
  }, [dirty, onDirtyChange]);

  const blockers = useMemo(
    () => dirty && original ? localBlockers(original, titles, body) : curation?.blockers ?? [],
    [body, curation?.blockers, dirty, original, titles],
  );
  const snapshot = curation?.current_snapshot ?? null;
  const busy = operation !== null;
  const pending = page.review_status === "pending";

  useEffect(() => {
    if (busy || !focusTarget) return;
    const target = focusTarget === "confirm"
      ? confirmRef.current
      : focusTarget === "review"
        ? reviewRef.current
        : approveRef.current;
    if (!target || target.disabled) return;
    window.requestAnimationFrame(() => {
      target.focus();
      setFocusTarget(null);
    });
  }, [busy, curation, focusTarget]);

  const applyCuration = (next: CurationState) => {
    setDetail((current) => current ? { ...current, curation: next } : current);
    if (next.current_snapshot) {
      setTitles(next.current_snapshot.source_content.titles);
      setBody(next.current_snapshot.source_content.body);
    }
  };

  const handleSave = async () => {
    if (!page.page_id || busy || (!dirty && snapshot)) return;
    setOperation("save");
    setAnnouncement("正在保存来源修改…");
    try {
      const next = await saveCurationSnapshot(
        page.page_id,
        snapshot?.snapshot_id ?? null,
        titles,
        body,
      );
      applyCuration(next);
      setAnnouncement("修改已保存为新的不可变策展快照。请继续确认文字来源。");
      setFocusTarget("confirm");
    } catch (cause) {
      setAnnouncement(
        cause instanceof OperatorError
          ? `${cause.message} 本地修改仍保留。`
          : "来源修改未能保存；本地修改仍保留。",
      );
    } finally {
      setOperation(null);
    }
  };

  const handleConfirm = async () => {
    if (!page.page_id || !snapshot || dirty || busy) return;
    setOperation("confirm");
    setAnnouncement("正在记录文字来源确认…");
    try {
      const next = await confirmCurationSource(page.page_id, snapshot.snapshot_id);
      applyCuration(next);
      setAnnouncement("文字来源已由人确认；字段完成与人工核对已分别记录。");
      setFocusTarget("review");
    } catch (cause) {
      setAnnouncement(
        cause instanceof OperatorError ? cause.message : "文字来源确认失败；当前状态未改变。",
      );
    } finally {
      setOperation(null);
    }
  };

  const handleReview = async () => {
    if (!page.page_id || !snapshot || dirty || busy) return;
    setOperation("review");
    setAnnouncement("正在完成来源审核…");
    try {
      const next = await completeCurationSourceReview(page.page_id, snapshot.snapshot_id);
      applyCuration(next);
      setAnnouncement("来源审核已完成。审核闸门已重新校验 Chunk 正文。");
      setFocusTarget("approve");
    } catch (cause) {
      setAnnouncement(
        cause instanceof OperatorError ? cause.message : "来源审核未能完成；当前状态未改变。",
      );
    } finally {
      setOperation(null);
    }
  };

  const handleApprove = useCallback(async () => {
    if (!page.page_id || !snapshot || dirty || busy || !curation?.can_approve) return;
    setOperation("approve");
    setAnnouncement("正在冻结当前快照并记录批准结论…");
    try {
      await approveCurationPage(page.page_id, snapshot.snapshot_id);
      setAnnouncement("页面已批准，正在转到下一待处理页。");
      await onApproved();
    } catch (cause) {
      setAnnouncement(
        cause instanceof OperatorError
          ? cause.message
          : "批准未完成；页面仍保留在待处理队列。",
      );
    } finally {
      setOperation(null);
    }
  }, [busy, curation?.can_approve, dirty, onApproved, page.page_id, snapshot]);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      if (target?.matches("input, textarea, select, [contenteditable='true']")) return;
      if (event.key.toLowerCase() !== "a" || !curation?.can_approve || dirty || busy) return;
      event.preventDefault();
      void handleApprove();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [busy, curation?.can_approve, dirty, handleApprove]);

  if (loading) {
    return (
      <div className="source-review-loading" aria-busy="true">
        <span />
        <span />
        <p>正在加载 AnyDoc 文字来源与当前策展快照…</p>
      </div>
    );
  }

  if (loadError || !detail || !original || !curation) {
    return (
      <div className="source-review-recovery" role="alert">
        <h2>来源日志连接中断</h2>
        <p>{loadError ?? "来源状态不完整，请重新读取。"}</p>
        <button type="button" onClick={() => setRetryRevision((value) => value + 1)}>
          重新加载来源
        </button>
      </div>
    );
  }

  return (
    <div className="source-review-log" aria-busy={busy}>
      <header className="source-review-header">
        <div>
          <h2>来源日志</h2>
          <p>
            <span>页已进入普通策展流程</span>
            <span>原始提取与当前编辑值并置</span>
          </p>
        </div>
        <span className={`pending-chip ${pending ? "" : "is-approved"}`}>
          {pending ? "待处理" : "已批准"}
        </span>
      </header>

      <div className="source-review-scroll">
        {announcement ? (
          <div
            className="source-live-status"
            role="status"
            aria-live="polite"
            tabIndex={-1}
            ref={statusRef}
          >
            {announcement}
          </div>
        ) : null}

        <section className="source-phase" aria-labelledby="source-text-heading">
          <header>
            <div>
              <h3 id="source-text-heading">文字来源</h3>
              <p>保留 AnyDoc 块的角色、顺序与段落边界</p>
            </div>
            <PhaseStatus complete={Boolean(snapshot) && !dirty}>
              {dirty ? "已修改，原确认失效" : snapshot ? "修改已保存" : "待保存"}
            </PhaseStatus>
          </header>

          <div className="source-field-group">
            <span className="source-field-label">标题</span>
            {original.titles.length ? original.titles.map((value, index) => (
              <label className="source-edit-block" key={`title-${index}`}>
                <span>标题来源 {index + 1}</span>
                <small>原始提取</small>
                <p>{value || "空标题"}</p>
                <small>当前编辑值</small>
                <textarea
                  ref={index === 0 ? firstFieldRef : undefined}
                  aria-label={`标题来源 ${index + 1} 当前编辑值`}
                  rows={2}
                  value={titles[index] ?? ""}
                  disabled={!pending}
                  onChange={(event) => setTitles((current) => current.map(
                    (item, candidate) => candidate === index ? event.target.value : item,
                  ))}
                />
              </label>
            )) : <p className="source-empty-block">AnyDoc 未生成标题块。</p>}
          </div>

          <div className="source-field-group">
            <span className="source-field-label">正文</span>
            {original.body.length ? original.body.map((value, index) => (
              <label className="source-edit-block" key={`body-${index}`}>
                <span>正文来源 {index + 1}</span>
                <small>原始提取</small>
                <p>{value || "空正文"}</p>
                <small>当前编辑值</small>
                <textarea
                  ref={original.titles.length === 0 && index === 0 ? firstFieldRef : undefined}
                  aria-label={`正文来源 ${index + 1} 当前编辑值`}
                  rows={4}
                  value={body[index] ?? ""}
                  disabled={!pending}
                  onChange={(event) => setBody((current) => current.map(
                    (item, candidate) => candidate === index ? event.target.value : item,
                  ))}
                />
              </label>
            )) : <p className="source-empty-block">AnyDoc 未生成正文块。</p>}
          </div>

          {original.speaker_notes.length ? (
            <div className="readonly-source-entry">
              <div>
                <strong>演讲者备注</strong>
                <span>只读来源 · 参与 Chunk 合成</span>
              </div>
              {original.speaker_notes.map((note, index) => <p key={`note-${index}`}>{note}</p>)}
            </div>
          ) : (
            <div className="readonly-source-entry is-empty">
              <div><strong>演讲者备注</strong><span>只读来源</span></div>
              <p>此页没有演讲者备注。</p>
            </div>
          )}

          {original.tables.length ? (
            <div className="readonly-source-entry">
              <div><strong>表格来源</strong><span>{original.tables.length} 项 · 只读</span></div>
              <p>表格保持原始行列顺序并参与 Chunk 合成。</p>
            </div>
          ) : null}

          {original.images.length ? (
            <div className="readonly-source-entry source-image-boundary">
              <div>
                <strong>图片来源</strong>
                <span>{original.images.length} 项 · 尚待逐项处置</span>
              </div>
              {original.images.map((image) => (
                <p key={`${image.reference_index}:${image.origin_part}`}>
                  {image.origin_part} · {image.media_type}
                  {image.alt_text ? ` · ${image.alt_text}` : " · 无替代文字"}
                </p>
              ))}
            </div>
          ) : null}

          <button
            type="button"
            className="source-action-button"
            disabled={!pending || busy || (!dirty && Boolean(snapshot))}
            onClick={() => void handleSave()}
          >
            {operation === "save" ? "正在保存" : "保存修改"}
          </button>
        </section>

        <section className="source-phase" aria-labelledby="source-confirm-heading">
          <header>
            <div>
              <h3 id="source-confirm-heading">文字确认</h3>
              <p>字段已填不等于人已核对</p>
            </div>
            <PhaseStatus complete={Boolean(snapshot?.source_confirmation) && !dirty}>
              {dirty || !snapshot?.source_confirmation ? "待确认" : "已确认"}
            </PhaseStatus>
          </header>
          {snapshot?.source_confirmation && !dirty ? (
            <p className="source-audit-record">
              {snapshot.source_confirmation.actor_id} · {formatTime(snapshot.source_confirmation.confirmed_at)}
            </p>
          ) : (
            <p className="source-phase-copy">保存当前修改后，再明确确认已逐块对照标准页渲染结果。</p>
          )}
          <button
            type="button"
            ref={confirmRef}
            className="source-action-button"
            disabled={
              !pending || busy || dirty || !snapshot || Boolean(snapshot.source_confirmation)
            }
            onClick={() => void handleConfirm()}
          >
            {operation === "confirm" ? "正在确认" : "确认文字来源"}
          </button>
        </section>

        <section className="source-phase" aria-labelledby="source-review-heading">
          <header>
            <div>
              <h3 id="source-review-heading">来源复核</h3>
              <p>关闭文字与图片来源的完整审核阶段</p>
            </div>
            <PhaseStatus complete={Boolean(snapshot?.source_review) && !dirty}>
              {dirty || !snapshot?.source_review ? "未完成" : "已完成"}
            </PhaseStatus>
          </header>
          {snapshot?.source_review && !dirty ? (
            <p className="source-audit-record">
              {snapshot.source_review.actor_id} · {formatTime(snapshot.source_review.completed_at)}
            </p>
          ) : (
            <p className="source-phase-copy">
              {curation.image_sources.unresolved
                ? `${curation.image_sources.unresolved} 个图片来源尚待逐项处置。`
                : "文字确认后，可显式完成来源审核。"}
            </p>
          )}
          <button
            type="button"
            ref={reviewRef}
            className="source-action-button"
            disabled={
              !pending || busy || dirty || !snapshot?.source_confirmation ||
              Boolean(snapshot.source_review) || curation.image_sources.unresolved > 0
            }
            onClick={() => void handleReview()}
          >
            {operation === "review" ? "正在完成审核" : "完成来源审核"}
          </button>
        </section>
      </div>

      <section className={`review-gate ${pending && blockers.length ? "is-blocked" : "is-clear"}`} aria-labelledby="review-gate-heading">
        <header>
          <div>
            <h3 id="review-gate-heading">页面结论</h3>
            <p>
              {!pending
                ? "批准结论已冻结"
                : blockers.length
                  ? `${blockers.length} 项结构性阻塞`
                  : "来源完整 · 无需截图"}
            </p>
          </div>
          <span>{!pending ? "已批准" : blockers.length ? "阻塞" : "可批准"}</span>
        </header>
        {pending && blockers.length ? (
          <ul>
            {blockers.map((blocker) => <li key={blocker.code}>{blocker.message}</li>)}
          </ul>
        ) : (
          <p className="review-gate-clear">
            {pending
              ? "当前确认来源可生成非空 Chunk 正文。"
              : "当前页面保留已批准快照及其来源审核记录。"}
          </p>
        )}
        <button
          type="button"
          ref={approveRef}
          disabled={!pending || busy || dirty || !curation.can_approve}
          onClick={() => void handleApprove()}
        >
          {!pending
            ? "页面已批准"
            : operation === "approve"
              ? "正在批准"
              : "批准并转到下一待处理页"}
        </button>
      </section>
    </div>
  );
}
