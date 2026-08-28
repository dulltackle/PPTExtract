import {
  type CSSProperties,
  type PointerEvent as ReactPointerEvent,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";

import {
  batchExcludeCurationPages,
  confirmAllRenderingWarnings,
  confirmRenderingWarning,
  type CurationState,
  type CurationVisual,
  type CurationPage,
  deleteCaptureVisual,
  enableHiddenPage,
  loadCurationPages,
  loadJob,
  loadPageDetail,
  loadRenderingWarnings,
  markCaptureSourceComplete,
  moveCaptureVisual,
  OperatorError,
  type NormalizedBounds,
  type ExclusionReason,
  type PageDetail,
  type RenderingWarning,
  saveCaptureVisual,
  updateCaptureVisual,
  type VisualType,
} from "./api";
import { SourceReviewLog } from "./SourceReviewLog";

type Filter = "pending" | "inherited" | "all" | "rendering-warnings";

interface PageOperation {
  submitting: boolean;
  announcement: string | null;
}

export interface CurationCommandState {
  navigation: boolean;
  approve: boolean;
  exclude: boolean;
  reopen: boolean;
  cancel: boolean;
  status: string;
}

const EXCLUSION_REASON_OPTIONS: Array<{ value: ExclusionReason; label: string }> = [
  { value: "no_meaningful_content", label: "无有意义内容" },
  { value: "duplicate", label: "重复内容" },
  { value: "irrelevant", label: "与知识库无关" },
  { value: "unreadable", label: "无法可靠阅读" },
  { value: "other", label: "其他" },
];

function isTextEntryTarget(target: EventTarget | null): boolean {
  return target instanceof HTMLElement && Boolean(
    target.closest("input, textarea, select, [contenteditable='true'], [role='dialog']"),
  );
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

function focusAfterLiveAnnouncement(target: HTMLElement | null) {
  window.requestAnimationFrame(() => {
    window.requestAnimationFrame(() => target?.focus());
  });
}

function adjustNormalizedBounds(
  bounds: NormalizedBounds,
  field: "left" | "top" | "width" | "height",
  delta: number,
): NormalizedBounds {
  const minimum = 0.005;
  const next = { ...bounds };
  if (field === "left") next.left = Math.min(1 - next.width, Math.max(0, next.left + delta));
  if (field === "top") next.top = Math.min(1 - next.height, Math.max(0, next.top + delta));
  if (field === "width") next.width = Math.min(1 - next.left, Math.max(minimum, next.width + delta));
  if (field === "height") next.height = Math.min(1 - next.top, Math.max(minimum, next.height + delta));
  return next;
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
  versionWarningSummary,
  interactionLocked,
  selectedForBatch,
  batchReason,
  batchNote,
  batchSubmitting,
  batchAnnouncement,
  onFilter,
  onSelect,
  onToggleBatch,
  onBatchReason,
  onBatchNote,
  onBatchExclude,
}: {
  pages: CurationPage[];
  filter: Filter;
  selectedKey: string | null;
  versionWarningSummary: CurationPage["version_rendering_warnings"];
  interactionLocked: boolean;
  selectedForBatch: Set<string>;
  batchReason: ExclusionReason | "";
  batchNote: string;
  batchSubmitting: boolean;
  batchAnnouncement: string | null;
  onFilter: (filter: Filter) => void;
  onSelect: (key: string) => void;
  onToggleBatch: (key: string, selected: boolean) => void;
  onBatchReason: (reason: ExclusionReason | "") => void;
  onBatchNote: (note: string) => void;
  onBatchExclude: () => void;
}) {
  const selectedCount = selectedForBatch.size;
  return (
    <aside className="page-rail" aria-label="页清单">
      <div className="page-rail-heading">
        <div>
          <h1>逐页策展</h1>
          <p>{pages.length} 页可见</p>
        </div>
        <div className="filter-tabs" aria-label="页清单筛选">
          {(["pending", "inherited", "all", "rendering-warnings"] as const).map((value) => (
            <button
              key={value}
              type="button"
              className={filter === value ? "is-current" : ""}
              aria-pressed={filter === value}
              disabled={interactionLocked}
              onClick={() => onFilter(value)}
            >
              {value === "pending"
                ? "待处理"
                : value === "inherited"
                  ? "已继承"
                  : value === "all"
                    ? "全部"
                    : "渲染警告"}
            </button>
          ))}
        </div>
        {versionWarningSummary && versionWarningSummary.total > 0 ? (
          <div className="version-warning-summary">
            <span aria-hidden="true">!</span>
            {versionWarningSummary.unconfirmed > 0
              ? `当前版本 · ${versionWarningSummary.unconfirmed} 条渲染警告待确认`
              : `当前版本 · ${versionWarningSummary.total} 条渲染警告已确认`}
          </div>
        ) : null}
      </div>
      {selectedCount > 0 ? (
        <section className="batch-exclusion-bar" role="region" aria-label="批量排除">
          <header>
            <strong>已选 {selectedCount} 页</strong>
            <button type="button" disabled={batchSubmitting} onClick={() => {
              selectedForBatch.forEach((key) => onToggleBatch(key, false));
            }}>取消选择</button>
          </header>
          <label>
            <span>统一排除原因</span>
            <select
              aria-label="统一排除原因"
              value={batchReason}
              disabled={batchSubmitting}
              onChange={(event) => onBatchReason(event.target.value as ExclusionReason | "")}
            >
              <option value="">请选择原因</option>
              {EXCLUSION_REASON_OPTIONS.map((option) => (
                <option key={option.value} value={option.value}>{option.label}</option>
              ))}
            </select>
          </label>
          <label>
            <span>补充说明（可选）</span>
            <textarea
              aria-label="批量排除补充说明"
              rows={2}
              value={batchNote}
              disabled={batchSubmitting}
              onChange={(event) => onBatchNote(event.target.value)}
            />
          </label>
          <button
            type="button"
            className="batch-exclusion-submit"
            disabled={!batchReason || batchSubmitting}
            onClick={onBatchExclude}
          >
            {batchSubmitting ? "正在逐页记录" : `批量排除 ${selectedCount} 页`}
          </button>
          {batchAnnouncement ? <p role="status">{batchAnnouncement}</p> : null}
        </section>
      ) : batchAnnouncement ? (
        <div className="batch-exclusion-result" role="status">{batchAnnouncement}</div>
      ) : null}
      <div className="page-list">
        {pages.length === 0 ? (
          <div className="page-list-empty">
            <strong>
              {filter === "pending"
                ? "待处理队列为空"
                : filter === "rendering-warnings"
                  ? "当前没有渲染警告"
                  : "当前版本没有可显示的页"}
            </strong>
            <span>
              {filter === "pending"
                ? "当前筛选保持不变。可检查继承结论或全部已启用页。"
                : filter === "rendering-warnings"
                  ? "字体与动画风险会在标准页渲染完成后出现在这里。"
                  : "上传并处理版本后，源页会按原始顺序出现。"}
            </span>
            {filter === "pending" ? (
              <div className="page-list-empty-actions">
                <button type="button" onClick={() => onFilter("inherited")}>查看已继承</button>
                <button type="button" onClick={() => onFilter("all")}>查看全部</button>
              </div>
            ) : null}
          </div>
        ) : null}
        {pages.map((page) => {
          const key = pageKey(page);
          const hiddenUnprocessed = page.hidden && !page.enabled;
          const title = hiddenUnprocessed ? "隐藏页 · 未处理" : page.title || `第 ${page.page_number} 页`;
          const status = pageStatusLabel(page);
          const warningLabel = (page.rendering_warnings?.total ?? 0) > 0
            ? `，渲染警告 ${page.rendering_warnings?.unconfirmed ?? 0}/${page.rendering_warnings?.total ?? 0} 未确认`
            : "";
          return (
            <div className={`page-row-shell ${selectedForBatch.has(key) ? "is-batch-selected" : ""}`} key={key}>
              {page.review_status === "pending" && page.page_id ? (
                <label className="page-batch-check">
                  <input
                    type="checkbox"
                    aria-label={`选择第 ${page.page_number} 页，${title}`}
                    checked={selectedForBatch.has(key)}
                    disabled={interactionLocked || batchSubmitting}
                    onChange={(event) => onToggleBatch(key, event.target.checked)}
                  />
                  <span aria-hidden="true" />
                </label>
              ) : <span className="page-batch-check-placeholder" aria-hidden="true" />}
              <button
                type="button"
                className={`page-row ${hiddenUnprocessed ? "page-row--hidden" : ""} ${selectedKey === key ? "is-selected" : ""}`}
                aria-label={`第 ${page.page_number} 页，${title}，${status}${warningLabel}`}
                aria-current={selectedKey === key ? "true" : undefined}
                disabled={interactionLocked || batchSubmitting}
                onClick={() => onSelect(key)}
              >
              <span className="page-number">{String(page.page_number).padStart(2, "0")}</span>
              <span className={`page-state-mark ${hiddenUnprocessed ? "is-hollow" : ""}`} aria-hidden="true" />
              <span className="page-row-copy">
                <strong>{title}</strong>
                <span>
                  {status}
                  {(page.rendering_warnings?.total ?? 0) > 0
                    ? ` · 渲染警告 ${page.rendering_warnings?.unconfirmed ?? 0}/${page.rendering_warnings?.total ?? 0} 未确认`
                    : ""}
                </span>
              </span>
              {(page.rendering_warnings?.total ?? 0) > 0 ? (
                <span className="page-warning-mark" aria-hidden="true">
                  !
                </span>
              ) : null}
              </button>
            </div>
          );
        })}
      </div>
    </aside>
  );
}

const VISUAL_TYPE_OPTIONS: Array<{ value: VisualType; label: string }> = [
  { value: "chart", label: "图表" },
  { value: "diagram", label: "示意图" },
  { value: "map", label: "地图" },
  { value: "table", label: "表格" },
  { value: "screenshot", label: "界面截图" },
  { value: "photo", label: "照片" },
  { value: "illustration", label: "插图" },
  { value: "other", label: "其他" },
];

interface VisualEditorCommand {
  kind: "add" | "edit";
  visualRef: string | null;
  trigger: HTMLElement | null;
  nonce: number;
}

function EvidencePanel({
  page,
  curation,
  captureVisuals,
  editorCommand,
  focusCapturePathNonce,
  onCurationChange,
  onCaptureVisualsChange,
  onEditorCommandHandled,
  onEditingChange,
  onFocusApproval,
}: {
  page: CurationPage | null;
  curation: CurationState | null;
  captureVisuals: CurationVisual[];
  editorCommand: VisualEditorCommand | null;
  focusCapturePathNonce: number;
  onCurationChange: (curation: CurationState) => void;
  onCaptureVisualsChange: (visuals: CurationVisual[]) => void;
  onEditorCommandHandled: () => void;
  onEditingChange: (editing: boolean) => void;
  onFocusApproval: () => void;
}) {
  const [mode, setMode] = useState<"decision" | "selecting" | "editing">("decision");
  const [editingRef, setEditingRef] = useState<string | null>(null);
  const [activeRef, setActiveRef] = useState<string | null>(null);
  const [selection, setSelection] = useState<NormalizedBounds | null>(null);
  const [summary, setSummary] = useState("");
  const [visualType, setVisualType] = useState<VisualType | "">("");
  const [summaryError, setSummaryError] = useState<string | null>(null);
  const [announcement, setAnnouncement] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [editorPosition, setEditorPosition] = useState<CSSProperties>({});
  const [editorPlacement, setEditorPlacement] = useState<"right" | "left" | "bottom" | "top">("right");
  const [editorPositioned, setEditorPositioned] = useState(false);
  const imageRef = useRef<HTMLImageElement>(null);
  const activeRangeRef = useRef<HTMLElement>(null);
  const editorRef = useRef<HTMLElement>(null);
  const summaryRef = useRef<HTMLTextAreaElement>(null);
  const capturePathButtonRef = useRef<HTMLButtonElement>(null);
  const returnFocusRef = useRef<HTMLElement | null>(null);
  const dragOperation = useRef<{
    kind: "create" | "move" | "resize";
    start: { x: number; y: number };
    original: NormalizedBounds | null;
    handle?: "nw" | "ne" | "sw" | "se";
  } | null>(null);

  const orderedVisuals = useMemo(
    () => [...captureVisuals].sort((left, right) => left.position - right.position),
    [captureVisuals],
  );

  const visualNumber = useCallback((visualRef: string | null) => {
    if (visualRef === null) return orderedVisuals.length + 1;
    const index = orderedVisuals.findIndex((visual) => visual.visual_ref === visualRef);
    return index < 0 ? orderedVisuals.length + 1 : index + 1;
  }, [orderedVisuals]);

  const formatNumber = (value: number) => String(value).padStart(2, "0");

  const restoreFocus = useCallback(() => {
    window.requestAnimationFrame(() => {
      const target = returnFocusRef.current?.isConnected
        ? returnFocusRef.current
        : capturePathButtonRef.current;
      target?.focus();
      returnFocusRef.current = null;
    });
  }, []);

  useEffect(() => {
    setMode("decision");
    setEditingRef(null);
    setActiveRef(null);
    setSelection(null);
    setSummary("");
    setVisualType("");
    setSummaryError(null);
    setAnnouncement(null);
    setSaving(false);
    setEditorPlacement("right");
    setEditorPositioned(false);
    onEditingChange(false);
  }, [onEditingChange, page?.page_id]);

  const sourceReviewed = Boolean(curation?.current_snapshot?.source_review);
  const canChoosePath = Boolean(
    page?.review_status === "pending" && sourceReviewed && orderedVisuals.length === 0,
  );

  const openEditor = useCallback((visual: CurationVisual, trigger: HTMLElement | null) => {
    if (page?.review_status !== "pending" || !visual.bounds) return;
    returnFocusRef.current = trigger;
    setEditingRef(visual.visual_ref);
    setActiveRef(visual.visual_ref);
    setSelection({ ...visual.bounds });
    setSummary(visual.summary ?? "");
    setVisualType((visual.visual_type as VisualType | null) ?? "");
    setSummaryError(null);
    setEditorPositioned(false);
    setMode("editing");
    setAnnouncement(`正在编辑视觉对象 ${formatNumber(visualNumber(visual.visual_ref))}。`);
    onEditingChange(true);
  }, [onEditingChange, page?.review_status, visualNumber]);

  const startAdding = useCallback((trigger: HTMLElement | null) => {
    if (page?.review_status !== "pending" || !sourceReviewed) return;
    returnFocusRef.current = trigger;
    setEditingRef(null);
    setActiveRef(null);
    setSelection(null);
    setSummary("");
    setVisualType("");
    setSummaryError(null);
    setEditorPositioned(false);
    setMode("selecting");
    setAnnouncement("框选模式已开启。请在标准页渲染结果上拖出缺失范围。");
    onEditingChange(true);
  }, [onEditingChange, page?.review_status, sourceReviewed]);

  useEffect(() => {
    if (!editorCommand) return;
    if (editorCommand.kind === "add") {
      startAdding(editorCommand.trigger);
    } else {
      const visual = orderedVisuals.find(
        (candidate) => candidate.visual_ref === editorCommand.visualRef,
      );
      if (visual) openEditor(visual, editorCommand.trigger);
    }
    onEditorCommandHandled();
  }, [editorCommand, onEditorCommandHandled, openEditor, orderedVisuals, startAdding]);

  const normalizedPoint = (
    event: ReactPointerEvent<HTMLElement>,
  ): { x: number; y: number } | null => {
    const rect = imageRef.current?.getBoundingClientRect();
    if (!rect) return null;
    if (!rect.width || !rect.height) return null;
    return {
      x: Math.min(1, Math.max(0, (event.clientX - rect.left) / rect.width)),
      y: Math.min(1, Math.max(0, (event.clientY - rect.top) / rect.height)),
    };
  };

  const updateDrag = (point: { x: number; y: number }) => {
    const operation = dragOperation.current;
    if (!operation) return;
    if (operation.kind === "create") {
      setSelection({
        left: Math.min(operation.start.x, point.x),
        top: Math.min(operation.start.y, point.y),
        width: Math.abs(point.x - operation.start.x),
        height: Math.abs(point.y - operation.start.y),
      });
      return;
    }
    const original = operation.original;
    if (!original) return;
    const dx = point.x - operation.start.x;
    const dy = point.y - operation.start.y;
    if (operation.kind === "move") {
      setSelection({
        ...original,
        left: Math.min(1 - original.width, Math.max(0, original.left + dx)),
        top: Math.min(1 - original.height, Math.max(0, original.top + dy)),
      });
      return;
    }
    const right = original.left + original.width;
    const bottom = original.top + original.height;
    const minimum = 0.005;
    const nextLeft = operation.handle?.includes("w")
      ? Math.min(right - minimum, Math.max(0, original.left + dx))
      : original.left;
    const nextTop = operation.handle?.includes("n")
      ? Math.min(bottom - minimum, Math.max(0, original.top + dy))
      : original.top;
    const nextRight = operation.handle?.includes("e")
      ? Math.max(original.left + minimum, Math.min(1, right + dx))
      : right;
    const nextBottom = operation.handle?.includes("s")
      ? Math.max(original.top + minimum, Math.min(1, bottom + dy))
      : bottom;
    setSelection({
      left: nextLeft,
      top: nextTop,
      width: nextRight - nextLeft,
      height: nextBottom - nextTop,
    });
  };

  const positionEditor = useCallback(() => {
    if (!selection || !imageRef.current) return;
    const activeRangeRect = activeRangeRef.current?.getBoundingClientRect();
    const imageRect = imageRef.current.getBoundingClientRect();
    const range = activeRangeRect && activeRangeRect.width && activeRangeRect.height
      ? activeRangeRect
      : {
          left: imageRect.left + selection.left * imageRect.width,
          top: imageRect.top + selection.top * imageRect.height,
          right: imageRect.left + (selection.left + selection.width) * imageRect.width,
          bottom: imageRect.top + (selection.top + selection.height) * imageRect.height,
        };
    const gap = 14;
    const margin = 12;
    const width = editorRef.current?.offsetWidth || 360;
    const height = editorRef.current?.offsetHeight || 430;
    const candidates = [
      { placement: "right" as const, left: range.right + gap, top: range.top },
      { placement: "left" as const, left: range.left - gap - width, top: range.top },
      { placement: "bottom" as const, left: range.left, top: range.bottom + gap },
      { placement: "top" as const, left: range.left, top: range.top - gap - height },
    ];
    const clamp = (value: number, minimum: number, maximum: number) => (
      Math.min(Math.max(minimum, value), Math.max(minimum, maximum))
    );
    const assessed = candidates.map((candidate, priority) => {
      const fits = candidate.left >= margin && candidate.top >= margin &&
        candidate.left + width <= window.innerWidth - margin &&
        candidate.top + height <= window.innerHeight - margin;
      const left = clamp(candidate.left, margin, window.innerWidth - width - margin);
      const top = clamp(candidate.top, margin, window.innerHeight - height - margin);
      const overlapWidth = Math.max(0, Math.min(left + width, range.right) - Math.max(left, range.left));
      const overlapHeight = Math.max(0, Math.min(top + height, range.bottom) - Math.max(top, range.top));
      return {
        ...candidate,
        left,
        top,
        fits,
        overlapArea: overlapWidth * overlapHeight,
        priority,
      };
    });
    const chosen = assessed.find((candidate) => candidate.fits)
      ?? [...assessed].sort((left, right) => (
        left.overlapArea - right.overlapArea || left.priority - right.priority
      ))[0];
    setEditorPlacement(chosen.placement);
    setEditorPosition({
      left: chosen.left,
      top: chosen.top,
    });
    setEditorPositioned(true);
  }, [selection]);

  useEffect(() => {
    if (mode !== "editing") return;
    const keepFocusInEditor = (event: FocusEvent) => {
      if (editorRef.current?.contains(event.target as Node)) return;
      summaryRef.current?.focus();
    };
    let frame = 0;
    const schedulePosition = () => {
      window.cancelAnimationFrame(frame);
      frame = window.requestAnimationFrame(positionEditor);
    };
    const initialFrame = window.requestAnimationFrame(schedulePosition);
    const resizeObserver = typeof ResizeObserver === "undefined"
      ? null
      : new ResizeObserver(schedulePosition);
    if (editorRef.current) resizeObserver?.observe(editorRef.current);
    if (imageRef.current) resizeObserver?.observe(imageRef.current);
    if (activeRangeRef.current) resizeObserver?.observe(activeRangeRef.current);
    document.addEventListener("focusin", keepFocusInEditor);
    document.addEventListener("scroll", schedulePosition, true);
    window.addEventListener("resize", schedulePosition);
    imageRef.current?.addEventListener("load", schedulePosition);
    return () => {
      window.cancelAnimationFrame(frame);
      window.cancelAnimationFrame(initialFrame);
      resizeObserver?.disconnect();
      document.removeEventListener("focusin", keepFocusInEditor);
      document.removeEventListener("scroll", schedulePosition, true);
      window.removeEventListener("resize", schedulePosition);
      imageRef.current?.removeEventListener("load", schedulePosition);
    };
  }, [mode, positionEditor]);

  useEffect(() => {
    if (mode !== "editing" || !editorPositioned) return;
    const frame = window.requestAnimationFrame(() => summaryRef.current?.focus());
    return () => window.cancelAnimationFrame(frame);
  }, [editorPositioned, mode]);

  useEffect(() => {
    if (!focusCapturePathNonce || !canChoosePath || mode !== "decision") return;
    let focusFrame = 0;
    const renderFrame = window.requestAnimationFrame(() => {
      focusFrame = window.requestAnimationFrame(() => capturePathButtonRef.current?.focus());
    });
    return () => {
      window.cancelAnimationFrame(renderFrame);
      window.cancelAnimationFrame(focusFrame);
    };
  }, [canChoosePath, focusCapturePathNonce, mode]);

  useEffect(() => {
    if (mode !== "selecting") return;
    const cancelSelection = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      event.stopImmediatePropagation();
      setSelection(null);
      setMode("decision");
      setAnnouncement(
        orderedVisuals.length
          ? "已取消追加框选；既有视觉对象保持不变。"
          : "已取消框选，返回来源完整性选择。",
      );
      onEditingChange(false);
      restoreFocus();
    };
    window.addEventListener("keydown", cancelSelection);
    return () => window.removeEventListener("keydown", cancelSelection);
  }, [mode, onEditingChange, orderedVisuals.length, restoreFocus]);

  const cancelEditor = () => {
    const number = formatNumber(visualNumber(editingRef));
    setSelection(null);
    setMode("decision");
    setEditingRef(null);
    setSummary("");
    setVisualType("");
    setSummaryError(null);
    setEditorPositioned(false);
    setAnnouncement(
      editingRef
        ? `已放弃视觉对象 ${number} 的修改；已保存内容与顺序保持不变。`
        : orderedVisuals.length
          ? "已取消追加框选；既有视觉对象保持不变。"
          : "已取消临时视觉对象，返回来源完整性选择。",
    );
    onEditingChange(false);
    restoreFocus();
  };

  const nudgeSelection = (
    field: "left" | "top" | "width" | "height",
    delta: number,
  ) => {
    if (!selection) return;
    setSelection(adjustNormalizedBounds(selection, field, delta));
  };

  const handleSave = async () => {
    const snapshotId = curation?.current_snapshot?.snapshot_id;
    if (!page?.page_id || !snapshotId || !selection || saving) return;
    const number = formatNumber(visualNumber(editingRef));
    if (!summary.trim()) {
      setSummaryError("summary 不能为空，请写成可独立理解的结论。");
      window.requestAnimationFrame(() => summaryRef.current?.focus());
      return;
    }
    setSaving(true);
    setSummaryError(null);
    setAnnouncement(`正在裁出 PNG 并保存视觉对象 ${number}…`);
    try {
      const result = editingRef
        ? await updateCaptureVisual(
            page.page_id,
            editingRef,
            snapshotId,
            summary,
            visualType || null,
            selection,
          )
        : await saveCaptureVisual(
            page.page_id,
            snapshotId,
            summary,
            visualType || null,
            selection,
          );
      onCurationChange(result.curation);
      const optimistic: CurationVisual = {
        visual_ref: editingRef ?? `pending-${Date.now()}`,
        position: editingRef
          ? orderedVisuals.find((visual) => visual.visual_ref === editingRef)?.position ?? orderedVisuals.length
          : Math.max(-1, ...orderedVisuals.map((visual) => visual.position)) + 1,
        source_kind: "capture",
        disposition: "included",
        summary: summary.trim(),
        visual_type: visualType || null,
        bounds: selection,
        source_visual_ref: null,
        confirmed: true,
      };
      const nextVisuals = result.visuals?.filter((visual) => visual.source_kind === "capture")
        ?? (editingRef
          ? orderedVisuals.map((visual) => visual.visual_ref === editingRef ? optimistic : visual)
          : [...orderedVisuals, optimistic]);
      onCaptureVisualsChange(nextVisuals);
      setMode("decision");
      setActiveRef(editingRef ?? optimistic.visual_ref);
      setEditingRef(null);
      setSelection(null);
      setAnnouncement(`视觉对象 ${number} 已保存，审核闸门已重新校验。`);
      onEditingChange(false);
      onFocusApproval();
      if (!result.visuals) try {
        const refreshed = await loadPageDetail(page.page_id);
        const savedCaptures = refreshed.annotation?.visuals.filter(
          (visual) => visual.source_kind === "capture",
        ) ?? [];
        onCaptureVisualsChange(savedCaptures);
      } catch {
        setAnnouncement(
          `视觉对象 ${number} 已保存；资产详情暂未刷新，可稍后刷新工作位。`,
        );
      }
    } catch (cause) {
      setAnnouncement(
        cause instanceof OperatorError
          ? `${cause.message} 当前范围和表单内容仍保留，可重试。`
          : "视觉对象未能保存；当前范围和表单内容仍保留，可重试。",
      );
    } finally {
      setSaving(false);
    }
  };

  const handleDragMove = (event: ReactPointerEvent<HTMLElement>) => {
    if (!dragOperation.current) return;
    const point = normalizedPoint(event);
    if (point) updateDrag(point);
  };

  const handleDragEnd = (event: ReactPointerEvent<HTMLElement>) => {
    const operation = dragOperation.current;
    if (!operation) return;
    const point = normalizedPoint(event);
    if (point) updateDrag(point);
    dragOperation.current = null;
    if (operation.kind !== "create") return;
    const rect = imageRef.current?.getBoundingClientRect();
    if (!rect) return;
    if (
      !point ||
      Math.abs(point.x - operation.start.x) * rect.width < 4 ||
      Math.abs(point.y - operation.start.y) * rect.height < 4
    ) {
      setSelection(null);
      setAnnouncement("范围太小，请重新框选。");
      return;
    }
    const nextSelection = {
      left: Number(Math.min(operation.start.x, point.x).toFixed(6)),
      top: Number(Math.min(operation.start.y, point.y).toFixed(6)),
      width: Number(Math.abs(point.x - operation.start.x).toFixed(6)),
      height: Number(Math.abs(point.y - operation.start.y).toFixed(6)),
    };
    setSelection(nextSelection);
    setEditorPositioned(false);
    setMode("editing");
    setAnnouncement(
      `已创建临时视觉对象 ${formatNumber(visualNumber(null))}，请填写自足 summary。`,
    );
    onEditingChange(true);
  };

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
          <figure className={`page-render ${mode === "selecting" || mode === "editing" ? "is-capture-mode" : ""} ${mode === "editing" ? "has-open-editor" : ""}`}>
            <div
              className="page-render-frame"
              onPointerMove={handleDragMove}
              onPointerUp={handleDragEnd}
            >
              <img
                ref={imageRef}
                src={`/api/v1/pages/${page.page_id}/render`}
                alt={`第 ${page.page_number} 页标准页渲染结果`}
                draggable={false}
                onPointerDown={(event) => {
                  if (mode !== "selecting") return;
                  const point = normalizedPoint(event);
                  if (!point) return;
                  dragOperation.current = { kind: "create", start: point, original: null };
                  setSelection({ left: point.x, top: point.y, width: 0, height: 0 });
                  event.currentTarget.setPointerCapture?.(event.pointerId);
                }}
                onPointerMove={handleDragMove}
                onPointerUp={handleDragEnd}
              />
              {orderedVisuals.map((visual, index) => visual.bounds ? (
                <button
                  ref={activeRef === visual.visual_ref ? (node) => {
                    activeRangeRef.current = node;
                  } : undefined}
                  type="button"
                  key={visual.visual_ref}
                  data-visual-ref={visual.visual_ref}
                  disabled={page.review_status !== "pending" || (mode === "editing" && activeRef !== visual.visual_ref)}
                  tabIndex={mode === "editing" ? -1 : undefined}
                  className={`capture-range is-saved ${activeRef === visual.visual_ref ? "is-active" : "is-secondary"}`}
                  style={{
                    left: `${(editingRef === visual.visual_ref && selection ? selection : visual.bounds).left * 100}%`,
                    top: `${(editingRef === visual.visual_ref && selection ? selection : visual.bounds).top * 100}%`,
                    width: `${(editingRef === visual.visual_ref && selection ? selection : visual.bounds).width * 100}%`,
                    height: `${(editingRef === visual.visual_ref && selection ? selection : visual.bounds).height * 100}%`,
                  }}
                  aria-label={`视觉对象 ${formatNumber(index + 1)} 框选范围，${visual.summary ?? "缺少 summary"}`}
                  aria-keyshortcuts="ArrowLeft ArrowRight ArrowUp ArrowDown Shift+ArrowLeft Shift+ArrowRight Shift+ArrowUp Shift+ArrowDown"
                  onKeyDown={(event) => {
                    if (mode !== "decision" || !visual.bounds) return;
                    const direction = event.key;
                    if (!["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"].includes(direction)) return;
                    event.preventDefault();
                    event.stopPropagation();
                    const field = event.shiftKey
                      ? direction === "ArrowLeft" || direction === "ArrowRight" ? "width" : "height"
                      : direction === "ArrowLeft" || direction === "ArrowRight" ? "left" : "top";
                    const delta = direction === "ArrowLeft" || direction === "ArrowUp" ? -0.001 : 0.001;
                    openEditor(visual, event.currentTarget);
                    setSelection(adjustNormalizedBounds(visual.bounds, field, delta));
                    setAnnouncement(
                      event.shiftKey
                        ? `视觉对象 ${formatNumber(index + 1)} 的尺寸已微调；左上角保持锚定。`
                        : `视觉对象 ${formatNumber(index + 1)} 的位置已微调。`,
                    );
                  }}
                  onClick={(event) => {
                    if (mode !== "editing") openEditor(visual, event.currentTarget);
                  }}
                  onPointerDown={(event) => {
                    if (page.review_status !== "pending" || !visual.bounds) return;
                    if (mode === "editing" && activeRef !== visual.visual_ref) return;
                    if (mode === "editing") event.preventDefault();
                    else openEditor(visual, event.currentTarget);
                    const point = normalizedPoint(event);
                    if (!point) return;
                    dragOperation.current = {
                      kind: "move",
                      start: point,
                      original: mode === "editing" && selection
                        ? { ...selection }
                        : { ...visual.bounds },
                    };
                    event.currentTarget.setPointerCapture?.(event.pointerId);
                  }}
                >
                  <span>{formatNumber(index + 1)}</span>
                  {editingRef === visual.visual_ref ? (["nw", "ne", "sw", "se"] as const).map((handle) => (
                    <span
                      key={handle}
                      className={`capture-resize-handle is-${handle}`}
                      aria-hidden="true"
                      onPointerDown={(event) => {
                        event.stopPropagation();
                        const point = normalizedPoint(event);
                        if (!point || !selection) return;
                        dragOperation.current = {
                          kind: "resize",
                          start: point,
                          original: { ...selection },
                          handle,
                        };
                        event.currentTarget.setPointerCapture?.(event.pointerId);
                      }}
                    />
                  )) : null}
                </button>
              ) : null)}
              {editingRef === null && selection ? (
                <div
                  ref={(node) => {
                    activeRangeRef.current = node;
                  }}
                  className="capture-range is-temporary is-active"
                  style={{
                    left: `${selection.left * 100}%`,
                    top: `${selection.top * 100}%`,
                    width: `${selection.width * 100}%`,
                    height: `${selection.height * 100}%`,
                  }}
                  aria-label={`临时视觉对象 ${formatNumber(visualNumber(null))} 框选范围`}
                >
                  <span>{formatNumber(visualNumber(null))}</span>
                </div>
              ) : null}
            </div>
            <figcaption>{page.title || `第 ${page.page_number} 页`}</figcaption>

            {canChoosePath && mode === "decision" ? (
              <div className="capture-decision" aria-label="选择来源完整性路径">
                <div>
                  <strong>来源审核已完成</strong>
                  <span>标准页仍有未被文字与图片来源完整表达的内容吗？</span>
                </div>
                <div>
                  <button type="button" onClick={onFocusApproval}>
                    来源完整，直接审核
                  </button>
                  <button
                    ref={capturePathButtonRef}
                    type="button"
                    className="is-primary"
                    onClick={(event) => startAdding(event.currentTarget)}
                  >
                    有缺口，在页面上框选
                  </button>
                </div>
              </div>
            ) : null}

            {mode === "selecting" ? (
              <div className="capture-mode-instruction">
                <strong>拖出缺失范围</strong>
                <span>不会自动显示候选框 · Esc 取消</span>
              </div>
            ) : null}

            {orderedVisuals.length > 0 && !sourceReviewed ? (
              <div className="capture-stale-note" role="status">
                已保存 {orderedVisuals.length} 个视觉对象；来源审核失效，重新完成审核后才能批准。
              </div>
            ) : null}
          </figure>
        )}
      </div>

      <div
        className="capture-live-status"
        role={announcement ? "status" : undefined}
        aria-live="polite"
      >
        {announcement}
      </div>

      {mode === "editing" && selection ? (
        <div
          className="capture-editor-backdrop"
          aria-hidden="true"
          onPointerDown={(event) => {
            event.preventDefault();
            summaryRef.current?.focus();
          }}
        />
      ) : null}

      {mode === "editing" && selection ? (
        <section
          ref={editorRef}
          className="capture-editor"
          style={editorPosition}
          data-placement={editorPlacement}
          data-positioned={editorPositioned}
          role="dialog"
          aria-modal="true"
          aria-labelledby="capture-editor-heading"
          onKeyDown={(event) => {
            if (event.key === "Escape") {
              event.preventDefault();
              cancelEditor();
              return;
            }
            if (event.key !== "Tab") return;
            const controls = Array.from(
              editorRef.current?.querySelectorAll<HTMLElement>(
                "textarea, select, button:not([disabled]), [tabindex]:not([tabindex='-1'])",
              ) ?? [],
            );
            if (!controls.length) return;
            const first = controls[0];
            const last = controls[controls.length - 1];
            if (event.shiftKey && document.activeElement === first) {
              event.preventDefault();
              last.focus();
            } else if (!event.shiftKey && document.activeElement === last) {
              event.preventDefault();
              first.focus();
            }
          }}
        >
          <header>
            <span>{formatNumber(visualNumber(editingRef))}</span>
            <div>
              <h2 id="capture-editor-heading">视觉对象 {formatNumber(visualNumber(editingRef))}</h2>
              <p>{editingRef ? "修改将生成新策展快照" : "范围将按整页归一化坐标保存"}</p>
            </div>
          </header>
          <label>
            <span>自足 summary <strong>必填</strong></span>
            <textarea
              ref={summaryRef}
              aria-label={`视觉对象 ${formatNumber(visualNumber(editingRef))} summary`}
              rows={4}
              value={summary}
              aria-invalid={Boolean(summaryError)}
              aria-describedby={summaryError ? "capture-summary-error" : undefined}
              onChange={(event) => {
                setSummary(event.target.value);
                if (summaryError && event.target.value.trim()) setSummaryError(null);
              }}
            />
          </label>
          {summaryError ? (
            <p className="capture-field-error" id="capture-summary-error">
              {summaryError}
            </p>
          ) : (
            <p className="capture-field-help">写成脱离当前页面也能独立理解的事实结论。</p>
          )}
          <label>
            <span>视觉类型 <small>可选</small></span>
            <select
              aria-label={`视觉对象 ${formatNumber(visualNumber(editingRef))} 类型`}
              value={visualType}
              onChange={(event) => setVisualType(event.target.value as VisualType | "")}
            >
              <option value="">不指定</option>
              {VISUAL_TYPE_OPTIONS.map((option) => (
                <option key={option.value} value={option.value}>{option.label}</option>
              ))}
            </select>
          </label>
          <fieldset className="capture-bounds-controls">
            <legend>范围微调</legend>
            <button type="button" onClick={() => nudgeSelection("left", -0.001)}>左移</button>
            <button type="button" onClick={() => nudgeSelection("top", -0.001)}>上移</button>
            <button type="button" onClick={() => nudgeSelection("top", 0.001)}>下移</button>
            <button type="button" onClick={() => nudgeSelection("left", 0.001)}>右移</button>
            <button type="button" onClick={() => nudgeSelection("width", -0.001)}>缩窄</button>
            <button type="button" onClick={() => nudgeSelection("width", 0.001)}>加宽</button>
            <button type="button" onClick={() => nudgeSelection("height", -0.001)}>减高</button>
            <button type="button" onClick={() => nudgeSelection("height", 0.001)}>增高</button>
          </fieldset>
          <div className="capture-editor-actions">
            <button type="button" disabled={saving} onClick={cancelEditor}>
              {editingRef ? "放弃修改" : "取消"}
            </button>
            <button
              type="button"
              className="is-primary"
              disabled={saving}
              onClick={() => void handleSave()}
            >
              {saving ? "正在裁图并保存" : editingRef ? "保存修改" : "保存并返回审核"}
            </button>
          </div>
        </section>
      ) : null}
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

function warningTitle(warning: RenderingWarning): string {
  return warning.code === "missing_font" ? "字体缺失或替代" : "动画时间线已静态扁平化";
}

function summarizePageWarnings(warnings: RenderingWarning[]) {
  const unconfirmed = warnings.filter((warning) => warning.status === "unconfirmed").length;
  return {
    total: warnings.length,
    pages: warnings.length ? 1 : 0,
    unconfirmed,
    unconfirmed_pages: unconfirmed ? 1 : 0,
  };
}

function WarningInspector({
  page,
  targetWarningId,
  onSummaryChange,
}: {
  page: CurationPage;
  targetWarningId: string | null;
  onSummaryChange: (
    versionSummary: NonNullable<CurationPage["version_rendering_warnings"]>,
    pageSummary: NonNullable<CurationPage["rendering_warnings"]>,
  ) => void;
}) {
  const [warnings, setWarnings] = useState<RenderingWarning[]>([]);
  const [summary, setSummary] = useState(page.rendering_warnings ?? {
    total: 0,
    pages: 0,
    unconfirmed: 0,
    unconfirmed_pages: 0,
  });
  const [renderConfigVersion, setRenderConfigVersion] = useState<string | null>(null);
  const [versionUnconfirmedIds, setVersionUnconfirmedIds] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState<string | null>(null);
  const [announcement, setAnnouncement] = useState<string | null>(null);
  const [showConfirmAll, setShowConfirmAll] = useState(false);
  const headingRef = useRef<HTMLHeadingElement>(null);
  const confirmAllTriggerRef = useRef<HTMLButtonElement>(null);
  const confirmAllSubmitRef = useRef<HTMLButtonElement>(null);
  const confirmAllDialogRef = useRef<HTMLElement>(null);

  useEffect(() => {
    if (showConfirmAll) {
      window.requestAnimationFrame(() => confirmAllSubmitRef.current?.focus());
    }
  }, [showConfirmAll]);

  const closeConfirmAll = () => {
    setShowConfirmAll(false);
    window.requestAnimationFrame(() => confirmAllTriggerRef.current?.focus());
  };

  useEffect(() => {
    if (!page.page_id) return;
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    loadRenderingWarnings(page, controller.signal)
      .then((payload) => {
        const pageWarnings = payload.warnings.filter(
          (warning) => warning.page_number === page.page_number,
        );
        setWarnings(pageWarnings);
        setSummary(payload.summary);
        setRenderConfigVersion(payload.render_config_version);
        setVersionUnconfirmedIds(
          payload.warnings
            .filter((warning) => warning.status === "unconfirmed")
            .map((warning) => warning.warning_id),
        );
        onSummaryChange(payload.summary, summarizePageWarnings(pageWarnings));
        window.requestAnimationFrame(() => headingRef.current?.focus());
      })
      .catch((cause) => {
        if (cause instanceof DOMException && cause.name === "AbortError") return;
        setError(cause instanceof OperatorError ? cause.message : "渲染警告加载失败，请重试。");
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
    // summary 仅作首屏占位；真实值始终由页详情覆盖。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [onSummaryChange, page.page_id, page.page_number]);

  const handleConfirm = async (warning: RenderingWarning) => {
    if (submitting) return;
    setSubmitting(warning.warning_id);
    setAnnouncement(null);
    try {
      const confirmed = await confirmRenderingWarning(page, warning.warning_id);
      const nextWarnings = warnings.map((candidate) =>
        candidate.warning_id === confirmed.warning_id ? confirmed : candidate,
      );
      const nextSummary = {
        ...summary,
        unconfirmed: Math.max(0, summary.unconfirmed - 1),
        unconfirmed_pages:
          summarizePageWarnings(nextWarnings).unconfirmed === 0
            ? Math.max(0, summary.unconfirmed_pages - 1)
            : summary.unconfirmed_pages,
      };
      setWarnings(nextWarnings);
      setVersionUnconfirmedIds((current) =>
        current.filter((warningId) => warningId !== confirmed.warning_id),
      );
      setSummary(nextSummary);
      onSummaryChange(nextSummary, summarizePageWarnings(nextWarnings));
      setAnnouncement("已确认 1 条渲染警告。");
    } catch (cause) {
      setAnnouncement(
        cause instanceof OperatorError
          ? `${cause.message} 警告仍保持未确认。`
          : "渲染警告确认失败；警告仍保持未确认。",
      );
    } finally {
      setSubmitting(null);
    }
  };

  const handleConfirmAll = async () => {
    if (!renderConfigVersion || submitting) return;
    setSubmitting("all");
    setAnnouncement(null);
    try {
      const result = await confirmAllRenderingWarnings(
        page,
        renderConfigVersion,
        versionUnconfirmedIds,
      );
      setSummary(result.summary);
      const pageWarnings = result.warnings.filter(
        (warning) => warning.page_number === page.page_number,
      );
      setWarnings(pageWarnings);
      setRenderConfigVersion(result.render_config_version);
      setVersionUnconfirmedIds(
        result.warnings
          .filter((warning) => warning.status === "unconfirmed")
          .map((warning) => warning.warning_id),
      );
      onSummaryChange(result.summary, summarizePageWarnings(pageWarnings));
      setAnnouncement(`已确认 ${result.confirmed_count} 条渲染警告。`);
      setShowConfirmAll(false);
      window.requestAnimationFrame(() => headingRef.current?.focus());
    } catch (cause) {
      setAnnouncement(
        cause instanceof OperatorError
          ? `${cause.message} 请重新检查当前版本。`
          : "整版确认失败，请重新检查当前版本。",
      );
      closeConfirmAll();
    } finally {
      setSubmitting(null);
    }
  };

  return (
    <div className="warning-inspector" aria-busy={loading}>
      <div className="warning-inspector-heading">
        <div>
          <h2 ref={headingRef} tabIndex={-1}>渲染警告</h2>
          <p>风险不阻止策展，但必须在发布前由人确认。</p>
        </div>
        <span className={summary.unconfirmed ? "warning-count-chip" : "warning-count-chip is-confirmed"}>
          {summary.unconfirmed ? `${summary.unconfirmed} 条待确认` : "全部已确认"}
        </span>
      </div>

      {announcement ? (
        <div className="warning-announcement" role="status" aria-live="polite">
          {announcement}
        </div>
      ) : null}
      {error ? <div className="warning-load-error" role="alert">{error}</div> : null}
      {!loading && !error && warnings.length === 0 ? (
        <div className="warning-empty-state">
          <strong>此页没有渲染警告</strong>
          <span>选择左侧带警告标记的页面继续复核。</span>
        </div>
      ) : null}

      <div className="warning-list">
        {warnings.map((warning) => {
          const confirmed = warning.status === "confirmed";
          const targeted = warning.warning_id === targetWarningId;
          return (
            <article
              className={`warning-entry ${confirmed ? "is-confirmed" : "is-unconfirmed"} ${targeted ? "is-targeted" : ""}`}
              key={warning.warning_id}
            >
              <header>
                <div>
                  <span className="warning-type-mark" aria-hidden="true" />
                  <strong>{warningTitle(warning)}</strong>
                </div>
                <span>{confirmed ? "已确认" : "未确认"}</span>
              </header>
              <p className="warning-detail">
                {warning.code === "missing_font"
                  ? `${warning.details.requested_font ?? "未知字体"} → ${warning.details.replacement_font ?? "未记录替代字体"}`
                  : `检测到 ${warning.details.timeline_count ?? 1} 条动画时间线；标准页渲染只保留静态打印状态。`}
              </p>
              <dl>
                <div><dt>页码</dt><dd>{warning.page_number}</dd></div>
                <div><dt>渲染配置</dt><dd>{warning.render_config_version}</dd></div>
              </dl>
              {confirmed ? (
                <p className="warning-confirmation">
                  {warning.confirmed_by ?? "未知操作者"} · 已确认
                  {warning.confirmed_at ? ` · ${new Intl.DateTimeFormat("zh-CN", { dateStyle: "short", timeStyle: "short" }).format(new Date(warning.confirmed_at))}` : ""}
                </p>
              ) : (
                <button
                  type="button"
                  className="warning-confirm-button"
                  disabled={submitting !== null}
                  onClick={() => void handleConfirm(warning)}
                  aria-label={`确认${warningTitle(warning)}警告`}
                >
                  {submitting === warning.warning_id ? "正在确认" : "确认此警告"}
                </button>
              )}
            </article>
          );
        })}
      </div>

      {summary.total > 0 ? (
        <div className="warning-version-action">
          <div>
            <strong>
              {summary.unconfirmed
                ? `当前版本仍有 ${summary.unconfirmed} 条未确认`
                : `当前版本 ${summary.total} 条渲染警告均已确认`}
            </strong>
            <span>{renderConfigVersion ?? "正在读取渲染配置"}</span>
          </div>
          <button
            type="button"
            ref={confirmAllTriggerRef}
            disabled={!summary.unconfirmed || submitting !== null}
            onClick={() => setShowConfirmAll(true)}
          >
            确认当前版本全部警告
          </button>
        </div>
      ) : null}

      {showConfirmAll ? (
        <div className="compact-dialog-backdrop">
          <section
            role="dialog"
            aria-modal="true"
            aria-labelledby="confirm-all-warning-title"
            className="compact-dialog"
            ref={confirmAllDialogRef}
            onKeyDown={(event) => {
              if (event.key === "Escape") {
                closeConfirmAll();
                return;
              }
              if (event.key !== "Tab") return;
              const controls = Array.from(
                confirmAllDialogRef.current?.querySelectorAll<HTMLElement>(
                  "button:not([disabled]), [href], [tabindex]:not([tabindex='-1'])",
                ) ?? [],
              );
              if (!controls.length) return;
              const first = controls[0];
              const last = controls[controls.length - 1];
              if (event.shiftKey && document.activeElement === first) {
                event.preventDefault();
                last.focus();
              } else if (!event.shiftKey && document.activeElement === last) {
                event.preventDefault();
                first.focus();
              }
            }}
          >
            <h2 id="confirm-all-warning-title">确认当前版本全部警告</h2>
            <p>{summary.unconfirmed_pages} 页 / {summary.unconfirmed} 条未确认</p>
            <dl>
              <div><dt>渲染配置版本</dt><dd>{renderConfigVersion}</dd></div>
            </dl>
            <p>系统仍会为每条警告分别保存操作者、时间、警告明细和配置版本。</p>
            <div className="dialog-actions">
              <button type="button" onClick={closeConfirmAll}>返回检查</button>
              <button
                type="button"
                ref={confirmAllSubmitRef}
                className="warning-confirm-button"
                onClick={() => void handleConfirmAll()}
              >
                确认全部 {summary.unconfirmed} 条警告
              </button>
            </div>
          </section>
        </div>
      ) : null}
    </div>
  );
}

function InspectorPanel({
  page,
  submitting,
  announcement,
  statusRef,
  onEnable,
  warningMode,
  targetWarningId,
  onWarningSummaryChange,
  curationAnnouncement,
  externalCuration,
  captureVisuals,
  focusApprovalNonce,
  interactionLocked,
  visualOperation,
  onCurationChange,
  onDetailLoaded,
  approvalPathReady,
  onAddCapture,
  onEditCapture,
  onMoveCapture,
  onDeleteCapture,
  onMarkSourceComplete,
  onModalStateChange,
  onSourceDirtyChange,
  onApproved,
  onExcluded,
  onReopened,
}: {
  page: CurationPage | null;
  submitting: boolean;
  announcement: string | null;
  statusRef: React.RefObject<HTMLDivElement | null>;
  onEnable: () => void;
  warningMode: boolean;
  targetWarningId: string | null;
  onWarningSummaryChange: (
    versionSummary: NonNullable<CurationPage["version_rendering_warnings"]>,
    pageSummary: NonNullable<CurationPage["rendering_warnings"]>,
  ) => void;
  curationAnnouncement: string | null;
  externalCuration: CurationState | null;
  captureVisuals: CurationVisual[];
  focusApprovalNonce: number;
  interactionLocked: boolean;
  visualOperation: string | null;
  onCurationChange: (curation: CurationState) => void;
  onDetailLoaded: (detail: PageDetail) => void;
  approvalPathReady: boolean;
  onAddCapture: (trigger: HTMLElement) => void;
  onEditCapture: (visualRef: string, trigger: HTMLElement) => void;
  onMoveCapture: (
    visualRef: string,
    direction: "up" | "down",
    number: number,
    trigger: HTMLElement,
  ) => void;
  onDeleteCapture: (visualRef: string, number: number, trigger: HTMLElement) => void;
  onMarkSourceComplete: (trigger: HTMLElement) => void;
  onModalStateChange: (open: boolean) => void;
  onSourceDirtyChange: (dirty: boolean) => void;
  onApproved: () => Promise<void>;
  onExcluded: () => Promise<void>;
  onReopened: () => Promise<void>;
}) {
  return (
    <aside
      className={`inspector-panel ${interactionLocked ? "is-interaction-locked" : ""}`}
      aria-label="来源与策展日志"
      inert={interactionLocked}
    >
      {!page ? (
        <div className="inspector-empty">
          {curationAnnouncement ?? "选择一页后，这里会显示可追溯来源与可用动作。"}
        </div>
      ) : warningMode && page.page_id ? (
        <WarningInspector
          key={`${page.document_id}:${page.version_id}:${page.page_number}`}
          page={page}
          targetWarningId={targetWarningId}
          onSummaryChange={onWarningSummaryChange}
        />
      ) : page.hidden && !page.enabled ? (
        <SourceRegistration
          page={page}
          submitting={submitting}
          announcement={announcement}
          statusRef={statusRef}
          onEnable={onEnable}
        />
      ) : (
        <SourceReviewLog
          page={page}
          arrivalAnnouncement={curationAnnouncement ?? announcement}
          statusRef={statusRef}
          onDirtyChange={onSourceDirtyChange}
          onApproved={onApproved}
          onExcluded={onExcluded}
          onReopened={onReopened}
          externalCuration={externalCuration}
          captureVisuals={captureVisuals}
          focusApprovalNonce={focusApprovalNonce}
          onCurationChange={onCurationChange}
          onDetailLoaded={onDetailLoaded}
          approvalPathReady={approvalPathReady}
          visualOperation={visualOperation}
          onAddCapture={onAddCapture}
          onEditCapture={onEditCapture}
          onMoveCapture={onMoveCapture}
          onDeleteCapture={onDeleteCapture}
          onMarkSourceComplete={onMarkSourceComplete}
          onModalStateChange={onModalStateChange}
        />
      )}
    </aside>
  );
}

export function CurationWorkbench({
  onCommandStateChange,
}: {
  onCommandStateChange?: (state: CurationCommandState) => void;
} = {}) {
  const requestedParams = new URLSearchParams(window.location.search);
  const requestedFilter = requestedParams.get("filter");
  const requestedDocumentId = requestedParams.get("document");
  const requestedVersionId = requestedParams.get("version");
  const requestedPageParam = requestedParams.get("page");
  const requestedPageNumber = requestedPageParam === null ? null : Number(requestedPageParam);
  const [filter, setFilter] = useState<Filter>(
    requestedFilter === "rendering-warnings" || requestedFilter === "inherited" || requestedFilter === "all"
      ? requestedFilter
      : "pending",
  );
  const targetWarningId = requestedParams.get("warning");
  const [pages, setPages] = useState<CurationPage[]>([]);
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [operations, setOperations] = useState<Record<string, PageOperation>>({});
  const [sourceDirty, setSourceDirty] = useState(false);
  const [curationAnnouncement, setCurationAnnouncement] = useState<string | null>(null);
  const [selectedCuration, setSelectedCuration] = useState<CurationState | null>(null);
  const [captureVisuals, setCaptureVisuals] = useState<CurationVisual[]>([]);
  const [captureEditing, setCaptureEditing] = useState(false);
  const [editorCommand, setEditorCommand] = useState<VisualEditorCommand | null>(null);
  const [visualOperation, setVisualOperation] = useState<string | null>(null);
  const [deleteCandidate, setDeleteCandidate] = useState<{
    visualRef: string;
    number: number;
    trigger: HTMLElement;
  } | null>(null);
  const [focusApprovalNonce, setFocusApprovalNonce] = useState(0);
  const [focusCapturePathNonce, setFocusCapturePathNonce] = useState(0);
  const [approvalPathReady, setApprovalPathReady] = useState(false);
  const [selectedForBatch, setSelectedForBatch] = useState<Set<string>>(new Set());
  const [batchReason, setBatchReason] = useState<ExclusionReason | "">("");
  const [batchNote, setBatchNote] = useState("");
  const [batchSubmitting, setBatchSubmitting] = useState(false);
  const [batchAnnouncement, setBatchAnnouncement] = useState<string | null>(null);
  const [sourceModalOpen, setSourceModalOpen] = useState(false);
  const request = useRef<AbortController | null>(null);
  const poll = useRef<AbortController | null>(null);
  const timer = useRef<number | null>(null);
  const statusRef = useRef<HTMLDivElement>(null);
  const deleteDialogRef = useRef<HTMLElement>(null);
  const deleteSubmitRef = useRef<HTMLButtonElement>(null);
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
        const requested = nextPages.find(
          (page) =>
            page.document_id === requestedDocumentId &&
            page.version_id === requestedVersionId &&
            (requestedPageNumber === null ||
              page.page_number === requestedPageNumber),
        );
        return nextPages.some((page) => pageKey(page) === preferred)
          ? preferred
          : requested
            ? pageKey(requested)
          : nextPages[0]
            ? pageKey(nextPages[0])
            : null;
      });
      return nextPages;
    } catch (cause) {
      if (cause instanceof DOMException && cause.name === "AbortError") return [];
      setError(cause instanceof OperatorError ? cause.message : "策展页清单发生未知错误。请重试。");
      return [];
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

  useEffect(() => {
    if (!onCommandStateChange) return;
    const busy = Boolean(selectedOperation?.submitting || visualOperation || batchSubmitting);
    const blockerCount = selectedCuration?.blockers.length ?? 0;
    onCommandStateChange({
      navigation: pages.length > 1 && !captureEditing && !batchSubmitting && !sourceModalOpen,
      approve: Boolean(
        selected?.review_status === "pending" && selectedCuration?.can_approve &&
        approvalPathReady && !sourceDirty && !busy && !sourceModalOpen,
      ),
      exclude: selected?.review_status === "pending" && !busy && !sourceModalOpen,
      reopen: Boolean(
        selected?.review_status && selected.review_status !== "pending" && !busy && !sourceModalOpen,
      ),
      cancel: sourceModalOpen || captureEditing || selectedForBatch.size > 0,
      status: loading
        ? "正在读取策展工作位"
        : busy
          ? "正在保存，快捷键已暂停"
          : sourceModalOpen
            ? "确认对话框已打开，可按 Esc 返回检查"
          : captureEditing
            ? "视觉对象编辑中，可按 Esc 放弃本地修改"
            : sourceDirty
              ? "有未保存的来源修改，批准快捷键已暂停"
              : blockerCount > 0
                ? `${blockerCount} 项审核阻塞待处理`
                : selected
                  ? `${pageStatusLabel(selected)} · 工作位就绪`
                  : "当前筛选没有可用页面",
    });
  }, [
    approvalPathReady,
    batchSubmitting,
    captureEditing,
    loading,
    onCommandStateChange,
    pages.length,
    selected,
    selectedCuration?.blockers.length,
    selectedCuration?.can_approve,
    selectedForBatch.size,
    selectedOperation?.submitting,
    sourceDirty,
    sourceModalOpen,
    visualOperation,
  ]);

  useEffect(() => {
    const eligible = new Set(
      pages
        .filter((page) => page.review_status === "pending" && page.page_id)
        .map(pageKey),
    );
    setSelectedForBatch((current) => {
      const next = new Set([...current].filter((key) => eligible.has(key)));
      return next.size === current.size ? current : next;
    });
  }, [pages]);

  useEffect(() => {
    setSelectedCuration(null);
    setSourceModalOpen(false);
    setCaptureVisuals([]);
    setCaptureEditing(false);
    setEditorCommand(null);
    setVisualOperation(null);
    setDeleteCandidate(null);
    setApprovalPathReady(false);
  }, [selectedKey]);

  useEffect(() => {
    if (!selectedCuration?.current_snapshot?.source_review) {
      setApprovalPathReady(false);
    } else if (captureVisuals.length > 0) {
      setApprovalPathReady(true);
    }
  }, [captureVisuals.length, selectedCuration?.current_snapshot?.source_review]);

  const handleDetailLoaded = useCallback((detail: PageDetail) => {
    setSelectedCuration(detail.curation ?? null);
    setCaptureVisuals(
      detail.annotation?.visuals
        .filter((visual) => visual.source_kind === "capture")
        .sort((left, right) => left.position - right.position) ?? [],
    );
  }, []);

  const confirmDiscard = useCallback(() => {
    if (!sourceDirty) return true;
    return window.confirm("当前页仍有未保存的来源修改。放弃这些修改并继续吗？");
  }, [sourceDirty]);

  const handleSelect = useCallback((key: string) => {
    if (captureEditing) return;
    if (!confirmDiscard()) return;
    setSourceDirty(false);
    setCurationAnnouncement(null);
    setSelectedKey(key);
  }, [captureEditing, confirmDiscard]);

  const handleFilter = useCallback((nextFilter: Filter) => {
    if (captureEditing) return;
    if (!confirmDiscard()) return;
    setSourceDirty(false);
    setCurationAnnouncement(null);
    setSelectedForBatch(new Set());
    setBatchReason("");
    setBatchNote("");
    setBatchAnnouncement(null);
    setFilter(nextFilter);
  }, [captureEditing, confirmDiscard]);

  const handleToggleBatch = useCallback((key: string, checked: boolean) => {
    setSelectedForBatch((current) => {
      const next = new Set(current);
      if (checked) next.add(key);
      else next.delete(key);
      return next;
    });
    setBatchAnnouncement(null);
  }, []);

  useEffect(() => {
    const handleQueueKeyboard = (event: KeyboardEvent) => {
      if (captureEditing || batchSubmitting || sourceModalOpen || isTextEntryTarget(event.target)) return;
      if (event.key === "Escape" && selectedForBatch.size > 0) {
        event.preventDefault();
        setSelectedForBatch(new Set());
        setBatchReason("");
        setBatchNote("");
        setBatchAnnouncement("已退出批量选择。");
        return;
      }
      if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
      if (!pages.length) return;
      event.preventDefault();
      const currentIndex = pages.findIndex((page) => pageKey(page) === selectedKeyRef.current);
      const delta = event.key === "ArrowLeft" ? -1 : 1;
      const nextIndex = currentIndex < 0
        ? 0
        : Math.min(pages.length - 1, Math.max(0, currentIndex + delta));
      const target = pages[nextIndex];
      if (target && pageKey(target) !== selectedKeyRef.current) handleSelect(pageKey(target));
    };
    document.addEventListener("keydown", handleQueueKeyboard);
    return () => document.removeEventListener("keydown", handleQueueKeyboard);
  }, [batchSubmitting, captureEditing, handleSelect, pages, selectedForBatch.size, sourceModalOpen]);

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

  const handleWarningSummaryChange = useCallback(
    (
      versionSummary: NonNullable<CurationPage["version_rendering_warnings"]>,
      pageSummary: NonNullable<CurationPage["rendering_warnings"]>,
    ) => {
      const targetKey = selectedKeyRef.current;
      setPages((current) => {
        const target = current.find((page) => pageKey(page) === targetKey);
        if (!target) return current;
        return current.map((page) =>
          page.version_id === target.version_id
            ? {
                ...page,
                version_rendering_warnings: versionSummary,
                rendering_warnings:
                  pageKey(page) === targetKey ? pageSummary : page.rendering_warnings,
              }
            : page,
        );
      });
    },
    [],
  );

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

  const nextPendingKeyAfter = useCallback((
    currentKey: string | null,
    removedPageIds: ReadonlySet<string> = new Set(),
  ) => {
    const currentIndex = pages.findIndex((page) => pageKey(page) === currentKey);
    const orderedPages = currentIndex < 0
      ? pages
      : [...pages.slice(currentIndex + 1), ...pages.slice(0, currentIndex)];
    const nextPending = orderedPages.find((page) => (
      page.review_status === "pending" &&
      pageKey(page) !== currentKey &&
      (!page.page_id || !removedPageIds.has(page.page_id))
    ));
    return nextPending ? pageKey(nextPending) : null;
  }, [pages]);

  const advanceAfterConclusion = useCallback(async (conclusion: "批准" | "排除") => {
    setSourceDirty(false);
    const concludedKey = selectedKeyRef.current;
    const preferredNextKey = nextPendingKeyAfter(concludedKey);
    if (concludedKey) {
      setSelectedForBatch((current) => {
        if (!current.has(concludedKey)) return current;
        const next = new Set(current);
        next.delete(concludedKey);
        return next;
      });
    }
    const nextPages = await loadPages(filter, preferredNextKey);
    const nextPending = nextPages.find((page) => (
      page.review_status === "pending" &&
      (preferredNextKey === null || pageKey(page) === preferredNextKey)
    )) ?? nextPages.find((page) => page.review_status === "pending");
    if (nextPending) setSelectedKey(pageKey(nextPending));
    setCurationAnnouncement(
      nextPending
        ? `上一页已${conclusion}。已转到下一待处理页。`
        : "待处理队列已清空",
    );
  }, [filter, loadPages, nextPendingKeyAfter]);

  const handleApproved = useCallback(async () => {
    await advanceAfterConclusion("批准");
  }, [advanceAfterConclusion]);

  const handleExcluded = useCallback(async () => {
    await advanceAfterConclusion("排除");
  }, [advanceAfterConclusion]);

  const handleReopened = useCallback(async () => {
    setSourceDirty(false);
    const reopenedKey = selectedKeyRef.current;
    await loadPages(filter, reopenedKey);
    setCurationAnnouncement("页面已重新打开，恢复为待处理并解锁编辑。");
  }, [filter, loadPages]);

  const handleBatchExclude = useCallback(async () => {
    if (!batchReason || batchSubmitting || selectedForBatch.size === 0) return;
    const selectedPages = pages.filter((page) => (
      selectedForBatch.has(pageKey(page)) && page.review_status === "pending" && page.page_id
    ));
    const pageIds = selectedPages.map((page) => page.page_id as string);
    if (!pageIds.length) return;
    setBatchSubmitting(true);
    setBatchAnnouncement(`正在逐页记录 ${pageIds.length} 页的排除结论…`);
    try {
      const result = await batchExcludeCurationPages(
        pageIds,
        batchReason,
        batchNote.trim() || null,
      );
      const failedIds = new Set(result.failed.map((failure) => failure.page_id));
      setSelectedForBatch(new Set(
        selectedPages
          .filter((page) => page.page_id && failedIds.has(page.page_id))
          .map(pageKey),
      ));
      const failureCopy = result.failed.length
        ? ` ${result.failed.length} 页未处理：${result.failed.map((failure) => `第 ${selectedPages.find((page) => page.page_id === failure.page_id)?.page_number ?? "?"} 页（${failure.message}）`).join("；")} 请刷新或重试。`
        : "";
      setBatchAnnouncement(
        result.excluded.length
          ? `已批量排除 ${result.excluded.length} 页。每页均已分别记录审核事件。${failureCopy}`
          : `没有页面完成排除。${failureCopy.trim()}`,
      );
      if (!result.failed.length) {
        setBatchReason("");
        setBatchNote("");
      }
      const currentKey = selectedKeyRef.current;
      const currentPageId = pages.find((page) => pageKey(page) === currentKey)?.page_id;
      const preferredNextKey = nextPendingKeyAfter(currentKey, new Set(result.excluded));
      const nextPages = await loadPages(filter, preferredNextKey);
      if (currentPageId && result.excluded.includes(currentPageId)) {
        const nextPending = nextPages.find((page) => (
          page.review_status === "pending" &&
          (preferredNextKey === null || pageKey(page) === preferredNextKey)
        )) ?? nextPages.find((page) => page.review_status === "pending");
        if (nextPending) setSelectedKey(pageKey(nextPending));
      }
    } catch (cause) {
      setBatchAnnouncement(
        cause instanceof OperatorError
          ? `${cause.message} 已选页面和原因仍保留，可重试。`
          : "批量排除未完成；已选页面和原因仍保留，可重试。",
      );
    } finally {
      setBatchSubmitting(false);
    }
  }, [batchNote, batchReason, batchSubmitting, filter, loadPages, nextPendingKeyAfter, pages, selectedForBatch]);

  const applyVisualMutation = useCallback((result: {
    curation: CurationState;
    visuals: CurationVisual[] | null;
  }) => {
    setSelectedCuration(result.curation);
    if (result.visuals) {
      setCaptureVisuals(
        result.visuals
          .filter((visual) => visual.source_kind === "capture")
          .sort((left, right) => left.position - right.position),
      );
    }
  }, []);

  const requestAddCapture = useCallback((trigger: HTMLElement) => {
    setEditorCommand({ kind: "add", visualRef: null, trigger, nonce: Date.now() });
  }, []);

  const requestEditCapture = useCallback((visualRef: string, trigger: HTMLElement) => {
    setEditorCommand({ kind: "edit", visualRef, trigger, nonce: Date.now() });
  }, []);

  const handleMoveCapture = useCallback(async (
    visualRef: string,
    direction: "up" | "down",
    number: number,
    trigger: HTMLElement,
  ) => {
    const pageId = selected?.page_id;
    const snapshotId = selectedCuration?.current_snapshot?.snapshot_id;
    if (!pageId || !snapshotId || visualOperation) return;
    setVisualOperation(`${visualRef}:move`);
    setCurationAnnouncement(`正在${direction === "up" ? "上移" : "下移"}视觉对象 ${String(number).padStart(2, "0")}…`);
    try {
      const result = await moveCaptureVisual(pageId, visualRef, snapshotId, direction);
      applyVisualMutation(result);
      const nextNumber = direction === "up" ? number - 1 : number + 1;
      setCurationAnnouncement(
        `视觉对象 ${String(number).padStart(2, "0")} 已${direction === "up" ? "上移" : "下移"}到第 ${nextNumber} 位。`,
      );
      focusAfterLiveAnnouncement(trigger);
    } catch (cause) {
      setCurationAnnouncement(
        cause instanceof OperatorError
          ? `${cause.message} 原顺序与原编号仍保留，可重试。`
          : "视觉对象排序失败；原顺序与原编号仍保留，可重试。",
      );
      focusAfterLiveAnnouncement(trigger);
    } finally {
      setVisualOperation(null);
    }
  }, [applyVisualMutation, selected?.page_id, selectedCuration?.current_snapshot?.snapshot_id, visualOperation]);

  const requestDeleteCapture = useCallback((
    visualRef: string,
    number: number,
    trigger: HTMLElement,
  ) => {
    setDeleteCandidate({ visualRef, number, trigger });
    setCaptureEditing(true);
  }, []);

  const closeDeleteDialog = useCallback(() => {
    const trigger = deleteCandidate?.trigger ?? null;
    setDeleteCandidate(null);
    setCaptureEditing(false);
    window.requestAnimationFrame(() => trigger?.focus());
  }, [deleteCandidate]);

  useEffect(() => {
    if (!deleteCandidate) return;
    const frame = window.requestAnimationFrame(() => deleteSubmitRef.current?.focus());
    const handleEscape = (event: KeyboardEvent) => {
      if (event.key !== "Escape" || visualOperation) return;
      event.preventDefault();
      closeDeleteDialog();
    };
    document.addEventListener("keydown", handleEscape);
    return () => {
      window.cancelAnimationFrame(frame);
      document.removeEventListener("keydown", handleEscape);
    };
  }, [closeDeleteDialog, deleteCandidate, visualOperation]);

  const handleDeleteCapture = useCallback(async () => {
    const pageId = selected?.page_id;
    const snapshotId = selectedCuration?.current_snapshot?.snapshot_id;
    if (!deleteCandidate || !pageId || !snapshotId || visualOperation) return;
    setVisualOperation(`${deleteCandidate.visualRef}:delete`);
    try {
      const result = await deleteCaptureVisual(
        pageId,
        deleteCandidate.visualRef,
        snapshotId,
      );
      applyVisualMutation(result);
      const remaining = result.visuals?.filter(
        (visual) => visual.source_kind === "capture",
      ).length ?? Math.max(0, captureVisuals.length - 1);
      setApprovalPathReady(remaining > 0);
      if (remaining > 0) setFocusApprovalNonce((value) => value + 1);
      else setFocusCapturePathNonce((value) => value + 1);
      setCurationAnnouncement(
        remaining
          ? `视觉对象 ${String(deleteCandidate.number).padStart(2, "0")} 已删除；其余对象已重新编号。`
          : "最后一个视觉对象已删除；来源仍有缺口，请重新框选或改选来源完整。",
      );
      setDeleteCandidate(null);
      setCaptureEditing(false);
    } catch (cause) {
      setCurationAnnouncement(
        cause instanceof OperatorError
          ? `${cause.message} 原对象与原编号仍保留，可重试。`
          : "视觉对象删除失败；原对象与原编号仍保留，可重试。",
      );
      focusAfterLiveAnnouncement(deleteSubmitRef.current);
    } finally {
      setVisualOperation(null);
    }
  }, [
    applyVisualMutation,
    captureVisuals.length,
    deleteCandidate,
    selected?.page_id,
    selectedCuration?.current_snapshot?.snapshot_id,
    visualOperation,
  ]);

  const handleMarkSourceComplete = useCallback(async (trigger: HTMLElement) => {
    const pageId = selected?.page_id;
    const snapshotId = selectedCuration?.current_snapshot?.snapshot_id;
    if (!pageId || !snapshotId || visualOperation) return;
    setVisualOperation("source-complete");
    try {
      const result = await markCaptureSourceComplete(pageId, snapshotId);
      applyVisualMutation(result);
      setApprovalPathReady(true);
      setFocusApprovalNonce((value) => value + 1);
      setCurationAnnouncement("已明确改选来源完整；来源缺口阻塞已解除。");
    } catch (cause) {
      setCurationAnnouncement(
        cause instanceof OperatorError
          ? `${cause.message} 来源缺口仍保持阻塞，可重试。`
          : "来源完整性未能更新；来源缺口仍保持阻塞，可重试。",
      );
      focusAfterLiveAnnouncement(trigger);
    } finally {
      setVisualOperation(null);
    }
  }, [applyVisualMutation, selected?.page_id, selectedCuration?.current_snapshot?.snapshot_id, visualOperation]);

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
    <>
    <main
      className={`curation-workspace ${captureEditing ? "has-protected-operation" : ""}`}
      aria-busy={loading}
      inert={deleteCandidate ? true : undefined}
    >
      <PageRail
        pages={pages}
        filter={filter}
        selectedKey={selectedKey}
        versionWarningSummary={selected?.version_rendering_warnings}
        interactionLocked={captureEditing}
        selectedForBatch={selectedForBatch}
        batchReason={batchReason}
        batchNote={batchNote}
        batchSubmitting={batchSubmitting}
        batchAnnouncement={batchAnnouncement}
        onFilter={handleFilter}
        onSelect={handleSelect}
        onToggleBatch={handleToggleBatch}
        onBatchReason={setBatchReason}
        onBatchNote={setBatchNote}
        onBatchExclude={() => void handleBatchExclude()}
      />
      <EvidencePanel
        page={selected}
        curation={selectedCuration}
        captureVisuals={captureVisuals}
        editorCommand={editorCommand}
        focusCapturePathNonce={focusCapturePathNonce}
        onCurationChange={setSelectedCuration}
        onCaptureVisualsChange={setCaptureVisuals}
        onEditorCommandHandled={() => setEditorCommand(null)}
        onEditingChange={setCaptureEditing}
        onFocusApproval={() => {
          setApprovalPathReady(true);
          setFocusApprovalNonce((value) => value + 1);
        }}
      />
      <InspectorPanel
        page={selected}
        submitting={selectedOperation?.submitting ?? false}
        announcement={selectedOperation?.announcement ?? null}
        statusRef={statusRef}
        onEnable={() => void handleEnable()}
        warningMode={filter === "rendering-warnings"}
        targetWarningId={targetWarningId}
        onWarningSummaryChange={handleWarningSummaryChange}
        curationAnnouncement={curationAnnouncement}
        externalCuration={selectedCuration}
        captureVisuals={captureVisuals}
        focusApprovalNonce={focusApprovalNonce}
        interactionLocked={captureEditing}
        visualOperation={visualOperation}
        onCurationChange={setSelectedCuration}
        onDetailLoaded={handleDetailLoaded}
        approvalPathReady={approvalPathReady}
        onAddCapture={requestAddCapture}
        onEditCapture={requestEditCapture}
        onMoveCapture={(visualRef, direction, number, trigger) => {
          void handleMoveCapture(visualRef, direction, number, trigger);
        }}
        onDeleteCapture={requestDeleteCapture}
        onMarkSourceComplete={(trigger) => void handleMarkSourceComplete(trigger)}
        onModalStateChange={setSourceModalOpen}
        onSourceDirtyChange={setSourceDirty}
        onApproved={handleApproved}
        onExcluded={handleExcluded}
        onReopened={handleReopened}
      />
    </main>
    {deleteCandidate ? (
      <div
        className="dialog-backdrop capture-delete-backdrop"
        onMouseDown={(event) => {
          if (event.target === event.currentTarget && !visualOperation) closeDeleteDialog();
        }}
      >
        <section
          ref={deleteDialogRef}
          className="capture-delete-dialog"
          role="dialog"
          aria-modal="true"
          aria-labelledby="capture-delete-heading"
          onKeyDown={(event) => {
            if (event.key !== "Tab") return;
            const controls = Array.from(
              deleteDialogRef.current?.querySelectorAll<HTMLButtonElement>(
                "button:not([disabled])",
              ) ?? [],
            );
            if (!controls.length) return;
            const first = controls[0];
            const last = controls[controls.length - 1];
            if (event.shiftKey && document.activeElement === first) {
              event.preventDefault();
              last.focus();
            } else if (!event.shiftKey && document.activeElement === last) {
              event.preventDefault();
              first.focus();
            }
          }}
        >
          <header>
            <span>{String(deleteCandidate.number).padStart(2, "0")}</span>
            <div>
              <h2 id="capture-delete-heading">
                删除视觉对象 {String(deleteCandidate.number).padStart(2, "0")}？
              </h2>
              <p>删除会生成新的不可变策展快照，历史快照仍保留。</p>
            </div>
          </header>
          <p>
            此操作不会合并范围。若范围不完整，请删除后重新框选覆盖完整内容的大范围。
          </p>
          <div className="capture-editor-actions">
            <button type="button" disabled={Boolean(visualOperation)} onClick={closeDeleteDialog}>
              返回检查
            </button>
            <button
              ref={deleteSubmitRef}
              type="button"
              className="is-danger"
              disabled={Boolean(visualOperation)}
              onClick={() => void handleDeleteCapture()}
            >
              {visualOperation ? "正在删除" : `确认删除视觉对象 ${String(deleteCandidate.number).padStart(2, "0")}`}
            </button>
          </div>
        </section>
      </div>
    ) : null}
    </>
  );
}
