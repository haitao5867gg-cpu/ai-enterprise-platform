import type { ReactNode } from "react";
import { AppShell } from "../components/app-shell";
import { isLocalMockEnabled } from "../lib/server/config";
import "@xyflow/react/dist/style.css";
import "./globals.css";

export const metadata = {
  title: {
    default: "对话工作台 · AEP",
    template: "%s · AEP",
  },
  description: "企业 AI 应用运行与审批工作台",
};

export const dynamic = "force-dynamic";

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="zh-CN">
      <body>
        <AppShell mockEnabled={isLocalMockEnabled()}>{children}</AppShell>
      </body>
    </html>
  );
}
