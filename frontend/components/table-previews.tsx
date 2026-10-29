import { isRecord } from "../lib/runs";

export function TablePreviews({ output }: { output: unknown }) {
  if (!isRecord(output) || !Array.isArray(output.preview)) return null;
  const previews = output.preview.filter(
    (item) =>
      isRecord(item) &&
      typeof item.table === "string" &&
      Array.isArray(item.columns) &&
      Array.isArray(item.rows),
  );
  if (!previews.length) return null;
  const totals = Array.isArray(output.tables)
    ? output.tables.filter(isRecord)
    : [];
  const display = (value: unknown) =>
    value === null
      ? "null"
      : typeof value === "object"
        ? JSON.stringify(value)
        : String(value ?? "");
  return (
    <section className="table-previews" aria-label="结果表预览">
      <h4>结果表预览</h4>
      {previews.map((preview, index) => {
        const columns = (preview.columns as unknown[]).filter(
          (value): value is string => typeof value === "string",
        );
        const rows = (preview.rows as unknown[]).filter(isRecord);
        const total = totals.find(
          (table) =>
            table.name === preview.table ||
            (preview.target && table.target === preview.target),
        );
        return (
          <div key={index} className="table-preview">
            <h5>{String(preview.table)}</h5>
            {Boolean(preview.target) && (
              <p className="muted">{String(preview.target)}</p>
            )}
            <p className="muted">
              {typeof total?.rows === "number" ? `共 ${total.rows} 行 · ` : ""}
              预览 {rows.length} 行
            </p>
            <div className="table-scroll">
              <table aria-label={`${preview.table} 预览`}>
                <thead>
                  <tr>
                    {columns.map((column) => (
                      <th key={column}>{column}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {rows.map((row, i) => (
                    <tr key={i}>
                      {columns.map((column) => (
                        <td key={column}>{display(row[column])}</td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {!rows.length && <p className="muted">这张表没有预览数据。</p>}
          </div>
        );
      })}
    </section>
  );
}
