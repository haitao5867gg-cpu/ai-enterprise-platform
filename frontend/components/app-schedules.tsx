"use client";

import Link from "next/link";
import { useEffect, useId, useRef, useState } from "react";
import { savedActor } from "../lib/actor";
import type { Input, ProjectUse } from "../lib/apps";
import {
  initialValues,
  inputPayload,
  validateFields,
  type FieldValues,
} from "../lib/input-fields";
import { getProject } from "../lib/projects";
import { errorMessage, fieldErrors, statusLabels } from "../lib/runs";
import type { RunStatus } from "../lib/runs";
import {
  cronText,
  createSchedule,
  DEFAULT_PRESET,
  deleteSchedule,
  listSchedules,
  PRESET_LABELS,
  presetCron,
  previewSchedule,
  readPreset,
  runSchedule,
  TIMEZONES,
  updateSchedule,
  WEEKDAYS,
  zonedTime,
  type Preset,
  type PresetKind,
  type Schedule,
  type ScheduleBody,
} from "../lib/schedules";
import { DatasourceDialog } from "./datasource-form";
import { InputFields } from "./input-fields";
import {
  ProjectPicker,
  projectChoice,
  type PickedProject,
} from "./project-picker";
import { useMay, useSignedIn } from "./session";
import { ErrorNotice } from "./ui";

type Source = { datasource_id: string; name: string; kind: string };
const NO_PROJECT: PickedProject = {
  project: null,
  environment: "",
  values: {},
};

function NumberSelect({
  id,
  label,
  value,
  from,
  to,
  format = String,
  onChange,
}: {
  id: string;
  label: string;
  value: number;
  from: number;
  to: number;
  format?: (n: number) => string;
  onChange: (value: number) => void;
}) {
  return (
    <select
      id={id}
      aria-label={label}
      value={value}
      onChange={(event) => onChange(Number(event.target.value))}
    >
      {Array.from({ length: to - from + 1 }, (_, i) => from + i).map((n) => (
        <option key={n} value={n}>
          {format(n)}
        </option>
      ))}
    </select>
  );
}

// When the schedule runs: a common kind picked from the list, or a cron written by hand.
function WhenFields({
  preset,
  onChange,
}: {
  preset: Preset;
  onChange: (preset: Preset) => void;
}) {
  const id = useId();
  const set = (changes: Partial<Preset>) => onChange({ ...preset, ...changes });
  const pad = (n: number) => String(n).padStart(2, "0");
  return (
    <div className="asset-field schedule-when">
      <label htmlFor={`${id}-kind`}>什么时候跑</label>
      <div className="schedule-when-row">
        <select
          id={`${id}-kind`}
          value={preset.kind}
          onChange={(event) => {
            const kind = event.target.value as PresetKind;
            // Writing it yourself starts from what was picked.
            set(
              kind === "custom" ? { kind, cron: presetCron(preset) } : { kind },
            );
          }}
        >
          {(Object.keys(PRESET_LABELS) as PresetKind[]).map((kind) => (
            <option key={kind} value={kind}>
              {PRESET_LABELS[kind]}
            </option>
          ))}
        </select>
        {preset.kind === "weekly" && (
          <NumberSelect
            id={`${id}-weekday`}
            label="星期几"
            value={preset.weekday}
            from={0}
            to={6}
            format={(n) => WEEKDAYS[n]}
            onChange={(weekday) => set({ weekday })}
          />
        )}
        {preset.kind === "monthly" && (
          <NumberSelect
            id={`${id}-day`}
            label="几号"
            value={preset.day}
            from={1}
            to={31}
            format={(n) => `${n} 日`}
            onChange={(day) => set({ day })}
          />
        )}
        {preset.kind !== "custom" && preset.kind !== "hourly" && (
          <NumberSelect
            id={`${id}-hour`}
            label="几点"
            value={preset.hour}
            from={0}
            to={23}
            format={(n) => `${pad(n)} 点`}
            onChange={(hour) => set({ hour })}
          />
        )}
        {preset.kind !== "custom" && (
          <NumberSelect
            id={`${id}-minute`}
            label="几分"
            value={preset.minute}
            from={0}
            to={59}
            format={(n) =>
              preset.kind === "hourly" ? `第 ${n} 分` : `${pad(n)} 分`
            }
            onChange={(minute) => set({ minute })}
          />
        )}
        {preset.kind === "custom" && (
          <input
            id={`${id}-cron`}
            aria-label="cron"
            className="schedule-cron"
            value={preset.cron}
            maxLength={200}
            placeholder="分 时 日 月 周，如 0 8 * * 1-5"
            onChange={(event) => set({ cron: event.target.value })}
          />
        )}
      </div>
      {preset.kind === "custom" && (
        <p className="field-hint">
          5 段：分 时 日 月 周，用空格分开；每段可以写 *、数字、a-b、*/n
          和逗号。周日写 0 或 7。
        </p>
      )}
    </div>
  );
}

function NextRuns({ cron, timezone }: { cron: string; timezone: string }) {
  const [shown, setShown] = useState<{ runs: string[]; error: string }>({
    runs: [],
    error: "",
  });
  useEffect(() => {
    const controller = new AbortController();
    const timer = setTimeout(() => {
      previewSchedule(cron, timezone, controller.signal)
        .then((value) => setShown({ runs: value.next_runs, error: "" }))
        .catch((err) => {
          if (!controller.signal.aborted)
            setShown({
              runs: [],
              // The backend's own words say what is wrong with the cron.
              error: errorMessage(err)
                .replace("提交内容未通过校验，请检查后重试。", "")
                .trim(),
            });
        });
    }, 300);
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  }, [cron, timezone]);
  if (shown.error)
    return (
      <p className="field-error" role="alert">
        {shown.error}
      </p>
    );
  if (!shown.runs.length) return null;
  return (
    <div className="schedule-preview" aria-live="polite">
      <span>接下来 3 次</span>
      <ul aria-label="接下来 3 次">
        {shown.runs.map((at) => (
          <li key={at}>{zonedTime(at, timezone)}</li>
        ))}
      </ul>
    </div>
  );
}

function ScheduleForm({
  app,
  inputs,
  uses,
  datasources,
  editing,
  onSaved,
}: {
  app: string;
  inputs: Input[] | null;
  uses: ProjectUse | null;
  datasources: Source[];
  editing: Schedule | null;
  onSaved: (schedule: Schedule) => void;
}) {
  const id = useId();
  const me = useSignedIn();
  const [title, setTitle] = useState(editing?.title ?? "");
  const [preset, setPreset] = useState<Preset>(
    editing ? readPreset(editing.cron) : DEFAULT_PRESET,
  );
  const [timezone, setTimezone] = useState(
    editing?.timezone ?? TIMEZONES[0].value,
  );
  const fields = inputs ?? [];
  const [values, setValues] = useState<FieldValues>(() => ({
    ...initialValues(fields),
    ...(editing?.input ?? {}),
  }));
  const [json, setJson] = useState(() =>
    JSON.stringify(editing?.input ?? {}, null, 2),
  );
  const [picked, setPicked] = useState<PickedProject>(NO_PROJECT);
  const [projectErrors, setProjectErrors] = useState<Record<string, string>>(
    {},
  );
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  // An edited schedule's project, as it was saved.
  useEffect(() => {
    const saved = editing?.project;
    if (!saved) return;
    const controller = new AbortController();
    getProject(saved.project_id, controller.signal)
      .then((project) => {
        if (controller.signal.aborted) return;
        setPicked({
          project,
          environment: saved.environment ?? project.environments[0]?.key ?? "",
          values: {
            ...Object.fromEntries(
              project.params.map((param) => [param.key, param.default]),
            ),
            ...(saved.params ?? {}),
          },
        });
      })
      .catch((err) => {
        if (!controller.signal.aborted) setError(errorMessage(err));
      });
    return () => controller.abort();
  }, [editing]);

  const cron = presetCron(preset);
  return (
    <form
      className="asset-form schedule-form"
      noValidate
      onSubmit={async (event) => {
        event.preventDefault();
        if (saving) return;
        const problems: Record<string, string> = {};
        let input: Record<string, unknown> = {};
        if (inputs) {
          for (const issue of validateFields(fields, values, "input"))
            problems[String(issue.loc[2])] = issue.msg;
          input = inputPayload(fields, values);
        } else {
          try {
            const parsed: unknown = JSON.parse(json || "{}");
            if (!parsed || typeof parsed !== "object" || Array.isArray(parsed))
              throw new Error();
            input = parsed as Record<string, unknown>;
          } catch {
            problems[""] = "运行输入要是一个 JSON 对象";
          }
        }
        const bound = uses ? projectChoice(uses, picked) : null;
        setErrors(problems);
        setProjectErrors(bound?.problems ?? {});
        if (!title.trim()) {
          setError("请填写名称");
          return;
        }
        if (
          Object.keys(problems).length ||
          Object.keys(bound?.problems ?? {}).length
        )
          return;
        setSaving(true);
        setError("");
        const body: ScheduleBody = {
          title: title.trim(),
          cron,
          timezone,
          input,
          project: bound?.choice ?? null,
        };
        try {
          const actor = me ? "" : savedActor();
          onSaved(
            editing
              ? await updateSchedule(editing.schedule_id, body)
              : await createSchedule(app, {
                  ...body,
                  ...(actor ? { requester: actor } : {}),
                }),
          );
        } catch (err) {
          setError(errorMessage(err));
          setErrors(fieldErrors(err, "input"));
          setSaving(false);
        }
      }}
    >
      <div className="asset-field">
        <label htmlFor={`${id}-title`}>名称</label>
        <input
          id={`${id}-title`}
          data-autofocus
          maxLength={100}
          value={title}
          placeholder="例如：每天早上的销售日报"
          onChange={(event) => setTitle(event.target.value)}
        />
      </div>
      <WhenFields preset={preset} onChange={setPreset} />
      <div className="asset-field">
        <label htmlFor={`${id}-timezone`}>时区</label>
        <select
          id={`${id}-timezone`}
          value={timezone}
          onChange={(event) => setTimezone(event.target.value)}
        >
          {TIMEZONES.some((item) => item.value === timezone) || (
            <option value={timezone}>{timezone}</option>
          )}
          {TIMEZONES.map((item) => (
            <option key={item.value} value={item.value}>
              {item.label}
            </option>
          ))}
        </select>
      </div>
      <NextRuns cron={cron} timezone={timezone} />
      {uses && (
        <ProjectPicker
          uses={uses}
          picked={picked}
          errors={projectErrors}
          disabled={saving}
          onChange={(next) => {
            setPicked(next);
            setProjectErrors({});
          }}
        />
      )}
      <fieldset className="list-editor">
        <legend>运行输入</legend>
        {inputs ? (
          fields.length ? (
            <InputFields
              fields={fields}
              values={values}
              onChange={setValues}
              errors={errors}
              disabled={saving}
              prefix={`${id}-input`}
              datasources={datasources}
            />
          ) : (
            <p className="muted">这个应用不需要输入。</p>
          )
        ) : (
          <div className="asset-field">
            <label htmlFor={`${id}-json`}>输入（JSON）</label>
            <textarea
              id={`${id}-json`}
              rows={5}
              value={json}
              aria-invalid={Boolean(errors[""])}
              onChange={(event) => setJson(event.target.value)}
            />
            <p className="field-hint">
              代码应用没有开始节点，按 POST /runs 的 input 填。
            </p>
            {errors[""] && <p className="field-error">{errors[""]}</p>}
          </div>
        )}
      </fieldset>
      <p className="field-hint">
        每次到点都用应用当时的生效版本运行；上一次还没结束时跳过这一次。
      </p>
      {error && <ErrorNotice message={error} />}
      <div className="datasource-save-row">
        <button className="button primary" disabled={saving}>
          {saving ? "正在保存…" : editing ? "保存修改" : "新建定时"}
        </button>
      </div>
    </form>
  );
}

// Runs the platform starts on a schedule (docs/api/schedules.md).
export function AppSchedules({
  app,
  inputs,
  uses,
  datasources,
}: {
  app: string;
  inputs: Input[] | null;
  uses: ProjectUse | null;
  datasources: Source[];
}) {
  const mayBuild = useMay("builder");
  const [items, setItems] = useState<Schedule[] | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [revision, setRevision] = useState(0);
  const [dialog, setDialog] = useState<"create" | Schedule | null>(null);
  const [busy, setBusy] = useState("");
  const dialogRef = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const controller = new AbortController();
    listSchedules(app, controller.signal)
      .then((value) => {
        setItems(value);
        setError("");
      })
      .catch((err) => {
        if (!controller.signal.aborted) setError(errorMessage(err));
      });
    return () => controller.abort();
  }, [app, revision]);

  async function act(schedule: Schedule, what: () => Promise<string>) {
    setBusy(schedule.schedule_id);
    setError("");
    try {
      setNotice(await what());
      setRevision((n) => n + 1);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy("");
    }
  }

  return (
    <section
      className="panel asset-section app-schedules"
      aria-labelledby="app-schedules-title"
    >
      <div className="asset-section-heading">
        <h2 id="app-schedules-title">定时运行</h2>
        {mayBuild && (
          <button
            type="button"
            className="button secondary"
            onClick={() => {
              setNotice("");
              setDialog("create");
            }}
          >
            新建定时
          </button>
        )}
      </div>
      <p className="muted">
        平台按时把这个应用跑一遍，比如每天早上出一份报表、每月 1
        日按新月份跑一次。定时发起的运行和其他运行一样出现在运行记录里，要审批的照常等审批者。
      </p>
      {notice && (
        <p className="datasource-note" role="status">
          {notice}
        </p>
      )}
      {error && (
        <ErrorNotice
          message={error}
          onRetry={() => setRevision((n) => n + 1)}
        />
      )}
      {items && !items.length && (
        <p className="muted">
          还没有定时。{mayBuild ? "" : "定时由搭建者设置，需要的话请找他们。"}
        </p>
      )}
      {items && items.length > 0 && (
        <div className="table-scroll">
          <table className="mapping-table" aria-label="定时运行">
            <thead>
              <tr>
                {["名称", "什么时候", "下一次", "最近一次", "启用", ""].map(
                  (h, i) => (
                    <th scope="col" key={i}>
                      {h}
                    </th>
                  ),
                )}
              </tr>
            </thead>
            <tbody>
              {items.map((s) => (
                <tr key={s.schedule_id}>
                  <td>
                    {s.title}
                    {s.last_error && (
                      <p className="schedule-error">{s.last_error}</p>
                    )}
                  </td>
                  <td>
                    {cronText(s.cron)}
                    <span className="run-step">{s.timezone}</span>
                  </td>
                  <td>
                    {s.next_run_at ? (
                      zonedTime(s.next_run_at, s.timezone)
                    ) : (
                      <span className="muted">已停用</span>
                    )}
                  </td>
                  <td>
                    {s.last_run ? (
                      <Link
                        className="text-button"
                        href={`/runs/${encodeURIComponent(s.last_run.run_id)}`}
                      >
                        {zonedTime(s.last_run.at, s.timezone)}
                        {s.last_run.status &&
                          ` · ${statusLabels[s.last_run.status as RunStatus] ?? s.last_run.status}`}
                      </Link>
                    ) : (
                      <span className="muted">还没跑过</span>
                    )}
                  </td>
                  <td>
                    <input
                      type="checkbox"
                      aria-label={`启用定时「${s.title}」`}
                      checked={s.enabled}
                      disabled={!mayBuild || busy === s.schedule_id}
                      onChange={(event) => {
                        const enabled = event.target.checked;
                        void act(s, async () => {
                          await updateSchedule(s.schedule_id, { enabled });
                          return enabled
                            ? `已启用定时「${s.title}」。`
                            : `已停用定时「${s.title}」，到点不再运行。`;
                        });
                      }}
                    />
                  </td>
                  <td className="schedule-actions">
                    {mayBuild && (
                      <>
                        <button
                          type="button"
                          className="text-button"
                          disabled={busy === s.schedule_id}
                          aria-label={`立即跑一次「${s.title}」`}
                          onClick={() =>
                            void act(s, async () => {
                              const run = await runSchedule(s.schedule_id);
                              return `已按「${s.title}」发起运行 ${run.run_id.slice(0, 8)}。`;
                            })
                          }
                        >
                          立即跑一次
                        </button>
                        <button
                          type="button"
                          className="text-button"
                          aria-label={`编辑定时「${s.title}」`}
                          onClick={() => {
                            setNotice("");
                            setDialog(s);
                          }}
                        >
                          编辑
                        </button>
                        <button
                          type="button"
                          className="text-button danger-text"
                          aria-label={`删除定时「${s.title}」`}
                          disabled={busy === s.schedule_id}
                          onClick={() => {
                            if (
                              !window.confirm(
                                `确定删除定时「${s.title}」吗？已经发起的运行不受影响。`,
                              )
                            )
                              return;
                            void act(s, async () => {
                              await deleteSchedule(s.schedule_id);
                              return `已删除定时「${s.title}」。`;
                            });
                          }}
                        >
                          删除
                        </button>
                      </>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {dialog && (
        <DatasourceDialog
          ref={dialogRef}
          title={
            dialog === "create" ? "新建定时" : `编辑定时「${dialog.title}」`
          }
          onClose={() => setDialog(null)}
        >
          <ScheduleForm
            app={app}
            inputs={inputs}
            uses={uses}
            datasources={datasources}
            editing={dialog === "create" ? null : dialog}
            onSaved={(saved) => {
              setNotice(
                `已保存定时「${saved.title}」${
                  saved.next_run_at
                    ? `，下一次 ${zonedTime(saved.next_run_at, saved.timezone)}`
                    : ""
                }。`,
              );
              setRevision((n) => n + 1);
              dialogRef.current?.close();
            }}
          />
        </DatasourceDialog>
      )}
    </section>
  );
}
