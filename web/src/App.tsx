import {
  Component,
  type ErrorInfo,
  type ReactNode,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";

import { type BootstrapData, loadBootstrap, OperatorError, type Runway } from "./api";
import { CurationWorkbench } from "./CurationWorkbench";
import { PageMappingWorkbench } from "./PageMappingWorkbench";
import { PublicationPreflight } from "./PublicationPreflight";
import "./styles.css";

type LoadState =
  | { kind: "loading" }
  | { kind: "ready"; data: BootstrapData }
  | { kind: "error"; message: string };

const runwayCopy: Record<Runway["id"], { description: string; empty: string; next: string }> = {
  pending: {
    description: "等待开始处理的文档",
    empty: "还没有待处理文档",
    next: "上传入口开放后，新文档会先出现在这里。",
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
  const [showUploadBoundary, setShowUploadBoundary] = useState(false);
  const activeRequest = useRef<{ controller: AbortController; requestId: number } | null>(null);
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

  useEffect(() => {
    if (window.location.pathname === "/") {
      window.history.replaceState(null, "", "/documents");
    }
    refresh();
    return () => activeRequest.current?.controller.abort();
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
      if (!isMapping && event.key.toLowerCase() === "r") {
        event.preventDefault();
        refresh();
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [isMapping, refresh]);

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
              <button
                type="button"
                className="upload-button"
                aria-describedby="upload-boundary-copy"
                aria-expanded={showUploadBoundary}
                onClick={() => setShowUploadBoundary((visible) => !visible)}
              >
                <UploadIcon />
                上传 PPTX<span className="sr-only">（暂未开放）</span>
              </button>
              <span id="upload-boundary-copy" className="sr-only">
                上传流程将在 #20 接入，本版本不会提交文件。
              </span>
              {showUploadBoundary ? (
                <div className="upload-notice" role="status">
                  <strong>上传暂未开放</strong>
                  <span>上传流程将在 #20 接入；本版本不会提交文件。</span>
                </div>
              ) : null}
            </div>
          ) : null}
        </div>
      </header>

      {state.kind === "loading" ? <LoadingRunways /> : null}
      {state.kind === "error" ? <ErrorWorkspace message={state.message} retry={refresh} /> : null}
      {state.kind === "ready" && isCuration ? <CurationWorkbench /> : null}
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

      <footer className="command-strip" aria-label="键盘操作">
        <span className="command-strip-label">键盘操作</span>
        <span>
          <kbd>Tab</kbd>
          <kbd>Shift</kbd> + <kbd>Tab</kbd> 移动焦点
        </span>
        <span>
          <kbd>R</kbd>{" "}
          {isMapping
            ? "刷新证据"
            : isCuration
              ? "刷新工作位"
              : isPublication
                ? "刷新校验"
                : "刷新入口"}
        </span>
        <span className="command-status">
          {state.kind === "ready"
            ? isMapping
              ? "页对应工作位就绪"
              : isCuration
              ? "策展工作位就绪"
              : isPublication
                ? "发布校验工作位就绪"
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
