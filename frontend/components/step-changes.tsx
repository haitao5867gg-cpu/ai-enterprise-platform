import { Icon } from "./icons";
import { isRecord } from "../lib/runs";

// What changed since the person last saw this step: a human node's `changes`
// (docs/api/flow-apps.md, 人工节点的 changes).
export type StepChange = {
  list: string;
  kind: "added" | "removed" | "changed";
  key: string;
  label: string;
  fields: string;
  // Whether the remarks that sent it back named the item or it follows from an answer;
  // null without such remarks.
  mentioned: boolean | null;
  // Why, when it was asked for: named in the remarks, or follows from an answer
  // (jadeflow 5th retest I-3).
  because: "remarks" | "answer" | null;
  // Items of the list in which the same fields changed, listed as one entry
  // (jadeflow 6th retest I-3); [] for a single item.
  items: { key: string; label: string; fields: string }[];
};

const KINDS = ["added", "removed", "changed"] as const;
const kindText: Record<StepChange["kind"], string> = {
  added: "新加",
  removed: "去掉",
  changed: "改了",
};

export function readChanges(value: unknown): StepChange[] {
  if (!Array.isArray(value)) return [];
  return value.filter(isRecord).flatMap((item) =>
    KINDS.includes(item.kind as StepChange["kind"]) &&
    typeof item.key === "string"
      ? [
          {
            list: typeof item.list === "string" ? item.list : "",
            kind: item.kind as StepChange["kind"],
            key: item.key,
            label: typeof item.label === "string" ? item.label : item.key,
            fields: typeof item.fields === "string" ? item.fields : "",
            mentioned:
              typeof item.mentioned === "boolean" ? item.mentioned : null,
            because:
              item.because === "remarks" || item.because === "answer"
                ? item.because
                : null,
            items: Array.isArray(item.items)
              ? item.items.filter(isRecord).map((one) => ({
                  key: String(one.key ?? ""),
                  label: String(one.label ?? one.key ?? ""),
                  fields: typeof one.fields === "string" ? one.fields : "",
                }))
              : [],
          },
        ]
      : [],
  );
}

// How many items an entry stands for.
function count(item: StepChange): number {
  return Math.max(item.items.length, 1);
}

export function StepChanges({ changes }: { changes: StepChange[] }) {
  if (!changes.length) return null;
  const total = changes.reduce((sum, item) => sum + count(item), 0);
  const unasked = changes
    .filter((item) => item.mentioned === false)
    .reduce((sum, item) => sum + count(item), 0);
  return (
    <section
      className="panel step-changes"
      role="note"
      aria-label="和上次看到的版本比改了什么"
    >
      <h2>
        <Icon name="alert" size={16} />
        和你上次看到的版本比，改了 {total} 处
      </h2>
      {unasked > 0 && (
        <p>其中 {unasked} 处意见里没有提到，请确认是不是要这样改。</p>
      )}
      <ul>
        {changes.map((item) => (
          <li key={`${item.list}:${item.kind}:${item.key}`}>
            <span className={`change-kind ${item.kind}`}>
              {kindText[item.kind]}
            </span>
            {item.list}「{item.label}」
            {item.fields && <span className="muted">：{item.fields}</span>}
            {item.mentioned === false && (
              <span className="app-tag unasked">意见里没提到</span>
            )}
            {item.because === "answer" && (
              <span className="app-tag answered">按你的回答</span>
            )}
            {item.items.length > 1 && (
              <details className="change-items">
                <summary>看这 {item.items.length} 条</summary>
                <ul>
                  {item.items.map((one) => (
                    <li key={one.key}>
                      「{one.label}」
                      {one.fields && (
                        <span className="muted">：{one.fields}</span>
                      )}
                    </li>
                  ))}
                </ul>
              </details>
            )}
          </li>
        ))}
      </ul>
    </section>
  );
}
