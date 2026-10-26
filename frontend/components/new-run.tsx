"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useRef, useState, type FormEvent } from "react";
import { errorMessage, readReceipt, requestJson } from "../lib/runs";
import { Icon } from "./icons";
import { ErrorNotice } from "./ui";

export function NewRun() {
  const router = useRouter();
  const [text, setText] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");
  const busy = useRef(false);
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (busy.current || !text.trim()) return;
    busy.current = true;
    setSubmitting(true);
    setError("");
    try {
      const receipt = readReceipt(
        await requestJson("/runs", {
          method: "POST",
          body: JSON.stringify({ app: "hello", input: { text: text.trim() } }),
        }),
      );
      router.push(`/runs/${encodeURIComponent(receipt.run_id)}`);
    } catch (err) {
      setError(errorMessage(err));
      busy.current = false;
      setSubmitting(false);
    }
  }
  return (
    <>
      <Link className="back-link" href="/">
        <Icon name="back" size={16} />
        返回运行记录
      </Link>
      <div className="page-heading">
        <div>
          <div className="eyebrow">START WITH AN IDEA</div>
          <h1>新建运行</h1>
          <p>告诉应用你想完成什么，接下来的过程在这里展开。</p>
        </div>
      </div>
      <div className="new-run-grid">
        <form
          className="panel new-run-form"
          onSubmit={submit}
          aria-label="新建运行"
        >
          <div className="form-section">
            <span className="step-label">
              01<span>选择应用</span>
            </span>
            <label htmlFor="app">应用</label>
            <select
              id="app"
              name="app"
              defaultValue="hello"
              disabled={submitting}
            >
              <option value="hello">hello · 示例应用</option>
            </select>
            <p className="field-hint">
              接收需求、请求人工确认，并返回运行结果。
            </p>
          </div>
          <div className="form-section">
            <span className="step-label">
              02<span>描述需求</span>
            </span>
            <label htmlFor="request">你想完成什么？</label>
            <textarea
              id="request"
              name="text"
              required
              rows={7}
              placeholder="例如：请整理一份客户数据清洗方案，并在执行前让我确认。"
              value={text}
              onChange={(e) => setText(e.target.value)}
              disabled={submitting}
              aria-describedby="request-hint"
            />
            <p className="field-hint" id="request-hint">
              尽量说明目标与约束，帮助应用理解你的需求。
            </p>
          </div>
          {error && <ErrorNotice message={error} />}
          <div className="form-actions">
            <span>运行过程与结果将自动记录</span>
            <button
              className="button primary"
              type="submit"
              disabled={submitting || !text.trim()}
            >
              {submitting ? (
                <>
                  <span className="spinner" />
                  正在创建…
                </>
              ) : (
                <>
                  开始运行
                  <Icon name="arrow" size={17} />
                </>
              )}
            </button>
          </div>
        </form>
        <aside className="guide-card">
          <span className="guide-icon">
            <Icon name="spark" size={24} />
          </span>
          <h2>让需求有迹可循</h2>
          <p>每一次运行，都是一段完整的协作。</p>
          <ol className="guide-steps">
            <li>
              <strong>描述目标</strong>
              <span>用自然语言提出你的需求</span>
            </li>
            <li>
              <strong>查看过程</strong>
              <span>跟随应用消息，了解执行进展</span>
            </li>
            <li>
              <strong>确认方向</strong>
              <span>需要时确认继续，或驳回并给出意见</span>
            </li>
            <li>
              <strong>获得结果</strong>
              <span>随时回来查看本次运行</span>
            </li>
          </ol>
        </aside>
      </div>
    </>
  );
}
