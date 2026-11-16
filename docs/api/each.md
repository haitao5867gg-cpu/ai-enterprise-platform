# 逐项处理

编排应用用「逐项处理」节点对一个列表的每一项做同一步（调一次模型、调一次工具、跑一段代码……），收齐每一项的结果再往下走。比如教练做视频：模型按配音排出 20 个镜头，「逐项处理」对每个镜头调一次看图模型，从候选素材的截图里挑出最合适的一段，收齐后交给渲染。

落在哪一层：**平台**。节点类型 `each` 是平台的通用节点，每一项做的那一步就是一个普通节点（`llm`、`tool`、`code`、`template`、`transcribe`），设置、检查、调模型、外发策略、用量都和单独用那种节点一样。应用里只写节点设置。

参考 Dify 的「迭代」、Coze 的「批处理」。和 Dify 不同，每一项只做一步，不在节点里再画一张图：要做几步就接几个逐项处理节点（前一个的 `results` 是后一个的 `items`），能打进运行包，画布上也不用嵌套。

## 节点 `each`

```jsonc
{"id": "pick", "type": "each", "title": "逐个挑镜头",
 "items": "{{plan.shots}}",              // 必填，恰好一个引用，取到的值要是列表
 "do": {                                 // 必填，每一项做的那一步：一个节点的设置，不写 id、title 等公共字段
   "type": "llm",
   "model": "company-vision",
   "prompt": "这句配音是「{{item.line}}」，从下面几张截图里挑最合适的一张，回答它的编号。",
   "images": ["{{item.frames}}"],
   "output": {"kind": "json", "fields": [{"key": "choice", "type": "number"}]}
 },
 "concurrency": 3,                       // 可选，同时处理几项，1–10，不写为 3
 "on_item_error": "stop"}                // 可选，某一项失败时："stop" 节点失败（默认），"skip" 记下来接着处理别的项
```

`do.type` 可以是 `llm`、`tool`、`code`、`template`、`transcribe`。其余设置按那种节点写（见 [flow-apps.md](flow-apps.md)「节点类型」和各自的契约），不写的按它的默认值。`do` 里不能写公共字段（`id`、`title`、`on_error`、`max_visits`、`note`、`ui`），失败处理写在逐项处理节点自己的 `on_error` 上，见下。

### 当前项 `{{item.*}}`

`item` 和 `env`、`param` 一样是保留的名字，节点不能叫 `item`。`{{item.*}}` 只能写在逐项处理节点的 `do` 里，写在别处保存时报错“{{item.*}} 只能写在逐项处理节点的「每一项做什么」里”。

| 写法 | 取到什么 |
| --- | --- |
| `{{item.value}}` | 整个当前项 |
| `{{item.position}}` | 当前项是第几项，从 1 开始 |
| `{{item.<字段>}}` | 当前项是对象时它的这个字段；不是对象或没有这个字段时为空 |

- `value`、`position` 总是指整项和位置。当前项自己有叫 `value`、`position` 的字段时取不到，要换个字段名。
- 其他规则同普通引用：整个值恰好是一个引用时传原始值（对象、数字、列表），写在文字里按文本插入。
- `do` 里也可以引用别的节点、常量、项目和会话，规则同逐项处理节点自己的设置（引用的节点要一定已经跑过）。
- 当前项来自数据值时（`items` 引用的是数据值，见 [projects.md](projects.md)「数据外发策略」），`{{item.*}}` 也按数据值外发。

### 产出

| 字段 | 内容 |
| --- | --- |
| `results` | 列表，和 `items` 一一对应、顺序相同。每项是这一步的产出（`llm` 是 `{"text": …}` 或 JSON 字段，`tool` 是 `{"result": …}`，`code` 是 `outputs` 的字段……）；跳过的项为 null |
| `failed` | 失败后跳过的项：`[{"position": 3, "error_type": "model_error", "error_message": "……"}]`，没有时是 `[]` |
| `count` | 一共几项 |

无分支，可以设 `on_error`。

### 运行时

- 取 `items` 的值。不是列表时失败（`input_error`，“「items」取到的不是列表”）；超过 100 项时失败（`input_error`，“最多处理 100 项，这次有 N 项”）。空列表直接往下走，`results` 为 `[]`。
- 每一项把 `{{item.*}}` 换成这一项的值，按 `do.type` 那种节点跑一次。调模型、外发策略检查、用量记录都和单独的节点一样，用量记在逐项处理节点上。
- 最多同时处理 `concurrency` 项，其余排队。每完成一项报一次进度（[connector-jobs.md](connector-jobs.md)）：`percent` 为完成的比例，`message` 为“已完成 3/20 项”，有跳过的项时加“，跳过 2 项”。这一步里调的长任务工具不再单独报进度。
- 重试：`on_error.retry` 按每一项算，一项失败只重试这一项；`input_error` 不重试（同普通节点）。
- 一项重试后仍失败：
  - `on_item_error` 为 `stop`：取消正在处理的项，不再开始没开始的项，节点按这一项的 `error_type` 失败，说明前面加“第 3 项：”。然后照 `on_error.then` 处理（停止运行、用默认值继续或走出错分支）。
  - 为 `skip`：这一项记进 `failed`、`results` 里为 null，接着处理别的项。每一项都失败时节点照样失败，按第一个失败的项报错。
- 平台重启时整个节点重跑。调的是长任务工具时（比如渲染），同一项的同一次调用接着等原来的任务，不重新开始（任务键按节点、第几次运行、工具和参数算，见 connector-jobs.md）。
- 本运行被取消时，正在处理的项也停下。
- 单节点试跑：可以，`items` 的值要在试跑的输入里给。

### 保存时检查

- `items` 恰好一个引用，引用的节点和字段规则同普通引用（存在、一定已经跑过）。
- `do` 是对象，`do.type` 是上面五种之一，不写公共字段；其余设置按那种节点检查，`loc` 指到 `do` 里面，如 `["body", "config", "graph", "nodes", "pick", "do", "prompt"]`。`code` 的 `code` 原样保留，里面的 `{{` 不当引用。
- `concurrency` 是 1–10 的整数；`on_item_error` 是 `stop` 或 `skip`。
- `on_error.then` 为 `default` 时，`default` 的键是 `results`、`failed`、`count`。
- 任何节点的 id 不能是 `item`。以前存下的应用里有节点叫 `item` 的，下次保存时报错，改个 id 即可。

## 用到什么

模型、Skill、数据源和工具、代码沙箱、密钥：`do` 里用到的和单独的节点一样算进应用用到的东西，包括导出文件的 `requires`、运行包的运行要求清单（[runtime.md](runtime.md)）、资产目录的使用情况和新建运行时的外发策略检查。`do` 是 `tool` 时，`{{secret.*}}` 可以写在 `do.args` 里。

## 页面

- 编辑页「添加节点」在「逻辑」下有「逐项处理」。设置表单：
  - 「对哪个列表逐项处理」：插一个变量；
  - 「每一项做什么」：选一种节点，下面是那种节点的设置表单；插变量时多出「当前项」「当前项的位置」（`{{item.value}}`、`{{item.position}}`），当前项的字段手写 `{{item.字段}}`；
  - 「同时处理几项」「某一项失败时」。
- 运行页：运行中这一步显示进度（“已完成 3/20 项”）；结束后同别的节点，显示 `results`、`failed`、`count`。
- mock（`AEP_MOCK_RUNS`）：按顺序模拟每一项，每一项的产出同模拟那种节点。

## `/v1` 和 MCP

不变。外部页面看 `step.node` 和 `progress` 就知道逐项处理到了第几项（[connector-jobs.md](connector-jobs.md)）。
