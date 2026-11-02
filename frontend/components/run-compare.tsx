"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import {
  msText,
  readFlow,
  tokensText,
  type Flow,
  type FlowStep,
} from "../lib/flow";
import {
  durationText,
  errorMessage,
  readRun,
  requestJson,
  statusText,
  type Run,
} from "../lib/runs";
import { AssetHeading } from "./asset-ui";
import { ErrorNotice } from "./ui";

type Side = { run: Run; flow: Flow | null };

async function load(id: string, signal: AbortSignal): Promise<Side> {
  const path = `/runs/${encodeURIComponent(id)}`;
  const run = readRun(await requestJson(path, { signal }));
  let flow: Flow | null = null;
  try {
    flow = readFlow(await requestJson(`${path}/flow`, { signal }));
  } catch {
    // A run without a flow still compares by its result.
  }
  return { run, flow };
}

const json = (value: unknown) => JSON.stringify(value ?? null, null, 2);

function Output({ step }: { step: FlowStep | undefined }) {
  if (!step) return <p className="muted">没有走到这一步</p>;
  return (
    <>
      <p className="flow-note">
        {step.status === "done" ? "已完成" : step.status} ·{" "}
        {msText(step.duration_ms)} · {tokensText(step.usage)}
      </p>
      <pre className="flow-json compare-output">{json(step.output)}</pre>
    </>
  );
}

function Picker({ a, b }: { a: string; b: string }) {
  const router = useRouter();
  const [left, setLeft] = useState(a);
  const [right, setRight] = useState(b);
  return (
    <form
      className="panel asset-section compare-picker"
      onSubmit={(event) => {
        event.preventDefault();
        router.push(
          `/runs/compare?a=${encodeURIComponent(left.trim())}&b=${encodeURIComponent(right.trim())}`,
        );
      }}
    >
      <label className="asset-field">
        运行 A
        <input
          required
          value={left}
          onChange={(e) => setLeft(e.target.value)}
        />
      </label>
      <label className="asset-field">
        运行 B
        <input
          required
          value={right}
          onChange={(e) => setRight(e.target.value)}
        />
      </label>
      <button className="button primary">对比</button>
    </form>
  );
}

// Two runs side by side, step by step (docs/api/evals.md, "两次运行对比").
export function RunCompare({ a, b }: { a: string; b: string }) {
  const [sides, setSides] = useState<[Side, Side] | null>(null);
  // When the runs were read: an unfinished run's time so far is counted to it.
  const [now, setNow] = useState(0);
  const [error, setError] = useState("");
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    if (!a || !b) return;
    const controller = new AbortController();
    Promise.all([load(a, controller.signal), load(b, controller.signal)])
      .then((found) => {
        if (controller.signal.aborted) return;
        setNow(Date.now());
        setSides(found);
      })
      .catch((err) => {
        if (!controller.signal.aborted) setError(errorMessage(err));
      });
    return () => controller.abort();
  }, [a, b, retry]);

  if (!a || !b)
    return (
      <div className="asset-page">
        <AssetHeading eyebrow="开发者视图" title="对比两次运行">
          填两次运行的编号（运行详情页最下面能看到）。
        </AssetHeading>
        <Picker a={a} b={b} />
      </div>
    );

  const nodes: { id: string; title: string }[] = [];
  if (sides)
    for (const side of sides)
      for (const node of side.flow?.nodes ?? [])
        if (
          node.kind !== "terminal" &&
          !nodes.some((item) => item.id === node.id)
        )
          nodes.push({ id: node.id, title: node.title });
  // The last time each run went through a node: what it ended with there.
  const last = (side: Side, node: string) =>
    side.flow?.steps.filter((step) => step.node === node).at(-1);
  const visits = (side: Side, node: string) =>
    side.flow?.steps.filter((step) => step.node === node).length ?? 0;

  return (
    <div className="asset-page run-compare">
      <Link href={`/runs/${encodeURIComponent(a)}`} className="back-link">
        ← 运行 A
      </Link>
      <AssetHeading eyebrow="开发者视图" title="对比两次运行">
        按步骤并排看两次运行，产出不同的地方标了出来。
      </AssetHeading>
      {error && (
        <ErrorNotice message={error} onRetry={() => setRetry((n) => n + 1)} />
      )}
      {!sides && !error && <p className="muted">正在加载两次运行…</p>}
      {sides && (
        <table
          className="mapping-table compare-table"
          aria-label="两次运行对比"
        >
          <thead>
            <tr>
              <th scope="col">步骤</th>
              {sides.map((side, i) => (
                <th scope="col" key={i}>
                  <Link href={`/runs/${encodeURIComponent(side.run.run_id)}`}>
                    运行 {i ? "B" : "A"}：{side.run.title || side.run.run_id}
                  </Link>
                  <small className="muted">
                    {statusText(side.run)} ·{" "}
                    {side.run.draft
                      ? "草稿"
                      : side.run.app_version != null
                        ? `v${side.run.app_version}`
                        : ""}{" "}
                    · {durationText(side.run, now)}
                  </small>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            <tr>
              <th scope="row">输入</th>
              {sides.map((side, i) => (
                <td key={i}>
                  <pre className="flow-json compare-output">
                    {json(side.run.input)}
                  </pre>
                </td>
              ))}
            </tr>
            {nodes.map((node) => {
              const [left, right] = sides.map((side) => last(side, node.id));
              const differs =
                json(left?.output) !== json(right?.output) ||
                Boolean(left) !== Boolean(right);
              return (
                <tr key={node.id} className={differs ? "differs" : ""}>
                  <th scope="row">
                    {node.title}
                    {differs && <span className="compare-flag">产出不同</span>}
                    <small className="muted monospace"> {node.id}</small>
                    {sides.some((side) => visits(side, node.id) > 1) && (
                      <small className="muted">
                        {" "}
                        · 执行次数 {visits(sides[0], node.id)} /{" "}
                        {visits(sides[1], node.id)}
                      </small>
                    )}
                  </th>
                  {sides.map((side, i) => (
                    <td key={i}>
                      <Output step={last(side, node.id)} />
                    </td>
                  ))}
                </tr>
              );
            })}
            <tr
              className={
                json(sides[0].run.result) !== json(sides[1].run.result)
                  ? "differs"
                  : ""
              }
            >
              <th scope="row">
                结果
                {json(sides[0].run.result) !== json(sides[1].run.result) && (
                  <span className="compare-flag">结果不同</span>
                )}
              </th>
              {sides.map((side, i) => (
                <td key={i}>
                  <pre className="flow-json compare-output">
                    {json(side.run.result ?? side.run.error)}
                  </pre>
                </td>
              ))}
            </tr>
          </tbody>
        </table>
      )}
      <details>
        <summary>换两次运行</summary>
        <Picker a={a} b={b} />
      </details>
    </div>
  );
}
