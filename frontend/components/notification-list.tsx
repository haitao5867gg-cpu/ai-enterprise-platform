"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import {
  eventLabels,
  listNotifications,
  markRead,
  UNREAD_EVENT,
  type AppNotification,
} from "../lib/notifications";
import { errorMessage } from "../lib/runs";
import { AssetHeading, AssetLoading } from "./asset-ui";
import { EmptyState, ErrorNotice } from "./ui";
import { useLocale, useT } from "../lib/i18n/client";
import { htmlLang } from "../lib/i18n/locale";

const time = (value: string, lang: string) =>
  new Date(value).toLocaleString(lang, {
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
const tell = (unread: number) =>
  window.dispatchEvent(new CustomEvent(UNREAD_EVENT, { detail: unread }));

export function NotificationList() {
  const t = useT();
  const lang = htmlLang(useLocale().locale);
  const router = useRouter();
  const [items, setItems] = useState<AppNotification[] | null>(null);
  const [unread, setUnread] = useState(0);
  const [onlyUnread, setOnlyUnread] = useState(false);
  const [next, setNext] = useState<number | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    listNotifications({ unread: onlyUnread }, controller.signal)
      .then((page) => {
        setItems(page.items);
        setUnread(page.unread);
        setNext(page.next);
        setError("");
      })
      .catch((err) => {
        if (!controller.signal.aborted) setError(errorMessage(err));
      });
    return () => controller.abort();
  }, [onlyUnread, revision]);

  async function more() {
    if (next === null || loadingMore) return;
    setLoadingMore(true);
    try {
      const page = await listNotifications({
        unread: onlyUnread,
        before: next,
      });
      setItems((old) => [...(old ?? []), ...page.items]);
      setNext(page.next);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setLoadingMore(false);
    }
  }
  async function read(ids?: number[]) {
    try {
      const left = (await markRead(ids)).unread;
      setUnread(left);
      setItems(
        (old) =>
          old?.map((item) =>
            !ids || ids.includes(item.id) ? { ...item, read: true } : item,
          ) ?? null,
      );
      tell(left);
    } catch (err) {
      setError(errorMessage(err));
    }
  }

  return (
    <div className="asset-page">
      <AssetHeading title={t("通知")}>
        {t(
          "运行等你确认、运行完成或失败时，平台会在这里通知你；配了邮件或群机器人的，同时发到那边。",
        )}
      </AssetHeading>
      <div className="asset-actions notification-actions">
        <label className="notification-filter">
          <input
            type="checkbox"
            checked={onlyUnread}
            onChange={(event) => setOnlyUnread(event.target.checked)}
          />
          {t("只看未读")}
        </label>
        <button
          type="button"
          className="button"
          disabled={!unread}
          onClick={() => void read()}
        >
          {t("全部标为已读")}
        </button>
      </div>
      {error && (
        <ErrorNotice
          message={error}
          onRetry={() => setRevision((n) => n + 1)}
        />
      )}
      {!items && !error && <AssetLoading />}
      {items && !items.length && (
        <EmptyState title={t(onlyUnread ? "没有未读通知" : "还没有通知")}>
          {t("运行等你确认、完成或失败时，通知会出现在这里。")}
        </EmptyState>
      )}
      {items && items.length > 0 && (
        <section className="panel asset-section">
          <ul className="notification-items" aria-label={t("通知")}>
            {items.map((item) => (
              <li key={item.id} className={item.read ? "" : "unread"}>
                <button
                  type="button"
                  className="notification-item"
                  onClick={async () => {
                    if (!item.read) await read([item.id]);
                    router.push(item.link);
                  }}
                >
                  <span className={`notification-event ${item.event}`}>
                    {t(eventLabels[item.event])}
                  </span>
                  <span className="notification-text">
                    <strong>{item.title}</strong>
                    {item.body && (
                      <span className="notification-body">{item.body}</span>
                    )}
                  </span>
                  <time className="muted" dateTime={item.created_at}>
                    {time(item.created_at, lang)}
                  </time>
                  {!item.read && <span className="sr-only">{t("（未读）")}</span>}
                </button>
              </li>
            ))}
          </ul>
          {next !== null && (
            <button
              type="button"
              className="button audit-more"
              disabled={loadingMore}
              onClick={() => void more()}
            >
              {t(loadingMore ? "正在加载…" : "加载更早的")}
            </button>
          )}
        </section>
      )}
    </div>
  );
}
