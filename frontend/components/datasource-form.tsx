"use client";

import {
  useEffect,
  useId,
  useState,
  type ReactNode,
  type RefObject,
} from "react";
import {
  CREDENTIALS_NOTE,
  READ_ONLY_TAG,
  TOOL_SERVICE_NOTE,
  UNDECLARED_TAG,
  createDatasource,
  isToolService,
  kindLabel,
  testConnector,
  undeclaredNote,
  updateDatasource,
  type Datasource,
  type DatasourceKind,
  type TestResult,
} from "../lib/datasources";
import { ApiError, errorMessage } from "../lib/runs";
import { useUnsavedWarning } from "./asset-ui";
import { FileUpload } from "./file-upload";
import { ErrorNotice } from "./ui";

// A modal dialog that closes the native way (关闭, Escape, or `ref.current.close()`), so
// the browser returns focus to the button that opened it; `onClose` then unmounts it.
export function DatasourceDialog({
  ref,
  title,
  onClose,
  children,
}: {
  ref: RefObject<HTMLDialogElement | null>;
  title: string;
  onClose: () => void;
  children: ReactNode;
}) {
  const titleId = useId();
  useEffect(() => {
    ref.current?.showModal();
    ref.current?.querySelector<HTMLElement>("[data-autofocus]")?.focus();
  }, [ref]);
  return (
    <dialog
      ref={ref}
      className="datasource-dialog"
      aria-labelledby={titleId}
      onClose={onClose}
    >
      <div className="asset-section-heading">
        <h2 id={titleId}>{title}</h2>
        <button
          type="button"
          className="text-button"
          onClick={() => ref.current?.close()}
        >
          关闭
        </button>
      </div>
      {children}
    </dialog>
  );
}

const kindOptions: { value: DatasourceKind; label: string }[] = [
  { value: "postgres", label: "PostgreSQL" },
  { value: "mysql", label: "MySQL" },
  { value: "sqlserver", label: "SQL Server" },
  { value: "file", label: "上传文件（CSV、XLSX）" },
  { value: "mcp", label: "工具服务" },
];
const kindHints: Record<string, string> = {
  file: "上传 CSV 或 XLSX 文件，平台自动生成数据源，不用填地址。",
  mcp: "工具服务只提供工具，比如查工单、查 CRM，不能用于 ETL；在编排应用的「调用工具」和「智能体」节点里使用。",
};
const DATABASE_HINT =
  "数据源给 ETL 和「数据工程」类节点读数据。连接器要提供 6 个读数据的工具，测试连接时会检查。";

type Tested = { kind: string; url: string; result: TestResult };
type Props =
  | {
      mode: "create";
      initialKind?: DatasourceKind | "";
      onKindChange?: (kind: string) => void;
      onCreated: (id: string) => void;
    }
  | {
      mode: "edit";
      initial: Datasource;
      onSaved: (source: Datasource) => void;
    };

export function DatasourceForm(props: Props) {
  const prefix = useId();
  const initial = props.mode === "edit" ? props.initial : null;
  const [kind, setKind] = useState<string>(
    initial?.kind ?? (props.mode === "create" ? (props.initialKind ?? "") : ""),
  );
  const [name, setName] = useState(initial?.name ?? "");
  const [url, setUrl] = useState(initial?.connector_url ?? "");
  const [description, setDescription] = useState(initial?.description ?? "");
  const [tested, setTested] = useState<Tested | null>(null);
  const [testing, setTesting] = useState(false);
  const [testNote, setTestNote] = useState("");
  const [saving, setSaving] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState("");
  const [nameError, setNameError] = useState("");
  const [urlError, setUrlError] = useState("");
  const trimmedUrl = url.trim();
  const uploadKind = kind === "file";
  const urlChanged = !initial || trimmedUrl !== initial.connector_url;
  // PATCH sends only what changed: GET returns a masked address, so it is never sent back.
  const changes = initial
    ? {
        ...(name.trim() !== initial.name ? { name: name.trim() } : {}),
        ...(description !== initial.description ? { description } : {}),
        ...(urlChanged ? { connector_url: trimmedUrl } : {}),
      }
    : {};
  const dirty = initial
    ? Object.keys(changes).length > 0
    : Boolean(name.trim() || trimmedUrl || description);
  useUnsavedWarning(dirty);
  const current =
    tested && tested.kind === kind && tested.url === trimmedUrl ? tested : null;
  // Changing only the name or description needs no test.
  const needsTest = urlChanged;
  const blocker = !kind
    ? "请先选择类型。"
    : initial && !dirty
      ? "还没有修改。"
      : needsTest && !current
        ? tested
          ? initial
            ? "地址改过了，请重新测试连接。"
            : "地址或类型改过了，请重新测试连接。"
          : "请先测试连接。"
        : needsTest && !current?.result.ok
          ? "测试连接通过后才能保存。"
          : !name.trim()
            ? "请填写名称。"
            : "";

  async function test() {
    if (testing) return;
    setUrlError("");
    setError("");
    if (!kind) return setTestNote("请先选择类型。");
    if (!trimmedUrl) return setTestNote("请先填写连接器地址。");
    setTesting(true);
    setTestNote("");
    setTested(null);
    try {
      const result = await testConnector({
        kind: kind as DatasourceKind,
        connector_url: trimmedUrl,
      });
      setTested({ kind, url: trimmedUrl, result });
    } catch (err) {
      setTestNote(errorMessage(err));
    } finally {
      setTesting(false);
    }
  }

  async function save() {
    if (blocker || saving) return;
    setSaving(true);
    setError("");
    setNameError("");
    setUrlError("");
    try {
      if (props.mode === "create")
        props.onCreated(
          (
            await createDatasource({
              name: name.trim(),
              kind: kind as DatasourceKind,
              connector_url: trimmedUrl,
              description,
            })
          ).datasource_id,
        );
      else
        props.onSaved(
          await updateDatasource(props.initial.datasource_id, changes),
        );
    } catch (err) {
      if (err instanceof ApiError && err.status === 409)
        setNameError(err.message);
      else if (
        err instanceof ApiError &&
        err.status === 422 &&
        err.message.startsWith("测试连接没有通过")
      ) {
        setUrlError(err.message);
        setTested(null);
      } else setError(errorMessage(err));
      setSaving(false);
    }
  }

  const result = current?.result;
  const undeclared = result?.tools.filter((tool) => !tool.read_only).length;
  const ids = {
    kind: `${prefix}-kind`,
    kindHint: `${prefix}-kind-hint`,
    name: `${prefix}-name`,
    nameError: `${prefix}-name-error`,
    url: `${prefix}-url`,
    urlError: `${prefix}-url-error`,
    urlHint: `${prefix}-url-hint`,
    note: `${prefix}-note`,
    description: `${prefix}-description`,
    saveHint: `${prefix}-save-hint`,
  };
  return (
    <form
      className="asset-form datasource-form"
      noValidate
      onSubmit={(event) => {
        event.preventDefault();
        void save();
      }}
    >
      {props.mode === "create" ? (
        <div className="asset-field">
          <label htmlFor={ids.kind}>类型</label>
          <select
            id={ids.kind}
            data-autofocus
            required
            value={kind}
            disabled={uploading || saving}
            aria-describedby={ids.kindHint}
            onChange={(event) => {
              setKind(event.target.value);
              setTestNote("");
              props.onKindChange?.(event.target.value);
            }}
          >
            <option value="">请选择类型</option>
            {kindOptions.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
          <p className="field-hint" id={ids.kindHint}>
            {kind && (kindHints[kind] ?? DATABASE_HINT)}
            类型登记后不能修改。
          </p>
        </div>
      ) : (
        <div className="asset-field">
          <span>类型</span>
          <strong className="datasource-kind">{kindLabel(kind)}</strong>
          <p className="field-hint">类型登记后不能修改。</p>
        </div>
      )}
      {props.mode === "create" && uploadKind ? (
        <FileUpload
          disabled={false}
          onBusy={setUploading}
          onUploaded={({ datasource }) =>
            props.onCreated(datasource.datasource_id)
          }
        />
      ) : (
        <fieldset className="datasource-fields" disabled={saving}>
          <div className="asset-field">
            <label htmlFor={ids.name}>名称</label>
            <input
              id={ids.name}
              data-autofocus={initial ? true : undefined}
              required
              maxLength={100}
              value={name}
              placeholder={
                isToolService({ kind })
                  ? "业务上的叫法，例如：工单系统"
                  : "业务上的叫法，例如：线上订单库"
              }
              aria-invalid={Boolean(nameError)}
              aria-describedby={nameError ? ids.nameError : undefined}
              onChange={(event) => {
                setName(event.target.value);
                setNameError("");
              }}
            />
            {nameError && (
              <p className="field-error" role="alert" id={ids.nameError}>
                {nameError}
              </p>
            )}
          </div>
          <div className="asset-field">
            <label htmlFor={ids.url}>连接器地址</label>
            <input
              id={ids.url}
              type="text"
              inputMode="url"
              autoComplete="off"
              spellCheck={false}
              required
              readOnly={uploadKind}
              value={url}
              placeholder={
                isToolService({ kind })
                  ? "http://connector-tickets:8010/mcp"
                  : "http://connector-postgres:8001/mcp"
              }
              aria-invalid={Boolean(urlError)}
              aria-describedby={[
                urlError && ids.urlError,
                ids.urlHint,
                ids.note,
              ]
                .filter(Boolean)
                .join(" ")}
              onChange={(event) => {
                setUrl(event.target.value);
                setUrlError("");
                setTestNote("");
              }}
            />
            {urlError && (
              <p className="field-error" role="alert" id={ids.urlError}>
                {urlError}
              </p>
            )}
            <p className="field-hint" id={ids.urlHint}>
              {uploadKind
                ? "上传文件的数据源由平台生成地址，不能修改。"
                : "填连接器的地址，不是数据库的地址。地址一般以 /mcp 结尾。"}
            </p>
            <p className="datasource-note" id={ids.note}>
              {CREDENTIALS_NOTE}
              {isToolService({ kind }) && TOOL_SERVICE_NOTE}
            </p>
          </div>
          <div className="asset-field">
            <label htmlFor={ids.description}>
              说明<span className="muted">（选填）</span>
            </label>
            <textarea
              id={ids.description}
              maxLength={2000}
              rows={3}
              value={description}
              placeholder={
                isToolService({ kind })
                  ? "能查什么、给谁用，例如：客服工单，按客户查询和查看工单"
                  : "里面有什么数据、给谁用，例如：订单和客户，只读副本，每天凌晨同步"
              }
              onChange={(event) => setDescription(event.target.value)}
            />
          </div>
          {!uploadKind && (
            <div className="datasource-test-row">
              <button
                type="button"
                className="button secondary"
                aria-disabled={testing || undefined}
                onClick={() => void test()}
              >
                {testing ? "正在测试…" : "测试连接"}
              </button>
              <div
                className={`datasource-test ${result ? (result.ok ? "ok" : "error") : ""}`}
                role="status"
                aria-live="polite"
                aria-busy={testing}
              >
                {testing ? (
                  "正在测试连接…"
                ) : testNote ? (
                  testNote
                ) : result?.ok ? (
                  <>
                    <strong>
                      连接成功 · {result.latency_ms} 毫秒 ·{" "}
                      {result.tools.length} 个工具
                    </strong>
                    {Boolean(undeclared) && (
                      <p>{undeclaredNote(undeclared ?? 0)}</p>
                    )}
                    <details>
                      <summary>查看工具</summary>
                      <ul className="datasource-tool-names">
                        {result.tools.map((tool) => (
                          <li key={tool.name}>
                            <code>{tool.name}</code>
                            <span
                              className={`datasource-tag ${tool.read_only ? "" : "undeclared"}`}
                            >
                              {tool.read_only ? READ_ONLY_TAG : UNDECLARED_TAG}
                            </span>
                          </li>
                        ))}
                      </ul>
                    </details>
                  </>
                ) : result ? (
                  <>
                    <strong>测试没有通过</strong>
                    <ul>
                      {result.problems.map((problem, index) => (
                        <li key={`${problem.code}-${index}`}>
                          {problem.message}
                        </li>
                      ))}
                    </ul>
                  </>
                ) : null}
              </div>
            </div>
          )}
          {error && <ErrorNotice message={error} />}
          <div className="datasource-save-row">
            <button
              className="button primary"
              disabled={Boolean(blocker) || saving}
              aria-describedby={blocker ? ids.saveHint : undefined}
            >
              {saving ? "正在保存…" : "保存"}
            </button>
            {blocker && (
              <p className="field-hint" id={ids.saveHint}>
                {blocker}
              </p>
            )}
          </div>
        </fieldset>
      )}
    </form>
  );
}
