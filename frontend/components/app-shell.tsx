"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useRef, useState, type ReactNode } from "react";
import {
  hasRole,
  logout,
  pageRole,
  roleLabels,
  type Role,
  type User,
} from "../lib/auth";
import { isRecord, requestJson } from "../lib/runs";
import { Icon } from "./icons";
import { NotificationBell } from "./notification-bell";
import { PasswordDialog } from "./password-dialog";
import { SessionProvider, useSession } from "./session";
import { EmptyState } from "./ui";

function BackendHealth() {
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
  return (
    <span className="health" role="status">
      <span
        className={`dot ${healthy === null ? "" : healthy ? "green" : "amber"}`}
      />
      服务状态：{healthy === null ? "检查中" : healthy ? "正常" : "不可用"}
    </span>
  );
}

type NavItem = {
  href: string;
  label: string;
  icon: Parameters<typeof Icon>[0]["name"];
  active: (pathname: string) => boolean;
};
const under = (prefix: string) => (pathname: string) =>
  pathname === prefix || pathname.startsWith(`${prefix}/`);
const NAV: NavItem[] = [
  {
    href: "/",
    label: "运行记录",
    icon: "runs",
    active: (p) => p === "/" || (p.startsWith("/runs/") && p !== "/runs/new"),
  },
  {
    href: "/catalog",
    label: "资产目录",
    icon: "folder",
    active: under("/catalog"),
  },
  { href: "/apps", label: "应用", icon: "grid", active: under("/apps") },
  { href: "/skills", label: "Skill", icon: "spark", active: under("/skills") },
  {
    href: "/knowledge",
    label: "知识库",
    icon: "grid",
    active: under("/knowledge"),
  },
  {
    href: "/datasources",
    label: "数据源与工具",
    icon: "plug",
    active: under("/datasources"),
  },
  { href: "/models", label: "模型", icon: "chip", active: under("/models") },
  {
    href: "/projects",
    label: "项目",
    icon: "folder",
    active: under("/projects"),
  },
  { href: "/ops", label: "运营看板", icon: "chart", active: under("/ops") },
  { href: "/users", label: "账号", icon: "user", active: under("/users") },
  {
    href: "/notification-channels",
    label: "通知渠道",
    icon: "bell",
    active: under("/notification-channels"),
  },
  { href: "/audit", label: "操作记录", icon: "log", active: under("/audit") },
  {
    href: "/runs/new",
    label: "新建运行",
    icon: "plus",
    active: (p) => p === "/runs/new",
  },
];

// Pages that exist only with sign-in on: accounts and the record of changes.
const accountsOnly = (pathname: string) =>
  under("/users")(pathname) || under("/audit")(pathname);

function crumb(pathname: string) {
  if (pathname === "/") return "运行记录";
  if (pathname === "/runs/new") return "新建运行";
  if (pathname === "/runs/compare") return "对比运行";
  if (pathname === "/notifications") return "通知";
  if (pathname.startsWith("/use/")) return "使用应用";
  const item = NAV.find((nav) => nav.href !== "/" && nav.active(pathname));
  return item?.label ?? "运行详情";
}

function Account({ user }: { user: User }) {
  const [changing, setChanging] = useState(false);
  const [leaving, setLeaving] = useState(false);
  const dialog = useRef<HTMLDialogElement>(null);
  return (
    <div className="account">
      <span className="account-avatar" aria-hidden>
        {user.display_name.slice(0, 1)}
      </span>
      <span className="account-name">
        <strong>{user.display_name}</strong>
        <span className="muted">
          {user.roles.map((role) => roleLabels[role]).join("、")}
        </span>
      </span>
      <span className="account-actions">
        <button
          type="button"
          className="text-button"
          onClick={() => setChanging(true)}
        >
          修改密码
        </button>
        <button
          type="button"
          className="text-button"
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
          {leaving ? "正在退出…" : "退出"}
        </button>
      </span>
      {changing && (
        <PasswordDialog ref={dialog} onClose={() => setChanging(false)} />
      )}
    </div>
  );
}

function Shell({
  children,
  mockEnabled,
}: {
  children: ReactNode;
  mockEnabled: boolean;
}) {
  const pathname = usePathname();
  const router = useRouter();
  const { status, loaded } = useSession();
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
  return (
    <div className="app-shell">
      <a className="skip-link" href="#main-content">
        跳到主要内容
      </a>
      <aside className="sidebar">
        <Link className="brand" href="/" aria-label="AEP 工作台首页">
          <span className="brand-mark">
            <Icon name="grid" size={21} />
          </span>
          <span>
            AEP<span className="brand-subtitle">企业 AI 应用平台</span>
          </span>
        </Link>
        <div className="nav-label">工作台</div>
        <nav aria-label="主导航">
          {NAV.filter(
            (item) =>
              // Account and record pages exist only with sign-in on.
              (signIn || !["/users", "/audit"].includes(item.href)) &&
              may(pageRole(item.href)),
          ).map((item) => (
            <Link
              key={item.href}
              href={item.href}
              className={`nav-link ${item.active(pathname) ? "active" : ""}`}
            >
              <Icon name={item.icon} />
              {item.label}
            </Link>
          ))}
        </nav>
        {user && <Account user={user} />}
      </aside>
      <div className="main-shell">
        <header className="topbar">
          <span>
            <span className="muted">工作台</span>
            <span className="crumb-divider">/</span>
            {crumb(pathname)}
          </span>
          <span className="topbar-tools">
            {loaded && !signedOut && <NotificationBell />}
            <BackendHealth />
          </span>
        </header>
        {mockEnabled && (
          <div className="mock-banner">
            <span className="dot violet" />
            本地模拟模式
            <span className="mock-description">
              运行数据仅保存在当前开发进程中，不会调用真实工作流。
            </span>
          </div>
        )}
        <main id="main-content" className="main-content">
          {!loaded || signedOut ? (
            <div className="loading-state" role="status">
              <span className="spinner" />
              正在加载…
            </div>
          ) : !signIn && accountsOnly(pathname) ? (
            <EmptyState title="没有打开登录">
              账号和操作记录在打开登录（AEP_AUTH_ENABLED）后才有。
            </EmptyState>
          ) : may(needed) ? (
            children
          ) : (
            <EmptyState title="没有权限">
              这个页面需要{roleLabels[needed]}
              权限。需要的话请找管理员在「账号」里给你加上。
            </EmptyState>
          )}
        </main>
        <footer className="footer">
          企业 AI 应用平台<span>让每一个需求，成为可追踪的交付。</span>
        </footer>
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
