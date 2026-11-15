"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import type { FlowConfig } from "../lib/apps";
import { useT } from "../lib/i18n/client";
import {
  cachedMediaLibraries,
  MAX_BINDINGS,
  mediaAccessLabels,
  type MediaAccess,
  type MediaBinding,
  type MediaLibrary,
} from "../lib/media";
import { errorMessage } from "../lib/runs";
import { SelectField } from "./flow-fields";

// All media libraries on the platform, or why they could not be read.
function useLibraries(): { libraries: MediaLibrary[] | null; error: string } {
  const [state, setState] = useState<{
    libraries: MediaLibrary[] | null;
    error: string;
  }>({ libraries: null, error: "" });
  useEffect(() => {
    let live = true;
    cachedMediaLibraries()
      .then((libraries) => live && setState({ libraries, error: "" }))
      .catch(
        (err) =>
          live && setState({ libraries: null, error: errorMessage(err) }),
      );
    return () => {
      live = false;
    };
  }, []);
  return state;
}

// The libraries the configuration binds, by name, for the start node's media inputs.
export function useBoundLibraries(
  config: FlowConfig,
): { value: string; label: string }[] {
  const { libraries } = useLibraries();
  return (config.media?.libraries ?? []).map((binding) => ({
    value: binding.library_id,
    label:
      libraries?.find((library) => library.library_id === binding.library_id)
        ?.name ?? binding.library_id,
  }));
}

function withoutMedia(config: FlowConfig): FlowConfig {
  const rest = { ...config };
  delete rest.media;
  return rest;
}

// Which media libraries the app uses (docs/api/media.md, 绑定): what its start inputs
// pick from and what its keys may list and change through the app API (/v1).
export function FlowMedia({
  config,
  setConfig,
  issues,
  disabled,
}: {
  config: FlowConfig;
  setConfig: (config: FlowConfig) => void;
  issues: Record<string, string>;
  disabled: boolean;
}) {
  const t = useT();
  const { libraries, error } = useLibraries();
  const bound = config.media?.libraries ?? [];
  const set = (next: MediaBinding[]) =>
    setConfig(
      next.length
        ? { ...config, media: { libraries: next } }
        : withoutMedia(config),
    );
  const unbound = (libraries ?? []).filter(
    (library) => !bound.some((b) => b.library_id === library.library_id),
  );
  const general = Object.entries(issues)
    .filter(([key]) => !/^libraries\.\d+/.test(key))
    .map(([, msg]) => msg);
  return (
    <section
      className="panel asset-section flow-media"
      aria-labelledby="flow-media-title"
    >
      <h2 id="flow-media-title">{t("媒体库")}</h2>
      <p className="muted">
        {t(
          "绑定后，开始步骤的输入可以是库里的素材；应用的 API 密钥也能经 /v1 列出、下载库里的素材，可以上传和修改时还能往库里传。",
        )}{" "}
        <Link className="text-button" href="/media">
          {t("管理媒体库")}
        </Link>
      </p>
      {error && <p className="field-error">{error}</p>}
      {general.map((msg) => (
        <p className="field-error" key={msg}>
          {msg}
        </p>
      ))}
      <fieldset disabled={disabled} className="flow-media-fields">
        <legend className="sr-only">{t("绑定的媒体库")}</legend>
        {bound.length === 0 && (
          <p className="muted">{t("还没有绑定媒体库。")}</p>
        )}
        <ul className="flow-media-list">
          {bound.map((binding, i) => {
            const library = libraries?.find(
              (item) => item.library_id === binding.library_id,
            );
            const problems = Object.entries(issues)
              .filter(
                ([key]) =>
                  key === `libraries.${i}` || key.startsWith(`libraries.${i}.`),
              )
              .map(([, msg]) => msg);
            return (
              <li key={binding.library_id} className="flow-media-row">
                <strong>
                  {library?.name ??
                    t("没有这个媒体库（{id}）", { id: binding.library_id })}
                </strong>
                <SelectField
                  label={t("权限")}
                  value={binding.access}
                  choices={(["read", "write"] as MediaAccess[]).map(
                    (value) => ({
                      value,
                      label: t(mediaAccessLabels[value]),
                    }),
                  )}
                  issues={problems}
                  onChange={(access) =>
                    set(
                      bound.map((b, j) =>
                        j === i ? { ...b, access: access as MediaAccess } : b,
                      ),
                    )
                  }
                />
                <button
                  type="button"
                  className="text-button danger-text"
                  onClick={() => set(bound.filter((_, j) => j !== i))}
                >
                  {t("移除")}
                </button>
              </li>
            );
          })}
        </ul>
        {bound.length < MAX_BINDINGS && unbound.length > 0 && (
          <SelectField
            label={t("再绑定一个")}
            value=""
            choices={[
              { value: "", label: t("选一个媒体库") },
              ...unbound.map((library) => ({
                value: library.library_id,
                label: library.name,
              })),
            ]}
            onChange={(library_id) =>
              library_id && set([...bound, { library_id, access: "read" }])
            }
          />
        )}
        {libraries?.length === 0 && (
          <p className="muted">
            {t("平台上还没有媒体库，先到媒体库页面新建一个。")}
          </p>
        )}
      </fieldset>
    </section>
  );
}
