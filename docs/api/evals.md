# 评测集、运行反馈和开发者视图

后端和前端按这份约定并行开发。字段名 snake_case，时间是 ISO 8601 字符串。错误格式同 [datasources.md](datasources.md)：`{"detail": "中文说明"}`。

落在哪一层：**平台能力**。每个编排应用都可以有评测集，不用写代码；运行反馈和开发者视图对所有应用都有。代码应用 `etl` 的评测集仍是仓库里的 `evals/etl/cases.yaml`（`make eval` 也用它），页面上只读。

## 评测集

每个应用最多一个评测集。评测集里至少有一条用例时，这个应用就有发布门槛：草稿评测的总分达到门槛才能发布（`apps-and-skills.md` 里的 `has_eval_set`、`draft.eval` 和发布接口照旧）。

```jsonc
// EvalSet
{
  "app": "after-sales",
  "source": "platform" | "file" | null,   // platform：在页面上编辑；file：仓库里的文件（只读）；null：还没有评测集
  "editable": true,                     // 只有编排应用和智能体应用的 platform 评测集能改（没有评测集的也是 true）
  "threshold": 0.8,                     // 0–1，草稿总分达到它才能发布；默认 AEP_EVAL_PUBLISH_THRESHOLD
  "judge_model": "company" | null,      // 「模型判断」检查用的模型别名；null 用平台默认对话模型
  "cases": [EvalCase],
  "updated_by": "王搭建" | null,
  "updated_at": "…" | null
}

// EvalCase（编排应用和智能体应用）
{
  "id": "refund-to-after-sales",        // 1–60 个字符：小写字母、数字、- 和 _；评测集里唯一
  "title": "退货问题交给售后" | null,      // 最多 100 字
  "input": {"question": "我要退货"},      // 这次运行的输入，和新建运行的 input 一样
  "answers": {                          // 运行停在人工节点时怎么回答，按节点 id；没写的审批节点选第一个选项
    "review": {"decision": "approve", "comment": null},
    "ask": {"values": {"order": "SO-1"}}  // 表单节点必须写，否则这条用例出错
  },
  "expect": [Check],                    // 0–20 项
  "history": [{"message": "出差住宿标准是多少？", "reply": "按城市分三档……"}],
                                        // 可选，智能体应用第 1 批新增：这条用例之前的几轮对话，旧的在前，0–50 轮（同 history_turns 的上限）；不写为 []
  "restored": {"‹文本#3f2a9c01d4›": "张三"},
                                        // 可选，智能体应用第 1 批新增：history 里的标记对应的原值，只在从按「脱敏」外发的会话轮次存下来时有；不写为 {}
  "from_run": "run_…" | null            // 从哪次运行存下来的
}

// Check：每一项是一个得分点
{"kind": "equals",   "path": "reply", "value": "售后"}                 // 结果里这个字段等于 value
{"kind": "contains", "path": "reply", "values": ["退货", "7 天|七天"]}   // 每个词都要出现（不分大小写；a|b 表示其一即可）
{"kind": "excludes", "path": "reply", "values": ["保证退款"]}            // 这些词都不能出现
{"kind": "judge",    "path": "reply", "criteria": "回答礼貌，并告诉客户去售后", "reference": "……" | null}
                                                                     // 让模型判断是否满足 criteria；reference 是参考答案
```

- `path` 是运行结果（`GET /runs/{id}` 的 `result`）里的字段，用 `.` 分层，比如 `reply` 或 `summary.total`；不写就是整个结果。非字符串的值按 JSON 文本比较 `contains` / `excludes`。
- 每条用例另有一个总是打分的检查「运行完成」：运行状态是 done。运行没完成时，其余检查都算没通过。
- 用例得分是通过的检查占比；评测集总分是各用例得分的平均。
- 「模型判断」经模型网关调用，和节点一样受平台默认的数据外发策略约束；模型不让用、调用失败或回答看不懂时，这一项算没通过，说明里写原因。
- `history`（多轮对话的用例）：只有能对话的应用（智能体应用、开了对话的编排应用）能写，别的应用写了报“这个应用没有开对话，用例不能带历史”。每轮 `message` 1–8000 字符、`reply` 0–20000 字符，所有轮的 `message` 和 `reply` 合计最多 20000 字（同会话历史的字数上限，conversations.md“历史怎么带”），所以从会话存下来的历史总能原样存进用例。评测时这条用例的运行就当作会话的下一轮：`history` 原样当作历史带给模型（不再按轮数和字数截，也不建会话），`{{conversation.turn}}` 是历史轮数加 1。历史和 `input` 一样当作搭建者写的输入。`restored` 在评测时放进这次运行的还原表，和会话里前几轮的还原表一样：不发给模型，模型在回答里原样写回表里的标记时换回原值（按标记本身对，不重新算标记），所以存下来的检查能照原样通过。`restored` 只能有 `history` 里出现过的标记，最多 200 项，否则报“还原表里有历史里没有的标记”。
- 评测时运行停在人工节点：审批节点用 `answers` 里的 `decision`（默认第一个选项）和 `comment`；表单节点用 `answers` 里的 `values`。一条用例最多回答 10 次，再停就算出错。

### 接口

| 方法 | 路径 | 请求体 | 返回 | 角色 |
| --- | --- | --- | --- | --- |
| GET | `/apps/{app}/eval-set` | | `EvalSet`；没有评测集时 `source` 为 null、`cases` 为 `[]` | 使用者 |
| PUT | `/apps/{app}/eval-set` | `{"threshold"?, "judge_model"?, "cases": [EvalCase]}` | `EvalSet` | 搭建者 |
| POST | `/apps/{app}/eval-set/cases/from-run` | `{"run_id", "rating"?: "good" \| "bad"}` | 201 `EvalCase`（已加进评测集） | 搭建者 |

- 不存在的应用 404「没有应用 xxx」；代码应用或 file 评测集改不了，409「这个应用的评测集在仓库里维护，页面上不能改」。
- PUT 整个替换评测集；`cases` 为空就是去掉发布门槛。校验不过 422，`loc` 指到具体用例和检查，比如 `["body", "cases", 2, "expect", 0, "values"]`。保存评测集不清空草稿已有的评测结果，但结果里记着当时的用例数，页面提示“评测集改过了，建议重新评测”。
- 从运行存用例：运行必须属于这个应用，并且已经结束（done 或 error），否则 409。`input` 抄运行的输入，`answers` 抄运行里人回答过的每个人工节点（同一节点回答多次取最后一次），`from_run` 是这次运行。运行是会话里的一轮时，`history` 抄这一轮实际带给模型的历史（conversations.md“历史怎么带”处理过的那份；按「脱敏」外发时是带标记的样子，不还原），所以用例在评测时不会把模型当时没看到的原值发出去；`restored` 抄这些标记在那一轮的还原表（只抄历史里出现的标记），存在评测集里，会话过期清掉后用例照样能跑。参考答案和生成的检查本来就是给人看的原值，`restored` 和它们一样随评测集保存和导出；会话已经过期清掉时 409「这次运行属于已过期的会话，内容已清除」。设计页调试对话里每条回答的「存为评测用例」就是调这个接口。`rating` 为 `good`（默认）且运行完成时，按运行结果生成检查：结果里每个顶层字段，短文本（50 字以内）和数字、布尔值用 `equals`，长文本用 `judge`（`criteria` 为“意思和参考答案一致”，`reference` 为这次的值），其他用 `equals`；最多 20 项。执行过程的记录不生成检查：结束节点的输出直接取自智能体节点的 `steps` 或 `hits`（`{{x.steps}}`、`{{x.hits}}`，智能体应用结果里的 `steps`、`hits` 就是这样来的）时跳过这个字段，因为里面有被调运行的 `run_id`、检索到的片段这类每次运行都会变的内容，照抄成 `equals` 会让没变的应用也评测不过；要检查调没调某个工具，搭建者自己写 `contains`。`rating` 为 `bad` 或运行出错时 `expect` 为空，由搭建者补上应该是什么。用例 `id` 取 `run-` 加运行 id 的后 8 位，重名时加 `-2`、`-3`。评测集已满 50 条时 409「一个评测集最多 50 条用例」。
- 用另一个应用（`from.app`）复制出来的新应用，同时复制它的评测集。

### 草稿评测结果

`draft.eval` 的格式不变（`apps-and-skills.md`），每条用例多两个字段：

```jsonc
{"id": "refund-to-after-sales", "status": "done", "score": 0.75, "run_id": "…", "error": null,
 "title": "退货问题交给售后" | null,
 "checks": [{"kind": "completed", "name": "运行完成", "passed": true, "detail": ""},
            {"kind": "contains", "name": "reply 包含 退货、7 天|七天", "passed": false, "detail": "没有出现 7 天|七天"}]}
```

`eval` 本身多一个 `case_count`：开始评测时评测集有几条用例。`etl` 的检查 `kind` 是 `plan`、`code`、`result`（它原来的层），`passed` 为 null 表示跳过。

## 运行反馈

运行结束后（done 或 error），看运行的人可以点“对”或“不对”，可以写一句说明。一人一次运行一条，再点就覆盖；没开登录时所有人算同一个人。

```jsonc
// RunFeedback：GET /runs/{id} 新增字段 "feedback"
{
  "good": 3,
  "bad": 1,
  "mine": {"rating": "good" | "bad", "note": "金额算对了" | null, "at": "…"} | null,
  "cases": [{"app": "after-sales", "case_id": "run-1a2b3c4d"}]   // 从这次运行存下的用例
}
```

| 方法 | 路径 | 请求体 | 返回 |
| --- | --- | --- | --- |
| PUT | `/runs/{run_id}/feedback` | `{"rating": "good" \| "bad", "note"?: string}`（`note` 最多 500 字） | `RunFeedback` |
| DELETE | `/runs/{run_id}/feedback` | | `RunFeedback`（去掉我的反馈） |

- 运行还没结束 409「运行结束后才能评价」；不存在 404。
- 反馈不改运行本身；操作记录里记“评价运行”。

## 开发者视图

给搭建者调试用，运行页上的一个开关。

### 每一步的耗时和用量

`GET /runs/{id}/flow` 的每个 `steps[]` 新增：

```jsonc
{
  "finished_at": "…" | null,           // 下一步开始的时间；还没结束为 null
  "duration_ms": 1830 | null,
  "usage": {"calls": 2, "prompt_tokens": 1520, "completion_tokens": 340, "cost_usd": 0.0041 | null} | null
                                        // 这一步里经网关的模型调用（模型调用记录里同一运行、同一节点、落在这一步时间里的）；没有调用为 null
}
```

Langfuse 链接照旧用运行详情的 `trace_id`。

### 从某一步重跑

| 方法 | 路径 | 请求体 | 返回 | 角色 |
| --- | --- | --- | --- | --- |
| POST | `/runs/{run_id}/rerun-from` | `{"node": "reply", "config": "draft" \| "same"}` | 201 `{"run_id", "status"}`（新运行） | 搭建者 |

- 新运行抄原运行在这一步之前的全部状态（输入、前面各步的产出、对话），从 `node` 开始往下跑。同一节点跑过多次时，从最后一次开始。
- `config` 为 `draft` 时用应用当前的草稿（没有草稿 409「这个应用没有草稿」），`same` 时用原运行的版本或配置。
- 只有编排应用可以，代码应用 409「代码应用不能从某一步重跑」；原运行没跑过这一步，或者选的配置里没有这个节点，422 并说明。
- 新运行和别的运行一样出现在运行记录里；运行详情新增 `start_at: {"node", "title", "run_id"} | null`：从哪次运行的哪一步开始的。草稿配置的运行 `app_version` 为 null，和试运行一样。

### 两次运行对比

前端页面 `/runs/compare?a={run_id}&b={run_id}`，只用已有接口（`GET /runs/{id}` 和 `/flow`）：按节点并排显示两次运行每一步的状态、耗时、用量和产出，产出不同的地方标出来。
