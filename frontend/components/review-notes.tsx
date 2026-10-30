"use client";

import {
  createContext,
  useContext,
  useId,
  useState,
  type ReactNode,
} from "react";

// A remark a person hangs on one item of what a human step shows ("这里不对"): the item is
// written 类别:标识 (docs/api/flow-apps.md, "带扩展设置的人工节点"); the label is how the
// page names it, the platform names it again when it records the note.
export type Note = { item: string; label: string; text: string };

export const MAX_NOTES = 50;
export const MAX_NOTE_LENGTH = 500;

type Notes = {
  notes: Note[];
  disabled: boolean;
  put: (note: Note) => void;
  remove: (item: string) => void;
};

const NotesContext = createContext<Notes | null>(null);

// Where notes can be hung: inside it each item offers 这里不对; outside (a result page,
// a step that takes no notes) nothing is offered.
export function NotesScope({
  notes,
  onChange,
  disabled,
  children,
}: {
  notes: Note[];
  onChange: (notes: Note[]) => void;
  disabled: boolean;
  children: ReactNode;
}) {
  return (
    <NotesContext.Provider
      value={{
        notes,
        disabled,
        put: (note) =>
          onChange(
            notes.some((item) => item.item === note.item)
              ? notes.map((item) => (item.item === note.item ? note : item))
              : [...notes, note],
          ),
        remove: (item) => onChange(notes.filter((note) => note.item !== item)),
      }}
    >
      {children}
    </NotesContext.Provider>
  );
}

export function NoteButton({ item, label }: { item: string; label: string }) {
  const scope = useContext(NotesContext);
  const id = useId();
  const [draft, setDraft] = useState<string | null>(null);
  if (!scope) return null;
  const note = scope.notes.find((entry) => entry.item === item);
  const full = !note && scope.notes.length >= MAX_NOTES;
  if (draft !== null)
    return (
      <span className="note-editor">
        <label htmlFor={id} className="sr-only">
          对「{label}」的意见
        </label>
        <textarea
          id={id}
          rows={2}
          maxLength={MAX_NOTE_LENGTH}
          placeholder={`「${label}」哪里不对，应该怎样…`}
          value={draft}
          autoFocus
          disabled={scope.disabled}
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Escape") setDraft(null);
          }}
        />
        <span className="note-editor-actions">
          <button
            type="button"
            className="button secondary small"
            disabled={scope.disabled || !draft.trim()}
            onClick={() => {
              scope.put({ item, label, text: draft.trim() });
              setDraft(null);
            }}
          >
            挂上意见
          </button>
          <button
            type="button"
            className="text-button"
            onClick={() => setDraft(null)}
          >
            取消
          </button>
        </span>
      </span>
    );
  if (note)
    return (
      <span className="note-attached">
        <span className="note-text">
          <span className="note-tag">意见</span>
          {note.text}
        </span>
        <button
          type="button"
          className="text-button"
          aria-label={`修改对「${label}」的意见`}
          disabled={scope.disabled}
          onClick={() => setDraft(note.text)}
        >
          修改
        </button>
        <button
          type="button"
          className="text-button"
          aria-label={`删除对「${label}」的意见`}
          disabled={scope.disabled}
          onClick={() => scope.remove(item)}
        >
          删除
        </button>
      </span>
    );
  return (
    <button
      type="button"
      className="note-button"
      aria-label={`这里不对：${label}`}
      title={full ? `最多挂 ${MAX_NOTES} 条意见` : undefined}
      disabled={scope.disabled || full}
      onClick={() => setDraft("")}
    >
      这里不对
    </button>
  );
}

// The notes hung so far, beside the decision.
export function NoteList({
  notes,
  onRemove,
  disabled,
}: {
  notes: Note[];
  onRemove: (item: string) => void;
  disabled: boolean;
}) {
  if (!notes.length) return null;
  return (
    <div className="note-list" aria-label="挂的意见">
      <p>挂了 {notes.length} 条意见</p>
      <ul>
        {notes.map((note) => (
          <li key={note.item}>
            <strong>{note.label}</strong>
            <span>{note.text}</span>
            <button
              type="button"
              className="text-button"
              aria-label={`删除对「${note.label}」的意见`}
              disabled={disabled}
              onClick={() => onRemove(note.item)}
            >
              删除
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}
