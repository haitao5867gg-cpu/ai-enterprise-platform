"use client";

import type { FlowConfig } from "../lib/apps";
import { useT } from "../lib/i18n/client";
import { CheckField, NumberField, SelectField } from "./flow-fields";

function withoutConversation(config: FlowConfig): FlowConfig {
  const rest = { ...config };
  delete rest.conversation;
  return rest;
}

// Whether people talk to the flow app, turn by turn (docs/api/flow-apps.md, "对话"):
// the start step needs a required text input `message`; one end output is the answer.
export function FlowConversation({
  config,
  setConfig,
  disabled,
}: {
  config: FlowConfig;
  setConfig: (config: FlowConfig) => void;
  disabled: boolean;
}) {
  const t = useT();
  const conversation = config.conversation;
  const enabled = Boolean(conversation?.enabled);
  const start = config.graph.nodes.find((node) => node.type === "start");
  const message =
    start?.type === "start"
      ? start.inputs.find((input) => input.key === "message")
      : undefined;
  const outputs = [
    ...new Set(
      config.graph.nodes.flatMap((node) =>
        node.type === "end" ? node.outputs.map((output) => output.key) : [],
      ),
    ),
  ];
  const set = (changes: Partial<NonNullable<FlowConfig["conversation"]>>) =>
    setConfig({
      ...config,
      conversation: {
        enabled,
        history_turns: conversation?.history_turns ?? 10,
        reply: conversation?.reply ?? outputs[0] ?? "reply",
        ...changes,
      },
    });
  return (
    <section
      className="panel asset-section flow-conversation"
      aria-labelledby="flow-conversation-title"
    >
      <h2 id="flow-conversation-title">{t("对话")}</h2>
      <p className="muted">
        {t(
          "打开后，使用页显示成聊天，每发一条消息跑一次这个应用；模型和智能体步骤可以勾「带上对话历史」，提示词里可以引用 {{conversation.history}}。",
        )}
      </p>
      <fieldset disabled={disabled} className="flow-conversation-fields">
        <legend className="sr-only">{t("对话设置")}</legend>
        <CheckField
          label={t("能多轮对话")}
          checked={enabled}
          help={
            enabled &&
            !(
              message?.required &&
              (message.type === "text" || message.type === "textarea")
            )
              ? t("开始步骤要有一个必填的文字输入，标识为 message。")
              : undefined
          }
          onChange={(on) =>
            on
              ? set({ enabled: true })
              : setConfig(withoutConversation(config))
          }
        />
        {enabled && (
          <>
            <NumberField
              label={t("带几轮历史")}
              min={0}
              max={50}
              value={conversation?.history_turns ?? 10}
              onChange={(history_turns) => set({ history_turns })}
            />
            <SelectField
              label={t("哪个结束输出是这一轮的回答")}
              value={conversation?.reply ?? ""}
              choices={(outputs.length ? outputs : ["reply"]).map((key) => ({
                value: key,
                label: key,
              }))}
              onChange={(reply) => set({ reply })}
            />
          </>
        )}
      </fieldset>
    </section>
  );
}
