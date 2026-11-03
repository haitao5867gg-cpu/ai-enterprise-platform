import type { ReactNode } from "react";
import { useT } from "../lib/i18n/client";
import { kindLabels, type FlowNode } from "../lib/flow";

export function NodeNote({ note }: { note?: string | null }) {
  const t = useT();
  return note ? (
    <section className="node-note" aria-label={t("讲解说明")}>
      <h4>{t("讲解说明")}</h4>
      <p>{note}</p>
    </section>
  ) : null;
}

export function FlowNodeDesign({ node }: { node: FlowNode }) {
  const t = useT();
  const design = node.design;
  const facts: [string, ReactNode][] = design
    ? [
        [t("读取"), design.inputs],
        [t("产出"), design.outputs],
        [
          t("工具"),
          design.tools.length > 0 ? (
            <ul>
              {design.tools.map((tool) => (
                <li key={tool}>{tool}</li>
              ))}
            </ul>
          ) : null,
        ],
        [t("模型#字段"), node.model ? <code>{node.model}</code> : null],
        [t("知识来源"), design.knowledge],
        [t("校验"), design.checks],
        [t("重试"), design.retry],
      ]
    : [];
  return (
    <>
      <div className="flow-detail-heading">
        <span className={`flow-kind ${node.kind}`}>
          {t(kindLabels[node.kind])}
        </span>
        <code>{node.id}</code>
      </div>
      <h3>{node.title}</h3>
      <NodeNote note={design?.note} />
      {design ? (
        <p className="flow-summary">{design.summary}</p>
      ) : (
        <p className="flow-summary muted">{t("这个节点没有填写设计说明。")}</p>
      )}
      <dl className="flow-facts">
        {facts
          .filter(([, value]) => value)
          .map(([label, value]) => (
            <div key={label}>
              <dt>{label}</dt>
              <dd>{value}</dd>
            </div>
          ))}
      </dl>
      {design?.prompt && (
        <details className="flow-prompt">
          <summary>{t("查看提示词全文")}</summary>
          <pre className="flow-json">{design.prompt}</pre>
        </details>
      )}
    </>
  );
}
