"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";
import { listNotifications, UNREAD_EVENT } from "../lib/notifications";
import { useT } from "../lib/i18n/client";
import { Icon } from "./icons";

// The unread count beside the service status; refreshed every 30 seconds, when the
// page changes and when notices are marked read (docs/api/notifications.md).
export function NotificationBell() {
  const t = useT();
  const pathname = usePathname();
  const [unread, setUnread] = useState<number | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    async function check() {
      try {
        const page = await listNotifications({ limit: 1 }, controller.signal);
        if (!controller.signal.aborted) setUnread(page.unread);
      } catch {
        // The bell stays as it was; the service status says when the API is down.
      }
    }
    void check();
    const timer = setInterval(() => void check(), 30_000);
    const refresh = (event: Event) => {
      const count = (event as CustomEvent<number>).detail;
      if (typeof count === "number") setUnread(count);
      else void check();
    };
    window.addEventListener(UNREAD_EVENT, refresh);
    return () => {
      controller.abort();
      clearInterval(timer);
      window.removeEventListener(UNREAD_EVENT, refresh);
    };
  }, [pathname]);
  const count = unread ?? 0;
  return (
    <Link
      href="/notifications"
      className={`notification-bell ${pathname === "/notifications" ? "active" : ""}`}
      aria-label={count ? t("通知，{count} 条未读", { count }) : t("通知")}
    >
      <Icon name="bell" />
      {count > 0 && (
        <span className="notification-count" aria-hidden>
          {count > 99 ? "99+" : count}
        </span>
      )}
    </Link>
  );
}
