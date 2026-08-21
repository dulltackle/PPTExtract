export interface Actor {
  actor_id: string;
  display_name: string;
}

export interface DocumentSummary {
  document_id: string;
  title: string;
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

export type JobStatus = "queued" | "running" | "requires_action" | "succeeded" | "failed" | "cancelled";

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
}

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

export async function loadCurationPages(
  reviewStatus: "pending" | "all",
  signal?: AbortSignal,
): Promise<CurationPage[]> {
  let response: Response;
  try {
    response = await fetch(`/api/v1/curation/pages?review_status=${reviewStatus}`, {
      headers: { Accept: "application/json" },
      signal,
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") throw error;
    throw new OperatorError("无法加载策展页清单。请检查连接后重试。");
  }
  return (await readJson<{ pages: CurationPage[] }>(response, "策展页清单暂时不可用。"))
    .pages;
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
