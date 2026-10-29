import "server-only";
import type { AppConfig, FlowNode } from "../apps";
import { isFlowConfig } from "../apps";
import type { AppManual, ManualNode } from "../app-manual";
import { kindLabels } from "../flow";
import { nodeKind, typeLabels } from "../flow-edit";
import { mockFlow } from "./mock-flow";
import { mockDataSources } from "./mock-etl";
import { mockUploads } from "./mock-uploads";
import { mockKnowledgeManual } from "./mock-knowledge";
import { mockSkillOptions, mockSkillVersionExists } from "./mock-skills";

export function buildMockManual(
  app: string,
  title: string,
  summary: string,
  version: number | null,
  config: AppConfig,
): AppManual {
  const manual: AppManual = {
    app,
    title,
    summary,
    version,
    kind: isFlowConfig(config) ? "flow" : "code",
    steps: [],
    skills: [],
    knowledge_bases: [],
    datasources: [],
    models: [],
    systems: [],
    human: [],
  };
  const flow = isFlowConfig(config) ? config : null;
  const ordered: FlowNode[] = [];
  if (flow) {
    const queue = flow.graph.nodes
      .filter((node) => node.type === "start")
      .map((node) => node.id);
    const seen = new Set<string>();
    while (queue.length) {
      const id = queue.shift()!;
      if (seen.has(id)) continue;
      seen.add(id);
      const node = flow.graph.nodes.find((node) => node.id === id);
      if (!node) continue;
      ordered.push(node);
      queue.push(
        ...flow.graph.edges
          .filter((edge) => edge.from === id)
          .map((edge) => edge.to),
      );
    }
  }
  const nodes = flow
    ? ordered.map((node) => ({
        id: node.id,
        title: node.title,
        type: node.type,
        kind: nodeKind(node.type),
        note: node.note ?? null,
      }))
    : mockFlow(app, [], true)
        .nodes.filter((node) => !node.id.startsWith("__"))
        .map((node) => ({
          id: node.id,
          title: node.title,
          type: node.type ?? null,
          kind: node.kind,
          note: node.design?.summary ?? null,
        }));
  for (const node of nodes) {
    const ref: ManualNode = { id: node.id, title: node.title };
    const setting = flow
      ? ordered.find((item) => item.id === node.id)
      : !isFlowConfig(config)
        ? config.nodes[node.id]
        : undefined;
    const uses: string[] = [];
    if (setting && "skills" in setting)
      for (const skill of setting.skills) {
        let item = manual.skills.find(
          (item) => item.name === skill.name && item.version === skill.version,
        );
        if (!item) {
          item = {
            ...skill,
            description: mockSkillVersionExists(skill.name, skill.version)
              ? (mockSkillOptions().find((item) => item.name === skill.name)
                  ?.description ?? "")
              : "已删除",
            nodes: [],
          };
          manual.skills.push(item);
        }
        item.nodes.push(ref);
        uses.push(`Skill ${skill.name} v${skill.version}`);
      }
    if (node.kind === "model" && setting && "model" in setting) {
      const alias = setting.model ?? "mock";
      let item = manual.models.find((item) => item.alias === alias);
      if (!item) manual.models.push((item = { alias, nodes: [] }));
      item.nodes.push(ref);
      uses.push(`模型 ${alias}`);
    }
    const knowledge =
      setting && "type" in setting
        ? setting.type === "knowledge"
          ? setting.knowledge_bases
          : setting.type === "agent"
            ? setting.knowledge?.knowledge_bases
            : []
        : setting &&
            "params" in setting &&
            Array.isArray(setting.params.knowledge_bases)
          ? setting.params.knowledge_bases
          : [];
    for (const id of knowledge ?? []) {
      let item = manual.knowledge_bases.find((item) => item.kb_id === id);
      if (!item)
        manual.knowledge_bases.push(
          (item = { ...mockKnowledgeManual(id), nodes: [] }),
        );
      item.nodes.push(ref);
      uses.push(`知识库 ${item.name}`);
    }
    const sources =
      setting && "type" in setting
        ? "datasource" in setting
          ? [setting.datasource]
          : setting.type === "agent"
            ? setting.tools.map((tool) => tool.datasource)
            : []
        : app === "etl" && node.id === "profile"
          ? ["{{start.datasource}}"]
          : [];
    for (const id of [...new Set(sources)]) {
      const runtime = id.includes("{{");
      const inputKey = id.match(
        /\{\{\s*[a-z][a-z0-9_]*\.([a-z][a-z0-9_]*)\s*\}\}/,
      )?.[1];
      const start = flow?.graph.nodes.find((node) => node.type === "start");
      const input = start?.inputs.find((input) => input.key === inputKey);
      const source = [
        ...mockDataSources,
        ...[...mockUploads.values()].map((item) => item.datasource),
      ].find((source) => source.datasource_id === id);
      const name = runtime
        ? `运行时选择（开始节点的「${input?.label ?? "数据源"}」）`
        : (source?.name ?? id);
      let item = manual.datasources.find(
        (item) =>
          item.datasource_id === (runtime ? null : id) && item.name === name,
      );
      if (!item)
        manual.datasources.push(
          (item = {
            datasource_id: runtime ? null : id,
            name,
            kind: runtime ? null : (source?.kind ?? null),
            nodes: [],
          }),
        );
      item.nodes.push(ref);
      uses.push(`数据源 ${name}`);
    }
    if (
      node.type?.startsWith("databricks_") ||
      (app === "etl" && node.id === "verify")
    ) {
      let system = manual.systems.find((system) => system.key === "databricks");
      if (!system)
        manual.systems.push(
          (system = {
            key: "databricks",
            name: "Databricks",
            detail:
              "模拟工作区；源表 catalog demo_source；结果写到 demo.analytics\n运行方式：Job（模拟集群）",
            nodes: [],
          }),
        );
      system.nodes.push(ref);
      uses.push("Databricks");
    }
    if (node.kind === "human")
      manual.human.push({
        ...ref,
        prompt:
          setting && "prompt" in setting
            ? setting.prompt
            : (node.note ?? "请确认"),
      });
    manual.steps.push({
      ...node,
      type_label: node.type ? typeLabels[node.type] : kindLabels[node.kind],
      uses,
    });
  }
  return manual;
}
