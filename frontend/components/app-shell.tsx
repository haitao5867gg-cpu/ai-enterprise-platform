"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import {
  useEffect,
  useRef,
  useState,
  useSyncExternalStore,
  type ReactNode,
} from "react";
import {
  hasRole,
  logout,
  pageRole,
  roleLabels,
  type Role,
  type User,
} from "../lib/auth";
import { isRecord, requestJson } from "../lib/runs";
import { useT } from "../lib/i18n/client";
import { Icon } from "./icons";
import { LocaleSwitch } from "./locale-switch";
import { NotificationBell } from "./notification-bell";
import { PasswordDialog } from "./password-dialog";
import { SessionProvider, useSession } from "./session";
import { EmptyState } from "./ui";

function BackendHealth() {
  const t = useT();
  const [healthy, setHealthy] = useState<boolean | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    async function check() {
      try {
        const value = await requestJson("/readyz", {
          signal: controller.signal,
        });
        if (!controller.signal.aborted)
          setHealthy(isRecord(value) && value.status === "ok");
      } catch {
        if (!controller.signal.aborted) setHealthy(false);
      }
    }
    void check();
    const timer = setInterval(() => void check(), 30_000);
    return () => {
      controller.abort();
      clearInterval(timer);
    };
  }, []);
  // Quiet while the service is fine: a dot with its state for screen readers and
  // on hover. Words only when it is down.
  const label = t("服务状态：{state}", {
    state: t(healthy === null ? "检查中" : healthy ? "正常" : "不可用"),
  });
  return (
    <span
      className={`health${healthy === false ? " down" : ""}`}
      role="status"
      aria-label={label}
      title={label}
    >
      <span
        className={`dot ${healthy === null ? "" : healthy ? "green" : "amber"}`}
      />
      {healthy === false && <span aria-hidden>{t("服务不可用")}</span>}
    </span>
  );
}

type NavItem = {
  href: string;
  label: string;
  icon: Parameters<typeof Icon>[0]["name"];
  active: (pathname: string) => boolean;
};
type NavGroup = { key: string; label: string; items: NavItem[] };
const under = (prefix: string) => (pathname: string) =>
  pathname === prefix || pathname.startsWith(`${prefix}/`);
const item = (href: string, label: string, icon: NavItem["icon"]): NavItem => ({
  href,
  label,
  icon,
  active: under(href),
});
// Three groups: daily work, building the platform's assets, running the platform.
// Each person sees only the pages their roles allow (pageRole).
const NAV: NavGroup[] = [
  {
    key: "work",
    label: "工作",
    items: [
      {
        href: "/",
        label: "运行记录",
        icon: "runs",
        active: (p) =>
          p === "/" || (p.startsWith("/runs/") && p !== "/runs/new"),
      },
      item("/apps", "应用", "grid"),
    ],
  },
  {
    key: "build",
    label: "搭建",
    items: [
      item("/skills", "Skill", "spark"),
      item("/knowledge", "知识库", "book"),
      item("/datasources", "数据源与工具", "plug"),
      item("/projects", "项目", "folder"),
      item("/catalog", "资产目录", "box"),
    ],
  },
  {
    key: "admin",
    label: "管理",
    items: [
      item("/models", "模型", "chip"),
      item("/ops", "运营看板", "chart"),
      item("/users", "账号", "user"),
      item("/notification-channels", "通知渠道", "bell"),
      item("/audit", "操作记录", "log"),
    ],
  },
];

// Pages that exist only with sign-in on: accounts and the record of changes.
const accountsOnly = (pathname: string) =>
  under("/users")(pathname) || under("/audit")(pathname);

// Remembered per browser: a folded sidebar and folded nav groups. Read through
// useSyncExternalStore so the server render (nothing folded) and the browser agree.
const SIDEBAR_KEY = "aep.sidebar";
const GROUPS_KEY = "aep.nav-collapsed";
const memory = new Map<string, string>();
const listeners = new Set<() => void>();
function subscribe(listener: () => void) {
  listeners.add(listener);
  window.addEventListener("storage", listener);
  return () => {
    listeners.delete(listener);
    window.removeEventListener("storage", listener);
  };
}
function readPref(key: string): string {
  try {
    return window.localStorage.getItem(key) ?? memory.get(key) ?? "";
  } catch {
    return memory.get(key) ?? "";
  }
}
function writePref(key: string, value: string) {
  memory.set(key, value);
  try {
    window.localStorage.setItem(key, value);
  } catch {
    // Storage off: the choice lasts until the page is left.
  }
  listeners.forEach((listener) => listener());
}
function usePref(key: string): [string, (value: string) => void] {
  const value = useSyncExternalStore(
    subscribe,
    () => readPref(key),
    () => "",
  );
  return [value, (next) => writePref(key, next)];
}

function Account({ user }: { user: User }) {
  const t = useT();
  const [changing, setChanging] = useState(false);
  const [leaving, setLeaving] = useState(false);
  const menu = useRef<HTMLDetailsElement>(null);
  const dialog = useRef<HTMLDialogElement>(null);
  // The menu closes on a click elsewhere and on Escape.
  useEffect(() => {
    function close(event: Event) {
      const details = menu.current;
      if (!details?.open) return;
      if (event instanceof KeyboardEvent) {
        if (event.key !== "Escape") return;
        details.open = false;
        details.querySelector("summary")?.focus();
        return;
      }
      if (!details.contains(event.target as Node)) details.open = false;
    }
    document.addEventListener("click", close);
    document.addEventListener("keydown", close);
    return () => {
      document.removeEventListener("click", close);
      document.removeEventListener("keydown", close);
    };
  }, []);
  const roles = user.roles.map((role) => t(roleLabels[role])).join(t("、"));
  return (
    <>
      <details className="account" ref={menu}>
        <summary
          aria-label={t("账号菜单：{name}", { name: user.display_name })}
        >
          <span className="account-avatar" aria-hidden>
            {user.display_name.slice(0, 1)}
          </span>
          <span className="account-short">{user.display_name}</span>
        </summary>
        <div className="account-menu">
          <span className="account-name">
            <strong>{user.display_name}</strong>
            <span className="muted">{roles}</span>
          </span>
          <button
            type="button"
            className="menu-item"
            onClick={() => {
              if (menu.current) menu.current.open = false;
              setChanging(true);
            }}
          >
            {t("修改密码")}
          </button>
          <button
            type="button"
            className="menu-item"
            aria-disabled={leaving || undefined}
            onClick={async () => {
              if (leaving) return;
              setLeaving(true);
              try {
                await logout();
              } finally {
                window.location.replace("/login");
              }
            }}
          >
            {t(leaving ? "正在退出…" : "退出")}
          </button>
        </div>
      </details>
      {/* Outside the menu: a closed <details> hides what it holds. */}
      {changing && (
        <PasswordDialog ref={dialog} onClose={() => setChanging(false)} />
      )}
    </>
  );
}

function Shell({
  children,
  mockEnabled,
}: {
  children: ReactNode;
  mockEnabled: boolean;
}) {
  const t = useT();
  const pathname = usePathname();
  const router = useRouter();
  const { status, loaded } = useSession();
  const [sidebarPref, setSidebarPref] = usePref(SIDEBAR_KEY);
  const [groupsPref, setGroupsPref] = usePref(GROUPS_KEY);
  const narrow = sidebarPref === "narrow";
  const collapsed = groupsPref.split(",").filter(Boolean);
  const signIn = Boolean(status?.enabled);
  const user = signIn ? (status?.user ?? null) : null;
  const signedOut = signIn && !user;
  useEffect(() => {
    if (signedOut && pathname !== "/login")
      router.replace(`/login?next=${encodeURIComponent(pathname)}`);
  }, [signedOut, pathname, router]);
  // The sign-in page has no navigation.
  if (pathname === "/login") return <>{children}</>;
  const may = (role: Role) => !signIn || hasRole(user, role);
  const needed = pageRole(pathname);
  const groups = NAV.map((group) => ({
    ...group,
    items: group.items.filter(
      (entry) =>
        // Account and record pages exist only with sign-in on.
        (signIn || !["/users", "/audit"].includes(entry.href)) &&
        may(pageRole(entry.href)),
    ),
  })).filter((group) => group.items.length > 0);
  const toggleGroup = (key: string) => {
    const next = collapsed.includes(key)
      ? collapsed.filter((other) => other !== key)
      : [...collapsed, key];
    setGroupsPref(next.join(","));
  };
  const toggleSidebar = () => setSidebarPref(narrow ? "wide" : "narrow");
  return (
    <div className={`app-shell${narrow ? " narrow" : ""}`}>
      <a className="skip-link" href="#main-content">
        {t("跳到主要内容")}
      </a>
      <aside className="sidebar">
        <Link className="brand" href="/" aria-label={t("AEP 工作台首页")}>
          <span className="brand-mark">
            <Icon name="grid" size={21} />
          </span>
          <span className="brand-text">
            AEP<span className="brand-subtitle">{t("企业 AI 应用平台")}</span>
          </span>
        </Link>
        <nav aria-label={t("主导航")}>
          {groups.map((group) => {
            // The group of the page being shown never folds away.
            const open =
              narrow ||
              !collapsed.includes(group.key) ||
              group.items.some((entry) => entry.active(pathname));
            const listId = `nav-group-${group.key}`;
            return (
              <div className="nav-group" key={group.key}>
                {groups.length > 1 && (
                  <button
                    type="button"
                    className="nav-label"
                    aria-expanded={open}
                    aria-controls={listId}
                    onClick={() => toggleGroup(group.key)}
                  >
                    {t(group.label)}
                    <Icon name="chevron" size={12} />
                  </button>
                )}
                <div className="nav-items" id={listId} hidden={!open}>
                  {group.items.map((entry) => (
                    <Link
                      key={entry.href}
                      href={entry.href}
                      title={narrow ? t(entry.label) : undefined}
                      aria-current={entry.active(pathname) ? "page" : undefined}
                      className={`nav-link ${entry.active(pathname) ? "active" : ""}`}
                    >
                      <Icon name={entry.icon} />
                      <span className="nav-text">{t(entry.label)}</span>
                    </Link>
                  ))}
                </div>
              </div>
            );
          })}
        </nav>
      </aside>
      <div className="main-shell">
        <header className="topbar">
          <button
            type="button"
            className="icon-button sidebar-toggle"
            aria-label={t(narrow ? "展开侧栏" : "收起侧栏")}
            aria-pressed={narrow}
            onClick={toggleSidebar}
          >
            <Icon name="sidebar" />
          </button>
          <span className="topbar-tools">
            <BackendHealth />
            <LocaleSwitch />
            {loaded && !signedOut && <NotificationBell />}
            {loaded && !signedOut && pathname !== "/runs/new" && (
              <Link className="button primary topbar-new" href="/runs/new">
                <Icon name="plus" size={16} />
                <span className="topbar-new-text">{t("新建运行")}</span>
              </Link>
            )}
            {user && <Account user={user} />}
          </span>
        </header>
        {mockEnabled && (
          <div className="mock-banner">
            <span className="dot violet" />
            {t("本地模拟模式")}
            <span className="mock-description">
              {t("运行数据仅保存在当前开发进程中，不会调用真实工作流。")}
            </span>
          </div>
        )}
        <main id="main-content" className="main-content">
          {!loaded || signedOut ? (
            <div className="loading-state" role="status">
              <span className="spinner" />
              {t("正在加载…")}
            </div>
          ) : !signIn && accountsOnly(pathname) ? (
            <EmptyState title={t("没有打开登录")}>
              {t("账号和操作记录在打开登录（AEP_AUTH_ENABLED）后才有。")}
            </EmptyState>
          ) : may(needed) ? (
            children
          ) : (
            <EmptyState title={t("没有权限")}>
              {t(
                "这个页面需要{role}权限。需要的话请找管理员在「账号」里给你加上。",
                {
                  role: t(roleLabels[needed]),
                },
              )}
            </EmptyState>
          )}
        </main>
      </div>
    </div>
  );
}

export function AppShell({
  children,
  mockEnabled,
}: {
  children: ReactNode;
  mockEnabled: boolean;
}) {
  return (
    <SessionProvider>
      <Shell mockEnabled={mockEnabled}>{children}</Shell>
    </SessionProvider>
  );
}
