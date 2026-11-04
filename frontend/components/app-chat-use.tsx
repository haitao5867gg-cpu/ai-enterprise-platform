"use client";

import { useEffect, useState } from "react";
import type { AppDetail, ChatSettings } from "../lib/apps";
import {
  deleteConversation,
  listConversations,
  renameConversation,
  type Conversation,
} from "../lib/conversations";
import { useT } from "../lib/i18n/client";
import { errorMessage } from "../lib/runs";
import { AssetTime } from "./asset-ui";
import { ChatThread } from "./chat";
import { ErrorNotice } from "./ui";

// The use page of an app people talk to (docs/api/agent-apps.md, "页面"): their
// conversations on the left, the one open on the right; a new one starts with its first
// message.
export function AppChatUse({
  app,
  detail,
  chat,
  initial = null,
}: {
  app: string;
  detail: AppDetail;
  chat: ChatSettings | null;
  // The conversation to open first, from the address; null: a new one.
  initial?: string | null;
}) {
  const t = useT();
  const [items, setItems] = useState<Conversation[] | null>(null);
  const [error, setError] = useState("");
  // Which conversation is open; null: a new one. `slot` changes only when the person
  // picks another, so the thread is not remounted when its first message opens it.
  const [selected, setSelected] = useState<string | null>(initial);
  const [slot, setSlot] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    listConversations(app, false, controller.signal)
      .then((value) => {
        setItems(value);
        setError("");
      })
      .catch((err) => {
        if (!controller.signal.aborted) setError(errorMessage(err));
      });
    return () => controller.abort();
  }, [app]);
  function pick(id: string | null) {
    setSelected(id);
    setSlot((n) => n + 1);
  }
  function upsert(conversation: Conversation) {
    setItems((now) => [
      conversation,
      ...(now ?? []).filter(
        (item) => item.conversation_id !== conversation.conversation_id,
      ),
    ]);
  }
  const inputs = (detail.inputs ?? []).filter((item) => item.key !== "message");
  return (
    <div className="chat-use">
      <aside className="panel chat-list" aria-labelledby="chat-list-title">
        <div className="asset-section-heading">
          <h2 id="chat-list-title">{t("我的对话")}</h2>
          <button
            type="button"
            className="button primary"
            onClick={() => pick(null)}
          >
            {t("新对话")}
          </button>
        </div>
        {error && <ErrorNotice message={error} />}
        {items === null && !error && (
          <p className="muted" role="status">
            {t("正在读取…")}
          </p>
        )}
        {items && !items.length && (
          <p className="muted">{t("还没有对话，在右边发一条消息就开始。")}</p>
        )}
        {items && items.length > 0 && (
          <ul aria-label={t("我的对话")}>
            {items.map((item) => (
              <li
                key={item.conversation_id}
                className={
                  item.conversation_id === selected ? "selected" : undefined
                }
              >
                <button
                  type="button"
                  className="chat-list-item"
                  aria-current={
                    item.conversation_id === selected ? "true" : undefined
                  }
                  onClick={() => pick(item.conversation_id)}
                >
                  <strong>{item.title}</strong>
                  <small className="muted">
                    {t("{n} 轮", { n: item.turn_count })} ·{" "}
                    <AssetTime value={item.updated_at} />
                  </small>
                </button>
                <span className="chat-list-actions">
                  <button
                    type="button"
                    className="text-button"
                    aria-label={t("给「{title}」改名", { title: item.title })}
                    onClick={async () => {
                      const title = window.prompt(t("对话的新名称"), item.title);
                      if (!title?.trim() || title.trim() === item.title) return;
                      try {
                        upsert(
                          await renameConversation(
                            item.conversation_id,
                            title.trim().slice(0, 80),
                          ),
                        );
                      } catch (err) {
                        setError(errorMessage(err));
                      }
                    }}
                  >
                    {t("改名")}
                  </button>
                  <button
                    type="button"
                    className="text-button danger-text"
                    aria-label={t("删除「{title}」", { title: item.title })}
                    onClick={async () => {
                      if (
                        !window.confirm(
                          t("确定删除这个对话吗？各轮的消息和回答都会清除。"),
                        )
                      )
                        return;
                      try {
                        await deleteConversation(item.conversation_id);
                        setItems((now) =>
                          (now ?? []).filter(
                            (other) =>
                              other.conversation_id !== item.conversation_id,
                          ),
                        );
                        if (selected === item.conversation_id) pick(null);
                      } catch (err) {
                        setError(errorMessage(err));
                      }
                    }}
                  >
                    {t("删除")}
                  </button>
                </span>
              </li>
            ))}
          </ul>
        )}
        {items && items[0]?.expires_at && (
          <p className="muted chat-retention">
            {t("对话在最后一条消息后保留一段时间，到期自动删除。")}
          </p>
        )}
      </aside>
      <section className="panel chat-main" aria-label={t("对话")}>
        <ChatThread
          key={slot}
          app={app}
          chat={chat}
          conversationId={selected}
          inputs={inputs}
          datasources={detail.options.datasources}
          onOpened={(conversation) => {
            setSelected(conversation.conversation_id);
            upsert(conversation);
          }}
          onChanged={upsert}
        />
      </section>
    </div>
  );
}
