"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import {
  deleteProject,
  emptyProject,
  getProject,
  getProjectOptions,
  paramText,
  paramTypeLabels,
  scheduledText,
  projectInput,
  samplesText,
  type Project,
  type ProjectOptions,
} from "../lib/projects";
import { errorMessage } from "../lib/runs";
import { AssetHeading, AssetLoading, AssetTime } from "./asset-ui";
import { Icon } from "./icons";
import { ProjectForm } from "./project-form";
import { ErrorNotice } from "./ui";

const checkKinds: Record<string, string> = {
  aggregate: "聚合值",
  reconcile: "对账",
  unique: "键不重复",
  golden: "和老系统对比",
  rows: "不该有的行",
};
const severityLabels: Record<string, string> = {
  error: "算错误",
  warn: "警告",
  info: "提示",
};

function useOptions() {
  const [options, setOptions] = useState<ProjectOptions | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    // Without options the form still works: models are typed in.
    getProjectOptions(controller.signal)
      .then((value) => {
        if (!controller.signal.aborted) setOptions(value);
      })
      .catch(() => undefined);
    return () => controller.abort();
  }, []);
  return options;
}

export function NewProject() {
  const router = useRouter();
  const options = useOptions();
  return (
    <div className="asset-page">
      <Link className="back-link" href="/projects">
        <Icon name="back" size={16} />
        返回项目
      </Link>
      <AssetHeading title="新建项目">
        先填写基本信息和环境，引用表、运行参数和数据外发策略可以以后再补。
      </AssetHeading>
      <ProjectForm
        mode="create"
        initial={emptyProject()}
        options={options}
        onSaved={(project) =>
          router.push(`/projects/${encodeURIComponent(project.project_id)}`)
        }
      />
    </div>
  );
}

function Rows({ headings, rows }: { headings: string[]; rows: (string | null)[][] }) {
  return (
    <div className="table-scroll">
      <table className="mapping-table">
        <thead>
          <tr>
            {headings.map((heading) => (
              <th scope="col" key={heading}>
                {heading}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, i) => (
            <tr key={i}>
              {row.map((cell, j) => (
                <td key={j} className={j === 0 ? "monospace" : undefined}>
                  {cell || "—"}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function ProjectView({ project, options }: { project: Project; options: ProjectOptions | null }) {
  const defaults = options?.defaults;
  const level = options?.sample_levels.find(
    (item) => item.value === (project.policy.samples ?? defaults?.samples),
  );
  return (
    <>
      <section className="panel asset-section" aria-labelledby="project-basics">
        <h2 id="project-basics">基本信息</h2>
        <dl className="datasource-meta">
          <div>
            <dt>项目标识</dt>
            <dd className="monospace">{project.project_id}</dd>
          </div>
          <div>
            <dt>说明</dt>
            <dd>{project.description || "—"}</dd>
          </div>
          <div>
            <dt>表名前缀</dt>
            <dd className="monospace">{project.table_prefix}</dd>
          </div>
          <div>
            <dt>更新时间</dt>
            <dd>
              <AssetTime value={project.updated_at} />
            </dd>
          </div>
        </dl>
      </section>
      <section className="panel asset-section" aria-labelledby="project-environments">
        <h2 id="project-environments">环境</h2>
        <Rows
          headings={["标识", "显示名", "写到哪里", "集群"]}
          rows={project.environments.map((env, i) => [
            env.key,
            i === 0 ? `${env.label}（默认）` : env.label,
            `${env.catalog}.${env.schema}`,
            env.cluster_id ?? "平台配置的集群",
          ])}
        />
        <p className="field-hint">
          绑定项目的运行只能写当前环境下、以 {project.table_prefix}{" "}
          开头而且属于本项目的表。
        </p>
      </section>
      <section className="panel asset-section" aria-labelledby="project-tables">
        <h2 id="project-tables">表登记</h2>
        <h3>引用表</h3>
        {project.references.length ? (
          <Rows
            headings={["表名", "名称", "读取筛选", "说明"]}
            rows={project.references.map((ref) => [
              ref.name,
              ref.label,
              ref.filter,
              ref.description,
            ])}
          />
        ) : (
          <p className="muted">没有登记引用表。</p>
        )}
        <h3>归本项目的表</h3>
        {project.adopted.length ? (
          <ul className="project-tables">
            {project.adopted.map((table) => (
              <li key={table} className="monospace">
                {table}
              </li>
            ))}
          </ul>
        ) : (
          <p className="muted">没有。平台自己建的表会打上项目标记，不用登记。</p>
        )}
      </section>
      <section className="panel asset-section" aria-labelledby="project-params">
        <h2 id="project-params">运行参数</h2>
        {project.params.length ? (
          <Rows
            headings={["标识", "显示名", "类型", "必填", "默认值", "上线后定时运行时", "说明"]}
            rows={project.params.map((param) => [
              param.key,
              param.label,
              param.type === "select"
                ? `选项：${(param.choices ?? []).map((choice) => choice.label ?? choice.value).join("、")}`
                : param.type === "text" && param.pattern
                  ? `${paramTypeLabels.text}，格式 ${param.pattern}`
                  : paramTypeLabels[param.type],
              param.required ? "是" : "否",
              paramText(param, param.default),
              scheduledText(param),
              param.description,
            ])}
          />
        ) : (
          <p className="muted">没有运行参数。</p>
        )}
      </section>
      <section className="panel asset-section" aria-labelledby="project-checks">
        <h2 id="project-checks">验收检查</h2>
        {project.checks?.length ? (
          <Rows
            headings={["id", "名称", "种类", "不通过时"]}
            rows={project.checks.map((check) => [
              String(check.id ?? ""),
              String(check.label ?? ""),
              checkKinds[String(check.kind)] ?? String(check.kind ?? ""),
              severityLabels[String(check.severity ?? "error")] ?? String(check.severity),
            ])}
          />
        ) : (
          <p className="muted">没有。绑定这个项目的运行只跑方案建议的检查。</p>
        )}
      </section>
      <section className="panel asset-section" aria-labelledby="project-policy">
        <h2 id="project-policy">数据外发策略</h2>
        <dl className="datasource-meta">
          <div>
            <dt>发给模型的数据</dt>
            <dd>
              {samplesText(project.policy.samples, defaults?.samples)}
              {level && <p className="field-hint">{level.description}</p>}
            </dd>
          </div>
          <div>
            <dt>允许的模型</dt>
            <dd>
              {project.policy.models?.length
                ? project.policy.models.join("、")
                : `平台默认（${defaults?.models?.length ? `只用 ${defaults.models.join("、")}` : "不限定"}）`}
            </dd>
          </div>
        </dl>
      </section>
    </>
  );
}

export function ProjectDetail({ id }: { id: string }) {
  const router = useRouter();
  const options = useOptions();
  const [project, setProject] = useState<Project | null>(null);
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(0);
  const [editing, setEditing] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState("");
  const [notice, setNotice] = useState("");
  useEffect(() => {
    const controller = new AbortController();
    getProject(id, controller.signal)
      .then((value) => {
        if (controller.signal.aborted) return;
        setProject(value);
        setError("");
      })
      .catch((err) => {
        if (!controller.signal.aborted) setError(errorMessage(err));
      });
    return () => controller.abort();
  }, [id, revision]);

  async function remove() {
    if (!project || deleting) return;
    if (
      !window.confirm(
        `删除项目「${project.name}」？已有的运行保存了项目的快照，不受影响；之后不能再用它新建运行。`,
      )
    )
      return;
    setDeleting(true);
    setDeleteError("");
    try {
      await deleteProject(project.project_id);
      router.push("/projects");
    } catch (err) {
      setDeleteError(errorMessage(err));
      setDeleting(false);
    }
  }

  return (
    <div className="asset-page">
      <Link className="back-link" href="/projects">
        <Icon name="back" size={16} />
        返回项目
      </Link>
      {error && <ErrorNotice message={error} onRetry={() => setRevision((n) => n + 1)} />}
      {!project && !error && <AssetLoading />}
      {project && (
        <>
          <AssetHeading
            eyebrow="项目"
            title={project.name}
            action={
              !editing && (
                <div className="asset-actions">
                  <button
                    type="button"
                    className="button primary"
                    onClick={() => {
                      setEditing(true);
                      setNotice("");
                    }}
                  >
                    编辑
                  </button>
                  <button
                    type="button"
                    className="button secondary"
                    disabled={deleting}
                    onClick={() => void remove()}
                  >
                    {deleting ? "正在删除…" : "删除"}
                  </button>
                </div>
              )
            }
          >
            {project.description}
          </AssetHeading>
          {deleteError && <ErrorNotice message={deleteError} />}
          <p className={notice ? "copy-notice" : "sr-only"} role="status" aria-live="polite">
            {notice}
          </p>
          {editing ? (
            <ProjectForm
              mode="edit"
              initial={projectInput(project)}
              options={options}
              onCancel={() => setEditing(false)}
              onSaved={(saved) => {
                setProject(saved);
                setEditing(false);
                setNotice("已保存。之后新建的运行按新的设置。");
              }}
            />
          ) : (
            <ProjectView project={project} options={options} />
          )}
        </>
      )}
    </div>
  );
}
