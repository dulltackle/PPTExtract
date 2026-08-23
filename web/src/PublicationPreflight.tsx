import { useCallback, useEffect, useRef, useState } from "react";

import {
  loadPublicationPreflight,
  OperatorError,
  type PublicationPreflight as PublicationPreflightData,
  validatePublicationPreflight,
} from "./api";

type LoadState =
  | { kind: "loading" }
  | { kind: "ready"; data: PublicationPreflightData }
  | { kind: "error"; message: string };

export function PublicationPreflight() {
  const [state, setState] = useState<LoadState>({ kind: "loading" });
  const request = useRef<AbortController | null>(null);
  const [validating, setValidating] = useState(false);
  const [announcement, setAnnouncement] = useState<string | null>(null);

  const load = useCallback(() => {
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    setState({ kind: "loading" });
    loadPublicationPreflight(controller.signal)
      .then((data) => setState({ kind: "ready", data }))
      .catch((cause) => {
        if (cause instanceof DOMException && cause.name === "AbortError") return;
        setState({
          kind: "error",
          message:
            cause instanceof OperatorError
              ? cause.message
              : "发布前置校验发生未知错误，请重试。",
        });
      });
  }, []);

  useEffect(() => {
    load();
    return () => request.current?.abort();
  }, [load]);

  if (state.kind === "loading") {
    return (
      <main className="publication-workspace" aria-busy="true">
        <section className="publication-ledger publication-ledger--loading">
          <h1>发布前置校验</h1>
          <p>正在复核当前业务状态与渲染警告确认记录…</p>
        </section>
      </main>
    );
  }

  if (state.kind === "error") {
    return (
      <main className="publication-workspace">
        <section className="publication-ledger publication-ledger--error" role="alert">
          <h1>发布前置校验</h1>
          <strong>校验连接中断</strong>
          <p>{state.message}</p>
          <button type="button" onClick={load}>重新校验</button>
        </section>
      </main>
    );
  }

  const { data } = state;
  const blocked = !data.can_publish;
  const validate = async () => {
    if (blocked || validating) return;
    setValidating(true);
    setAnnouncement(null);
    try {
      await validatePublicationPreflight();
      setAnnouncement("发布前置校验已确认，可以进入发布候选流程。");
    } catch (cause) {
      setAnnouncement(
        cause instanceof OperatorError ? cause.message : "发布前置校验未完成，请重试。",
      );
      load();
    } finally {
      setValidating(false);
    }
  };
  return (
    <main className="publication-workspace">
      <section className={`publication-ledger ${blocked ? "is-blocked" : "is-clear"}`}>
        <header>
          <div>
            <h1>发布前置校验</h1>
            <p>发布候选建立前的独立质量闸门</p>
          </div>
          <span className="publication-state-chip">{blocked ? "发布被阻止" : "可以继续发布"}</span>
        </header>

        <div className="publication-check-row">
          <span className="publication-check-mark" aria-hidden="true" />
          <div>
            <strong>渲染警告确认</strong>
            <p>
              {data.stale_render_versions > 0
                ? `${data.stale_render_versions} 个版本正在按新渲染配置重建`
                : blocked
                ? `${data.summary.unconfirmed_pages} 页 / ${data.summary.unconfirmed} 条未确认`
                : data.summary.total
                  ? `${data.summary.total} 条渲染警告均已确认`
                  : "当前内容未发现渲染警告"}
            </p>
          </div>
          <span>{blocked ? "硬阻塞" : "通过"}</span>
        </div>

        <div className="publication-actions">
          {blocked && data.href ? (
            <a href={data.href}>前往确认渲染警告</a>
          ) : blocked ? (
            <span>渲染配置重建完成后可继续。</span>
          ) : (
            <span>后续发布候选能力由发布流程接续。</span>
          )}
          <button type="button" disabled={blocked || validating} onClick={() => void validate()}>
            {validating ? "正在确认" : "确认并继续"}
          </button>
        </div>
        {announcement ? <p role="status" aria-live="polite">{announcement}</p> : null}
      </section>
    </main>
  );
}
