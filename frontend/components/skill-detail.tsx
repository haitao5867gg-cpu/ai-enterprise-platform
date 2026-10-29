"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { actorPayload, savedActor } from "../lib/actor";
import { ApiError, errorMessage } from "../lib/runs";
import {
  deleteSkillDraft,
  getSkill,
  getSkillVersion,
  publishSkillDraft,
  saveSkillDraft,
  type SkillContent,
  type SkillDetail,
} from "../lib/skills";
import {
  ActorField,
  AssetHeading,
  AssetLoading,
  AssetTime,
  useUnsavedWarning,
  VersionPicker,
} from "./asset-ui";
import {
  editSkill,
  SkillContentView,
  SkillEditor,
  skillInput,
  type SkillEdit,
} from "./skill-editor";
import { ErrorNotice } from "./ui";

function BodyDiff({
  left,
  right,
}: {
  left: SkillContent;
  right: SkillContent;
}) {
  const a = left.body.split("\n");
  const b = right.body.split("\n");
  // Compare the same line in each immutable snapshot; no Markdown dependency needed.
  const lines = Array.from(
    { length: Math.max(a.length, b.length) },
    (_, index) => ({ index, before: a[index], after: b[index] }),
  );
  return (
    <section className="asset-diff" aria-label="正文差异">
      <h3>
        正文差异：v{left.version} → v{right.version}
      </h3>
      {left.body === right.body ? (
        <p className="muted">两个版本正文相同。</p>
      ) : (
        <ol className="line-diff">
          {lines.map((line) => (
            <li key={line.index}>
              <span className="diff-line-number">第 {line.index + 1} 行</span>
              {line.before === line.after ? (
                <pre>{line.before || " "}</pre>
              ) : (
                <>
                  {line.before !== undefined && (
                    <del>
                      <pre>− {line.before}</pre>
                    </del>
                  )}
                  {line.after !== undefined && (
                    <ins>
                      <pre>+ {line.after}</pre>
                    </ins>
                  )}
                </>
              )}
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}

export function SkillDetailView({ name }: { name: string }) {
  const [data, setData] = useState<SkillDetail | null>(null);
  const [editor, setEditor] = useState<SkillEdit | null>(null);
  const [baseline, setBaseline] = useState<SkillEdit | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState("");
  const [actor, setActor] = useState(savedActor);
  const [retry, setRetry] = useState(0);
  const [viewing, setViewing] = useState<number | null>(null);
  const [historical, setHistorical] = useState<SkillContent | null>(null);
  const [versionError, setVersionError] = useState("");
  const [versionRetry, setVersionRetry] = useState(0);
  const [compare, setCompare] = useState<number[]>([]);
  const [diff, setDiff] = useState<{
    key: string;
    left: SkillContent;
    right: SkillContent;
  } | null>(null);
  const [diffError, setDiffError] = useState("");
  const [diffRetry, setDiffRetry] = useState(0);
  const compareKey = compare.length === 2 ? compare.join(",") : "";

  useEffect(() => {
    const controller = new AbortController();
    getSkill(name, controller.signal)
      .then((value) => {
        if (!controller.signal.aborted) {
          setData(value);
          setError("");
        }
      })
      .catch((err) => {
        if (!controller.signal.aborted)
          setError(
            err instanceof ApiError && err.status === 404
              ? "没有找到这个 Skill。"
              : errorMessage(err),
          );
      });
    return () => controller.abort();
  }, [name, retry]);
  useEffect(() => {
    if (viewing === null) return;
    const controller = new AbortController();
    getSkillVersion(name, viewing, controller.signal)
      .then((value) => {
        if (!controller.signal.aborted) {
          setHistorical(value);
          setVersionError("");
        }
      })
      .catch((err) => {
        if (!controller.signal.aborted) setVersionError(errorMessage(err));
      });
    return () => controller.abort();
  }, [name, viewing, versionRetry]);
  useEffect(() => {
    if (!compareKey) return;
    const controller = new AbortController();
    const [a, b] = compareKey.split(",").map(Number);
    Promise.all([
      getSkillVersion(name, a, controller.signal),
      getSkillVersion(name, b, controller.signal),
    ])
      .then(([left, right]) => {
        if (!controller.signal.aborted) {
          setDiff({ key: compareKey, left, right });
          setDiffError("");
        }
      })
      .catch((err) => {
        if (!controller.signal.aborted) setDiffError(errorMessage(err));
      });
    return () => controller.abort();
  }, [name, compareKey, diffRetry]);

  const dirty =
    editor !== null && JSON.stringify(editor) !== JSON.stringify(baseline);
  useUnsavedWarning(dirty);
  async function action(work: () => Promise<void>) {
    if (busy) return;
    setBusy(true);
    setError("");
    setNotice("");
    try {
      await work();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }
  function startEdit() {
    if (!data) return;
    const value = editSkill(
      data.draft ??
        data.current ?? {
          description: data.description,
          body: "",
          references: {},
        },
    );
    setEditor(value);
    setBaseline(value);
    setError("");
    setNotice("");
  }
  function save() {
    if (!editor) return;
    const input = skillInput(editor);
    if (typeof input === "string") {
      setError(input);
      return;
    }
    void action(async () => {
      const value = await saveSkillDraft(name, input);
      setData(value);
      const next = editSkill(value.draft!);
      setEditor(next);
      setBaseline(next);
      setNotice("Skill 草稿已保存。");
    });
  }

  if (!data)
    return (
      <div className="asset-page">
        <Link href="/skills" className="back-link">
          ← Skill
        </Link>
        {error ? (
          <ErrorNotice
            message={error}
            onRetry={() => setRetry((value) => value + 1)}
          />
        ) : (
          <AssetLoading />
        )}
      </div>
    );
  const content =
    viewing === null
      ? data.current
      : historical?.version === viewing
        ? historical
        : null;
  return (
    <div className="asset-page">
      <Link href="/skills" className="back-link">
        ← Skill
      </Link>
      <AssetHeading
        title={name}
        action={
          <button
            type="button"
            className="button primary"
            disabled={busy || editor !== null}
            onClick={startEdit}
          >
            编辑草稿
          </button>
        }
      >
        {data.description}
      </AssetHeading>
      {error && <ErrorNotice message={error} />}
      {notice && (
        <p className="asset-notice" role="status">
          {notice}
        </p>
      )}
      <section className="panel asset-section" aria-label="Skill 正文">
        <div className="asset-section-heading">
          <h2>
            {viewing !== null
              ? `正在查看：历史版本 v${viewing}`
              : data.current
                ? `正在查看：当前版本 v${data.published_version}`
                : "尚未发布"}
          </h2>
          {viewing !== null && (
            <button
              type="button"
              className="button secondary"
              onClick={() => {
                setViewing(null);
                setVersionError("");
              }}
            >
              返回当前版本
            </button>
          )}
        </div>
        {versionError && viewing !== null ? (
          <ErrorNotice
            message={versionError}
            onRetry={() => setVersionRetry((value) => value + 1)}
          />
        ) : content ? (
          <SkillContentView content={content} />
        ) : viewing !== null ? (
          <AssetLoading />
        ) : (
          <p className="muted">
            此 Skill 只有草稿。保存并发布后，应用节点即可引用。
          </p>
        )}
      </section>
      <section
        className={`panel asset-section ${data.has_draft || editor ? "draft-panel" : ""}`}
        aria-label="Skill 草稿"
      >
        <div className="asset-section-heading">
          <h2>
            {editor
              ? "正在编辑：草稿"
              : data.has_draft
                ? "已保存的草稿"
                : "草稿"}
          </h2>
          {editor && (
            <span className="draft-tag">
              {dirty ? "有未保存的修改" : "没有未保存的修改"}
            </span>
          )}
        </div>
        {editor ? (
          <form
            className="asset-form"
            noValidate
            onSubmit={(event) => {
              event.preventDefault();
              save();
            }}
          >
            <SkillEditor editor={editor} onChange={setEditor} disabled={busy} />
            <div className="asset-actions">
              <button
                className="button primary"
                disabled={busy || (!dirty && data.has_draft)}
              >
                保存草稿
              </button>
              <button
                type="button"
                className="button secondary"
                disabled={busy}
                onClick={() => {
                  if (dirty && !window.confirm("确定丢弃未保存的修改吗？"))
                    return;
                  setEditor(null);
                  setBaseline(null);
                }}
              >
                关闭编辑
              </button>
            </div>
          </form>
        ) : data.draft ? (
          <SkillContentView content={data.draft} />
        ) : (
          <p className="muted">点击“编辑草稿”创建下一版本。</p>
        )}
        {data.has_draft && (
          <button
            type="button"
            className="button secondary discard-skill"
            disabled={busy}
            onClick={() => {
              if (!window.confirm("确定放弃 Skill 草稿和未保存的修改吗？"))
                return;
              void action(async () => {
                await deleteSkillDraft(name);
                setData(await getSkill(name));
                setEditor(null);
                setBaseline(null);
                setNotice("已放弃 Skill 草稿。");
              });
            }}
          >
            放弃草稿
          </button>
        )}
      </section>
      <section className="panel asset-section" aria-label="发布 Skill">
        <h2>发布 Skill</h2>
        <form
          className="asset-form"
          onSubmit={(event) => {
            event.preventDefault();
            void action(async () => {
              setData(
                await publishSkillDraft(
                  name,
                  note.trim(),
                  actorPayload(actor).actor,
                ),
              );
              setEditor(null);
              setBaseline(null);
              setViewing(null);
              setNote("");
              setNotice("Skill 已发布为新版本。");
            });
          }}
        >
          <label className="asset-field">
            发布说明（必填）
            <textarea
              required
              rows={3}
              value={note}
              disabled={busy}
              onChange={(event) => setNote(event.target.value)}
            />
          </label>
          <ActorField actor={actor} onChange={setActor} disabled={busy} />
          {(!data.has_draft || dirty) && (
            <p className="muted" id="skill-publish-reason">
              {dirty
                ? "有未保存的修改，请先保存草稿。"
                : "请先保存 Skill 草稿。"}
            </p>
          )}
          <button
            className="button primary"
            disabled={busy || dirty || !data.has_draft || !note.trim()}
            aria-describedby={
              !data.has_draft || dirty ? "skill-publish-reason" : undefined
            }
          >
            发布新版本
          </button>
        </form>
      </section>
      <section className="panel asset-section" aria-label="版本历史">
        <h2>版本历史</h2>
        {data.versions.length ? (
          <ul className="asset-history">
            {data.versions.map((version) => (
              <li key={version.version}>
                <div>
                  <strong>
                    v{version.version}
                    {version.version === data.published_version
                      ? " · 当前版本"
                      : ""}
                  </strong>
                  <p>{version.note}</p>
                  <small>
                    {version.created_by ?? "未署名"} ·{" "}
                    <AssetTime value={version.created_at} />
                  </small>
                </div>
                <button
                  type="button"
                  className="button secondary"
                  onClick={() => {
                    setViewing(version.version);
                    setVersionError("");
                  }}
                >
                  查看 v{version.version}
                </button>
              </li>
            ))}
          </ul>
        ) : (
          <p className="muted">暂无已发布版本。</p>
        )}
        {data.versions.length >= 2 && (
          <VersionPicker
            versions={data.versions.map((version) => version.version)}
            selected={compare}
            onChange={setCompare}
          />
        )}
        {compareKey &&
          (diffError ? (
            <ErrorNotice
              message={diffError}
              onRetry={() => setDiffRetry((value) => value + 1)}
            />
          ) : diff?.key === compareKey ? (
            <BodyDiff left={diff.left} right={diff.right} />
          ) : (
            <AssetLoading />
          ))}
      </section>
    </div>
  );
}
