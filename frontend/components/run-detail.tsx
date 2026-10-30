"use client";

import Link from "next/link";
import { CitationScope, CitedMarkdown, SourceList } from "./citations";
import { useEffect, useRef, useState } from "react";
import {
  ApiError,
  answersChanged,
  cancelRun,
  messageTime,
  durationText,
  errorMessage,
  failureText,
  fieldErrors,
  formatTime,
  isFinished,
  isRecord,
  jsonObject,
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
import {
  etlFailed,
  getEtlPlan,
  getEtlRelease,
  getEtlResult,
  readEtlPlan,
} from "../lib/etl";
import { EtlPlanView } from "./etl-plan";
import { EtlReleaseView, EtlTrail } from "./etl-release";
import { GeneratedFiles } from "./generated-files";
import { RunFlow } from "./run-flow";
import { RunManual } from "./app-manual";
import { savedActor, saveActor } from "../lib/actor";
import {
  initialValues,
  inputPayload,
  questionErrors,
  validateFields,
  type FieldValues,
} from "../lib/input-fields";
import { ApprovalQuestions } from "./approval-questions";
import { InputFields } from "./input-fields";
import {
  EtlDevReview,
  EtlPlanReview,
  readDevReview,
  readPlanReview,
} from "./etl-review";
import { NoteList, NotesScope, type Note } from "./review-notes";
import { EtlResultView } from "./etl-result";
import { isV2Result, readAnswer } from "../lib/etl-v2";

// What a person typed is shown as typed; replies are Markdown with [n]
// citations. A reply that is one JSON object is folded away: LLM nodes with JSON
// output used to add them, and so did text nodes asked for JSON before they
// could be quiet (such as plan and codegen in older runs and apps).
function MessageContent({ role, content }: { role: string; content: string }) {
  if (role === "user") return <p className="message-content">{content}</p>;
  const data = jsonObject(content);
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

// The result's own texts (its top-level strings) are read as Markdown; the
// whole result, structured parts included, is in the developer view.
function ResultView({ result }: { result: unknown }) {
  if (result == null) return <p>应用已完成本次运行。</p>;
  if (typeof result === "string") return <CitedMarkdown text={result} />;
  const texts = isRecord(result)
    ? Object.entries(result).filter(
        (entry): entry is [string, string] =>
          typeof entry[1] === "string" && entry[1].trim() !== "",
      )
    : [];
  return (
    <>
      {texts.map(([key, text]) => (
        <div key={key} className="result-text">
          <CitedMarkdown text={text} />
        </div>
      ))}
      {texts.length === 0 && (
        <p>
          {isRecord(result) &&
          Object.values(result).every(
            (value) =>
              value == null || (typeof value === "string" && !value.trim()),
          )
            ? "应用已完成本次运行，没有产出结果。"
            : "应用已完成本次运行，结果在下面的开发者视图里。"}
        </p>
      )}
      <details className="developer-view">
        <summary>开发者视图</summary>
        <pre className="result-json">{JSON.stringify(result, null, 2)}</pre>
      </details>
    </>
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
  const [answersFor, setAnswersFor] = useState("");
  const [notes, setNotes] = useState<Note[]>([]);
  const [comment, setComment] = useState("");
  // On the server there is no storage and "" comes back; the field only renders after loading.
  const [actor, setActor] = useState(savedActor);
  const [actorError, setActorError] = useState("");
  const actorInput = useRef<HTMLInputElement>(null);
  const [submitting, setSubmitting] = useState<Decision | null>(null);
  const [rechecking, setRechecking] = useState(false);
  const [revision, setRevision] = useState(0);
  // Whether the run process is open, once someone opened or closed it (the
  // page is keyed by the run, so another run starts afresh).
  const [historyOpen, setHistoryOpen] = useState<boolean | null>(null);
  const historyRef = useRef<HTMLDetailsElement>(null);
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
        // 运行过程 does not fold away under someone reading in it.
        const history = historyRef.current;
        const active = document.activeElement;
        if (
          history?.open &&
          active &&
          history.contains(active) &&
          active !== history.querySelector("summary")
        )
          setHistoryOpen((open) => open ?? true);
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
    // An approval's questions have no defaults: each one is answered.
    const asking = run.pending.kind === "approval" && fields.length > 0;
    const issues =
      run.pending.kind === "form"
        ? Object.fromEntries(
            validateFields(fields, submittedValues, "values").map((issue) => [
              issue.loc[2],
              issue.msg,
            ]),
          )
        : asking
          ? questionErrors(fields, values)
          : {};
    setValueErrors(issues);
    // The record says who decided: the page asks for a name (the API does not).
    const nameless = !actor.trim();
    if (nameless) {
      setActorError("先填你的名字再提交，记录里会写是谁处理的。");
      actorInput.current?.focus();
    }
    if (nameless || Object.keys(issues).length) return;
    // What the person saw: a step whose content changed since is refused (409).
    const details = run.pending.details;
    const seen =
      isRecord(details) && typeof details.seen === "string"
        ? { seen: details.seen }
        : {};
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
              : {
                  comment: comment.trim(),
                  ...(notes.length
                    ? { notes: notes.map(({ item, text }) => ({ item, text })) }
                    : {}),
                  ...(asking
                    ? {
                        values: Object.fromEntries(
                          fields.map((field) => [field.key, values[field.key]]),
                        ),
                      }
                    : {}),
                }),
            ...(actor.trim() ? { actor: actor.trim() } : {}),
            ...seen,
          }),
        }),
      );
      if (controller.signal.aborted) return;
      if (receipt.run_id !== id)
        throw new ApiError("服务返回的运行编号不匹配，请刷新核对。");
      setComment("");
      setValues({});
      setValueErrors({});
      setNotes([]);
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
        // The run is read again below; the answers and the remark stay for another look.
        setMutationError(
          err instanceof ApiError &&
            err.status === 409 &&
            err.message.includes("内容已经更新")
            ? "内容已经更新：已重新载入，请看过最新的内容再处理。"
            : errorMessage(err),
        );
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
  // A result of the ETL template's second version has its own page (tabs, answer first).
  const v2Result =
    run?.status === "done" && isV2Result(run.result) ? run.result : null;
  const failedResult =
    (etlResult && etlFailed(etlResult)) ||
    (v2Result && run?.outcome === "stopped");
  const hasReport =
    !etlResult &&
    !v2Result &&
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
  const questions =
    run?.pending?.kind === "approval" ? (run.pending.fields ?? []) : [];
  // Answers belong to one step and round: when the run moves on (say, someone decided in
  // another browser) they are cleared, while a refresh of the same step keeps them.
  const pendingStep =
    run?.status === "waiting" && run.pending
      ? [
          run.pending.kind,
          String(humanDetails?.node ?? run.step?.node ?? ""),
          String(humanDetails?.round ?? ""),
        ].join(":")
      : "";
  if (pendingStep !== answersFor) {
    setAnswersFor(pendingStep);
    setValues({});
    setValueErrors({});
    setNotes([]);
  }
  // Questions the step was given but could not ask (badly written, past the 20th).
  const dropped =
    run?.status === "waiting" && Array.isArray(humanDetails?.dropped_fields)
      ? humanDetails.dropped_fields.filter(isRecord)
      : [];
  // A step whose view the page knows shows what it was given, and takes notes on its items.
  const view =
    run?.status === "waiting" && run.pending?.kind === "approval"
      ? humanDetails?.view
      : null;
  const planReview =
    view === "etl_plan_review" ? readPlanReview(humanDetails?.data) : null;
  const devReview =
    view === "etl_dev_review" ? readDevReview(humanDetails?.data) : null;
  const viewShown = Boolean(planReview || devReview);
  const round =
    typeof humanDetails?.round === "number" ? humanDetails.round : null;
  const unanswered = questions.filter(
    (question) =>
      values[question.key] === undefined || values[question.key] === "",
  ).length;
  const remarked = Boolean(comment.trim()) || (viewShown && notes.length > 0);
  const answered = answersChanged(questions, values);
  // A changed answer or a remark turns the decision into the step's changed_branch: that
  // is the button then, and the options it would turn are not offered.
  const turnTo =
    run?.pending?.kind === "approval" &&
    run.pending.changed_branch &&
    run.pending.options.includes(run.pending.changed_branch)
      ? run.pending.changed_branch
      : null;
  const turning = Boolean(turnTo && (answered || remarked));
  const optionLabel = (option: string) =>
    run?.pending?.option_labels?.[option] ??
    (option === "approve"
      ? release
        ? "确认上线"
        : "确认"
      : option === "reject"
        ? "驳回"
        : option === "submit"
          ? "提交"
          : option);
  const humanShow =
    run?.status === "waiting" &&
    typeof humanDetails?.show === "string" &&
    humanDetails.show
      ? humanDetails.show
      : "";
  // A flow app's ETL plan, as its result carries it.
  const resultPlan =
    run?.status === "done" && !etlResult && !v2Result && isRecord(run.result)
      ? readEtlPlan(run.result.plan)
      : null;
  // How the run went is open until there is something else to read.
  const historyDefault = !(
    humanShow ||
    viewShown ||
    v2Result ||
    plan ||
    release ||
    etlResult ||
    (run?.status === "done" && run.result != null)
  );
  const showHistory = historyOpen ?? historyDefault;
  const stateText =
    run?.status === "running"
      ? "应用正在运行，消息将自动更新"
      : run?.status === "waiting"
        ? "应用已暂停，等待人工确认"
        : run?.status === "done"
          ? "本次运行已结束"
          : run?.status === "cancelled"
            ? "本次运行已取消"
            : "本次运行失败";
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
            {run && <StatusBadge run={run} />}
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
              {planReview && (
                <NotesScope
                  notes={notes}
                  onChange={setNotes}
                  disabled={controlsDisabled}
                >
                  <EtlPlanReview
                    review={planReview}
                    title={run.step?.title ?? "确认方案"}
                    round={round}
                    questions={questions}
                    values={values}
                    onChange={(next) => {
                      setValues(next);
                      setValueErrors({});
                    }}
                    errors={valueErrors}
                    disabled={controlsDisabled}
                  />
                </NotesScope>
              )}
              {devReview && (
                <NotesScope
                  notes={notes}
                  onChange={setNotes}
                  disabled={controlsDisabled}
                >
                  <EtlDevReview
                    review={devReview}
                    title={run.step?.title ?? "审核试算结果"}
                    round={round}
                    runId={id}
                  />
                </NotesScope>
              )}
              {v2Result && (
                <EtlResultView
                  run={run}
                  result={v2Result}
                  traceUrl={
                    langfuseTraceBaseUrl && run.trace_id
                      ? `${langfuseTraceBaseUrl}${encodeURIComponent(run.trace_id)}`
                      : null
                  }
                />
              )}
              {humanShow && !viewShown && (
                <section
                  className="panel human-show-panel"
                  aria-labelledby="human-show-title"
                >
                  <h2 id="human-show-title">
                    {run.step?.title ?? "待确认的内容"}
                  </h2>
                  <div className="human-show">
                    <CitedMarkdown text={humanShow} breaks />
                  </div>
                  {/* On a narrow screen the decision comes after the whole page. */}
                  <a className="jump-to-decision" href="#approval">
                    {run.pending?.kind === "form" ? "去填写" : "去确认"}
                    <Icon name="arrow" size={14} />
                  </a>
                </section>
              )}
              {plan && <EtlPlanView plan={plan} />}
              {release && <EtlReleaseView request={release} />}
              {etlResult && <EtlTrail result={etlResult} />}
              {etlResult && <GeneratedFiles result={etlResult} runId={id} />}
              <RunManual runId={id} />
              {/* A delivered or stopped ETL run keeps it in its developer view. */}
              {!v2Result && (
                <RunFlow
                  runId={id}
                  version={`${run.status}:${run.messages.length}`}
                  live={run.status === "running"}
                />
              )}
              {resultPlan && <EtlPlanView plan={resultPlan} asResult />}
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
                ref={historyRef}
                className="conversation-history"
                open={showHistory}
                // A citation tag may open it to show a source inside.
                onToggle={(event) => {
                  if (event.currentTarget.open !== showHistory)
                    setHistoryOpen(event.currentTarget.open);
                }}
              >
                <summary
                  onClick={(event) => {
                    // A person's choice holds when a poll changes the default.
                    event.preventDefault();
                    setHistoryOpen(!showHistory);
                  }}
                >
                  <h2>
                    <Icon name="runs" />
                    运行过程
                    <span className="muted">
                      {" "}
                      · {run.messages.length} 条消息
                    </span>
                  </h2>
                </summary>
                <section
                  className="panel conversation-panel"
                  aria-label="运行过程"
                >
                  <ol className="messages" aria-label="消息记录">
                    {run.messages.map((message, index) => {
                      // A person is named by who they said they are; a step
                      // of the app by its title.
                      const fromPerson = message.role === "user";
                      // Without a name, a person is not taken for the reader.
                      // A request from before messages named their step is
                      // the requester's.
                      const name = fromPerson
                        ? message.actor?.trim() ||
                          (!message.node && index === 0
                            ? run.created_by?.trim()
                            : "") ||
                          "未署名"
                        : message.node_title || run.app_title || run.app;
                      const step = fromPerson ? message.node_title : null;
                      return (
                        <li
                          className={`message ${fromPerson ? "user-message" : ""}`}
                          key={index}
                        >
                          <span className="message-avatar" aria-hidden="true">
                            {fromPerson ? (
                              [...name][0]
                            ) : (
                              <Icon name="spark" size={18} />
                            )}
                          </span>
                          <div className="message-body">
                            <div className="message-meta">
                              <strong>{name}</strong>
                              {step && (
                                <span className="message-step">{step}</span>
                              )}
                              {message.at && (
                                <time
                                  dateTime={message.at}
                                  title={formatTime(message.at)}
                                >
                                  {messageTime(
                                    message.at,
                                    run.messages[index - 1]?.at,
                                  )}
                                </time>
                              )}
                              <span className="message-index">
                                {String(index + 1).padStart(2, "0")}
                              </span>
                            </div>
                            <MessageContent
                              role={message.role}
                              content={message.content}
                            />
                          </div>
                        </li>
                      );
                    })}
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
                  <div className="conversation-state" aria-hidden="true">
                    {run.status === "running" ? (
                      <span className="spinner" />
                    ) : run.status === "waiting" ? (
                      <span className="dot amber" />
                    ) : run.status === "done" ? (
                      <Icon name="check" size={16} />
                    ) : run.status === "cancelled" ? (
                      <Icon name="close" size={16} />
                    ) : (
                      <Icon name="alert" size={16} />
                    )}
                    {stateText}
                  </div>
                </section>
              </details>
              {/* Heard even while 运行过程 is closed. */}
              <p className="sr-only" role="status">
                {stateText}
              </p>
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
                        ? "试运行（未发布版本）"
                        : run.app_version
                          ? `v${run.app_version}`
                          : "—"}
                    </dd>
                  </div>
                  <div>
                    <dt>当前状态</dt>
                    <dd>
                      <StatusBadge run={run} />
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
                  id="approval"
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
                        你的名字
                        <span className="muted">（必填，浏览器会记住）</span>
                      </label>
                      <input
                        id="approval-actor"
                        ref={actorInput}
                        autoComplete="name"
                        value={actor}
                        maxLength={80}
                        aria-required
                        aria-invalid={Boolean(actorError)}
                        aria-describedby={
                          actorError ? "approval-actor-error" : undefined
                        }
                        onChange={(e) => {
                          setActor(e.target.value);
                          setActorError("");
                          saveActor(e.target.value.trim());
                        }}
                        disabled={controlsDisabled}
                      />
                      {actorError && (
                        <p
                          className="field-error"
                          id="approval-actor-error"
                          role="alert"
                        >
                          {actorError}
                        </p>
                      )}
                      {dropped.length > 0 && (
                        <div className="dropped-questions" role="note">
                          <p>没有问的题</p>
                          <ul>
                            {dropped.map((item, i) => (
                              <li key={i}>
                                {typeof item.index === "number"
                                  ? `第 ${item.index + 1} 题${
                                      typeof item.key === "string"
                                        ? `（${item.key}）`
                                        : ""
                                    }：`
                                  : ""}
                                {String(item.problem ?? "")}
                              </li>
                            ))}
                          </ul>
                        </div>
                      )}
                      {planReview && questions.length > 0 && (
                        <p
                          className={
                            unanswered && Object.keys(valueErrors).length
                              ? "field-error"
                              : "field-hint"
                          }
                          role={
                            unanswered && Object.keys(valueErrors).length
                              ? "alert"
                              : undefined
                          }
                        >
                          {unanswered
                            ? `还有 ${unanswered} 题没答，`
                            : `${questions.length} 题都答了，`}
                          <a href="#questions">去看问题</a>
                        </p>
                      )}
                      {viewShown && (
                        <NoteList
                          notes={notes}
                          onRemove={(item) =>
                            setNotes(notes.filter((note) => note.item !== item))
                          }
                          disabled={controlsDisabled}
                        />
                      )}
                      {!planReview && questions.length > 0 && (
                        <ApprovalQuestions
                          key={`${String(humanDetails?.node)}-${String(humanDetails?.round)}`}
                          questions={questions}
                          values={values}
                          onChange={(next) => {
                            setValues(next);
                            setValueErrors({});
                          }}
                          errors={valueErrors}
                          disabled={controlsDisabled}
                        />
                      )}
                      {run.pending.kind !== "form" && (
                        <>
                          <label htmlFor="rejection-comment">
                            {humanDetails &&
                            !run.pending.options.includes("reject")
                              ? "意见"
                              : "修改意见"}
                            <span className="muted">
                              {humanDetails
                                ? requiredOptions.length
                                  ? `（选${requiredOptions
                                      .filter((option) =>
                                        run.pending!.options.includes(option),
                                      )
                                      .map(
                                        (option) =>
                                          `「${optionLabel(option)}」`,
                                      )
                                      .join("")}时必填${
                                      questions.length && viewShown
                                        ? "，改了答案、挂了意见也算"
                                        : questions.length
                                          ? "，改了答案也算"
                                          : viewShown
                                            ? "，挂了意见也算"
                                            : ""
                                    }）`
                                  : "（选填）"
                                : commentRequired
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
                      {turning && turnTo && (
                        <p className="turn-hint" role="status">
                          你改了建议的答案或写了意见，会按「
                          {optionLabel(turnTo)}
                          」处理。要按原样继续，把答案改回建议、清空意见。
                        </p>
                      )}
                      <div className="approval-actions">
                        {(run.pending.option_labels ||
                        run.pending.kind === "form"
                          ? run.pending.options
                          : ["approve", "reject"]
                        )
                          .filter(
                            (option) =>
                              !turning ||
                              option === turnTo ||
                              requiredOptions.includes(option),
                          )
                          .map((option) => {
                            const stops =
                              option === "reject" || option === "stop";
                            // A step's own options: the one that needs a remark is
                            // not the main one, unless an answer or remark made it so.
                            const tone = stops
                              ? "reject"
                              : turning
                                ? option === turnTo
                                  ? "primary"
                                  : "secondary"
                                : humanDetails &&
                                    requiredOptions.includes(option)
                                  ? "secondary"
                                  : "primary";
                            return (
                              <button
                                key={option}
                                type="button"
                                className={`button ${tone}`}
                                disabled={
                                  controlsDisabled ||
                                  !run.pending!.options.includes(option) ||
                                  (requiredOptions.includes(option) &&
                                    !remarked &&
                                    !answered)
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
                                    {stops ? (
                                      <Icon name="close" size={16} />
                                    ) : (
                                      <Icon name="check" size={16} />
                                    )}
                                    {optionLabel(option)}
                                  </>
                                )}
                              </button>
                            );
                          })}
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
                    className={`result-icon ${failedResult ? "failed" : ""}`}
                  >
                    <Icon name={failedResult ? "alert" : "check"} size={20} />
                  </span>
                  <h2 id="result-title">运行结果</h2>
                  {v2Result ? (
                    <p>
                      {run.outcome === "stopped"
                        ? `已终止：${typeof v2Result.reason === "string" && v2Result.reason ? v2Result.reason : "没有写明原因"}`
                        : `已交付。${readAnswer(v2Result.answer)?.conclusion ?? ""}`}
                    </p>
                  ) : etlResult ? (
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
