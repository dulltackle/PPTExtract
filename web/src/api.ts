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

export interface CurationPage {
  page_id: string | null;
  chunk_id: string | null;
  document_id: string;
  version_id: string;
  page_number: number;
  review_status: "pending" | "approved" | "excluded" | null;
  title: string | null;
  hidden: boolean;
  enabled: boolean;
  source_reference: SourceReference;
  enablement: EnablementState | null;
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

export interface PageDetail {
  page_id: string;
  page_number: number;
  review_status: "pending" | "approved" | "excluded";
  source_content: {
    titles: string[];
    body: string[];
    speaker_notes: string[];
  };
  rendering_warnings?: {
    summary: RenderingWarningSummary;
    warnings: RenderingWarning[];
  };
}

export interface PublicationPreflight {
  can_publish: boolean;
  summary: RenderingWarningSummary;
  stale_render_versions: number;
  href: string | null;
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
  };
}

export class OperatorError extends Error {
  constructor(message: string) {
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

async function readJson<T>(response: Response, fallback: string): Promise<T> {
  const payload = (await response.json().catch(() => ({}))) as T & ErrorEnvelope;
  if (!response.ok) {
    throw new OperatorError(payload.error?.message ?? fallback);
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
  reviewStatus: "pending" | "all" | "rendering-warnings",
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
