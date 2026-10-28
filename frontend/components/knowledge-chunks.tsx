"use client";

import { useEffect, useRef, useState } from "react";
import { documentFileUrl, getChunks, type Chunk } from "../lib/knowledge";
import { errorMessage } from "../lib/runs";
import { AssetLoading } from "./asset-ui";
import { ErrorNotice } from "./ui";

export function KnowledgeChunks({
  kb,
  documentId,
  name,
  position,
  close,
}: {
  kb: string;
  documentId: string;
  name: string;
  position?: number;
  close: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [offset, setOffset] = useState(Math.floor((position ?? 0) / 50) * 50);
  const [data, setData] = useState<{ total: number; items: Chunk[] } | null>(
    null,
  );
  const [error, setError] = useState("");
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    dialog.current?.showModal();
  }, []);
  useEffect(() => {
    const controller = new AbortController();
    getChunks(kb, documentId, offset, controller.signal)
      .then((value) => {
        if (!controller.signal.aborted) {
          setData(value);
          setError("");
        }
      })
      .catch((err) => {
        if (!controller.signal.aborted) setError(errorMessage(err));
      });
    return () => controller.abort();
  }, [kb, documentId, offset, retry]);
  useEffect(() => {
    if (position === undefined || !data) return;
    const target = dialog.current?.querySelector<HTMLElement>(
      `[data-position="${position}"]`,
    );
    target?.scrollIntoView({ block: "center" });
    target?.focus({ preventScroll: true });
  }, [data, position]);
  return (
    <dialog
      ref={dialog}
      className="knowledge-drawer"
      aria-labelledby="chunks-title"
      onCancel={close}
      onClose={close}
    >
      <div className="asset-section-heading">
        <div>
          <span className="eyebrow">文档片段</span>
          <h2 id="chunks-title">{name}</h2>
        </div>
        <button className="text-button" onClick={close}>
          关闭
        </button>
      </div>
      <a
        className="text-button"
        href={documentFileUrl(kb, documentId)}
        download
      >
        下载原文件
      </a>
      {error && (
        <ErrorNotice message={error} onRetry={() => setRetry((n) => n + 1)} />
      )}
      {!data && !error && <AssetLoading />}
      {data && (
        <>
          <p className="muted">共 {data.total} 个片段 · 每页 50 个</p>
          <div className="knowledge-chunk-list">
            {data.items.map((chunk) => (
              <article
                tabIndex={-1}
                data-position={chunk.position}
                className={`knowledge-chunk ${chunk.position === position ? "highlighted" : ""}`}
                key={chunk.chunk_id}
              >
                <div className="asset-section-heading">
                  <strong>片段 {chunk.position + 1}</strong>
                  {chunk.page !== null && (
                    <span className="app-tag">第 {chunk.page} 页</span>
                  )}
                </div>
                <p className="muted">{chunk.heading || "无标题路径"}</p>
                <p className="knowledge-text">{chunk.text}</p>
              </article>
            ))}
          </div>
          {data.total === 0 && (
            <p className="muted">暂无片段，请等待文档处理完成。</p>
          )}
          <nav className="knowledge-pagination" aria-label="片段分页">
            <button
              className="button secondary"
              disabled={offset === 0}
              onClick={() => {
                setData(null);
                setOffset(offset - 50);
              }}
            >
              上一页
            </button>
            <span>
              第 {Math.floor(offset / 50) + 1} /{" "}
              {Math.max(1, Math.ceil(data.total / 50))} 页
            </span>
            <button
              className="button secondary"
              disabled={offset + 50 >= data.total}
              onClick={() => {
                setData(null);
                setOffset(offset + 50);
              }}
            >
              下一页
            </button>
          </nav>
        </>
      )}
    </dialog>
  );
}
