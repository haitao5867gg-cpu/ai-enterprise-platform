"use client";

import { useT, type Translate } from "../lib/i18n/client";
import type { ReactNode } from "react";
import type { FieldValues } from "../lib/input-fields";
import type { Question } from "../lib/runs";
import { InputFields } from "./input-fields";

type Choice = { value: unknown; label: string };

function choicesOf(question: Question, t: Translate): Choice[] | null {
  if (question.type === "select") return question.choices ?? [];
  if (question.type === "boolean")
    return [
      { value: true, label: t("是") },
      { value: false, label: t("否") },
    ];
  return null;
}

// The questions an approval step asks: a person picks every answer; the step's
// suggestion (what it does now) is marked, with why it is asked. `aside` adds what a page
// shows under a question (what it affects, a remark on it).
export function ApprovalQuestions({
  questions,
  values,
  onChange,
  errors,
  disabled,
  aside,
  optionLabel,
}: {
  questions: Question[];
  values: FieldValues;
  onChange: (values: FieldValues) => void;
  errors: Record<string, string>;
  disabled: boolean;
  aside?: (question: Question) => ReactNode;
  // An option's name, for a question answered only with some options.
  optionLabel?: (option: string) => string;
}) {
  const t = useT();
  return (
    <div className="approval-questions">
      {questions.map((question, index) => {
        const id = `question-${question.key}`;
        const choices = choicesOf(question, t);
        const error = errors[question.key];
        const suggested =
          question.suggested === undefined || question.suggested === null
            ? null
            : question.suggested;
        return (
          <fieldset
            key={question.key}
            className="approval-question"
            aria-invalid={Boolean(error)}
            aria-describedby={error ? `${id}-error` : undefined}
            disabled={disabled}
          >
            <legend>
              {index + 1}. {question.label}
            </legend>
            {question.only_for?.length ? (
              <p className="question-only-for">
                {t("只在选{options}时要答，选别的不用答", {
                  options: question.only_for
                    .map((option) =>
                      t("「{option}」", {
                        option: optionLabel?.(option) ?? option,
                      }),
                    )
                    .join(t("或")),
                })}
              </p>
            ) : null}
            {question.why && (
              <p className="question-why">
                <span>{t("为什么问：")}</span>
                {question.why}
              </p>
            )}
            {choices ? (
              <div className="question-choices">
                {choices.map((choice) => (
                  <label key={String(choice.value)} className="question-choice">
                    <input
                      type="radio"
                      name={id}
                      checked={values[question.key] === choice.value}
                      onChange={() =>
                        onChange({ ...values, [question.key]: choice.value })
                      }
                    />
                    <span>{choice.label}</span>
                    {suggested === choice.value && (
                      <span className="suggested-tag">{t("建议")}</span>
                    )}
                  </label>
                ))}
              </div>
            ) : (
              <>
                <InputFields
                  fields={[
                    { ...question, label: t("你的答案"), required: false },
                  ]}
                  values={values}
                  onChange={onChange}
                  disabled={disabled}
                  prefix="question"
                />
                {suggested !== null && (
                  <p className="question-why">
                    <span className="suggested-tag">{t("建议")}</span>
                    {String(suggested)}
                  </p>
                )}
              </>
            )}
            {aside?.(question)}
            {error && (
              <p className="field-error" id={`${id}-error`} role="alert">
                {error}
              </p>
            )}
          </fieldset>
        );
      })}
    </div>
  );
}
