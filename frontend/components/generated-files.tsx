"use client";

import { useState } from "react";
import { languageLabels, type EtlResult } from "../lib/etl";
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

export function GeneratedFiles({
  result,
  runId,
}: {
  result: EtlResult;
  runId: string;
}) {
  const paths = Object.keys(result.files).sort();
  const [selection, setSelection] = useState("");
  const selected = paths.includes(selection)
    ? selection
    : paths.find((path) => /\.(py|sql)$/i.test(path)) || paths[0];
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
      {selected ? (
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
              <span>{codeLanguage(selected)}</span>
            </div>
            <CodeView path={selected} content={result.files[selected]} />
          </div>
        </div>
      ) : (
        <p className="no-files">本次运行未生成文件。</p>
      )}
    </section>
  );
}
