"use client";

import { useId } from "react";
import {
  REFERENCE_NAME_PATTERN,
  type SkillContent,
  type SkillInput,
} from "../lib/skills";

export type SkillEdit = {
  description: string;
  body: string;
  references: { id: string; name: string; content: string }[];
};
export function editSkill(
  content: Pick<SkillContent, "description" | "body" | "references">,
): SkillEdit {
  return {
    description: content.description,
    body: content.body,
    references: Object.entries(content.references).map(([name, content]) => ({
      id: name,
      name,
      content,
    })),
  };
}
export function skillInput(editor: SkillEdit): SkillInput | string {
  if (!editor.description.trim() || editor.description.length > 500)
    return "说明不能为空，且最多 500 字符。";
  if (editor.body.length > 20_000) return "正文最多 20000 字符。";
  if (editor.references.length > 10) return "参考资料最多 10 个。";
  const references: Record<string, string> = {};
  for (const ref of editor.references) {
    if (!new RegExp(REFERENCE_NAME_PATTERN).test(ref.name))
      return "参考资料文件名须为 1~64 位字母、数字、下划线、点或短横线，并以 .md 结尾。";
    if (Object.hasOwn(references, ref.name))
      return `参考资料文件名重复：${ref.name}`;
    if (ref.content.length > 20_000)
      return `参考资料 ${ref.name} 最多 20000 字符。`;
    references[ref.name] = ref.content;
  }
  return {
    description: editor.description.trim(),
    body: editor.body,
    references,
  };
}

export function SkillContentView({ content }: { content: SkillContent }) {
  return (
    <div className="skill-content">
      <p>{content.description}</p>
      <h3>SKILL.md</h3>
      <pre className="markdown-preview">{content.body || "（正文为空）"}</pre>
      <h3>参考资料</h3>
      {Object.entries(content.references).length ? (
        Object.entries(content.references).map(([name, body]) => (
          <details key={name} open>
            <summary>{name}</summary>
            <pre className="markdown-preview">{body || "（内容为空）"}</pre>
          </details>
        ))
      ) : (
        <p className="muted">无参考资料。</p>
      )}
    </div>
  );
}

export function SkillEditor({
  editor,
  onChange,
  disabled,
}: {
  editor: SkillEdit;
  onChange: (value: SkillEdit) => void;
  disabled: boolean;
}) {
  const prefix = useId();
  function reference(
    id: string,
    patch: Partial<SkillEdit["references"][number]>,
  ) {
    onChange({
      ...editor,
      references: editor.references.map((ref) =>
        ref.id === id ? { ...ref, ...patch } : ref,
      ),
    });
  }
  return (
    <fieldset className="skill-editor" disabled={disabled}>
      <legend className="sr-only">编辑 Skill 草稿</legend>
      <div className="asset-field">
        <label htmlFor={`${prefix}-description`}>说明</label>
        <textarea
          id={`${prefix}-description`}
          required
          maxLength={500}
          rows={2}
          value={editor.description}
          onChange={(event) =>
            onChange({ ...editor, description: event.target.value })
          }
        />
      </div>
      <div className="markdown-editor">
        <div className="asset-field">
          <label htmlFor={`${prefix}-body`}>正文（Markdown）</label>
          <textarea
            id={`${prefix}-body`}
            maxLength={20_000}
            rows={12}
            value={editor.body}
            onChange={(event) =>
              onChange({ ...editor, body: event.target.value })
            }
          />
        </div>
        <div>
          <h3>正文预览（Markdown 原文）</h3>
          <pre className="markdown-preview">
            {editor.body || "输入正文后会显示在这里。"}
          </pre>
        </div>
      </div>
      <div className="asset-section-heading">
        <h3>参考资料（{editor.references.length}/10）</h3>
        <button
          type="button"
          className="button secondary"
          disabled={disabled || editor.references.length >= 10}
          onClick={() =>
            onChange({
              ...editor,
              references: [
                ...editor.references,
                { id: crypto.randomUUID(), name: "", content: "" },
              ],
            })
          }
        >
          添加参考资料
        </button>
      </div>
      {editor.references.map((ref, index) => (
        <div
          key={ref.id}
          className="reference-editor"
          role="group"
          aria-label={`参考资料 ${index + 1}`}
        >
          <div className="reference-heading">
            <label className="asset-field">
              文件名
              <input
                required
                pattern={REFERENCE_NAME_PATTERN}
                value={ref.name}
                placeholder="glossary.md"
                onChange={(event) =>
                  reference(ref.id, { name: event.target.value })
                }
              />
            </label>
            <button
              type="button"
              className="text-button"
              onClick={() =>
                onChange({
                  ...editor,
                  references: editor.references.filter(
                    (item) => item.id !== ref.id,
                  ),
                })
              }
            >
              删除参考资料 {index + 1}
            </button>
          </div>
          <div className="markdown-editor">
            <div className="asset-field">
              <label htmlFor={`${prefix}-reference-${ref.id}`}>
                参考资料正文
              </label>
              <textarea
                id={`${prefix}-reference-${ref.id}`}
                maxLength={20_000}
                rows={6}
                value={ref.content}
                onChange={(event) =>
                  reference(ref.id, { content: event.target.value })
                }
              />
            </div>
            <div>
              <h3>参考资料预览</h3>
              <pre className="markdown-preview">
                {ref.content || "（内容为空）"}
              </pre>
            </div>
          </div>
        </div>
      ))}
      <small className="muted">
        正文和每份参考资料最多 20000 字符；只支持 Markdown 文字。
      </small>
    </fieldset>
  );
}
