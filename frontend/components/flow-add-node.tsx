import type { FlowConfig, NodeType } from "../lib/apps";

const categories: { key: NodeType["category"]; label: string }[] = [
  { key: "model", label: "模型" },
  { key: "logic", label: "逻辑" },
  { key: "tool", label: "工具" },
  { key: "human", label: "人工" },
  { key: "terminal", label: "开始和结束" },
];
export type Pending = { from: string; branch: string } | null;

export function AddNodePanel({
  types,
  config,
  pending,
  add,
  close,
}: {
  types: NodeType[];
  config: FlowConfig;
  pending: Pending;
  add: (type: NodeType, preset?: NodeType["presets"][number]) => void;
  close: () => void;
}) {
  return (
    <div
      className="add-node"
      role="dialog"
      aria-label={
        pending
          ? `新建节点并连到「${config.graph.nodes.find((n) => n.id === pending.from)?.title ?? pending.from}」`
          : "添加节点"
      }
    >
      <div className="asset-section-heading">
        <h3>
          {pending
            ? `新建节点并连到「${config.graph.nodes.find((n) => n.id === pending.from)?.title ?? pending.from}」`
            : "添加节点"}
        </h3>
        <button type="button" className="text-button" onClick={close}>
          取消
        </button>
      </div>
      {categories.map((category) => {
        const list = types.filter((type) => type.category === category.key);
        if (!list.length) return null;
        return (
          <section key={category.key} aria-label={category.label}>
            <h4>{category.label}</h4>
            <div className="node-type-grid">
              {list.map((type) => {
                const full =
                  type.max_count !== null &&
                  config.graph.nodes.filter((node) => node.type === type.type)
                    .length >= type.max_count;
                return (
                  <div className="node-type" key={type.type}>
                    <button
                      type="button"
                      disabled={full}
                      onClick={() => add(type)}
                      aria-describedby={`type-${type.type}`}
                    >
                      {type.label}
                    </button>
                    <small id={`type-${type.type}`}>
                      {full ? `最多 ${type.max_count} 个。` : type.description}
                    </small>
                    {!full &&
                      type.presets.map((preset) => (
                        <button
                          key={preset.key}
                          type="button"
                          className="text-button"
                          title={preset.description}
                          onClick={() => add(type, preset)}
                        >
                          {type.label} · {preset.label}
                        </button>
                      ))}
                  </div>
                );
              })}
            </div>
          </section>
        );
      })}
    </div>
  );
}
