"use client";

import { useT } from "../lib/i18n/client";
import Link from "next/link";
import { useEffect, useState } from "react";
import { listProjects, type ProjectSummary } from "../lib/projects";
import { errorMessage } from "../lib/runs";
import { AssetHeading, AssetLoading, AssetTime } from "./asset-ui";
import { ErrorNotice } from "./ui";

export function ProjectList() {
  const t = useT();
  const [items, setItems] = useState<ProjectSummary[] | null>(null);
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    listProjects(controller.signal)
      .then((value) => {
        if (controller.signal.aborted) return;
        setItems(value);
        setError("");
      })
      .catch((err) => {
        if (!controller.signal.aborted) setError(errorMessage(err));
      });
    return () => controller.abort();
  }, [revision]);
  return (
    <div className="asset-page">
      <AssetHeading
        title={t("项目")}
        action={
          <Link className="button primary" href="/projects/new">
            {t("新建项目")}
          </Link>
        }
      >
        {t(
          "项目定下运行写到哪里、读哪些表、要填哪些参数；任何应用的运行都可以绑定一个项目。",
        )}
      </AssetHeading>
      {error && (
        <ErrorNotice
          message={error}
          onRetry={() => setRevision((n) => n + 1)}
        />
      )}
      {!items && !error && <AssetLoading />}
      {items && (
        <section
          className="panel asset-section"
          aria-labelledby="project-list-title"
        >
          <h2 id="project-list-title">{t("全部项目")}</h2>
          {items.length ? (
            <div className="table-scroll">
              <table className="mapping-table" aria-label={t("项目")}>
                <thead>
                  <tr>
                    <th scope="col">{t("名称")}</th>
                    <th scope="col">{t("说明")}</th>
                    <th scope="col">{t("环境")}</th>
                    <th scope="col">{t("更新时间")}</th>
                  </tr>
                </thead>
                <tbody>
                  {items.map((project) => (
                    <tr key={project.project_id}>
                      <td>
                        <Link
                          href={`/projects/${encodeURIComponent(project.project_id)}`}
                        >
                          {project.name}
                        </Link>
                        <small className="monospace project-id">
                          {project.project_id}
                        </small>
                      </td>
                      <td>{project.description || "—"}</td>
                      <td>
                        {project.environments
                          .map((env) => env.label)
                          .join(t("、"))}
                      </td>
                      <td>
                        <AssetTime value={project.updated_at} />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <p className="muted">
              {t(
                "还没有项目。要让运行写到固定的位置、填运行参数时，先新建一个项目。",
              )}
            </p>
          )}
        </section>
      )}
    </div>
  );
}
