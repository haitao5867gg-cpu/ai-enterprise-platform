"use client";

import { useT, type Translate } from "../lib/i18n/client";
import { KnowledgePicker } from "./knowledge-form";
import { RetrievalFields } from "./retrieval-fields";
import { ChecksField } from "./checks-field";

import { useEffect, useId, useState, type ReactNode } from "react";
import type {
  AgentNode,
  AppCallNode,
  AppTool,
  CallableApp,
  AppDetail,
  AppSummary,
  ClassifierNode,
  Condition,
  ConditionNode,
  FlowConfig,
  FlowNode,
  FormField,
  GitPrNode,
  HumanNode,
  Input,
  InputType,
  OutputField,
  ReviewNode,
  ReviewRule,
  ToolNode,
  ValueType,
} from "../lib/apps";
import { chatModelChoices, getApp, isFlowConfig, listApps } from "../lib/apps";
import {
  cachedTools,
  isCodeRepo,
  isToolService,
  kindLabel,
  listRepos,
  toolDescription,
  type CodeRepos,
} from "../lib/datasources";
import { issuesUnder, variablesFor, type Variable } from "../lib/flow-edit";
import { errorMessage } from "../lib/runs";
import { SkillPicker } from "./app-settings";
import {
  CheckField,
  FieldIssues,
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
type HumanOption = Extract<HumanNode, { mode: "approval" }>["options"][number];
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
  read_only?: boolean;
};
// Phase 1 supports read-only tools only; a tool that does not say so is marked.
const toolLabel = (t: Translate, tool: Tool) =>
  tool.read_only === false
    ? t("{name}（未声明只读）", { name: tool.name })
    : tool.name;
// Option lists below keep Chinese labels; show them through `translated`.
const translated = <T extends { label: string }>(t: Translate, items: T[]) =>
  items.map((item) => ({ ...item, label: t(item.label) }));

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
    cachedTools(datasource)
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

// `etlOnly`: the data nodes read data, so tool services are left out.
function datasourceChoices(
  t: Translate,
  config: FlowConfig,
  options: Options,
  etlOnly = false,
  databricksOnly = false,
) {
  const start = config.graph.nodes.find((node) => node.type === "start");
  return [
    ...options.datasources
      .filter((item) => !isCodeRepo(item))
      .filter((item) => !etlOnly || !isToolService(item))
      .filter((item) => !databricksOnly || item.kind === "databricks")
      .map((item) => ({
        value: item.datasource_id,
        label: `${item.name} · ${t(kindLabel(item.kind))}`,
      })),
    ...(start?.type === "start" ? start.inputs : [])
      .filter((input) => input.type === "datasource")
      .map((input) => ({
        value: `{{start.${input.key}}}`,
        label: t("运行时选择：{label}（{ref}）", {
          label: input.label,
          ref: `{{start.${input.key}}}`,
        }),
      })),
  ];
}

function DatasourceField({
  value,
  onChange,
  config,
  options,
  issues,
  etlOnly = false,
  databricksOnly = false,
}: {
  value: string;
  onChange: (value: string) => void;
  config: FlowConfig;
  options: Options;
  issues: Issues;
  etlOnly?: boolean;
  // Data checks read through a Databricks data source only.
  databricksOnly?: boolean;
}) {
  const t = useT();
  const choices = datasourceChoices(
    t,
    config,
    options,
    etlOnly,
    databricksOnly,
  );
  const registered = options.datasources.find(
    (item) => item.datasource_id === value,
  );
  return (
    <SelectField
      label={t("数据源")}
      value={value}
      onChange={onChange}
      issues={issuesUnder(issues, "datasource")}
      choices={[
        { value: "", label: t("请选择数据源") },
        ...choices,
        ...(value && !choices.some((item) => item.value === value)
          ? [
              {
                value,
                label: value.includes("{{")
                  ? value
                  : registered
                    ? t("{name} · {kind}（这里不能用）", {
                        name: registered.name,
                        kind: isToolService(registered)
                          ? t("工具服务")
                          : t(kindLabel(registered.kind)),
                      })
                    : t("{value}（未登记）", { value }),
              },
            ]
          : []),
      ]}
    />
  );
}

export function ModelFields({
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
  const t = useT();
  return (
    <>
      <SelectField
        label={t("模型#字段")}
        value={node.model ?? ""}
        issues={issuesUnder(issues, "model")}
        onChange={(model) => change({ ...node, model: model || null })}
        choices={[
          {
            value: "",
            label: t("平台默认（{default_model}）", {
              default_model: options.default_model,
            }),
          },
          ...chatModelChoices(options, node.model),
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
  const t = useT();
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
            label={t("字段名")}
            value={field.key}
            onChange={(key) => update({ ...field, key })}
          />
          <SelectField
            label={t("类型")}
            value={field.type}
            choices={translated(t, valueTypes)}
            onChange={(type) => update({ ...field, type: type as ValueType })}
          />
          {describe && (
            <TextField
              label={t("说明")}
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
  start = false,
}: {
  label: string;
  inputs: T[];
  onChange: (inputs: T[]) => void;
  issues: Issues;
  prefix: string;
  allowDatasource: boolean;
  // A start node's inputs also say how the new-run page shows them.
  start?: boolean;
}) {
  const t = useT();
  return (
    <ListEditor
      label={label}
      items={inputs}
      onChange={onChange}
      min={1}
      make={() =>
        ({
          key: `field_${inputs.length + 1}`,
          label: t("新字段"),
          type: "text",
          required: false,
        }) as T
      }
      issues={(i) => issuesUnder(issues, `${prefix}.${i}`)}
      render={(input, _i, update) => (
        <>
          <TextField
            label={t("字段名")}
            value={input.key}
            onChange={(key) => update({ ...input, key })}
          />
          <TextField
            label={t("显示名称")}
            value={input.label}
            onChange={(value) => update({ ...input, label: value })}
          />
          <SelectField
            label={t("类型")}
            value={input.type}
            choices={translated(
              t,
              inputTypes.filter(
                (item) => allowDatasource || item.value !== "datasource",
              ),
            )}
            onChange={(type) => {
              const next = { ...input, type: type as InputType };
              if (type !== "select") delete next.choices;
              else next.choices ??= [{ value: "a", label: t("选项 A") }];
              // Examples only fit text: dropped here, not left to fail the save unseen.
              if (type !== "text" && type !== "textarea") {
                delete next.max_length;
                delete next.examples;
              }
              update(next);
            }}
          />
          <CheckField
            label={t("必填")}
            checked={Boolean(input.required)}
            onChange={(required) => update({ ...input, required })}
          />
          {start && (
            <CheckField
              label={t("收进高级选项")}
              checked={input.advanced === true}
              onChange={(advanced) => {
                const next: T = { ...input };
                if (advanced) next.advanced = true;
                else delete next.advanced;
                update(next);
              }}
            />
          )}
          {(input.type === "text" || input.type === "textarea") && (
            <NumberField
              label={t("最多字数")}
              min={1}
              max={20000}
              value={input.max_length}
              onChange={(max_length) => update({ ...input, max_length })}
            />
          )}
          {start && (input.type === "text" || input.type === "textarea") && (
            <ListEditor
              label={t("例子")}
              items={input.examples ?? []}
              max={5}
              make={() => ""}
              onChange={(examples) => {
                const next: T = { ...input };
                if (examples.length) next.examples = examples;
                else delete next.examples;
                update(next);
              }}
              render={(example, _j, set) => (
                <TextField label={t("例子")} value={example} onChange={set} />
              )}
            />
          )}
          {input.type === "select" && (
            <ListEditor
              label={t("选项")}
              items={input.choices ?? []}
              min={1}
              make={() => ({
                value: `option_${(input.choices?.length ?? 0) + 1}`,
                label: t("新选项"),
              })}
              onChange={(choices) => update({ ...input, choices })}
              render={(choice, _j, set) => (
                <>
                  <TextField
                    label={t("值")}
                    value={choice.value}
                    onChange={(value) => set({ ...choice, value })}
                  />
                  <TextField
                    label={t("显示文字")}
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
  const t = useT();
  const { tools, error, loading } = useTools(node.datasource);
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
      <DatasourceField
        config={config}
        options={options}
        value={node.datasource}
        issues={issues}
        onChange={(datasource) =>
          change({ ...node, datasource, tool: "", args: {} })
        }
      />
      {tools ? (
        <SelectField
          label={t("工具")}
          value={node.tool}
          issues={issuesUnder(issues, "tool")}
          onChange={(name) => change({ ...node, tool: name, args: {} })}
          help={
            tool
              ? toolDescription(
                  { name: tool.name, description: tool.description ?? "" },
                  options.datasources.find(
                    (item) => item.datasource_id === node.datasource,
                  )?.kind ?? "",
                ) || undefined
              : undefined
          }
          choices={[
            { value: "", label: t("请选择工具") },
            ...tools.map((item) => ({
              value: item.name,
              label: toolLabel(t, item),
            })),
            ...(node.tool && !tool
              ? [
                  {
                    value: node.tool,
                    label: t("{tool}（数据源上没有）", { tool: node.tool }),
                  },
                ]
              : []),
          ]}
        />
      ) : (
        <TextField
          label={t("工具")}
          value={node.tool}
          issues={issuesUnder(issues, "tool")}
          help={
            loading
              ? t("正在读取这个数据源的工具…")
              : error
                ? t("读取工具失败：{error}", { error })
                : node.datasource
                  ? t("数据源在运行时选择，请填写工具名。")
                  : t("先选择数据源。")
          }
          onChange={(name) => change({ ...node, tool: name })}
        />
      )}
      {keys.length > 0 && (
        <fieldset className="list-editor">
          <legend>{t("参数")}</legend>
          {keys.map((key) => (
            <RefField
              key={key}
              label={required.includes(key) ? t("{key}（必填）", { key }) : key}
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
        <p className="muted">{t("这个工具没有参数。")}</p>
      )}
      {!tools && !loading && (
        <AddKey
          label={t("参数名")}
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
  const t = useT();
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
        {t("添加参数")}
      </button>
    </div>
  );
}

export function AgentTools({
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
  const t = useT();
  const known = datasourceChoices(t, config, options);
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
          ? t("{datasource}（未登记）", { datasource: item.datasource })
          : t("未选择的数据源"),
      })),
  ];
  return (
    <fieldset className="list-editor">
      <legend>{t("允许使用的工具")}</legend>
      {!choices.length && (
        <p className="muted">{t("平台上还没有登记数据源。")}</p>
      )}
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
  const t = useT();
  const { tools, loading, error } = useTools(datasource);
  return (
    <>
      <CheckField
        label={t("只允许部分工具")}
        checked={value !== null}
        onChange={(some) => onChange(some ? [] : null)}
      />
      {value !== null &&
        (tools ? (
          <div className="check-grid">
            {tools.map((tool) => (
              <CheckField
                key={tool.name}
                label={toolLabel(t, tool)}
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
            label={t("工具名（用逗号分隔）")}
            value={value.join(",")}
            help={loading ? t("正在读取工具…") : error}
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
  const t = useT();
  return (
    <ListEditor
      label={t("类别")}
      items={node.classes}
      min={1}
      max={10}
      onChange={(classes) => change({ ...node, classes })}
      make={() => ({
        key: `class_${node.classes.length + 1}`,
        label: t("新类别"),
        description: "",
      })}
      issues={(i) => issuesUnder(issues, `classes.${i}`)}
      render={(item, i) => (
        <>
          <TextField
            label={t("标识")}
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
            label={t("名称")}
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
            label={t("说明")}
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

// A review node's checklist: rules written here, or references to lists of rules such as
// {{project.code_rules}} (docs/api/flow-apps.md, "llm_review 模型审查").
function ReviewRules({
  node,
  change,
  issues,
  variables,
}: {
  node: ReviewNode;
  change: Change;
  issues: Issues;
  variables: Variable[];
}) {
  const t = useT();
  const set = (i: number, rule: ReviewRule) =>
    change({
      ...node,
      rules: node.rules.map((r, j) => (j === i ? rule : r)),
    });
  return (
    <ListEditor
      label={t("审查清单")}
      items={node.rules}
      min={1}
      max={20}
      onChange={(rules) => change({ ...node, rules })}
      make={() => ({
        key: `rule_${node.rules.length + 1}`,
        title: "",
        instruction: "",
      })}
      issues={(i) => issuesUnder(issues, `rules.${i}`)}
      render={(item, i) => (
        <>
          <SelectField
            label={t("这一项")}
            value={typeof item === "string" ? "ref" : "rule"}
            onChange={(kind) =>
              set(
                i,
                kind === "ref"
                  ? ""
                  : { key: `rule_${i + 1}`, title: "", instruction: "" },
              )
            }
            choices={[
              { value: "rule", label: t("一条规则") },
              { value: "ref", label: t("引用一组规则") },
            ]}
          />
          {typeof item === "string" ? (
            <RefField
              label={t("规则的引用")}
              value={item}
              variables={variables}
              help={t(
                "比如项目的约定 {{project.plan_rules}}、{{project.code_rules}}；没有规则时这一项不算。",
              )}
              onChange={(value) => set(i, value)}
            />
          ) : (
            <>
              <TextField
                label={t("标识")}
                value={item.key}
                onChange={(key) => set(i, { ...item, key })}
              />
              <TextField
                label={t("规则名称")}
                value={item.title}
                onChange={(title) => set(i, { ...item, title })}
              />
              <TextField
                label={t("怎么判断")}
                value={item.instruction ?? ""}
                onChange={(instruction) => set(i, { ...item, instruction })}
              />
            </>
          )}
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
  const t = useT();
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
      label={t("规则")}
      items={node.rules}
      min={1}
      max={10}
      onChange={(rules) => change({ ...node, rules })}
      make={(): ConditionNode["rules"][number] => ({
        key: `rule_${node.rules.length + 1}`,
        label: t("新规则"),
        match: "all",
        conditions: [{ left: "", op: "not_empty" }],
      })}
      issues={(i) => issuesUnder(issues, `rules.${i}`)}
      render={(rule, i) => (
        <>
          <TextField
            label={t("标识")}
            value={rule.key}
            onChange={(key) => setRule(i, { ...rule, key }, key)}
          />
          <TextField
            label={t("名称")}
            value={rule.label}
            onChange={(label) => setRule(i, { ...rule, label })}
          />
          <SelectField
            label={t("满足")}
            value={rule.match}
            choices={[
              { value: "all", label: t("全部条件") },
              { value: "any", label: t("任一条件") },
            ]}
            onChange={(match) =>
              setRule(i, { ...rule, match: match as "all" | "any" })
            }
          />
          <ListEditor
            label={t("条件")}
            items={rule.conditions}
            min={1}
            max={10}
            onChange={(conditions) => setRule(i, { ...rule, conditions })}
            make={(): Condition => ({ left: "", op: "eq", right: "" })}
            render={(condition, _j, update) => (
              <>
                <RefField
                  label={t("左边")}
                  value={text(condition.left)}
                  variables={variables}
                  onChange={(left) => update({ ...condition, left })}
                />
                <SelectField
                  label={t("比较")}
                  value={condition.op}
                  choices={translated(t, operators)}
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
                    label={t("右边")}
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
  const t = useT();
  const base = {
    id: node.id,
    type: node.type,
    title: node.title,
    max_visits: node.max_visits,
    ui: node.ui,
    prompt: node.prompt,
    show: node.show,
    note: node.note,
  };
  return (
    <>
      <SelectField
        label={t("处理方式")}
        value={node.mode}
        choices={[
          { value: "approval", label: t("确认：选一个选项") },
          { value: "form", label: t("填写：填一张表") },
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
                      label: t("内容"),
                      type: "text",
                      required: true,
                    },
                  ],
                }
              : {
                  ...base,
                  mode: "approval",
                  options: [
                    { key: "approve", label: t("确认") },
                    { key: "reject", label: t("驳回") },
                  ],
                  comment_required: [],
                },
          )
        }
      />
      <RefField
        label={t("提示")}
        value={node.prompt}
        variables={variables}
        issues={issuesUnder(issues, "prompt")}
        onChange={(prompt) => change({ ...node, prompt })}
      />
      <RefField
        label={t("给处理人看的内容")}
        multiline
        value={node.show ?? ""}
        variables={variables}
        issues={issuesUnder(issues, "show")}
        help={t("按 Markdown 显示，可选。")}
        onChange={(show) => change({ ...node, show })}
      />
      {node.mode === "approval" ? (
        <ListEditor<HumanOption>
          label={t("选项")}
          items={node.options}
          min={2}
          max={5}
          onChange={(options) =>
            change(
              withChangedBranch(
                {
                  ...node,
                  options,
                  comment_required: (node.comment_required ?? []).filter(
                    (key) => options.some((option) => option.key === key),
                  ),
                },
                options.some((option) => option.key === node.changed_branch)
                  ? node.changed_branch
                  : null,
              ),
            )
          }
          make={() => ({
            key: `option_${node.options.length + 1}`,
            label: t("新选项"),
          })}
          issues={(i) => issuesUnder(issues, `options.${i}`)}
          render={(option, i) => {
            const required = node.comment_required ?? [];
            return (
              <>
                <TextField
                  label={t("标识")}
                  value={option.key}
                  onChange={(key) =>
                    change(
                      withChangedBranch(
                        {
                          ...node,
                          options: node.options.map((o, j) =>
                            j === i ? { ...o, key } : o,
                          ),
                          comment_required: required.map((item) =>
                            item === option.key ? key : item,
                          ),
                        },
                        node.changed_branch === option.key
                          ? key
                          : node.changed_branch,
                      ),
                      { from: option.key, to: key },
                    )
                  }
                />
                <TextField
                  label={t("按钮文字")}
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
                {option.requires != null && option.requires !== "" && (
                  <ReadOnlySetting
                    label={t("显示条件")}
                    issues={issuesUnder(issues, `options.${i}.requires`)}
                  >
                    {Array.isArray(option.requires)
                      ? option.requires.map(settingText).join(t("、"))
                      : settingText(option.requires)}
                    <span className="muted">
                      {Array.isArray(option.requires)
                        ? t("（都成立时才显示这个选项）")
                        : t("（成立时才显示这个选项）")}
                    </span>
                  </ReadOnlySetting>
                )}
                {typeof option.unmet === "string" && option.unmet !== "" && (
                  <ReadOnlySetting
                    label={t("不显示时的说明")}
                    issues={issuesUnder(issues, `options.${i}.unmet`)}
                  >
                    {option.unmet}
                  </ReadOnlySetting>
                )}
                <CheckField
                  label={t("必须写意见")}
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
          label={t("表单字段")}
          inputs={node.fields}
          prefix="fields"
          issues={issues}
          allowDatasource={false}
          onChange={(fields) => change({ ...node, fields })}
        />
      )}
      <HumanExtras node={node} issues={issues} />
    </>
  );
}

// changed_branch follows its option; without one the key goes (never written as null).
function withChangedBranch(
  node: Extract<HumanNode, { mode: "approval" }>,
  branch: string | null | undefined,
): HumanNode {
  const rest = { ...node };
  delete rest.changed_branch;
  return branch ? { ...rest, changed_branch: branch } : rest;
}

function settingText(value: unknown): string {
  return typeof value === "string" ? value : JSON.stringify(value);
}

function ReadOnlySetting({
  label,
  issues,
  children,
}: {
  label: string;
  issues: string[];
  children: ReactNode;
}) {
  const id = useId();
  return (
    <div className="asset-field read-only-setting">
      <span className="read-only-label" id={id}>
        {label}
      </span>
      <div aria-labelledby={id}>{children}</div>
      <FieldIssues id={`${id}-issues`} messages={issues} />
    </div>
  );
}

// The extended settings written in a template or an imported file: shown, not edited,
// and saved as they are.
function HumanExtras({ node, issues }: { node: HumanNode; issues: Issues }) {
  const t = useT();
  const fields = node.mode === "approval" ? node.fields : null;
  const data = node.data && Object.entries(node.data);
  const asked =
    typeof fields === "string"
      ? fields
        ? settingText(fields)
        : ""
      : Array.isArray(fields) && fields.length
        ? fields
            .map((field) =>
              typeof field === "object" && field && "label" in field
                ? String(field.label)
                : settingText(field),
            )
            .join(t("、"))
        : "";
  const changed =
    node.mode === "approval" && node.changed_branch
      ? (node.options.find((option) => option.key === node.changed_branch)
          ?.label ?? node.changed_branch)
      : "";
  const compared =
    node.mode === "approval" && node.changes
      ? t("{source} 里的{lists}", {
          source: settingText(node.changes.of),
          lists: (node.changes.lists ?? [])
            .map((item) => item.name || item.path)
            .join(t("、")),
        })
      : "";
  if (!node.view && !data?.length && !asked && !changed && !compared)
    return null;
  return (
    <section className="human-extras" aria-label={t("扩展设置")}>
      <h4>{t("扩展设置")}</h4>
      <p className="muted">
        {t("写在模板或导入的文件里，这里只能查看，保存时原样保留。")}
      </p>
      {node.view && (
        <ReadOnlySetting
          label={t("页面样式")}
          issues={issuesUnder(issues, "view")}
        >
          <code>{node.view}</code>
        </ReadOnlySetting>
      )}
      {data && data.length > 0 && (
        <ReadOnlySetting
          label={t("页面数据")}
          issues={issuesUnder(issues, "data")}
        >
          <ul>
            {data.map(([key, value]) => (
              <li key={key}>
                <code>{key}</code>：{settingText(value)}
              </li>
            ))}
          </ul>
        </ReadOnlySetting>
      )}
      {asked && (
        <ReadOnlySetting
          label={t("要回答的题")}
          issues={issuesUnder(issues, "fields")}
        >
          {asked}
        </ReadOnlySetting>
      )}
      {changed && (
        <ReadOnlySetting
          label={t("改了建议答案或写了意见时按")}
          issues={issuesUnder(issues, "changed_branch")}
        >
          {t("「{branch}」处理", { branch: changed })}
        </ReadOnlySetting>
      )}
      {compared && (
        <ReadOnlySetting
          label={t("和上次看到的比改了什么")}
          issues={issuesUnder(issues, "changes")}
        >
          {compared}
        </ReadOnlySetting>
      )}
    </section>
  );
}

function DataCheckFields({
  node,
  config,
  options,
  change,
  issues,
}: {
  node: Extract<FlowNode, { type: "data_check" }>;
  config: FlowConfig;
  options: Options;
  change: Change;
  issues: Issues;
}) {
  const t = useT();
  const variables = variablesFor(config, node.id);
  const at = (path: string) => issuesUnder(issues, path);
  const golden = node.golden ?? {};
  const keys = Array.isArray(golden.keys)
    ? golden.keys.join(", ")
    : (golden.keys ?? "");
  function setGolden(
    key: "expected" | "table" | "keys" | "tolerance" | "severity",
    value: string,
  ) {
    const next: Record<string, unknown> = { ...golden };
    const number = Number(value.trim());
    if (!value.trim()) delete next[key];
    else if (key === "keys" && !value.includes("{{"))
      next.keys = value.split(/[,，、\s]+/).filter(Boolean);
    else if (
      key === "tolerance" &&
      !value.includes("{{") &&
      Number.isFinite(number)
    )
      next.tolerance = number;
    else next[key] = value;
    const rest = { ...node };
    delete rest.golden;
    change(Object.keys(next).length ? { ...rest, golden: next } : rest);
  }
  return (
    <>
      <DatasourceField
        config={config}
        options={options}
        value={node.datasource}
        issues={issues}
        databricksOnly
        onChange={(datasource) => change({ ...node, datasource })}
      />
      <ChecksField
        value={node.checks ?? []}
        issues={at("checks")}
        onChange={(checks) => change({ ...node, checks })}
      />
      <ListEditor
        label={t("引用的检查")}
        items={node.include ?? []}
        max={5}
        make={() => ""}
        issues={(i) => at(`include.${i}`)}
        onChange={(include) => change({ ...node, include })}
        render={(value, _i, update) => (
          <RefField
            label={t("检查列表")}
            value={value}
            variables={variables}
            help={t("引用一个检查列表，比如方案里的 {{check_plan.checks}}。")}
            onChange={update}
          />
        )}
      />
      {at("include").map((msg) => (
        <p className="field-error" key={msg}>
          {msg}
        </p>
      ))}
      <fieldset className="list-editor" aria-label={t("和老系统对比")}>
        <legend>{t("和老系统对比")}</legend>
        <small className="muted">
          {t(
            "填了老系统结果表时，自动加一条按主键逐行对比的检查；运行时值为空就不加。",
          )}
        </small>
        <RefField
          label={t("老系统结果表")}
          value={golden.expected ?? ""}
          variables={variables}
          issues={at("golden.expected")}
          help={t("catalog.schema.table，或引用开始节点的输入。")}
          onChange={(value) => setGolden("expected", value)}
        />
        <RefField
          label={t("结果表")}
          value={golden.table ?? ""}
          variables={variables}
          issues={at("golden.table")}
          onChange={(value) => setGolden("table", value)}
        />
        <RefField
          label={t("主键")}
          value={keys}
          variables={variables}
          issues={at("golden.keys")}
          help={t("列名用逗号分隔，或引用一个列名列表。")}
          onChange={(value) => setGolden("keys", value)}
        />
        <RefField
          label={t("容差（选填）")}
          value={golden.tolerance === undefined ? "" : String(golden.tolerance)}
          variables={variables}
          issues={at("golden.tolerance")}
          help={t(
            "数字列差的绝对值不超过它算一致，比如 0.001；留空时要完全相等。",
          )}
          onChange={(value) => setGolden("tolerance", value)}
        />
        <SelectField
          label={t("不一致时")}
          value={golden.severity ?? ""}
          choices={[
            { value: "", label: t("算错误（检查不通过）") },
            { value: "warn", label: t("只警告") },
            { value: "info", label: t("只提示") },
          ]}
          onChange={(value) => setGolden("severity", value)}
        />
        {at("golden.severity").map((msg) => (
          <p className="field-error" key={msg}>
            {msg}
          </p>
        ))}
      </fieldset>
      <RefField
        label={t("只在这个值为 true 时检查")}
        value={node.only_if ?? ""}
        variables={variables}
        issues={at("only_if")}
        help={t(
          "通常引用试算是否通过；不为 true 时不读表，直接走“不通过”。留空时总是检查。",
        )}
        onChange={(value) => {
          const rest = { ...node };
          delete rest.only_if;
          change(value.trim() ? { ...rest, only_if: value } : rest);
        }}
      />
    </>
  );
}

// The app an app node calls, its inputs and the fields its end nodes give
// (docs/api/flow-apps.md, "app 调用应用").
function AppCallFields({
  node,
  variables,
  issues,
  change,
}: {
  node: AppCallNode;
  variables: Variable[];
  issues: Issues;
  change: Change;
}) {
  const t = useT();
  const at = (path: string) => issuesUnder(issues, path);
  const [apps, setApps] = useState<AppSummary[] | null>(null);
  const [called, setCalled] = useState<AppDetail | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    const controller = new AbortController();
    listApps(controller.signal)
      .then((value) => setApps(value.filter((item) => item.kind === "flow")))
      .catch((err) => {
        if (!controller.signal.aborted) setError(errorMessage(err));
      });
    return () => controller.abort();
  }, []);
  useEffect(() => {
    if (!node.app) return;
    const controller = new AbortController();
    getApp(node.app, controller.signal)
      .then(setCalled)
      .catch((err) => {
        if (!controller.signal.aborted) setError(errorMessage(err));
      });
    return () => controller.abort();
  }, [node.app]);
  const shown = called && called.app === node.app ? called : null;
  const inputs = shown?.inputs ?? [];
  const results = (() => {
    const config = shown?.current.config;
    if (!config || !isFlowConfig(config)) return [];
    const keys: string[] = [];
    for (const item of config.graph.nodes)
      if (item.type === "end")
        for (const output of item.outputs)
          if (!keys.includes(output.key)) keys.push(output.key);
    return keys;
  })();
  return (
    <>
      <SelectField
        label={t("调用哪个应用")}
        value={node.app}
        choices={[
          {
            value: "",
            label: apps === null ? t("正在加载应用…") : t("请选择应用"),
          },
          ...(apps ?? []).map((item) => ({
            value: item.app,
            label: t("{title}（{id}）", { title: item.title, id: item.app }),
          })),
          ...(node.app && apps && !apps.some((item) => item.app === node.app)
            ? [{ value: node.app, label: node.app }]
            : []),
        ]}
        issues={at("app")}
        help={t(
          "只能调用编排应用，用它当时的生效版本；它不能有人工确认的步骤。",
        )}
        onChange={(app) => change({ ...node, app, input: {}, outputs: [] })}
      />
      {error && <p className="field-error">{error}</p>}
      {inputs.map((field) => (
        <RefField
          key={field.key}
          label={t(field.required ? "输入：{label}（必填）" : "输入：{label}", {
            label: field.label,
          })}
          value={
            typeof node.input[field.key] === "string"
              ? (node.input[field.key] as string)
              : node.input[field.key] === undefined
                ? ""
                : JSON.stringify(node.input[field.key])
          }
          variables={variables}
          issues={at(`input.${field.key}`)}
          onChange={(value) => {
            const input = { ...node.input };
            if (value === "") delete input[field.key];
            else input[field.key] = value;
            change({ ...node, input });
          }}
        />
      ))}
      {shown && (
        <fieldset className="list-editor">
          <legend>{t("取出的结果字段")}</legend>
          {results.length ? (
            results.map((key) => (
              <CheckField
                key={key}
                label={key}
                checked={node.outputs.includes(key)}
                onChange={(checked) =>
                  change({
                    ...node,
                    outputs: checked
                      ? [...node.outputs, key]
                      : node.outputs.filter((item) => item !== key),
                  })
                }
              />
            ))
          ) : (
            <p className="muted">{t("这个应用的结束节点没有结果字段。")}</p>
          )}
          <small className="muted">
            {t("勾上的字段可以在后面用 {field} 引用；整个结果是 {result}。", {
              field: "{{" + node.id + "." + t("字段#变量名") + "}}",
              result: `{{${node.id}.result}}`,
            })}
          </small>
          <FieldIssues
            id={`${node.id}-outputs-issues`}
            messages={at("outputs")}
          />
        </fieldset>
      )}
      <NumberField
        label={t("最长等待（秒）")}
        value={node.timeout_seconds ?? 600}
        min={10}
        max={3600}
        issues={at("timeout_seconds")}
        help={t("超过这个时间还没结束，就取消它，这一步按超时失败。")}
        onChange={(timeout_seconds) => change({ ...node, timeout_seconds })}
      />
    </>
  );
}

// docs/api/git.md ("提交 PR"): a code repository, one of its repositories, and what
// goes into the pull request.
function GitPrFields({
  node,
  options,
  variables,
  issues,
  change,
}: {
  node: GitPrNode;
  options: Options;
  variables: Variable[];
  issues: Issues;
  change: Change;
}) {
  const t = useT();
  const at = (path: string) => issuesUnder(issues, path);
  const sources = options.datasources.filter(isCodeRepo);
  const [repos, setRepos] = useState<CodeRepos | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    if (
      !node.datasource ||
      !sources.some((s) => s.datasource_id === node.datasource)
    )
      return;
    const controller = new AbortController();
    listRepos(node.datasource, controller.signal)
      .then((value) => {
        setRepos(value);
        setError("");
      })
      .catch((err) => {
        if (!controller.signal.aborted) setError(errorMessage(err));
      });
    return () => controller.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [node.datasource]);
  const shown = repos && node.datasource ? repos : null;
  const set = (key: keyof GitPrNode) => (value: string) =>
    change({ ...node, [key]: value });
  return (
    <>
      <SelectField
        label={t("代码仓库")}
        value={node.datasource}
        choices={[
          {
            value: "",
            label: sources.length
              ? t("请选择代码仓库")
              : t("还没有登记代码仓库（在「数据源与工具」页登记）"),
          },
          ...sources.map((item) => ({
            value: item.datasource_id,
            label: item.name,
          })),
          ...(node.datasource &&
          !sources.some((item) => item.datasource_id === node.datasource)
            ? [
                {
                  value: node.datasource,
                  label: t("{datasource}（不是代码仓库）", {
                    datasource: node.datasource,
                  }),
                },
              ]
            : []),
        ]}
        issues={at("datasource")}
        help={t(
          "经 Git 连接器接入的 GitHub 或 Bitbucket。平台只在自己的 aep/ 分支上提交，合不合并由人决定。",
        )}
        onChange={(datasource) => {
          setRepos(null);
          change({ ...node, datasource });
        }}
      />
      {error && <p className="field-error">{error}</p>}
      {shown && shown.repos.length ? (
        <SelectField
          label={t("仓库")}
          value={node.repo}
          choices={[
            { value: "", label: t("请选择仓库") },
            ...shown.repos.map((item) => ({
              value: item.repo,
              label: t("{repo}（默认分支 {default_branch}）", {
                repo: item.repo,
                default_branch: item.default_branch,
              }),
            })),
            ...(node.repo &&
            !shown.repos.some((item) => item.repo === node.repo)
              ? [{ value: node.repo, label: node.repo }]
              : []),
          ]}
          issues={at("repo")}
          onChange={set("repo")}
        />
      ) : (
        <TextField
          label={t("仓库")}
          value={node.repo}
          issues={at("repo")}
          help={t("GitHub 写 owner/仓库，Bitbucket 写 PROJECT/仓库。")}
          onChange={set("repo")}
        />
      )}
      <RefField
        label={t("要提交的文件")}
        value={node.files}
        variables={variables}
        issues={at("files")}
        help={t(
          "引用一个「路径: 文本」对象，比如检查并打包节点的 {{check_code.bundle.files}}；值为 null 的路径会被删除。",
        )}
        onChange={set("files")}
      />
      <RefField
        label={t("放在哪个目录")}
        value={node.directory ?? ""}
        variables={variables}
        issues={at("directory")}
        help={t(
          "文件放在仓库的这个目录下，比如 pipelines/{{check_code.bundle.name}}；空着是仓库根目录。",
        )}
        onChange={set("directory")}
      />
      <CheckField
        label={t("用这些文件替换整个目录（删掉目录里没有提交的旧文件）")}
        checked={node.replace_directory ?? false}
        onChange={(replace_directory) => change({ ...node, replace_directory })}
      />
      <RefField
        label={t("PR 标题")}
        value={node.pr_title}
        variables={variables}
        issues={at("pr_title")}
        onChange={set("pr_title")}
      />
      <RefField
        label={t("PR 说明")}
        value={node.body ?? ""}
        variables={variables}
        issues={at("body")}
        multiline
        help={t("空着时写上运行的链接。")}
        onChange={set("body")}
      />
      <RefField
        label={t("提交信息")}
        value={node.message ?? ""}
        variables={variables}
        issues={at("message")}
        help={t(
          "可以带工单号，比如 {{start.ticket}} 新增管道；空着用 PR 标题。",
        )}
        onChange={set("message")}
      />
      <TextField
        label={t("目标分支")}
        value={node.base ?? ""}
        issues={at("base")}
        help={t("PR 合到哪个分支，空着用仓库的默认分支。")}
        onChange={set("base")}
      />
      <RefField
        label={t("提交到的分支")}
        value={node.branch ?? ""}
        variables={variables}
        issues={at("branch")}
        help={t(
          "空着时平台按运行起名：{prefix}runs/<运行>/{id}。要以 {prefix} 开头；同一个分支再提交会更新原来的 PR。",
          {
            prefix: shown?.branch_prefix ?? "aep/",
            id: node.id,
          },
        )}
        onChange={set("branch")}
      />
    </>
  );
}

// The agent's knowledge bases (docs/api/knowledge.md, "编排应用智能体调用知识库").
export function AgentKnowledgeFields<T extends { knowledge?: AgentNode["knowledge"] }>({
  node,
  options,
  change,
  at,
}: {
  node: T;
  options: Options;
  change: (node: T) => void;
  at: (path: string) => string[];
}) {
  const t = useT();
  return (
    <fieldset className="list-editor" aria-label={t("知识库")}>
        <legend>{t("知识库")}</legend>
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

          {t("让智能体检索知识库")}
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
  );
}

// Published apps the agent may call as tools (docs/api/agent-apps.md, "调用应用"): what
// each is for decides whether the model calls it; a stopped one stays in the settings.
// ``choices`` lists the apps with why one may not be called; null: list them here.
export function AgentAppsField({
  value,
  choices,
  issues,
  onChange,
}: {
  value: AppTool[];
  choices: CallableApp[] | null;
  issues: Issues;
  onChange: (value: AppTool[]) => void;
}) {
  const t = useT();
  const [listed, setListed] = useState<CallableApp[] | null>(choices);
  const [adding, setAdding] = useState("");
  useEffect(() => {
    if (choices) return;
    const controller = new AbortController();
    listApps(controller.signal)
      .then((apps) =>
        setListed(
          apps.map((app) => ({
            app: app.app,
            title: app.title,
            kind: app.kind,
            callable: app.kind !== "code",
            reason:
              app.kind === "code"
                ? t("代码应用不能被调用")
                : null,
          })),
        ),
      )
      .catch(() => {
        if (!controller.signal.aborted) setListed([]);
      });
    return () => controller.abort();
  }, [choices, t]);
  const options = choices ?? listed ?? [];
  const titleOf = (app: string) =>
    options.find((item) => item.app === app)?.title ?? app;
  const left = options.filter(
    (item) => !value.some((entry) => entry.app === item.app),
  );
  return (
    <fieldset className="list-editor agent-apps">
      <legend>{t("可以调用的应用")}</legend>
      <p className="muted">
        {t(
          "把已发布的应用当工具：模型按「什么时候调用」决定调不调，被调的应用不带对话历史。",
        )}
      </p>
      {issues?.apps && <p className="field-error">{issues.apps}</p>}
      {value.map((entry, index) => (
        <div className="list-row" key={`${entry.app}-${index}`}>
          <div className="list-row-fields">
            <strong>
              {titleOf(entry.app)}{" "}
              <small className="muted monospace">{entry.app}</small>
            </strong>
            <FieldIssues
              id={`agent-app-${index}`}
              messages={issuesUnder(issues, `apps.${index}.app`)}
            />
            <div className="asset-field">
              <label htmlFor={`agent-app-when-${index}`}>
                {t("什么时候调用")}
              </label>
              <textarea
                id={`agent-app-when-${index}`}
                rows={2}
                maxLength={500}
                value={entry.when}
                placeholder={t("例如：用户问订单状态时")}
                aria-invalid={issuesUnder(issues, `apps.${index}.when`).length > 0}
                onChange={(event) =>
                  onChange(
                    value.map((item, i) =>
                      i === index ? { ...item, when: event.target.value } : item,
                    ),
                  )
                }
              />
              <FieldIssues
                id={`agent-app-when-${index}-issues`}
                messages={issuesUnder(issues, `apps.${index}.when`)}
              />
            </div>
            <CheckField
              label={t("启用")}
              checked={entry.enabled !== false}
              help={
                entry.enabled === false
                  ? t("停用后保留设置，但不提供给模型。")
                  : undefined
              }
              onChange={(enabled) =>
                onChange(
                  value.map((item, i) =>
                    i === index ? { ...item, enabled } : item,
                  ),
                )
              }
            />
          </div>
          <button
            type="button"
            className="text-button danger-text"
            aria-label={t("不再调用「{title}」", { title: titleOf(entry.app) })}
            onClick={() => onChange(value.filter((_, i) => i !== index))}
          >
            {t("移除")}
          </button>
        </div>
      ))}
      {value.length < 10 && (
        <div className="list-add">
          <label className="sr-only" htmlFor="agent-app-add">
            {t("添加要调用的应用")}
          </label>
          <select
            id="agent-app-add"
            value={adding}
            onChange={(event) => setAdding(event.target.value)}
          >
            <option value="">{t("选择一个已发布的应用…")}</option>
            {left.map((item) => (
              <option key={item.app} value={item.app} disabled={!item.callable}>
                {item.title}
                {item.callable
                  ? item.kind === "agent"
                    ? t("（智能体）")
                    : ""
                  : t("（{reason}）", { reason: item.reason ?? t("不能调用") })}
              </option>
            ))}
          </select>
          <button
            type="button"
            className="button secondary"
            disabled={!adding}
            onClick={() => {
              onChange([...value, { app: adding, when: "", enabled: true }]);
              setAdding("");
            }}
          >
            {t("添加")}
          </button>
        </div>
      )}
    </fieldset>
  );
}

// The settings of an agent node, and (standalone) of an agent app's one agent
// (docs/api/flow-apps.md, "智能体节点的新设置"; agent-apps.md, "配置").
export function AgentFields({
  node,
  config,
  options,
  issues,
  change,
}: {
  node: AgentNode;
  config: FlowConfig;
  options: Options;
  issues: Issues;
  change: Change;
}) {
  const t = useT();
  const variables = variablesFor(config, node.id);
  const at = (path: string) => issuesUnder(issues, path);
  return (
    <>
      <ModelFields
        node={node}
        options={options}
        change={change}
        issues={issues}
      />
      <RefField
        label={t("角色和做事规则（可选）")}
        multiline
        rows={4}
        value={node.instructions ?? ""}
        variables={variables}
        issues={at("instructions")}
        help={t("放进系统提示词；任务说明写这一次要做的事。")}
        onChange={(instructions) => change({ ...node, instructions })}
      />
      <RefField
        label={t("任务说明")}
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
      <AgentKnowledgeFields
        node={node}
        options={options}
        change={change}
        at={at}
      />
      <AgentAppsField
        value={node.apps ?? []}
        choices={null}
        issues={issues}
        onChange={(apps) => change({ ...node, apps })}
      />
      <CheckField
        label={t("只依据工具和资料回答")}
        checked={node.grounded_only ?? false}
        help={t(
          "没有调成功工具、也没标出资料出处时，平台把回答换成「没有找到依据」。",
        )}
        onChange={(grounded_only) => change({ ...node, grounded_only })}
      />
      {(config.conversation?.enabled || node.history) && (
        <CheckField
          label={t("带上对话历史")}
          checked={node.history ?? false}
          issues={at("history")}
          onChange={(history) => change({ ...node, history })}
        />
      )}
      <NumberField
        label={t("最多步数")}
        min={1}
        max={20}
        value={node.max_steps}
        issues={at("max_steps")}
        help={t("一次工具调用、一次检索、一次调用应用各算一步。")}
        onChange={(max_steps) => change({ ...node, max_steps })}
      />
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
  const t = useT();
  const variables = variablesFor(config, node.id);
  const at = (path: string) => issuesUnder(issues, path);
  switch (node.type) {
    case "etl_profile":
    case "databricks_ingest":
      return (
        <>
          <DatasourceField
            config={config}
            options={options}
            value={node.datasource}
            issues={issues}
            etlOnly
            onChange={(datasource) => change({ ...node, datasource })}
          />
          <RefField
            label={node.type === "etl_profile" ? t("表范围") : t("表")}
            multiline
            value={
              Array.isArray(node.tables) ? node.tables.join("\n") : node.tables
            }
            variables={variables}
            issues={at("tables")}
            help={
              node.type === "etl_profile"
                ? t("schema.table 或 schema.*，逗号或换行分隔；留空为全部")
                : t("引用源表列表，或填写 schema.table，逗号或换行分隔。")
            }
            onChange={(tables) => change({ ...node, tables })}
          />
          {node.type === "etl_profile" ? (
            <NumberField
              label={t("样本行数")}
              value={node.sample_rows}
              min={1}
              max={20}
              issues={at("sample_rows")}
              onChange={(sample_rows) => change({ ...node, sample_rows })}
            />
          ) : (
            <NumberField
              label={t("每表最多行数")}
              value={node.max_rows}
              min={1}
              max={1_000_000}
              issues={at("max_rows")}
              onChange={(max_rows) => change({ ...node, max_rows })}
            />
          )}
        </>
      );
    case "etl_plan_check":
      return (
        <>
          <RefField
            label={t("模型回答")}
            value={node.reply}
            variables={variables}
            issues={at("reply")}
            onChange={(reply) => change({ ...node, reply })}
          />
          <RefField
            label={t("探查结果")}
            value={node.profile}
            variables={variables}
            issues={at("profile")}
            onChange={(profile) => change({ ...node, profile })}
          />
          <ListEditor
            label={t("知识库命中")}
            items={node.hits}
            max={5}
            make={() => ""}
            issues={(i) => at(`hits.${i}`)}
            onChange={(hits) => change({ ...node, hits })}
            render={(value, _i, update) => (
              <RefField
                label={t("命中引用")}
                value={value}
                variables={variables}
                onChange={update}
              />
            )}
          />
          {at("hits").map((msg) => (
            <p className="field-error" key={msg}>
              {msg}
            </p>
          ))}
          <RefField
            label={t("代码语言")}
            value={node.language ?? ""}
            variables={variables}
            issues={at("language")}
            help={t("可引用 pyspark 或 sql；留空时使用方案里的语言。")}
            onChange={(language) => change({ ...node, language })}
          />
          <NumberField
            label={t("最多连续重写次数")}
            value={node.max_attempts}
            min={1}
            max={5}
            issues={at("max_attempts")}
            onChange={(max_attempts) => change({ ...node, max_attempts })}
          />
          <StuckField
            value={node.on_stuck}
            issues={at("on_stuck")}
            onChange={(on_stuck) => change({ ...node, on_stuck })}
          />
        </>
      );
    case "etl_code_check":
      return (
        <>
          {(
            [
              ["reply", t("模型回答")],
              ["plan", t("方案")],
              ["source_name", t("数据源名称")],
              ["name", t("bundle 名")],
            ] as const
          ).map(([key, label]) => (
            <RefField
              key={key}
              label={label}
              value={node[key] ?? ""}
              variables={variables}
              issues={at(key)}
              onChange={(value) => change({ ...node, [key]: value })}
            />
          ))}
          <NumberField
            label={t("最多连续重写次数")}
            value={node.max_attempts}
            min={1}
            max={5}
            issues={at("max_attempts")}
            onChange={(max_attempts) => change({ ...node, max_attempts })}
          />
          <StuckField
            value={node.on_stuck}
            issues={at("on_stuck")}
            onChange={(on_stuck) => change({ ...node, on_stuck })}
          />
        </>
      );
    case "data_check":
      return (
        <DataCheckFields
          node={node}
          config={config}
          options={options}
          change={change}
          issues={issues}
        />
      );
    case "databricks_dev_run":
      return (
        <>
          <RefField
            label="bundle"
            value={node.bundle}
            variables={variables}
            issues={at("bundle")}
            onChange={(bundle) => change({ ...node, bundle })}
          />
          <NumberField
            label={t("最多运行次数")}
            value={node.max_runs}
            min={1}
            max={3}
            issues={at("max_runs")}
            onChange={(max_runs) => change({ ...node, max_runs })}
          />
          <NumberField
            label={t("预览行数")}
            value={node.preview_rows}
            min={0}
            max={100}
            issues={at("preview_rows")}
            help={t("0 表示不预览。")}
            onChange={(preview_rows) => change({ ...node, preview_rows })}
          />
        </>
      );
    case "git_pr":
      return (
        <GitPrFields
          node={node}
          options={options}
          variables={variables}
          issues={issues}
          change={change}
        />
      );
    case "app":
      return (
        <AppCallFields
          node={node}
          variables={variables}
          issues={issues}
          change={change}
        />
      );
    case "databricks_release":
      return (
        <>
          <RefField
            label="bundle"
            value={node.bundle}
            variables={variables}
            issues={at("bundle")}
            onChange={(bundle) => change({ ...node, bundle })}
          />
          <RefField
            label={t("定时（Quartz cron）")}
            value={node.schedule ?? ""}
            variables={variables}
            issues={at("schedule")}
            help={t(
              "如每天 2 点：0 0 2 * * ?。可以引用上线审批的回答（如 {{release_review.values}}，取其中的 schedule）；空着用平台默认的定时。",
            )}
            onChange={(schedule) => change({ ...node, schedule })}
          />
          <TextField
            label={t("上线环境")}
            value={node.environment ?? "prod"}
            issues={at("environment")}
            help={t(
              "只对绑定项目的运行有用：上线到项目的哪个环境。不绑定项目时上线到平台的 prod。",
            )}
            onChange={(environment) => change({ ...node, environment })}
          />
        </>
      );
    case "start":
      return (
        <>
          <SelectField
            label={t("使用项目")}
            value={node.project ?? ""}
            choices={[
              { value: "", label: t("不用项目") },
              { value: "optional", label: t("可以选项目（选填）") },
              { value: "required", label: t("必须选一个项目") },
            ]}
            issues={at("project")}
            help={t(
              "用项目时，新建运行要选项目、环境并填运行参数；节点里可以引用 {{project.字段}} 和 {{param.参数标识}}。",
            )}
            onChange={(value) => {
              const { project: _old, ...rest } = node;
              void _old;
              change(
                value === "required" || value === "optional"
                  ? { ...rest, project: value }
                  : rest,
              );
            }}
          />
          <InputList
            label={t("输入字段")}
            inputs={node.inputs}
            prefix="inputs"
            issues={issues}
            allowDatasource
            start
            onChange={(inputs) => change({ ...node, inputs })}
          />
        </>
      );
    case "knowledge":
      return (
        <>
          <fieldset className="list-editor">
            <legend>{t("知识库（1–5 个）")}</legend>
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
          <SelectField
            label={t("取资料的方式")}
            value={node.mode ?? "search"}
            choices={[
              { value: "search", label: t("检索相关片段") },
              { value: "full", label: t("整篇放进去") },
            ]}
            issues={at("mode")}
            help={t(
              "资料是一份要从头到尾照着做的短说明时用整篇：不检索，知识库里所有文档的全部片段按顺序放进 context，超过字数上限时节点报错，不截断。",
            )}
            onChange={(mode) => {
              const next = { ...node };
              if (mode === "full") {
                next.mode = "full";
                next.max_chars = node.max_chars ?? 40000;
              } else {
                delete next.mode;
                delete next.max_chars;
              }
              change(next);
            }}
          />
          {node.mode === "full" ? (
            <NumberField
              label={t("字数上限")}
              value={node.max_chars ?? 40000}
              min={1000}
              max={120000}
              issues={at("max_chars")}
              help={t("1000–120000 个字符；知识库的正文超过它时节点报错。")}
              onChange={(max_chars) => change({ ...node, max_chars })}
            />
          ) : (
            <>
              <RefField
                label={t("检索问题")}
                value={node.query}
                multiline
                variables={variables}
                issues={at("query")}
                help={t("最多 2000 字符，可引用前面节点的变量。")}
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
          )}
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
            label={t("系统提示词")}
            multiline
            value={node.system ?? ""}
            variables={variables}
            issues={at("system")}
            help={t("可选，拼在 Skill 正文后面。")}
            onChange={(system) => change({ ...node, system })}
          />
          <RefField
            label={t("提示词")}
            multiline
            rows={6}
            value={node.prompt}
            variables={variables}
            issues={at("prompt")}
            onChange={(prompt) => change({ ...node, prompt })}
          />
          {(config.conversation?.enabled || node.history) && (
            <CheckField
              label={t("带上对话历史")}
              checked={node.history ?? false}
              issues={at("history")}
              help={t("前几轮的问答按对话消息给模型。")}
              onChange={(history) => change({ ...node, history })}
            />
          )}
          <SelectField
            label={t("输出方式")}
            value={node.output.kind}
            choices={[
              { value: "text", label: t("一段文字") },
              { value: "json", label: t("几个字段（JSON）") },
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
          {node.output.kind === "text" && (
            <CheckField
              label={t("回答不写进运行过程")}
              checked={Boolean(node.quiet)}
              onChange={(quiet) => change({ ...node, quiet })}
            />
          )}
          {node.output.kind === "json" && (
            <OutputFields
              label={t("输出字段")}
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
            label={t("要分类的文本")}
            value={node.text}
            variables={variables}
            issues={at("text")}
            onChange={(value) => change({ ...node, text: value })}
          />
          <RefField
            label={t("补充说明")}
            multiline
            value={node.instruction ?? ""}
            variables={variables}
            issues={at("instruction")}
            onChange={(instruction) => change({ ...node, instruction })}
          />
          <ClassifierFields node={node} change={change} issues={issues} />
          <p className="muted">{t("都不匹配时走“都不是”。")}</p>
        </>
      );
    case "llm_review":
      return (
        <>
          <ModelFields
            node={node}
            options={options}
            change={change}
            issues={issues}
          />
          <RefField
            label={t("要审查的内容")}
            value={node.subject}
            variables={variables}
            issues={at("subject")}
            help={t("一般引用前一步的产出，比如 {{check_plan.plan}}。")}
            onChange={(subject) => change({ ...node, subject })}
          />
          <RefField
            label={t("参考资料")}
            multiline
            value={node.context ?? ""}
            variables={variables}
            issues={at("context")}
            help={t("审查员按它判断，比如需求、知识库、项目说明。")}
            onChange={(context) => change({ ...node, context })}
          />
          <RefField
            label={t("补充说明")}
            multiline
            value={node.instruction ?? ""}
            variables={variables}
            issues={at("instruction")}
            onChange={(instruction) => change({ ...node, instruction })}
          />
          <RefField
            label={t("人已经做的决定")}
            multiline
            value={node.decided ?? ""}
            variables={variables}
            issues={at("decided")}
            help={t(
              "可选，一般引用人工节点的 decisions。审查员不会把这些决定报成问题，还会核对它们都做到了。",
            )}
            onChange={(decided) => change({ ...node, decided })}
          />
          <RefField
            label={t("平台限制")}
            multiline
            value={node.limits ?? ""}
            variables={variables}
            issues={at("limits")}
            help={t(
              "可选，内容必须遵守的限制，比如 {{check_plan.limits}}。审查给的改法不能违反它们，违反的意见交给人判断，不让上一步照改。",
            )}
            onChange={(limits) => change({ ...node, limits })}
          />
          <RefField
            label={t("检查结果")}
            multiline
            value={node.evidence ?? ""}
            variables={variables}
            issues={at("evidence")}
            help={t(
              "可选，比审查员更可靠的结果，比如真实数据上的 {{checks.report}}。和它相反的意见交给人判断，不让上一步照改。",
            )}
            onChange={(evidence) => change({ ...node, evidence })}
          />
          <ReviewRules
            node={node}
            change={change}
            issues={issues}
            variables={variables}
          />
          {at("rules").map((msg) => (
            <p className="field-error" key={msg}>
              {msg}
            </p>
          ))}
          <NumberField
            label={t("连续几次没改过来就停")}
            value={node.max_attempts ?? 2}
            min={1}
            max={5}
            issues={at("max_attempts")}
            onChange={(max_attempts) => change({ ...node, max_attempts })}
          />
          <SelectField
            label={t("没改过来之后")}
            value={node.on_stuck ?? "pass"}
            issues={at("on_stuck")}
            help={t("交给人时多一个分支「交给人」，一般连到人工节点。")}
            onChange={(next) =>
              change({
                ...node,
                on_stuck: next === "branch" || next === "fail" ? next : "pass",
              })
            }
            choices={[
              { value: "pass", label: t("放行，把问题交给后面确认的人") },
              { value: "branch", label: t("交给人决定") },
              { value: "fail", label: t("报错停下") },
            ]}
          />
          <p className="muted">
            {t(
              "有问题时走“重写”，一般连回写这份内容的那一步，并在它的提示词里引用 {ref}。",
              { ref: `{{${node.id}.problems}}` },
            )}
          </p>
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
            {t("按顺序判断，第一条成立的规则决定下一步；都不成立时走“否则”。")}
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
        <AgentFields
          node={node}
          config={config}
          options={options}
          issues={issues}
          change={change}
        />
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
          label={t("模板")}
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
            <label htmlFor={`code-${node.id}`}>{t("代码")}</label>
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
              {t(
                "定义 main(inputs) 并返回包含下面返回字段的 dict。可用标准库、pandas、numpy，没有网络。",
              )}
            </small>
            {at("code").map((msg) => (
              <p className="field-error" key={msg}>
                {msg}
              </p>
            ))}
          </div>
          <ListEditor
            label={t("输入")}
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
                  label={t("名称")}
                  value={key}
                  onChange={(name) => update([name, value])}
                />
                <RefField
                  label={t("值")}
                  value={text(value)}
                  variables={variables}
                  onChange={(next) => update([key, next])}
                />
              </>
            )}
          />
          <OutputFields
            label={t("返回字段")}
            fields={node.outputs}
            prefix="outputs"
            issues={issues}
            onChange={(outputs) => change({ ...node, outputs })}
          />
          <NumberField
            label={t("超时秒数")}
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
          label={t("结果字段")}
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
                label={t("字段名")}
                value={output.key}
                onChange={(key) => update({ ...output, key })}
              />
              <ListEditor
                label={t("取值")}
                items={output.from}
                min={1}
                make={() => ""}
                onChange={(from) => update({ ...output, from })}
                render={(ref, _j, set) => (
                  <RefField
                    label={t("变量")}
                    value={ref}
                    variables={variables}
                    onChange={set}
                  />
                )}
              />
              <small className="muted">
                {t("依次取第一个有值的变量，几个分支可以汇到这里。")}
              </small>
            </>
          )}
        />
      );
  }
}

// What a check node does after its rewrites run out (docs/api/data-nodes.md, on_stuck):
// stop the run with an error, or take branch 交给人, usually to a human node.
function StuckField({
  value,
  issues,
  onChange,
}: {
  value: "fail" | "branch" | undefined;
  issues: string[];
  onChange: (value: "fail" | "branch") => void;
}) {
  const t = useT();
  return (
    <SelectField
      label={t("连续不过之后")}
      value={value ?? "fail"}
      issues={issues}
      help={t(
        "交给人时多一个分支「交给人」，一般连到人工节点：按意见重写，或终止。",
      )}
      onChange={(next) => onChange(next === "branch" ? "branch" : "fail")}
      choices={[
        { value: "fail", label: t("报错停下") },
        { value: "branch", label: t("交给人") },
      ]}
    />
  );
}
