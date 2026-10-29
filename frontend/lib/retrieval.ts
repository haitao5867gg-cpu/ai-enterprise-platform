import type { RerankModel, Retrieval } from "./knowledge";
import { isRecord } from "./runs";

// Missing fields preserve the first version's vector-only behavior.
export function retrievalValue(value?: Partial<Retrieval>): Retrieval {
  return { mode: "vector", vector_weight: 0.7, rerank: null, ...value };
}

export function retrievalProblems(
  value: unknown,
  topK: number,
  models: RerankModel[],
): Record<string, string> {
  if (value === undefined) return {};
  if (!isRecord(value)) return { "": "检索方式须为对象" };
  const errors: Record<string, string> = {};
  if (
    value.mode !== undefined &&
    !["hybrid", "vector", "keyword"].includes(String(value.mode))
  )
    errors.mode = "请选择混合、向量或关键词";
  if (
    value.vector_weight !== undefined &&
    (typeof value.vector_weight !== "number" ||
      !Number.isFinite(value.vector_weight) ||
      value.vector_weight < 0 ||
      value.vector_weight > 1)
  )
    errors.vector_weight = "向量权重须为 0–1";
  if (value.rerank != null) {
    if (!isRecord(value.rerank))
      return { ...errors, rerank: "重排须为对象或 null" };
    const rerank = value.rerank;
    if (!models.some((item) => item.model === rerank.model))
      errors["rerank.model"] = "请选择可用的重排模型";
    const candidates = value.rerank.candidates ?? Math.max(20, topK);
    if (
      typeof candidates !== "number" ||
      !Number.isInteger(candidates) ||
      candidates < 5 ||
      candidates > 50
    )
      errors["rerank.candidates"] = "重排候选数须为 5–50 的整数";
    else if (candidates < topK)
      errors["rerank.candidates"] = "重排候选数不能少于 top_k";
  }
  return errors;
}
