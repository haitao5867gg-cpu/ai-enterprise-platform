"""English for the platform's own error messages (see ``aep.i18n``).

Keys are the Chinese messages as the code writes them; ``{name}`` stands for a value
the message carries, and the English names the same values. A message missing here is
returned in Chinese. Run content (plans, replies, reports) is the app's and is never
translated.
"""

MESSAGES: dict[str, str] = {
    # Sign-in and roles
    "请先登录": "Sign in first",
    "没有权限：这项操作需要{role}": "Not allowed: this needs the {role} role",
    "没有权限：这一步要审批者确认": "Not allowed: an approver has to decide this step",
    "使用者": "user",
    "审批者": "approver",
    "搭建者": "builder",
    "管理员": "admin",
    "账号服务还没有准备好": "The account service is not ready yet",
    "没有打开登录，不用登录": "Sign-in is off; there is no need to sign in",
    "没有打开登录，没有密码可改": "Sign-in is off; there is no password to change",
    "登录名要以小写字母开头，只能有小写字母、数字、. _ 和 -，2–40 个字符": (
        "A username starts with a lowercase letter and has 2–40 lowercase letters, "
        "digits, '.', '_' or '-'"
    ),
    # Request validation (aep.api.validation)
    "「{label}」的格式、长度或范围不正确": "“{label}” has the wrong format, length or range",
    "参数「{field}」的格式、长度或范围不正确": (
        "The field “{field}” has the wrong format, length or range"
    ),
    "请求字段不能为 null": "Request fields cannot be null",
    "名称不能为空": "The name cannot be empty",
    "「{label}」不能为空": "“{label}” cannot be empty",
    # Runs
    "draft 和 app_version 不能同时给": "Give draft or app_version, not both",
    "运行结束后才能评价": "A run can be rated once it has ended",
    "反馈服务还没有准备好": "The feedback service is not ready yet",
    "这次运行没有可下载的代码": "This run has no code to download",
    "没有应用 {app}": "There is no app {app}",
    "没有应用「{app}」": "There is no app “{app}”",
    "应用 {app} 没有版本 {version}": "App {app} has no version {version}",
    "应用 {app} 没有草稿": "App {app} has no draft",
    "代码应用不能从某一步重跑": "A code app cannot be rerun from a step",
    "这个应用没有草稿": "This app has no draft",
    "这次运行用的应用或版本已经不在了": "The app or version this run used no longer exists",
    "内容已经更新，请刷新后再处理": "The content has changed; refresh and try again",
    "这一步现在不能选「{decision}」": "“{decision}” cannot be chosen at this step now",
    "这个决定需要写明意见": "This decision needs a comment",
    "只有运行中或等待确认的运行可以取消": "Only a running or waiting run can be cancelled",
    "这次运行已经不在失败状态，可能有人已经重试过": (
        "This run is no longer failed; someone may have retried it already"
    ),
    "这次运行不能重试，请复制后重新发起": "This run cannot be retried; copy it and run it again",
    "没有找到这次运行": "Run not found",
    "这次运行现在没有在等表单": "This run is not waiting for a form now",
    "这一步要审批者在平台上确认": "An approver has to decide this step on the platform",
    "应用「{title}」需要选择一个项目": "App “{title}” needs a project",
    "应用「{title}」不使用项目": "App “{title}” does not use projects",
    "没有项目 {project}": "There is no project {project}",
    "应用用到了参数 {param}，项目「{project}」没有定义它": (
        "The app uses the parameter {param}, which project “{project}” does not define"
    ),
    "项目 {project} 已经删除，不能重新运行": (
        "Project {project} has been deleted; the run cannot be run again"
    ),
    "{where}里没有节点 {node}": "{where} has no node {node}",
    "从开始节点重跑就是重新运行，请用“复制并重新发起”": (
        "Rerunning from the start node is a new run; use “Copy and run again”"
    ),
    "原运行没有跑到「{step}」这一步": "The original run did not reach the step “{step}”",
    "运行超过 {steps} 步，已经停止；请检查流程里的循环": (
        "The run went past {steps} steps and was stopped; check the flow for loops"
    ),
    "应用「{title}」本月的模型预算 ${budget} 已经用完，请管理员在运营看板调高预算或下月再用": (
        "App “{title}” has used its model budget of ${budget} for this month; an admin "
        "can raise it on the operations dashboard, or use it again next month"
    ),
    # Apps, versions, evals
    "已经有应用 {app}": "There is already an app {app}",
    "没有节点 {node}": "There is no node {node}",
    "没有草稿": "There is no draft",
    "草稿刚被改过，请刷新后重新评测再发布": (
        "The draft was just changed; refresh, run the evaluation again, then publish"
    ),
    "没有模板 {template}": "There is no template {template}",
    "没有编排应用 {app}，代码应用不能复制": (
        "There is no flow app {app}; code apps cannot be copied"
    ),
    "代码应用的名称和流程由代码定义，不能在这里修改或删除": (
        "A code app's name and flow are defined in code and cannot be changed or deleted here"
    ),
    "开始、人工和结束节点不能单独试跑": "Start, human and end nodes cannot be test-run alone",
    "{label}节点会写 Databricks，不能单独试跑": (
        "A {label} node writes to Databricks and cannot be test-run alone"
    ),
    "数据检查节点要读这次运行的结果表和项目，不能单独试跑": (
        "A data check node reads the run's result tables and project; it cannot be test-run alone"
    ),
    "调用应用节点会真的发起一次运行，不能单独试跑": (
        "A call-app node really starts a run; it cannot be test-run alone"
    ),
    "提交 PR 节点会真的在代码仓库开 PR，不能单独试跑": (
        "A submit-PR node really opens a PR; it cannot be test-run alone"
    ),
    "{reason}，通过后才能发布": "{reason}; it can be published once it passes",
    "已经有评测在进行": "An evaluation is already running",
    "这个应用没有评测集": "This app has no eval set",
    "找不到评测集文件 {path}，请检查 AEP_EVALS_DIR": (
        "Eval set file {path} not found; check AEP_EVALS_DIR"
    ),
    "平台里没有登记数据源，评测跑不了": (
        "No data source is registered, so the evaluation cannot run"
    ),
    "找不到评测用的数据源 {source}（AEP_EVAL_DATASOURCE）": (
        "The evaluation data source {source} was not found (AEP_EVAL_DATASOURCE)"
    ),
    "运行等人回答超过 {count} 次": "The run waited for answers more than {count} times",
    "用例没有给表单「{node}」的值": "The case gives no values for the form “{node}”",
    "表单「{node}」的值不对：{problems}": "The values for the form “{node}” are wrong: {problems}",
    "「{title}」没有选项 {decision}": "“{title}” has no option {decision}",
    "「{title}」选 {decision} 要写意见，用例里没写": (
        "Choosing {decision} at “{title}” needs a comment, and the case has none"
    ),
    "「{title}」的回答不对：{problems}": "The answer to “{title}” is wrong: {problems}",
    "run {run_id} 超时没有结束": "Run {run_id} did not end in time",
    "这次运行不是应用 {app} 的": "This run is not of app {app}",
    "运行结束后才能存成用例": "A run can be saved as a case once it has ended",
    "一个评测集最多 {count} 条用例": "An eval set holds at most {count} cases",
    "这个应用没有开对话，用例不能带历史": (
        "This app does not take conversations, so a case cannot have history"
    ),
    "历史的消息和回答合计最多 {count} 字": (
        "The history's messages and replies have at most {count} characters together"
    ),
    "还原表里有历史里没有的标记": "The restored map has tokens that are not in the history",
    "这是智能体应用的文件，不能导入成编排应用的草稿": (
        "This file is an agent app's; it cannot become a flow app's draft"
    ),
    "这是编排应用的文件，不能导入成智能体应用的草稿": (
        "This file is a flow app's; it cannot become an agent app's draft"
    ),
    "不认识的应用类型 {kind}，这里只读编排应用和智能体应用": (
        "Unknown app kind {kind}; only flow and agent apps are read here"
    ),
    "只有智能体应用能转成编排应用": "Only agent apps can be turned into flow apps",
    "这个应用没有开放给使用者": "This app is not open to users",
    "这个应用没有开放这个入口": "This app does not take requests this way",
    "应用「{title}」不允许被其他应用调用": "The app “{title}” cannot be called by other apps",
    "这次运行属于已过期的会话，内容已清除": (
        "This run was a turn of an expired conversation; its content has been cleared"
    ),
    # Skills
    "没有 Skill {name}": "There is no Skill {name}",
    "Skill {name} 没有版本 {version}": "Skill {name} has no version {version}",
    "发布说明不能为空": "The release note cannot be empty",
    "已经有叫 {name} 的 Skill": "There is already a Skill named {name}",
    "Skill {name} 没有草稿": "Skill {name} has no draft",
    "Skill {name} 没有草稿可以发布": "Skill {name} has no draft to publish",
    # Schedules
    "没有定时 {schedule}": "There is no schedule {schedule}",
    "每个应用最多 {count} 个定时": "An app can have at most {count} schedules",
    "应用 {app} 还没有发布，不能定时运行": "App {app} is not published yet and cannot be scheduled",
    "应用 {app} 不能运行了：{reason}": "App {app} can no longer run: {reason}",
    "定时写法不对：{where}的「{text}」看不懂，只能写数字、*、-、/ 和逗号": (
        "Invalid schedule: “{text}” in {where} is not understood; use digits, *, -, / and commas"
    ),
    "定时写法不对：{where}的 {value} 超出 {low}–{high}": (
        "Invalid schedule: {value} in {where} is outside {low}–{high}"
    ),
    "定时写法不对：要写 5 段（分 时 日 月 周），用空格分开": (
        "Invalid schedule: write 5 fields (minute hour day month weekday) separated by spaces"
    ),
    "定时写法不对：{where}的步长「{step}」要是正整数": (
        "Invalid schedule: the step “{step}” in {where} must be a positive integer"
    ),
    "定时写法不对：{where}的范围 {range} 从大到小": (
        "Invalid schedule: the range {range} in {where} goes from high to low"
    ),
    "时区 {name} 不认识": "Unknown time zone {name}",
    # Conversations (docs/api/conversations.md)
    "没有找到这个会话": "Conversation not found",
    "这个应用没有开对话": "This app does not take conversations",
    "没有草稿可以试": "There is no draft to try",
    "上一条还没处理完": "The previous message is still being handled",
    "会话里已经有后面的消息，这一轮不能重试": (
        "The conversation has later messages, so this turn cannot be retried"
    ),
    "上一条还没处理完，稍后用 get_run 查它的结果": (
        "The previous message is still being handled; check its result later with get_run"
    ),
    "消息不能为空": "The message cannot be empty",
    "消息最多 {count} 个字符": "A message has at most {count} characters",
    "消息在发消息时给，不放在会话的输入里": (
        "The message goes with each message sent, not in the conversation's inputs"
    ),
    "new_conversation 和 conversation_id 只能给一个": (
        "Give new_conversation or conversation_id, not both"
    ),
    "会话里的其他输入在开会话时已经定了": (
        "The conversation's other inputs were set when it was opened"
    ),
    "应用没有打开对话，不能带会话历史": (
        "The app does not take conversations, so nodes cannot use the history"
    ),
    "应用没有打开对话，不能引用会话": (
        "The app does not take conversations, so nothing can refer to one"
    ),
    "会话没有 {name} 这一项，可以用 {fields}": (
        "A conversation has no {name}; use one of {fields}"
    ),
    "结束节点没有输出 {reply}": "No end node has the output {reply}",
    "要选一个结束节点的输出作为回答": "Choose an end node output as the reply",
    "带的历史轮数要在 0–{max} 之间": "The turns of history must be between 0 and {max}",
    "打开对话要在开始节点里有一个必填的文字输入 message": (
        "A conversation needs a required text input named message in the start node"
    ),
    "conversation 必须是对象": "conversation must be an object",
    "history 必须是 true 或 false": "history must be true or false",
    # App API keys and notifications
    "API 密钥服务还没有准备好": "The API key service is not ready yet",
    "没有找到这个密钥": "Key not found",
    "缺少或无效的 API 密钥": "Missing or invalid API key",
    "这个密钥不能调用应用 {app}": "This key cannot call app {app}",
    "一个应用最多 20 个密钥": "An app can have at most 20 keys",
    "通知服务还没有准备好": "The notification service is not ready yet",
    "没有找到这个 Webhook": "Webhook not found",
    # Knowledge
    "没有这个知识库": "Knowledge base not found",
    "没有知识库 {kb}": "There is no knowledge base {kb}",
    "知识库里没有这篇文档": "The knowledge base has no such document",
    "没有可用的向量模型别名「{model}」": "No embedding model alias “{model}” is available",
    "没有可用的重排模型别名「{model}」": "No rerank model alias “{model}” is available",
    "请选择至少一个 PDF、DOCX、MD 或 TXT 文件": "Choose at least one PDF, DOCX, MD or TXT file",
    "单次最多上传 20 个文件": "Upload at most 20 files at a time",
    "一个知识库最多 500 个文档": "A knowledge base holds at most 500 documents",
    "失效日期不能早于生效日期": "The expiry date cannot be before the effective date",
    "原文件不存在": "The original file does not exist",
    "用例编号「{case}」重复了": "Case ID “{case}” is repeated",
    "先加检索用例再运行回归": "Add retrieval cases before running the regression",
    "已存在名为「{name}」的知识库": "There is already a knowledge base named “{name}”",
    "已存在同名知识库": "There is already a knowledge base with this name",
    "知识库有文档后不能更改向量模型": (
        "The embedding model cannot change once the knowledge base has documents"
    ),
    "这次上传里有两个「{file}」，替换时每个文件名只能传一个": (
        "This upload has two “{file}”; when replacing, give each file name once"
    ),
    "向量模型调用失败：{error}": "The embedding model call failed: {error}",
    "知识库里没有文档「{document}」": "The knowledge base has no document “{document}”",
    "重叠字符数不能超过片段大小的一半": "The overlap cannot be more than half the chunk size",
    "切分大小和重叠长度不正确": "The chunk size or overlap is wrong",
    "重排候选数不能少于 top_k": "Rerank candidates cannot be fewer than top_k",
    "重排模型调用失败：{error}": "The rerank model call failed: {error}",
    "重排模型返回的候选数量或编号不正确": (
        "The rerank model returned the wrong number or IDs of candidates"
    ),
    "候选编号超出范围或重复": "Candidate IDs are out of range or repeated",
    "对话模型重排输出格式不正确：{error}": "The chat model's rerank output is malformed: {error}",
    "未知的文档字段": "Unknown document field",
    "未知的知识库字段": "Unknown knowledge base field",
    "文字编码必须为 UTF-8 或 GB18030": "Text must be encoded as UTF-8 or GB18030",
    "不支持的文档格式": "Unsupported document format",
    "文件「{file}」：只支持 .pdf、.docx、.md 和 .txt": (
        "File “{file}”: only .pdf, .docx, .md and .txt are supported"
    ),
    "文件「{file}」：文件名含有无效字符": "File “{file}”: the file name has invalid characters",
    "文件「{file}」：单文件不能超过 20 MB": "File “{file}”: a file cannot exceed 20 MB",
    "平台没有配置知识库": "The platform has no knowledge base configured",
    "向量模型返回的向量数量或顺序不正确": (
        "The embedding model returned the wrong number or order of vectors"
    ),
    "向量维度必须为 {expected}，收到 {got}": "Vectors must have {expected} dimensions, got {got}",
    "向量模型返回了无效数值": "The embedding model returned invalid numbers",
    "回复里没有 JSON 对象": "The reply has no JSON object",
    # Data sources and uploads
    "数据集名称必须为 1～100 个字符": "A dataset name has 1–100 characters",
    "请选择至少一个 CSV 或 XLSX 文件": "Choose at least one CSV or XLSX file",
    "「{name}」不是代码仓库": "“{name}” is not a code repository",
    "代码仓库连接器返回的仓库列表格式不对": (
        "The code repository connector returned a malformed repository list"
    ),
    "第一行必须是完整、非空的表头": "The first row must be a complete, non-empty header",
    "缺少表头，第一行不能全是数字": "The header is missing: the first row cannot be all numbers",
    "表头重复（忽略大小写和首尾空格）": (
        "The header repeats a name (ignoring case and surrounding spaces)"
    ),
    "表头不能包含空字符": "The header cannot contain null characters",
    "CSV 含有无效的空字符": "The CSV contains invalid null characters",
    "文件或工作表为空，表头后至少需要一行数据": (
        "The file or sheet is empty; it needs at least one row after the header"
    ),
    "文件「{file}」：{detail}": "File “{file}”: {detail}",
    "第 {row} 行的列数与表头不一致": "Row {row} has a different number of columns from the header",
    "CSV 编码必须是 UTF-8 或 GBK": "A CSV must be encoded as UTF-8 or GBK",
    "只支持 .csv 和 .xlsx 文件": "Only .csv and .xlsx files are supported",
    "单文件不能超过 50 MB": "A file cannot exceed 50 MB",
    "文件为空": "The file is empty",
    "一个数据集最多 {count} 张表（每个 CSV 一张，Excel 每个工作表一张），请拆成多个数据集上传": (
        "A dataset holds at most {count} tables (one per CSV, one per Excel sheet); "
        "split it into several datasets"
    ),
    "工作表「{sheet}」：{detail}": "Sheet “{sheet}”: {detail}",
    # Operations, catalog, assets
    "days 只能是 7、30 或 90": "days must be 7, 30 or 90",
    "不认识的类别 {kind}": "Unknown kind {kind}",
    "没配资产仓库（AEP_ASSETS_REPO、AEP_ASSETS_GIT_URL）": (
        "No assets repository is configured (AEP_ASSETS_REPO, AEP_ASSETS_GIT_URL)"
    ),
    "标签最多 {count} 个": "At most {count} tags",
    "每个标签 1–{count} 个字符": "Each tag has 1–{count} characters",
    "{label}不提交到资产仓库": "{label} is not committed to the assets repository",
    "「{item}」还没有发布的版本": "“{item}” has no published version yet",
    "「{item}」是代码应用，不提交到资产仓库": (
        "“{item}” is a code app and is not committed to the assets repository"
    ),
    "「{item}」放在代码里，不提交到资产仓库": (
        "“{item}” lives in code and is not committed to the assets repository"
    ),
    # Projects
    "没有项目 {project_id}": "There is no project {project_id}",
    "模型网关上没有模型 {models}": "The model gateway has no model {models}",
    "项目标识建好后不能改，要和地址里的一致": (
        "A project ID cannot change once created; it must match the one in the address"
    ),
    "项目 {project_id} 已经存在": "Project {project_id} already exists",
    "已经有叫「{name}」的项目": "There is already a project named “{name}”",
    "项目「{project}」没有环境 {environment}": (
        "Project “{project}” has no environment {environment}"
    ),
    "项目「{project}」没有参数 {param}": "Project “{project}” has no parameter {param}",
    "运行参数要是一个对象": "Run parameters must be an object",
    "引用表 {name} 要写成 catalog.schema.table 三段，每段不含空白和反引号": (
        "Referenced table {name} must be catalog.schema.table, with no spaces or "
        "backticks in each part"
    ),
    "引用表 {name}：{problem}": "Referenced table {name}: {problem}",
    "参数「{label}」定时运行时的取法不对：{problem}": (
        "How parameter “{label}” is set for scheduled runs is wrong: {problem}"
    ),
    "项目标识不能是 new，页面地址 /projects/new 用了它": (
        "A project ID cannot be new; the page /projects/new uses it"
    ),
    "参数「{label}」不是文本，不能写格式": (
        "Parameter “{label}” is not text and cannot have a format"
    ),
    "参数「{label}」没有写格式，不用写格式说明": (
        "Parameter “{label}” has no format, so it needs no format hint"
    ),
    "参数「{label}」是选项，要列出 1–50 个选项": (
        "Parameter “{label}” is a choice; list 1–50 options"
    ),
    "参数「{label}」的选项值重复": "Parameter “{label}” repeats an option value",
    "参数「{label}」不是选项，不能写 choices": (
        "Parameter “{label}” is not a choice and cannot have choices"
    ),
    "默认值不对：{problem}": "The default value is wrong: {problem}",
    "表名 {table} 只能用小写字母、数字和下划线，不含 catalog、schema": (
        "Table name {table} may use only lowercase letters, digits and underscores, "
        "without catalog or schema"
    ),
    "归本项目的表 {table} 要以表名前缀 {prefix} 开头": (
        "Table {table} owned by this project must start with the prefix {prefix}"
    ),
    "请填写参数「{label}」": "Fill in the parameter “{label}”",
    "{what} {names} 重复": "{what} {names} is repeated",
    # Flow files and nodes
    "文件不能超过 1 MB": "The file cannot exceed 1 MB",
    "文件内容必须是一个对象": "The file must hold an object",
    "不认识的文件格式 {got}，这里只读 {expected}": (
        "Unknown file format {got}; only {expected} is read here"
    ),
    "文件里没有 config": "The file has no config",
    "不是合法的 YAML：{error}": "Not valid YAML: {error}",
    "知识库的内容超过整篇放进去的上限 {count} 个字符，没有截断：调高 max_chars，或者改用检索（mode 写 search）": (  # noqa: E501
        "The knowledge base is longer than the {count}-character limit for whole "
        "documents and was not cut: raise max_chars, or use search (mode: search)"
    ),
    "绑定项目的运行请用 job 方式上线（AEP_DATABRICKS_DEPLOY）": (
        "A run bound to a project is released as a job (AEP_DATABRICKS_DEPLOY)"
    ),
    "这次运行绑定项目时还没有记下项目的全部环境，请重新发起运行": (
        "This run did not record all of the project's environments; start a new run"
    ),
    "上线的环境 {environment} 就是这次试算的环境，请选别的环境": (
        "The release environment {environment} is the one this trial ran in; choose another"
    ),
    "这次运行的代码是平台升级前生成的，定时运行时没法按项目的规则算参数，请重新发起运行": (
        "This run's code predates a platform upgrade and cannot work out the project's "
        "parameters on a schedule; start a new run"
    ),
    # ETL app
    "请选择一个已登记的数据源（input.datasource_id）": (
        "Choose a registered data source (input.datasource_id)"
    ),
    "「{name}」是 Databricks 数据源，ETL 应用要选数据库或上传文件的数据源，Databricks 的表请用编排应用 etl-lakehouse 第 3 版": (  # noqa: E501
        "“{name}” is a Databricks data source; the ETL app needs a database or uploaded "
        "files. For Databricks tables use the flow app etl-lakehouse version 3"
    ),
    "「{name}」是{what}，ETL 要选数据库或上传文件的数据源": (
        "“{name}” is {what}; the ETL app needs a database or uploaded files"
    ),
    "请用自然语言描述需求（input.request）": "Describe the need in plain words (input.request)",
    "模型给出的方案不可用：{error}": "The model's plan cannot be used: {error}",
    "方案要是一个 JSON 对象": "The plan must be a JSON object",
    "方案格式不符合要求：{errors}": "The plan does not follow the format: {errors}",
    "数据源 {name} 里没有可读的表": "Data source {name} has no readable tables",
    "数据源里有 {count} 张表，一次最多 {limit} 张，请指定 tables": (
        "The data source has {count} tables and at most {limit} are read at once; name the tables"
    ),
    "表 {name} 要写成 catalog.schema.table": "Table {name} must be written catalog.schema.table",
    "表 {name} 要写成 schema.table": "Table {name} must be written schema.table",
}

# The node types offered when building a flow app (GET /node-types): names, what each
# does, its outputs and presets.
NODE_TYPES: dict[str, str] = {
    "Databricks bundle 文件": "Databricks bundle files",
    "Databricks 上这个 Job 的链接": "Link to this Job on Databricks",
    "Databricks 上这次运行的链接": "Link to this run on Databricks",
    "Job 的 id": "Job ID",
    "PR 编号": "PR number",
    "PR 链接": "PR link",
    "bundle 里的文件路径": "File paths in the bundle",
    "deployed 或 failed": "deployed or failed",
    "dev 运行": "Dev run",
    "passed 或 failed": "passed or failed",
    "一句话总结": "One-sentence summary",
    "一句话说明": "One-sentence description",
    "上线到 prod": "Release to prod",
    "上线报告（Markdown）": "Release report (Markdown)",
    "业务说明（第 2 版方案）": "Business brief (plan version 2)",
    "交给后面步骤的一段话，按意见处理时才有（确认模式）": (
        "A note for the next steps, only when handled by the comments (approval mode)"
    ),
    "人工": "Human",
    "从文本里抽取结构化字段，每个字段成为一个变量": (
        "Extract structured fields from text; each field becomes a variable"
    ),
    "代码": "Code",
    "写入的 catalog": "Catalog written to",
    "发现的问题，一条一行，给重写的那一步": (
        "Problems found, one per line, for the step that rewrites"
    ),
    "发现的问题：规则、位置、问题、依据、怎么改": (
        "Problems found: rule, place, problem, basis, how to fix"
    ),
    "另调一次模型，只按一份清单审查前面某一步的产出；有问题就让那一步带着问题重写": (
        "Call a model once more to review an earlier step's output against a checklist; "
        "on problems that step rewrites with them"
    ),
    "可放进提示词的资料": "Material to put in a prompt",
    "命中的片段和出处编号": "Matched chunks and their source numbers",
    "回答是否有依据（只依据资料回答时检查）": (
        "Whether the answer rests on what was found (checked when answering only from sources)"
    ),
    "没有依据时被换掉的原回答": "The original answer, when it was replaced for having no grounds",
    "指令最多 {count} 个字符": "Instructions have at most {count} characters",
    "grounded_only 必须是 true 或 false": "grounded_only must be true or false",
    "调用的应用必须是列表": "The apps to call must be a list",
    "最多调用 {count} 个应用": "An agent calls at most {count} apps",
    "每项要有 app、when 和 enabled": "Each item needs app, when and enabled",
    "调用的应用没有 {key} 这一项": "An app to call has no setting {key}",
    "要选择被调用的应用": "Choose the app to call",
    "应用 {name} 选了两次": "The app {name} is chosen twice",
    "要写什么时候调用它": "Say when to call it",
    "什么时候调用最多 {count} 个字符": "When to call it has at most {count} characters",
    "enabled 必须是 true 或 false": "enabled must be true or false",
    "不能调用自己": "An app cannot call itself",
    "没有已发布的应用 {name}": "There is no published app {name}",
    "「{title}」是代码应用，智能体只能调用编排应用和智能体应用": (
        "“{title}” is a code app; agents call flow and agent apps only"
    ),
    "应用「{title}」里有人工节点，智能体等不了人": (
        "The app “{title}” has a human step, and an agent cannot wait for a person"
    ),
    "应用「{title}」的必填输入「{label}」要选{kind}，模型填不了": (
        "The app “{title}” needs a {kind} chosen for “{label}”, which a model cannot fill in"
    ),
    "没有这个已发布的应用，请在智能体里重新选择": (
        "There is no such published app; choose another one in the agent"
    ),
    "在沙箱里运行一段 Python（标准库、pandas、numpy），做计算、格式转换、数据整理": (
        "Run Python in a sandbox (standard library, pandas, numpy) to calculate, convert "
        "or tidy data"
    ),
    "处理人": "Handled by",
    "失败时的原因": "Why it failed",
    "失败时的报错": "The error when it failed",
    "定义运行时要填的输入": "Define the inputs a run asks for",
    "定时的中文说法": "The schedule in words",
    "工具返回的内容": "What the tool returned",
    "平台自动整理过的地方（第 2 版方案）": "What the platform tidied (plan version 2)",
    "开始": "Start",
    "弹一个表单让人填写，填好的字段成为变量": (
        "Show a form for a person to fill in; the fields become variables"
    ),
    "意图分类": "Classify intent",
    "意见（确认模式）": "Comment (approval mode)",
    "成立的规则标识，都不成立时为 else": "The rule that holds; else when none does",
    "所有结果表的质量小结": "Quality summary of all result tables",
    "把 bundle 部署到 Databricks dev 目标运行，收集行数和质量规则结果，预览结果数据": (
        "Deploy the bundle to the Databricks dev target and run it; collect row counts "
        "and quality rule results and preview the data"
    ),
    "把几个变量拼成一段文字": "Join variables into one text",
    "把另一个已发布的编排应用当成一步：按它的输入发起一次运行，等它结束后取出结果": (
        "Use another published flow app as a step: start a run with its inputs and take "
        "its result when it ends"
    ),
    "把审核过的 bundle 部署到 prod（绑定项目时是项目的某个环境），按定时在 Databricks 上自动刷新": (  # noqa: E501
        "Deploy the reviewed bundle to prod (a project environment when bound to a "
        "project) and refresh it on Databricks on a schedule"
    ),
    "把文件以 PR 提交到代码仓库（GitHub 或 Bitbucket）：在平台自己的分支上提交，合不合并由人决定": (  # noqa: E501
        "Submit files to a code repository (GitHub or Bitbucket) as a PR on the "
        "platform's own branch; people decide whether to merge"
    ),
    "把源表的数据整表复制到 Databricks（源表 catalog 下同名的 schema 和表）": (
        "Copy source tables to Databricks in full (same schema and table names under the "
        "source catalog)"
    ),
    "抽取到湖仓": "Load to lakehouse",
    "抽取字段": "Extract fields",
    "拼好的文字": "The joined text",
    "挂在条目上的意见（确认模式）": "Comments on items (approval mode)",
    "按方案的答案设计算出的答案": "The answer worked out from the plan's answer design",
    "按规则走不同分支，不调模型": "Branch by rules, without a model",
    "按问题检索知识库，产出带出处编号的片段和提示词上下文": (
        "Search knowledge bases for a question; give chunks with source numbers and prompt context"
    ),
    "探查数据": "Profile data",
    "提交 PR": "Submit PR",
    "提交到的分支": "Branch committed to",
    "提交号": "Commit",
    "改动的文件": "Changed files",
    "数据检查": "Data check",
    "数据源 id": "Data source ID",
    "数据源名称": "Data source name",
    "文本拼接": "Join text",
    "方案建议的数据检查，可以整段给数据检查节点的 include（第 3 版方案）": (
        "Data checks the plan proposes, ready for a data check node's include (plan version 3)"
    ),
    "方案引用了的资料": "Material the plan cites",
    "方案用到的源表": "Source tables the plan uses",
    "方案被校验退回后，通过的这一版去掉了的检查和列": (
        "Checks and columns the passing version dropped after the plan was sent back"
    ),
    "是不是沿用了这次运行上次抽取的表": "Whether the tables loaded earlier in this run were reused",
    "是否上线成功": "Whether the release succeeded",
    "是否新开的 PR（否：更新了已有的）": "Whether the PR is new (no: an existing one was updated)",
    "是否通过": "Whether it passed",
    "是否通过（没有严重级别为 error 的检查不通过或出错）": (
        "Whether it passed (no check of severity error failed or broke)"
    ),
    "智能体": "Agent",
    "暂停运行，等人确认或补充信息": "Pause the run for a person to approve or add information",
    "最后一次审查有没有发现问题（没有为 true）": "Whether the last review found nothing (true)",
    "最后按哪个选项处理（确认模式）": "The option finally chosen (approval mode)",
    "最终回答": "Final answer",
    "有没有哪题没按建议答（确认模式）": (
        "Whether any question was answered against the suggestion (approval mode)"
    ),
    "条件判断": "Condition",
    "校验后的方案（JSON）": "The checked plan (JSON)",
    "校验方案": "Check plan",
    "检查并打包": "Check and package",
    "检查报告（Markdown）": "Check report (Markdown)",
    "模型审查": "Model review",
    "模型的回答（输出方式为文本时）": "The model's answer (when the output is text)",
    "模型自己决定调用哪些工具、调用几次，直到给出回答": (
        "The model decides which tools to call and how often, until it answers"
    ),
    "每张目标表的代码": "Code for each target table",
    "每张结果表的前几行": "The first rows of each result table",
    "每张结果表的行数和质量规则结果": "Row counts and quality rule results of each result table",
    "每张表的探查结果": "Profile of each table",
    "每张表的来源、目标、行数和列数": "Source, target, rows and columns of each table",
    "每条检查的结果": "The result of each check",
    "每题的答案（确认模式）": "The answer to each question (approval mode)",
    "没找到口径依据的指标、维度和筛选（第 2 版方案）": (
        "Metrics, dimensions and filters without a documented definition (plan version 2)"
    ),
    "没改过来时给人看的说明（Markdown）；改好了或没问题时为空": (
        "A note for people when it was not fixed (Markdown); empty when fixed or fine"
    ),
    "没通过的原因": "Why it did not pass",
    "用的 Quartz cron": "The Quartz cron used",
    "目标表名": "Target table names",
    "知识检索": "Knowledge search",
    "答案设计（第 2 版方案）": "Answer design (plan version 2)",
    "类别名称": "Class name",
    "类别标识，都不是时为 other": "Class key; other when none fits",
    "经 Databricks 只读连接器核对结果数据：总数、和源表对账、键是否重复、不该有的行、和老系统逐行对比": (  # noqa: E501
        "Check result data through the Databricks read-only connector: totals, "
        "reconciliation with sources, duplicate keys, unexpected rows, row-by-row "
        "comparison with the legacy system"
    ),
    "结束": "End",
    "结束运行，给出结果": "End the run and give the result",
    "结果表的主键（第 3 版方案）": "Primary keys of result tables (plan version 3)",
    "结果表的表名，给数据检查的 golden 用（第 3 版方案）": (
        "Result table names, for a data check's golden (plan version 3)"
    ),
    "给人看的方案": "The plan for people",
    "给模型看的已有的表和写到这些表的规则": (
        "Existing tables and the rules for writing them, for the model"
    ),
    "给模型看的源表说明": "Source table notes for the model",
    "表名列表": "Table names",
    "被调用运行的 id": "ID of the called run",
    "被调用运行的整个结果": "The called run's whole result",
    "要人回答的题，可以整段给人工节点的 fields（第 2 版方案）": (
        "Questions for people, ready for a human node's fields (plan version 2)"
    ),
    "解析模型写的 ETL 方案，按探查结果校验表、列和取值，通过后整理成给人确认的方案": (
        "Parse the model's ETL plan, check tables, columns and values against the "
        "profile, and turn it into a plan for people to confirm"
    ),
    "让模型把文本归到几类中的一类，每类走一条分支": (
        "Have a model put text into one of several classes; each class takes a branch"
    ),
    "请人补充信息": "Ask a person for information",
    "调一次模型，产出一段文字或几个字段": "Call a model once for a text or a few fields",
    "调用工具": "Call tool",
    "调用应用": "Call app",
    "调用数据源的一个只读工具": "Call one read-only tool of a data source",
    "调用模型": "Call model",
    "调用过的工具": "Tools called",
    "运行报告（Markdown）": "Run report (Markdown)",
    "这次上线的详情": "Details of this release",
    "这次处理的完整记录（确认模式）": "The full record of this decision (approval mode)",
    "这次运行的详情": "Details of this run",
    "通过数据源的只读连接器读取源表的结构、统计、取值分布和样本行": (
        "Read source tables' structure, statistics, value distributions and sample rows "
        "through the data source's read-only connector"
    ),
    "静态检查模型写的转换代码，通过后打包成 Databricks bundle": (
        "Statically check the model's transformation code and package it as a Databricks bundle"
    ),
    "项目在环境里已有的表和列（绑定项目的 Databricks 数据源）": (
        "The project's existing tables and columns in the environment (Databricks data "
        "source bound to the project)"
    ),
}
