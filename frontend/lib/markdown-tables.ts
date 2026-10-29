// A small remark extension for pipe tables, using react-markdown's existing parser
// for cell contents. Fenced code and inline Markdown keep their normal behavior.
type Node = {
  type: string;
  children?: Node[];
  position?: { start: { offset?: number }; end: { offset?: number } };
  align?: ("left" | "right" | "center" | null)[];
};

function cells(line: string): string[] {
  const row: string[] = [];
  let cell = "",
    fence = 0;
  const value = line.trim();
  for (let i = 0; i < value.length; i++) {
    const char = value[i];
    if (char === "\\" && i + 1 < value.length) {
      cell += char + value[++i];
    } else if (char === "`") {
      let count = 1;
      while (value[i + count] === "`") count++;
      if (!fence) fence = count;
      else if (count === fence) fence = 0;
      cell += "`".repeat(count);
      i += count - 1;
    } else if (char === "|" && !fence) {
      row.push(cell.trim());
      cell = "";
    } else cell += char;
  }
  row.push(cell.trim());
  if (value.startsWith("|")) row.shift();
  if (value.endsWith("|") && !value.endsWith("\\|")) row.pop();
  return row;
}

export function remarkTables(this: { parse: (text: string) => unknown }) {
  const parse = (text: string) => this.parse(text) as Node;
  return (tree: unknown, file: { toString: () => string }) => {
    const source = file.toString();
    function visit(parent: Node) {
      if (!parent.children) return;
      parent.children = parent.children.flatMap((node): Node[] => {
        if (
          node.type !== "paragraph" ||
          node.position?.start.offset === undefined ||
          node.position.end.offset === undefined
        ) {
          visit(node);
          return [node];
        }
        const lines = source
          .slice(node.position.start.offset, node.position.end.offset)
          .split(/\r?\n/);
        const output: Node[] = [];
        let start = 0;
        for (let i = 0; i + 1 < lines.length; i++) {
          if (!lines[i].includes("|")) continue;
          const header = cells(lines[i]);
          const separator = cells(lines[i + 1]);
          if (
            !header.length ||
            header.length !== separator.length ||
            !separator.every((cell) => /^:?-{3,}:?$/.test(cell))
          )
            continue;
          if (start < i)
            output.push(
              ...(parse(lines.slice(start, i).join("\n")).children ?? []),
            );
          let end = i + 2;
          while (
            end < lines.length &&
            lines[end].includes("|") &&
            lines[end].trim()
          )
            end++;
          const rows = [header, ...lines.slice(i + 2, end).map(cells)];
          output.push({
            type: "table",
            align: separator.map((cell) =>
              cell.startsWith(":")
                ? cell.endsWith(":")
                  ? "center"
                  : "left"
                : cell.endsWith(":")
                  ? "right"
                  : null,
            ),
            children: rows.map((row) => ({
              type: "tableRow",
              children: header.map((_cell, index) => ({
                type: "tableCell",
                children: parse(row[index] ?? "").children?.[0]?.children ?? [],
              })),
            })),
          });
          start = end;
          i = end - 1;
        }
        if (!output.length) return [node];
        if (start < lines.length)
          output.push(...(parse(lines.slice(start).join("\n")).children ?? []));
        return output;
      });
    }
    visit(tree as Node);
  };
}
