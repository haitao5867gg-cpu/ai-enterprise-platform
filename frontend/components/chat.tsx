"use client";

import Link from "next/link";
import { useEffect, useRef, useState, type FormEvent } from "react";
import type { ChatSettings, Input } from "../lib/apps";
import {
  getConversation,
  MAX_MESSAGE,
  openConversation,
  sendMessage,
  type Conversation,
  type ConversationDetail,
  type Turn,
} from "../lib/conversations";
import { caseFromRun, rateRun, type RunFeedback } from "../lib/evals";
import { useT } from "../lib/i18n/client";
import {
  defaultValues,
  inputPayload,
  validateFields,
  type FieldValues,
} from "../lib/input-fields";
import {
  ApiError,
  errorMessage,
  isRecord,
  readRun,
  requestJson,
  type Run,
} from "../lib/runs";
import { CitationScope, CitedMarkdown, SourceList } from "./citations";
import { InputFields } from "./input-fields";
import { AgentSteps } from "./run-flow";
import { useMay } from "./session";
import { ErrorNotice } from "./ui";

// A conversation with an app (docs/api/conversations.md): the use page's chat and the
// design tab's debug chat. Each message starts one run of the app; the platform brings
// the earlier turns along.

const POLL_MS = 1500;
const WAIT_POLL_MS = 5000;

type Props = {
  app: string;
  chat: ChatSettings | null;
  // The conversation to show; null: a new one, opened with the first message.
  conversationId: string | null;
  // A debug chat runs the draft and offers each answer as an eval case.
  draft?: boolean;
  // The app's start inputs other than message: chosen once, for the whole conversation.
  inputs?: Input[];
  datasources?: { datasource_id: string; name: string; kind: string }[];
  onOpened?: (conversation: Conversation) => void;
  onChanged?: (conversation: Conversation) => void;
  // Disabled sending, with why (for example: no draft to try).
  blocked?: string;
  // A turn was saved as an eval case (the app page then reads its eval set again).
  onCaseSaved?: () => void;
};

export function ChatThread({
  app,
  chat,
  conversationId,
  draft = false,
  inputs = [],
  datasources = [],
  onOpened,
  onChanged,
  blocked,
  onCaseSaved,
}: Props) {
  const t = useT();
  const [id, setId] = useState(conversationId);
  const [detail, setDetail] = useState<ConversationDetail | null>(null);
  const [text, setText] = useState("");
  const [sending, setSending] = useState(false);
  const [error, setError] = useState("");
  const [values, setValues] = useState<FieldValues>(() =>
    defaultValues(inputs),
  );
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  const [tick, setTick] = useState(0);
  const list = useRef<HTMLOListElement>(null);
  const changed = useRef(onChanged);
  useEffect(() => {
    changed.current = onChanged;
  }, [onChanged]);

  // Load the conversation, and keep reading it while its last turn is going on.
  useEffect(() => {
    if (!id) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    getConversation(id, controller.signal)
      .then((value) => {
        if (controller.signal.aborted) return;
        setDetail(value);
        changed.current?.(value);
        setError("");
        const last = value.turns.at(-1);
        if (value.busy)
          timer = setTimeout(
            () => setTick((n) => n + 1),
            last?.status === "waiting" ? WAIT_POLL_MS : POLL_MS,
          );
      })
      .catch((err) => {
        if (controller.signal.aborted) return;
        setError(
          err instanceof ApiError && err.status === 404
            ? t("没有找到这个会话，可能已经过期删除了。")
            : errorMessage(err),
        );
        timer = setTimeout(() => setTick((n) => n + 1), WAIT_POLL_MS);
      });
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [id, tick, t]);

  const turns = detail?.turns ?? [];
  const count = turns.length;
  const lastStatus = turns.at(-1)?.status;
  // The newest message in view, inside the list (the page itself does not move).
  useEffect(() => {
    const element = list.current;
    if (element) element.scrollTop = element.scrollHeight;
  }, [count, lastStatus]);

  const busy = Boolean(detail?.busy) || sending;
  const extra = inputs.filter((field) => field.key !== "message");
  const reason = blocked ? blocked : busy ? t("上一条还没处理完") : "";

  async function send(message: string) {
    const trimmed = message.trim();
    if (!trimmed || busy || blocked) return;
    if (!id && extra.length) {
      const problems = validateFields(extra, values, "input");
      if (problems.length) {
        setFieldErrors(
          Object.fromEntries(
            problems.map((issue) => [String(issue.loc.at(-1)), issue.msg]),
          ),
        );
        return;
      }
    }
    setSending(true);
    setError("");
    setFieldErrors({});
    try {
      let current = id;
      if (!current) {
        const opened = await openConversation({
          app,
          draft,
          input: inputPayload(extra, values),
        });
        current = opened.conversation_id;
        setId(current);
        onOpened?.(opened);
      }
      const started = await sendMessage(current, trimmed);
      setText("");
      // Shown at once; the next read replaces it.
      setDetail((now) => ({
        ...(now ?? emptyDetail(current!, app, draft)),
        busy: true,
        turns: [
          ...(now?.turns ?? []),
          {
            turn: started.turn,
            run_id: started.run_id,
            message: trimmed,
            reply: null,
            status: started.status,
            pending: null,
            sources: [],
            app_version: null,
            created_at: new Date().toISOString(),
            finished_at: null,
          },
        ],
      }));
      setTick((n) => n + 1);
    } catch (err) {
      if (err instanceof ApiError && err.status === 422 && err.issues.length)
        setFieldErrors(
          Object.fromEntries(
            err.issues.map((issue) => [String(issue.loc.at(-1)), issue.msg]),
          ),
        );
      setError(errorMessage(err));
    } finally {
      setSending(false);
    }
  }

  function submit(event: FormEvent) {
    event.preventDefault();
    void send(text);
  }

  return (
    <div className="chat-thread">
      <ol
        ref={list}
        className="chat-messages"
        aria-label={t("对话")}
        aria-live="polite"
      >
        {chat?.opening && (
          <li className="chat-row assistant">
            <div className="chat-bubble">
              <CitedMarkdown text={chat.opening} breaks />
            </div>
          </li>
        )}
        {turns.map((turn) => (
          <TurnView
            key={turn.run_id}
            app={app}
            turn={turn}
            draft={draft}
            onCaseSaved={onCaseSaved}
          />
        ))}
      </ol>
      {!count && !id && chat?.suggestions.length ? (
        <div
          className="chat-suggestions"
          role="group"
          aria-label={t("推荐问题")}
        >
          {chat.suggestions.map((question) => (
            <button
              key={question}
              type="button"
              className="button secondary"
              disabled={Boolean(reason)}
              onClick={() => void send(question)}
            >
              {question}
            </button>
          ))}
        </div>
      ) : null}
      {error && <ErrorNotice message={error} />}
      {!id && extra.length > 0 && (
        <details className="chat-inputs" open>
          <summary>{t("这次对话用的设置")}</summary>
          <p className="muted">{t("整个对话不变，要换就开新对话。")}</p>
          <InputFields
            fields={extra}
            values={values}
            onChange={setValues}
            errors={fieldErrors}
            disabled={sending}
            prefix={`chat-${app}`}
            datasources={datasources}
          />
        </details>
      )}
      <form className="chat-composer" onSubmit={submit}>
        <label className="sr-only" htmlFor={`chat-input-${app}-${draft}`}>
          {t("消息")}
        </label>
        <textarea
          id={`chat-input-${app}-${draft}`}
          rows={2}
          value={text}
          maxLength={MAX_MESSAGE}
          placeholder={reason || t("输入消息，Enter 发送，Shift + Enter 换行")}
          onChange={(event) => setText(event.target.value)}
          onKeyDown={(event) => {
            if (
              event.key === "Enter" &&
              !event.shiftKey &&
              !event.nativeEvent.isComposing
            ) {
              event.preventDefault();
              void send(text);
            }
          }}
        />
        <button
          className="button primary"
          disabled={!text.trim() || Boolean(reason)}
        >
          {sending ? t("正在发送…") : t("发送")}
        </button>
      </form>
    </div>
  );
}

function emptyDetail(
  id: string,
  app: string,
  draft: boolean,
): ConversationDetail {
  const now = new Date().toISOString();
  return {
    conversation_id: id,
    app,
    app_title: app,
    title: "",
    draft,
    input: {},
    created_by: null,
    created_at: now,
    updated_at: now,
    expires_at: null,
    turn_count: 0,
    busy: true,
    turns: [],
  };
}

function TurnView({
  app,
  turn,
  draft,
  onCaseSaved,
}: {
  app: string;
  turn: Turn;
  draft: boolean;
  onCaseSaved?: () => void;
}) {
  const t = useT();
  const runLink = `/runs/${encodeURIComponent(turn.run_id)}`;
  return (
    <>
      <li className="chat-row user">
        <div className="chat-bubble">{turn.message}</div>
      </li>
      <li className="chat-row assistant">
        <div className="chat-bubble">
          {turn.status === "running" ? (
            <p className="chat-typing" role="status">
              {t("正在回答…")}
            </p>
          ) : turn.status === "waiting" ? (
            <div className="chat-pending">
              <strong>{t("这一轮在等人处理")}</strong>
              {turn.pending?.prompt && <p>{turn.pending.prompt}</p>}
              <Link className="button secondary" href={runLink}>
                {t("去处理")}
              </Link>
            </div>
          ) : turn.status === "done" ? (
            turn.reply ? (
              <CitationScope sources={turn.sources}>
                <CitedMarkdown text={turn.reply} breaks />
                <SourceList />
              </CitationScope>
            ) : (
              <p className="muted">{t("这一轮没有回答。")}</p>
            )
          ) : (
            <p className="field-error">
              {turn.status === "cancelled"
                ? t("这一轮已取消，没有回答。")
                : t("这一轮出错了，没有回答。")}{" "}
              <Link href={runLink}>{t("看运行")}</Link>
            </p>
          )}
          {(turn.status === "done" || turn.status === "error") && (
            <TurnActions
              app={app}
              turn={turn}
              draft={draft}
              onCaseSaved={onCaseSaved}
            />
          )}
        </div>
      </li>
    </>
  );
}

// Under an answer: what the agent did, right or wrong, and (for builders) saving the turn
// as an eval case with the history it had (docs/api/evals.md, "从运行存用例").
function TurnActions({
  app,
  turn,
  draft,
  onCaseSaved,
}: {
  app: string;
  turn: Turn;
  draft: boolean;
  onCaseSaved?: () => void;
}) {
  const t = useT();
  const mayBuild = useMay("builder");
  const [run, setRun] = useState<Run | null>(null);
  const [open, setOpen] = useState(false);
  const [feedback, setFeedback] = useState<RunFeedback | null>(null);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState("");
  const [error, setError] = useState("");
  useEffect(() => {
    const controller = new AbortController();
    requestJson(`/runs/${encodeURIComponent(turn.run_id)}`, {
      signal: controller.signal,
    })
      .then((value) => {
        if (controller.signal.aborted) return;
        const found = readRun(value);
        setRun(found);
        setFeedback(found.feedback ?? null);
      })
      .catch(() => {
        // The answer is already shown; only the extras wait for the run.
      });
    return () => controller.abort();
  }, [turn.run_id, turn.status]);

  async function act(work: () => Promise<void>) {
    setBusy(true);
    setError("");
    setNote("");
    try {
      await work();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }
  const mine = feedback?.mine?.rating;
  const saved = feedback?.cases.find((item) => item.app === app);
  const result = run && isRecord(run.result) ? run.result : null;
  const offerCase = mayBuild && (draft || mine === "bad");
  return (
    <div className="chat-actions">
      <div className="chat-action-row">
        <button
          type="button"
          className="text-button"
          aria-expanded={open}
          onClick={() => setOpen((value) => !value)}
        >
          {open ? t("收起过程") : t("查看过程")}
        </button>
        {feedback !== null && (
          <span role="group" aria-label={t("评价")}>
            <button
              type="button"
              className={`text-button ${mine === "good" ? "selected" : ""}`}
              aria-pressed={mine === "good"}
              aria-label={t("回答得对")}
              disabled={busy}
              onClick={() =>
                void act(async () =>
                  setFeedback(await rateRun(turn.run_id, "good", null)),
                )
              }
            >
              👍
            </button>
            <button
              type="button"
              className={`text-button ${mine === "bad" ? "selected" : ""}`}
              aria-pressed={mine === "bad"}
              aria-label={t("回答得不对")}
              disabled={busy}
              onClick={() =>
                void act(async () =>
                  setFeedback(await rateRun(turn.run_id, "bad", null)),
                )
              }
            >
              👎
            </button>
          </span>
        )}
        {offerCase &&
          (saved ? (
            // Right after saving, the note below says it with where to change it.
            !note && (
              <span className="muted">
                {t("已存为评测用例 {id}", { id: saved.case_id })}
              </span>
            )
          ) : (
            <button
              type="button"
              className="text-button"
              disabled={busy}
              onClick={() =>
                void act(async () => {
                  const created = await caseFromRun(
                    app,
                    turn.run_id,
                    mine === "bad" || turn.status === "error" ? "bad" : "good",
                  );
                  setFeedback((now) =>
                    now
                      ? {
                          ...now,
                          cases: [...now.cases, { app, case_id: created.id }],
                        }
                      : now,
                  );
                  onCaseSaved?.();
                  setNote(
                    t("已存为评测用例 {id}，可以在「评测与发布」里改检查。", {
                      id: created.id,
                    }),
                  );
                })
              }
            >
              {t("存为评测用例")}
            </button>
          ))}
      </div>
      {note && (
        <p className="muted" role="status">
          {note}
        </p>
      )}
      {error && <p className="field-error">{error}</p>}
      {open && (
        <div className="chat-process">
          {!run ? (
            <p className="muted" role="status">
              {t("正在读取…")}
            </p>
          ) : (
            <>
              {result && result.grounded === false && (
                <p className="field-error">
                  {t(
                    "没有在已接入的资料和数据里找到依据，平台把回答换成了固定回复。模型原来的回答在运行页的流程视图里。",
                  )}
                </p>
              )}
              {result && Array.isArray(result.steps) ? (
                <AgentSteps output={result} />
              ) : (
                <p className="muted">{t("每一步做了什么在运行页里看。")}</p>
              )}
            </>
          )}
          <Link href={`/runs/${encodeURIComponent(turn.run_id)}`}>
            {t("在运行页打开")}
          </Link>
        </div>
      )}
    </div>
  );
}
