"use client";

import Link from "next/link";
import { CitationScope, CitedMarkdown, SourceList } from "./citations";
import { useEffect, useRef, useState } from "react";
import {
  ApiError,
  cancelRun,
  durationText,
  errorMessage,
  failureText,
  fieldErrors,
  formatTime,
  isFinished,
  isRecord,
  retryRun,
  readReceipt,
  readRun,
  requestJson,
  runTitle,
  stepText,
  timeAgo,
  versionLabel,
  type Decision,
  type Run,
} from "../lib/runs";
import { Icon } from "./icons";
import { ErrorNotice, StatusBadge } from "./ui";
import { etlFailed, getEtlPlan, getEtlRelease, getEtlResult } from "../lib/etl";
import { EtlPlanView } from "./etl-plan";
import { EtlReleaseView, EtlTrail } from "./etl-release";
import { GeneratedFiles } from "./generated-files";
import { RunFlow } from "./run-flow";
import { RunManual } from "./app-manual";
import { savedActor, saveActor } from "../lib/actor";
import {
  initialValues,
  inputPayload,
  validateFields,
  type FieldValues,
} from "../lib/input-fields";
import { InputFields } from "./input-fields";

// A JSON object as a whole message, bare or in a code fence: what LLM nodes with
// JSON output used to add to the conversation (older runs still hold them), and
// what text nodes asked for JSON (such as plan and codegen) still add.
function jsonMessage(text: string): Record<string, unknown> | null {
  const trimmed = text.trim();
  const body =
    /^```(?:json)?\s*([\s\S]*?)\s*```$/.exec(trimmed)?.[1] ?? trimmed;
  if (!body.startsWith("{")) return null;
  try {
    const value: unknown = JSON.parse(body);
    return isRecord(value) ? value : null;
  } catch {
    return null;
  }
}

// What a person typed is shown as typed; replies are Markdown with [n]
// citations, and raw JSON is folded away.
function MessageContent({ role, content }: { role: string; content: string }) {
  if (role === "user") return <p className="message-content">{content}</p>;
  const data = jsonMessage(content);
  if (data)
    return (
      <details className="message-content message-data">
        <summary>模型返回的结构化数据</summary>
        <pre>{JSON.stringify(data, null, 2)}</pre>
      </details>
    );
  return (
    <div className="message-content message-markdown">
      <CitedMarkdown text={content} breaks />
    </div>
  );
}

function ResultView({ result }: { result: unknown }) {
  if (result === null) return <p>应用已完成本次运行。</p>;
  if (typeof result === "string") return <CitedMarkdown text={result} />;
  function renderedStrings(
    value: unknown,
    path = "",
  ): { path: string; text: string }[] {
    if (typeof value === "string") return [{ path, text: value }];
    if (Array.isArray(value))
      return value.flatMap((item, i) => renderedStrings(item, `${path}[${i}]`));
    if (isRecord(value))
      return Object.entries(value).flatMap(([key, item]) =>
        renderedStrings(item, path ? `${path}.${key}` : key),
      );
    return [];
  }
  const strings = renderedStrings(result);
  const content = (
    <pre className="result-json">{JSON.stringify(result, null, 2)}</pre>
  );
  return strings.length ? (
    <>
      {strings.map(({ path, text }) => (
        <div key={path} className="result-field">
          {strings.length > 1 && <h3>{path}</h3>}
          <CitedMarkdown text={text} />
        </div>
      ))}
      <details>
        <summary>查看完整结果</summary>
        {content}
      </details>
    </>
  ) : (
    content
  );
}

export function RunDetail({
  id,
  langfuseTraceBaseUrl,
}: {
  id: string;
  langfuseTraceBaseUrl: string | null;
}) {
  const [run, setRun] = useState<Run | null>(null);
  const [now, setNow] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [mutationError, setMutationError] = useState("");
  const [confirmingCancel, setConfirmingCancel] = useState(false);
  const [acting, setActing] = useState<"cancel" | "retry" | null>(null);
  const [actionError, setActionError] = useState("");
  const [values, setValues] = useState<FieldValues>({});
  const [valueErrors, setValueErrors] = useState<Record<string, string>>({});
  const [comment, setComment] = useState("");
  // On the server there is no storage and "" comes back; the field only renders after loading.
  const [actor, setActor] = useState(savedActor);
  const [submitting, setSubmitting] = useState<Decision | null>(null);
  const [rechecking, setRechecking] = useState(false);
  const [revision, setRevision] = useState(0);
  const pollAbort = useRef<AbortController | null>(null);
  const mutationAbort = useRef<AbortController | null>(null);
  const busy = useRef(false);
  const title = useRef<HTMLHeadingElement>(null);
  const actionBar = useRef<HTMLDivElement>(null);
  const cancelButton = useRef<HTMLButtonElement>(null);
  const keepButton = useRef<HTMLButtonElement>(null);
  const confirmPanel = useRef<HTMLElement>(null);
  // Set when the control holding focus may go away with the next render.
  const refocus = useRef(false);
  const confirmOpen = confirmingCancel && Boolean(run?.actions?.cancel);

  useEffect(() => () => mutationAbort.current?.abort(), []);

  // The confirmation opens on its safe choice.
  useEffect(() => {
    if (confirmOpen) keepButton.current?.focus();
  }, [confirmOpen]);

  // A button that went away (the run moved on) leaves focus on the title, not the page.
  useEffect(() => {
    if (!refocus.current) return;
    refocus.current = false;
    const active = document.activeElement;
    if (!active || active === document.body) title.current?.focus();
  });

  function controlsHoldFocus() {
    const active = document.activeElement;
    return Boolean(
      active &&
      (actionBar.current?.contains(active) ||
        confirmPanel.current?.contains(active)),
    );
  }

  function closeConfirm() {
    setConfirmingCancel(false);
    cancelButton.current?.focus();
  }

  useEffect(() => {
    const controller = new AbortController();
    pollAbort.current = controller;
    let timer: ReturnType<typeof setTimeout> | undefined;
    async function poll() {
      if (controller.signal.aborted) return;
      let terminal = false;
      let delay = 2_000;
      try {
        const value = readRun(
          await requestJson(`/runs/${encodeURIComponent(id)}`, {
            signal: controller.signal,
          }),
        );
        if (controller.signal.aborted) return;
        if (value.run_id !== id)
          throw new ApiError("服务返回的运行编号不匹配，请重试。");
        if (controlsHoldFocus()) refocus.current = true;
        setRun(value);
        // A confirmation for a cancel that is no longer offered must not come back.
        if (!value.actions?.cancel) setConfirmingCancel(false);
        setNow(Date.now());
        setError("");
        setRechecking(false);
        // A failed run someone may retry is still watched, less often.
        terminal = isFinished(value.status) && !value.actions?.retry;
        delay = value.status === "error" ? 10_000 : 2_000;
      } catch (err) {
        if (controller.signal.aborted) return;
        setError(errorMessage(err));
        terminal = err instanceof ApiError && err.status === 404;
      } finally {
        if (!controller.signal.aborted) {
          setLoading(false);
          // Schedule after completion: slow requests never overlap or reorder messages.
          if (!terminal) timer = setTimeout(() => void poll(), delay);
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
    const fields = run.pending.fields ?? [];
    const submittedValues = { ...initialValues(fields), ...values };
    if (run.pending.kind === "form") {
      const issues = validateFields(fields, submittedValues, "values");
      setValueErrors(
        Object.fromEntries(issues.map((issue) => [issue.loc[2], issue.msg])),
      );
      if (issues.length) return;
    }
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
            ...(run.pending.kind === "form"
              ? { values: inputPayload(fields, submittedValues) }
              : { comment: comment.trim() }),
            ...(actor.trim() ? { actor: actor.trim() } : {}),
          }),
        }),
      );
      if (controller.signal.aborted) return;
      if (receipt.run_id !== id)
        throw new ApiError("服务返回的运行编号不匹配，请刷新核对。");
      setComment("");
      setValues({});
      setValueErrors({});
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
      if (!controller.signal.aborted) {
        setMutationError(errorMessage(err));
        setValueErrors(fieldErrors(err, "values"));
      }
    } finally {
      if (!controller.signal.aborted) {
        setRechecking(true);
        setSubmitting(null);
        busy.current = false;
        setRevision((value) => value + 1);
      }
    }
  }

  // Cancel the run, or retry it from the step that failed.
  async function act(kind: "cancel" | "retry") {
    if (busy.current || !run) return;
    busy.current = true;
    const hadFocus = controlsHoldFocus();
    setActing(kind);
    setActionError("");
    // Invalidate any GET sent before the change so it cannot show the old state.
    pollAbort.current?.abort();
    const controller = new AbortController();
    mutationAbort.current = controller;
    try {
      const who = actor.trim() ? { actor: actor.trim() } : {};
      const receipt =
        kind === "cancel"
          ? await cancelRun(id, who, controller.signal)
          : await retryRun(id, who, controller.signal);
      if (controller.signal.aborted) return;
      if (receipt.run_id !== id)
        throw new ApiError("服务返回的运行编号不匹配，请刷新核对。");
      setConfirmingCancel(false);
      // Until the run is read again, offer only what fits its new status.
      setRun((previous) =>
        previous
          ? {
              ...previous,
              status: receipt.status,
              pending: null,
              failed: null,
              actions: previous.actions && {
                ...previous.actions,
                retry: false,
                cancel: !isFinished(receipt.status),
              },
            }
          : null,
      );
    } catch (err) {
      if (!controller.signal.aborted) setActionError(errorMessage(err));
    } finally {
      if (!controller.signal.aborted) {
        refocus.current = hadFocus;
        setRechecking(true);
        setActing(null);
        busy.current = false;
        setRevision((value) => value + 1);
      }
    }
  }

  const controlsDisabled = submitting !== null || rechecking || Boolean(error);
  const plan =
    run?.status === "waiting" ? getEtlPlan(run.pending?.details) : null;
  const release =
    run?.status === "waiting" ? getEtlRelease(run.pending?.details) : null;
  // The code waiting for the release approval is shown with it.
  const etlResult =
    run?.app === "etl" && (run.status === "done" || release)
      ? getEtlResult(run.result)
      : null;
  const hasReport =
    !etlResult &&
    isRecord(run?.result) &&
    typeof run.result.report === "string";
  // A rejected release sends the code back to be rewritten, so the reason must be given.
  const requiredOptions =
    run?.pending?.comment_required ?? (release ? ["reject"] : []);
  const commentRequired = requiredOptions.includes("reject");
  const humanDetails =
    isRecord(run?.pending?.details) && run.pending.details.type === "flow_human"
      ? run.pending.details
      : null;
  return (
    <CitationScope sources={run?.sources ?? []}>
      <Link className="back-link" href="/">
        <Icon name="back" size={16} />
        返回运行记录
      </Link>
      <div className="page-heading detail-heading">
        <div>
          <h1 ref={title} tabIndex={-1}>
            <span className="detail-title">
              {run ? runTitle(run) : "运行详情"}
            </span>
            {run && <StatusBadge status={run.status} />}
          </h1>
          {run && (
            <p className="detail-meta">
              {[
                run.created_by && `${run.created_by} 提出`,
                run.created_at && timeAgo(run.created_at, now),
                durationText(run, now),
                [run.app_title || run.app, versionLabel(run)]
                  .filter(Boolean)
                  .join(" "),
              ]
                .filter(Boolean)
                .join(" · ")}
            </p>
          )}
        </div>
        <div className="detail-actions" ref={actionBar}>
          {run?.actions?.cancel && (
            <button
              ref={cancelButton}
              className="button reject"
              disabled={acting !== null || submitting !== null || rechecking}
              aria-expanded={confirmingCancel}
              aria-controls="cancel-confirm"
              onClick={() => setConfirmingCancel(true)}
            >
              <Icon name="close" size={15} />
              取消运行
            </button>
          )}
          {run?.actions?.retry && (
            <button
              className="button primary"
              disabled={acting !== null || rechecking}
              onClick={() => void act("retry")}
            >
              {acting === "retry" ? (
                <>
                  <span className="spinner" />
                  正在重试…
                </>
              ) : (
                <>
                  <Icon name="refresh" size={15} />
                  {run.failed?.title ? `从「${run.failed.title}」重试` : "重试"}
                </>
              )}
            </button>
          )}
          {run?.actions?.rerun && (
            <Link
              className="button secondary"
              href={`/runs/new?from=${encodeURIComponent(id)}`}
            >
              复制并重新发起
            </Link>
          )}
          <Link className="button secondary" href="/runs/new">
            <Icon name="plus" size={16} />
            新建运行
          </Link>
        </div>
      </div>
      {confirmOpen && (
        <section
          ref={confirmPanel}
          id="cancel-confirm"
          className="panel cancel-confirm"
          role="alertdialog"
          aria-labelledby="cancel-title"
          aria-describedby="cancel-description"
          onKeyDown={(event) => {
            if (event.key === "Escape" && acting === null) closeConfirm();
          }}
        >
          <h2 id="cancel-title">取消这次运行？</h2>
          <p id="cancel-description">
            取消后这次运行不能再继续，只能复制后重新发起。已经写进开发环境的表会保留，正在
            Databricks 上运行的作业也不会被停下。
          </p>
          <div className="cancel-confirm-actions">
            <button
              className="button reject"
              disabled={acting !== null || rechecking}
              onClick={() => void act("cancel")}
            >
              {acting === "cancel" ? (
                <>
                  <span className="spinner" />
                  正在取消…
                </>
              ) : (
                "确定取消"
              )}
            </button>
            <button
              ref={keepButton}
              className="button secondary"
              disabled={acting !== null}
              onClick={closeConfirm}
            >
              不取消
            </button>
          </div>
        </section>
      )}
      {actionError && <ErrorNotice message={actionError} />}
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
            <div className="run-main">
              {plan && <EtlPlanView plan={plan} />}
              {release && <EtlReleaseView request={release} />}
              {etlResult && <EtlTrail result={etlResult} />}
              {etlResult && <GeneratedFiles result={etlResult} runId={id} />}
              <RunManual runId={id} />
              <RunFlow
                runId={id}
                version={`${run.status}:${run.messages.length}`}
                live={run.status === "running"}
              />
              {run.status === "done" && hasReport && (
                <section
                  className="panel result-card run-report"
                  aria-labelledby="result-title"
                >
                  <span className="result-icon">
                    <Icon name="check" size={20} />
                  </span>
                  <h2 id="result-title">运行结果</h2>
                  <ResultView result={run.result} />
                  <SourceList />
                </section>
              )}
              <details
                className={`conversation-history ${run.app === "etl" ? "collapsible" : ""}`}
                open={run.app !== "etl" || (!plan && !release && !etlResult)}
              >
                <summary>查看对话过程 · {run.messages.length} 条消息</summary>
                <section
                  className="panel conversation-panel"
                  aria-label="运行对话"
                >
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
                          <MessageContent
                            role={message.role}
                            content={message.content}
                          />
                        </div>
                      </li>
                    ))}
                  </ol>
                  {run.messages.length === 0 && (
                    <p className="panel-padding muted">应用暂未返回消息。</p>
                  )}
                  {/* Until the result card shows them, [n] in replies point here. */}
                  {run.status !== "done" && run.sources?.length ? (
                    <div className="panel-padding">
                      <SourceList />
                    </div>
                  ) : null}
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
                    ) : run.status === "cancelled" ? (
                      <>
                        <Icon name="close" size={16} />
                        本次运行已取消
                      </>
                    ) : (
                      <>
                        <Icon name="alert" size={16} />
                        本次运行失败
                      </>
                    )}
                  </div>
                </section>
              </details>
            </div>
            <aside className="run-sidebar">
              <div className="panel run-info">
                <h2>运行信息</h2>
                <dl>
                  <div>
                    <dt>应用</dt>
                    <dd>
                      <span className="app-tag">
                        <Icon name="spark" size={14} />
                        {run.app_title || run.app}
                      </span>
                    </dd>
                  </div>
                  <div>
                    <dt>应用版本</dt>
                    <dd>
                      {run.draft
                        ? "草稿试运行"
                        : run.app_version
                          ? `v${run.app_version}`
                          : "—"}
                    </dd>
                  </div>
                  <div>
                    <dt>当前状态</dt>
                    <dd>
                      <StatusBadge status={run.status} />
                      {stepText(run) && (
                        <span className="run-step">{stepText(run)}</span>
                      )}
                    </dd>
                  </div>
                  <div>
                    <dt>提出人</dt>
                    <dd>{run.created_by || "—"}</dd>
                  </div>
                  {run.rerun_of && (
                    <div>
                      <dt>复制自</dt>
                      <dd>
                        <Link
                          className="text-button"
                          href={`/runs/${encodeURIComponent(run.rerun_of)}`}
                        >
                          查看原运行
                        </Link>
                      </dd>
                    </div>
                  )}
                  {run.created_at && (
                    <div>
                      <dt>提交时间</dt>
                      <dd>{formatTime(run.created_at)}</dd>
                    </div>
                  )}
                  <div>
                    <dt>运行编号</dt>
                    <dd className="monospace">{run.run_id}</dd>
                  </div>
                </dl>
                {langfuseTraceBaseUrl && run.trace_id && (
                  <a
                    className="trace-link"
                    target="_blank"
                    rel="noopener noreferrer"
                    href={`${langfuseTraceBaseUrl}${encodeURIComponent(run.trace_id)}`}
                  >
                    在 Langfuse 中查看
                    <Icon name="arrow" size={14} />
                  </a>
                )}
              </div>
              {run.status === "waiting" && (
                <section
                  className="approval-card"
                  aria-labelledby="approval-title"
                >
                  <span className="approval-icon">
                    <Icon name="alert" size={20} />
                  </span>
                  <h2 id="approval-title">
                    {run.pending?.kind === "form"
                      ? "需要你填写"
                      : "需要你的确认"}
                  </h2>
                  {run.pending ? (
                    <>
                      <p className="message-content">{run.pending.prompt}</p>
                      {humanDetails &&
                        typeof humanDetails.show === "string" &&
                        humanDetails.show && (
                          <pre className="human-show">{humanDetails.show}</pre>
                        )}
                      {run.pending.kind === "form" && (
                        <InputFields
                          key={String(humanDetails?.node ?? "form")}
                          fields={run.pending.fields ?? []}
                          values={{
                            ...initialValues(run.pending.fields ?? []),
                            ...values,
                          }}
                          onChange={(next) => {
                            setValues(next);
                            setValueErrors({});
                          }}
                          errors={valueErrors}
                          disabled={controlsDisabled}
                          prefix="human"
                        />
                      )}
                      <label htmlFor="approval-actor">
                        审批人<span className="muted">（浏览器会记住）</span>
                      </label>
                      <input
                        id="approval-actor"
                        placeholder="你的姓名"
                        value={actor}
                        maxLength={80}
                        onChange={(e) => {
                          setActor(e.target.value);
                          saveActor(e.target.value.trim());
                        }}
                        disabled={controlsDisabled}
                      />
                      {run.pending.kind !== "form" && (
                        <>
                          <label htmlFor="rejection-comment">
                            驳回意见
                            <span className="muted">
                              {commentRequired
                                ? "（驳回时必填）"
                                : requiredOptions.length
                                  ? "（部分操作必填）"
                                  : "（选填）"}
                            </span>
                          </label>
                          <textarea
                            id="rejection-comment"
                            rows={3}
                            placeholder="说明需要调整的地方…"
                            value={comment}
                            onChange={(e) => setComment(e.target.value)}
                            disabled={controlsDisabled}
                          />
                        </>
                      )}
                      <div className="approval-actions">
                        {(run.pending.option_labels ||
                        run.pending.kind === "form"
                          ? run.pending.options
                          : ["approve", "reject"]
                        ).map((option) => (
                          <button
                            key={option}
                            type="button"
                            className={`button ${option === "reject" ? "reject" : "primary"}`}
                            disabled={
                              controlsDisabled ||
                              !run.pending!.options.includes(option) ||
                              (requiredOptions.includes(option) &&
                                !comment.trim())
                            }
                            onClick={() => void resume(option)}
                          >
                            {submitting === option ? (
                              <>
                                <span className="spinner" />
                                提交中…
                              </>
                            ) : (
                              <>
                                {option === "reject" ? (
                                  <Icon name="close" size={16} />
                                ) : (
                                  <Icon name="check" size={16} />
                                )}
                                {run.pending!.option_labels?.[option] ??
                                  (option === "approve"
                                    ? release
                                      ? "确认上线"
                                      : "确认"
                                    : option === "reject"
                                      ? "驳回"
                                      : option === "submit"
                                        ? "提交"
                                        : option)}
                              </>
                            )}
                          </button>
                        ))}
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
              {run.status === "done" && !hasReport && (
                <section
                  className="panel result-card"
                  aria-labelledby="result-title"
                >
                  <span
                    className={`result-icon ${etlResult && etlFailed(etlResult) ? "failed" : ""}`}
                  >
                    <Icon
                      name={
                        etlResult && etlFailed(etlResult) ? "alert" : "check"
                      }
                      size={20}
                    />
                  </span>
                  <h2 id="result-title">运行结果</h2>
                  {etlResult ? (
                    <p>
                      {etlResult.release?.status === "deployed"
                        ? `已上线到 prod（${etlResult.release.catalog}.${etlResult.release.schema}）。`
                        : etlResult.release?.status === "failed"
                          ? "审批已通过，但上线 prod 失败，请查看主区域列出的报错。"
                          : !etlResult.checks.passed
                            ? "代码检查未通过，请查看并处理主区域列出的问题。"
                            : etlResult.verification?.status === "failed"
                              ? "代码已生成，但 Databricks dev 试跑未通过，请查看主区域列出的报错。"
                              : etlResult.verification?.status === "passed"
                                ? `已生成 ${Object.keys(etlResult.files).length} 个文件，并在 Databricks dev 目标上试跑通过。`
                                : `已生成 ${Object.keys(etlResult.files).length} 个文件，可查看代码或下载 ZIP。`}
                    </p>
                  ) : (
                    <ResultView result={run.result} />
                  )}
                  <SourceList />
                </section>
              )}
              {run.status === "error" && (
                <section className="panel failure-card">
                  <h2>运行失败</h2>
                  {run.failed && <p>{failureText(run.failed)}</p>}
                  <p className="message-content failure-detail">
                    {run.error || "应用未提供错误详情。"}
                  </p>
                  {run.actions?.rerun !== false && (
                    <Link
                      className="text-button"
                      href={
                        run.actions
                          ? `/runs/new?from=${encodeURIComponent(id)}`
                          : "/runs/new"
                      }
                    >
                      {run.actions ? "复制并重新发起" : "重新发起"}
                      <Icon name="arrow" size={14} />
                    </Link>
                  )}
                </section>
              )}
              {run.status === "cancelled" && (
                <section className="panel failure-card cancelled-card">
                  <h2>已取消</h2>
                  <p>
                    {run.cancelled_by
                      ? `${run.cancelled_by} 取消了这次运行`
                      : "这次运行已取消"}
                    {run.error ? `，原因：${run.error}` : ""}
                    。已经写进开发环境的表会保留。
                  </p>
                  {run.actions?.rerun && (
                    <Link
                      className="text-button"
                      href={`/runs/new?from=${encodeURIComponent(id)}`}
                    >
                      复制并重新发起
                      <Icon name="arrow" size={14} />
                    </Link>
                  )}
                </section>
              )}
              {!isFinished(run.status) && (
                <p className="detail-note">
                  <Icon name="refresh" size={14} />
                  运行期间每 2 秒自动更新
                </p>
              )}
            </aside>
          </div>
        )
      )}
    </CitationScope>
  );
}
