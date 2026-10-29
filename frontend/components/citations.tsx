"use client";

import {
  Children,
  cloneElement,
  createContext,
  isValidElement,
  useContext,
  useId,
  useState,
  type ReactNode,
} from "react";
import Markdown from "react-markdown";
import { remarkBreaks } from "../lib/markdown-breaks";
import { remarkTables } from "../lib/markdown-tables";
import type { Reference, RunSource } from "../lib/knowledge";
import { documentFileUrl } from "../lib/knowledge";

const Citations = createContext<{
  sources: (Reference | RunSource)[];
  prefix: string;
  active: number | null;
  select: (index: number) => void;
}>({ sources: [], prefix: "", active: null, select: () => {} });
export function CitationScope({
  sources,
  children,
}: {
  sources: (Reference | RunSource)[];
  children: ReactNode;
}) {
  const prefix = useId().replace(/[^a-zA-Z0-9_-]/g, "");
  const [active, select] = useState<number | null>(null);
  return (
    <Citations.Provider value={{ sources, prefix, active, select }}>
      {children}
    </Citations.Provider>
  );
}

// Used by both Markdown results and structured ETL fields. Unknown numbers and
// text inside links/code remain untouched; React escapes all source text.
export function CitedText({ children }: { children: ReactNode }) {
  const { sources, prefix, select } = useContext(Citations);
  const numbers = new Set(sources.map((source) => source.index));
  function enhance(value: ReactNode): ReactNode {
    return Children.map(value, (child) => {
      if (typeof child === "string")
        return child.split(/(\[\d+\])/g).map((part, i) => {
          const match = part.match(/^\[(\d+)\]$/);
          const index = match ? Number(match[1]) : 0;
          if (!numbers.has(index)) return part;
          const target = `source-${prefix}-${index}`;
          return (
            <span className="citation-marker" key={i}>
              <a
                href={`#${target}`}
                aria-label={`引用来源 ${index}`}
                onClick={(event) => {
                  event.preventDefault();
                  select(index);
                  const element = document.getElementById(target);
                  // The source itself, and any group it is folded into.
                  for (
                    let details = element?.closest("details") ?? null;
                    details;
                    details = details.parentElement?.closest("details") ?? null
                  )
                    details.open = true;
                  element?.scrollIntoView({
                    behavior: "smooth",
                    block: "center",
                  });
                  element?.focus({ preventScroll: true });
                }}
              >
                [{index}]
              </a>
            </span>
          );
        });
      if (
        isValidElement<{ children?: ReactNode }>(child) &&
        !["a", "code", "pre", "sup"].includes(String(child.type))
      )
        return cloneElement(child, undefined, enhance(child.props.children));
      return child;
    });
  }
  return <>{sources.length ? enhance(children) : children}</>;
}
// `breaks` keeps single line breaks (chat replies). Images are shown as links:
// model output can carry any URL, and an image would be fetched on page load.
export function CitedMarkdown({
  text,
  breaks = false,
}: {
  text: string;
  breaks?: boolean;
}) {
  return (
    <div className="markdown-content">
      <Markdown
        remarkPlugins={breaks ? [remarkTables, remarkBreaks] : [remarkTables]}
        components={{
          img: ({ src, alt }) =>
            typeof src === "string" ? (
              <a href={src} target="_blank" rel="noopener noreferrer">
                图片：{alt || src}
              </a>
            ) : null,
          table: ({ children }) => (
            <div className="table-scroll">
              <table>{children}</table>
            </div>
          ),
          th: ({ children, style }) => (
            <th style={style}>
              <CitedText>{children}</CitedText>
            </th>
          ),
          td: ({ children, style }) => (
            <td style={style}>
              <CitedText>{children}</CitedText>
            </td>
          ),
          p: ({ children }) => (
            <p>
              <CitedText>{children}</CitedText>
            </p>
          ),
          li: ({ children }) => (
            <li>
              <CitedText>{children}</CitedText>
            </li>
          ),
          h1: ({ children }) => (
            <h1>
              <CitedText>{children}</CitedText>
            </h1>
          ),
          h2: ({ children }) => (
            <h2>
              <CitedText>{children}</CitedText>
            </h2>
          ),
          h3: ({ children }) => (
            <h3>
              <CitedText>{children}</CitedText>
            </h3>
          ),
          h4: ({ children }) => (
            <h4>
              <CitedText>{children}</CitedText>
            </h4>
          ),
          h5: ({ children }) => (
            <h5>
              <CitedText>{children}</CitedText>
            </h5>
          ),
          h6: ({ children }) => (
            <h6>
              <CitedText>{children}</CitedText>
            </h6>
          ),
        }}
      >
        {text}
      </Markdown>
    </div>
  );
}
function SourceItem({ source }: { source: Reference | RunSource }) {
  const { prefix, active } = useContext(Citations);
  return (
    <details
      id={`source-${prefix}-${source.index}`}
      tabIndex={-1}
      className={`citation-source ${active === source.index ? "highlighted" : ""}`}
    >
      <summary>
        <strong>
          [{source.index}] {source.document_name}
        </strong>
        <span className="citation-meta">
          {"node_title" in source && <span>节点：{source.node_title} · </span>}
          {source.heading || "无标题路径"}
          {source.page !== null && ` · 第 ${source.page} 页`} · 分数{" "}
          {source.score.toFixed(2)}
        </span>
      </summary>
      <p className="knowledge-text">{source.text}</p>
      <a
        className="text-button"
        href={documentFileUrl(source.kb_id, source.document_id)}
        download
      >
        下载原文件
      </a>
    </details>
  );
}
// Sources the shown text cites by [n] are listed; the rest of what was
// retrieved is folded away. A list without `cited` counts as all cited.
export function SourceList({ title = "引用来源" }: { title?: string }) {
  const { sources } = useContext(Citations);
  if (!sources.length) return null;
  const sorted = [...sources].sort((a, b) => a.index - b.index);
  const shown = sorted.filter(
    (source) => !("cited" in source) || source.cited !== false,
  );
  const rest = sorted.filter((source) => !shown.includes(source));
  return (
    <section className="citation-sources" aria-label={title}>
      <h3>{title}</h3>
      {shown.length === 0 && (
        <p className="muted">目前显示的内容没有引用检索到的资料。</p>
      )}
      {shown.map((source) => (
        <SourceItem key={source.index} source={source} />
      ))}
      {rest.length > 0 && (
        <details className="uncited-sources">
          <summary>另有 {rest.length} 条检索到但未引用</summary>
          {rest.map((source) => (
            <SourceItem key={source.index} source={source} />
          ))}
        </details>
      )}
    </section>
  );
}
