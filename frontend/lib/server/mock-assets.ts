import "server-only";
import { isRecord } from "../runs";

export type ValidationIssue = { loc: (string | number)[]; msg: string };
export function mockJson(value: unknown, status = 200) {
  return Response.json(value, {
    status,
    headers: { "Cache-Control": "no-store" },
  });
}
export const mockError = (detail: string, status: number) =>
  mockJson({ detail }, status);
export const invalid = (issues: ValidationIssue[]) =>
  mockJson({ detail: issues }, 422);
export const noContent = () =>
  new Response(null, { status: 204, headers: { "Cache-Control": "no-store" } });
export async function readBody(
  request: Request,
): Promise<Record<string, unknown> | Response> {
  try {
    const body: unknown = await request.json();
    if (isRecord(body)) return body;
  } catch {
    // Return the same validation shape for invalid JSON and non-object bodies.
  }
  return invalid([{ loc: ["body"], msg: "请求体必须是 JSON 对象" }]);
}
export function validatePublish(body: Record<string, unknown>) {
  const issues: ValidationIssue[] = [];
  if (typeof body.note !== "string" || !body.note.trim())
    issues.push({ loc: ["body", "note"], msg: "发布说明不能为空" });
  if (body.actor !== undefined && typeof body.actor !== "string")
    issues.push({ loc: ["body", "actor"], msg: "作者必须是文字" });
  return issues;
}
