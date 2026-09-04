import {
  type RefObject,
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { flushSync } from "react-dom";

import {
  approveCurationPage,
  confirmRepeatedFooterNoise,
  type CurationBlocker,
  type CurationImageSource,
  type CurationState,
  type CurationVisual,
  completeCurationSourceReview,
  type CurationPage,
  excludeCurationPage,
  type ExclusionReason,
  type ImageDisposition,
  type ImageIgnoreReason,
  loadPageDetail,
  loadRepeatedFooterNoiseCandidate,
  OperatorError,
  type PageDetail,
  reopenCurationPage,
  type RepeatedFooterNoiseCandidate,
  revokeRepeatedFooterNoise,
  reviewCurationText,
  saveCurationImageSource,
  type SourceContent,
} from "./api";

interface ImageDraft {
  disposition: ImageDisposition | null;
  summary: string;
  ignoreReason: ImageIgnoreReason | null;
  ignoreNote: string;
}

type SourceTextKind = "title" | "body";

interface ActiveTextEditor {
  kind: SourceTextKind;
  index: number;
  baseline: string;
}

interface BodyAuditLocation {
  index: number;
  mode: "preview" | "expanded";
}

export interface SourceDraftActions {
  discardText: () => void;
  discardImages: () => void;
  restoreTextFocus: () => void;
}

const IGNORE_REASONS: Array<{ value: ImageIgnoreReason; label: string }> = [
  { value: "decorative", label: "装饰性内容" },
  { value: "duplicate_source", label: "重复来源" },
  { value: "expressed_elsewhere", label: "其他来源已完整表达" },
  { value: "not_relevant", label: "与页面知识无关" },
  { value: "corrupt_or_unverifiable", label: "内容损坏或无法核验" },
  { value: "other", label: "其他" },
];

const EXCLUSION_REASONS: Array<{ value: ExclusionReason; label: string }> = [
  { value: "no_meaningful_content", label: "无有意义内容" },
  { value: "duplicate", label: "重复内容" },
  { value: "irrelevant", label: "与知识库无关" },
  { value: "unreadable", label: "无法可靠阅读" },
  { value: "other", label: "其他" },
];

function draftFromImage(item: CurationImageSource): ImageDraft {
  return {
    disposition: item.disposition,
    summary: item.summary ?? "",
    ignoreReason: item.ignore_reason,
    ignoreNote: item.ignore_note ?? "",
  };
}

function imageDrafts(items: CurationImageSource[]): Record<string, ImageDraft> {
  return Object.fromEntries(items.map((item) => [item.source_ref, draftFromImage(item)]));
}

function imageDraftComplete(draft: ImageDraft): boolean {
  if (draft.disposition === "included") return Boolean(draft.summary.trim());
  if (draft.disposition !== "ignored" || !draft.ignoreReason) return false;
  return draft.ignoreReason !== "other" || Boolean(draft.ignoreNote.trim());
}

function formatBytes(value: number | null): string {
  if (value === null) return "字节缺失";
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / 1024 / 1024).toFixed(1)} MB`;
}

function browserPreviewable(mediaType: string): boolean {
  return new Set([
    "image/png",
    "image/jpeg",
    "image/gif",
    "image/webp",
    "image/avif",
    "image/svg+xml",
    "image/bmp",
  ]).has(mediaType.toLowerCase());
}

function normalizedSource(source: Partial<SourceContent>): SourceContent {
  return {
    titles: source.titles ?? [],
    body: source.body ?? [],
    tables: source.tables ?? [],
    images: source.images ?? [],
    speaker_notes: source.speaker_notes ?? [],
  };
}

const BODY_BOUNDARY_MESSAGE = "已到达来源段落边界。请在当前来源文字块内编辑。";

interface BodySelection {
  startIndex: number;
  startOffset: number;
  endIndex: number;
  endOffset: number;
  collapsed: boolean;
}

function bodyEditableFromNode(node: Node, root: HTMLElement): HTMLElement | null {
  const element = node instanceof HTMLElement ? node : node.parentElement;
  const editable = element?.closest<HTMLElement>("[data-body-editable-index]") ?? null;
  return editable && root.contains(editable) ? editable : null;
}

function bodyOffsetAt(editable: HTMLElement, node: Node, offset: number): number {
  const lines = Array.from(editable.querySelectorAll<HTMLElement>(":scope > [data-body-line]"));
  const element = node instanceof HTMLElement ? node : node.parentElement;
  const line = element?.closest<HTMLElement>("[data-body-line]") ?? null;
  if (!line || !editable.contains(line)) {
    const childOffset = node === editable ? offset : 0;
    const precedingTextLength = lines.slice(0, childOffset).reduce(
      (length, candidate) => length + (candidate.textContent ?? "").length,
      0,
    );
    return precedingTextLength + Math.min(childOffset, Math.max(lines.length - 1, 0));
  }
  const lineIndex = lines.indexOf(line);
  const before = lines.slice(0, lineIndex).reduce(
    (length, candidate) => length + (candidate.textContent ?? "").length + 1,
    0,
  );
  const range = document.createRange();
  range.selectNodeContents(line);
  range.setEnd(node, offset);
  return before + range.toString().length;
}

function bodyTextFromNode(root: Node): string {
  if (!(root instanceof HTMLElement)) return root.textContent ?? "";
  const lines = Array.from(root.querySelectorAll<HTMLElement>(":scope > [data-body-line]"));
  return lines.length
    ? lines.map((line) => line.textContent ?? "").join("\n")
    : root.textContent ?? "";
}

function currentBodySelection(root: HTMLElement | null): BodySelection | null {
  if (!root) return null;
  const selection = window.getSelection();
  if (!selection?.rangeCount) return null;
  const range = selection.getRangeAt(0);
  const start = bodyEditableFromNode(range.startContainer, root);
  const end = bodyEditableFromNode(range.endContainer, root);
  if (!start || !end) return null;
  return {
    startIndex: Number(start.dataset.bodyEditableIndex),
    startOffset: bodyOffsetAt(start, range.startContainer, range.startOffset),
    endIndex: Number(end.dataset.bodyEditableIndex),
    endOffset: bodyOffsetAt(end, range.endContainer, range.endOffset),
    collapsed: range.collapsed,
  };
}

function placeBodyCaret(editable: HTMLElement | null, offset: number): void {
  if (!editable) return;
  editable.focus();
  const lines = Array.from(editable.querySelectorAll<HTMLElement>(":scope > [data-body-line]"));
  let remaining = offset;
  let target = lines[lines.length - 1] ?? editable;
  for (const [index, line] of lines.entries()) {
    const length = (line.textContent ?? "").length;
    if (remaining <= length) {
      target = line;
      break;
    }
    remaining -= length;
    if (index < lines.length - 1) remaining -= 1;
  }
  const text = Array.from(target.childNodes).find((node) => node.nodeType === Node.TEXT_NODE);
  const range = document.createRange();
  if (text) range.setStart(text, Math.min(remaining, text.textContent?.length ?? 0));
  else range.setStart(target, 0);
  range.collapse(true);
  const selection = window.getSelection();
  selection?.removeAllRanges();
  selection?.addRange(range);
}

function bodyEditableHtml(value: string): string {
  return value.split("\n").map((line) => {
    const escaped = line
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#39;");
    return `<div data-body-line>${escaped || "<br>"}</div>`;
  }).join("");
}

function fallbackCuration(source: SourceContent): CurationState {
  const hasText = [
    ...source.titles,
    ...source.body,
    ...source.speaker_notes,
  ].some((value) => value.trim()) || source.tables.length > 0;
  const imageCount = source.images.length;
  const items: CurationImageSource[] = source.images.map((image, index) => ({
    ...image,
    source_ref: `unavailable-${index}`,
    position: index,
    object_sha256: null,
    size_bytes: null,
    integrity: "missing",
    duplicate_object: false,
    preview_url: "",
    disposition: null,
    summary: null,
    ignore_reason: null,
    ignore_note: null,
    visual_ref: null,
    decided_by: null,
    decided_at: null,
  }));
  return {
    current_snapshot: null,
    image_sources: { total: imageCount, unresolved: imageCount, items },
    chunk_body: { nonempty: hasText },
    blockers: [
      { code: "source_unsaved", message: "文字修改尚未保存。" },
      { code: "source_unconfirmed", message: "文字来源尚未确认。" },
      { code: "source_review_incomplete", message: "来源审核尚未完成。" },
      ...items.map((item) => ({
        code: "image_disposition_required" as const,
        message: `图片来源 ${String(item.position + 1).padStart(2, "0")}：尚未选择保留或忽略。`,
        source_ref: item.source_ref,
      })),
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
  const nonempty = [
    ...titles,
    ...body,
    ...source.speaker_notes,
  ].some((value) => value.trim()) || source.tables.length > 0;
  return [
    { code: "source_unsaved", message: "文字修改尚未保存。" },
    { code: "source_unconfirmed", message: "文字来源尚未确认。" },
    { code: "source_review_incomplete", message: "来源审核尚未完成。" },
    ...(!nonempty
      ? [{ code: "chunk_body_empty" as const, message: "已确认来源无法生成非空 Chunk 正文。" }]
      : []),
  ];
}

function localImageBlockers(
  items: CurationImageSource[],
  drafts: Record<string, ImageDraft>,
): CurationBlocker[] {
  return items.flatMap((item): CurationBlocker[] => {
    const draft = drafts[item.source_ref] ?? draftFromImage(item);
    const number = String(item.position + 1).padStart(2, "0");
    if (!draft.disposition) return [{
      code: "image_disposition_required",
      message: `图片来源 ${number}：尚未选择保留或忽略。`,
      source_ref: item.source_ref,
    }];
    if (draft.disposition === "included" && !draft.summary.trim()) return [{
      code: "image_summary_required",
      message: `图片来源 ${number}：保留项缺少 summary。`,
      source_ref: item.source_ref,
    }];
    if (draft.disposition === "ignored" && !draft.ignoreReason) return [{
      code: "image_reason_required",
      message: `图片来源 ${number}：忽略项缺少原因。`,
      source_ref: item.source_ref,
    }];
    if (
      draft.disposition === "ignored" &&
      draft.ignoreReason === "other" &&
      !draft.ignoreNote.trim()
    ) return [{
      code: "image_other_note_required",
      message: `图片来源 ${number}：“其他”原因缺少说明。`,
      source_ref: item.source_ref,
    }];
    if (draft.disposition === "included" && item.object_sha256 === null) return [{
      code: "image_bytes_unavailable",
      message: `图片来源 ${number}：原始字节缺失或无法校验。`,
      source_ref: item.source_ref,
    }];
    if (draft.disposition === "included" && item.integrity === "hash_mismatch") {
      return [{
        code: "image_hash_mismatch",
        message: `图片来源 ${number}：原始字节与已记录哈希不一致。`,
        source_ref: item.source_ref,
      }];
    }
    if (draft.disposition === "included" && !item.media_type.startsWith("image/")) {
      return [{
        code: "image_media_type_unsupported",
        message: `图片来源 ${number}：媒体类型不受产物契约支持。`,
        source_ref: item.source_ref,
      }];
    }
    return [];
  });
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
  onTextDirtyChange,
  onImageDirtyChange,
  onDraftActionsChange,
  onOperationStateChange,
  onApproved,
  onExcluded,
  onReopened,
  externalCuration,
  captureVisuals,
  focusApprovalNonce,
  onCurationChange,
  onDetailLoaded,
  approvalPathReady,
  visualOperation,
  onAddCapture,
  onEditCapture,
  onMoveCapture,
  onDeleteCapture,
  onMarkSourceComplete,
  onSourceReviewCompleted,
  onModalStateChange,
}: {
  page: CurationPage;
  arrivalAnnouncement: string | null;
  statusRef: RefObject<HTMLDivElement | null>;
  onDirtyChange: (dirty: boolean) => void;
  onTextDirtyChange: (dirty: boolean) => void;
  onImageDirtyChange: (dirty: boolean) => void;
  onDraftActionsChange: (actions: SourceDraftActions | null) => void;
  onOperationStateChange: (busy: boolean) => void;
  onApproved: () => Promise<void>;
  onExcluded: () => Promise<void>;
  onReopened: () => Promise<void>;
  externalCuration: CurationState | null;
  captureVisuals: CurationVisual[];
  focusApprovalNonce: number;
  onCurationChange: (curation: CurationState) => void;
  onDetailLoaded: (detail: PageDetail) => void;
  approvalPathReady: boolean;
  visualOperation: string | null;
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
  onSourceReviewCompleted: () => void;
  onModalStateChange: (open: boolean) => void;
}) {
  const [detail, setDetail] = useState<PageDetail | null>(null);
  const [titles, setTitles] = useState<string[]>([]);
  const [body, setBody] = useState<string[]>([]);
  const [drafts, setDrafts] = useState<Record<string, ImageDraft>>({});
  const [expandedSourceRef, setExpandedSourceRef] = useState<string | null>(null);
  const [focusSourceRef, setFocusSourceRef] = useState<string | null>(null);
  const [imageDimensions, setImageDimensions] = useState<Record<string, string>>({});
  const [previewFailures, setPreviewFailures] = useState<Record<string, boolean>>({});
  const [previewRevisions, setPreviewRevisions] = useState<Record<string, number>>({});
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [retryRevision, setRetryRevision] = useState(0);
  const [textExpanded, setTextExpanded] = useState(true);
  const [textEditingEnabled, setTextEditingEnabled] = useState(true);
  const [activeTextEditor, setActiveTextEditor] = useState<ActiveTextEditor | null>(null);
  const [bodyManuscriptExpanded, setBodyManuscriptExpanded] = useState(false);
  const [bodyPreviewOverflow, setBodyPreviewOverflow] = useState(false);
  const [bodyAuditLocation, setBodyAuditLocation] = useState<BodyAuditLocation | null>(null);
  const [bodyBoundaryNotice, setBodyBoundaryNotice] = useState<string | null>(null);
  const [narrowBodyManuscript, setNarrowBodyManuscript] = useState(false);
  const [operation, setOperation] = useState<
    "text-review" | "image" | "review" | "approve" | "exclude" | "reopen" |
    "noise-preview" | "noise-confirm" | "noise-revoke" | null
  >(null);
  const [focusTarget, setFocusTarget] = useState<"review" | "approve" | null>(null);
  const [announcement, setAnnouncement] = useState<string | null>(arrivalAnnouncement);
  const [exclusionReason, setExclusionReason] = useState<ExclusionReason | "">("");
  const [exclusionNote, setExclusionNote] = useState("");
  const [exclusionExpanded, setExclusionExpanded] = useState(false);
  const [showReopen, setShowReopen] = useState(false);
  const [noiseFocusTarget, setNoiseFocusTarget] = useState<{
    kind: "check" | "revoke";
    sourceRef: string;
  } | null>(null);
  const [noiseCandidate, setNoiseCandidate] = useState<RepeatedFooterNoiseCandidate | null>(null);
  const [noiseAcknowledged, setNoiseAcknowledged] = useState(false);
  const [noiseNote, setNoiseNote] = useState("");
  const firstFieldRef = useRef<HTMLButtonElement>(null);
  const textEditorRefs = useRef<Record<string, HTMLElement | null>>({});
  const textEditButtonRefs = useRef<Record<string, HTMLButtonElement | null>>({});
  const bodyPreviewRef = useRef<HTMLDivElement>(null);
  const bodyExpandedRef = useRef<HTMLElement>(null);
  const bodyEditingSurfaceRef = useRef<HTMLDivElement>(null);
  const bodyCloseRef = useRef<HTMLButtonElement>(null);
  const bodyExpandButtonRef = useRef<HTMLButtonElement>(null);
  const bodyTextReviewRef = useRef<HTMLButtonElement>(null);
  const bodyReturnFocusKeyRef = useRef<string | null>(null);
  const bodyAuditPanelRef = useRef<HTMLElement>(null);
  const bodyAuditCloseRef = useRef<HTMLButtonElement>(null);
  const bodyAuditTriggerRefs = useRef<Record<string, HTMLButtonElement | null>>({});
  const bodyAuditReturnFocusRef = useRef<string | null>(null);
  const lastBodyIndexRef = useRef<number | null>(null);
  const textReviewRef = useRef<HTMLButtonElement>(null);
  const reviewRef = useRef<HTMLButtonElement>(null);
  const approveRef = useRef<HTMLButtonElement>(null);
  const exclusionReasonRef = useRef<HTMLSelectElement>(null);
  const exclusionFocusRequestedRef = useRef(false);
  const reopenTriggerRef = useRef<HTMLButtonElement>(null);
  const reopenDialogRef = useRef<HTMLElement>(null);
  const reopenSubmitRef = useRef<HTMLButtonElement>(null);
  const noiseDialogRef = useRef<HTMLElement>(null);
  const noiseSubmitRef = useRef<HTMLButtonElement>(null);
  const noiseAcknowledgeRef = useRef<HTMLInputElement>(null);
  const noiseTriggerRef = useRef<HTMLElement | null>(null);
  const noiseCandidateRequestRef = useRef<AbortController | null>(null);
  const noiseCheckButtonRefs = useRef<Record<string, HTMLButtonElement | null>>({});
  const noiseRevokeButtonRefs = useRef<Record<string, HTMLButtonElement | null>>({});
  const imageChoiceRefs = useRef<Record<string, HTMLInputElement | null>>({});
  const imageFieldRefs = useRef<Record<string, HTMLElement | null>>({});
  const lastModifiedTextKeyRef = useRef<string | null>(null);
  const textDraftNavigationRef = useRef<{
    activeTextEditor: ActiveTextEditor | null;
    savedSource: SourceContent | null;
    sourceConfirmed: boolean;
    imageSources: CurationImageSource[];
  }>({
    activeTextEditor: null,
    savedSource: null,
    sourceConfirmed: false,
    imageSources: [],
  });

  useLayoutEffect(() => {
    if (!exclusionExpanded || !exclusionFocusRequestedRef.current) return;
    exclusionFocusRequestedRef.current = false;
    exclusionReasonRef.current?.focus();
  }, [exclusionExpanded]);

  useLayoutEffect(() => {
    if (!noiseFocusTarget) return;
    const target = noiseFocusTarget.kind === "revoke"
      ? noiseRevokeButtonRefs.current[noiseFocusTarget.sourceRef]
      : noiseCheckButtonRefs.current[noiseFocusTarget.sourceRef];
    if (target) target.focus();
    else statusRef.current?.focus();
    setNoiseFocusTarget(null);
  }, [noiseFocusTarget, statusRef]);

  useEffect(() => {
    onModalStateChange(showReopen || noiseCandidate !== null || bodyAuditLocation !== null);
    return () => onModalStateChange(false);
  }, [bodyAuditLocation, noiseCandidate, onModalStateChange, showReopen]);

  useEffect(() => () => noiseCandidateRequestRef.current?.abort(), []);

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
        const normalizedDetail = { ...payload, source_content: original, curation };
        setDetail(normalizedDetail);
        onDetailLoaded(normalizedDetail);
        onCurationChange(curation);
        setTitles(effective.titles);
        setBody(effective.body);
        setDrafts(imageDrafts(curation.image_sources.items));
        setExpandedSourceRef(
          curation.image_sources.items.find((item) => item.disposition === null)?.source_ref ??
            curation.image_sources.items[0]?.source_ref ??
            null,
        );
        setImageDimensions({});
        setPreviewFailures({});
        setPreviewRevisions({});
        setTextExpanded(!curation.current_snapshot?.source_confirmation);
        setTextEditingEnabled(!curation.current_snapshot?.source_confirmation);
        setActiveTextEditor(null);
        setBodyManuscriptExpanded(false);
        setBodyAuditLocation(null);
        setBodyBoundaryNotice(null);
        lastModifiedTextKeyRef.current = null;
        lastBodyIndexRef.current = null;
        setExclusionReason("");
        setExclusionNote("");
        setExclusionExpanded(false);
        setShowReopen(false);
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
  }, [onCurationChange, onDetailLoaded, page.page_id, retryRevision]);

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
  const textDirty = Boolean(
    savedSource &&
      (JSON.stringify(titles) !== JSON.stringify(savedSource.titles) ||
        JSON.stringify(body) !== JSON.stringify(savedSource.body)),
  );
  const imageDirtyRefs = (curation?.image_sources.items ?? []).filter((item) => (
    JSON.stringify(drafts[item.source_ref] ?? draftFromImage(item)) !==
      JSON.stringify(draftFromImage(item))
  )).map((item) => item.source_ref);
  const imageDirty = imageDirtyRefs.length > 0;
  const dirty = textDirty || imageDirty;

  textDraftNavigationRef.current = {
    activeTextEditor,
    savedSource,
    sourceConfirmed: Boolean(curation?.current_snapshot?.source_confirmation),
    imageSources: curation?.image_sources.items ?? [],
  };

  const discardTextDraft = useCallback(() => {
    const { savedSource: persisted, sourceConfirmed } = textDraftNavigationRef.current;
    if (!persisted) return;
    setTitles([...persisted.titles]);
    setBody([...persisted.body]);
    setActiveTextEditor(null);
    setTextEditingEnabled(!sourceConfirmed);
    lastModifiedTextKeyRef.current = null;
  }, []);

  const restoreTextDraftFocus = useCallback(() => {
    const active = textDraftNavigationRef.current.activeTextEditor;
    const key = active
      ? `${active.kind}-${active.index}`
      : lastModifiedTextKeyRef.current;
    if (key) setTextExpanded(true);
    window.requestAnimationFrame(() => window.requestAnimationFrame(() => {
      if (document.querySelector("[aria-modal='true']")) return;
      const target = active
        ? textEditorRefs.current[key ?? ""]
        : key
          ? textEditButtonRefs.current[key]
          : firstFieldRef.current;
      target?.focus();
    }));
  }, []);

  const discardImageDrafts = useCallback(() => {
    setDrafts(imageDrafts(textDraftNavigationRef.current.imageSources));
  }, []);

  useEffect(() => {
    onDraftActionsChange({
      discardText: discardTextDraft,
      discardImages: discardImageDrafts,
      restoreTextFocus: restoreTextDraftFocus,
    });
    return () => onDraftActionsChange(null);
  }, [discardImageDrafts, discardTextDraft, onDraftActionsChange, restoreTextDraftFocus]);

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

  useEffect(() => {
    onTextDirtyChange(textDirty);
    return () => onTextDirtyChange(false);
  }, [onTextDirtyChange, textDirty]);

  useEffect(() => {
    onImageDirtyChange(imageDirty);
    return () => onImageDirtyChange(false);
  }, [imageDirty, onImageDirtyChange]);

  const blockers = useMemo<CurationBlocker[]>(() => {
    if (!curation) return [];
    const imageBlockers = localImageBlockers(curation.image_sources.items, drafts);
    if (textDirty && original) {
      return [...localBlockers(original, titles, body), ...imageBlockers];
    }
    if (imageDirty) {
      const stable = curation.blockers.filter((blocker) => (
        !blocker.code.startsWith("image_") && blocker.code !== "source_review_incomplete"
      ));
      const currentSourceRef = imageDirtyRefs[0];
      return [
        ...stable,
        { code: "source_review_incomplete", message: "来源审核尚未完成。" },
        {
          code: "image_changes_unsaved",
          message: `图片来源 ${String(
            (curation.image_sources.items.find((item) => item.source_ref === currentSourceRef)
              ?.position ?? 0) + 1,
          ).padStart(2, "0")}：修改尚未保存。`,
          source_ref: currentSourceRef,
        },
        ...imageBlockers,
      ];
    }
    return curation.blockers;
  }, [body, curation, drafts, imageDirty, imageDirtyRefs, original, textDirty, titles]);
  const snapshot = curation?.current_snapshot ?? null;
  const busy = operation !== null;
  const pending = (detail?.review_status ?? page.review_status) === "pending";

  useLayoutEffect(() => {
    onOperationStateChange(busy);
    return () => onOperationStateChange(false);
  }, [busy, onOperationStateChange]);

  const textBlockKey = (kind: SourceTextKind, index: number) => `${kind}-${index}`;
  const textBlockLabel = (kind: SourceTextKind, index: number) => (
    kind === "title" ? `标题 ${index + 1}` : `正文 ${String(index + 1).padStart(2, "0")}`
  );
  const textBlockValue = (kind: SourceTextKind, index: number) => (
    kind === "title" ? titles[index] ?? "" : body[index] ?? ""
  );

  const openTextEditor = (kind: SourceTextKind, index: number) => {
    if (!pending || busy || !textEditingEnabled) return;
    const key = textBlockKey(kind, index);
    setActiveTextEditor({ kind, index, baseline: textBlockValue(kind, index) });
    window.requestAnimationFrame(() => textEditorRefs.current[key]?.focus());
  };

  const updateBodyBlock = (index: number, value: string) => {
    const key = textBlockKey("body", index);
    lastModifiedTextKeyRef.current = key;
    lastBodyIndexRef.current = index;
    setBody((current) => current.map(
      (item, candidate) => candidate === index ? value : item,
    ));
  };

  const announceBodyBoundary = () => {
    setBodyBoundaryNotice(BODY_BOUNDARY_MESSAGE);
  };

  const replaceBodySelection = (selection: BodySelection, replacement: string) => {
    if (selection.startIndex !== selection.endIndex) return;
    const index = selection.startIndex;
    const current = body[index] ?? "";
    const next = `${current.slice(0, selection.startOffset)}${replacement}${
      current.slice(selection.endOffset)
    }`;
    flushSync(() => {
      updateBodyBlock(index, next);
      setBodyBoundaryNotice(null);
    });
    const caretOffset = selection.startOffset + replacement.length;
    placeBodyCaret(textEditorRefs.current[textBlockKey("body", index)], caretOffset);
  };

  const bodySelectionIsProtected = (selection: BodySelection | null) => (
    Boolean(selection && selection.startIndex !== selection.endIndex)
  );

  const handleBodyKeyDown = (event: React.KeyboardEvent<HTMLElement>, index: number) => {
    if (event.nativeEvent.isComposing || event.nativeEvent.keyCode === 229) return;
    const selection = currentBodySelection(bodyEditingSurfaceRef.current);
    if (!selection) return;
    const crossesBoundary = bodySelectionIsProtected(selection);
    if (event.key === "Enter") {
      event.preventDefault();
      event.stopPropagation();
      if (crossesBoundary) announceBodyBoundary();
      else replaceBodySelection(selection, "\n");
      return;
    }
    if (
      crossesBoundary && event.key.length === 1 &&
      !event.ctrlKey && !event.metaKey && !event.altKey
    ) {
      event.preventDefault();
      announceBodyBoundary();
      return;
    }
    if (event.key !== "Backspace" && event.key !== "Delete") return;
    const atProtectedEdge = selection.collapsed && selection.startIndex === index && (
      (event.key === "Backspace" && selection.startOffset === 0) ||
      (event.key === "Delete" && selection.endOffset === (body[index]?.length ?? 0))
    );
    if (!crossesBoundary && !atProtectedEdge) return;
    event.preventDefault();
    event.stopPropagation();
    announceBodyBoundary();
  };

  const syncBodyInput = (event: React.FormEvent<HTMLElement>, index: number) => {
    const selection = currentBodySelection(bodyEditingSurfaceRef.current);
    const caretOffset = selection?.collapsed && selection.startIndex === index
      ? selection.startOffset
      : null;
    flushSync(() => updateBodyBlock(index, bodyTextFromNode(event.currentTarget)));
    if (caretOffset === null || (event.nativeEvent as InputEvent).isComposing) return;
    placeBodyCaret(textEditorRefs.current[textBlockKey("body", index)], caretOffset);
  };

  const handleBodyPaste = (event: React.ClipboardEvent<HTMLElement>) => {
    const selection = currentBodySelection(bodyEditingSurfaceRef.current);
    if (!selection || bodySelectionIsProtected(selection)) {
      event.preventDefault();
      announceBodyBoundary();
      return;
    }
    event.preventDefault();
    replaceBodySelection(
      selection,
      event.clipboardData.getData("text/plain").replace(/\r\n?/g, "\n"),
    );
  };

  const handleBodyCut = (event: React.ClipboardEvent<HTMLElement>) => {
    const selection = currentBodySelection(bodyEditingSurfaceRef.current);
    if (!selection || selection.collapsed) return;
    if (!bodySelectionIsProtected(selection)) {
      event.preventDefault();
      event.clipboardData.setData(
        "text/plain",
        (body[selection.startIndex] ?? "").slice(selection.startOffset, selection.endOffset),
      );
      replaceBodySelection(selection, "");
      return;
    }
    event.preventDefault();
    announceBodyBoundary();
  };

  const handleBodyCopy = (event: React.ClipboardEvent<HTMLElement>) => {
    const selection = currentBodySelection(bodyEditingSurfaceRef.current);
    if (!selection || selection.collapsed) return;
    if (selection.startIndex === selection.endIndex) {
      event.preventDefault();
      event.clipboardData.setData(
        "text/plain",
        (body[selection.startIndex] ?? "").slice(selection.startOffset, selection.endOffset),
      );
      return;
    }
    const fragments = body.slice(selection.startIndex, selection.endIndex + 1);
    fragments[0] = fragments[0].slice(selection.startOffset);
    fragments[fragments.length - 1] = fragments[fragments.length - 1].slice(
      0,
      selection.endOffset,
    );
    event.preventDefault();
    event.clipboardData.setData("text/plain", fragments.join("\n\n"));
  };

  useEffect(() => {
    const root = bodyEditingSurfaceRef.current;
    if (!root || !bodyManuscriptExpanded || !pending || !textEditingEnabled || busy) return;
    const protectBodyStructure = (event: InputEvent) => {
      const target = event.target instanceof HTMLElement
        ? event.target.closest<HTMLElement>("[data-body-editable-index]")
        : null;
      if (!target || !root.contains(target)) return;
      const index = Number(target.dataset.bodyEditableIndex);
      const selection = currentBodySelection(root);
      if (!selection) return;
      const inputType = event.inputType ?? "";
      if (bodySelectionIsProtected(selection)) {
        event.preventDefault();
        announceBodyBoundary();
        return;
      }
      if (inputType === "insertParagraph" || inputType === "insertLineBreak") {
        event.preventDefault();
        replaceBodySelection(selection, "\n");
        return;
      }
      const atStart = selection.collapsed && selection.startOffset === 0;
      const atEnd = selection.collapsed && selection.endOffset === (body[index]?.length ?? 0);
      if (
        (inputType.endsWith("Backward") && atStart) ||
        (inputType.endsWith("Forward") && atEnd)
      ) {
        event.preventDefault();
        announceBodyBoundary();
      }
    };
    root.addEventListener("beforeinput", protectBodyStructure, true);
    return () => root.removeEventListener("beforeinput", protectBodyStructure, true);
  }, [body, bodyManuscriptExpanded, busy, pending, textEditingEnabled]);

  const closeBodyManuscript = useCallback(() => {
    setBodyManuscriptExpanded(false);
    setBodyAuditLocation(null);
    setBodyBoundaryNotice(null);
    setActiveTextEditor((current) => current?.kind === "body" ? null : current);
    window.requestAnimationFrame(() => window.requestAnimationFrame(() => {
      const key = bodyReturnFocusKeyRef.current;
      const target = key ? textEditButtonRefs.current[key] : bodyExpandButtonRef.current;
      target?.focus();
      bodyReturnFocusKeyRef.current = null;
    }));
  }, []);

  const openBodyManuscript = (index: number | null) => {
    if (!original?.body.length || busy) return;
    const preferredIndex = index ?? lastBodyIndexRef.current ?? 0;
    bodyReturnFocusKeyRef.current = index === null ? null : textBlockKey("body", index);
    setBodyManuscriptExpanded(true);
    setBodyAuditLocation(null);
    setBodyBoundaryNotice(null);
    if (pending && textEditingEnabled) {
      const key = textBlockKey("body", preferredIndex);
      setActiveTextEditor({
        kind: "body",
        index: preferredIndex,
        baseline: body[preferredIndex] ?? "",
      });
      window.requestAnimationFrame(() => window.requestAnimationFrame(() => {
        textEditorRefs.current[key]?.focus();
      }));
    } else {
      setActiveTextEditor(null);
      window.requestAnimationFrame(() => window.requestAnimationFrame(() => {
        bodyCloseRef.current?.focus();
      }));
    }
  };

  const closeBodyAudit = useCallback(() => {
    const candidateRequestPending = noiseCandidateRequestRef.current !== null;
    noiseCandidateRequestRef.current?.abort();
    noiseCandidateRequestRef.current = null;
    setOperation((current) => current === "noise-preview" ? null : current);
    if (candidateRequestPending) {
      setAnnouncement("已关闭来源审计；重复页脚检查已取消，整稿草稿保持不变。");
    }
    setBodyAuditLocation(null);
    window.requestAnimationFrame(() => window.requestAnimationFrame(() => {
      const triggerKey = bodyAuditReturnFocusRef.current;
      bodyAuditTriggerRefs.current[triggerKey ?? ""]?.focus();
      bodyAuditReturnFocusRef.current = null;
    }));
  }, []);

  const openBodyAudit = (index: number, mode: BodyAuditLocation["mode"]) => {
    if (busy) return;
    const triggerKey = `${mode}-${index}`;
    bodyAuditReturnFocusRef.current = triggerKey;
    setBodyAuditLocation({ index, mode });
  };

  const cancelTextEditor = () => {
    if (!activeTextEditor) return;
    const { kind, index, baseline } = activeTextEditor;
    if (kind === "title") {
      setTitles((current) => current.map(
        (item, candidate) => candidate === index ? baseline : item,
      ));
    } else {
      setBody((current) => current.map(
        (item, candidate) => candidate === index ? baseline : item,
      ));
    }
    const key = textBlockKey(kind, index);
    setActiveTextEditor(null);
    window.requestAnimationFrame(() => textEditButtonRefs.current[key]?.focus());
  };

  const enableTextEditing = () => {
    if (!pending || busy) return;
    setTextEditingEnabled(true);
    setAnnouncement(
      "已进入文字修订；当前仅为本地草稿，持久状态未改变。",
    );
    window.requestAnimationFrame(() => window.requestAnimationFrame(() => {
      firstFieldRef.current?.focus();
    }));
  };

  useEffect(() => {
    if (busy || !focusTarget) return;
    const target = focusTarget === "review"
        ? reviewRef.current
        : approveRef.current;
    if (!target || target.disabled) return;
    window.requestAnimationFrame(() => {
      target.focus();
      setFocusTarget(null);
    });
  }, [busy, curation, focusTarget]);

  useEffect(() => {
    if (busy || !focusSourceRef) return;
    const target = imageChoiceRefs.current[focusSourceRef];
    if (!target || target.disabled) return;
    window.requestAnimationFrame(() => {
      target.focus();
      setFocusSourceRef(null);
    });
  }, [busy, curation, focusSourceRef]);

  const applyCuration = useCallback((
    next: CurationState,
    { preserveImageDrafts = false }: { preserveImageDrafts?: boolean } = {},
  ) => {
    setDetail((current) => current ? { ...current, curation: next } : current);
    if (next.current_snapshot) {
      setTitles(next.current_snapshot.source_content.titles);
      setBody(next.current_snapshot.source_content.body);
    }
    if (!preserveImageDrafts) setDrafts(imageDrafts(next.image_sources.items));
    onCurationChange(next);
  }, [onCurationChange]);

  const refreshDetail = useCallback(async () => {
    if (!page.page_id) return;
    const payload = await loadPageDetail(page.page_id);
    const refreshedOriginal = normalizedSource(payload.source_content);
    const refreshedCuration = payload.curation ?? fallbackCuration(refreshedOriginal);
    const effective = normalizedSource(
      refreshedCuration.current_snapshot?.source_content ?? refreshedOriginal,
    );
    const normalizedDetail = {
      ...payload,
      source_content: refreshedOriginal,
      curation: refreshedCuration,
    };
    setDetail(normalizedDetail);
    setTitles(effective.titles);
    setBody(effective.body);
    setTextEditingEnabled(!refreshedCuration.current_snapshot?.source_confirmation);
    setActiveTextEditor(null);
    lastModifiedTextKeyRef.current = null;
    setDrafts(imageDrafts(refreshedCuration.image_sources.items));
    onDetailLoaded(normalizedDetail);
    onCurationChange(refreshedCuration);
  }, [onCurationChange, onDetailLoaded, page.page_id]);

  const closeNoiseDialog = useCallback(() => {
    setNoiseCandidate(null);
    setNoiseAcknowledged(false);
    setNoiseNote("");
    window.requestAnimationFrame(() => window.requestAnimationFrame(() => {
      noiseTriggerRef.current?.focus();
    }));
  }, []);

  const handleNoisePreview = async (sourceRef: string, trigger: HTMLElement) => {
    if (!page.page_id || busy || dirty) return;
    noiseCandidateRequestRef.current?.abort();
    const controller = new AbortController();
    noiseCandidateRequestRef.current = controller;
    noiseTriggerRef.current = trigger;
    setOperation("noise-preview");
    setAnnouncement("正在核对跨页重复来源…");
    try {
      const candidate = await loadRepeatedFooterNoiseCandidate(
        page.page_id,
        sourceRef,
        controller.signal,
      );
      if (controller.signal.aborted || noiseCandidateRequestRef.current !== controller) return;
      setNoiseCandidate(candidate);
      setNoiseAcknowledged(false);
      setNoiseNote("");
      setAnnouncement(
        `已找到 ${candidate.affected_pages.length} 页相同来源；确认前请逐页查看标准页渲染。`,
      );
    } catch (cause) {
      if (controller.signal.aborted) return;
      setAnnouncement(
        cause instanceof OperatorError
          ? cause.message
          : "跨页重复检查失败；来源内容和正文均未改变。",
      );
      window.requestAnimationFrame(() => noiseTriggerRef.current?.focus());
    } finally {
      if (noiseCandidateRequestRef.current === controller) {
        noiseCandidateRequestRef.current = null;
        setOperation((current) => current === "noise-preview" ? null : current);
      }
    }
  };

  const handleNoiseConfirm = async () => {
    if (!page.page_id || !noiseCandidate || !noiseAcknowledged || busy || dirty) return;
    const currentSourceRef = noiseCandidate.affected_pages.find(
      (item) => item.page_id === page.page_id,
    )?.source_ref ?? noiseCandidate.affected_pages[0].source_ref;
    setOperation("noise-confirm");
    try {
      await confirmRepeatedFooterNoise(
        page.page_id,
        noiseCandidate.candidate_id,
        currentSourceRef,
        noiseNote.trim() || null,
      );
      setNoiseCandidate(null);
      setNoiseAcknowledged(false);
      setNoiseNote("");
      try {
        await refreshDetail();
      } catch {
        setAnnouncement("重复页脚噪声确认已保存；详情暂未刷新，请重新加载当前页。");
        window.requestAnimationFrame(() => noiseTriggerRef.current?.focus());
        return;
      }
      setAnnouncement(
        `已确认 ${noiseCandidate.affected_pages.length} 页重复页脚噪声；仅后续 Chunk 正文发生变化。`,
      );
      setNoiseFocusTarget({ kind: "revoke", sourceRef: currentSourceRef });
    } catch (cause) {
      setAnnouncement(
        cause instanceof OperatorError
          ? cause.message
          : "重复页脚噪声确认未能保存；正文保持不变。",
      );
    } finally {
      setOperation(null);
    }
  };

  const handleNoiseRevoke = async (
    confirmationId: string,
    sourceNumber: number,
    sourceRef: string,
  ) => {
    if (busy || dirty) return;
    setOperation("noise-revoke");
    setAnnouncement(`正在撤销正文来源 ${sourceNumber} 的重复页脚排除…`);
    try {
      await revokeRepeatedFooterNoise(confirmationId, "从策展工作台撤销并恢复正文。");
      try {
        await refreshDetail();
      } catch {
        setAnnouncement("重复页脚排除撤销已保存；详情暂未刷新，请重新加载当前页。");
        window.requestAnimationFrame(() => noiseRevokeButtonRefs.current[sourceRef]?.focus());
        return;
      }
      setAnnouncement("重复页脚排除已撤销；后续 Chunk 正文已恢复，撤销审计已追加。 ");
      setNoiseFocusTarget({ kind: "check", sourceRef });
    } catch (cause) {
      setAnnouncement(
        cause instanceof OperatorError
          ? cause.message
          : "重复页脚排除未能撤销；正文状态未改变。",
      );
    } finally {
      setOperation(null);
    }
  };

  useEffect(() => {
    if (!externalCuration) return;
    const incomingId = externalCuration.current_snapshot?.snapshot_id ?? null;
    const currentId = curation?.current_snapshot?.snapshot_id ?? null;
    if (incomingId !== currentId) applyCuration(externalCuration);
  }, [applyCuration, curation?.current_snapshot?.snapshot_id, externalCuration]);

  useEffect(() => {
    if (!focusApprovalNonce || busy) return;
    window.requestAnimationFrame(() => approveRef.current?.focus());
  }, [busy, focusApprovalNonce]);

  const handleTextReview = async () => {
    if (
      !page.page_id || busy || !pending ||
      (!textDirty && Boolean(snapshot?.source_confirmation))
    ) return;
    setOperation("text-review");
    setAnnouncement("正在原子提交整页文字核对…");
    try {
      const result = await reviewCurationText(
        page.page_id,
        snapshot?.snapshot_id ?? null,
        titles,
        body,
      );
      applyCuration(result.curation, { preserveImageDrafts: imageDirty });
      setActiveTextEditor(null);
      setBodyManuscriptExpanded(false);
      bodyReturnFocusKeyRef.current = null;
      setTextEditingEnabled(false);
      setTextExpanded(false);
      if (result.next_unresolved_image) {
        setExpandedSourceRef(result.next_unresolved_image.source_ref);
        setFocusSourceRef(result.next_unresolved_image.source_ref);
        setAnnouncement("文字已确认，继续处理图片。");
      } else {
        setAnnouncement("文字及来源审核均已完成。");
        onSourceReviewCompleted();
      }
    } catch (cause) {
      const reason = cause instanceof OperatorError
        ? cause.message
          .replace(/[；。]?\s*持久(?:审核)?状态未改变。?\s*$/, "")
          .replace(/[；。]\s*$/, "")
        : "文字核对未能提交";
      setAnnouncement(`${reason}；持久审核状态未改变，本地文字修改仍保留。`);
      window.requestAnimationFrame(() => {
        const target = bodyManuscriptExpanded
          ? bodyTextReviewRef.current
          : textReviewRef.current;
        target?.focus();
      });
    } finally {
      setOperation(null);
    }
  };

  const updateImageDraft = (sourceRef: string, change: Partial<ImageDraft>) => {
    setDrafts((current) => ({
      ...current,
      [sourceRef]: {
        ...(current[sourceRef] ?? {
          disposition: null,
          summary: "",
          ignoreReason: null,
          ignoreNote: "",
        }),
        ...change,
      },
    }));
  };

  const handleImageSave = async (item: CurationImageSource) => {
    const draft = drafts[item.source_ref] ?? draftFromImage(item);
    if (
      !page.page_id || !snapshot || textDirty || busy || !draft.disposition ||
      !pending
    ) return;
    const itemBlocker = localImageBlockers([item], {
      [item.source_ref]: draft,
    })[0];
    if (itemBlocker) {
      setExpandedSourceRef(item.source_ref);
      setAnnouncement(itemBlocker.message);
      window.requestAnimationFrame(() => {
        const target = imageFieldRefs.current[item.source_ref] ??
          imageChoiceRefs.current[item.source_ref];
        target?.focus();
      });
      return;
    }
    setOperation("image");
    setAnnouncement(`正在保存图片来源 ${String(item.position + 1).padStart(2, "0")}…`);
    try {
      const next = await saveCurationImageSource(
        page.page_id,
        item.source_ref,
        snapshot.snapshot_id,
        draft.disposition,
        draft.disposition === "included" ? draft.summary : null,
        draft.disposition === "ignored" ? draft.ignoreReason : null,
        draft.disposition === "ignored" ? draft.ignoreNote : null,
      );
      const remainingDrafts = Object.fromEntries(next.image_sources.items.map((candidate) => [
        candidate.source_ref,
        candidate.source_ref === item.source_ref
          ? draftFromImage(candidate)
          : drafts[candidate.source_ref] ?? draftFromImage(candidate),
      ]));
      applyCuration(next);
      setDrafts(remainingDrafts);
      const needsAttention = (candidate: CurationImageSource) => {
        const effectiveDraft = remainingDrafts[candidate.source_ref] ?? draftFromImage(candidate);
        return (
          JSON.stringify(effectiveDraft) !== JSON.stringify(draftFromImage(candidate)) ||
          !imageDraftComplete(effectiveDraft)
        );
      };
      const nextItem = next.image_sources.items.find((candidate) => (
        candidate.position > item.position && needsAttention(candidate)
      )) ?? next.image_sources.items.find((candidate) => (
        needsAttention(candidate)
      ));
      if (nextItem) {
        setExpandedSourceRef(nextItem.source_ref);
        setFocusSourceRef(nextItem.source_ref);
        setAnnouncement(
          `图片来源 ${String(item.position + 1).padStart(2, "0")} 已保存。` +
          `请处理图片来源 ${String(nextItem.position + 1).padStart(2, "0")}。`,
        );
      } else {
        setExpandedSourceRef(item.source_ref);
        setAnnouncement("全部图片来源字段已保存；仍需显式完成来源审核。");
        setFocusTarget("review");
      }
    } catch (cause) {
      setAnnouncement(
        cause instanceof OperatorError
          ? `${cause.message} 本地图片处置仍保留。`
          : "图片来源处置未能保存；本地修改仍保留。",
      );
    } finally {
      setOperation(null);
    }
  };

  const focusBlocker = (blocker: CurationBlocker) => {
    if (blocker.source_ref) {
      setExpandedSourceRef(blocker.source_ref);
      window.requestAnimationFrame(() => window.requestAnimationFrame(() => {
        const target = imageFieldRefs.current[blocker.source_ref!] ??
          imageChoiceRefs.current[blocker.source_ref!];
        target?.focus();
      }));
      return;
    }
    if (blocker.code === "source_unsaved" || blocker.code === "source_unconfirmed") {
      setTextExpanded(true);
      window.requestAnimationFrame(() => window.requestAnimationFrame(() => {
        const target = blocker.code === "source_unsaved"
          ? firstFieldRef.current
          : textReviewRef.current;
        target?.focus();
      }));
    } else {
      const target = blocker.code === "source_review_incomplete" ? reviewRef.current : null;
      target?.focus();
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
    if (
      !page.page_id || !snapshot || dirty || busy || !curation?.can_approve ||
      !approvalPathReady
    ) return;
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
  }, [approvalPathReady, busy, curation?.can_approve, dirty, onApproved, page.page_id, snapshot]);

  const handleExclude = useCallback(async () => {
    if (!page.page_id || !pending || !exclusionReason || dirty || busy) return;
    setOperation("exclude");
    setAnnouncement("正在记录整页排除原因并冻结当前页面…");
    try {
      const result = await excludeCurationPage(
        page.page_id,
        exclusionReason,
        exclusionNote.trim() || null,
      );
      setDetail((current) => current ? {
        ...current,
        review_status: "excluded",
        review: result.review,
      } : current);
      setAnnouncement("页面已排除，正在转到下一待处理页。");
      await onExcluded();
    } catch (cause) {
      setAnnouncement(
        cause instanceof OperatorError
          ? `${cause.message} 当前原因和补充说明仍保留，可重试。`
          : "排除未完成；当前原因和补充说明仍保留，可重试。",
      );
    } finally {
      setOperation(null);
    }
  }, [busy, dirty, exclusionNote, exclusionReason, onExcluded, page.page_id, pending]);

  const closeReopenDialog = useCallback(() => {
    setShowReopen(false);
    window.requestAnimationFrame(() => reopenTriggerRef.current?.focus());
  }, []);

  const openReopenDialog = useCallback(() => {
    if (pending || busy) return;
    setShowReopen(true);
  }, [busy, pending]);

  const handleReopen = useCallback(async () => {
    if (!page.page_id || pending || busy) return;
    setOperation("reopen");
    try {
      const result = await reopenCurationPage(page.page_id);
      setDetail((current) => current ? {
        ...current,
        review_status: "pending",
        review: result.review,
      } : current);
      setShowReopen(false);
      setAnnouncement("页面已重新打开，恢复为待处理并解锁编辑。");
      await onReopened();
      try {
        await refreshDetail();
      } catch {
        setAnnouncement("页面已重新打开，但最新详情刷新失败，请刷新恢复。");
      }
      window.requestAnimationFrame(() => firstFieldRef.current?.focus());
    } catch (cause) {
      setAnnouncement(
        cause instanceof OperatorError
          ? `${cause.message} 页面仍保持冻结，可重试。`
          : "重新打开未完成；页面仍保持冻结，可重试。",
      );
      window.requestAnimationFrame(() => reopenSubmitRef.current?.focus());
    } finally {
      setOperation(null);
    }
  }, [busy, onReopened, page.page_id, pending, refreshDetail]);

  useEffect(() => {
    if (!showReopen) return;
    const frame = window.requestAnimationFrame(() => reopenSubmitRef.current?.focus());
    const handleDialogKey = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !busy) {
        event.preventDefault();
        closeReopenDialog();
        return;
      }
      if (event.key !== "Tab") return;
      const controls = Array.from(
        reopenDialogRef.current?.querySelectorAll<HTMLButtonElement>("button:not([disabled])") ?? [],
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
    };
    document.addEventListener("keydown", handleDialogKey);
    return () => {
      window.cancelAnimationFrame(frame);
      document.removeEventListener("keydown", handleDialogKey);
    };
  }, [busy, closeReopenDialog, showReopen]);

  useEffect(() => {
    if (!noiseCandidate) return;
    const frame = window.requestAnimationFrame(() => noiseAcknowledgeRef.current?.focus());
    const handleDialogKey = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !busy) {
        event.preventDefault();
        closeNoiseDialog();
        return;
      }
      if (event.key !== "Tab") return;
      const controls = Array.from(
        noiseDialogRef.current?.querySelectorAll<HTMLElement>(
          "a[href], input:not([disabled]), textarea:not([disabled]), button:not([disabled])",
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
    };
    document.addEventListener("keydown", handleDialogKey);
    return () => {
      window.cancelAnimationFrame(frame);
      document.removeEventListener("keydown", handleDialogKey);
    };
  }, [busy, closeNoiseDialog, noiseCandidate]);

  useEffect(() => {
    if (!bodyAuditLocation) return;
    const frame = window.requestAnimationFrame(() => bodyAuditCloseRef.current?.focus());
    const handleAuditKey = (event: KeyboardEvent) => {
      if (noiseCandidate || showReopen) return;
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopImmediatePropagation();
        closeBodyAudit();
        return;
      }
      if (event.key !== "Tab") return;
      const controls = Array.from(
        bodyAuditPanelRef.current?.querySelectorAll<HTMLElement>(
          "a[href], button:not([disabled]), input:not([disabled]), textarea:not([disabled])",
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
    };
    document.addEventListener("keydown", handleAuditKey);
    return () => {
      window.cancelAnimationFrame(frame);
      document.removeEventListener("keydown", handleAuditKey);
    };
  }, [bodyAuditLocation, closeBodyAudit, noiseCandidate, showReopen]);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      if (target?.matches("input, textarea, select, [contenteditable='true']")) return;
      if (document.querySelector("[aria-modal='true']")) return;
      const key = event.key.toLowerCase();
      if (key === "x" && pending && !busy) {
        event.preventDefault();
        exclusionFocusRequestedRef.current = true;
        setExclusionExpanded(true);
      } else if (key === "r" && !pending && !busy) {
        event.preventDefault();
        openReopenDialog();
      } else if (
        key === "a" && curation?.can_approve && approvalPathReady && !dirty && !busy
      ) {
        event.preventDefault();
        void handleApprove();
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [approvalPathReady, busy, curation?.can_approve, dirty, handleApprove, openReopenDialog, pending]);

  useLayoutEffect(() => {
    if (!textExpanded || bodyManuscriptExpanded || body.length === 0) {
      setBodyPreviewOverflow(false);
      return;
    }
    const preview = bodyPreviewRef.current;
    if (!preview) return;
    const measure = () => {
      setBodyPreviewOverflow(preview.scrollHeight > preview.clientHeight + 1);
    };
    measure();
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(measure);
    observer?.observe(preview);
    window.addEventListener("resize", measure);
    return () => {
      observer?.disconnect();
      window.removeEventListener("resize", measure);
    };
  }, [body, bodyManuscriptExpanded, textExpanded]);

  useLayoutEffect(() => {
    if (!bodyManuscriptExpanded) return;
    body.forEach((value, index) => {
      const editable = textEditorRefs.current[textBlockKey("body", index)];
      if (!editable) return;
      const hasLineStructure = editable.querySelector(":scope > [data-body-line]") !== null;
      if (!hasLineStructure || bodyTextFromNode(editable) !== value) {
        editable.innerHTML = bodyEditableHtml(value);
      }
    });
  }, [body, bodyManuscriptExpanded]);

  useEffect(() => {
    if (typeof window.matchMedia !== "function") return;
    const query = window.matchMedia("(max-width: 1100px)");
    const sync = () => setNarrowBodyManuscript(query.matches);
    sync();
    query.addEventListener("change", sync);
    return () => query.removeEventListener("change", sync);
  }, []);

  useEffect(() => {
    if (!bodyManuscriptExpanded) return;
    const handleExpandedKeys = (event: KeyboardEvent) => {
      if (event.isComposing || event.keyCode === 229) return;
      const higherModalOpen = Array.from(
        document.querySelectorAll<HTMLElement>("[aria-modal='true']"),
      ).some((element) => element !== bodyExpandedRef.current);
      if (higherModalOpen) return;
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopImmediatePropagation();
        closeBodyManuscript();
        return;
      }
      if (event.key !== "Tab" || !narrowBodyManuscript) return;
      const controls = Array.from(
        bodyExpandedRef.current?.querySelectorAll<HTMLElement>(
          "button:not([disabled]), textarea:not([disabled]), details > summary",
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
    };
    document.addEventListener("keydown", handleExpandedKeys, true);
    return () => document.removeEventListener("keydown", handleExpandedKeys, true);
  }, [bodyManuscriptExpanded, closeBodyManuscript, narrowBodyManuscript]);

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

  const disposedCount = curation.image_sources.items.filter((item) => (
    imageDraftComplete(draftFromImage(item))
  )).length;
  const displayedImageBlockers = localImageBlockers(curation.image_sources.items, drafts);
  const noiseSources = curation.repeated_footer_noise?.sources ?? [];
  const noiseHistory = curation.repeated_footer_noise?.history ?? [];
  const noiseMetadata = curation.chunk_metadata?.excluded_repeated_footer_noise ?? [];
  const review = detail.review ?? page.review ?? null;
  const frozenStatus = review?.status ?? detail.review_status;
  const frozenLabel = frozenStatus === "excluded" ? "已排除" : "已批准";
  const frozenCopy = frozenStatus === "excluded" ? "排除结论已冻结" : "批准结论已冻结";

  const renderBodyAttachedState = (index: number) => {
    const noiseSource = noiseSources.find((item) => item.source_index === index);
    const activeNoise = noiseMetadata.find(
      (item) => item.source_ref === noiseSource?.source_ref,
    );
    const latestNoiseHistory = noiseHistory.find(
      (item) => item.source_ref === noiseSource?.source_ref,
    );
    if (activeNoise) {
      return (
        <div className="footer-noise-source-state">
          <div>
            <strong>已从 Chunk 正文排除</strong>
            <span>{activeNoise.confirmed_by} · {formatTime(activeNoise.confirmed_at)}</span>
            <span>规则 {activeNoise.rule_version}</span>
          </div>
          <button
            type="button"
            ref={(element) => {
              noiseRevokeButtonRefs.current[activeNoise.source_ref] = element;
            }}
            aria-label={`撤销正文来源 ${index + 1} 的重复页脚排除`}
            disabled={busy || dirty}
            title={dirty ? "请先保存或还原当前文字与图片修改" : undefined}
            onClick={() => void handleNoiseRevoke(
              activeNoise.confirmation_id,
              index + 1,
              activeNoise.source_ref,
            )}
          >
            {operation === "noise-revoke" ? "正在撤销" : "撤销并恢复正文"}
          </button>
        </div>
      );
    }
    if (!noiseSource) return null;
    return (
      <>
        {latestNoiseHistory?.status === "revoked" ? (
          <div className="footer-noise-revoked-audit">
            <strong>最近一次排除已撤销</strong>
            <span>
              {latestNoiseHistory.revoked_by ?? "未知操作者"} · {
                latestNoiseHistory.revoked_at
                  ? formatTime(latestNoiseHistory.revoked_at)
                  : "时间未知"
              }
            </span>
            <span>规则 {latestNoiseHistory.rule_version}</span>
            {latestNoiseHistory.revoke_note ? <p>{latestNoiseHistory.revoke_note}</p> : null}
          </div>
        ) : null}
        <button
          type="button"
          ref={(element) => {
            noiseCheckButtonRefs.current[noiseSource.source_ref] = element;
          }}
          className="footer-noise-check"
          disabled={busy || dirty}
          title={dirty ? "请先保存或还原当前文字与图片修改" : undefined}
          onClick={(event) => void handleNoisePreview(
            noiseSource.source_ref,
            event.currentTarget,
          )}
        >
          {operation === "noise-preview"
            ? "正在检查是否为重复页脚噪声"
            : latestNoiseHistory?.status === "revoked"
              ? "再次检查是否为重复页脚噪声"
              : "检查是否为重复页脚噪声"}
        </button>
      </>
    );
  };

  const renderTextBlock = (
    kind: SourceTextKind,
    index: number,
    originalValue: string,
    attachedState?: React.ReactNode,
  ) => {
    const key = textBlockKey(kind, index);
    const label = textBlockLabel(kind, index);
    const currentValue = textBlockValue(kind, index);
    const modified = currentValue !== originalValue;
    const active = activeTextEditor?.kind === kind && activeTextEditor.index === index;
    const hasPrimaryActions = modified || (pending && textEditingEnabled);
    const isFirst = (kind === "title" && index === 0) || (
      kind === "body" && original.titles.length === 0 && index === 0
    );
    return (
      <article
        className={`source-manuscript-block is-${kind} ${modified ? "is-modified" : ""}`}
        data-source-text-block={key}
        key={key}
      >
        <span className="source-manuscript-number">{label}</span>
        <div className="source-manuscript-content">
          {active ? (
            <label className="source-inline-editor">
              <span>{`${label} 当前编辑值`}</span>
              <textarea
                ref={(element) => { textEditorRefs.current[key] = element; }}
                aria-label={`${label} 当前编辑值`}
                rows={kind === "title" ? 2 : 5}
                value={currentValue}
                disabled={busy}
                onChange={(event) => {
                  const value = event.target.value;
                  lastModifiedTextKeyRef.current = key;
                  if (kind === "title") {
                    setTitles((current) => current.map(
                      (item, candidate) => candidate === index ? value : item,
                    ));
                  } else {
                    setBody((current) => current.map(
                      (item, candidate) => candidate === index ? value : item,
                    ));
                  }
                }}
                onKeyDown={(event) => {
                  if (event.key !== "Escape") return;
                  event.preventDefault();
                  event.stopPropagation();
                  cancelTextEditor();
                }}
              />
            </label>
          ) : (
            <p
              className={`source-manuscript-text ${
                pending && textEditingEnabled && !busy ? "is-editable" : ""
              }`}
              title={pending && textEditingEnabled && !busy ? `点击编辑${label}` : undefined}
              onClick={() => openTextEditor(kind, index)}
            >
              {currentValue || <span className="source-empty-inline">空块</span>}
            </p>
          )}
          {hasPrimaryActions ? <div className="source-manuscript-actions">
            {modified ? <span className="source-modified-status">已修改</span> : null}
            {pending && textEditingEnabled ? (
              <button
                type="button"
                ref={(element) => {
                  textEditButtonRefs.current[key] = element;
                  if (isFirst) firstFieldRef.current = element;
                }}
                aria-label={`编辑${label}`}
                disabled={busy || active}
                onClick={() => openTextEditor(kind, index)}
              >
                {active ? "正在编辑" : "编辑"}
              </button>
            ) : null}
          </div> : null}
          {modified ? (
            <details className="source-original-disclosure">
              <summary>{`查看${label}的原始提取`}</summary>
              <p>{originalValue || "空块"}</p>
            </details>
          ) : null}
          {attachedState}
        </div>
      </article>
    );
  };

  const renderBodyBlock = (
    index: number,
    originalValue: string,
    mode: "preview" | "expanded",
  ) => {
    const key = textBlockKey("body", index);
    const label = textBlockLabel("body", index);
    const currentValue = textBlockValue("body", index);
    const modified = currentValue !== originalValue;
    const auditOpen = bodyAuditLocation?.index === index && bodyAuditLocation.mode === mode;
    const empty = currentValue.length === 0;
    const auditStatus = empty
      ? `当前值为空，${modified ? "已修改" : "未修改"}`
      : modified ? "已修改" : "未修改";
    const canEdit = pending && textEditingEnabled && !busy;
    return (
      <article
        className={`source-body-paragraph is-${mode} ${modified ? "is-modified" : ""}`}
        data-source-text-block={key}
        key={key}
      >
        <button
          type="button"
          ref={(element) => {
            bodyAuditTriggerRefs.current[`${mode}-${index}`] = element;
          }}
          className={`source-manuscript-number source-manuscript-number-button ${
            empty ? "is-empty" : modified ? "is-modified" : ""
          }`}
          aria-label={`${label}，${auditStatus}，打开来源审计`}
          aria-expanded={auditOpen}
          aria-pressed={auditOpen}
          aria-controls={`source-body-audit-panel-${index}`}
          onClick={() => openBodyAudit(index, mode)}
        >
          <span>{label}</span>
          {empty || modified ? (
            <span
              className={`source-number-state-mark ${empty ? "is-empty" : "is-modified"}`}
              aria-hidden="true"
            />
          ) : null}
        </button>
        <div className="source-body-paragraph-content">
          {mode === "preview" ? (
            <button
              type="button"
              ref={(element) => { textEditButtonRefs.current[key] = element; }}
              className="source-body-preview-text"
              aria-label={`从${label} 打开放大视图`}
              onClick={() => openBodyManuscript(index)}
            >
              {currentValue || <span className="source-empty-inline">空块</span>}
            </button>
          ) : (
            <div className={`source-expanded-editor ${canEdit ? "" : "is-readonly"}`}>
              <div
                ref={(element) => { textEditorRefs.current[key] = element; }}
                role="textbox"
                aria-label={canEdit ? `${label} 当前编辑值` : `${label} 当前只读值`}
                aria-multiline="true"
                aria-readonly={!canEdit}
                aria-placeholder={currentValue.length === 0 ? "空来源段落" : undefined}
                contentEditable={canEdit && !busy}
                suppressContentEditableWarning
                data-body-editable-index={index}
                data-empty={currentValue.length === 0 ? "true" : undefined}
                tabIndex={0}
                onFocus={() => {
                  lastBodyIndexRef.current = index;
                  if (canEdit) {
                    flushSync(() => setActiveTextEditor((current) => (
                      current?.kind === "body" && current.index === index
                        ? current
                        : { kind: "body", index, baseline: currentValue }
                    )));
                  }
                }}
                onKeyDown={canEdit ? (event) => handleBodyKeyDown(event, index) : undefined}
                onPaste={canEdit ? handleBodyPaste : undefined}
                onCut={canEdit ? handleBodyCut : undefined}
                onInput={canEdit ? (event) => syncBodyInput(event, index) : undefined}
              />
            </div>
          )}
        </div>
      </article>
    );
  };

  const auditedIndex = bodyAuditLocation?.index ?? null;
  const auditedLabel = auditedIndex === null ? null : textBlockLabel("body", auditedIndex);
  const auditedCurrentValue = auditedIndex === null ? "" : body[auditedIndex] ?? "";
  const auditedOriginalValue = auditedIndex === null ? "" : original.body[auditedIndex] ?? "";
  const auditedSavedValue = auditedIndex === null ? "" : savedSource?.body[auditedIndex] ?? "";
  const auditedModified = auditedIndex !== null && auditedCurrentValue !== auditedOriginalValue;
  const auditedLocalDraft = auditedIndex !== null && auditedCurrentValue !== auditedSavedValue;
  const auditedSource = auditedIndex === null
    ? null
    : noiseSources.find((item) => item.source_index === auditedIndex) ?? null;
  const auditedAttachedState = auditedIndex === null ? null : renderBodyAttachedState(auditedIndex);

  return (
    <div className="source-review-shell">
    <div
      className={`source-review-log ${bodyManuscriptExpanded ? "is-body-expanded" : ""}`}
      aria-busy={busy}
      inert={showReopen || noiseCandidate || bodyAuditLocation ? true : undefined}
    >
      <header className="source-review-header">
        <div>
          <h2>来源日志</h2>
          <p>
            普通策展 · 完整核对来源文字，按需原位修订
          </p>
        </div>
        <span className={`pending-chip ${pending ? "" : frozenStatus === "excluded" ? "is-excluded" : "is-approved"}`}>
          {pending ? "待处理" : frozenLabel}
        </span>
      </header>

      {bodyManuscriptExpanded ? (
        <section
          ref={bodyExpandedRef}
          className="source-body-expanded"
          role="dialog"
          aria-modal={narrowBodyManuscript ? "true" : undefined}
          aria-labelledby="source-body-expanded-heading"
        >
          <header>
            <div>
              <h3 id="source-body-expanded-heading">正文放大视图</h3>
              <p>
                {`${original.body.length} 段 · ${pending && textEditingEnabled ? "直接编辑本地草稿" : "只读核对"} · 统一保存时才会持久化`}
              </p>
            </div>
            <button
              ref={bodyCloseRef}
              type="button"
              className="source-body-expanded-close"
              onClick={closeBodyManuscript}
            >
              关闭正文放大视图 <kbd>Esc</kbd>
            </button>
          </header>
          <div className="source-body-expanded-scroll">
            {announcement ? (
              <div className="source-body-expanded-status" role="status" aria-live="polite">
                {announcement}
              </div>
            ) : null}
            <div
              ref={bodyEditingSurfaceRef}
              className="source-body-editing-surface"
              role="region"
              aria-label="连续正文编辑面"
              onCopy={handleBodyCopy}
            >
              {original.body.map((value, index) => renderBodyBlock(
                index,
                value,
                "expanded",
              ))}
            </div>
          </div>
          {bodyBoundaryNotice ? (
            <div
              className="source-body-boundary-status"
              role="status"
              aria-live="polite"
              aria-atomic="true"
            >
              {bodyBoundaryNotice}
            </div>
          ) : null}
          <footer>
            <span>{textDirty ? "本地草稿已保留，尚未保存" : "当前正文与已保存来源一致"}</span>
            <div className="source-body-expanded-actions">
              <button type="button" onClick={closeBodyManuscript}>返回来源日志</button>
              {pending && (!snapshot?.source_confirmation || textDirty) ? (
                <button
                  ref={bodyTextReviewRef}
                  type="button"
                  className="is-primary"
                  disabled={busy || (!textDirty && Boolean(snapshot?.source_confirmation))}
                  onClick={() => void handleTextReview()}
                >
                  {operation === "text-review"
                    ? "正在提交文字核对"
                    : textDirty
                      ? "保存并确认修改"
                      : "文字一致，确认"}
                </button>
              ) : null}
            </div>
          </footer>
        </section>
      ) : (
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
            <div className="source-text-heading-actions">
              <PhaseStatus complete={Boolean(snapshot?.source_confirmation)}>
                {snapshot?.source_confirmation
                  ? (textDirty ? "已确认 · 草稿待存" : "已确认")
                  : (textDirty ? "有本地修改" : "待确认")}
              </PhaseStatus>
              <button
                type="button"
                className="source-text-toggle"
                aria-expanded={textExpanded}
                aria-controls="source-text-work"
                onClick={() => setTextExpanded((current) => !current)}
              >{textExpanded ? "折叠文字核对" : "展开文字核对"}</button>
            </div>
          </header>

          {textExpanded ? (
          <div className="source-text-work" id="source-text-work">
          {snapshot?.source_confirmation && !textDirty && pending && !textEditingEnabled ? (
            <div className="source-confirmed-edit-boundary">
              <p>
                进入本地修订不会立即改变持久状态；保存新快照后，此前文字确认及来源审核将失效。
              </p>
              <button type="button" disabled={busy} onClick={enableTextEditing}>修改文字</button>
            </div>
          ) : textEditingEnabled && snapshot?.source_confirmation ? (
            <p className="source-review-invalidated">
              当前仅打开本地草稿，持久状态未改变；保存新快照后，此前文字确认及来源审核将失效。
            </p>
          ) : null}
          <div className="source-manuscript" role="region" aria-label="标题与正文核对稿">
            {original.titles.length === 0 && original.body.length === 0 ? (
              <div
                className="source-text-empty-state"
                role="status"
                aria-label="标题和正文来源为空"
              >
                <strong>未发现标题或正文来源</strong>
                <p>请对照标准页渲染，确认 AnyDoc 确实没有遗漏可用文字。</p>
              </div>
            ) : (
              <>
              {original.titles.length ? (
                <div className="source-manuscript-title-group" role="group" aria-label="标题">
                  {original.titles.map((value, index) => renderTextBlock("title", index, value))}
                </div>
              ) : <p className="source-empty-block">AnyDoc 未生成标题块。</p>}
              {original.body.length ? (
                <section
                  className="source-manuscript-body-group"
                  aria-labelledby="source-body-group-heading"
                >
                  <header className="source-manuscript-body-heading">
                    <h4 id="source-body-group-heading">正文</h4>
                    <span>{`${original.body.length} 段 · 保留原始段落边界`}</span>
                  </header>
                  <div className="source-manuscript-body-copy">
                  <div
                    ref={bodyPreviewRef}
                    className="source-body-preview"
                    role="region"
                    aria-label="正文整稿预览"
                  >
                  {original.body.map((value, index) => renderBodyBlock(
                    index,
                    value,
                    "preview",
                  ))}
                  </div>
                  <footer className={`source-body-preview-footer ${bodyPreviewOverflow ? "has-overflow" : ""}`}>
                    <span>
                      {bodyPreviewOverflow
                        ? "还有正文内容，请在放大视图中继续查看。"
                        : "正文已完整显示。"}
                    </span>
                    <button
                      type="button"
                      ref={(element) => {
                        bodyExpandButtonRef.current = element;
                        if (original.titles.length === 0) firstFieldRef.current = element;
                      }}
                      disabled={busy}
                      onClick={() => openBodyManuscript(null)}
                    >
                      {pending && textEditingEnabled ? "放大编辑正文" : "放大查看正文"}
                    </button>
                  </footer>
                  </div>
                </section>
              ) : <p className="source-empty-block">AnyDoc 未生成正文块。</p>}
              </>
            )}
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

          {snapshot?.source_confirmation ? (
            <>
              <p className="source-audit-record">
                {snapshot.source_confirmation.actor_id} · {formatTime(snapshot.source_confirmation.confirmed_at)}
              </p>
              {textDirty ? (
                <p className="source-persisted-boundary">此前文字确认仍保留至新快照保存</p>
              ) : null}
            </>
          ) : (
            <p className="source-phase-copy">
              此动作会一次提交完整标题、正文和当前基准，并明确记录人工文字确认。
            </p>
          )}
          {pending && (!snapshot?.source_confirmation || textDirty) ? (
          <button
            type="button"
            ref={textReviewRef}
            className="source-action-button"
            disabled={
              !pending || busy || (!textDirty && Boolean(snapshot?.source_confirmation))
            }
            onClick={() => void handleTextReview()}
          >
            {operation === "text-review"
              ? "正在提交文字核对"
              : original.titles.length === 0 && original.body.length === 0
                ? "确认无标题/正文来源"
                : textDirty
                  ? "保存并确认修改"
                  : "文字一致，确认"}
          </button>
          ) : null}
          </div>
          ) : (
            <div
              className="source-text-collapsed"
              role="status"
              aria-label="文字核对摘要"
            >
              <div>
                <strong>{snapshot?.source_confirmation ? "文字已确认" : "文字待确认"}</strong>
                <span>
                  {snapshot?.source_confirmation
                    ? `由 ${snapshot.source_confirmation.actor_id} 完成`
                    : "展开后继续核对"}
                </span>
              </div>
              <dl>
                <div><dt>标题</dt><dd>{titles.length}</dd></div>
                <div><dt>正文</dt><dd>{body.length}</dd></div>
                <div><dt>表格</dt><dd>{original.tables.length}</dd></div>
              </dl>
            </div>
          )}
        </section>

        <section className="source-phase source-image-phase" aria-labelledby="source-image-heading">
          <header>
            <div>
              <h3 id="source-image-heading">图片来源</h3>
              <p>固定沿用 AnyDoc 页内引用顺序</p>
            </div>
            <PhaseStatus complete={disposedCount === curation.image_sources.total}>
              {`${disposedCount} / ${curation.image_sources.total} 已处置`}
            </PhaseStatus>
          </header>

          {!snapshot?.source_confirmation || textDirty ? (
            <p className="source-image-locked">
              先提交整页文字核对，再逐项处置图片来源。
            </p>
          ) : curation.image_sources.total === 0 ? (
            <div className="source-image-empty">
              <strong>当前页没有图片来源</strong>
              <span>可继续完成来源审核并批准当前页。</span>
            </div>
          ) : (
            <div className="source-image-queue">
              {curation.image_sources.items.map((item) => {
                const number = String(item.position + 1).padStart(2, "0");
                const draft = drafts[item.source_ref] ?? draftFromImage(item);
                const expanded = expandedSourceRef === item.source_ref;
                const itemDirty = imageDirtyRefs.includes(item.source_ref);
                const savedComplete = imageDraftComplete(draftFromImage(item));
                const dispositionLabel = item.disposition === "included"
                  ? "保留"
                  : item.disposition === "ignored"
                    ? "忽略"
                    : "未处置";
                const previewFailed = previewFailures[item.source_ref];
                const previewable = browserPreviewable(item.media_type);
                const previewRevision = previewRevisions[item.source_ref] ?? 0;
                return (
                  <article
                    className={`source-image-item ${expanded ? "is-expanded" : ""} ${
                      savedComplete ? "is-complete" : "is-incomplete"
                    }`}
                    key={item.source_ref}
                  >
                    <button
                      type="button"
                      className="source-image-summary"
                      aria-expanded={expanded}
                      aria-controls={`source-image-${item.source_ref}`}
                      aria-label={`图片来源 ${number}，${item.alt_text || "无替代文字"}，${
                        itemDirty ? "修改未保存" : dispositionLabel
                      }`}
                      onClick={() => setExpandedSourceRef((current) => (
                        current === item.source_ref ? null : item.source_ref
                      ))}
                    >
                      <span className="source-image-number">{number}</span>
                      <span className="source-image-summary-copy">
                        <strong>{item.alt_text || "无替代文字"}</strong>
                        <small>{item.origin_part}</small>
                      </span>
                      {item.duplicate_object ? (
                        <span className="duplicate-object-chip">重复对象</span>
                      ) : null}
                      <span className={`source-image-disposition is-${item.disposition ?? "pending"}`}>
                        {itemDirty ? "未保存" : dispositionLabel}
                      </span>
                    </button>

                    {expanded ? (
                      <div className="source-image-editor" id={`source-image-${item.source_ref}`}>
                        <div className="source-image-preview">
                          {previewable && !previewFailed ? (
                            <>
                              <img
                                key={previewRevision}
                                src={`${item.preview_url}${previewRevision ? `?retry=${previewRevision}` : ""}`}
                                alt={`图片来源 ${number} 原始预览，${item.media_type}`}
                                onLoad={(event) => {
                                  const image = event.currentTarget;
                                  setImageDimensions((current) => ({
                                    ...current,
                                    [item.source_ref]: `${image.naturalWidth} × ${image.naturalHeight} px`,
                                  }));
                                }}
                                onError={() => setPreviewFailures((current) => ({
                                  ...current,
                                  [item.source_ref]: true,
                                }))}
                              />
                              {!imageDimensions[item.source_ref] ? (
                                <span className="source-preview-state">正在读取原始预览…</span>
                              ) : null}
                            </>
                          ) : (
                            <div
                              className="source-preview-unavailable"
                              role="img"
                              aria-label={`图片来源 ${number} 无法在浏览器内预览，${item.media_type}`}
                            >
                              <strong>{previewFailed ? "原始预览加载失败" : "浏览器无法预览此媒体类型"}</strong>
                              <span>{item.media_type} · 仍可依据来源元数据完成带审计的处置</span>
                              {previewFailed ? (
                                <button
                                  type="button"
                                  onClick={() => {
                                    setPreviewFailures((current) => ({
                                      ...current,
                                      [item.source_ref]: false,
                                    }));
                                    setPreviewRevisions((current) => ({
                                      ...current,
                                      [item.source_ref]: previewRevision + 1,
                                    }));
                                  }}
                                >
                                  重试原始预览
                                </button>
                              ) : null}
                            </div>
                          )}
                        </div>

                        <dl className="source-image-facts">
                          <div><dt>来源部件</dt><dd>{item.origin_part}</dd></div>
                          <div><dt>媒体类型</dt><dd>{item.media_type}</dd></div>
                          <div><dt>字节大小</dt><dd>{formatBytes(item.size_bytes)}</dd></div>
                          <div>
                            <dt>完整性</dt>
                            <dd className={`source-image-integrity is-${item.integrity}`}>
                              {item.integrity === "verified"
                                ? "SHA-256 已校验"
                                : item.integrity === "hash_mismatch"
                                  ? "哈希不一致"
                                  : "原始字节缺失"}
                            </dd>
                          </div>
                          <div><dt>像素尺寸</dt><dd>{imageDimensions[item.source_ref] ?? "预览后读取"}</dd></div>
                          <div><dt>替代文字</dt><dd>{item.alt_text || "未提供"}</dd></div>
                        </dl>

                        <fieldset className="source-image-decision" disabled={!pending || busy || textDirty}>
                          <legend>选择此来源的处置</legend>
                          <div className="source-image-decision-grid">
                            <label className={draft.disposition === "included" ? "is-selected" : ""}>
                              <input
                                ref={(node) => { imageChoiceRefs.current[item.source_ref] = node; }}
                                type="radio"
                                name={`disposition-${item.source_ref}`}
                                aria-label="保留原始图片"
                                checked={draft.disposition === "included"}
                                onChange={() => updateImageDraft(item.source_ref, {
                                  disposition: "included",
                                  ignoreReason: null,
                                  ignoreNote: "",
                                })}
                              />
                              <span><strong>保留原始图片</strong><small>进入产物并形成视觉对象引用</small></span>
                            </label>
                            <label className={draft.disposition === "ignored" ? "is-selected" : ""}>
                              <input
                                type="radio"
                                name={`disposition-${item.source_ref}`}
                                aria-label="忽略此来源"
                                checked={draft.disposition === "ignored"}
                                onChange={() => updateImageDraft(item.source_ref, {
                                  disposition: "ignored",
                                  summary: "",
                                })}
                              />
                              <span><strong>忽略此来源</strong><small>保留来源身份与审计，不进入产物</small></span>
                            </label>
                          </div>

                          {draft.disposition === "included" ? (
                            <div className="source-image-branch">
                              <p className="source-original-contract">
                                将以原始字节与媒体类型进入产物
                              </p>
                              <label>
                                <span>自足 summary <strong aria-hidden="true">必填</strong></span>
                                <textarea
                                  ref={(node) => { imageFieldRefs.current[item.source_ref] = node; }}
                                  aria-label={`图片来源 ${number} summary`}
                                  rows={4}
                                  value={draft.summary}
                                  onChange={(event) => updateImageDraft(item.source_ref, {
                                    summary: event.target.value,
                                  })}
                                />
                              </label>
                            </div>
                          ) : null}

                          {draft.disposition === "ignored" ? (
                            <div className="source-image-branch">
                              <label>
                                <span>忽略原因 <strong aria-hidden="true">必填</strong></span>
                                <select
                                  ref={(node) => {
                                    if (draft.ignoreReason !== "other") {
                                      imageFieldRefs.current[item.source_ref] = node;
                                    }
                                  }}
                                  aria-label={`图片来源 ${number} 忽略原因`}
                                  value={draft.ignoreReason ?? ""}
                                  onChange={(event) => updateImageDraft(item.source_ref, {
                                    ignoreReason: event.target.value as ImageIgnoreReason,
                                    ignoreNote: event.target.value === "other" ? draft.ignoreNote : "",
                                  })}
                                >
                                  <option value="">选择稳定原因</option>
                                  {IGNORE_REASONS.map((reason) => (
                                    <option key={reason.value} value={reason.value}>{reason.label}</option>
                                  ))}
                                </select>
                              </label>
                              {draft.ignoreReason === "other" ? (
                                <label>
                                  <span>其他原因说明 <strong aria-hidden="true">必填</strong></span>
                                  <textarea
                                    ref={(node) => { imageFieldRefs.current[item.source_ref] = node; }}
                                    aria-label={`图片来源 ${number} 其他原因说明`}
                                    rows={3}
                                    value={draft.ignoreNote}
                                    onChange={(event) => updateImageDraft(item.source_ref, {
                                      ignoreNote: event.target.value,
                                    })}
                                  />
                                </label>
                              ) : null}
                            </div>
                          ) : null}
                        </fieldset>

                        {item.decided_by && !itemDirty ? (
                          <p className="source-audit-record">
                            {item.decided_by} · {item.decided_at ? formatTime(item.decided_at) : "时间未知"}
                          </p>
                        ) : null}
                        <button
                          type="button"
                          className="source-action-button"
                          disabled={
                            !pending || busy || textDirty || !draft.disposition || !itemDirty ||
                            localImageBlockers([item], { [item.source_ref]: draft }).length > 0
                          }
                          onClick={() => void handleImageSave(item)}
                        >
                          {operation === "image" ? "正在保存此项" : "保存并处理下一项"}
                        </button>
                      </div>
                    ) : null}
                  </article>
                );
              })}
            </div>
          )}
          {imageDirty && snapshot?.source_review ? (
            <p className="source-review-invalidated" role="status">
              当前图片修改仅保存在本地；保存后来源审核确认将失效。
            </p>
          ) : null}
        </section>

        <section className="source-phase" aria-labelledby="source-review-heading">
          <header>
            <div>
              <h3 id="source-review-heading">来源复核</h3>
              <p>关闭文字与图片来源的完整审核阶段</p>
            </div>
            <PhaseStatus complete={Boolean(snapshot?.source_review)}>
              {snapshot?.source_review ? (dirty ? "已完成 · 草稿待存" : "已完成") : "未完成"}
            </PhaseStatus>
          </header>
          {snapshot?.source_review ? (
            <>
              <p className="source-audit-record">
                {snapshot.source_review.actor_id} · {formatTime(snapshot.source_review.completed_at)}
              </p>
              {dirty ? (
                <p className="source-persisted-boundary">此前来源审核仍保留至新快照保存</p>
              ) : null}
            </>
          ) : (
            <p className="source-phase-copy">
              {displayedImageBlockers.length
                ? `${displayedImageBlockers.length} 个图片来源尚待逐项处置。`
                : "文字确认后，可显式完成来源审核。"}
            </p>
          )}
          {pending && !snapshot?.source_review ? (
          <button
            type="button"
            ref={reviewRef}
            className="source-action-button"
            disabled={
              !pending || busy || dirty || !snapshot?.source_confirmation ||
              Boolean(snapshot.source_review) || displayedImageBlockers.length > 0
            }
            onClick={() => void handleReview()}
          >
            {operation === "review" ? "正在完成审核" : "完成来源审核"}
          </button>
          ) : null}
        </section>

        {captureVisuals.length > 0 || blockers.some((blocker) => blocker.code === "capture_required") ? (
          <section className="source-phase capture-summary" aria-labelledby="capture-summary-heading">
            <header>
              <div>
                <h3 id="capture-summary-heading">人工截图</h3>
                <p>语义顺序由可见编号确定 · AnyDoc 引用顺序不变</p>
              </div>
              <PhaseStatus complete={captureVisuals.length > 0}>
                {captureVisuals.length ? `${captureVisuals.length} 个已保存` : "来源有缺口"}
              </PhaseStatus>
            </header>
            <div className="capture-summary-list">
              {captureVisuals.map((captureVisual, index) => {
                const number = index + 1;
                const label = String(number).padStart(2, "0");
                const operationKey = `${captureVisual.visual_ref}:`;
                return (
                  <article className="capture-summary-row" key={captureVisual.visual_ref}>
                    <span className="capture-summary-number">{label}</span>
                    <button
                      type="button"
                      className="capture-summary-main"
                      aria-label={`编辑视觉对象 ${label}`}
                      disabled={!pending || visualOperation !== null}
                      onClick={(event) => onEditCapture(captureVisual.visual_ref, event.currentTarget)}
                    >
                      <strong>{captureVisual.summary || "缺少 summary"}</strong>
                      <span>
                        {captureVisual.visual_type || "未分类"}
                        {captureVisual.asset?.width_px && captureVisual.asset?.height_px
                          ? ` · ${captureVisual.asset.width_px} × ${captureVisual.asset.height_px} px`
                          : ""}
                      </span>
                    </button>
                    {pending ? (
                      <div className="capture-summary-actions">
                        <button
                          type="button"
                          aria-label={`视觉对象 ${label} 上移`}
                          disabled={index === 0 || visualOperation !== null}
                          onClick={(event) => onMoveCapture(
                            captureVisual.visual_ref,
                            "up",
                            number,
                            event.currentTarget,
                          )}
                        >上移</button>
                        <button
                          type="button"
                          aria-label={`视觉对象 ${label} 下移`}
                          disabled={index === captureVisuals.length - 1 || visualOperation !== null}
                          onClick={(event) => onMoveCapture(
                            captureVisual.visual_ref,
                            "down",
                            number,
                            event.currentTarget,
                          )}
                        >下移</button>
                        <button
                          type="button"
                          className="is-danger"
                          aria-label={`删除视觉对象 ${label}`}
                          disabled={visualOperation?.startsWith(operationKey) ?? false}
                          onClick={(event) => onDeleteCapture(
                            captureVisual.visual_ref,
                            number,
                            event.currentTarget,
                          )}
                        >删除</button>
                      </div>
                    ) : null}
                  </article>
                );
              })}
            </div>
            {pending ? (
              <div className="capture-summary-footer">
                <button
                  type="button"
                  disabled={visualOperation !== null}
                  onClick={(event) => onAddCapture(event.currentTarget)}
                >{captureVisuals.length ? "再截一个" : "重新框选"}</button>
                {captureVisuals.length === 0 ? (
                  <button
                    type="button"
                    disabled={visualOperation !== null}
                    onClick={(event) => onMarkSourceComplete(event.currentTarget)}
                  >改选来源完整</button>
                ) : null}
              </div>
            ) : null}
          </section>
        ) : null}
      </div>
      )}

      <section className={`review-gate ${pending && blockers.length ? "is-blocked" : "is-clear"}`} aria-labelledby="review-gate-heading">
        <header>
          <div>
            <h3 id="review-gate-heading">页面结论</h3>
            <p>
              {!pending
                ? frozenCopy
                : blockers.length
                  ? `${blockers.length} 项结构性阻塞`
                  : !approvalPathReady
                    ? "等待来源完整性选择"
                    : captureVisuals.length
                    ? `来源已补全 · ${captureVisuals.length} 个视觉对象`
                    : "来源完整 · 无需截图"}
            </p>
          </div>
          <span>
            {!pending
              ? frozenLabel
              : blockers.length
                ? "阻塞"
                : approvalPathReady
                  ? "可批准"
                  : "待选择"}
          </span>
        </header>
        {pending && blockers.length ? (
          <ul>
            {blockers.map((blocker, index) => (
              <li key={`${blocker.code}:${blocker.source_ref ?? index}`}>
                <button type="button" onClick={() => focusBlocker(blocker)}>
                  {blocker.message}
                </button>
              </li>
            ))}
          </ul>
        ) : (
          <p className="review-gate-clear">
            {pending
              ? approvalPathReady
                ? "当前确认来源可生成非空 Chunk 正文。"
                : "请在中央标准页渲染结果下方选择来源是否完整。"
              : `当前页面保留${frozenLabel}结论及其来源审核记录。`}
          </p>
        )}
        {!pending && review ? (
          <dl className="frozen-review-facts">
            <div><dt>原结论</dt><dd>{frozenLabel}</dd></div>
            {review.inherited_from_page_version_id ? (
              <div><dt>继承来源</dt><dd>{review.inherited_from_page_version_id}</dd></div>
            ) : null}
            <div><dt>原策展人员</dt><dd>{review.reviewed_by ?? "历史记录未署名"}</dd></div>
            <div><dt>结论时间</dt><dd>{review.reviewed_at ? formatTime(review.reviewed_at) : "历史记录未提供时间"}</dd></div>
            {review.exclusion_reason ? (
              <div><dt>排除原因</dt><dd>{EXCLUSION_REASONS.find((item) => item.value === review.exclusion_reason)?.label ?? review.exclusion_reason}</dd></div>
            ) : null}
            {review.exclusion_note ? <div><dt>补充说明</dt><dd>{review.exclusion_note}</dd></div> : null}
          </dl>
        ) : null}
        {pending ? (
          <div className="review-decision-actions">
            <button
              type="button"
              ref={approveRef}
              disabled={busy || dirty || !curation.can_approve || !approvalPathReady}
              onClick={() => void handleApprove()}
            >
              {operation === "approve" ? "正在批准" : "批准并转到下一待处理页"}
            </button>
            <button
              type="button"
              className="review-exclusion-toggle"
              aria-expanded={exclusionExpanded}
              aria-controls="page-exclusion-form"
              disabled={busy}
              onClick={() => setExclusionExpanded((current) => {
                exclusionFocusRequestedRef.current = !current;
                return !current;
              })}
            >
              {exclusionExpanded ? "收起排除选项" : "排除此页"}
            </button>
            {exclusionExpanded ? (
              <div className="page-exclusion-form" id="page-exclusion-form">
                <label>
                  <span>整页排除原因</span>
                  <select
                    ref={exclusionReasonRef}
                    aria-label="整页排除原因"
                    value={exclusionReason}
                    disabled={busy}
                    onChange={(event) => setExclusionReason(event.target.value as ExclusionReason | "")}
                  >
                    <option value="">请选择规定原因</option>
                    {EXCLUSION_REASONS.map((reason) => (
                      <option key={reason.value} value={reason.value}>{reason.label}</option>
                    ))}
                  </select>
                </label>
                <label>
                  <span>补充说明（可选）</span>
                  <textarea
                    aria-label="整页排除补充说明"
                    rows={2}
                    value={exclusionNote}
                    disabled={busy}
                    onChange={(event) => setExclusionNote(event.target.value)}
                  />
                </label>
                <button
                  type="button"
                  className="is-danger"
                  disabled={busy || dirty || !exclusionReason}
                  onClick={() => void handleExclude()}
                >
                  {operation === "exclude" ? "正在排除" : "排除并转到下一待处理页"}
                </button>
              </div>
            ) : null}
          </div>
        ) : (
          <button
            ref={reopenTriggerRef}
            type="button"
            className="reopen-page-button"
            disabled={busy}
            onClick={openReopenDialog}
          >重新打开此页</button>
        )}
      </section>
    </div>
    {bodyAuditLocation && auditedIndex !== null && auditedLabel ? (
      <section
        ref={bodyAuditPanelRef}
        id={`source-body-audit-panel-${auditedIndex}`}
        className="source-body-audit-panel"
        role="dialog"
        aria-modal="true"
        aria-labelledby="source-body-audit-heading"
        aria-describedby="source-body-audit-status"
        inert={showReopen || noiseCandidate ? true : undefined}
      >
        <header className="source-body-audit-header">
          <div>
            <h2 id="source-body-audit-heading">{auditedLabel} · 来源审计</h2>
            <p>{`来源文字块 ${String(auditedIndex + 1).padStart(2, "0")} · AnyDoc 正文 · 原始顺序 ${auditedIndex + 1}`}</p>
          </div>
          <button
            ref={bodyAuditCloseRef}
            type="button"
            aria-label={`关闭${auditedLabel} 来源审计`}
            onClick={closeBodyAudit}
          >
            关闭 <kbd>Esc</kbd>
          </button>
        </header>

        <div className="source-body-audit-scroll">
          <div
            id="source-body-audit-status"
            className={`source-body-audit-status ${
              auditedCurrentValue.length === 0
                ? "is-empty"
                : auditedModified ? "is-modified" : "is-matching"
            }`}
            role="status"
            aria-label={`${auditedLabel} 修改状态`}
          >
            <span aria-hidden="true" />
            <strong>
              {auditedCurrentValue.length === 0
                ? "当前值为空"
                : auditedModified
                  ? "当前值与 AnyDoc 原文不同"
                  : "当前值与 AnyDoc 原文一致"}
            </strong>
            <small>
              {auditedLocalDraft
                ? "当前值有未保存修改"
                : auditedModified
                  ? "当前修改已经保存"
                  : "未修改"}
            </small>
          </div>

          <div className="source-body-audit-compare" aria-label={`${auditedLabel} 当前值与 AnyDoc 原文对照`}>
            <section role="region" aria-label={`${auditedLabel} 当前值`}>
              <header>
                <h3>当前值</h3>
                <span>{auditedLocalDraft ? "本地草稿" : "已保存值"}</span>
              </header>
              {auditedCurrentValue.length ? (
                <p>{auditedCurrentValue}</p>
              ) : (
                <p className="source-body-audit-empty">当前值为空</p>
              )}
            </section>
            <section role="region" aria-label={`${auditedLabel} AnyDoc 原文`}>
              <header>
                <h3>AnyDoc 原文</h3>
                <span>只读来源</span>
              </header>
              {auditedOriginalValue.length ? (
                <p>{auditedOriginalValue}</p>
              ) : (
                <p className="source-body-audit-empty">AnyDoc 原文为空</p>
              )}
            </section>
          </div>

          <section className="source-body-audit-facts" aria-labelledby="source-body-audit-facts-heading">
            <h3 id="source-body-audit-facts-heading">来源身份与审计记录</h3>
            <dl>
              <div><dt>来源类型</dt><dd>AnyDoc 正文</dd></div>
              <div><dt>原始顺序</dt><dd>{String(auditedIndex + 1).padStart(2, "0")}</dd></div>
              <div><dt>来源引用</dt><dd>{auditedSource?.source_ref ?? "当前响应未提供"}</dd></div>
              <div>
                <dt>文字确认</dt>
                <dd>
                  {snapshot?.source_confirmation
                    ? `${snapshot.source_confirmation.actor_id} · ${formatTime(snapshot.source_confirmation.confirmed_at)}`
                    : "尚未确认"}
                </dd>
              </div>
            </dl>
          </section>

          {auditedAttachedState ? (
            <section className="source-body-audit-operations" aria-labelledby="source-body-audit-operations-heading">
              <header>
                <h3 id="source-body-audit-operations-heading">逐块审计操作</h3>
                <p>操作只影响该来源的审计处置，不会保存整稿草稿。</p>
              </header>
              {auditedAttachedState}
            </section>
          ) : (
            <section className="source-body-audit-operations is-empty" aria-label="逐块审计操作">
              <h3>逐块审计操作</h3>
              <p>此来源当前没有待执行的逐块操作。</p>
            </section>
          )}

          {announcement ? (
            <p className="source-body-audit-announcement" role="status" aria-live="polite">
              {announcement}
            </p>
          ) : null}
        </div>
        <footer>
          <span>关闭面板不会保存、还原或丢弃整稿草稿</span>
          <button type="button" onClick={closeBodyAudit}>返回正文</button>
        </footer>
      </section>
    ) : null}
    {showReopen ? (
      <div
        className="dialog-backdrop reopen-page-backdrop"
        onMouseDown={(event) => {
          if (event.target === event.currentTarget && !busy) closeReopenDialog();
        }}
      >
        <section
          ref={reopenDialogRef}
          className="reopen-page-dialog"
          role="dialog"
          aria-modal="true"
          aria-labelledby="reopen-page-heading"
        >
          <h2 id="reopen-page-heading">重新打开第 {page.page_number} 页？</h2>
          <p>页面将恢复为待处理并解锁编辑。原结论不会改写，只保留在审计历史中。</p>
          <div>
            <button type="button" disabled={busy} onClick={closeReopenDialog}>取消，保持冻结</button>
            <button
              ref={reopenSubmitRef}
              type="button"
              disabled={busy}
              onClick={() => void handleReopen()}
            >{operation === "reopen" ? "正在重新打开" : "确认重新打开"}</button>
          </div>
        </section>
      </div>
    ) : null}
    {noiseCandidate ? (
      <div
        className="dialog-backdrop footer-noise-backdrop"
        onMouseDown={(event) => {
          if (event.target === event.currentTarget && !busy) closeNoiseDialog();
        }}
      >
        <section
          ref={noiseDialogRef}
          className="footer-noise-dialog"
          role="dialog"
          aria-modal="true"
          aria-labelledby="footer-noise-heading"
        >
          <header>
            <div>
              <h2 id="footer-noise-heading">确认排除重复页脚噪声</h2>
              <p>共影响 {noiseCandidate.affected_pages.length} 页</p>
            </div>
            <span>人工确认</span>
          </header>
          <blockquote>{noiseCandidate.source_text}</blockquote>
          <p className="footer-noise-dialog-copy">
            系统只按规范化后的精确文本生成候选，不会自动改变正文、审核状态或页指纹。
            请在提交前逐页核对位置与语义。
          </p>
          <ol className="footer-noise-pages">
            {noiseCandidate.affected_pages.map((affected) => (
              <li key={affected.source_ref}>
                <span>第 {affected.page_number} 页</span>
                <a
                  href={affected.standard_render.url}
                  target="_blank"
                  rel="noreferrer"
                  aria-label={`查看第 ${affected.page_number} 页标准页渲染`}
                >查看标准页渲染</a>
              </li>
            ))}
          </ol>
          <label className="footer-noise-acknowledgement">
            <input
              ref={noiseAcknowledgeRef}
              type="checkbox"
              checked={noiseAcknowledged}
              onChange={(event) => setNoiseAcknowledged(event.target.checked)}
            />
            <span>我已核对全部受影响页</span>
          </label>
          <label className="footer-noise-note">
            <span>确认说明（可选）</span>
            <textarea
              aria-label="确认说明（可选）"
              rows={3}
              value={noiseNote}
              onChange={(event) => setNoiseNote(event.target.value)}
            />
          </label>
          <div className="footer-noise-dialog-actions">
            <button type="button" disabled={busy} onClick={closeNoiseDialog}>
              取消，保留为内容
            </button>
            <button
              ref={noiseSubmitRef}
              type="button"
              disabled={busy || dirty || !noiseAcknowledged}
              onClick={() => void handleNoiseConfirm()}
            >
              {operation === "noise-confirm"
                ? "正在记录确认"
                : `确认排除 ${noiseCandidate.affected_pages.length} 页中的此来源`}
            </button>
          </div>
        </section>
      </div>
    ) : null}
    </div>
  );
}
