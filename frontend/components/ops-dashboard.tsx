"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { kindLabel } from "../lib/catalog";
import {
  budgetStateLabels,
  deleteBudget,
  duration,
  hours,
  MAX_BUDGET,
  MAX_PRICE,
  percent,
  PERIODS,
  setBudget,
  setComputePrice,
  successRate,
  usd,
  getSummary,
  type AppMetrics,
  type Budget,
  type Day,
  type Period,
  type Summary,
} from "../lib/ops";
import { errorMessage } from "../lib/runs";
import { AssetHeading, AssetLoading } from "./asset-ui";
import { Dialog, useActor } from "./catalog-list";
import { useMay } from "./session";
import { ErrorNotice } from "./ui";

// docs/api/ops.md: how much the platform is used, how well runs go, what they cost,
// and per-app monthly model budgets.

const number = new Intl.NumberFormat("zh-CN");

function Card({
  label,
  value,
  note,
}: {
  label: string;
  value: string;
  note?: string;
}) {
  return (
    <div className="ops-card">
      <span>{label}</span>
      <strong>{value}</strong>
      {note && <p className="muted">{note}</p>}
    </div>
  );
}

function Overview({ data }: { data: Summary }) {
  const t = data.totals;
  return (
    <section className="ops-cards" aria-label="总览">
      <Card
        label="运行数"
        value={number.format(t.runs)}
        note={`成功 ${t.done}，失败 ${t.error}，取消 ${t.cancelled}${t.active ? `，进行中 ${t.active}` : ""}`}
      />
      <Card
        label="成功率"
        value={percent(successRate(t))}
        note="结束的运行里成功的比例（不算取消）"
      />
      <Card
        label="一次通过率"
        value={percent(t.first_pass_rate)}
        note="首次运行没重跑就成功交付的比例"
      />
      <Card
        label="交付时长（中位数）"
        value={duration(t.delivery_seconds_median)}
        note={
          t.wait_seconds_median === null
            ? "从新建到成功结束，含等人确认"
            : `含等人确认 ${duration(t.wait_seconds_median)}`
        }
      />
      <Card
        label="模型费用"
        value={usd(t.model_cost_usd)}
        note={`${number.format(t.model_calls)} 次调用${t.unpriced_calls ? `，${t.unpriced_calls} 次网关没报费用` : ""}`}
      />
      <Card
        label="Databricks"
        value={hours(t.compute_seconds)}
        note={
          t.compute_cost_usd === null
            ? "没设单价，算不出费用"
            : `按单价估算 ${usd(t.compute_cost_usd)}，不是账单`
        }
      />
    </section>
  );
}

const dayLabel = (date: string) => {
  const [, month, day] = date.split("-");
  return `${Number(month)}/${Number(day)}`;
};

function Bars({
  title,
  days,
  value,
  format,
}: {
  title: string;
  days: Day[];
  value: (day: Day) => number;
  format: (day: Day) => string;
}) {
  const [hover, setHover] = useState<number | null>(null);
  const values = days.map(value);
  const max = Math.max(...values, 0);
  const width = 600;
  const height = 120;
  const step = width / days.length;
  const bar = Math.max(2, Math.min(18, step - 2));
  const shown = hover === null ? null : days[hover];
  return (
    <figure className="ops-chart">
      <figcaption>
        <strong>{title}</strong>
        <span className="muted" aria-live="polite">
          {shown
            ? `${shown.date}：${format(shown)}`
            : "鼠标移到柱子上看当天数字"}
        </span>
      </figcaption>
      <svg
        viewBox={`0 0 ${width} ${height + 18}`}
        role="img"
        aria-label={`${title}，按天的柱状图，表格见下方`}
        onMouseLeave={() => setHover(null)}
      >
        <line x1={0} x2={width} y1={height} y2={height} className="ops-axis" />
        {days.map((day, index) => {
          const h = max ? (values[index] / max) * (height - 6) : 0;
          const x = index * step + (step - bar) / 2;
          // Rounded only at the data end; square on the baseline.
          const r = Math.min(4, bar / 2, h);
          return (
            <g key={day.date} onMouseEnter={() => setHover(index)}>
              <rect
                x={index * step}
                y={0}
                width={step}
                height={height}
                fill="transparent"
              />
              {h > 0 && (
                <path
                  className={hover === index ? "ops-bar active" : "ops-bar"}
                  d={`M${x},${height} v${r - h} q0,${-r} ${r},${-r} h${bar - 2 * r} q${r},0 ${r},${r} v${h - r} z`}
                />
              )}
            </g>
          );
        })}
        {[0, days.length - 1].map((index) => (
          <text
            key={index}
            x={index === 0 ? 0 : width}
            y={height + 14}
            textAnchor={index === 0 ? "start" : "end"}
            className="ops-tick"
          >
            {dayLabel(days[index].date)}
          </text>
        ))}
      </svg>
    </figure>
  );
}

function Daily({ days }: { days: Day[] }) {
  return (
    <section className="panel ops-section" aria-label="每天">
      <div className="ops-charts">
        <Bars
          title="每天的运行数"
          days={days}
          value={(day) => day.runs}
          format={(day) =>
            `${day.runs} 次运行，成功 ${day.done}，失败 ${day.error}`
          }
        />
        <Bars
          title="每天的模型费用"
          days={days}
          value={(day) => day.model_cost_usd ?? 0}
          format={(day) => `模型费用 ${usd(day.model_cost_usd)}`}
        />
      </div>
      <details className="ops-table-toggle">
        <summary>按天看表格</summary>
        <div className="table-scroll">
          <table className="mapping-table" aria-label="每天的数字">
            <thead>
              <tr>
                {["日期", "运行", "成功", "失败", "模型费用"].map((h) => (
                  <th scope="col" key={h}>
                    {h}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {[...days].reverse().map((day) => (
                <tr key={day.date}>
                  <td>{day.date}</td>
                  <td>{day.runs}</td>
                  <td>{day.done}</td>
                  <td>{day.error}</td>
                  <td>{usd(day.model_cost_usd)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </details>
    </section>
  );
}

export function BudgetCell({ budget }: { budget: Budget | null }) {
  if (!budget) return <span className="muted">没设</span>;
  return (
    <span className="ops-budget">
      <span>
        {usd(budget.month_cost_usd)} / {usd(budget.monthly_usd)}
      </span>
      <span className={`ops-budget-state ${budget.state}`}>
        {budgetStateLabels[budget.state]}
        {budget.block ? "" : "（只提醒）"}
      </span>
    </span>
  );
}

export function BudgetDialog({
  app,
  title,
  budget,
  close,
  saved,
}: {
  app: string;
  title: string;
  budget: Budget | null;
  close: () => void;
  saved: (budget: Budget | null) => void;
}) {
  const actor = useActor();
  const [monthly, setMonthly] = useState(
    budget ? String(budget.monthly_usd) : "",
  );
  const [warn, setWarn] = useState(
    String(Math.round((budget?.warn_at ?? 0.8) * 100)),
  );
  const [block, setBlock] = useState(budget?.block ?? true);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function save() {
    const amount = Number(monthly);
    const line = Number(warn);
    if (!monthly.trim() || !(amount >= 0.01 && amount <= MAX_BUDGET))
      return setError(
        `每月预算要在 $0.01 到 $${number.format(MAX_BUDGET)} 之间。`,
      );
    if (!(line >= 50 && line <= 100))
      return setError("提醒线要在 50% 到 100% 之间。");
    setBusy(true);
    setError("");
    try {
      saved(
        await setBudget(app, {
          monthly_usd: amount,
          warn_at: line / 100,
          block,
          actor: actor(),
        }),
      );
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  async function remove() {
    if (!window.confirm(`确定取消「${title}」的模型预算吗？`)) return;
    setBusy(true);
    setError("");
    try {
      await deleteBudget(app);
      saved(null);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog title={`「${title}」的模型预算`} close={close}>
      <form
        className="asset-form"
        noValidate
        onSubmit={(event) => {
          event.preventDefault();
          void save();
        }}
      >
        {error && <ErrorNotice message={error} />}
        <label className="asset-field">
          每月预算（美元）
          <input
            type="number"
            min={0.01}
            max={MAX_BUDGET}
            step="any"
            inputMode="decimal"
            value={monthly}
            onChange={(event) => setMonthly(event.target.value)}
          />
          <span className="field-hint">
            按平台时区的自然月算，只算网关报了费用的调用。
            {budget && `本月已用 ${usd(budget.month_cost_usd)}。`}
          </span>
        </label>
        <label className="asset-field">
          用到多少时提醒（%）
          <input
            type="number"
            min={50}
            max={100}
            step={5}
            value={warn}
            onChange={(event) => setWarn(event.target.value)}
          />
          <span className="field-hint">
            过了提醒线和用完时，各给管理员发一条通知，每月各一次。
          </span>
        </label>
        <label className="check-field">
          <input
            type="checkbox"
            checked={block}
            onChange={(event) => setBlock(event.target.checked)}
          />
          用完后拦下这个应用的模型调用
        </label>
        <p className="field-hint">
          拦下后节点按模型调用失败处理。本月已用每 30
          秒更新一次，可能会超出一点。 不勾就只提醒，不拦截。
        </p>
        <div className="asset-actions">
          <button type="submit" className="button primary" disabled={busy}>
            保存
          </button>
          {budget && (
            <button
              type="button"
              className="button reject"
              disabled={busy}
              onClick={() => void remove()}
            >
              取消预算
            </button>
          )}
        </div>
      </form>
    </Dialog>
  );
}

function AppTable({
  apps,
  admin,
  edit,
}: {
  apps: AppMetrics[];
  admin: boolean;
  edit: (app: AppMetrics) => void;
}) {
  if (!apps.length)
    return <p className="muted ops-empty">这段时间没有运行。</p>;
  const headings = [
    "应用",
    "运行",
    "成功率",
    "一次通过率",
    "交付时长",
    "模型费用",
    "Databricks",
    "本月模型预算",
    ...(admin ? ["操作"] : []),
  ];
  return (
    <div className="table-scroll">
      <table className="mapping-table ops-table" aria-label="各应用">
        <thead>
          <tr>
            {headings.map((h) => (
              <th scope="col" key={h}>
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {apps.map((app) => (
            <tr key={app.app}>
              <td>
                <Link href={`/apps/${encodeURIComponent(app.app)}`}>
                  {app.title}
                </Link>
              </td>
              <td>
                {app.runs}
                <span className="muted">
                  {" "}
                  成功 {app.done} · 失败 {app.error}
                </span>
              </td>
              <td>{percent(successRate(app))}</td>
              <td>{percent(app.first_pass_rate)}</td>
              <td>{duration(app.delivery_seconds_median)}</td>
              <td>{usd(app.model_cost_usd)}</td>
              <td>
                {hours(app.compute_seconds)}
                {app.compute_cost_usd !== null && app.compute_seconds > 0 && (
                  <span className="muted"> ≈ {usd(app.compute_cost_usd)}</span>
                )}
              </td>
              <td>
                <BudgetCell budget={app.budget} />
              </td>
              {admin && (
                <td>
                  <button
                    type="button"
                    className="text-button"
                    aria-label={`设「${app.title}」的预算`}
                    onClick={() => edit(app)}
                  >
                    设预算
                  </button>
                </td>
              )}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function ReuseSection({ reuse }: { reuse: Summary["reuse"] }) {
  if (!reuse) return null;
  return (
    <section className="panel ops-section" aria-label="资产复用">
      <h2>资产复用</h2>
      <p className="muted">
        资产目录里有 {reuse.assets} 项资产，其中 {reuse.reused}{" "}
        项被两个及以上应用用到；
        {reuse.apps_from_templates} 个应用是从模板新建的。
      </p>
      {reuse.top.length > 0 && (
        <ol className="ops-reuse">
          {reuse.top.map((item) => (
            <li key={`${item.kind}:${item.id}`}>
              <Link href={item.link}>{item.title}</Link>
              <span className="muted">
                {" "}
                {kindLabel(item.kind)} · {item.used_by} 个应用在用
              </span>
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}

function PriceSetting({
  price,
  saved,
}: {
  price: number | null;
  saved: (value: number | null) => void;
}) {
  const [value, setValue] = useState(price === null ? "" : String(price));
  const [note, setNote] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  async function save() {
    const text = value.trim();
    const amount = text ? Number(text) : null;
    if (amount !== null && !(amount >= 0 && amount <= MAX_PRICE))
      return setError(`单价要在 0 到 ${MAX_PRICE} 之间，不填表示不估算。`);
    setBusy(true);
    setError("");
    setNote("");
    try {
      const out = await setComputePrice(amount);
      saved(out.compute_hourly_usd);
      setNote("已保存。");
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }
  return (
    <section className="panel ops-section" aria-label="Databricks 单价">
      <h2>Databricks 单价</h2>
      <form
        className="ops-price"
        noValidate
        onSubmit={(event) => {
          event.preventDefault();
          void save();
        }}
      >
        <label className="asset-field">
          每小时多少美元
          <input
            type="number"
            min={0}
            max={MAX_PRICE}
            step="any"
            inputMode="decimal"
            value={value}
            onChange={(event) => setValue(event.target.value)}
            aria-describedby="ops-price-hint"
          />
        </label>
        <button type="submit" className="button" disabled={busy}>
          保存
        </button>
      </form>
      <p className="field-hint" id="ops-price-hint" role="status">
        {error ||
          note ||
          "平台读不到 Databricks 账单，按作业时长 × 这个单价估算。单价按集群规格估：DBU 单价 × 每小时 DBU 数 + 云主机费用。不填就不估算。"}
      </p>
    </section>
  );
}

export function OpsDashboard() {
  const [days, setDays] = useState<Period>(30);
  const [data, setData] = useState<Summary | null>(null);
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(0);
  const [editing, setEditing] = useState<AppMetrics | null>(null);
  const [notice, setNotice] = useState("");
  const admin = useMay("admin");
  useEffect(() => {
    const controller = new AbortController();
    getSummary(days, controller.signal)
      .then((value) => {
        setData(value);
        setError("");
      })
      .catch((err) => {
        if (!controller.signal.aborted) setError(errorMessage(err));
      });
    return () => controller.abort();
  }, [days, revision]);

  return (
    <div className="asset-page">
      <AssetHeading title="运营看板">
        平台用得怎么样、运行顺不顺、花了多少钱。统计期内新建的运行都算，草稿的试跑和评测不算。
        模型费用来自模型网关的报价，Databricks 费用按单价估算，都不是账单。
      </AssetHeading>
      <div className="filter-tabs" role="group" aria-label="统计期">
        {PERIODS.map((value) => (
          <button
            key={value}
            type="button"
            aria-pressed={days === value}
            className={days === value ? "selected" : ""}
            onClick={() => setDays(value)}
          >
            近 {value} 天
          </button>
        ))}
      </div>
      {error && (
        <ErrorNotice
          message={error}
          onRetry={() => setRevision((n) => n + 1)}
        />
      )}
      {notice && (
        <p className="datasource-note" role="status">
          {notice}
        </p>
      )}
      {!data && !error && <AssetLoading />}
      {data && (
        <>
          <Overview data={data} />
          <Daily days={data.daily} />
          <section className="panel ops-section" aria-label="各应用">
            <h2>各应用</h2>
            <AppTable
              apps={data.apps}
              admin={admin}
              edit={(app) => {
                setNotice("");
                setEditing(app);
              }}
            />
          </section>
          <ReuseSection reuse={data.reuse} />
          {admin && (
            <PriceSetting
              key={String(data.compute_hourly_usd)}
              price={data.compute_hourly_usd}
              saved={() => setRevision((n) => n + 1)}
            />
          )}
        </>
      )}
      {editing && (
        <BudgetDialog
          app={editing.app}
          title={editing.title}
          budget={editing.budget}
          close={() => setEditing(null)}
          saved={(budget) => {
            setNotice(
              budget
                ? `已保存「${editing.title}」的预算。`
                : `已取消「${editing.title}」的预算。`,
            );
            setEditing(null);
            setRevision((n) => n + 1);
          }}
        />
      )}
    </div>
  );
}
