export interface Actor {
  actor_id: string;
  display_name: string;
}

export interface DocumentSummary {
  document_id: string;
  version_id?: string;
  title: string;
  status?: JobStatus;
  status_label?: string;
  action?: { label: string; href: string } | null;
  rendering_warnings?: RenderingWarningSummary;
}

export interface Runway {
  id: "pending" | "processing" | "curatable";
  label: "待处理" | "处理中" | "可策展";
  documents: DocumentSummary[];
}

export interface BootstrapData {
  actor: Actor;
  runways: Runway[];
}

export interface UploadAccepted {
  document_id: string;
  version_id: string;
  job_id: string;
  status: "accepted" | "coalesced" | "no_change";
}

export interface SourceReference {
  slide_id: number;
  relationship_id: string;
  part: string;
}

export interface JobError {
  code: string;
  message: string;
  phase?: string;
  retryable?: boolean;
}

export type JobStatus = "queued" | "running" | "requires_action" | "succeeded" | "failed" | "cancelled" | "ready";

export interface RenderingWarningSummary {
  total: number;
  pages: number;
  unconfirmed: number;
  unconfirmed_pages: number;
}

export interface RenderingWarning {
  warning_id: string;
  page_number: number;
  code: "missing_font" | "animation_flattened";
  details: {
    requested_font?: string | null;
    replacement_font?: string | null;
    timeline_count?: number | null;
  };
  render_config_version: string;
  observed_at: string;
  status: "unconfirmed" | "confirmed";
  confirmed_by: string | null;
  confirmed_at: string | null;
}

export interface EnablementState {
  status: JobStatus | "not_started";
  job_id: string | null;
  error: JobError | null;
}

export type ReviewStatus = "pending" | "approved" | "excluded";

export type ExclusionReason =
  | "no_meaningful_content"
  | "duplicate"
  | "irrelevant"
  | "unreadable"
  | "other";

export interface PageReview {
  status: ReviewStatus;
  reviewed_by: string | null;
  reviewed_at: string | null;
  source_version_id: string | null;
  inherited_from_page_version_id: string | null;
  exclusion_reason: ExclusionReason | null;
  exclusion_note: string | null;
}

export interface CurationPage {
  page_id: string | null;
  chunk_id: string | null;
  document_id: string;
  version_id: string;
  page_number: number;
  review_status: ReviewStatus | null;
  title: string | null;
  hidden: boolean;
  enabled: boolean;
  source_reference: SourceReference;
  enablement: EnablementState | null;
  review?: PageReview | null;
  rendering_warnings?: RenderingWarningSummary;
  version_rendering_warnings?: RenderingWarningSummary;
}

export interface JobData {
  job_id: string;
  kind: string;
  status: JobStatus;
  attempts: number;
  next_retry_at: string | null;
  progress: {
    phase: string;
    completed_pages: number;
    total_pages: number;
  } | null;
  error: JobError | null;
}

export interface PageEnablementAccepted {
  document_id: string;
  version_id: string;
  page_number: number;
  job_id: string | null;
  status: "accepted" | "coalesced" | "no_change";
  page_id: string | null;
}

export interface SourceImage {
  reference_index: number;
  alt_text: string;
  media_type: string;
  origin_part: string;
  data_base64?: string;
}

export type ImageDisposition = "included" | "ignored";

export type VisualType =
  | "chart"
  | "diagram"
  | "map"
  | "table"
  | "screenshot"
  | "photo"
  | "illustration"
  | "other";

export interface NormalizedBounds {
  left: number;
  top: number;
  width: number;
  height: number;
}

export interface CurationVisual {
  visual_ref: string;
  position: number;
  source_kind: "source_image" | "capture";
  disposition: "included" | "ignored";
  summary: string | null;
  visual_type: string | null;
  bounds: NormalizedBounds | null;
  source_visual_ref: string | null;
  confirmed: boolean;
  asset?: {
    sha256: string;
    media_type: string;
    size_bytes: number;
    width_px?: number;
    height_px?: number;
    byte_contract: "anydoc_original" | "standard_render_crop";
  };
}

export type ImageIgnoreReason =
  | "decorative"
  | "duplicate_source"
  | "expressed_elsewhere"
  | "not_relevant"
  | "corrupt_or_unverifiable"
  | "other";

export interface CurationImageSource extends SourceImage {
  source_ref: string;
  position: number;
  object_sha256: string | null;
  size_bytes: number | null;
  integrity: "verified" | "missing" | "hash_mismatch";
  duplicate_object: boolean;
  preview_url: string;
  disposition: ImageDisposition | null;
  summary: string | null;
  ignore_reason: ImageIgnoreReason | null;
  ignore_note: string | null;
  visual_ref: string | null;
  decided_by: string | null;
  decided_at: string | null;
}

export interface SourceContent {
  titles: string[];
  body: string[];
  tables: unknown[];
  images: SourceImage[];
  speaker_notes: string[];
}

export interface CurationSnapshot {
  snapshot_id: string;
  source_snapshot_id: string | null;
  source_content: SourceContent;
  created_by: string | null;
  created_at: string;
  source_confirmation: { actor_id: string; confirmed_at: string } | null;
  source_review: { actor_id: string; completed_at: string } | null;
  image_source_decisions?: CurationImageSource[];
}

export interface CurationTextReviewResult {
  curation: CurationState;
  transition: {
    snapshot: "created" | "reused";
    source_saved: boolean;
    source_confirmed: boolean;
    source_review_completed: boolean;
  };
  next_unresolved_image: {
    source_ref: string;
    position: number;
    blocker_code: CurationBlocker["code"];
  } | null;
}

export interface CurationBlocker {
  code:
    | "source_unsaved"
    | "source_unconfirmed"
    | "source_review_incomplete"
    | "image_disposition_required"
    | "image_summary_required"
    | "image_reason_required"
    | "image_other_note_required"
    | "image_changes_unsaved"
    | "image_bytes_unavailable"
    | "image_hash_mismatch"
    | "image_media_type_unsupported"
    | "visual_summary_required"
    | "capture_required"
    | "chunk_body_empty";
  message: string;
  source_ref?: string;
}

export interface RepeatedFooterNoiseSource {
  source_ref: string;
  source_kind: "body";
  source_index: number;
  text: string;
  active_confirmation_id: string | null;
}

export interface RepeatedFooterNoiseMetadata {
  confirmation_id: string;
  source_ref: string;
  source_text: string;
  rule_version: string;
  confirmed_by: string;
  confirmed_at: string;
}

export interface RepeatedFooterNoiseHistory {
  confirmation_id: string;
  source_ref: string;
  source_text: string;
  rule_version: string;
  confirmation_note: string | null;
  confirmed_by: string;
  confirmed_at: string;
  status: "active" | "revoked";
  revoked_by: string | null;
  revoked_at: string | null;
  revoke_note: string | null;
}

export interface RepeatedFooterNoiseAffectedPage {
  page_id: string;
  page_version_id: string;
  page_number: number;
  source_ref: string;
  source_kind: "body";
  source_index: number;
  source_text: string;
  standard_render: { url: string };
}

export interface RepeatedFooterNoiseCandidate {
  candidate_id: string;
  document_id: string;
  version_id: string;
  source_text: string;
  normalized_text: string;
  rule_version: string;
  affected_pages: RepeatedFooterNoiseAffectedPage[];
}

export interface CurationState {
  current_snapshot: CurationSnapshot | null;
  image_sources: {
    total: number;
    unresolved: number;
    items: CurationImageSource[];
  };
  repeated_footer_noise?: {
    sources: RepeatedFooterNoiseSource[];
    active_count: number;
    history?: RepeatedFooterNoiseHistory[];
  };
  chunk_body: { nonempty: boolean; preview?: string };
  chunk_metadata?: {
    excluded_repeated_footer_noise: RepeatedFooterNoiseMetadata[];
  };
  blockers: CurationBlocker[];
  can_confirm_source: boolean;
  can_complete_source_review: boolean;
  can_approve: boolean;
}

export type CurationTimingStage =
  | "source_review"
  | "capture_annotation"
  | "page_decision";

export interface PageDetail {
  page_id: string;
  page_number: number;
  review_status: ReviewStatus;
  review?: PageReview;
  source_content: SourceContent;
  curation?: CurationState;
  annotation?: {
    snapshot_id: string;
    visuals: CurationVisual[];
  } | null;
  standard_render?: {
    sha256: string;
    media_type: string;
    dpi: number;
    width_px: number;
    height_px: number;
    url: string;
  };
  rendering_warnings?: {
    summary: RenderingWarningSummary;
    warnings: RenderingWarning[];
  };
}

export interface VisualMutationResult {
  curation: CurationState;
  visuals: CurationVisual[] | null;
}

export interface PublicationPreflight {
  can_publish: boolean;
  summary: RenderingWarningSummary;
  stale_render_versions: number;
  href: string | null;
}

export type PublicationCandidateStatus =
  | "ready"
  | "stale"
  | "confirmed"
  | "no_change"
  | "succeeded"
  | "failed";

export interface PublicationPageScope {
  page_number: number;
  title: string;
  page_id: string;
  chunk_id: string;
  snapshot_id: string;
  reviewed_by: string;
  reviewed_at: string;
  change: "added" | "updated" | "unchanged";
}

export interface PublicationDocumentScope {
  document_id: string;
  version_id: string;
  title: string;
  pages: PublicationPageScope[];
}

export interface PublicationCandidate {
  candidate_id: string;
  status: PublicationCandidateStatus;
  business_state_token: string;
  content_set_hash: string;
  created_by: string;
  created_at: string;
  confirmed_by: string | null;
  confirmed_at: string | null;
  publication_seq: number | null;
  frozen_input_hash: string | null;
  diff: { added: number; updated: number; removed: number; unchanged: number };
  excluded: {
    pending_pages: number;
    excluded_pages: number;
    disabled_hidden_pages: number;
    soft_deleted_documents: number;
  };
  documents: PublicationDocumentScope[];
  chunk_count: number;
  asset_count: number;
}

export interface PublicationArtifact {
  publication_seq: number;
  candidate_id: string;
  snapshot_id: string;
  published_at: string;
  chunk_count: number;
  asset_count: number;
  size_bytes: number;
  sha256: string;
  media_type: "application/zip";
  download_url: string;
}

export interface PublicationTask {
  job_id: string;
  candidate_id: string;
  publication_seq: number;
  status: "queued" | "running" | "succeeded" | "failed";
  phase: "frozen_input" | "build" | "validate" | "store" | "switch_pointer" | "succeeded";
  progress: {
    phase: "frozen_input" | "build" | "validate" | "store" | "switch_pointer" | "succeeded";
    completed_pages: number;
    total_pages: number;
  } | null;
  error: JobError | null;
  attempts: number;
  updated_at: string;
}

export interface PublicationWorkspace {
  preflight: PublicationPreflight;
  current: PublicationArtifact | null;
  candidate: PublicationCandidate | null;
  task: PublicationTask | null;
}

export interface PublicationConfirmation {
  candidate_id: string;
  status: "queued" | "no_change";
  publication_seq: number | null;
  job_id: string | null;
  frozen_input_hash?: string;
}

export interface RenderingWarningsPayload {
  document_id: string;
  version_id: string;
  render_config_version: string;
  summary: RenderingWarningSummary;
  warnings: RenderingWarning[];
}

export interface MappingAdjacentPage {
  source_page_number: number;
  page_id: string;
}

export interface PageMappingCandidate {
  page_id: string;
  chunk_id: string;
  version_id: string;
  page_number: number;
  slide_id: number;
  review_status: "pending" | "approved" | "excluded";
  fingerprint_relation: "same" | "changed";
  adjacent_confirmed: {
    before: MappingAdjacentPage | null;
    after: MappingAdjacentPage | null;
  };
  relative_order: {
    source_page_number: number;
    candidate_page_number: number;
    delta: number;
  };
  occupied_by_case_id: string | null;
  standard_render: { url: string };
}

export interface PageMappingCase {
  case_id: string;
  kind: "duplicate_fingerprint" | "slide_id_conflict" | "multiple_candidates";
  status: "unresolved" | "saved";
  source_page: {
    page_number: number;
    slide_id: number;
    fingerprint: { version: number; sha256: string };
    standard_render: { url: string };
  };
  candidates: PageMappingCandidate[];
  decision: { kind: "reuse" | "new"; page_id: string | null } | null;
  decided_by: string | null;
  decided_at: string | null;
}

export interface PageMappingWorkspace {
  document_id: string;
  version_id: string;
  source_filename: string;
  status: "awaiting_mapping" | "ready" | "voided";
  revision: number;
  remaining_cases: number;
  current_version: { version_id: string | null; still_serving: boolean };
  cases: PageMappingCase[];
  can_confirm: boolean;
  confirmed_at: string | null;
  confirmed_by: string | null;
  impact_summary: {
    reused_unchanged: number;
    reused_changed: number;
    created_new: number;
    soft_deleted: number;
    unresolved: number;
    save_conflicts: number;
    evidence_errors: number;
  };
}

export type PageMappingDraft =
  | { kind: "reuse"; page_id: string }
  | { kind: "new"; page_id?: null };

interface ErrorEnvelope {
  error?: {
    code?: string;
    message?: string;
    details?: unknown;
  };
}

export class OperatorError extends Error {
  constructor(
    message: string,
    readonly code: string | null = null,
    readonly details: unknown = null,
  ) {
    super(message);
    this.name = "OperatorError";
  }
}

export class PageMappingConflictError extends OperatorError {
  constructor() {
    super("页对应决定已被其他会话更新，请比较后重新确认。");
    this.name = "PageMappingConflictError";
  }
}

export async function loadBootstrap(signal?: AbortSignal): Promise<BootstrapData> {
  let response: Response;
  try {
    response = await fetch("/api/v1/app/bootstrap", {
      headers: { Accept: "application/json" },
      signal,
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") {
      throw error;
    }
    throw new OperatorError("无法连接 PPTExtract。请确认服务已启动后重试。");
  }

  const payload = (await response.json().catch(() => ({}))) as BootstrapData & ErrorEnvelope;
  if (!response.ok) {
    throw new OperatorError(payload.error?.message ?? "文档入口暂时不可用，请稍后重试。");
  }
  return payload;
}

export async function uploadDocument(
  file: File,
  idempotencyKey: string,
  signal?: AbortSignal,
): Promise<UploadAccepted> {
  const form = new FormData();
  form.append("file", file);

  let response: Response;
  try {
    response = await fetch("/api/v1/documents", {
      method: "POST",
      headers: {
        Accept: "application/json",
        "Idempotency-Key": idempotencyKey,
      },
      body: form,
      signal,
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") throw error;
    throw new OperatorError("无法连接上传服务，文件尚未提交。请检查网络后重试。");
  }

  return readJson<UploadAccepted>(response, "上传未能完成，请稍后重试。");
}

async function readJson<T>(response: Response, fallback: string): Promise<T> {
  const payload = (await response.json().catch(() => ({}))) as T & ErrorEnvelope;
  if (!response.ok) {
    throw new OperatorError(
      payload.error?.message ?? fallback,
      payload.error?.code ?? null,
      payload.error?.details ?? null,
    );
  }
  return payload;
}

async function readMappingResponse(
  response: Response,
): Promise<{ data: PageMappingWorkspace; etag: string }> {
  const payload = (await response.json().catch(() => ({}))) as
    | PageMappingWorkspace
    | ErrorEnvelope;
  if (response.status === 412) throw new PageMappingConflictError();
  if (!response.ok) {
    const error = payload as ErrorEnvelope;
    throw new OperatorError(error.error?.message ?? "页对应工作面暂时不可用。");
  }
  const etag = response.headers.get("ETag");
  if (!etag) throw new OperatorError("页对应工作面缺少并发版本信息，请重新加载。");
  return { data: payload as PageMappingWorkspace, etag };
}

function mappingRoute(documentId: string, versionId: string): string {
  return `/api/v1/documents/${documentId}/versions/${versionId}/page-mapping`;
}

export async function loadPageMapping(
  documentId: string,
  versionId: string,
  signal?: AbortSignal,
): Promise<{ data: PageMappingWorkspace; etag: string }> {
  const response = await fetch(mappingRoute(documentId, versionId), {
    headers: { Accept: "application/json" },
    signal,
  });
  return readMappingResponse(response);
}

export async function savePageMappingDecision(
  workspace: PageMappingWorkspace,
  caseId: string,
  draft: PageMappingDraft,
  etag: string,
): Promise<{ data: PageMappingWorkspace; etag: string }> {
  const response = await fetch(
    `${mappingRoute(workspace.document_id, workspace.version_id)}/cases/${caseId}`,
    {
      method: "PUT",
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json",
        "If-Match": etag,
      },
      body: JSON.stringify({
        decision: draft.kind,
        page_id: draft.kind === "reuse" ? draft.page_id : undefined,
      }),
    },
  );
  return readMappingResponse(response);
}

export async function confirmPageMapping(
  workspace: PageMappingWorkspace,
  etag: string,
): Promise<void> {
  const response = await fetch(
    `${mappingRoute(workspace.document_id, workspace.version_id)}/confirm`,
    { method: "POST", headers: { Accept: "application/json", "If-Match": etag } },
  );
  if (response.status === 412) throw new PageMappingConflictError();
  if (!response.ok) {
    const payload = (await response.json().catch(() => ({}))) as ErrorEnvelope;
    throw new OperatorError(payload.error?.message ?? "最终确认未完成，请重新检查。");
  }
}

export async function loadCurationPages(
  reviewStatus: "pending" | "inherited" | "all" | "rendering-warnings",
  signal?: AbortSignal,
): Promise<CurationPage[]> {
  let response: Response;
  try {
    const apiReviewStatus = reviewStatus === "rendering-warnings" ? "all" : reviewStatus;
    response = await fetch(`/api/v1/curation/pages?review_status=${apiReviewStatus}`, {
      headers: { Accept: "application/json" },
      signal,
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") throw error;
    throw new OperatorError("无法加载策展页清单。请检查连接后重试。");
  }
  const pages = (
    await readJson<{ pages: CurationPage[] }>(response, "策展页清单暂时不可用。")
  ).pages;
  return reviewStatus === "rendering-warnings"
    ? pages.filter((page) => (page.rendering_warnings?.total ?? 0) > 0)
    : pages;
}

export async function recordCurationTimingSample(
  sampleId: string,
  pageId: string,
  versionId: string,
  stage: CurationTimingStage,
  durationMs: number,
): Promise<void> {
  enqueueCurationTimingSample({
    sample_id: sampleId,
    page_id: pageId,
    version_id: versionId,
    stage,
    duration_ms: Math.max(0, Math.round(durationMs)),
  });
  await retryPendingCurationTimingSamples();
}

interface PendingCurationTimingSample {
  sample_id: string;
  page_id: string;
  version_id: string;
  stage: CurationTimingStage;
  duration_ms: number;
}

const CURATION_TIMING_QUEUE_KEY = "pptextract:curation-timing-samples";
let volatileTimingQueue: PendingCurationTimingSample[] = [];
let timingFlush: Promise<void> | null = null;

function readCurationTimingQueue(): PendingCurationTimingSample[] {
  try {
    const serialized = globalThis.localStorage.getItem(CURATION_TIMING_QUEUE_KEY);
    return serialized ? JSON.parse(serialized) as PendingCurationTimingSample[] : [];
  } catch {
    return volatileTimingQueue;
  }
}

function writeCurationTimingQueue(queue: PendingCurationTimingSample[]): void {
  volatileTimingQueue = queue;
  try {
    if (queue.length === 0) {
      globalThis.localStorage.removeItem(CURATION_TIMING_QUEUE_KEY);
    } else {
      globalThis.localStorage.setItem(CURATION_TIMING_QUEUE_KEY, JSON.stringify(queue));
    }
  } catch {
    // 浏览器禁用持久存储时仍在当前页面内保留恢复队列。
  }
}

function enqueueCurationTimingSample(sample: PendingCurationTimingSample): void {
  const queue = readCurationTimingQueue();
  if (queue.some((candidate) => candidate.sample_id === sample.sample_id)) return;
  writeCurationTimingQueue([...queue, sample]);
}

export function retryPendingCurationTimingSamples(): Promise<void> {
  if (timingFlush) return timingFlush;
  timingFlush = (async () => {
    const attempted = new Set<string>();
    while (true) {
      const sample = readCurationTimingQueue().find(
        (candidate) => !attempted.has(candidate.sample_id),
      );
      if (!sample) return;
      attempted.add(sample.sample_id);
      try {
        const response = await fetch("/api/v1/curation/runtime-facts/samples", {
          method: "POST",
          headers: { Accept: "application/json", "Content-Type": "application/json" },
          body: JSON.stringify(sample),
          keepalive: true,
        });
        if (!response.ok) continue;
        writeCurationTimingQueue(
          readCurationTimingQueue().filter(
            (candidate) => candidate.sample_id !== sample.sample_id,
          ),
        );
      } catch {
        // 运行事实不得中断策展主流程；下次进入工作台时会重试。
      }
    }
  })().finally(() => {
    timingFlush = null;
  });
  return timingFlush;
}

export async function enableHiddenPage(page: CurationPage): Promise<PageEnablementAccepted> {
  const response = await fetch(
    `/api/v1/documents/${page.document_id}/versions/${page.version_id}` +
      `/source-pages/${page.page_number}/enable`,
    {
      method: "POST",
      headers: {
        Accept: "application/json",
        "Idempotency-Key": globalThis.crypto.randomUUID(),
      },
    },
  );
  return readJson<PageEnablementAccepted>(response, "隐藏页启用请求未被接受，请重试。");
}

export async function loadJob(jobId: string, signal?: AbortSignal): Promise<JobData> {
  const response = await fetch(`/api/v1/jobs/${jobId}`, {
    headers: { Accept: "application/json" },
    signal,
  });
  return readJson<JobData>(response, "处理任务状态暂时不可用，请刷新恢复。");
}

export async function loadPageDetail(pageId: string, signal?: AbortSignal): Promise<PageDetail> {
  const response = await fetch(`/api/v1/pages/${pageId}`, {
    headers: { Accept: "application/json" },
    signal,
  });
  return readJson<PageDetail>(response, "AnyDoc 来源暂时不可用，请重试。");
}

export async function loadRepeatedFooterNoiseCandidate(
  pageId: string,
  sourceRef: string,
): Promise<RepeatedFooterNoiseCandidate> {
  const response = await fetch(
    `/api/v1/pages/${pageId}/repeated-footer-noise/candidates/${sourceRef}`,
    { headers: { Accept: "application/json" } },
  );
  return (
    await readJson<{ candidate: RepeatedFooterNoiseCandidate }>(
      response,
      "无法检查此正文来源的跨页重复情况。",
    )
  ).candidate;
}

export async function confirmRepeatedFooterNoise(
  pageId: string,
  candidateId: string,
  sourceRef: string,
  note: string | null,
): Promise<void> {
  const response = await fetch(
    `/api/v1/pages/${pageId}/repeated-footer-noise/confirmations`,
    {
      method: "POST",
      headers: { Accept: "application/json", "Content-Type": "application/json" },
      body: JSON.stringify({ candidate_id: candidateId, source_ref: sourceRef, note }),
    },
  );
  await readJson(response, "重复页脚噪声确认未能保存；正文保持不变。");
}

export async function revokeRepeatedFooterNoise(
  confirmationId: string,
  note: string | null,
): Promise<void> {
  const response = await fetch(
    `/api/v1/repeated-footer-noise/confirmations/${confirmationId}/revoke`,
    {
      method: "POST",
      headers: { Accept: "application/json", "Content-Type": "application/json" },
      body: JSON.stringify({ note }),
    },
  );
  await readJson(response, "重复页脚噪声排除未能撤销；正文状态未改变。");
}

export async function saveCurationSnapshot(
  pageId: string,
  baseSnapshotId: string | null,
  titles: string[],
  body: string[],
): Promise<CurationState> {
  const response = await fetch(`/api/v1/pages/${pageId}/curation/snapshots`, {
    method: "POST",
    headers: { Accept: "application/json", "Content-Type": "application/json" },
    body: JSON.stringify({ base_snapshot_id: baseSnapshotId, titles, body }),
  });
  return (
    await readJson<{ curation: CurationState }>(
      response,
      "来源修改未能保存；本地修改仍保留。",
    )
  ).curation;
}

export async function reviewCurationText(
  pageId: string,
  baseSnapshotId: string | null,
  titles: string[],
  body: string[],
): Promise<CurationTextReviewResult> {
  const response = await fetch(`/api/v1/pages/${pageId}/curation/text-review`, {
    method: "POST",
    headers: { Accept: "application/json", "Content-Type": "application/json" },
    body: JSON.stringify({ base_snapshot_id: baseSnapshotId, titles, body }),
  });
  return readJson<CurationTextReviewResult>(
    response,
    "文字核对未能提交；持久状态未改变，本地文字修改仍保留。",
  );
}

export async function saveCurationImageSource(
  pageId: string,
  sourceRef: string,
  baseSnapshotId: string,
  disposition: ImageDisposition,
  summary: string | null,
  ignoreReason: ImageIgnoreReason | null,
  ignoreNote: string | null,
): Promise<CurationState> {
  const response = await fetch(
    `/api/v1/pages/${pageId}/curation/image-sources/${sourceRef}`,
    {
      method: "POST",
      headers: { Accept: "application/json", "Content-Type": "application/json" },
      body: JSON.stringify({
        base_snapshot_id: baseSnapshotId,
        disposition,
        summary,
        ignore_reason: ignoreReason,
        ignore_note: ignoreNote,
      }),
    },
  );
  return (
    await readJson<{ curation: CurationState }>(
      response,
      "图片来源处置未能保存；本地修改仍保留。",
    )
  ).curation;
}

export async function saveCaptureVisual(
  pageId: string,
  baseSnapshotId: string,
  summary: string,
  visualType: VisualType | null,
  bounds: NormalizedBounds,
): Promise<VisualMutationResult> {
  const response = await fetch(`/api/v1/pages/${pageId}/curation/visuals`, {
    method: "POST",
    headers: { Accept: "application/json", "Content-Type": "application/json" },
    body: JSON.stringify({
      base_snapshot_id: baseSnapshotId,
      summary,
      visual_type: visualType,
      bounds,
    }),
  });
  const payload = await readJson<{
    curation: CurationState;
    annotation?: { visuals: CurationVisual[] } | null;
  }>(response, "视觉对象未能保存；当前范围和表单内容仍保留。");
  return { curation: payload.curation, visuals: payload.annotation?.visuals ?? null };
}

export async function updateCaptureVisual(
  pageId: string,
  visualRef: string,
  baseSnapshotId: string,
  summary: string,
  visualType: VisualType | null,
  bounds: NormalizedBounds,
): Promise<VisualMutationResult> {
  const response = await fetch(`/api/v1/pages/${pageId}/curation/visuals/${visualRef}`, {
    method: "PATCH",
    headers: { Accept: "application/json", "Content-Type": "application/json" },
    body: JSON.stringify({
      base_snapshot_id: baseSnapshotId,
      summary,
      visual_type: visualType,
      bounds,
    }),
  });
  const payload = await readJson<{
    curation: CurationState;
    annotation?: { visuals: CurationVisual[] } | null;
  }>(response, "视觉对象修改未能保存；当前范围和表单内容仍保留。");
  return { curation: payload.curation, visuals: payload.annotation?.visuals ?? null };
}

export async function deleteCaptureVisual(
  pageId: string,
  visualRef: string,
  baseSnapshotId: string,
): Promise<VisualMutationResult> {
  const response = await fetch(`/api/v1/pages/${pageId}/curation/visuals/${visualRef}`, {
    method: "DELETE",
    headers: { Accept: "application/json", "Content-Type": "application/json" },
    body: JSON.stringify({ base_snapshot_id: baseSnapshotId }),
  });
  const payload = await readJson<{
    curation: CurationState;
    annotation?: { visuals: CurationVisual[] } | null;
  }>(response, "视觉对象删除失败；原对象与原编号仍保留。");
  return { curation: payload.curation, visuals: payload.annotation?.visuals ?? null };
}

export async function moveCaptureVisual(
  pageId: string,
  visualRef: string,
  baseSnapshotId: string,
  direction: "up" | "down",
): Promise<VisualMutationResult> {
  const response = await fetch(
    `/api/v1/pages/${pageId}/curation/visuals/${visualRef}/move`,
    {
      method: "POST",
      headers: { Accept: "application/json", "Content-Type": "application/json" },
      body: JSON.stringify({ base_snapshot_id: baseSnapshotId, direction }),
    },
  );
  const payload = await readJson<{
    curation: CurationState;
    annotation?: { visuals: CurationVisual[] } | null;
  }>(response, "视觉对象排序失败；原顺序与原编号仍保留。");
  return { curation: payload.curation, visuals: payload.annotation?.visuals ?? null };
}

export async function markCaptureSourceComplete(
  pageId: string,
  snapshotId: string,
): Promise<VisualMutationResult> {
  const response = await fetch(`/api/v1/pages/${pageId}/curation/source-completeness`, {
    method: "POST",
    headers: { Accept: "application/json", "Content-Type": "application/json" },
    body: JSON.stringify({ snapshot_id: snapshotId }),
  });
  const payload = await readJson<{
    curation: CurationState;
    annotation?: { visuals: CurationVisual[] } | null;
  }>(response, "来源完整性未能更新；来源缺口仍保持阻塞。");
  return { curation: payload.curation, visuals: payload.annotation?.visuals ?? null };
}

async function submitSnapshotCommand(
  pageId: string,
  action: "source-confirmation" | "source-review",
  snapshotId: string,
  fallback: string,
): Promise<CurationState> {
  const response = await fetch(`/api/v1/pages/${pageId}/curation/${action}`, {
    method: "POST",
    headers: { Accept: "application/json", "Content-Type": "application/json" },
    body: JSON.stringify({ snapshot_id: snapshotId }),
  });
  return (await readJson<{ curation: CurationState }>(response, fallback)).curation;
}

export function confirmCurationSource(
  pageId: string,
  snapshotId: string,
): Promise<CurationState> {
  return submitSnapshotCommand(
    pageId,
    "source-confirmation",
    snapshotId,
    "文字来源确认失败；当前状态未改变。",
  );
}

export function completeCurationSourceReview(
  pageId: string,
  snapshotId: string,
): Promise<CurationState> {
  return submitSnapshotCommand(
    pageId,
    "source-review",
    snapshotId,
    "来源审核未能完成；当前状态未改变。",
  );
}

export async function approveCurationPage(
  pageId: string,
  snapshotId: string,
): Promise<{ review: { status: "approved" }; chunk_body: string }> {
  const response = await fetch(`/api/v1/pages/${pageId}/approve`, {
    method: "POST",
    headers: { Accept: "application/json", "Content-Type": "application/json" },
    body: JSON.stringify({ snapshot_id: snapshotId }),
  });
  return readJson<{ review: { status: "approved" }; chunk_body: string }>(
    response,
    "批准未完成；页面仍保留在待处理队列。",
  );
}

export async function excludeCurationPage(
  pageId: string,
  reason: ExclusionReason,
  note: string | null,
): Promise<{ review: PageReview & { status: "excluded" } }> {
  const response = await fetch(`/api/v1/pages/${pageId}/exclude`, {
    method: "POST",
    headers: { Accept: "application/json", "Content-Type": "application/json" },
    body: JSON.stringify({ reason, note }),
  });
  return readJson<{ review: PageReview & { status: "excluded" } }>(
    response,
    "排除未完成；页面仍保留在待处理队列。",
  );
}

export async function reopenCurationPage(
  pageId: string,
): Promise<{ review: PageReview & { status: "pending" } }> {
  const response = await fetch(`/api/v1/pages/${pageId}/reopen`, {
    method: "POST",
    headers: { Accept: "application/json" },
  });
  return readJson<{ review: PageReview & { status: "pending" } }>(
    response,
    "重新打开未完成；页面仍保持冻结。",
  );
}

export interface BatchExclusionResult {
  requested: number;
  excluded: string[];
  failed: Array<{ page_id: string; code: string; message: string }>;
  complete: boolean;
}

export async function batchExcludeCurationPages(
  pageIds: string[],
  reason: ExclusionReason,
  note: string | null,
): Promise<BatchExclusionResult> {
  const response = await fetch("/api/v1/pages/batch-exclude", {
    method: "POST",
    headers: { Accept: "application/json", "Content-Type": "application/json" },
    body: JSON.stringify({ page_ids: pageIds, reason, note }),
  });
  return readJson<BatchExclusionResult>(
    response,
    "批量排除未完成；已选页面保持不变，可重试。",
  );
}

function renderingWarningRoute(page: CurationPage): string {
  return `/api/v1/documents/${page.document_id}/versions/${page.version_id}/rendering-warnings`;
}

export async function loadRenderingWarnings(
  page: CurationPage,
  signal?: AbortSignal,
): Promise<RenderingWarningsPayload> {
  const response = await fetch(renderingWarningRoute(page), {
    headers: { Accept: "application/json" },
    signal,
  });
  return readJson<RenderingWarningsPayload>(response, "渲染警告暂时不可用，请重试。");
}

export async function confirmRenderingWarning(
  page: CurationPage,
  warningId: string,
): Promise<RenderingWarning> {
  const response = await fetch(`${renderingWarningRoute(page)}/${warningId}/confirm`, {
    method: "POST",
    headers: { Accept: "application/json" },
  });
  return readJson<RenderingWarning>(response, "渲染警告确认失败，请重试。");
}

export async function confirmAllRenderingWarnings(
  page: CurationPage,
  renderConfigVersion: string,
  warningIds: string[],
): Promise<{
  confirmed_count: number;
  summary: RenderingWarningSummary;
  render_config_version: string;
  warnings: RenderingWarning[];
}> {
  const response = await fetch(`${renderingWarningRoute(page)}/confirm-all`, {
    method: "POST",
    headers: { Accept: "application/json", "Content-Type": "application/json" },
      body: JSON.stringify({
        render_config_version: renderConfigVersion,
        warning_ids: warningIds,
      }),
  });
  return readJson<{
    confirmed_count: number;
    summary: RenderingWarningSummary;
    render_config_version: string;
    warnings: RenderingWarning[];
  }>(
    response,
    "整版渲染警告确认失败，请重新检查。",
  );
}

export async function loadPublicationPreflight(
  signal?: AbortSignal,
): Promise<PublicationPreflight> {
  const response = await fetch("/api/v1/publications/preflight", {
    headers: { Accept: "application/json" },
    signal,
  });
  return readJson(response, "发布前置校验暂时不可用，请重试。");
}

export async function validatePublicationPreflight(): Promise<PublicationPreflight> {
  const response = await fetch("/api/v1/publications/preflight", {
    method: "POST",
    headers: { Accept: "application/json" },
  });
  return readJson(response, "发布前置校验未通过，请重新检查。");
}

export async function loadPublicationWorkspace(
  signal?: AbortSignal,
): Promise<PublicationWorkspace> {
  const response = await fetch("/api/v1/publications", {
    headers: { Accept: "application/json" },
    signal,
  });
  return readJson(response, "发布台账暂时不可用，请重试。");
}

export async function createPublicationCandidate(): Promise<PublicationCandidate> {
  const response = await fetch("/api/v1/publications/candidates", {
    method: "POST",
    headers: { Accept: "application/json" },
  });
  return readJson(response, "发布候选未创建，请重新检查当前业务状态。");
}

export async function confirmPublicationCandidate(
  candidateId: string,
): Promise<PublicationConfirmation> {
  const response = await fetch(`/api/v1/publications/candidates/${candidateId}/confirm`, {
    method: "POST",
    headers: { Accept: "application/json" },
  });
  return readJson(response, "发布候选未确认，请重新核验候选范围。");
}

export async function retryPublicationTask(jobId: string): Promise<PublicationConfirmation> {
  const response = await fetch(`/api/v1/publications/tasks/${jobId}/retry`, {
    method: "POST",
    headers: { Accept: "application/json" },
  });
  return readJson(response, "发布重试未开始；当前产物保持不变。");
}
