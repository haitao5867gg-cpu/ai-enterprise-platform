"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import {
  ApiError,
  errorMessage,
  isRecord,
  readReceipt,
  readRun,
  requestJson,
  type Decision,
  type Run,
} from "../lib/runs";
import { Icon } from "./icons";
import { ErrorNotice, StatusBadge } from "./ui";

function ResultView({ result }: { result: unknown }) {
  if (result === null) return <p>应用已完成本次运行。</p>;
  if (typeof result === "string")
    return <p className="message-content">{result}</p>;
  const text = isRecord(result)
    ? typeof result.answer === "string"
      ? result.answer
      : typeof result.text === "string"
        ? result.text
        : null
    : null;
  const content = (
    <pre className="result-json">{JSON.stringify(result, null, 2)}</pre>
  );
  return text ? (
    <>
      <p className="message-content">{text}</p>
      <details>
        <summary>查看完整结果</summary>
        {content}
      </details>
    </>
  ) : (
    content
  );
}

export function RunDetail({ id }: { id: string }) {
  const [run, setRun] = useState<Run | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [mutationError, setMutationError] = useState("");
  const [comment, setComment] = useState("");
  const [submitting, setSubmitting] = useState<Decision | null>(null);
  const [rechecking, setRechecking] = useState(false);
  const [revision, setRevision] = useState(0);
  const pollAbort = useRef<AbortController | null>(null);
  const mutationAbort = useRef<AbortController | null>(null);
  const busy = useRef(false);

  useEffect(() => () => mutationAbort.current?.abort(), []);

  useEffect(() => {
    const controller = new AbortController();
    pollAbort.current = controller;
    let timer: ReturnType<typeof setTimeout> | undefined;
    async function poll() {
      if (controller.signal.aborted) return;
      let terminal = false;
      try {
        const value = readRun(
          await requestJson(`/runs/${encodeURIComponent(id)}`, {
            signal: controller.signal,
          }),
        );
        if (controller.signal.aborted) return;
        if (value.run_id !== id)
          throw new ApiError("服务返回的运行编号不匹配，请重试。");
        setRun(value);
        setError("");
        setRechecking(false);
        terminal = value.status === "done" || value.status === "error";
      } catch (err) {
        if (controller.signal.aborted) return;
        setError(errorMessage(err));
        terminal = err instanceof ApiError && err.status === 404;
      } finally {
        if (!controller.signal.aborted) {
          setLoading(false);
          // Schedule after completion: slow requests never overlap or reorder messages.
          if (!terminal) timer = setTimeout(() => void poll(), 2_000);
        }
      }
    }
    void poll();
    return () => {
      controller.abort();
      if (timer) clearTimeout(timer);
    };
  }, [id, revision]);

  async function resume(decision: Decision) {
    if (
      busy.current ||
      rechecking ||
      error ||
      run?.status !== "waiting" ||
      !run.pending?.options.includes(decision)
    )
      return;
    busy.current = true;
    setSubmitting(decision);
    setMutationError("");
    // Invalidate any pre-resume GET so it cannot restore an old approval card.
    pollAbort.current?.abort();
    const controller = new AbortController();
    mutationAbort.current = controller;
    try {
      const receipt = readReceipt(
        await requestJson(`/runs/${encodeURIComponent(id)}/resume`, {
          method: "POST",
          signal: controller.signal,
          body: JSON.stringify({
            decision,
            comment: decision === "reject" ? comment.trim() : "",
          }),
        }),
      );
      if (controller.signal.aborted) return;
      if (receipt.run_id !== id)
        throw new ApiError("服务返回的运行编号不匹配，请刷新核对。");
      setRun((previous) =>
        previous
          ? {
              ...previous,
              status: receipt.status,
              pending: receipt.status === "waiting" ? previous.pending : null,
            }
          : null,
      );
    } catch (err) {
      if (!controller.signal.aborted) setMutationError(errorMessage(err));
    } finally {
      if (!controller.signal.aborted) {
        setRechecking(true);
        setSubmitting(null);
        busy.current = false;
        setRevision((value) => value + 1);
      }
    }
  }

  const controlsDisabled = submitting !== null || rechecking || Boolean(error);
  return (
    <>
      <Link className="back-link" href="/">
        <Icon name="back" size={16} />
        返回运行记录
      </Link>
      <div className="page-heading detail-heading">
        <div>
          <div className="eyebrow">EVERY STEP, CONNECTED</div>
          <h1>运行详情{run && <StatusBadge status={run.status} />}</h1>
          <p className="detail-id">{id}</p>
        </div>
        <Link className="button secondary" href="/runs/new">
          <Icon name="plus" size={16} />
          新建运行
        </Link>
      </div>
      {error && (
        <ErrorNotice
          message={error}
          onRetry={() => setRevision((value) => value + 1)}
        />
      )}
      {loading ? (
        <div className="panel loading-state" role="status">
          <span className="spinner" />
          正在加载运行详情…
        </div>
      ) : (
        run && (
          <div className="detail-grid">
            <section className="panel conversation-panel" aria-label="运行对话">
              <div className="section-heading">
                <h2>
                  <Icon name="runs" />
                  对话过程
                </h2>
                <span className="muted">{run.messages.length} 条消息</span>
              </div>
              <ol className="messages" aria-label="消息记录">
                {run.messages.map((message, index) => (
                  <li
                    className={`message ${message.role === "user" ? "user-message" : ""}`}
                    key={index}
                  >
                    <span className="message-avatar">
                      {message.role === "user" ? (
                        "你"
                      ) : (
                        <Icon name="spark" size={18} />
                      )}
                    </span>
                    <div className="message-body">
                      <div className="message-meta">
                        <strong>
                          {message.role === "user"
                            ? "你"
                            : message.role === "assistant"
                              ? run.app
                              : "系统"}
                        </strong>
                        <span>{String(index + 1).padStart(2, "0")}</span>
                      </div>
                      <p className="message-content">{message.content}</p>
                    </div>
                  </li>
                ))}
              </ol>
              {run.messages.length === 0 && (
                <p className="panel-padding muted">应用暂未返回消息。</p>
              )}
              <div className="conversation-state" role="status">
                {run.status === "running" ? (
                  <>
                    <span className="spinner" />
                    应用正在运行，消息将自动更新
                  </>
                ) : run.status === "waiting" ? (
                  <>
                    <span className="dot amber" />
                    应用已暂停，等待人工确认
                  </>
                ) : run.status === "done" ? (
                  <>
                    <Icon name="check" size={16} />
                    本次运行已结束
                  </>
                ) : (
                  <>
                    <Icon name="alert" size={16} />
                    本次运行失败
                  </>
                )}
              </div>
            </section>
            <aside className="run-sidebar">
              <div className="panel run-info">
                <h2>运行信息</h2>
                <dl>
                  <div>
                    <dt>应用</dt>
                    <dd>
                      <span className="app-tag">
                        <Icon name="spark" size={14} />
                        {run.app}
                      </span>
                    </dd>
                  </div>
                  <div>
                    <dt>当前状态</dt>
                    <dd>
                      <StatusBadge status={run.status} />
                    </dd>
                  </div>
                  <div>
                    <dt>运行编号</dt>
                    <dd className="monospace">{run.run_id}</dd>
                  </div>
                </dl>
              </div>
              {run.status === "waiting" && (
                <section
                  className="approval-card"
                  aria-labelledby="approval-title"
                >
                  <span className="approval-icon">
                    <Icon name="alert" size={20} />
                  </span>
                  <h2 id="approval-title">需要你的确认</h2>
                  {run.pending ? (
                    <>
                      <p className="message-content">{run.pending.prompt}</p>
                      <label htmlFor="rejection-comment">
                        驳回意见<span className="muted">（选填）</span>
                      </label>
                      <textarea
                        id="rejection-comment"
                        rows={3}
                        placeholder="说明需要调整的地方…"
                        value={comment}
                        onChange={(e) => setComment(e.target.value)}
                        disabled={controlsDisabled}
                      />
                      <div className="approval-actions">
                        <button
                          className="button primary"
                          disabled={
                            controlsDisabled ||
                            !run.pending.options.includes("approve")
                          }
                          onClick={() => void resume("approve")}
                        >
                          {submitting === "approve" ? (
                            <>
                              <span className="spinner" />
                              提交中…
                            </>
                          ) : (
                            <>
                              <Icon name="check" size={16} />
                              确认
                            </>
                          )}
                        </button>
                        <button
                          className="button reject"
                          disabled={
                            controlsDisabled ||
                            !run.pending.options.includes("reject")
                          }
                          onClick={() => void resume("reject")}
                        >
                          {submitting === "reject" ? (
                            <>
                              <span className="spinner" />
                              提交中…
                            </>
                          ) : (
                            <>
                              <Icon name="close" size={16} />
                              驳回
                            </>
                          )}
                        </button>
                      </div>
                      {rechecking && (
                        <p className="field-hint" role="status">
                          正在核对最新运行状态…
                        </p>
                      )}
                    </>
                  ) : (
                    <p>应用正在准备确认内容，请稍候…</p>
                  )}
                </section>
              )}
              {mutationError && <ErrorNotice message={mutationError} />}
              {run.status === "done" && (
                <section
                  className="panel result-card"
                  aria-labelledby="result-title"
                >
                  <span className="result-icon">
                    <Icon name="check" size={20} />
                  </span>
                  <h2 id="result-title">运行结果</h2>
                  <ResultView result={run.result} />
                </section>
              )}
              {run.status === "error" && (
                <section className="panel failure-card">
                  <h2>运行失败</h2>
                  <p className="message-content">
                    {run.error || "应用未提供错误详情，请稍后重新发起运行。"}
                  </p>
                  <Link className="text-button" href="/runs/new">
                    重新发起
                    <Icon name="arrow" size={14} />
                  </Link>
                </section>
              )}
              <p className="detail-note">
                <Icon name="refresh" size={14} />
                运行期间每 2 秒自动更新
              </p>
            </aside>
          </div>
        )
      )}
    </>
  );
}
