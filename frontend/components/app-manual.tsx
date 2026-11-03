"use client";

import { useT } from "../lib/i18n/client";
import Link from "next/link";
import { useEffect, useState, type ReactNode } from "react";
import { getManual, type AppManual, type ManualNode } from "../lib/app-manual";
import { kindLabel } from "../lib/datasources";
import { errorMessage } from "../lib/runs";

export function useManual(path: string, revision = "") {
  const [result, setResult] = useState<{
    path: string;
    revision: string;
    data?: AppManual;
    error?: string;
  } | null>(null);
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    getManual(path, controller.signal)
      .then(
        (data) =>
          !controller.signal.aborted && setResult({ path, revision, data }),
      )
      .catch(
        (error) =>
          !controller.signal.aborted &&
          setResult({ path, revision, error: errorMessage(error) }),
      );
    return () => controller.abort();
  }, [path, revision, retry]);
  const current =
    result?.path === path && result.revision === revision ? result : null;
  return {
    manual: current?.data ?? null,
    error: current?.error,
    reload: () => setRetry((value) => value + 1),
  };
}

export function UsesTags({ uses, limit }: { uses: string[]; limit?: number }) {
  const t = useT();
  if (!uses.length) return null;
  const visible = limit === undefined ? uses : uses.slice(0, limit);
  return (
    <span className="uses-tags" aria-label={t("使用的资源")}>
      {visible.map((use, i) => (
        <span className="uses-tag" key={`${use}-${i}`} title={use}>
          {use}
        </span>
      ))}
      {limit !== undefined && uses.length > limit && (
        <span
          className="uses-tag uses-more"
          title={uses.slice(limit).join("、")}
        >
          +{uses.length - limit}
        </span>
      )}
    </span>
  );
}

function UsedBy({ nodes }: { nodes: ManualNode[] }) {
  const t = useT();
  return nodes.length ? (
    <p className="muted manual-used-by">
      {t("用于：{nodes}", {
        nodes: nodes.map((node) => node.title).join(t("、")),
      })}
    </p>
  ) : null;
}
function Group({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="manual-group" aria-label={title}>
      <h3>{title}</h3>
      {children}
    </section>
  );
}

export function AppManualPanel({
  resource,
  defaultOpen = true,
  controls,
  versionLabel,
}: {
  resource: ReturnType<typeof useManual>;
  defaultOpen?: boolean;
  controls?: ReactNode;
  versionLabel?: string;
}) {
  const t = useT();
  const { manual, error, reload } = resource;
  return (
    <details
      className="panel app-manual"
      open={defaultOpen}
      aria-label={t("应用说明书")}
    >
      <summary>
        <strong>{t("应用说明书")}</strong>
        <span className="muted">{t("了解流程与使用的资源")}</span>
      </summary>
      <div className="manual-content">
        {controls}
        {error ? (
          <p className="muted">
            {t("暂时无法加载应用说明书：{error}", { error })}{" "}
            <button type="button" className="text-button" onClick={reload}>
              {t("重新加载说明书")}
            </button>
          </p>
        ) : !manual ? (
          <p role="status" className="muted">
            {t("正在加载应用说明书…")}
          </p>
        ) : (
          <>
            <p className="manual-summary">{manual.summary}</p>
            <span className="app-tag">
              {manual.version === null
                ? t("草稿")
                : t("{label} v{version}", {
                    label: versionLabel ?? t("生效版本"),
                    version: manual.version,
                  })}
            </span>
            {manual.steps.length > 0 && (
              <Group title={t("节点讲解")}>
                <ol className="manual-steps">
                  {manual.steps.map((step) => (
                    <li key={step.id}>
                      <strong>{step.title}</strong>{" "}
                      <span className="app-tag">{step.type_label}</span>
                      {step.note && <p className="manual-note">{step.note}</p>}
                      <UsesTags uses={step.uses} />
                    </li>
                  ))}
                </ol>
              </Group>
            )}
            {manual.skills.length > 0 && (
              <Group title="Skill">
                <ul className="manual-resources">
                  {manual.skills.map((skill) => (
                    <li key={`${skill.name}-${skill.version}`}>
                      <Link href={`/skills/${encodeURIComponent(skill.name)}`}>
                        {skill.name}
                      </Link>{" "}
                      <span className="app-tag">v{skill.version}</span>
                      <p>{skill.description}</p>
                      <UsedBy nodes={skill.nodes} />
                    </li>
                  ))}
                </ul>
              </Group>
            )}
            {manual.knowledge_bases.length > 0 && (
              <Group title={t("知识库")}>
                <ul className="manual-resources">
                  {manual.knowledge_bases.map((kb) => (
                    <li key={kb.kb_id}>
                      <Link href={`/knowledge/${encodeURIComponent(kb.kb_id)}`}>
                        {kb.name}
                      </Link>{" "}
                      <span className="muted">
                        {t("{count} 篇文档", { count: kb.document_count })}
                      </span>
                      <p>{kb.description}</p>
                      <UsedBy nodes={kb.nodes} />
                      {kb.documents.length > 0 && (
                        <details className="manual-documents">
                          <summary>{t("文档文件名列表")}</summary>
                          <ul>
                            {kb.documents.map((doc) => (
                              <li key={doc.document_id}>{doc.filename}</li>
                            ))}
                          </ul>
                        </details>
                      )}
                    </li>
                  ))}
                </ul>
              </Group>
            )}
            {manual.datasources.length > 0 && (
              <Group title={t("数据源")}>
                <ul className="manual-resources">
                  {manual.datasources.map((source, i) => (
                    <li key={source.datasource_id ?? i}>
                      <strong>{source.name}</strong>
                      {source.kind && (
                        <span className="app-tag">
                          {kindLabel(source.kind)}
                        </span>
                      )}
                      <UsedBy nodes={source.nodes} />
                    </li>
                  ))}
                </ul>
              </Group>
            )}
            {manual.models.length > 0 && (
              <Group title={t("模型")}>
                <ul className="manual-resources">
                  {manual.models.map((model) => (
                    <li key={model.alias}>
                      <code>{model.alias}</code>
                      <UsedBy nodes={model.nodes} />
                    </li>
                  ))}
                </ul>
              </Group>
            )}
            {manual.systems.length > 0 && (
              <Group title={t("外部系统")}>
                <ul className="manual-resources">
                  {manual.systems.map((system) => (
                    <li key={system.key}>
                      <strong>{system.name}</strong>
                      <p className="manual-note">{system.detail}</p>
                      <UsedBy nodes={system.nodes} />
                    </li>
                  ))}
                </ul>
              </Group>
            )}
            {manual.human.length > 0 && (
              <Group title={t("人工确认点")}>
                <ul className="manual-resources">
                  {manual.human.map((human) => (
                    <li key={human.id}>
                      <strong>{human.title}</strong>
                      <p className="manual-note">{human.prompt}</p>
                    </li>
                  ))}
                </ul>
              </Group>
            )}
          </>
        )}
      </div>
    </details>
  );
}

export function RunManual({ runId }: { runId: string }) {
  const t = useT();
  const resource = useManual(`/runs/${encodeURIComponent(runId)}/manual`);
  return (
    <AppManualPanel
      resource={resource}
      defaultOpen={false}
      versionLabel={t("运行版本")}
    />
  );
}
