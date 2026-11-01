"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { listProjects, type ProjectSummary } from "../lib/projects";
import { errorMessage } from "../lib/runs";
import { AssetHeading, AssetLoading, AssetTime } from "./asset-ui";
import { ErrorNotice } from "./ui";

export function ProjectList() {
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
        title="项目"
        action={
          <Link className="button primary" href="/projects/new">
            新建项目
          </Link>
        }
      >
        项目说明一组运行在 Databricks
        上写到哪里（环境）、表名用什么前缀、只读引用哪些表、每次运行要填哪些参数，以及发给模型的数据按什么程度外发。任何应用的运行都可以绑定一个项目。
      </AssetHeading>
      {error && (
        <ErrorNotice
          message={error}
          onRetry={() => setRevision((n) => n + 1)}
        />
      )}
      {!items && !error && <AssetLoading />}
      {items && (
        <section className="panel asset-section" aria-labelledby="project-list-title">
          <h2 id="project-list-title">全部项目</h2>
          {items.length ? (
            <div className="table-scroll">
              <table className="mapping-table" aria-label="项目">
                <thead>
                  <tr>
                    {["名称", "标识", "说明", "环境", "更新时间"].map((heading) => (
                      <th scope="col" key={heading}>
                        {heading}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {items.map((project) => (
                    <tr key={project.project_id}>
                      <td>
                        <Link href={`/projects/${encodeURIComponent(project.project_id)}`}>
                          {project.name}
                        </Link>
                      </td>
                      <td className="monospace">{project.project_id}</td>
                      <td>{project.description || "—"}</td>
                      <td>
                        {project.environments.map((env) => env.label).join("、")}
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
              还没有项目。要让运行写到固定的位置、填运行参数时，先新建一个项目。
            </p>
          )}
        </section>
      )}
    </div>
  );
}
