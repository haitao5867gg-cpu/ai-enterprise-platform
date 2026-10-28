"use client";

import Link from "next/link";
import { useEffect, useId, useState } from "react";
import type {
  AppDetail,
  AppNode,
  NodeConfig,
  Setting,
  SkillRef,
} from "../lib/apps";
import { getSkill, type SkillVersion } from "../lib/skills";
import { KnowledgePicker } from "./knowledge-form";
import { errorMessage } from "../lib/runs";

export function settingError(setting: Setting, config: NodeConfig): string {
  const value = config.params[setting.key];
  if (
    setting.type === "integer" &&
    (typeof value !== "number" ||
      !Number.isInteger(value) ||
      (setting.min !== undefined && value < setting.min) ||
      (setting.max !== undefined && value > setting.max))
  )
    return `${setting.label}须为 ${setting.min ?? "不限"}~${setting.max ?? "不限"} 的整数`;
  if (
    setting.type === "string" &&
    (typeof value !== "string" ||
      (setting.max_length !== undefined && value.length > setting.max_length))
  )
    return `${setting.label}文字长度不合法`;
  if (
    setting.type === "enum" &&
    !setting.choices.some((choice) => choice.value === value)
  )
    return `${setting.label}请选择可用选项`;
  if (
    setting.type === "knowledge_bases" &&
    (!Array.isArray(value) || !value.every((id) => typeof id === "string"))
  )
    return "请选择知识库";
  return "";
}

function SkillVersionSelect({
  name,
  version,
  latest,
  onChange,
}: {
  name: string;
  version: number;
  latest: number | null;
  onChange: (value: number) => void;
}) {
  const [versions, setVersions] = useState<SkillVersion[]>([]);
  const [error, setError] = useState("");
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    getSkill(name, controller.signal)
      .then((skill) => {
        if (!controller.signal.aborted) {
          setVersions(skill.versions);
          setError("");
        }
      })
      .catch((err) => {
        if (!controller.signal.aborted) setError(errorMessage(err));
      });
    return () => controller.abort();
  }, [name, retry]);
  const choices = [
    ...new Set([
      version,
      ...(latest ? [latest] : []),
      ...versions.map((item) => item.version),
    ]),
  ].sort((a, b) => b - a);
  return (
    <div>
      <select
        aria-label={`${name} 版本`}
        value={version}
        onChange={(event) => onChange(Number(event.target.value))}
      >
        {choices.map((item) => (
          <option key={item} value={item}>
            v{item}
            {item === latest ? "（最新）" : ""}
          </option>
        ))}
      </select>
      {error && (
        <p className="field-error" role="alert">
          {error}
          <button
            className="text-button"
            type="button"
            onClick={() => setRetry((value) => value + 1)}
          >
            重试
          </button>
        </p>
      )}
    </div>
  );
}

export function SkillPicker({
  options,
  value,
  onChange,
}: {
  options: AppDetail["options"]["skills"];
  value: SkillRef[];
  onChange: (value: SkillRef[]) => void;
}) {
  const skills = [
    ...options,
    ...value
      .filter((ref) => !options.some((option) => option.name === ref.name))
      .map((ref) => ({
        name: ref.name,
        description: "已引用的 Skill",
        published_version: ref.version,
      })),
  ];
  return (
    <div className="skill-options">
      {!skills.length && (
        <p className="muted">还没有可用 Skill。请先在 Skill 页面创建并发布。</p>
      )}
      {skills.map((skill) => {
        const ref = value.find((item) => item.name === skill.name);
        return (
          <div className="skill-option" key={skill.name}>
            <label>
              <input
                type="checkbox"
                aria-label={skill.name}
                checked={Boolean(ref)}
                disabled={!ref && skill.published_version === null}
                onChange={(event) =>
                  onChange(
                    event.target.checked && skill.published_version !== null
                      ? [
                          ...value,
                          {
                            name: skill.name,
                            version: skill.published_version,
                          },
                        ]
                      : value.filter((item) => item.name !== skill.name),
                  )
                }
              />
              <span>
                {skill.name}
                <small>
                  {skill.description}
                  {skill.published_version === null ? "（尚未发布）" : ""}
                </small>
              </span>
            </label>
            <div className="skill-option-actions">
              {ref && (
                <SkillVersionSelect
                  name={ref.name}
                  version={ref.version}
                  latest={skill.published_version}
                  onChange={(version) =>
                    onChange(
                      value.map((item) =>
                        item.name === ref.name ? { ...item, version } : item,
                      ),
                    )
                  }
                />
              )}
              <Link href={`/skills/${encodeURIComponent(skill.name)}`}>
                查看
              </Link>
            </div>
          </div>
        );
      })}
    </div>
  );
}

export function AppSettings({
  node,
  config,
  options,
  onChange,
  disabled,
}: {
  node: AppNode;
  config: NodeConfig;
  options: AppDetail["options"];
  onChange: (value: NodeConfig) => void;
  disabled: boolean;
}) {
  const prefix = useId();
  return (
    <fieldset className="node-settings" disabled={disabled}>
      <legend>节点设置</legend>
      {node.settings.map((setting) => {
        const id = `${prefix}-${setting.key}`;
        const value = config.params[setting.key];
        const error = settingError(setting, config);
        const param = (value: string | number | boolean | string[]) =>
          onChange({
            ...config,
            params: { ...config.params, [setting.key]: value },
          });
        const description = `${id}-help`;
        return (
          <div className="asset-field" key={setting.key}>
            {setting.type === "skills" || setting.type === "knowledge_bases" ? (
              <span>{setting.label}</span>
            ) : (
              <label htmlFor={id}>{setting.label}</label>
            )}
            {setting.type === "model" ? (
              <select
                id={id}
                value={config.model ?? ""}
                onChange={(event) =>
                  onChange({ ...config, model: event.target.value || null })
                }
              >
                <option value="">平台默认（{options.default_model}）</option>
                {[
                  ...new Set([
                    ...options.models,
                    ...(config.model ? [config.model] : []),
                  ]),
                ].map((model) => (
                  <option key={model} value={model}>
                    {model}
                  </option>
                ))}
              </select>
            ) : setting.type === "skills" ? (
              <SkillPicker
                options={options.skills}
                value={config.skills}
                onChange={(skills) => onChange({ ...config, skills })}
              />
            ) : setting.type === "knowledge_bases" ? (
              <KnowledgePicker
                options={options.knowledge_bases ?? []}
                value={Array.isArray(value) ? value : []}
                onChange={param}
              />
            ) : setting.type === "enum" ? (
              <select
                id={id}
                value={String(value ?? "")}
                aria-describedby={description}
                onChange={(event) => param(event.target.value)}
              >
                <option value="" disabled>
                  请选择
                </option>
                {setting.choices.map((choice) => (
                  <option key={choice.value} value={choice.value}>
                    {choice.label}
                  </option>
                ))}
              </select>
            ) : setting.type === "boolean" ? (
              <input
                id={id}
                type="checkbox"
                role="switch"
                checked={value === true}
                aria-describedby={description}
                onChange={(event) => param(event.target.checked)}
              />
            ) : setting.type === "integer" ? (
              <input
                id={id}
                type="number"
                step={1}
                min={setting.min}
                max={setting.max}
                value={typeof value === "number" ? value : ""}
                aria-invalid={Boolean(error)}
                aria-describedby={description}
                onChange={(event) =>
                  param(
                    event.target.value === "" ? "" : Number(event.target.value),
                  )
                }
              />
            ) : setting.type === "string" ? (
              <input
                id={id}
                value={String(value ?? "")}
                maxLength={setting.max_length}
                aria-describedby={description}
                onChange={(event) => param(event.target.value)}
              />
            ) : null}
            <div id={description}>
              {setting.help && <small className="muted">{setting.help}</small>}
              {error && <p className="field-error">{error}</p>}
            </div>
          </div>
        );
      })}
    </fieldset>
  );
}
