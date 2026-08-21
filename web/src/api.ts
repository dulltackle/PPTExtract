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
