"use client";

import { useEffect, useState } from "react";
import type { Input } from "../lib/apps";
import { useT } from "../lib/i18n/client";
import {
  getMediaItem,
  listRunMedia,
  mediaFileUrl,
  mediaPosterUrl,
  type MediaItem,
  type RunMediaFile,
} from "../lib/media";
import { Icon } from "./icons";
import { MediaPreview, mediaMeta } from "./media-ui";

// The files the run made into the libraries its app binds (docs/api/media.md, 结果里的
// 文件). Listed while the run is still going too; nothing is shown when there are none.
export function RunMediaFiles({
  runId,
  version,
}: {
  runId: string;
  // Changes when the run moves on: the list is read again.
  version: string;
}) {
  const t = useT();
  const [files, setFiles] = useState<RunMediaFile[]>([]);
  useEffect(() => {
    const controller = new AbortController();
    listRunMedia(runId, controller.signal)
      .then((found) => !controller.signal.aborted && setFiles(found))
      // A run whose app binds no library has none; an error leaves the list as it is.
      .catch(() => undefined);
    return () => controller.abort();
  }, [runId, version]);
  if (!files.length) return null;
  return (
    <section className="panel run-media" aria-labelledby="run-media-title">
      <h2 id="run-media-title">{t("产出的文件")}</h2>
      <ul className="media-grid">
        {files.map((file) => {
          const src = `/api${file.download_path}`;
          return (
            <li className="media-card" key={file.item_id}>
              <MediaPreview kind={file.kind} src={src} name={file.name} />
              <div className="media-card-body">
                <strong className="media-name" title={file.name}>
                  {file.name}
                </strong>
                <span className="muted">{mediaMeta(t, file)}</span>
                <a className="text-button" href={src} download={file.name}>
                  <Icon name="download" size={14} />
                  {t("下载")}
                </a>
              </div>
            </li>
          );
        })}
      </ul>
    </section>
  );
}

// The items a run was started with, for its media inputs: their names, to play.
export function RunMediaInputs({
  inputs,
  values,
}: {
  inputs: Input[];
  values: Record<string, unknown>;
}) {
  const t = useT();
  const fields = inputs.filter(
    (field) =>
      field.type === "media" &&
      field.library &&
      typeof values[field.key] === "string" &&
      values[field.key],
  );
  const [items, setItems] = useState<Record<string, MediaItem | null>>({});
  const wanted = fields
    .map((field) => `${field.library}/${values[field.key]}`)
    .join(",");
  useEffect(() => {
    if (!wanted) return;
    const controller = new AbortController();
    Promise.all(
      wanted.split(",").map(async (pair) => {
        const [library, id] = pair.split("/");
        try {
          return [
            pair,
            await getMediaItem(library, id, controller.signal),
          ] as const;
        } catch {
          return [pair, null] as const;
        }
      }),
    ).then((found) => {
      if (!controller.signal.aborted) setItems(Object.fromEntries(found));
    });
    return () => controller.abort();
  }, [wanted]);
  if (!fields.length) return null;
  return (
    <>
      {fields.map((field) => {
        const item = items[`${field.library}/${values[field.key]}`];
        return (
          <div key={field.key}>
            <dt>{field.label}</dt>
            <dd className="run-media-input">
              {item ? (
                <>
                  <span>
                    {item.name}
                    <span className="muted"> · {mediaMeta(t, item)}</span>
                  </span>
                  <MediaPreview
                    kind={item.kind}
                    src={mediaFileUrl(item.library_id, item.item_id)}
                    name={item.name}
                    poster={mediaPosterUrl(item)}
                  />
                </>
              ) : item === null ? (
                <span className="muted">{t("素材已经不在了")}</span>
              ) : (
                <span className="muted">{t("正在加载…")}</span>
              )}
            </dd>
          </div>
        );
      })}
    </>
  );
}
