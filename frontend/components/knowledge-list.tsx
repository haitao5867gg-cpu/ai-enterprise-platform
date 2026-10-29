"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import {
  createKnowledgeBase,
  getKnowledgeOptions,
  listKnowledgeBases,
  type KnowledgeBase,
  type KnowledgeOptions,
} from "../lib/knowledge";
import { errorMessage } from "../lib/runs";
import { AssetHeading, AssetLoading } from "./asset-ui";
import { ErrorNotice, EmptyState } from "./ui";
import { KnowledgeForm } from "./knowledge-form";

function CreateDialog({
  options,
  close,
  created,
}: {
  options: KnowledgeOptions;
  close: () => void;
  created: (kb: KnowledgeBase) => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    dialog.current?.showModal();
  }, []);
  return (
    <dialog
      ref={dialog}
      className="knowledge-dialog"
      aria-labelledby="create-knowledge-title"
      onCancel={close}
      onClose={close}
    >
      <div className="asset-section-heading">
        <h2 id="create-knowledge-title">新建知识库</h2>
        <button type="button" className="text-button" onClick={close}>
          关闭
        </button>
      </div>
      <KnowledgeForm
        options={options}
        save={async (input) => {
          created(await createKnowledgeBase(input));
        }}
      />
    </dialog>
  );
}
export function KnowledgeList() {
  const [items, setItems] = useState<KnowledgeBase[] | null>(null);
  const [options, setOptions] = useState<KnowledgeOptions | null>(null);
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(0);
  const [creating, setCreating] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    Promise.all([
      listKnowledgeBases(controller.signal),
      getKnowledgeOptions(controller.signal),
    ])
      .then(([bases, opts]) => {
        if (!controller.signal.aborted) {
          setItems(bases);
          setOptions(opts);
          setError("");
        }
      })
      .catch((err) => {
        if (!controller.signal.aborted) setError(errorMessage(err));
      });
    return () => controller.abort();
  }, [revision]);
  return (
    <>
      <AssetHeading
        title="知识库"
        action={
          <button
            className="button primary"
            disabled={!options}
            onClick={() => setCreating(true)}
          >
            新建知识库
          </button>
        }
      >
        上传企业资料，在应用中检索并引用可信的内容。
      </AssetHeading>
      {error && (
        <ErrorNotice
          message={error}
          onRetry={() => setRevision((n) => n + 1)}
        />
      )}
      {!items && !error && <AssetLoading />}
      {items?.length === 0 && (
        <EmptyState title="还没有知识库">
          创建知识库后，上传 PDF、Word、Markdown 或文本文件。
        </EmptyState>
      )}
      <div className="knowledge-cards">
        {items?.map((kb) => (
          <Link
            className="panel knowledge-card"
            href={`/knowledge/${encodeURIComponent(kb.kb_id)}`}
            key={kb.kb_id}
          >
            <span className="app-tag">{kb.embedding_model}</span>
            <h2>{kb.name}</h2>
            <p className="muted">{kb.description || "暂无说明"}</p>
            <dl className="knowledge-stats">
              <div>
                <dt>文档</dt>
                <dd>{kb.document_count}</dd>
              </div>
              <div>
                <dt>片段</dt>
                <dd>{kb.chunk_count}</dd>
              </div>
              <div>
                <dt>处理中</dt>
                <dd>{kb.status_counts.queued + kb.status_counts.processing}</dd>
              </div>
            </dl>
            <span className="text-button">查看知识库 →</span>
          </Link>
        ))}
      </div>
      {creating && options && (
        <CreateDialog
          options={options}
          close={() => setCreating(false)}
          created={(kb) => {
            setItems((old) =>
              [...(old ?? []), kb].sort((a, b) => a.name.localeCompare(b.name)),
            );
            setCreating(false);
          }}
        />
      )}
    </>
  );
}
