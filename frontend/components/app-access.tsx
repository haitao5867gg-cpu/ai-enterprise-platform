"use client";

import { useEffect, useId, useRef, useState } from "react";
import {
  createKey,
  deleteKey,
  exampleInput,
  listKeys,
  type ApiKey,
  type NewApiKey,
} from "../lib/api-keys";
import type { Input } from "../lib/apps";
import { errorMessage } from "../lib/runs";
import { AssetTime } from "./asset-ui";
import { DatasourceDialog } from "./datasource-form";
import { useMay } from "./session";
import { ErrorNotice } from "./ui";

function CopyButton({ text, label }: { text: string; label: string }) {
  const [done, setDone] = useState(false);
  return (
    <button
      type="button"
      className="button secondary"
      aria-label={label}
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(text);
          setDone(true);
          setTimeout(() => setDone(false), 2000);
        } catch {
          // Clipboard refused (no permission): the text is on the page to select.
        }
      }}
    >
      {done ? "已复制" : "复制"}
    </button>
  );
}

function NewKeyForm({
  app,
  onCreated,
}: {
  app: string;
  onCreated: (key: NewApiKey) => void;
}) {
  const id = useId();
  const [name, setName] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  return (
    <form
      className="asset-form"
      noValidate
      onSubmit={async (event) => {
        event.preventDefault();
        if (!name.trim() || saving) return;
        setSaving(true);
        setError("");
        try {
          onCreated(await createKey(app, name.trim()));
        } catch (err) {
          setError(errorMessage(err));
          setSaving(false);
        }
      }}
    >
      <div className="asset-field">
        <label htmlFor={`${id}-name`}>名称</label>
        <input
          id={`${id}-name`}
          data-autofocus
          maxLength={50}
          value={name}
          placeholder="谁用这个密钥，例如：CRM 系统"
          onChange={(event) => setName(event.target.value)}
        />
      </div>
      {error && <ErrorNotice message={error} />}
      <div className="datasource-save-row">
        <button className="button primary" disabled={!name.trim() || saving}>
          {saving ? "正在新建…" : "新建密钥"}
        </button>
      </div>
    </form>
  );
}

// The app's API and MCP endpoints, and its API keys (docs/api/app-access.md).
export function AppAccess({
  app,
  inputs,
}: {
  app: string;
  inputs: Input[] | null;
}) {
  const mayBuild = useMay("builder");
  const [keys, setKeys] = useState<ApiKey[] | null>(null);
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(0);
  const [dialog, setDialog] = useState<"create" | NewApiKey | null>(null);
  const [notice, setNotice] = useState("");
  const dialogRef = useRef<HTMLDialogElement>(null);
  const [origin, setOrigin] = useState("https://平台地址");
  useEffect(() => {
    // Only known in the browser.
    const frame = requestAnimationFrame(() =>
      setOrigin(window.location.origin),
    );
    return () => cancelAnimationFrame(frame);
  }, []);
  useEffect(() => {
    if (!mayBuild) return;
    const controller = new AbortController();
    listKeys(app, controller.signal)
      .then((value) => {
        setKeys(value);
        setError("");
      })
      .catch((err) => {
        if (!controller.signal.aborted) setError(errorMessage(err));
      });
    return () => controller.abort();
  }, [app, mayBuild, revision]);

  const runs = `${origin}/api/v1/apps/${app}/runs`;
  const mcp = `${origin}/api/mcp/apps/${app}`;
  const body = JSON.stringify({ input: exampleInput(inputs), wait: 30 });
  const curl = [
    `curl -s -X POST ${runs} \\`,
    `  -H "Authorization: Bearer $AEP_KEY" -H "Content-Type: application/json" \\`,
    `  -d '${body.replaceAll("'", "'\\''")}'`,
  ].join("\n");
  const mcpConfig = JSON.stringify(
    {
      mcpServers: {
        [app]: {
          type: "http",
          url: mcp,
          headers: { Authorization: "Bearer aep_…" },
        },
      },
    },
    null,
    2,
  );

  return (
    <section
      className="panel asset-section app-access"
      aria-labelledby="app-access-title"
    >
      <h2 id="app-access-title">API 和 MCP</h2>
      <p className="muted">
        别的系统可以用 API 密钥调这个应用：经 API
        发起运行、查结果、填表单，或者在 Claude、Cursor 等 MCP
        客户端里把它当成一个工具。用密钥发起的运行和其他运行一样出现在运行记录里，审批仍由审批者在平台上处理。
      </p>
      <dl className="app-access-endpoints">
        <dt>API</dt>
        <dd>
          <code>{runs}</code>
        </dd>
        <dt>MCP</dt>
        <dd>
          <code>{mcp}</code>
        </dd>
      </dl>
      <details className="app-access-example">
        <summary>用 curl 跑一次</summary>
        <pre>
          <code>{curl}</code>
        </pre>
        <CopyButton text={curl} label="复制 curl 示例" />
        <p className="field-hint">
          返回运行状态；运行没结束时用{" "}
          <code>GET /api/v1/runs/run_id?wait=30</code> 查结果。完整说明见
          docs/api/app-access.md。
        </p>
      </details>
      <details className="app-access-example">
        <summary>MCP 客户端配置</summary>
        <pre>
          <code>{mcpConfig}</code>
        </pre>
        <CopyButton text={mcpConfig} label="复制 MCP 配置" />
      </details>
      {mayBuild ? (
        <>
          <div className="asset-section-heading">
            <h3>API 密钥</h3>
            <button
              type="button"
              className="button secondary"
              onClick={() => {
                setNotice("");
                setDialog("create");
              }}
            >
              新建密钥
            </button>
          </div>
          {notice && (
            <p className="datasource-note" role="status">
              {notice}
            </p>
          )}
          {error && (
            <ErrorNotice
              message={error}
              onRetry={() => setRevision((n) => n + 1)}
            />
          )}
          {keys && !keys.length && (
            <p className="muted">
              还没有密钥。新建一个给要调用这个应用的系统。
            </p>
          )}
          {keys && keys.length > 0 && (
            <div className="table-scroll">
              <table className="mapping-table" aria-label="API 密钥">
                <thead>
                  <tr>
                    {["名称", "密钥", "谁建的", "最近使用", ""].map((h, i) => (
                      <th scope="col" key={i}>
                        {h}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {keys.map((key) => (
                    <tr key={key.key_id}>
                      <td>{key.name}</td>
                      <td>
                        <code>{key.prefix}…</code>
                      </td>
                      <td>{key.created_by ?? "—"}</td>
                      <td>
                        {key.last_used_at ? (
                          <AssetTime value={key.last_used_at} />
                        ) : (
                          <span className="muted">还没用过</span>
                        )}
                      </td>
                      <td>
                        <button
                          type="button"
                          className="text-button danger-text"
                          aria-label={`删除密钥「${key.name}」`}
                          onClick={async () => {
                            if (
                              !window.confirm(
                                `确定删除密钥「${key.name}」吗？用它的系统会立刻调不通。`,
                              )
                            )
                              return;
                            try {
                              await deleteKey(app, key.key_id);
                              setNotice(`已删除密钥「${key.name}」。`);
                              setRevision((n) => n + 1);
                            } catch (err) {
                              setError(errorMessage(err));
                            }
                          }}
                        >
                          删除
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      ) : (
        <p className="muted">密钥由搭建者管理，需要的话请找他们。</p>
      )}
      {dialog && (
        <DatasourceDialog
          ref={dialogRef}
          title={dialog === "create" ? "新建 API 密钥" : "密钥已建好"}
          onClose={() => {
            setDialog(null);
            setRevision((n) => n + 1);
          }}
        >
          {dialog === "create" ? (
            <NewKeyForm app={app} onCreated={(key) => setDialog(key)} />
          ) : (
            <div className="asset-form">
              <p>
                这是「{dialog.name}」的完整密钥，<strong>只显示这一次</strong>
                。请现在复制，放进对方系统的配置里；关掉后平台也看不到它。
              </p>
              <code className="app-access-secret" aria-label="完整密钥">
                {dialog.secret}
              </code>
              <div className="datasource-save-row">
                <CopyButton text={dialog.secret} label="复制密钥" />
                <button
                  type="button"
                  className="button primary"
                  onClick={() => dialogRef.current?.close()}
                >
                  我已保存好
                </button>
              </div>
            </div>
          )}
        </DatasourceDialog>
      )}
    </section>
  );
}
