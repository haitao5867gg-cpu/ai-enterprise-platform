"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import {
  createSkill,
  listSkills,
  SKILL_NAME_PATTERN,
  type SkillSummary,
} from "../lib/skills";
import { errorMessage } from "../lib/runs";
import {
  AssetHeading,
  AssetLoading,
  AssetTime,
  useUnsavedWarning,
} from "./asset-ui";
import { EmptyState, ErrorNotice } from "./ui";

export function SkillList() {
  const router = useRouter();
  const [skills, setSkills] = useState<SkillSummary[] | null>(null);
  const [error, setError] = useState("");
  const [retry, setRetry] = useState(0);
  const [creating, setCreating] = useState(false);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [body, setBody] = useState("");
  const [busy, setBusy] = useState(false);
  const [formError, setFormError] = useState("");
  useUnsavedWarning(creating && Boolean(name || description || body));
  useEffect(() => {
    const controller = new AbortController();
    listSkills(controller.signal)
      .then((value) => {
        if (!controller.signal.aborted) {
          setSkills(value);
          setError("");
        }
      })
      .catch((err) => {
        if (!controller.signal.aborted) setError(errorMessage(err));
      });
    return () => controller.abort();
  }, [retry]);
  async function create() {
    if (!new RegExp(SKILL_NAME_PATTERN).test(name)) {
      setFormError(
        "名称须以小写字母开头，包含 2~64 位小写字母、数字或短横线。",
      );
      return;
    }
    if (!description.trim()) {
      setFormError("说明不能为空。");
      return;
    }
    setBusy(true);
    setFormError("");
    try {
      const skill = await createSkill({
        name,
        description: description.trim(),
        body,
        references: {},
      });
      setCreating(false);
      router.push(`/skills/${encodeURIComponent(skill.name)}`);
    } catch (err) {
      setFormError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="asset-page">
      <AssetHeading
        eyebrow="REUSABLE KNOW-HOW"
        title="Skill"
        action={
          <button
            type="button"
            className="button primary"
            onClick={() => setCreating(true)}
          >
            新建 Skill
          </button>
        }
      >
        把做法沉淀成可复用、可追溯版本的团队资产。
      </AssetHeading>
      {creating && (
        <section className="panel asset-section" aria-label="新建 Skill">
          <h2>新建 Skill</h2>
          <form
            className="asset-form"
            noValidate
            onSubmit={(event) => {
              event.preventDefault();
              void create();
            }}
          >
            {formError && <ErrorNotice message={formError} />}
            <label className="asset-field">
              名称
              <input
                aria-label="名称"
                aria-describedby="skill-name-help"
                required
                pattern={SKILL_NAME_PATTERN}
                maxLength={64}
                value={name}
                disabled={busy}
                onChange={(event) => setName(event.target.value)}
                placeholder="order-quality"
              />
              <small className="muted" id="skill-name-help">
                2~64 位小写字母、数字或短横线，以小写字母开头。
              </small>
            </label>
            <label className="asset-field">
              说明
              <textarea
                required
                maxLength={500}
                rows={2}
                value={description}
                disabled={busy}
                onChange={(event) => setDescription(event.target.value)}
              />
            </label>
            <label className="asset-field">
              正文（Markdown）
              <textarea
                maxLength={20_000}
                rows={6}
                value={body}
                disabled={busy}
                onChange={(event) => setBody(event.target.value)}
              />
            </label>
            <div className="asset-actions">
              <button className="button primary" disabled={busy}>
                创建草稿
              </button>
              <button
                type="button"
                className="button secondary"
                disabled={busy}
                onClick={() => {
                  if (
                    !(name || description || body) ||
                    window.confirm("确定放弃新建 Skill 的内容吗？")
                  ) {
                    setCreating(false);
                    setName("");
                    setDescription("");
                    setBody("");
                    setFormError("");
                  }
                }}
              >
                取消
              </button>
            </div>
          </form>
        </section>
      )}
      {error ? (
        <ErrorNotice
          message={error}
          onRetry={() => setRetry((value) => value + 1)}
        />
      ) : !skills ? (
        <AssetLoading />
      ) : !skills.length ? (
        <section className="panel">
          <EmptyState title="还没有 Skill">
            新建一份做法说明，发布后即可供应用节点引用。
          </EmptyState>
        </section>
      ) : (
        <div className="asset-cards">
          {skills.map((skill) => (
            <article className="panel asset-card" key={skill.name}>
              <h2>
                <Link href={`/skills/${encodeURIComponent(skill.name)}`}>
                  {skill.name}
                </Link>
              </h2>
              <p>{skill.description}</p>
              <div className="asset-tags">
                <span>
                  {skill.published_version === null
                    ? "尚未发布"
                    : `最新 v${skill.published_version}`}
                </span>
                <span className={skill.has_draft ? "draft-tag" : ""}>
                  {skill.has_draft ? "有草稿" : "无草稿"}
                </span>
              </div>
              <div className="skill-usage">
                <strong>used_by · 当前引用</strong>
                {skill.used_by.length ? (
                  <ul>
                    {skill.used_by.map((usage) => (
                      <li key={`${usage.app}.${usage.node}`}>
                        <Link href={`/apps/${encodeURIComponent(usage.app)}`}>
                          {usage.app} / {usage.node} · 应用 v{usage.version}
                        </Link>
                      </li>
                    ))}
                  </ul>
                ) : (
                  <p className="muted">暂无应用引用</p>
                )}
              </div>
              <small className="muted">
                更新于 <AssetTime value={skill.updated_at} />
              </small>
            </article>
          ))}
        </div>
      )}
    </div>
  );
}
