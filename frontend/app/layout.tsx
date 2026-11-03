import type { ReactNode } from "react";
import { AppShell } from "../components/app-shell";
import { LocaleProvider } from "../lib/i18n/client";
import { htmlLang, translate } from "../lib/i18n/locale";
import { isLocalMockEnabled } from "../lib/server/config";
import { requestLocale } from "../lib/server/locale";
import "@xyflow/react/dist/style.css";
import "./globals.css";

export async function generateMetadata() {
  const locale = await requestLocale();
  return {
    title: {
      default: translate(locale, "对话工作台 · AEP"),
      template: "%s · AEP",
    },
    description: translate(locale, "企业 AI 应用运行与审批工作台"),
  };
}

export const dynamic = "force-dynamic";

export default async function RootLayout({
  children,
}: {
  children: ReactNode;
}) {
  const locale = await requestLocale();
  return (
    <html lang={htmlLang(locale)}>
      <body>
        <LocaleProvider initial={locale}>
          <AppShell mockEnabled={isLocalMockEnabled()}>{children}</AppShell>
        </LocaleProvider>
      </body>
    </html>
  );
}
