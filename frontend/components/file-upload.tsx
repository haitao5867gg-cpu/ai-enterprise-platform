"use client";

import { useEffect, useRef, useState } from "react";
import { ApiError, errorMessage } from "../lib/runs";
import { readUploadedDataset, type UploadedDataset } from "../lib/uploads";
import { ErrorNotice } from "./ui";

export function FileUpload({
  onUploaded,
  onBusy,
  disabled,
}: {
  onUploaded: (result: UploadedDataset) => void;
  onBusy: (busy: boolean) => void;
  disabled: boolean;
}) {
  const [name, setName] = useState("");
  const [files, setFiles] = useState<File[]>([]);
  const [uploading, setUploading] = useState(false);
  const [progress, setProgress] = useState(0);
  const [error, setError] = useState("");
  const [result, setResult] = useState<UploadedDataset | null>(null);
  const [dragging, setDragging] = useState(false);
  const active = useRef<XMLHttpRequest | null>(null);
  const input = useRef<HTMLInputElement | null>(null);
  useEffect(() => () => active.current?.abort(), []);

  function chooseFiles(chosen: File[]) {
    if (uploading || disabled) return;
    setFiles(chosen);
    setError("");
    setResult(null);
  }

  function upload() {
    if (active.current || disabled) return;
    setError("");
    setResult(null);
    if (!name.trim() || name.trim().length > 100) {
      setError("数据集名称必须为 1～100 个字符。");
      return;
    }
    if (!files.length || files.length > 20) {
      setError("请选择 1～20 个 CSV 或 XLSX 文件。");
      return;
    }
    for (const file of files) {
      if (!/\.(csv|xlsx)$/i.test(file.name)) {
        setError(`文件「${file.name}」：只支持 .csv 和 .xlsx 文件。`);
        return;
      }
      if (!file.size || file.size > 50 * 1024 * 1024) {
        setError(`文件「${file.name}」：文件不能为空，单文件不能超过 50 MB。`);
        return;
      }
    }
    const body = new FormData();
    body.append("name", name.trim());
    files.forEach((file) => body.append("files", file));
    const xhr = new XMLHttpRequest();
    active.current = xhr;
    setUploading(true);
    setProgress(0);
    onBusy(true);
    function finish() {
      active.current = null;
      setUploading(false);
      onBusy(false);
    }
    xhr.open("POST", "/api/datasources/files");
    xhr.timeout = 300_000;
    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable)
        setProgress(Math.round((event.loaded / event.total) * 100));
    };
    xhr.onload = () => {
      try {
        const value: unknown = JSON.parse(xhr.responseText);
        if (xhr.status < 200 || xhr.status >= 300) {
          const detail =
            typeof value === "object" && value !== null && "detail" in value
              ? value.detail
              : null;
          throw new ApiError(
            typeof detail === "string" ? detail : "上传失败，请重试。",
          );
        }
        const uploaded = readUploadedDataset(value);
        setResult(uploaded);
        setProgress(100);
        onUploaded(uploaded);
      } catch (err) {
        setError(errorMessage(err));
      } finally {
        finish();
      }
    };
    xhr.onerror = () => {
      setError("上传连接中断，请重试。");
      finish();
    };
    xhr.ontimeout = () => {
      setError("上传超时，请重试。");
      finish();
    };
    xhr.onabort = finish;
    xhr.send(body);
  }

  return (
    <section
      className="upload-panel"
      aria-label="上传文件面板"
      aria-busy={uploading}
    >
      <label htmlFor="dataset-name">数据集名称</label>
      <input
        id="dataset-name"
        className="upload-name"
        value={name}
        maxLength={100}
        placeholder="例如：本月订单"
        onChange={(event) => setName(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === "Enter") {
            event.preventDefault();
            upload();
          }
        }}
        disabled={uploading || disabled}
      />
      <div
        className={`upload-dropzone${dragging ? " dragging" : ""}`}
        onDragOver={(event) => {
          event.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={(event) => {
          event.preventDefault();
          setDragging(false);
          chooseFiles(Array.from(event.dataTransfer.files));
          if (input.current) input.current.value = "";
        }}
      >
        <label htmlFor="dataset-files">选择多个文件，或拖拽到这里</label>
        <input
          ref={input}
          id="dataset-files"
          type="file"
          accept=".csv,.xlsx"
          multiple
          onChange={(event) =>
            chooseFiles(Array.from(event.target.files ?? []))
          }
          disabled={uploading || disabled}
          aria-describedby="upload-limits"
        />
        <p className="field-hint" id="upload-limits">
          CSV / XLSX · 单文件最多 50 MB · 每次最多 20 个文件
        </p>
      </div>
      {files.length > 0 && (
        <ul className="upload-files">
          {files.map((file, index) => (
            <li key={`${file.name}-${index}`}>{file.name}</li>
          ))}
        </ul>
      )}
      {uploading && (
        <div className="upload-progress" role="status">
          <progress aria-label="上传进度" max={100} value={progress} />
          <span>
            {progress === 100
              ? "上传完成，正在校验并转换…"
              : `正在上传 ${progress}%`}
          </span>
        </div>
      )}
      {error && <ErrorNotice message={error} />}
      {result && (
        <div className="upload-result" role="status">
          <strong>上传成功，已选中「{result.datasource.name}」</strong>
          <ul>
            {result.tables.map((table) => (
              <li key={table.table}>
                <strong>{table.table}</strong> · {table.columns.length} 列 ·{" "}
                {table.row_count} 行
                <span>
                  {table.columns.map((column) => column.name).join("、")}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}
      <button
        type="button"
        className="button secondary"
        disabled={uploading || disabled}
        onClick={upload}
      >
        {uploading ? "正在上传…" : "上传并创建数据源"}
      </button>
    </section>
  );
}
