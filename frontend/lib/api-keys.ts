import type { Input } from "./apps";
import { requestJson } from "./runs";

// docs/api/app-access.md is the source of truth for these wire types.
export type ApiKey = {
  key_id: string;
  app: string;
  name: string;
  prefix: string;
  created_by: string | null;
  created_at: string;
  last_used_at: string | null;
};
export type NewApiKey = ApiKey & { secret: string };

const base = (app: string) => `/apps/${encodeURIComponent(app)}/keys`;
export const listKeys = (app: string, signal?: AbortSignal) =>
  requestJson(base(app), { signal }) as Promise<ApiKey[]>;
export const createKey = (app: string, name: string) =>
  requestJson(base(app), {
    method: "POST",
    body: JSON.stringify({ name }),
  }) as Promise<NewApiKey>;
export const deleteKey = (app: string, keyId: string) =>
  requestJson(`${base(app)}/${encodeURIComponent(keyId)}`, {
    method: "DELETE",
  });

// An example input for the docs on the page: each required input (else the first),
// with a placeholder of its type.
export function exampleInput(inputs: Input[] | null): Record<string, unknown> {
  if (inputs === null) return { request: "按月统计各城市的销售额" };
  const shown = inputs.filter((f) => f.required && f.default == null);
  const fields = shown.length ? shown : inputs.slice(0, 1);
  return Object.fromEntries(
    fields.map((f) => [
      f.key,
      f.type === "number"
        ? 1
        : f.type === "boolean"
          ? true
          : f.type === "select"
            ? (f.choices?.[0]?.value ?? "")
            : (f.examples?.[0] ?? `${f.label}……`),
    ]),
  );
}
