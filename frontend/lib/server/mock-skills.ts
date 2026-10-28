import "server-only";
import {
  REFERENCE_NAME_PATTERN,
  SKILL_NAME_PATTERN,
  type SkillContent,
  type SkillDetail,
  type SkillInput,
  type SkillSummary,
} from "../skills";
import { isRecord } from "../runs";
import {
  invalid,
  mockError,
  mockJson,
  noContent,
  readBody,
  validatePublish,
  type ValidationIssue,
} from "./mock-assets";

type StoredSkill = {
  name: string;
  updated_at: string;
  draft: SkillContent | null;
  versions: SkillContent[];
};
function builtIn(name: string, description: string, body: string): StoredSkill {
  const created_at = "2026-10-28T02:00:00+00:00";
  return {
    name,
    updated_at: created_at,
    draft: null,
    versions: [
      {
        version: 1,
        description,
        body,
        references: {},
        note: "平台内置",
        created_by: null,
        created_at,
      },
    ],
  };
}
const globalStore = globalThis as typeof globalThis & {
  aepMockSkills?: Map<string, StoredSkill>;
};
const store = (globalStore.aepMockSkills ??= new Map([
  [
    "etl-plan",
    builtIn(
      "etl-plan",
      "根据源表结构和需求设计 ETL 方案",
      "# ETL 方案设计\n\n先核对源表和字段，再设计目标表、增量策略和质量规则。\n明确列出需要用户确认的假设。",
    ),
  ],
  [
    "etl-codegen",
    builtIn(
      "etl-codegen",
      "按已确认的方案生成 Databricks 管道代码",
      "# ETL 代码生成\n\n使用 PySpark 编写 Lakeflow 管道。\n仅引用已探查的表和列，为质量规则添加检查。",
    ),
  ],
]));

export function mockSkillOptions() {
  return [...store.values()]
    .sort((a, b) => a.name.localeCompare(b.name))
    .map((skill) => ({
      name: skill.name,
      description: (skill.versions[0] ?? skill.draft)?.description ?? "",
      published_version: skill.versions[0]?.version ?? null,
    }));
}
export function mockSkillVersionExists(name: string, version: number) {
  return (
    store.get(name)?.versions.some((item) => item.version === version) ?? false
  );
}
async function summary(skill: StoredSkill): Promise<SkillSummary> {
  const { mockSkillUsage } = await import("./mock-apps");
  return {
    name: skill.name,
    description: (skill.versions[0] ?? skill.draft)?.description ?? "",
    published_version: skill.versions[0]?.version ?? null,
    has_draft: skill.draft !== null,
    updated_at: skill.updated_at,
    used_by: mockSkillUsage(skill.name),
  };
}
async function detail(skill: StoredSkill): Promise<SkillDetail> {
  return {
    ...(await summary(skill)),
    draft: skill.draft,
    current: skill.versions[0] ?? null,
    versions: skill.versions.map((item) => ({
      version: item.version!,
      note: item.note!,
      created_by: item.created_by,
      created_at: item.created_at,
    })),
  };
}
function validateInput(body: Record<string, unknown>): ValidationIssue[] {
  const issues: ValidationIssue[] = [];
  if (
    typeof body.description !== "string" ||
    !body.description.trim() ||
    body.description.length > 500
  )
    issues.push({
      loc: ["body", "description"],
      msg: "说明不能为空，且最多 500 字符",
    });
  if (typeof body.body !== "string" || body.body.length > 20_000)
    issues.push({
      loc: ["body", "body"],
      msg: "正文必须是文字，且最多 20000 字符",
    });
  if (body.references !== undefined) {
    if (!isRecord(body.references) || Object.keys(body.references).length > 10)
      issues.push({
        loc: ["body", "references"],
        msg: "参考资料必须是对象，最多 10 个",
      });
    else
      for (const [name, content] of Object.entries(body.references)) {
        if (!new RegExp(REFERENCE_NAME_PATTERN).test(name))
          issues.push({
            loc: ["body", "references", name],
            msg: "文件名须为 1~64 位字母、数字、下划线、点或短横线，并以 .md 结尾",
          });
        if (typeof content !== "string" || content.length > 20_000)
          issues.push({
            loc: ["body", "references", name],
            msg: "参考资料必须是文字，且最多 20000 字符",
          });
      }
  }
  return issues;
}
function draft(body: Record<string, unknown>): SkillContent {
  const input = body as SkillInput;
  return {
    version: null,
    description: input.description.trim(),
    body: input.body,
    references: structuredClone(input.references ?? {}),
    note: null,
    created_by: null,
    created_at: new Date().toISOString(),
  };
}

export async function mockSkills(
  request: Request,
  path: string[],
): Promise<Response> {
  if (path.length === 1 && request.method === "GET")
    return mockJson(
      await Promise.all(
        [...store.values()]
          .sort((a, b) => a.name.localeCompare(b.name))
          .map(summary),
      ),
    );
  if (path.length === 1 && request.method === "POST") {
    const body = await readBody(request);
    if (body instanceof Response) return body;
    const issues = validateInput(body);
    if (
      typeof body.name !== "string" ||
      !new RegExp(SKILL_NAME_PATTERN).test(body.name)
    )
      issues.push({
        loc: ["body", "name"],
        msg: "名称须以小写字母开头，包含 2~64 位小写字母、数字或短横线",
      });
    if (issues.length) return invalid(issues);
    const name = body.name as string;
    if (store.has(name)) return mockError("Skill 名称已存在", 409);
    const content = draft(body);
    const skill: StoredSkill = {
      name,
      draft: content,
      versions: [],
      updated_at: content.created_at,
    };
    store.set(name, skill);
    return mockJson(await detail(skill), 201);
  }
  const skill = store.get(path[1]);
  if (!skill) return mockError("Skill 不存在", 404);
  if (path.length === 2) return mockJson(await detail(skill));
  if (path[2] === "versions") {
    const content = skill.versions.find(
      (item) => item.version === Number(path[3]),
    );
    return content ? mockJson(content) : mockError("Skill 版本不存在", 404);
  }
  if (path[2] === "draft" && request.method === "DELETE") {
    if (!skill.draft) return mockError("没有 Skill 草稿", 404);
    skill.draft = null;
    skill.updated_at = new Date().toISOString();
    return noContent();
  }
  const body = await readBody(request);
  if (body instanceof Response) return body;
  if (path[2] === "draft") {
    const issues = validateInput(body);
    if (issues.length) return invalid(issues);
    skill.draft = draft(body);
    skill.updated_at = skill.draft.created_at;
    return mockJson(await detail(skill));
  }
  const issues = validatePublish(body);
  if (issues.length) return invalid(issues);
  if (!skill.draft) return mockError("没有可发布的 Skill 草稿", 409);
  const content: SkillContent = {
    ...skill.draft,
    version: (skill.versions[0]?.version ?? 0) + 1,
    note: (body.note as string).trim(),
    created_by:
      typeof body.actor === "string" ? body.actor.trim() || null : null,
    created_at: new Date().toISOString(),
  };
  skill.versions.unshift(content);
  skill.draft = null;
  skill.updated_at = content.created_at;
  return mockJson(await detail(skill));
}
