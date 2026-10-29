"use client";

import { KnowledgePicker } from "./knowledge-form";
import { RetrievalFields } from "./retrieval-fields";

import { useEffect, useState } from "react";
import type {
  AgentNode,
  AppDetail,
  ClassifierNode,
  Condition,
  ConditionNode,
  FlowConfig,
  FlowNode,
  FormField,
  HumanNode,
  Input,
  InputType,
  OutputField,
  ToolNode,
  ValueType,
} from "../lib/apps";
import { issuesUnder, variablesFor, type Variable } from "../lib/flow-edit";
import { errorMessage, requestJson } from "../lib/runs";
import { SkillPicker } from "./app-settings";
import {
  CheckField,
  ListEditor,
  NumberField,
  RefField,
  SelectField,
  TextField,
} from "./flow-fields";

export type Change = (
  node: FlowNode,
  rename?: { from: string; to: string },
) => void;
type Options = AppDetail["options"];
type Issues = Record<string, string> | undefined;
type Tool = {
  name: string;
  description?: string | null;
  input_schema?: {
    properties?: Record<
      string,
      { type?: string; description?: string; title?: string }
    >;
    required?: string[];
  };
};

const valueTypes: { value: ValueType; label: string }[] = [
  { value: "string", label: "文字" },
  { value: "number", label: "数字" },
  { value: "boolean", label: "是/否" },
  { value: "array", label: "列表" },
  { value: "object", label: "对象" },
];
const inputTypes: { value: InputType; label: string }[] = [
  { value: "text", label: "单行文字" },
  { value: "textarea", label: "多行文字" },
  { value: "number", label: "数字" },
  { value: "boolean", label: "是/否" },
  { value: "select", label: "下拉选择" },
  { value: "datasource", label: "数据源" },
];
const operators: { value: Condition["op"]; label: string }[] = [
  { value: "eq", label: "等于" },
  { value: "ne", label: "不等于" },
  { value: "contains", label: "包含" },
  { value: "not_contains", label: "不包含" },
  { value: "gt", label: "大于" },
  { value: "gte", label: "大于等于" },
  { value: "lt", label: "小于" },
  { value: "lte", label: "小于等于" },
  { value: "empty", label: "为空" },
  { value: "not_empty", label: "不为空" },
];
const text = (value: unknown) =>
  typeof value === "string"
    ? value
    : value === undefined
      ? ""
      : JSON.stringify(value);

const toolCache = new Map<string, Promise<Tool[]>>();
function useTools(datasource: string) {
  const literal = Boolean(datasource) && !datasource.includes("{{");
  const [state, setState] = useState<{
    id: string;
    tools?: Tool[];
    error?: string;
  }>({ id: "" });
  useEffect(() => {
    if (!literal) return;
    let live = true;
    let request = toolCache.get(datasource);
    if (!request) {
      request = requestJson(
        `/datasources/${encodeURIComponent(datasource)}/tools`,
      ).then((value) => (Array.isArray(value) ? (value as Tool[]) : []));
      toolCache.set(datasource, request);
      request.catch(() => toolCache.delete(datasource));
    }
    request
      .then((tools) => live && setState({ id: datasource, tools }))
      .catch(
        (err) => live && setState({ id: datasource, error: errorMessage(err) }),
      );
    return () => {
      live = false;
    };
  }, [datasource, literal]);
  if (!literal || state.id !== datasource) return { loading: literal };
  return { tools: state.tools, error: state.error, loading: false };
}

function datasourceChoices(config: FlowConfig, options: Options) {
  const start = config.graph.nodes.find((node) => node.type === "start");
  return [
    ...options.datasources.map((item) => ({
      value: item.datasource_id,
      label: `${item.name} · ${item.kind}`,
    })),
    ...(start?.type === "start" ? start.inputs : [])
      .filter((input) => input.type === "datasource")
      .map((input) => ({
        value: `{{start.${input.key}}}`,
        label: `运行时选择：${input.label}（{{start.${input.key}}}）`,
      })),
  ];
}

function ModelFields({
  node,
  options,
  change,
  issues,
}: {
  node: Extract<FlowNode, { model: string | null }>;
  options: Options;
  change: Change;
  issues: Issues;
}) {
  return (
    <>
      <SelectField
        label="模型"
        value={node.model ?? ""}
        issues={issuesUnder(issues, "model")}
        onChange={(model) => change({ ...node, model: model || null })}
        choices={[
          { value: "", label: `平台默认（${options.default_model}）` },
          ...[
            ...new Set([
              ...options.models,
              ...(node.model ? [node.model] : []),
            ]),
          ].map((model) => ({
            value: model,
            label: options.models.includes(model)
              ? model
              : `${model}（网关上没有）`,
          })),
        ]}
      />
      <div className="asset-field">
        <span>Skill</span>
        <SkillPicker
          options={options.skills}
          value={node.skills}
          onChange={(skills) => change({ ...node, skills })}
        />
        {issuesUnder(issues, "skills").map((msg) => (
          <p className="field-error" key={msg}>
            {msg}
          </p>
        ))}
      </div>
    </>
  );
}

function OutputFields({
  label,
  fields,
  onChange,
  issues,
  prefix,
  describe = false,
}: {
  label: string;
  fields: OutputField[];
  onChange: (fields: OutputField[]) => void;
  issues: Issues;
  prefix: string;
  describe?: boolean;
}) {
  return (
    <ListEditor
      label={label}
      items={fields}
      onChange={onChange}
      min={1}
      make={(): OutputField => ({
        key: `field_${fields.length + 1}`,
        type: "string",
      })}
      issues={(i) => issuesUnder(issues, `${prefix}.${i}`)}
      render={(field, _i, update) => (
        <>
          <TextField
            label="字段名"
            value={field.key}
            onChange={(key) => update({ ...field, key })}
          />
          <SelectField
            label="类型"
            value={field.type}
            choices={valueTypes}
            onChange={(type) => update({ ...field, type: type as ValueType })}
          />
          {describe && (
            <TextField
              label="说明"
              value={field.description ?? ""}
              onChange={(description) => update({ ...field, description })}
            />
          )}
        </>
      )}
    />
  );
}

function InputList<T extends Input>({
  label,
  inputs,
  onChange,
  issues,
  prefix,
  allowDatasource,
}: {
  label: string;
  inputs: T[];
  onChange: (inputs: T[]) => void;
  issues: Issues;
  prefix: string;
  allowDatasource: boolean;
}) {
  return (
    <ListEditor
      label={label}
      items={inputs}
      onChange={onChange}
      min={1}
      make={() =>
        ({
          key: `field_${inputs.length + 1}`,
          label: "新字段",
          type: "text",
          required: false,
        }) as T
      }
      issues={(i) => issuesUnder(issues, `${prefix}.${i}`)}
      render={(input, _i, update) => (
        <>
          <TextField
            label="字段名"
            value={input.key}
            onChange={(key) => update({ ...input, key })}
          />
          <TextField
            label="显示名称"
            value={input.label}
            onChange={(value) => update({ ...input, label: value })}
          />
          <SelectField
            label="类型"
            value={input.type}
            choices={inputTypes.filter(
              (item) => allowDatasource || item.value !== "datasource",
            )}
            onChange={(type) => {
              const next = { ...input, type: type as InputType };
              if (type !== "select") delete next.choices;
              else next.choices ??= [{ value: "a", label: "选项 A" }];
              if (type !== "text" && type !== "textarea")
                delete next.max_length;
              update(next);
            }}
          />
          <CheckField
            label="必填"
            checked={Boolean(input.required)}
            onChange={(required) => update({ ...input, required })}
          />
          {(input.type === "text" || input.type === "textarea") && (
            <NumberField
              label="最多字数"
              min={1}
              max={20000}
              value={input.max_length}
              onChange={(max_length) => update({ ...input, max_length })}
            />
          )}
          {input.type === "select" && (
            <ListEditor
              label="选项"
              items={input.choices ?? []}
              min={1}
              make={() => ({
                value: `option_${(input.choices?.length ?? 0) + 1}`,
                label: "新选项",
              })}
              onChange={(choices) => update({ ...input, choices })}
              render={(choice, _j, set) => (
                <>
                  <TextField
                    label="值"
                    value={choice.value}
                    onChange={(value) => set({ ...choice, value })}
                  />
                  <TextField
                    label="显示文字"
                    value={choice.label}
                    onChange={(value) => set({ ...choice, label: value })}
                  />
                </>
              )}
            />
          )}
        </>
      )}
    />
  );
}

function ToolFields({
  node,
  config,
  options,
  change,
  issues,
  variables,
}: {
  node: ToolNode;
  config: FlowConfig;
  options: Options;
  change: Change;
  issues: Issues;
  variables: Variable[];
}) {
  const { tools, error, loading } = useTools(node.datasource);
  const choices = datasourceChoices(config, options);
  const tool = tools?.find((item) => item.name === node.tool);
  const properties = tool?.input_schema?.properties ?? {};
  const required = tool?.input_schema?.required ?? [];
  const keys = tools ? Object.keys(properties) : Object.keys(node.args);
  function setArg(key: string, value: string) {
    const args = { ...node.args };
    const type = properties[key]?.type;
    if (!value && tools && !required.includes(key)) delete args[key];
    else if (
      (type === "integer" || type === "number") &&
      /^-?\d+(\.\d+)?$/.test(value)
    )
      args[key] = Number(value);
    else if (type === "boolean" && (value === "true" || value === "false"))
      args[key] = value === "true";
    else args[key] = value;
    change({ ...node, args });
  }
  return (
    <>
      <SelectField
        label="数据源"
        value={node.datasource}
        issues={issuesUnder(issues, "datasource")}
        onChange={(datasource) =>
          change({ ...node, datasource, tool: "", args: {} })
        }
        choices={[
          { value: "", label: "请选择数据源" },
          ...choices,
          ...(node.datasource &&
          !choices.some((item) => item.value === node.datasource)
            ? [
                {
                  value: node.datasource,
                  label: `${node.datasource}（未登记）`,
                },
              ]
            : []),
        ]}
      />
      {tools ? (
        <SelectField
          label="工具"
          value={node.tool}
          issues={issuesUnder(issues, "tool")}
          onChange={(name) => change({ ...node, tool: name, args: {} })}
          help={tool?.description ?? undefined}
          choices={[
            { value: "", label: "请选择工具" },
            ...tools.map((item) => ({ value: item.name, label: item.name })),
            ...(node.tool && !tool
              ? [{ value: node.tool, label: `${node.tool}（数据源上没有）` }]
              : []),
          ]}
        />
      ) : (
        <TextField
          label="工具"
          value={node.tool}
          issues={issuesUnder(issues, "tool")}
          help={
            loading
              ? "正在读取这个数据源的工具…"
              : error
                ? `读取工具失败：${error}`
                : node.datasource
                  ? "数据源在运行时选择，请填写工具名。"
                  : "先选择数据源。"
          }
          onChange={(name) => change({ ...node, tool: name })}
        />
      )}
      {keys.length > 0 && (
        <fieldset className="list-editor">
          <legend>参数</legend>
          {keys.map((key) => (
            <RefField
              key={key}
              label={`${key}${required.includes(key) ? "（必填）" : ""}`}
              value={text(node.args[key])}
              variables={variables}
              help={properties[key]?.description ?? properties[key]?.title}
              issues={issuesUnder(issues, `args.${key}`)}
              onChange={(value) => setArg(key, value)}
            />
          ))}
        </fieldset>
      )}
      {tools && tool && !keys.length && (
        <p className="muted">这个工具没有参数。</p>
      )}
      {!tools && !loading && (
        <AddKey
          label="参数名"
          onAdd={(key) =>
            change({ ...node, args: { ...node.args, [key]: "" } })
          }
        />
      )}
    </>
  );
}

function AddKey({
  label,
  onAdd,
}: {
  label: string;
  onAdd: (key: string) => void;
}) {
  const [key, setKey] = useState("");
  return (
    <div className="add-key">
      <TextField label={label} value={key} onChange={setKey} />
      <button
        type="button"
        className="button secondary"
        disabled={!key.trim()}
        onClick={() => {
          onAdd(key.trim());
          setKey("");
        }}
      >
        添加参数
      </button>
    </div>
  );
}

function AgentTools({
  node,
  config,
  options,
  change,
}: {
  node: AgentNode;
  config: FlowConfig;
  options: Options;
  change: Change;
}) {
  const known = datasourceChoices(config, options);
  // Entries for data sources that are not offered (unset or not registered) stay visible
  // so they can be unticked.
  const choices = [
    ...known,
    ...node.tools
      .filter(
        (item) => !known.some((choice) => choice.value === item.datasource),
      )
      .map((item) => ({
        value: item.datasource,
        label: item.datasource
          ? `${item.datasource}（未登记）`
          : "未选择的数据源",
      })),
  ];
  return (
    <fieldset className="list-editor">
      <legend>允许使用的工具</legend>
      {!choices.length && <p className="muted">平台上还没有登记数据源。</p>}
      {choices.map((choice) => {
        const entry = node.tools.find(
          (item) => item.datasource === choice.value,
        );
        return (
          <div className="list-row" key={choice.value}>
            <div className="list-row-fields">
              <CheckField
                label={choice.label}
                checked={Boolean(entry)}
                onChange={(on) =>
                  change({
                    ...node,
                    tools: on
                      ? [
                          ...node.tools,
                          { datasource: choice.value, tools: null },
                        ]
                      : node.tools.filter(
                          (item) => item.datasource !== choice.value,
                        ),
                  })
                }
              />
              {entry && (
                <AgentToolPicker
                  datasource={choice.value}
                  value={entry.tools}
                  onChange={(tools) =>
                    change({
                      ...node,
                      tools: node.tools.map((item) =>
                        item.datasource === choice.value
                          ? { ...item, tools }
                          : item,
                      ),
                    })
                  }
                />
              )}
            </div>
          </div>
        );
      })}
    </fieldset>
  );
}

function AgentToolPicker({
  datasource,
  value,
  onChange,
}: {
  datasource: string;
  value: string[] | null;
  onChange: (value: string[] | null) => void;
}) {
  const { tools, loading, error } = useTools(datasource);
  return (
    <>
      <CheckField
        label="只允许部分工具"
        checked={value !== null}
        onChange={(some) => onChange(some ? [] : null)}
      />
      {value !== null &&
        (tools ? (
          <div className="check-grid">
            {tools.map((tool) => (
              <CheckField
                key={tool.name}
                label={tool.name}
                checked={value.includes(tool.name)}
                onChange={(on) =>
                  onChange(
                    on
                      ? [...value, tool.name]
                      : value.filter((item) => item !== tool.name),
                  )
                }
              />
            ))}
          </div>
        ) : (
          <TextField
            label="工具名（用逗号分隔）"
            value={value.join(",")}
            help={loading ? "正在读取工具…" : error}
            onChange={(names) =>
              onChange(
                names
                  .split(/[,，]/)
                  .map((item) => item.trim())
                  .filter(Boolean),
              )
            }
          />
        ))}
    </>
  );
}

function ClassifierFields({
  node,
  change,
  issues,
}: {
  node: ClassifierNode;
  change: Change;
  issues: Issues;
}) {
  return (
    <ListEditor
      label="类别"
      items={node.classes}
      min={1}
      max={10}
      onChange={(classes) => change({ ...node, classes })}
      make={() => ({
        key: `class_${node.classes.length + 1}`,
        label: "新类别",
        description: "",
      })}
      issues={(i) => issuesUnder(issues, `classes.${i}`)}
      render={(item, i) => (
        <>
          <TextField
            label="标识"
            value={item.key}
            onChange={(key) =>
              change(
                {
                  ...node,
                  classes: node.classes.map((c, j) =>
                    j === i ? { ...c, key } : c,
                  ),
                },
                { from: item.key, to: key },
              )
            }
          />
          <TextField
            label="名称"
            value={item.label}
            onChange={(label) =>
              change({
                ...node,
                classes: node.classes.map((c, j) =>
                  j === i ? { ...c, label } : c,
                ),
              })
            }
          />
          <TextField
            label="说明"
            value={item.description ?? ""}
            onChange={(description) =>
              change({
                ...node,
                classes: node.classes.map((c, j) =>
                  j === i ? { ...c, description } : c,
                ),
              })
            }
          />
        </>
      )}
    />
  );
}

function ConditionFields({
  node,
  change,
  issues,
  variables,
}: {
  node: ConditionNode;
  change: Change;
  issues: Issues;
  variables: Variable[];
}) {
  const setRule = (
    i: number,
    rule: ConditionNode["rules"][number],
    rename?: string,
  ) =>
    change(
      { ...node, rules: node.rules.map((r, j) => (j === i ? rule : r)) },
      rename !== undefined
        ? { from: node.rules[i].key, to: rename }
        : undefined,
    );
  return (
    <ListEditor
      label="规则"
      items={node.rules}
      min={1}
      max={10}
      onChange={(rules) => change({ ...node, rules })}
      make={(): ConditionNode["rules"][number] => ({
        key: `rule_${node.rules.length + 1}`,
        label: "新规则",
        match: "all",
        conditions: [{ left: "", op: "not_empty" }],
      })}
      issues={(i) => issuesUnder(issues, `rules.${i}`)}
      render={(rule, i) => (
        <>
          <TextField
            label="标识"
            value={rule.key}
            onChange={(key) => setRule(i, { ...rule, key }, key)}
          />
          <TextField
            label="名称"
            value={rule.label}
            onChange={(label) => setRule(i, { ...rule, label })}
          />
          <SelectField
            label="满足"
            value={rule.match}
            choices={[
              { value: "all", label: "全部条件" },
              { value: "any", label: "任一条件" },
            ]}
            onChange={(match) =>
              setRule(i, { ...rule, match: match as "all" | "any" })
            }
          />
          <ListEditor
            label="条件"
            items={rule.conditions}
            min={1}
            max={10}
            onChange={(conditions) => setRule(i, { ...rule, conditions })}
            make={(): Condition => ({ left: "", op: "eq", right: "" })}
            render={(condition, _j, update) => (
              <>
                <RefField
                  label="左边"
                  value={text(condition.left)}
                  variables={variables}
                  onChange={(left) => update({ ...condition, left })}
                />
                <SelectField
                  label="比较"
                  value={condition.op}
                  choices={operators}
                  onChange={(op) => {
                    const next: Condition = {
                      ...condition,
                      op: op as Condition["op"],
                    };
                    if (op === "empty" || op === "not_empty") delete next.right;
                    else next.right ??= "";
                    update(next);
                  }}
                />
                {condition.op !== "empty" && condition.op !== "not_empty" && (
                  <RefField
                    label="右边"
                    value={text(condition.right)}
                    variables={variables}
                    onChange={(right) => update({ ...condition, right })}
                  />
                )}
              </>
            )}
          />
        </>
      )}
    />
  );
}

function HumanFields({
  node,
  change,
  issues,
  variables,
}: {
  node: HumanNode;
  change: Change;
  issues: Issues;
  variables: Variable[];
}) {
  const base = {
    id: node.id,
    type: node.type,
    title: node.title,
    max_visits: node.max_visits,
    ui: node.ui,
    prompt: node.prompt,
    show: node.show,
  };
  return (
    <>
      <SelectField
        label="处理方式"
        value={node.mode}
        choices={[
          { value: "approval", label: "确认：选一个选项" },
          { value: "form", label: "填写：填一张表" },
        ]}
        onChange={(mode) =>
          change(
            mode === "form"
              ? {
                  ...base,
                  mode: "form",
                  fields: [
                    {
                      key: "value",
                      label: "内容",
                      type: "text",
                      required: true,
                    },
                  ],
                }
              : {
                  ...base,
                  mode: "approval",
                  options: [
                    { key: "approve", label: "确认" },
                    { key: "reject", label: "驳回" },
                  ],
                  comment_required: [],
                },
          )
        }
      />
      <RefField
        label="提示"
        value={node.prompt}
        variables={variables}
        issues={issuesUnder(issues, "prompt")}
        onChange={(prompt) => change({ ...node, prompt })}
      />
      <RefField
        label="给处理人看的内容"
        multiline
        value={node.show ?? ""}
        variables={variables}
        issues={issuesUnder(issues, "show")}
        help="按 Markdown 显示，可选。"
        onChange={(show) => change({ ...node, show })}
      />
      {node.mode === "approval" ? (
        <ListEditor
          label="选项"
          items={node.options}
          min={2}
          max={5}
          onChange={(options) =>
            change({
              ...node,
              options,
              comment_required: (node.comment_required ?? []).filter((key) =>
                options.some((option) => option.key === key),
              ),
            })
          }
          make={() => ({
            key: `option_${node.options.length + 1}`,
            label: "新选项",
          })}
          issues={(i) => issuesUnder(issues, `options.${i}`)}
          render={(option, i) => {
            const required = node.comment_required ?? [];
            return (
              <>
                <TextField
                  label="标识"
                  value={option.key}
                  onChange={(key) =>
                    change(
                      {
                        ...node,
                        options: node.options.map((o, j) =>
                          j === i ? { ...o, key } : o,
                        ),
                        comment_required: required.map((item) =>
                          item === option.key ? key : item,
                        ),
                      },
                      { from: option.key, to: key },
                    )
                  }
                />
                <TextField
                  label="按钮文字"
                  value={option.label}
                  onChange={(label) =>
                    change({
                      ...node,
                      options: node.options.map((o, j) =>
                        j === i ? { ...o, label } : o,
                      ),
                    })
                  }
                />
                <CheckField
                  label="必须写意见"
                  checked={required.includes(option.key)}
                  onChange={(on) =>
                    change({
                      ...node,
                      comment_required: on
                        ? [...required, option.key]
                        : required.filter((item) => item !== option.key),
                    })
                  }
                />
              </>
            );
          }}
        />
      ) : (
        <InputList<FormField>
          label="表单字段"
          inputs={node.fields}
          prefix="fields"
          issues={issues}
          allowDatasource={false}
          onChange={(fields) => change({ ...node, fields })}
        />
      )}
    </>
  );
}

export function NodeSettings({
  node,
  config,
  options,
  issues,
  change,
}: {
  node: FlowNode;
  config: FlowConfig;
  options: Options;
  issues: Issues;
  change: Change;
}) {
  const variables = variablesFor(config, node.id);
  const at = (path: string) => issuesUnder(issues, path);
  switch (node.type) {
    case "start":
      return (
        <InputList
          label="输入字段"
          inputs={node.inputs}
          prefix="inputs"
          issues={issues}
          allowDatasource
          onChange={(inputs) => change({ ...node, inputs })}
        />
      );
    case "knowledge":
      return (
        <>
          <fieldset className="list-editor">
            <legend>知识库（1–5 个）</legend>
            <KnowledgePicker
              options={options.knowledge_bases ?? []}
              value={node.knowledge_bases}
              max={5}
              onChange={(knowledge_bases) =>
                change({ ...node, knowledge_bases })
              }
            />
            {at("knowledge_bases").map((message) => (
              <p key={message} className="field-error">
                {message}
              </p>
            ))}
          </fieldset>
          <RefField
            label="检索问题"
            value={node.query}
            multiline
            variables={variables}
            issues={at("query")}
            help="最多 2000 字符，可引用前面节点的变量。"
            onChange={(query) => change({ ...node, query })}
          />
          <NumberField
            label="top_k"
            value={node.top_k}
            min={1}
            max={20}
            issues={at("top_k")}
            onChange={(top_k) => change({ ...node, top_k })}
          />
          <label className="asset-field">
            min_score
            <input
              type="number"
              min={0}
              max={1}
              step="any"
              value={Number.isNaN(node.min_score) ? "" : node.min_score}
              onChange={(event) =>
                change({ ...node, min_score: event.target.valueAsNumber })
              }
            />
            {at("min_score").map((message) => (
              <span key={message} className="field-error">
                {message}
              </span>
            ))}
          </label>
          <RetrievalFields
            value={node.retrieval}
            topK={node.top_k}
            models={options.rerank_models ?? []}
            issues={at("retrieval")}
            onChange={(retrieval) => change({ ...node, retrieval })}
          />
        </>
      );
    case "llm":
      return (
        <>
          <ModelFields
            node={node}
            options={options}
            change={change}
            issues={issues}
          />
          <RefField
            label="系统提示词"
            multiline
            value={node.system ?? ""}
            variables={variables}
            issues={at("system")}
            help="可选，拼在 Skill 正文后面。"
            onChange={(system) => change({ ...node, system })}
          />
          <RefField
            label="提示词"
            multiline
            rows={6}
            value={node.prompt}
            variables={variables}
            issues={at("prompt")}
            onChange={(prompt) => change({ ...node, prompt })}
          />
          <SelectField
            label="输出方式"
            value={node.output.kind}
            choices={[
              { value: "text", label: "一段文字" },
              { value: "json", label: "几个字段（JSON）" },
            ]}
            onChange={(kind) =>
              change({
                ...node,
                output:
                  kind === "json"
                    ? {
                        kind: "json",
                        fields: [
                          { key: "answer", type: "string", description: "" },
                        ],
                      }
                    : { kind: "text" },
              })
            }
          />
          {node.output.kind === "json" && (
            <OutputFields
              label="输出字段"
              fields={node.output.fields}
              prefix="output.fields"
              issues={issues}
              describe
              onChange={(fields) =>
                change({ ...node, output: { kind: "json", fields } })
              }
            />
          )}
        </>
      );
    case "classifier":
      return (
        <>
          <ModelFields
            node={node}
            options={options}
            change={change}
            issues={issues}
          />
          <RefField
            label="要分类的文本"
            value={node.text}
            variables={variables}
            issues={at("text")}
            onChange={(value) => change({ ...node, text: value })}
          />
          <RefField
            label="补充说明"
            multiline
            value={node.instruction ?? ""}
            variables={variables}
            issues={at("instruction")}
            onChange={(instruction) => change({ ...node, instruction })}
          />
          <ClassifierFields node={node} change={change} issues={issues} />
          <p className="muted">都不匹配时走“都不是”。</p>
        </>
      );
    case "condition":
      return (
        <>
          <ConditionFields
            node={node}
            change={change}
            issues={issues}
            variables={variables}
          />
          <p className="muted">
            按顺序判断，第一条成立的规则决定下一步；都不成立时走“否则”。
          </p>
        </>
      );
    case "tool":
      return (
        <ToolFields
          node={node}
          config={config}
          options={options}
          change={change}
          issues={issues}
          variables={variablesFor(config, node.id, true)}
        />
      );
    case "agent":
      return (
        <>
          <ModelFields
            node={node}
            options={options}
            change={change}
            issues={issues}
          />
          <RefField
            label="任务说明"
            multiline
            rows={5}
            value={node.task}
            variables={variables}
            issues={at("task")}
            onChange={(task) => change({ ...node, task })}
          />
          <AgentTools
            node={node}
            config={config}
            options={options}
            change={change}
          />
          {at("tools").map((msg) => (
            <p className="field-error" key={msg}>
              {msg}
            </p>
          ))}
          <fieldset className="list-editor" aria-label="知识库">
            <legend>知识库</legend>
            <label className="asset-field retrieval-switch">
              <input
                type="checkbox"
                role="switch"
                checked={Boolean(node.knowledge)}
                onChange={(event) =>
                  change({
                    ...node,
                    knowledge: event.target.checked
                      ? {
                          knowledge_bases: [],
                          top_k: 5,
                          min_score: 0.3,
                          retrieval: {
                            mode: "hybrid",
                            vector_weight: 0.7,
                            rerank: null,
                          },
                        }
                      : null,
                  })
                }
              />
              让智能体检索知识库
            </label>
            {node.knowledge && (
              <>
                <KnowledgePicker
                  options={options.knowledge_bases ?? []}
                  value={node.knowledge.knowledge_bases}
                  max={5}
                  onChange={(knowledge_bases) =>
                    change({
                      ...node,
                      knowledge: { ...node.knowledge!, knowledge_bases },
                    })
                  }
                />
                {at("knowledge.knowledge_bases").map((message) => (
                  <p className="field-error" key={message}>
                    {message}
                  </p>
                ))}
                <NumberField
                  label="top_k"
                  min={1}
                  max={10}
                  value={node.knowledge.top_k}
                  issues={at("knowledge.top_k")}
                  onChange={(top_k) =>
                    change({
                      ...node,
                      knowledge: { ...node.knowledge!, top_k },
                    })
                  }
                />
                <label className="asset-field">
                  min_score
                  <input
                    type="number"
                    min={0}
                    max={1}
                    step="any"
                    value={
                      Number.isNaN(node.knowledge.min_score)
                        ? ""
                        : node.knowledge.min_score
                    }
                    onChange={(event) =>
                      change({
                        ...node,
                        knowledge: {
                          ...node.knowledge!,
                          min_score: event.target.valueAsNumber,
                        },
                      })
                    }
                  />
                  {at("knowledge.min_score").map((message) => (
                    <span className="field-error" key={message}>
                      {message}
                    </span>
                  ))}
                </label>
                <RetrievalFields
                  value={node.knowledge.retrieval}
                  topK={node.knowledge.top_k}
                  models={options.rerank_models ?? []}
                  issues={at("knowledge.retrieval")}
                  onChange={(retrieval) =>
                    change({
                      ...node,
                      knowledge: { ...node.knowledge!, retrieval },
                    })
                  }
                />
              </>
            )}
          </fieldset>
          {!node.tools.length && !node.knowledge && (
            <p className="field-error" role="alert">
              至少选一个数据源工具或知识库
            </p>
          )}
          <NumberField
            label="最多步数"
            min={1}
            max={20}
            value={node.max_steps}
            issues={at("max_steps")}
            help="一次工具调用算一步。"
            onChange={(max_steps) => change({ ...node, max_steps })}
          />
        </>
      );
    case "human":
      return (
        <HumanFields
          node={node}
          change={change}
          issues={issues}
          variables={variables}
        />
      );
    case "template":
      return (
        <RefField
          label="模板"
          multiline
          rows={6}
          value={node.template}
          variables={variables}
          issues={at("template")}
          onChange={(template) => change({ ...node, template })}
        />
      );
    case "code": {
      const entries = Object.entries(node.inputs);
      return (
        <>
          <div className="asset-field">
            <label htmlFor={`code-${node.id}`}>代码</label>
            <textarea
              id={`code-${node.id}`}
              className="code-input"
              rows={12}
              spellCheck={false}
              value={node.code}
              aria-invalid={at("code").length > 0}
              onChange={(event) =>
                change({ ...node, code: event.target.value })
              }
            />
            <small className="muted">
              定义 main(inputs) 并返回包含下面返回字段的
              dict。可用标准库、pandas、numpy，没有网络。
            </small>
            {at("code").map((msg) => (
              <p className="field-error" key={msg}>
                {msg}
              </p>
            ))}
          </div>
          <ListEditor
            label="输入"
            items={entries}
            make={() =>
              [`input_${entries.length + 1}`, ""] as [string, unknown]
            }
            onChange={(items) =>
              change({ ...node, inputs: Object.fromEntries(items) })
            }
            issues={(i) => issuesUnder(issues, `inputs.${entries[i]?.[0]}`)}
            render={([key, value], _i, update) => (
              <>
                <TextField
                  label="名称"
                  value={key}
                  onChange={(name) => update([name, value])}
                />
                <RefField
                  label="值"
                  value={text(value)}
                  variables={variables}
                  onChange={(next) => update([key, next])}
                />
              </>
            )}
          />
          <OutputFields
            label="返回字段"
            fields={node.outputs}
            prefix="outputs"
            issues={issues}
            onChange={(outputs) => change({ ...node, outputs })}
          />
          <NumberField
            label="超时秒数"
            min={1}
            max={30}
            value={node.timeout_seconds}
            issues={at("timeout_seconds")}
            onChange={(timeout_seconds) => change({ ...node, timeout_seconds })}
          />
        </>
      );
    }
    case "end":
      return (
        <ListEditor
          label="结果字段"
          items={node.outputs}
          min={1}
          onChange={(outputs) => change({ ...node, outputs })}
          make={() => ({
            key: `result_${node.outputs.length + 1}`,
            from: [""],
          })}
          issues={(i) => issuesUnder(issues, `outputs.${i}`)}
          render={(output, _i, update) => (
            <>
              <TextField
                label="字段名"
                value={output.key}
                onChange={(key) => update({ ...output, key })}
              />
              <ListEditor
                label="取值"
                items={output.from}
                min={1}
                make={() => ""}
                onChange={(from) => update({ ...output, from })}
                render={(ref, _j, set) => (
                  <RefField
                    label="变量"
                    value={ref}
                    variables={variables}
                    onChange={set}
                  />
                )}
              />
              <small className="muted">
                依次取第一个有值的变量，几个分支可以汇到这里。
              </small>
            </>
          )}
        />
      );
  }
}
