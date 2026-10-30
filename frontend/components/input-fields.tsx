"use client";

import type { Input } from "../lib/apps";
import { isToolService, kindLabel } from "../lib/datasources";
import type { FieldValues } from "../lib/input-fields";

type Source = { datasource_id: string; name: string; kind: string };
// Tool services come last, in their own group: a start input may feed a tool node, but an
// ETL step that gets one fails with a Chinese input error.
function DatasourceOptions({ sources }: { sources: Source[] }) {
  const databases = sources.filter((source) => !isToolService(source));
  const services = sources.filter(isToolService);
  const options = (items: Source[], kind: boolean) =>
    items.map((source) => (
      <option key={source.datasource_id} value={source.datasource_id}>
        {kind ? `${source.name} · ${kindLabel(source.kind)}` : source.name}
      </option>
    ));
  return services.length ? (
    <>
      <optgroup label="数据源">{options(databases, true)}</optgroup>
      <optgroup label="工具服务">{options(services, false)}</optgroup>
    </>
  ) : (
    options(databases, true)
  );
}

export function InputFields({
  fields,
  values,
  onChange,
  errors = {},
  disabled = false,
  prefix = "input",
  datasources = [],
}: {
  fields: Input[];
  values: FieldValues;
  onChange: (values: FieldValues) => void;
  errors?: Record<string, string>;
  disabled?: boolean;
  prefix?: string;
  datasources?: Source[];
}) {
  return (
    <div className="input-fields">
      {fields.map((field) => {
        const id = `${prefix}-${field.key}`;
        const common = {
          id,
          disabled,
          "aria-invalid": Boolean(errors[field.key]),
          "aria-describedby": errors[field.key] ? `${id}-error` : undefined,
          "aria-required": field.required,
        };
        const change = (value: unknown) =>
          onChange({ ...values, [field.key]: value });
        return (
          <div className="asset-field" key={field.key}>
            <label htmlFor={id}>
              {field.label}
              {field.required && <span className="muted">（必填）</span>}
            </label>
            {field.type === "boolean" ? (
              <input
                {...common}
                type="checkbox"
                checked={values[field.key] === true}
                onChange={(e) => change(e.target.checked)}
              />
            ) : field.type === "textarea" ? (
              <textarea
                {...common}
                rows={5}
                value={String(values[field.key] ?? "")}
                onChange={(e) => change(e.target.value)}
              />
            ) : field.type === "select" || field.type === "datasource" ? (
              <select
                {...common}
                value={String(values[field.key] ?? "")}
                onChange={(e) => change(e.target.value)}
              >
                <option value="">请选择{field.label}</option>
                {field.type === "datasource" ? (
                  <DatasourceOptions sources={datasources} />
                ) : (
                  field.choices?.map((choice) => (
                    <option key={choice.value} value={choice.value}>
                      {choice.label}
                    </option>
                  ))
                )}
              </select>
            ) : (
              <input
                {...common}
                type={field.type === "number" ? "number" : "text"}
                step={field.type === "number" ? "any" : undefined}
                value={String(values[field.key] ?? "")}
                onChange={(e) =>
                  change(
                    field.type === "number" && e.target.value !== ""
                      ? Number(e.target.value)
                      : e.target.value,
                  )
                }
              />
            )}
            {field.max_length !== undefined && (
              <span className="field-hint">最多 {field.max_length} 个字符</span>
            )}
            {errors[field.key] && (
              <p className="field-error" id={`${id}-error`} role="alert">
                {errors[field.key]}
              </p>
            )}
          </div>
        );
      })}
    </div>
  );
}
