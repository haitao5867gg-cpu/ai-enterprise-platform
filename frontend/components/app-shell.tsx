"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState, type ReactNode } from "react";
import { isRecord, requestJson } from "../lib/runs";
import { Icon } from "./icons";

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
      后端状态：{healthy === null ? "检查中" : healthy ? "正常" : "不可用"}
    </span>
  );
}

export function AppShell({
  children,
  mockEnabled,
}: {
  children: ReactNode;
  mockEnabled: boolean;
}) {
  const pathname = usePathname();
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
            AEP<span className="brand-subtitle">AI Enterprise Platform</span>
          </span>
        </Link>
        <div className="workspace">
          <span className="workspace-avatar">W</span>
          <div>
            <strong>默认工作空间</strong>
            <small>企业 AI 工作台</small>
          </div>
          <span className="workspace-chevron">⌄</span>
        </div>
        <div className="nav-label">工作台</div>
        <nav aria-label="主导航">
          <Link
            href="/"
            className={`nav-link ${pathname !== "/runs/new" ? "active" : ""}`}
          >
            <Icon name="runs" />
            运行记录
          </Link>
          <Link
            href="/runs/new"
            className={`nav-link ${pathname === "/runs/new" ? "active" : ""}`}
          >
            <Icon name="plus" />
            新建运行
          </Link>
        </nav>
        <div className="sidebar-bottom">
          <span className="mini-app">
            <Icon name="spark" />
          </span>
          <div>
            <strong>hello / etl</strong>
            <small>示例与数据工作流 · M2</small>
          </div>
          <span className="version">v0.1</span>
        </div>
      </aside>
      <div className="main-shell">
        <header className="topbar">
          <span>
            <span className="muted">工作台</span>
            <span className="crumb-divider">/</span>
            {pathname === "/"
              ? "运行记录"
              : pathname === "/runs/new"
                ? "新建运行"
                : "运行详情"}
          </span>
          <BackendHealth />
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
          {children}
        </main>
        <footer className="footer">
          AI Enterprise Platform<span>让每一个需求，成为可追踪的交付。</span>
        </footer>
      </div>
    </div>
  );
}
