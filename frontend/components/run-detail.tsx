"use client";

import Link from "next/link";
import { CitationScope, CitedMarkdown, SourceList } from "./citations";
import { useEffect, useRef, useState } from "react";
import {
  ApiError,
  errorMessage,
  fieldErrors,
  isRecord,
  readReceipt,
  readRun,
  requestJson,
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
import { savedActor, saveActor } from "../lib/actor";
import {
  initialValues,
  inputPayload,
  validateFields,
  type FieldValues,
} from "../lib/input-fields";
import { InputFields } from "./input-fields";

function ResultView({ result }: { result: unknown }) {
  if (result === null) return <p>应用已完成本次运行。</p>;
  if (typeof result === "string") return <CitedMarkdown text={result} />;
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
      <CitedMarkdown text={text} />
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
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [mutationError, setMutationError] = useState("");
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
            <div className="run-main">
              {plan && <EtlPlanView plan={plan} />}
              {release && <EtlReleaseView request={release} />}
              {etlResult && <EtlTrail result={etlResult} />}
              {etlResult && <GeneratedFiles result={etlResult} runId={id} />}
              <RunFlow
                runId={id}
                version={`${run.status}:${run.messages.length}`}
                live={run.status === "running"}
              />
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
                        {run.app}
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
                    </dd>
                  </div>
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
              {run.status === "done" && (
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
    </CitationScope>
  );
}
