"use client";

import { useId, useState, type ReactNode } from "react";
import {
  agentAsFlow,
  isAgentConfig,
  type AgentAppConfig,
  type AgentNode,
  type AppDetail,
  type FlowNode,
} from "../lib/apps";
import { useT } from "../lib/i18n/client";
import { ChatThread } from "./chat";
import { FlowEnv } from "./flow-env";
import { CheckField, FieldIssues, NumberField } from "./flow-fields";
import {
  AgentAppsField,
  AgentKnowledgeFields,
  AgentTools,
  ModelFields,
} from "./flow-node-form";

// Problems the API found in an agent app's configuration, by where they are:
// "agent.apps.0.when", "chat.suggestions.1", "env.vars.0.key".
export type AgentIssues = Record<string, string>;
export function agentIssues(
  issues: { loc: (string | number)[]; msg: string }[],
): { fields: AgentIssues; other: string[] } {
  const fields: AgentIssues = {};
  const other: string[] = [];
  for (const { loc, msg } of issues) {
    if (loc[0] === "body" && loc[1] === "config" && loc.length > 2) {
      const key = loc.slice(2).join(".");
      fields[key] = fields[key] ? `${fields[key]} · ${msg}` : msg;
    } else other.push(msg);
  }
  return { fields, other };
}
const under = (issues: AgentIssues, prefix: string) =>
  Object.fromEntries(
    Object.entries(issues)
      .filter(([key]) => key.startsWith(`${prefix}.`))
      .map(([key, msg]) => [key.slice(prefix.length + 1), msg]),
  );
const at = (issues: AgentIssues, path: string) =>
  Object.entries(issues)
    .filter(([key]) => key === path || key.startsWith(`${path}.`))
    .map(([, msg]) => msg);

function Section({
  title,
  help,
  children,
}: {
  title: string;
  help?: string;
  children: ReactNode;
}) {
  const id = useId();
  return (
    <section className="agent-section" aria-labelledby={id}>
      <h3 id={id}>{title}</h3>
      {help && <p className="muted">{help}</p>}
      {children}
    </section>
  );
}

// The form an agent app is built with (docs/api/agent-apps.md, "页面"): 角色和指令 ·
// 模型 · 知识和工具 · 对话 · 高级. Each setting is in one place only.
export function AgentForm({
  app,
  data,
  config,
  setConfig,
  issues,
  setSecrets,
  disabled,
}: {
  app: string;
  data: AppDetail;
  config: AgentAppConfig;
  setConfig: (config: AgentAppConfig) => void;
  issues: AgentIssues;
  setSecrets: (secrets: AppDetail["secrets"]) => void;
  disabled: boolean;
}) {
  const t = useT();
  const flow = agentAsFlow(config);
  const node = flow.graph.nodes[1] as AgentNode;
  const agentIssues = under(issues, "agent");
  const setAgent = (changes: Partial<AgentAppConfig["agent"]>) =>
    setConfig({ ...config, agent: { ...config.agent, ...changes } });
  // The node fields shared with flow apps' agent nodes write back here.
  const fromNode = (next: FlowNode) => {
    if (next.type !== "agent") return;
    setAgent({
      model: next.model,
      skills: next.skills,
      tools: next.tools,
      knowledge: next.knowledge ?? null,
      apps: next.apps ?? [],
      grounded_only: next.grounded_only ?? true,
      max_steps: next.max_steps,
    });
  };
  const instructionsId = useId();
  const openingId = useId();
  const chat = config.chat;
  const setChat = (changes: Partial<AgentAppConfig["chat"]>) =>
    setConfig({ ...config, chat: { ...chat, ...changes } });
  const grounding =
    config.agent.tools.length > 0 ||
    Boolean(config.agent.knowledge) ||
    config.agent.apps.some((item) => item.enabled !== false);
  return (
    <fieldset className="agent-form" disabled={disabled}>
      <legend className="sr-only">{t("智能体设置")}</legend>
      <Section
        title={t("角色和指令")}
        help={t("写它是谁、帮谁做什么、怎么回答。会放进每一轮的系统提示词。")}
      >
        <div className="asset-field">
          <label htmlFor={instructionsId}>{t("指令")}</label>
          <textarea
            id={instructionsId}
            rows={8}
            maxLength={20000}
            value={config.agent.instructions}
            aria-invalid={at(agentIssues, "instructions").length > 0}
            onChange={(event) => setAgent({ instructions: event.target.value })}
          />
          <small className="muted">
            {t("可以引用 {{env.名称}}，不能引用密钥。")}
          </small>
          <FieldIssues
            id={`${instructionsId}-issues`}
            messages={at(agentIssues, "instructions")}
          />
        </div>
      </Section>
      <Section title={t("模型")}>
        <ModelFields
          node={node}
          options={data.options}
          change={fromNode}
          issues={agentIssues}
        />
      </Section>
      <Section
        title={t("知识和工具")}
        help={t("都不选就是只靠指令和 Skill 聊天的助手。")}
      >
        <AgentTools
          node={node}
          config={flow}
          options={data.options}
          change={fromNode}
        />
        <FieldIssues
          id={`${instructionsId}-tools`}
          messages={at(agentIssues, "tools")}
        />
        <AgentKnowledgeFields
          node={node}
          options={data.options}
          change={fromNode}
          at={(path) => at(agentIssues, path)}
        />
        <AgentAppsField
          value={config.agent.apps}
          choices={data.options.apps ?? []}
          issues={agentIssues}
          onChange={(apps) => setAgent({ apps })}
        />
        <CheckField
          label={t("只依据资料回答")}
          checked={config.agent.grounded_only}
          help={
            grounding
              ? t(
                  "开着时，一轮里没有调成功工具、回答里也没有标出资料出处，平台就把回答换成「没有在已接入的资料和数据里找到依据」。",
                )
              : t("没接任何知识库、工具或应用时不生效。")
          }
          issues={at(agentIssues, "grounded_only")}
          onChange={(grounded_only) => setAgent({ grounded_only })}
        />
      </Section>
      <Section title={t("对话")}>
        <div className="asset-field">
          <label htmlFor={openingId}>{t("开场白")}</label>
          <textarea
            id={openingId}
            rows={2}
            maxLength={500}
            value={chat.opening}
            placeholder={t("例如：你好，我可以回答报销相关的问题。")}
            onChange={(event) => setChat({ opening: event.target.value })}
          />
          <FieldIssues
            id={`${openingId}-issues`}
            messages={at(issues, "chat.opening")}
          />
        </div>
        <Suggestions
          value={chat.suggestions}
          issues={issues}
          onChange={(suggestions) => setChat({ suggestions })}
        />
        <NumberField
          label={t("带几轮历史")}
          min={0}
          max={50}
          value={config.agent.history_turns}
          issues={at(agentIssues, "history_turns")}
          help={t("每轮把同一会话最近几轮的问答带给模型；0 就是不记上下文。")}
          onChange={(history_turns) => setAgent({ history_turns })}
        />
      </Section>
      <Section title={t("高级")}>
        <NumberField
          label={t("每轮最多几步")}
          min={1}
          max={20}
          value={config.agent.max_steps}
          issues={at(agentIssues, "max_steps")}
          help={t("一次工具调用、一次检索、一次调用应用各算一步。")}
          onChange={(max_steps) => setAgent({ max_steps })}
        />
        <FlowEnv
          app={app}
          config={flow}
          setConfig={(next) => setConfig({ ...config, env: next.env })}
          secrets={data.secrets}
          setSecrets={setSecrets}
          issues={under(issues, "env")}
          disabled={disabled}
        />
      </Section>
    </fieldset>
  );
}

function Suggestions({
  value,
  issues,
  onChange,
}: {
  value: string[];
  issues: AgentIssues;
  onChange: (value: string[]) => void;
}) {
  const t = useT();
  return (
    <fieldset className="list-editor">
      <legend>{t("推荐问题")}</legend>
      <p className="muted">
        {t("显示在对话开头，点一下就发出去。最多 5 个。")}
      </p>
      {value.map((question, index) => (
        <div className="list-row" key={index}>
          <div className="list-row-fields">
            <label className="sr-only" htmlFor={`suggestion-${index}`}>
              {t("推荐问题 {n}", { n: index + 1 })}
            </label>
            <input
              id={`suggestion-${index}`}
              maxLength={100}
              value={question}
              aria-invalid={at(issues, `chat.suggestions.${index}`).length > 0}
              onChange={(event) =>
                onChange(
                  value.map((item, i) =>
                    i === index ? event.target.value : item,
                  ),
                )
              }
            />
            <FieldIssues
              id={`suggestion-${index}-issues`}
              messages={at(issues, `chat.suggestions.${index}`)}
            />
          </div>
          <button
            type="button"
            className="text-button danger-text"
            aria-label={t("删除推荐问题 {n}", { n: index + 1 })}
            onClick={() => onChange(value.filter((_, i) => i !== index))}
          >
            {t("删除")}
          </button>
        </div>
      ))}
      {value.length < 5 && (
        <button
          type="button"
          className="button secondary"
          onClick={() => onChange([...value, ""])}
        >
          {t("添加推荐问题")}
        </button>
      )}
    </fieldset>
  );
}

// The design tab's chat (docs/api/agent-apps.md, "页面"): it runs the saved draft, or the
// current version while there is no draft. A new chat starts when that changes.
export function DebugChat({
  app,
  data,
  dirty,
  onCaseSaved,
}: {
  app: string;
  data: AppDetail;
  dirty: boolean;
  onCaseSaved?: () => void;
}) {
  const t = useT();
  const [slot, setSlot] = useState(0);
  const draft = Boolean(data.draft);
  const source = data.draft ?? data.current;
  const chat = isAgentConfig(source.config) ? source.config.chat : null;
  return (
    <section className="panel debug-chat" aria-labelledby="debug-chat-title">
      <div className="asset-section-heading">
        <div>
          <h2 id="debug-chat-title">{t("调试对话")}</h2>
          <p className="muted">
            {draft
              ? t("用已保存的草稿回答，改了配置要先保存草稿。")
              : t("还没有草稿，用当前版本 v{version} 回答。", {
                  version: data.current_version,
                })}
          </p>
        </div>
        <button
          type="button"
          className="button secondary"
          onClick={() => setSlot((n) => n + 1)}
        >
          {t("新对话")}
        </button>
      </div>
      <ChatThread
        key={`${draft ? "draft" : "current"}-${slot}`}
        app={app}
        chat={chat}
        conversationId={null}
        draft={draft}
        blocked={dirty ? t("有未保存的修改，保存草稿后再试") : undefined}
        onCaseSaved={onCaseSaved}
      />
    </section>
  );
}
