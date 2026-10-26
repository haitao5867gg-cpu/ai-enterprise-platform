import type { ReactNode } from "react";

export const metadata = {
  title: "AI Enterprise Platform",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="zh-CN">
      <body style={{ fontFamily: "system-ui, sans-serif", margin: 0, padding: 32 }}>
        {children}
      </body>
    </html>
  );
}
