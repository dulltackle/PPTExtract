import {
  Component,
  type ChangeEvent,
  type ErrorInfo,
  type ReactNode,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";

import {
  type BootstrapData,
  loadBootstrap,
  OperatorError,
  type Runway,
  uploadDocument,
} from "./api";
import { CurationWorkbench, type CurationCommandState } from "./CurationWorkbench";
import { PageMappingWorkbench } from "./PageMappingWorkbench";
import { PublicationPreflight } from "./PublicationPreflight";
import "./styles.css";

type LoadState =
  | { kind: "loading" }
  | { kind: "ready"; data: BootstrapData }
  | { kind: "error"; message: string };

type UploadState =
  | { kind: "idle" }
  | { kind: "uploading"; fileName: string }
  | { kind: "success"; fileName: string; jobId: string }
  | { kind: "error"; fileName: string; message: string };

interface UploadAttempt {
  file: File;
  idempotencyKey: string;
}

function createUploadIdempotencyKey(): string {
  const suffix =
    typeof crypto !== "undefined" && typeof crypto.randomUUID === "function"
      ? crypto.randomUUID()
      : `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
  return `browser-upload-${suffix}`;
}

const runwayCopy: Record<Runway["id"], { description: string; empty: string; next: string }> = {
  pending: {
    description: "等待开始处理的文档",
    empty: "还没有待处理文档",
    next: "上传新文档后，它会先出现在这里。",
  },
  processing: {
    description: "正在建立可策展版本",
    empty: "当前没有处理中的文档",
    next: "处理任务开始后，这里会显示真实阶段与进度。",
  },
  curatable: {
    description: "处理完成，等待策展",
    empty: "还没有可策展文档",
    next: "完成处理的文档会在这里等待策展人员续接。",
  },
};

function ProductMark() {
  return (
    <span className="product-lockup" aria-label="PPTExtract">
      <span className="product-mark" aria-hidden="true">
        PX
      </span>
      <span className="product-name">PPTExtract</span>
    </span>
  );
}

function UserIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true" className="icon">
      <circle cx="12" cy="8" r="3.25" />
      <path d="M5.75 19c.55-3.35 2.63-5 6.25-5s5.7 1.65 6.25 5" />
    </svg>
  );
}

function UploadIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true" className="icon">
      <path d="M12 16V4m0 0L7.75 8.25M12 4l4.25 4.25" />
      <path d="M5 14.5V20h14v-5.5" />
    </svg>
  );
}

function KeyboardIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true" className="icon">
      <rect x="3.25" y="6.25" width="17.5" height="11.5" rx="2" />
      <path d="M6.5 9.5h1m2.5 0h1m2.5 0h1m2.5 0h1M6.5 12.5h1m2.5 0h1m2.5 0h1m2.5 0h1M8 15.25h8" />
    </svg>
  );
}

function EmptyDocumentIcon() {
  return (
    <svg viewBox="0 0 32 38" aria-hidden="true" className="empty-document-icon">
      <path d="M5.5 1.5h13l8 8v27h-21z" />
      <path d="M18.5 1.5v8h8" />
      <path d="M10 18h12M10 24h12M10 30h7" />
    </svg>
  );
}

function StageRunway({ runway }: { runway: Runway }) {
  const copy = runwayCopy[runway.id];
  return (
    <section className={`runway runway--${runway.id}`} aria-labelledby={`runway-${runway.id}`}>
      <header className="runway-label">
        <div className="runway-heading-row">
          <span className="stage-signal" aria-hidden="true" />
          <h2 id={`runway-${runway.id}`}>{runway.label}</h2>
          <span className="runway-count" aria-label={`${runway.documents.length} 个文档`}>
            {runway.documents.length}
          </span>
        </div>
        <p>{copy.description}</p>
        <span className="runway-state">
          当前 · {runway.documents.length === 0 ? "空" : `${runway.documents.length} 项`}
        </span>
      </header>
      <div className="runway-content">
        <div className="column-guide" aria-hidden="true">
          <span>文档标题</span>
          <span>版本</span>
          <span>最近活动</span>
          <span>状态</span>
        </div>
        {runway.documents.length === 0 ? (
          <div className="empty-slot">
            <EmptyDocumentIcon />
            <div>
              <strong>{copy.empty}</strong>
              <p>{copy.next}</p>
            </div>
          </div>
        ) : (
          <div className="runway-documents">
            {runway.documents.map((document) => (
              <article className="runway-document-row" key={`${document.document_id}-${document.version_id ?? "current"}`}>
                <div>
                  <strong>{document.title}</strong>
                  <code>{document.document_id.slice(0, 10)}</code>
                </div>
                <code>{document.version_id?.slice(0, 10) ?? "当前"}</code>
                <span>
                  {document.rendering_warnings
                    ? document.rendering_warnings.unconfirmed > 0
                      ? `渲染风险 · ${document.rendering_warnings.unconfirmed_pages} 页 / ${document.rendering_warnings.unconfirmed} 条未确认`
                      : document.rendering_warnings.total > 0
                        ? `渲染风险 · ${document.rendering_warnings.total} 条已确认`
                        : "未发现渲染风险"
                    : document.status === "requires_action"
                      ? "等待人工决定"
                      : "摄取任务进行中"}
                </span>
                <div>
                  <span className={document.status === "requires_action" ? "runway-status-chip is-action" : "runway-status-chip"}>
                    {document.status_label ?? "处理中"}
                  </span>
                  {document.action ? <a href={document.action.href}>{document.action.label}</a> : null}
                </div>
              </article>
            ))}
          </div>
        )}
      </div>
    </section>
  );
}

function LoadingRunways() {
  return (
    <main className="workspace workspace--loading" aria-live="polite" aria-busy="true">
      <p className="loading-message">正在连接文档入口…</p>
      {["pending", "processing", "curatable"].map((id) => (
        <div className="runway loading-runway" key={id} aria-hidden="true">
          <div className="loading-label shimmer" />
          <div className="loading-lines">
            <span className="shimmer" />
            <span className="shimmer" />
          </div>
        </div>
      ))}
    </main>
  );
}

function ErrorWorkspace({ message, retry }: { message: string; retry: () => void }) {
  return (
    <main className="workspace workspace--error">
      <section className="error-panel" role="alert">
        <h1>文档入口连接中断</h1>
        <p>{message}</p>
        <button type="button" className="secondary-button" onClick={retry}>
          重新连接
        </button>
      </section>
    </main>
  );
}

export function App() {
  const mappingMatch = window.location.pathname.match(
    /^\/documents\/([^/]+)\/versions\/([^/]+)\/page-mapping\/?$/,
  );
  const isMapping = mappingMatch !== null;
  const isCuration = window.location.pathname.startsWith("/curation");
  const isPublication = window.location.pathname.startsWith("/publication");
  const [state, setState] = useState<LoadState>({ kind: "loading" });
  const [uploadState, setUploadState] = useState<UploadState>({ kind: "idle" });
  const [curationCommands, setCurationCommands] = useState<CurationCommandState | null>(null);
  const [isKeyboardHelpOpen, setIsKeyboardHelpOpen] = useState(false);
  const activeRequest = useRef<{ controller: AbortController; requestId: number } | null>(null);
  const uploadInput = useRef<HTMLInputElement | null>(null);
  const uploadRequest = useRef<AbortController | null>(null);
  const uploadAttempt = useRef<UploadAttempt | null>(null);
  const keyboardHelp = useRef<HTMLDivElement | null>(null);
  const nextRequestId = useRef(0);

  const refresh = useCallback(() => {
    activeRequest.current?.controller.abort();
    const controller = new AbortController();
    const requestId = ++nextRequestId.current;
    activeRequest.current = { controller, requestId };
    setState({ kind: "loading" });
    loadBootstrap(controller.signal)
      .then((data) => {
        if (activeRequest.current?.requestId === requestId) {
          setState({ kind: "ready", data });
        }
      })
      .catch((error: unknown) => {
        if (activeRequest.current?.requestId !== requestId) return;
        if (error instanceof DOMException && error.name === "AbortError") return;
        const message =
          error instanceof OperatorError
            ? error.message
            : "文档入口发生未知错误，请重新连接。";
        setState({ kind: "error", message });
      });
  }, []);

  const submitUpload = useCallback(
    (attempt: UploadAttempt) => {
      uploadRequest.current?.abort();
      const controller = new AbortController();
      uploadRequest.current = controller;
      setUploadState({ kind: "uploading", fileName: attempt.file.name });

      uploadDocument(attempt.file, attempt.idempotencyKey, controller.signal)
        .then((accepted) => {
          if (uploadRequest.current !== controller) return;
          setUploadState({
            kind: "success",
            fileName: attempt.file.name,
            jobId: accepted.job_id,
          });
          refresh();
        })
        .catch((error: unknown) => {
          if (uploadRequest.current !== controller) return;
          if (error instanceof DOMException && error.name === "AbortError") return;
          setUploadState({
            kind: "error",
            fileName: attempt.file.name,
            message:
              error instanceof OperatorError
                ? error.message
                : "上传发生未知错误，文件尚未提交。请重试或重新选择文件。",
          });
        })
        .finally(() => {
          if (uploadRequest.current === controller) uploadRequest.current = null;
        });
    },
    [refresh],
  );

  const chooseUpload = useCallback(() => {
    uploadInput.current?.click();
  }, []);

  const onUploadSelected = useCallback(
    (event: ChangeEvent<HTMLInputElement>) => {
      const file = event.currentTarget.files?.[0];
      event.currentTarget.value = "";
      if (!file) return;
      if (!file.name.toLocaleLowerCase().endsWith(".pptx")) {
        uploadAttempt.current = null;
        setUploadState({
          kind: "error",
          fileName: file.name,
          message: "仅支持 .pptx 文件，请重新选择 PowerPoint 演示文稿。",
        });
        return;
      }
      const attempt = { file, idempotencyKey: createUploadIdempotencyKey() };
      uploadAttempt.current = attempt;
      submitUpload(attempt);
    },
    [submitUpload],
  );

  useEffect(() => {
    if (window.location.pathname === "/") {
      window.history.replaceState(null, "", "/documents");
    }
    refresh();
    return () => {
      activeRequest.current?.controller.abort();
      uploadRequest.current?.abort();
    };
  }, [refresh]);

  useEffect(() => {
    document.title = isMapping
      ? "PPTExtract · 页对应"
      : isCuration
        ? "PPTExtract · 逐页策展"
        : isPublication
          ? "PPTExtract · 发布"
          : "PPTExtract · 文档";
  }, [isCuration, isMapping, isPublication]);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      if (target?.matches("input, textarea, select, [contenteditable='true']")) return;
      if (document.querySelector("[aria-modal='true']")) return;
      if (event.key === "Escape" && isKeyboardHelpOpen) {
        event.preventDefault();
        setIsKeyboardHelpOpen(false);
        return;
      }
      if (event.key === "?") {
        event.preventDefault();
        setIsKeyboardHelpOpen((open) => !open);
        return;
      }
      if (!isMapping && !isCuration && event.key.toLowerCase() === "r") {
        event.preventDefault();
        refresh();
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [isCuration, isKeyboardHelpOpen, isMapping, refresh]);

  useEffect(() => {
    if (!isKeyboardHelpOpen) return;
    const closeOnOutsidePointer = (event: PointerEvent) => {
      if (!keyboardHelp.current?.contains(event.target as Node)) {
        setIsKeyboardHelpOpen(false);
      }
    };
    document.addEventListener("pointerdown", closeOnOutsidePointer, true);
    return () => document.removeEventListener("pointerdown", closeOnOutsidePointer, true);
  }, [isKeyboardHelpOpen]);

  const actor = state.kind === "ready" ? state.data.actor : null;

  return (
    <div className="app-shell">
      <header className="topbar">
        <ProductMark />
        <span className="topbar-divider" aria-hidden="true" />
        <nav aria-label="主要区域">
          <a href="/documents" aria-current={!isCuration && !isPublication ? "page" : undefined}>
            文档
          </a>
          <a href="/curation" aria-current={isCuration ? "page" : undefined}>
            策展
          </a>
          <a href="/publication" aria-current={isPublication ? "page" : undefined}>
            发布
          </a>
        </nav>
        <div className="topbar-actions">
          <div className="actor-context" aria-label="当前操作者">
            <UserIcon />
            <span>{actor?.display_name ?? "正在识别操作者"}</span>
          </div>
          {!isCuration && !isMapping && !isPublication ? (
            <div className="upload-boundary">
              <input
                ref={uploadInput}
                hidden
                type="file"
                accept=".pptx,application/vnd.openxmlformats-officedocument.presentationml.presentation"
                aria-label="选择 PPTX 文件"
                disabled={state.kind !== "ready" || uploadState.kind === "uploading"}
                onChange={onUploadSelected}
              />
              <button
                type="button"
                className={`upload-button${uploadState.kind === "uploading" ? " is-uploading" : ""}`}
                aria-describedby="upload-button-help"
                aria-expanded={uploadState.kind !== "idle"}
                aria-controls="upload-feedback"
                disabled={state.kind !== "ready" || uploadState.kind === "uploading"}
                onClick={chooseUpload}
              >
                <UploadIcon />
                {uploadState.kind === "uploading" ? "正在上传…" : "上传 PPTX"}
              </button>
              <span id="upload-button-help" className="sr-only">
                选择一个 PPTX 文件，可靠保存后启动后台处理。
              </span>
              {uploadState.kind !== "idle" ? (
                <div
                  id="upload-feedback"
                  className={`upload-notice upload-notice--${uploadState.kind}`}
                  role={uploadState.kind === "error" ? "alert" : "status"}
                  aria-live="polite"
                >
                  {uploadState.kind === "uploading" ? (
                    <>
                      <strong>正在可靠提交“{uploadState.fileName}”</strong>
                      <span>请保持此页面打开，完整保存源文件后才会启动后台处理。</span>
                    </>
                  ) : null}
                  {uploadState.kind === "success" ? (
                    <>
                      <strong>已接收“{uploadState.fileName}”</strong>
                      <span>源文件已可靠保存，后台处理已启动；文档跑道正在刷新。</span>
                      <div className="upload-notice-actions">
                        <button type="button" onClick={() => setUploadState({ kind: "idle" })}>
                          关闭提示
                        </button>
                      </div>
                    </>
                  ) : null}
                  {uploadState.kind === "error" ? (
                    <>
                      <strong>“{uploadState.fileName}”上传未完成</strong>
                      <span>{uploadState.message}</span>
                      <div className="upload-notice-actions">
                        {uploadAttempt.current ? (
                          <button
                            type="button"
                            onClick={() => {
                              if (uploadAttempt.current) submitUpload(uploadAttempt.current);
                            }}
                          >
                            重试上传
                          </button>
                        ) : null}
                        <button type="button" onClick={chooseUpload}>
                          重新选择
                        </button>
                      </div>
                    </>
                  ) : null}
                </div>
              ) : null}
            </div>
          ) : null}
        </div>
      </header>

      {state.kind === "loading" ? <LoadingRunways /> : null}
      {state.kind === "error" ? <ErrorWorkspace message={state.message} retry={refresh} /> : null}
      {state.kind === "ready" && isCuration ? (
        <CurationWorkbench onCommandStateChange={setCurationCommands} />
      ) : null}
      {state.kind === "ready" && isPublication ? <PublicationPreflight /> : null}
      {state.kind === "ready" && isMapping && mappingMatch ? (
        <PageMappingWorkbench documentId={mappingMatch[1]} versionId={mappingMatch[2]} />
      ) : null}
      {state.kind === "ready" && !isCuration && !isMapping && !isPublication ? (
        <main className="workspace" aria-label="文档阶段跑道">
          <div className="runway-stack">
            {state.data.runways.map((runway) => (
              <StageRunway key={runway.id} runway={runway} />
            ))}
          </div>
        </main>
      ) : null}

      <footer className="command-strip" aria-label="工作位状态">
        <div className="command-help" ref={keyboardHelp}>
          <button
            type="button"
            className="command-help-trigger"
            aria-expanded={isKeyboardHelpOpen}
            aria-controls="keyboard-command-panel"
            onClick={() => setIsKeyboardHelpOpen((open) => !open)}
          >
            <KeyboardIcon />
            快捷键
          </button>
          {isKeyboardHelpOpen ? (
            <section
              id="keyboard-command-panel"
              className="command-help-panel"
              aria-label="键盘操作"
            >
              <header>
                <strong>键盘操作</strong>
                <span><kbd>Esc</kbd> 关闭</span>
              </header>
              <div className="command-help-items">
                {isCuration ? (
                  <>
                    {curationCommands?.navigation ? (
                      <span><kbd>←</kbd><kbd>→</kbd> 上一页 / 下一页</span>
                    ) : null}
                    {curationCommands?.approve ? <span><kbd>A</kbd> 批准</span> : null}
                    {curationCommands?.exclude ? <span><kbd>X</kbd> 排除原因</span> : null}
                    {curationCommands?.reopen ? <span><kbd>R</kbd> 重新打开</span> : null}
                    {curationCommands?.cancel ? <span><kbd>Esc</kbd> 取消</span> : null}
                    <span className="workspace-support-note">完整三栏适配 1280px 及以上</span>
                  </>
                ) : (
                  <>
                    <span>
                      <kbd>Tab</kbd>
                      <kbd>Shift</kbd> + <kbd>Tab</kbd> 移动焦点
                    </span>
                    <span>
                      <kbd>R</kbd>{" "}
                      {isMapping ? "刷新证据" : isPublication ? "刷新发布状态" : "刷新入口"}
                    </span>
                  </>
                )}
              </div>
            </section>
          ) : null}
        </div>
        <span className="command-status">
          {state.kind === "ready"
            ? isMapping
              ? "页对应工作位就绪"
              : isCuration
              ? curationCommands?.status ?? "正在读取策展工作位"
              : isPublication
                ? "发布工作位就绪"
              : uploadState.kind === "uploading"
                ? "正在可靠提交文件"
                : uploadState.kind === "error"
                  ? "上传需要处理"
                  : uploadState.kind === "success"
                    ? "上传已接受"
                    : "入口就绪"
            : state.kind === "error"
              ? "需要恢复"
              : "连接中"}
        </span>
      </footer>
    </div>
  );
}

interface ErrorBoundaryState {
  failed: boolean;
}

export class AppErrorBoundary extends Component<{ children: ReactNode }, ErrorBoundaryState> {
  state: ErrorBoundaryState = { failed: false };

  static getDerivedStateFromError(): ErrorBoundaryState {
    return { failed: true };
  }

  componentDidCatch(_error: Error, _info: ErrorInfo) {
    // 堆栈只留在开发控制台，不呈现在操作者界面。
  }

  render() {
    if (this.state.failed) {
      return (
        <div className="fatal-boundary" role="alert">
          <ProductMark />
          <h1>产品壳层未能完成加载</h1>
          <p>请刷新页面重新建立工作位；若问题持续，请联系系统维护人员。</p>
          <button type="button" onClick={() => window.location.reload()}>
            刷新页面
          </button>
        </div>
      );
    }
    return this.props.children;
  }
}
