import type { Input } from "./apps";
import type { ValidationIssue } from "./runs";

export type FieldValues = Record<string, unknown>;
export function defaultValues(fields: Input[]): FieldValues {
  return Object.fromEntries(
    fields
      .filter((field) => field.default !== undefined && field.default !== null)
      .map((field) => [field.key, field.default]),
  );
}
export function initialValues(fields: Input[]): FieldValues {
  return Object.fromEntries(
    fields.map((field) => [
      field.key,
      field.default ?? (field.type === "boolean" ? false : ""),
    ]),
  );
}
export function validateFields(
  fields: Input[],
  values: FieldValues,
  section: "input" | "values",
): ValidationIssue[] {
  return fields.flatMap((field) => {
    const value = values[field.key];
    const empty =
      value === null ||
      value === undefined ||
      (typeof value === "string" && !value.trim());
    let msg = "";
    if (empty) {
      if (field.required) msg = `${field.label}为必填项`;
    } else if (field.type === "number") {
      if (typeof value !== "number" || !Number.isFinite(value))
        msg = `${field.label}必须为数字`;
    } else if (field.type === "boolean") {
      if (typeof value !== "boolean") msg = `${field.label}必须为布尔值`;
    } else if (typeof value !== "string") msg = `${field.label}必须为文字`;
    else if (
      (field.type === "text" || field.type === "textarea") &&
      field.max_length !== undefined &&
      value.length > field.max_length
    )
      msg = `${field.label}最多 ${field.max_length} 个字符`;
    else if (
      field.type === "select" &&
      !field.choices?.some((choice) => choice.value === value)
    )
      msg = `${field.label}请选择有效选项`;
    return msg ? [{ loc: ["body", section, field.key], msg }] : [];
  });
}
// An approval's questions: each must be answered, and like a field otherwise.
export function questionErrors(
  questions: Input[],
  values: FieldValues,
): Record<string, string> {
  return Object.fromEntries(
    questions.flatMap((question) => {
      const value = values[question.key];
      if (
        value === undefined ||
        value === null ||
        (typeof value === "string" && !value.trim())
      )
        return [
          [
            question.key,
            question.type === "select" || question.type === "boolean"
              ? "请选一个答案"
              : "请填写答案",
          ],
        ];
      return validateFields([question], values, "values").map((issue) => [
        question.key,
        issue.msg,
      ]);
    }),
  );
}
export function inputPayload(
  fields: Input[],
  values: FieldValues,
): FieldValues {
  return Object.fromEntries(
    fields.flatMap((field) => {
      const value = values[field.key];
      if (
        !field.required &&
        (value === "" || value === undefined || value === null)
      )
        return [];
      return [[field.key, value]];
    }),
  );
}
