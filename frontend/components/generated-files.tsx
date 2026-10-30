"use client";

import { useState, type ReactNode } from "react";
import {
  deployModeLabels,
  languageLabels,
  type EtlResult,
  type EtlVerification,
} from "../lib/etl";
import { CodeView, codeLanguage } from "./code-view";
import { Icon } from "./icons";

type FileNode = {
  name: string;
  path: string;
  children: FileNode[];
  file: boolean;
};

function fileTree(paths: string[]): FileNode[] {
  const roots: FileNode[] = [];
  for (const path of paths) {
    let siblings = roots;
    const parts = path.split("/");
    parts.forEach((name, index) => {
      const current = parts.slice(0, index + 1).join("/");
      let node = siblings.find((node) => node.path === current);
      if (!node) {
        node = { name, path: current, children: [], file: false };
        siblings.push(node);
      }
      if (index === parts.length - 1) node.file = true;
      siblings = node.children;
    });
  }
  return roots;
}

function FileTree({
  nodes,
  selected,
  onSelect,
}: {
  nodes: FileNode[];
  selected: string;
  onSelect: (path: string) => void;
}) {
  return (
    <ul>
      {nodes.map((node) => (
        <li key={node.path}>
          {node.file && (
            <button
              type="button"
              onClick={() => onSelect(node.path)}
              className={selected === node.path ? "selected" : ""}
              aria-label={`打开 ${node.path}`}
              aria-current={selected === node.path ? "true" : undefined}
            >
              <Icon name="runs" size={14} />
              <span>{node.name}</span>
            </button>
          )}
          {node.children.length > 0 && (
            <details open>
              <summary>{node.name}/</summary>
              <FileTree
                nodes={node.children}
                selected={selected}
                onSelect={onSelect}
              />
            </details>
          )}
        </li>
      ))}
    </ul>
  );
}

function VerificationView({ verification }: { verification: EtlVerification }) {
  const mode = verification.mode
    ? ` · ${deployModeLabels[verification.mode]}`
    : "";
  const link = verification.url && (
    <a href={verification.url} target="_blank" rel="noopener noreferrer">
      在 Databricks 中查看
    </a>
  );
  if (verification.status === "skipped")
    return (
      <p className="verification-note">
        没有在 Databricks dev 目标上试跑
        {verification.reason ? `：${verification.reason}` : "。"}
      </p>
    );
  if (verification.status === "failed")
    return (
      <div className="checks-failed" role="alert">
        <h3>
          <Icon name="alert" />
          Databricks dev 试跑未通过{mode}
        </h3>
        <p>
          代码已生成，但在 dev 目标上运行失败，修正前请不要部署到生产。{link}
        </p>
        {verification.errors.length > 0 && (
          <ul>
            {verification.errors.map((error, i) => (
              <li key={i}>{error}</li>
            ))}
          </ul>
        )}
      </div>
    );
  return (
    <div className="checks-passed verification-passed">
      <p>
        <Icon name="check" size={16} />
        Databricks dev 试跑通过{mode}
        {link}
      </p>
      {verification.tables.length > 0 && (
        <ul>
          {verification.tables.map((table) => (
            <li key={table.name}>
              {table.name}：
              {table.rows === null ? "行数未知" : `${table.rows} 行`}
              {table.dropped > 0 && `，质量规则丢弃 ${table.dropped} 行`}
            </li>
          ))}
        </ul>
      )}
      {verification.warnings.map((warning, i) => (
        <p key={i}>注意：{warning}</p>
      ))}
    </div>
  );
}

// A bundle's files: the tree, and the open file's text. `toolbar` adds what the open file
// offers beside its name.
export function FileBrowser({
  files,
  toolbar,
}: {
  files: Record<string, string>;
  toolbar?: (path: string) => ReactNode;
}) {
  const paths = Object.keys(files).sort();
  const [selection, setSelection] = useState("");
  const selected = paths.includes(selection)
    ? selection
    : paths.find((path) => /(^|\/)pipeline\.(py|sql)$/i.test(path)) ||
      paths.find((path) => /\.(py|sql)$/i.test(path)) ||
      paths[0];
  if (!selected) return <p className="no-files">本次运行未生成文件。</p>;
  return (
    <div className="file-browser">
      <nav className="file-tree" aria-label="生成文件目录">
        <FileTree
          nodes={fileTree(paths)}
          selected={selected}
          onSelect={setSelection}
        />
      </nav>
      <div className="code-panel">
        <div className="code-toolbar">
          <span>{selected}</span>
          {toolbar?.(selected)}
          <span>{codeLanguage(selected)}</span>
        </div>
        <CodeView path={selected} content={files[selected]} />
      </div>
    </div>
  );
}

export function GeneratedFiles({
  result,
  runId,
}: {
  result: EtlResult;
  runId: string;
}) {
  const paths = Object.keys(result.files).sort();
  return (
    <section className="panel generated-files" aria-labelledby="files-title">
      <div className="section-heading files-heading">
        <div>
          <h2 id="files-title">
            <Icon name="runs" />
            生成的文件
          </h2>
          <p>
            {result.bundle} · {languageLabels[result.language]} · {paths.length}{" "}
            个文件
          </p>
        </div>
        {paths.length > 0 && (
          <a
            className="button primary"
            href={`/api/runs/${encodeURIComponent(runId)}/files.zip`}
            download
          >
            <Icon name="download" size={16} />
            下载 ZIP
          </a>
        )}
      </div>
      {result.checks.passed ? (
        <p className="checks-passed">
          <Icon name="check" size={16} />
          代码检查通过
        </p>
      ) : (
        <div className="checks-failed" role="alert">
          <h3>
            <Icon name="alert" />
            代码检查未通过
          </h3>
          <p>请处理以下问题后再使用生成的代码。</p>
          {result.checks.problems.length > 0 ? (
            <ul>
              {result.checks.problems.map((problem, i) => (
                <li key={i}>{problem}</li>
              ))}
            </ul>
          ) : (
            <p>检查未提供具体问题。</p>
          )}
        </div>
      )}
      {result.verification && (
        <VerificationView verification={result.verification} />
      )}
      <FileBrowser files={result.files} />
    </section>
  );
}
