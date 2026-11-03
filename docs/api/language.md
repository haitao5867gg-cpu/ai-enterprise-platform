# 界面语言与接口返回的语言

网页有中文 / English 切换（`frontend/lib/i18n`，选择记在 `aep_locale` cookie）。接口按请求的 `Accept-Language` 决定平台自己的文字用哪种语言。

## 请求

- `Accept-Language` 里英文排在中文前面（按 `q` 值，同值按先后）时用英文，否则用中文。没有这个头、或只有别的语言时用中文。
- 网页的接口代理（`frontend/app/api/[...path]/route.ts`）不转发浏览器自己的 `Accept-Language`，而是按 `aep_locale` 发：`en` 或 `zh-CN`。所以页面上选的语言就是接口回答的语言。
- 直接调接口的客户端（应用 API、脚本）自己带这个头；不带时和以前一样是中文。

## 哪些文字会变

| 内容 | 英文时 |
| --- | --- |
| 出错时的 `detail`（字符串） | 平台登记过的报错换成英文（`aep.messages_en.MESSAGES`）；报错里带的值（名称、数字）原样保留；没登记的报错仍是中文 |
| `GET /node-types` 的 `label`、`description`、`outputs[].description`、`presets[].label/description` | 英文（`aep.messages_en.NODE_TYPES`）；`defaults`、`presets[].settings` 里的提示词等设置不变 |
| 网页代理自己的报错（后端不可用、上传格式不对） | 英文 |

不会变的：应用和用户写的内容（应用名、节点名、出口名、方案、回答、报告、运行记录里的消息）、校验错误的列表形式 `detail`、MCP 端点（`/mcp/apps`）的报错。

## 加新的报错

报错照常用中文写。要有英文就在 `backend/src/aep/messages_en.py` 的 `MESSAGES` 里加一条：键是代码里的中文，`{名字}` 代表报错里带的值（名字随意，但一条里不能重复），英文用同样的 `{名字}`。几条用「；」连起来的报错会逐条翻译。`tests/test_i18n.py` 检查每条英文只用到它的中文里有的值。
