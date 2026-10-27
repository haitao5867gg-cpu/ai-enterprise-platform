import { readDataSources, type DataSource } from "./etl";
import { ApiError, isRecord } from "./runs";

export type UploadedTable = {
  table: string;
  columns: { name: string; data_type: string }[];
  row_count: number;
};
export type UploadedDataset = {
  datasource: DataSource;
  dataset_id: string;
  schema: string;
  tables: UploadedTable[];
};

export function readUploadedDataset(value: unknown): UploadedDataset {
  if (
    !isRecord(value) ||
    typeof value.dataset_id !== "string" ||
    typeof value.schema !== "string" ||
    !Array.isArray(value.tables) ||
    !value.tables.length ||
    !value.tables.every(
      (table: unknown) =>
        isRecord(table) &&
        typeof table.table === "string" &&
        typeof table.row_count === "number" &&
        Number.isSafeInteger(table.row_count) &&
        table.row_count >= 0 &&
        Array.isArray(table.columns) &&
        table.columns.every(
          (column: unknown) =>
            isRecord(column) &&
            typeof column.name === "string" &&
            typeof column.data_type === "string",
        ),
    )
  )
    throw new ApiError("上传结果格式不正确，请刷新数据源列表。");
  const [datasource] = readDataSources([value.datasource]);
  return { ...(value as UploadedDataset), datasource };
}
