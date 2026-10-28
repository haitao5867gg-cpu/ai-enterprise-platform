import "server-only";
import { randomUUID } from "node:crypto";
import readXlsxFile from "read-excel-file/node";
import type { EtlLanguage, EtlPlan } from "../etl";
import type { UploadedDataset, UploadedTable } from "../uploads";

const globalStore = globalThis as typeof globalThis & {
  aepMockUploads?: Map<string, UploadedDataset>;
};
export const mockUploads = (globalStore.aepMockUploads ??= new Map<
  string,
  UploadedDataset
>());

function csvRows(text: string): string[][] {
  const rows: string[][] = [];
  let row: string[] = [],
    value = "",
    quoted = false;
  for (let i = 0; i < text.length; i++) {
    const char = text[i];
    if (char === '"') {
      if (quoted && text[i + 1] === '"') {
        value += '"';
        i++;
      } else quoted = !quoted;
    } else if (!quoted && (char === "," || char === "\n" || char === "\r")) {
      row.push(value);
      value = "";
      if (char !== ",") {
        rows.push(row);
        row = [];
        if (char === "\r" && text[i + 1] === "\n") i++;
      }
    } else value += char;
  }
  if (quoted) throw new Error("CSV 引号不完整");
  if (value || row.length) {
    row.push(value);
    rows.push(row);
  }
  return rows;
}

function table(
  original: string,
  rows: unknown[][],
  taken: Set<string>,
): UploadedTable {
  const header = rows[0];
  if (
    !header?.length ||
    header.some((cell) => typeof cell !== "string" || !cell.trim())
  )
    throw new Error("第一行必须是完整、非空的表头");
  const names = header.map((cell) => (cell as string).trim());
  if (names.every((cell) => /^[-+]?\d+(\.\d+)?$/.test(cell)))
    throw new Error("缺少表头");
  if (new Set(names.map((cell) => cell.toLowerCase())).size !== names.length)
    throw new Error("表头重复（忽略大小写和首尾空格）");
  const data = rows
    .slice(1)
    .filter((row) => row.some((cell) => cell !== null && cell !== ""));
  if (!data.length) throw new Error("文件或工作表为空，表头后至少需要一行数据");
  if (data.some((row) => row.length !== names.length))
    throw new Error("列数与表头不一致");
  let base =
    original
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "_")
      .replace(/^_+|_+$/g, "")
      .slice(0, 50) || "table";
  if (/^\d/.test(base)) base = `table_${base}`;
  let name = base,
    index = 1;
  while (taken.has(name)) name = `${base}_${++index}`;
  taken.add(name);
  return {
    table: name,
    columns: names.map((name) => ({ name, data_type: "VARCHAR" })),
    row_count: data.length,
  };
}

export async function mockUpload(request: Request) {
  try {
    const form = await request.formData();
    const name = String(form.get("name") ?? "").trim();
    const files = form.getAll("files");
    if (!name || name.length > 100)
      throw new Error("数据集名称必须为 1～100 个字符");
    if (!files.length || files.length > 20)
      throw new Error("请选择 1～20 个 CSV 或 XLSX 文件");
    if (
      [...mockUploads.values()].some((item) => item.datasource.name === name) ||
      name === "示例库"
    )
      return Response.json(
        { detail: `已存在名为「${name}」的数据源` },
        { status: 409 },
      );
    const tables: UploadedTable[] = [],
      taken = new Set<string>();
    for (const file of files) {
      if (!(file instanceof File)) throw new Error("请选择文件");
      try {
        if (!/\.(csv|xlsx)$/i.test(file.name))
          throw new Error("只支持 .csv 和 .xlsx 文件");
        if (!file.size) throw new Error("文件为空");
        if (file.size > 50 * 1024 * 1024)
          throw new Error("单文件不能超过 50 MB");
        const bytes = await file.arrayBuffer();
        if (/\.xlsx$/i.test(file.name)) {
          const sheets = await readXlsxFile(Buffer.from(bytes));
          for (const sheet of sheets)
            tables.push(table(sheet.sheet, sheet.data, taken));
        } else {
          let text: string;
          try {
            text = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
          } catch {
            text = new TextDecoder("gbk", { fatal: true }).decode(bytes);
          }
          if (text.includes("\x00")) throw new Error("CSV 含有无效的空字符");
          tables.push(
            table(file.name.replace(/\.[^.]+$/, ""), csvRows(text), taken),
          );
        }
      } catch (error) {
        throw new Error(
          `文件「${file.name}」：${error instanceof Error ? error.message : "文件解析失败"}`,
        );
      }
    }
    const id = randomUUID();
    const result: UploadedDataset = {
      dataset_id: id,
      schema: `upload_${id.slice(0, 8)}`,
      tables,
      datasource: {
        datasource_id: id,
        kind: "file",
        name,
        description: `上传文件：${tables.map((table) => table.table).join("、")}`,
      },
    };
    mockUploads.set(id, result);
    return Response.json(result, { status: 201 });
  } catch (error) {
    return Response.json(
      { detail: error instanceof Error ? error.message : "上传失败" },
      { status: 422 },
    );
  }
}

export function mockUploadedPlan(
  upload: UploadedDataset,
  language: EtlLanguage,
  feedback?: string,
): EtlPlan {
  return {
    summary: `清洗「${upload.datasource.name}」中的 ${upload.tables.length} 张表。${feedback ? `已根据意见调整：${feedback}` : ""}`,
    language,
    assumptions: [],
    targets: upload.tables.map((table) => ({
      name: `${table.table}_clean`,
      description: "保留上传表的列并清理数据。",
      sources: [`${upload.schema}.${table.table}`],
      logic: "保留原始列并清理空值。",
      columns: table.columns.map((column) => ({
        name: column.name,
        source: column.name,
        description: column.name,
      })),
      primary_key: [],
      load: { mode: "full", key: null },
      quality_rules: [],
    })),
  };
}
