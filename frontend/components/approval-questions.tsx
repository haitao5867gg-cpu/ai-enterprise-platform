"use client";

import type { ReactNode } from "react";
import type { FieldValues } from "../lib/input-fields";
import type { Question } from "../lib/runs";
import { InputFields } from "./input-fields";

type Choice = { value: unknown; label: string };

function choicesOf(question: Question): Choice[] | null {
  if (question.type === "select") return question.choices ?? [];
  if (question.type === "boolean")
    return [
      { value: true, label: "是" },
      { value: false, label: "否" },
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
}: {
  questions: Question[];
  values: FieldValues;
  onChange: (values: FieldValues) => void;
  errors: Record<string, string>;
  disabled: boolean;
  aside?: (question: Question) => ReactNode;
}) {
  return (
    <div className="approval-questions">
      {questions.map((question, index) => {
        const id = `question-${question.key}`;
        const choices = choicesOf(question);
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
            {question.why && (
              <p className="question-why">
                <span>为什么问：</span>
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
                      <span className="suggested-tag">建议</span>
                    )}
                  </label>
                ))}
              </div>
            ) : (
              <>
                <InputFields
                  fields={[{ ...question, label: "你的答案", required: false }]}
                  values={values}
                  onChange={onChange}
                  disabled={disabled}
                  prefix="question"
                />
                {suggested !== null && (
                  <p className="question-why">
                    <span className="suggested-tag">建议</span>
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
