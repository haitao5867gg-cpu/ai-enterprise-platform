// Single line breaks stay line breaks, as they did when replies were shown as
// plain text. Code keeps its own text: only paragraph text is split.
type Node = { type: string; value?: string; children?: Node[] };

export function remarkBreaks() {
  return (tree: unknown) => {
    function visit(parent: Node) {
      if (!parent.children) return;
      parent.children = parent.children.flatMap((node): Node[] => {
        if (node.type === "text" && node.value?.includes("\n"))
          return node.value
            .split(/\r?\n/)
            .flatMap((value, i): Node[] =>
              i
                ? [{ type: "break" }, { type: "text", value }]
                : [{ type: "text", value }],
            );
        visit(node);
        return [node];
      });
    }
    visit(tree as Node);
  };
}
