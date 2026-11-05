"use client";

import { useState } from "react";
import { parse, stringify } from "yaml";
import type { AppConfig } from "../lib/apps";
import { useT } from "../lib/i18n/client";
import { isRecord } from "../lib/runs";

// The configuration as YAML, the `config` part of the app's export file
// (docs/api/agent-apps.md, "新建、模板、导出导入"). Saving checks it as the form does.
export function ConfigCode({
  config,
  onChange,
  onProblem,
  disabled,
}: {
  config: AppConfig;
  onChange: (config: AppConfig) => void;
  // A text that does not read as a configuration; null once it does again.
  onProblem: (problem: string | null) => void;
  disabled?: boolean;
}) {
  const t = useT();
  const [text, setText] = useState(() => stringify(config, { lineWidth: 0 }));
  const [problem, setProblem] = useState("");
  function change(next: string) {
    setText(next);
    let value: unknown;
    try {
      value = parse(next);
    } catch (err) {
      const message = t("YAML 写得不对：{detail}", {
        detail: err instanceof Error ? err.message.split("\n")[0] : "",
      });
      setProblem(message);
      onProblem(message);
      return;
    }
    if (!isRecord(value)) {
      const message = t("配置要是一个对象（键: 值）。");
      setProblem(message);
      onProblem(message);
      return;
    }
    setProblem("");
    onProblem(null);
    onChange(value as AppConfig);
  }
  return (
    <div className="config-code">
      <label className="sr-only" htmlFor="config-code">
        {t("配置（YAML）")}
      </label>
      <textarea
        id="config-code"
        className="monospace"
        spellCheck={false}
        rows={28}
        value={text}
        disabled={disabled}
        aria-invalid={Boolean(problem)}
        aria-describedby="config-code-help"
        onChange={(event) => change(event.target.value)}
      />
      <p id="config-code-help" className={problem ? "field-error" : "muted"}>
        {problem ||
          t(
            "和导出文件里的 config 部分同一格式。改完点「保存草稿」，检查和表单一样。",
          )}
      </p>
    </div>
  );
}
