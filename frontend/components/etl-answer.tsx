"use client";

import { useState, type ReactNode } from "react";
import { answerCsv, shown, type Answer } from "../lib/etl-v2";
import { Icon } from "./icons";

export function CopyButton({ text, label }: { text: string; label: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      type="button"
      className="text-button copy-button"
      onClick={() => {
        void navigator.clipboard?.writeText(text).then(() => {
          setCopied(true);
          setTimeout(() => setCopied(false), 1500);
        });
      }}
    >
      {copied ? "已复制" : label}
    </button>
  );
}
const WIDTH = 640;

// One measure across the groups the answer shows, in their order: bars from zero, or a
// line through the points. The table under it is the full view; each mark names its
// value on hover.
function AnswerChart({ answer }: { answer: Answer }) {
  const { chart } = answer;
  const measure = answer.columns.find((column) => column.name === chart.value);
  if (chart.type === "none" || !measure || !answer.rows.length) return null;
  const label = (row: Record<string, unknown>) =>
    chart.label.map((name) => shown(row[name])).join(" · ") || "合计";
  const points = answer.rows.map((row) => ({
    label: label(row),
    value:
      typeof row[measure.name] === "number"
        ? (row[measure.name] as number)
        : null,
  }));
  const values = points.flatMap((point) =>
    point.value === null ? [] : [point.value],
  );
  if (!values.length) return null;
  const low = Math.min(0, ...values);
  const high = Math.max(0, ...values);
  const span = high - low || 1;
  const title = `${measure.label}，按${chart.label.length ? "各组" : "合计"}`;
  if (chart.type === "line") {
    const height = 220;
    const left = 16;
    const right = WIDTH - 16;
    const top = 20;
    const bottom = height - 36;
    const x = (i: number) =>
      points.length === 1
        ? (left + right) / 2
        : left + ((right - left) * i) / (points.length - 1);
    const y = (value: number) =>
      bottom - ((value - low) / span) * (bottom - top);
    const path = points
      .map((point, i) =>
        point.value === null ? null : `${x(i)},${y(point.value)}`,
      )
      .filter(Boolean)
      .join(" ");
    return (
      <figure className="answer-chart">
        <svg
          viewBox={`0 0 ${WIDTH} ${height}`}
          role="img"
          aria-label={`折线图：${title}`}
        >
          <line
            className="chart-axis"
            x1={left}
            x2={right}
            y1={y(0)}
            y2={y(0)}
          />
          <polyline className="chart-line" points={path} />
          {points.map((point, i) =>
            point.value === null ? null : (
              <circle
                key={i}
                className="chart-dot"
                cx={x(i)}
                cy={y(point.value)}
                r={4}
              >
                <title>{`${point.label}：${shown(point.value, measure.format)}`}</title>
              </circle>
            ),
          )}
          <text className="chart-label" x={left} y={height - 12}>
            {points[0].label}
          </text>
          {points.length > 1 && (
            <text
              className="chart-label"
              x={right}
              y={height - 12}
              textAnchor="end"
            >
              {points.at(-1)!.label}
            </text>
          )}
        </svg>
        <figcaption>{title}</figcaption>
      </figure>
    );
  }
  const row = 26;
  const labelWidth = 170;
  const valueWidth = 96;
  const plot = WIDTH - labelWidth - valueWidth;
  const height = points.length * row + 8;
  const x = (value: number) => labelWidth + ((value - low) / span) * plot;
  const best = Math.max(...values);
  return (
    <figure className="answer-chart">
      <svg
        viewBox={`0 0 ${WIDTH} ${height}`}
        role="img"
        aria-label={`柱状图：${title}`}
      >
        {points.map((point, i) => {
          const middle = i * row + row / 2 + 4;
          const from = x(Math.min(0, point.value ?? 0));
          const to = x(Math.max(0, point.value ?? 0));
          return (
            <g key={i}>
              <text
                className="chart-label"
                x={labelWidth - 10}
                y={middle + 4}
                textAnchor="end"
              >
                {point.label.length > 14
                  ? `${point.label.slice(0, 13)}…`
                  : point.label}
              </text>
              {point.value !== null && (
                <rect
                  className="chart-bar"
                  x={from}
                  y={middle - 8}
                  width={Math.max(to - from, 1)}
                  height={16}
                  rx={4}
                >
                  <title>{`${point.label}：${shown(point.value, measure.format)}`}</title>
                </rect>
              )}
              {(point.value === best || point.value === null) && (
                <text className="chart-value" x={to + 6} y={middle + 4}>
                  {shown(point.value, measure.format)}
                </text>
              )}
            </g>
          );
        })}
        <line className="chart-axis" x1={x(0)} x2={x(0)} y1={0} y2={height} />
      </svg>
      <figcaption>{title}</figcaption>
    </figure>
  );
}

// The answer card (docs/api/data-nodes.md, "结果页"): the conclusion, the groups with their
// total, the chart, how the data held up and, folded, where it came from. The same card
// previews the answer when the dev run is reviewed.
export function AnswerCard({
  answer,
  heading,
  file,
}: {
  answer: Answer;
  heading?: ReactNode;
  file: string;
}) {
  const dimensions = answer.columns.filter(
    (column) => column.role === "dimension",
  );
  const totalLabel = answer.period ? `${answer.period} 合计` : "合计";
  return (
    <section className="answer-card" aria-label="答案">
      {heading && <p className="answer-heading">{heading}</p>}
      {answer.status === "error" ? (
        <p className="answer-error" role="alert">
          <Icon name="alert" size={16} />
          答案没算出来：{answer.error || "没有说明原因"}
        </p>
      ) : (
        <>
          <p className="answer-conclusion">{answer.conclusion}</p>
          {answer.rows.length > 0 && (
            <div className="table-scroll">
              <table
                className="mapping-table answer-table"
                aria-label="答案表格"
              >
                <thead>
                  <tr>
                    {answer.columns.map((column) => (
                      <th
                        key={column.name}
                        scope="col"
                        className={
                          column.role === "metric" ? "number" : undefined
                        }
                      >
                        {column.label}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {answer.rows.map((row, i) => (
                    <tr key={i}>
                      {answer.columns.map((column) => (
                        <td
                          key={column.name}
                          className={
                            column.role === "metric" ? "number" : undefined
                          }
                        >
                          {shown(row[column.name], column.format)}
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
                {dimensions.length > 0 && (
                  <tfoot>
                    <tr>
                      {answer.columns.map((column, i) => (
                        <td
                          key={column.name}
                          className={
                            column.role === "metric" ? "number" : undefined
                          }
                        >
                          {column.role === "dimension"
                            ? i === 0
                              ? totalLabel
                              : ""
                            : column.agg === "none"
                              ? "—（这个指标不能直接相加）"
                              : shown(
                                  answer.totals[column.name],
                                  column.format,
                                )}
                        </td>
                      ))}
                    </tr>
                  </tfoot>
                )}
              </table>
            </div>
          )}
          {answer.truncated && answer.groups !== null && (
            <p className="muted">
              共 {answer.groups} 组，显示前 {answer.rows.length}
            </p>
          )}
          <AnswerChart answer={answer} />
        </>
      )}
      {answer.quality && (
        <p className="answer-quality">质量：{answer.quality.text}</p>
      )}
      <div className="answer-actions">
        {answer.status === "ok" && answer.rows.length > 0 && (
          <button
            type="button"
            className="button secondary"
            onClick={() => {
              // The values as they came; the mark at the start lets spreadsheets read Chinese.
              const blob = new Blob(["﻿", answerCsv(answer)], {
                type: "text/csv;charset=utf-8",
              });
              const link = document.createElement("a");
              link.href = URL.createObjectURL(blob);
              link.download = `${file}.csv`;
              link.click();
              setTimeout(() => URL.revokeObjectURL(link.href), 1000);
            }}
          >
            <Icon name="download" size={15} />
            下载 CSV
          </button>
        )}
      </div>
      <details className="answer-details">
        <summary>技术细节</summary>
        <dl className="plan-facts">
          <div>
            <dt>表的位置</dt>
            <dd>
              <code>{answer.target}</code>
              <CopyButton text={answer.target} label="复制" />
            </dd>
          </div>
          {answer.url && (
            <div>
              <dt>Databricks</dt>
              <dd>
                <a href={answer.url} target="_blank" rel="noopener noreferrer">
                  在 Databricks 中查看这次试算
                </a>
              </dd>
            </div>
          )}
          {answer.sql && (
            <div>
              <dt>查询</dt>
              <dd>
                <p className="muted">等价的查询，只展示，平台不会执行它。</p>
                <pre className="answer-sql">{answer.sql}</pre>
                <CopyButton text={answer.sql} label="复制查询" />
              </dd>
            </div>
          )}
        </dl>
      </details>
    </section>
  );
}
