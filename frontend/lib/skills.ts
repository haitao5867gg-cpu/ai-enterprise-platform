import { ApiError, isRecord, requestJson } from "./runs";

export const SKILL_NAME_PATTERN = "^[a-z][a-z0-9-]{1,63}$";
export const REFERENCE_NAME_PATTERN = "^[\\w.-]{1,64}\\.md$";
export type SkillUsage = { app: string; node: string; version: number };
export type SkillSummary = {
  name: string;
  description: string;
  published_version: number | null;
  has_draft: boolean;
  updated_at: string;
  used_by: SkillUsage[];
};
export type SkillVersion = {
  version: number;
  note: string;
  created_by: string | null;
  created_at: string;
};
export type SkillContent = {
  version: number | null;
  description: string;
  body: string;
  references: Record<string, string>;
  note: string | null;
  created_by: string | null;
  created_at: string;
};
export type SkillDetail = SkillSummary & {
  draft: SkillContent | null;
  versions: SkillVersion[];
  current: SkillContent | null;
};
export type SkillInput = Pick<
  SkillContent,
  "description" | "body" | "references"
>;

function isSummary(
  value: unknown,
): value is SkillSummary & Record<string, unknown> {
  return (
    isRecord(value) &&
    typeof value.name === "string" &&
    typeof value.description === "string" &&
    (value.published_version === null ||
      Number.isInteger(value.published_version)) &&
    typeof value.has_draft === "boolean" &&
    Array.isArray(value.used_by)
  );
}
function isContent(value: unknown): value is SkillContent {
  return (
    isRecord(value) &&
    (value.version === null || Number.isInteger(value.version)) &&
    typeof value.description === "string" &&
    typeof value.body === "string" &&
    isRecord(value.references) &&
    Object.values(value.references).every((item) => typeof item === "string")
  );
}
export function readSkillDetail(value: unknown): SkillDetail {
  if (
    !isRecord(value) ||
    !isSummary(value) ||
    !Array.isArray(value.versions) ||
    !(value.current === null || isContent(value.current)) ||
    !(value.draft === null || isContent(value.draft))
  ) {
    throw new ApiError("Skill 详情格式不正确，请稍后重试。");
  }
  return value as SkillDetail;
}
const pathFor = (name: string) => `/skills/${encodeURIComponent(name)}`;
export async function listSkills(
  signal?: AbortSignal,
): Promise<SkillSummary[]> {
  const value = await requestJson("/skills", { signal });
  if (!Array.isArray(value) || !value.every(isSummary))
    throw new ApiError("Skill 列表格式不正确，请稍后重试。");
  return value;
}
export async function getSkill(name: string, signal?: AbortSignal) {
  return readSkillDetail(await requestJson(pathFor(name), { signal }));
}
export async function getSkillVersion(
  name: string,
  version: number,
  signal?: AbortSignal,
): Promise<SkillContent> {
  const value = await requestJson(`${pathFor(name)}/versions/${version}`, {
    signal,
  });
  if (!isContent(value))
    throw new ApiError("Skill 版本格式不正确，请稍后重试。");
  return value;
}
export async function createSkill(input: SkillInput & { name: string }) {
  return readSkillDetail(
    await requestJson("/skills", {
      method: "POST",
      body: JSON.stringify(input),
    }),
  );
}
export async function saveSkillDraft(name: string, input: SkillInput) {
  return readSkillDetail(
    await requestJson(`${pathFor(name)}/draft`, {
      method: "PUT",
      body: JSON.stringify(input),
    }),
  );
}
export const deleteSkillDraft = (name: string) =>
  requestJson(`${pathFor(name)}/draft`, { method: "DELETE" });
export async function publishSkillDraft(
  name: string,
  note: string,
  actor?: string,
) {
  return readSkillDetail(
    await requestJson(`${pathFor(name)}/publish`, {
      method: "POST",
      body: JSON.stringify({ note, ...(actor ? { actor } : {}) }),
    }),
  );
}
