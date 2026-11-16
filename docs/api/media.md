# 媒体库接口

后端和前端按这份约定并行开发。字段名 snake_case，时间是 ISO 8601 字符串，错误格式同 [apps-and-skills.md](apps-and-skills.md)：`{"detail": "中文说明"}`，校验错误 422。

落在哪一层：**平台资源**。媒体库不属于某个应用，任何应用都能用；第一个用它的是「教练做视频」（教练自己的素材、从可商用素材站下载的网络素材）。

## 概念

- **媒体库**：一组视频、音频和图片，比如「教练素材」「网络素材缓存」。文件存在运行这套平台的服务器本地硬盘（`AEP_MEDIA_DIR`，默认 `/data/media`），布局是 `<library_id>/<item_id>/<文件名>`。外部应用装运行包时，媒体库就在外部应用自己的机器上，素材和成片不出那台机器。以后换成对象存储时只改存储这一层，接口不变。
- **素材**：库里的一个文件，带画面描述、标签、来源和授权。描述和标签决定流程能不能按一句话找到它，所以都能改。
- **用在哪里**：编排应用绑定媒体库后，开始节点的输入可以是库里的素材，外部页面经应用 API（`/v1`）上传、列出、下载素材，见下面“应用用媒体库”。后续批次：媒体处理连接器读这些文件做探测、抽帧、剪辑和渲染；编排应用的节点按描述检索素材。这两项各有自己的契约。

## 数据结构

```jsonc
// MediaLibrary
{
  "library_id": "uuid",
  "name": "教练素材",              // 1–50 字符，不重名
  "description": "",              // 最多 500 字符
  "item_count": 9,
  "total_bytes": 201326592,
  "status_counts": {"processing": 0, "ready": 9, "error": 0},
  "created_at": "……", "updated_at": "……"
}

// MediaItem
{
  "item_id": "uuid",
  "library_id": "uuid",
  "name": "卧推侧面.mp4",           // 上传时的文件名，去掉路径
  "kind": "video" | "audio" | "image",
  "size_bytes": 26312704,
  "status": "processing" | "ready" | "error",
  "error": null | "……",             // status 为 error 时的原因
  "duration_seconds": 12.4 | null,  // 探测出来的，图片和没探测时为 null
  "width": 720 | null,
  "height": 1280 | null,
  "orientation": "portrait" | "landscape" | "square" | null,   // 由宽高算出
  "description": "健身房杠铃卧推，侧面全景",   // 最多 500 字符
  "tags": ["胸", "卧推"],           // 最多 20 个，每个 1–20 字符，去重，保持顺序
  "usable": true,                  // false：留在库里，但流程不会选中它
  "source": {                      // 素材从哪来
    "kind": "upload" | "stock" | "run",   // 人上传的、从素材站下载的、某次运行产出的
    "site": null | "Pexels",
    "url": null | "https://……",    // 素材在来源站的页面
    "author": null | "……",
    "license": null | "Pexels License",
    "license_url": null | "https://……",
    "run_id": null | "……"          // kind 为 run 时是产出它的运行
  },
  "uploaded_by": "王教练" | null,
  "created_at": "……", "updated_at": "……"
}
```

- `kind` 按扩展名定：视频 `.mp4 .mov .m4v .webm .mkv`，音频 `.m4a .mp3 .wav .aac .ogg .opus .flac`，图片 `.jpg .jpeg .png .webp`。别的扩展名返回 422「文件「x」：只支持视频（mp4、mov、m4v、webm、mkv）、音频（m4a、mp3、wav、aac、ogg、opus、flac）和图片（jpg、jpeg、png、webp）」。
- `status`：上传后是 `processing`，平台在后台探测时长、宽高（媒体处理连接器，后续批次）后变成 `ready` 或 `error`。**没有配置媒体处理连接器时**，上传后直接是 `ready`，`duration_seconds`、`width`、`height`、`orientation` 都是 null。
- `error` 的素材也能下载、能删，只是流程不会选中它。

## 接口

库（写操作要搭建者角色，同知识库）：

- `GET /media-libraries` → `[MediaLibrary]`，按名称排序。
- `POST /media-libraries`，`{"name", "description"?}` → 201 `MediaLibrary`。重名 409「已存在名为「x」的媒体库」。
- `GET /media-libraries/{library_id}` → `MediaLibrary`；没有 404「没有这个媒体库」。
- `PATCH /media-libraries/{library_id}`，`{"name"?, "description"?}` → `MediaLibrary`。
- `DELETE /media-libraries/{library_id}` → 204，连同库里的素材和文件一起删。

素材：

- `GET /media-libraries/{library_id}/items?q=&tag=&kind=&usable=&limit=&offset=` → `{"items": [MediaItem], "total": 37}`，新的在前。
  - `q`：在名称、描述和标签里找，空格分开的每个词都要出现（不分大小写）。
  - `tag`：有这个标签的；`kind`：`video`、`audio`、`image`；`usable`：`true` 或 `false`。
  - `limit` 1–200，默认 50；`offset` 默认 0。`total` 是符合条件的总数。
- `POST /media-libraries/{library_id}/items`，`multipart/form-data`：
  - `files`：1–20 个文件，单个不超过 `AEP_MEDIA_MAX_FILE_MB`（默认 1024）MB。超了 422「文件「x」：单个文件不能超过 1024 MB」；空文件 422「文件「x」：是空文件」；一个都没有 422「请选择至少一个视频、音频或图片文件」；多于 20 个 422「单次最多上传 20 个文件」。文件名去掉路径，超过 200 字节时截短主名、保留扩展名。
  - `description`、`tags`（JSON 数组字符串，如 `["胸"]`）：可选，用于这批所有文件。`tags` 不是这样的数组时 422「「标签」要是 JSON 数组，最多 20 个，每个 1–20 个字」。
  - `uploaded_by`：可选，最多 50 字符；开了登录时用登录用户。
  - 一个库最多 `AEP_MEDIA_MAX_ITEMS`（默认 5000）个素材，超了 422「一个媒体库最多 5000 个素材」。
  - 校验整批通过后才保存任何文件；出错时这批一个都不留。服务器硬盘满了 507「服务器硬盘满了，这批素材没有保存」。
  - → 202 `[MediaItem]`，`source.kind` 为 `upload`。
- `GET /media-libraries/{library_id}/items/{item_id}` → `MediaItem`；没有 404「没有这个素材」。
- `PATCH /media-libraries/{library_id}/items/{item_id}`，`{"description"?, "tags"?, "usable"?, "source"?}` → `MediaItem`。`source` 整个替换，字段同上（`kind` 必填，`site`、`author`、`license`、`run_id` 最多 200 字符，`url`、`license_url` 最多 2000 字符且要以 `http://` 或 `https://` 开头，否则 422「来源链接要以 http:// 或 https:// 开头」）。
- `DELETE /media-libraries/{library_id}/items/{item_id}` → 204，文件一起删。
- `GET /media-libraries/{library_id}/items/{item_id}/file` → 文件本身，`Content-Type` 按扩展名，支持 `Range`（手机上边下边播），`Content-Disposition: inline`，文件名按 RFC 5987 编码。文件丢了 404「素材文件不见了」。

## 设置

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `AEP_MEDIA_DIR` | `/data/media` | 文件存放目录；compose 里是 `media-data` 卷，媒体处理连接器挂同一个卷 |
| `AEP_MEDIA_MAX_FILE_MB` | 1024 | 单个文件上限 |
| `AEP_MEDIA_MAX_ITEMS` | 5000 | 一个库最多几个素材 |

## 应用用媒体库（`/v1` 传文件）

落在哪一层：**平台能力**。编排应用在配置里绑定媒体库，开始节点的输入可以是库里的一个素材；外部页面用应用的 API 密钥往绑定的库里上传、列出、下载素材；运行产出的文件在 `RunResult.files` 里给下载地址。任何编排应用都能这样接外部页面，应用不用写代码。第一个用的是「教练做视频」：教练在手机页面上传配音，页面拿配音的素材 id 发起运行，看进度，最后下载成片。

### 绑定

编排配置加一个可选的顶层键 `media`，跟着版本走：

```jsonc
"media": {
  "libraries": [
    {"library_id": "uuid", "access": "write"},   // write：/v1 能上传、改、删，也能列出和下载
    {"library_id": "uuid", "access": "read"}     // read：/v1 只能列出和下载
  ]
}
```

- 最多 10 个，同一个库不能出现两次（「媒体库 x 绑定了两次」），`access` 只能是 `read` 或 `write`。库不存在时保存报 422「没有这个媒体库」，`loc` 如 `["body", "config", "media", "libraries", 0, "library_id"]`。不写 `media` 等于不绑定。
- `/v1` 按应用**现在的版本**的绑定放行。草稿里改了绑定，发布后才生效；回滚到旧版本，绑定也回到旧版本的。
- 导出文件原样带上 `media`；`requires` 加 `media_libraries: [{"library_id", "name", "access"}]`。导入时库不存在不拦，`problems` 加一条 `{"kind": "media_library", "library_id": "uuid", "name": "教练素材", "message": "没有这个媒体库，请新建一个或在配置里换掉"}`，导入的配置放进草稿，规则同缺数据源（[flow-apps.md](flow-apps.md)“导出和导入”）。
- 智能体应用暂不能绑定（配置表单里没有这一项）。

### 素材输入

开始节点的输入加一种类型 `media`：

```jsonc
{"key": "voiceover", "label": "配音", "type": "media", "required": true,
 "library": "uuid",          // 必填：从哪个库里选，要是 media.libraries 里绑定的库
 "kinds": ["audio"]}         // 可选：只收这几类（video、audio、image），不写为都收
```

保存时检查（`loc` 指到这个输入里的键）：

- `library` 没写，或不在 `media.libraries` 里：「素材输入要选一个绑定到应用的媒体库」。
- `kinds` 要是 1–3 个不重复的 `video`、`audio`、`image`：「素材类型只能是 video、audio、image」。
- 不能写 `default`（「素材输入不能设默认值」）；`max_length`、`examples`、`choices` 的规则同别的类型（只有文字、下拉能写）。
- 只有开始节点的输入能用这种类型；人工节点表单的字段用了，报「只有开始节点的输入可以是素材」。

新建运行（`POST /runs`、`POST /v1/apps/{app}/runs`、MCP 的运行工具、定时运行、调用应用节点）时，值是素材的 `item_id`。按顺序检查，第一条不过就 422，`loc` 为 `["body", "input", "voiceover"]`：

1. 不是文字：「配音要是素材 id」
2. 库里没有：「配音：媒体库「配音」里没有这个素材」
3. 类型不对：「配音要是音频」（`kinds` 有几种时「要是视频或音频」）
4. 还在 `processing`：「配音：素材还在处理，稍后再试」；`error`：「配音：素材处理失败了，不能用」

`usable: false` 的素材照样能用：“流程不会选中它”说的是流程自己挑素材，人点名要用的不受限。运行记录的 `input` 里存的就是 `item_id`；复制运行、重跑时重新检查。

开始节点的产出不是 id，而是这个素材的摘要：

```jsonc
"voiceover": {
  "item_id": "uuid", "library_id": "uuid", "name": "配音0115.m4a", "kind": "audio",
  "duration_seconds": 48.2 | null, "width": null, "height": null,
  "path": "<library_id>/<item_id>/配音0115.m4a"   // 相对 AEP_MEDIA_DIR；连接器挂同一个卷，拼上自己的挂载点就是文件
}
```

- 整段引用（工具参数、代码输入里只写 `{{start.voiceover}}`）拿到这个对象；写在提示词等文字里是它的 JSON。
- 运行开始前素材被删了：开始节点以 `input_error` 失败，「配音：素材已经被删除」。
- MCP 运行工具的参数里，这种输入是字符串，说明末尾加「（素材 id，先经 /v1 上传）」。智能体调用应用时模型填不了素材，规则同 `datasource` 输入（[agent-apps.md](agent-apps.md)“调用应用”）。

### `/v1` 的素材接口

认证、密钥、入口开关同 [app-access.md](app-access.md)：密钥只能用于自己的应用，`api` 入口关掉时 403「这个应用没有开放这个入口」。路径里的库要在应用现在版本的 `media.libraries` 里，否则 404「这个应用没有绑定这个媒体库」；对只读的库做写操作 403「这个应用对媒体库「教练素材」只有读权限」。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/v1/apps/{app}/media-libraries` | 绑定的库：`[MediaLibrary]`，每个多一个 `access`，按绑定的顺序 |
| GET | `/v1/apps/{app}/media-libraries/{library_id}/items` | 查询参数和返回同平台的素材列表 |
| POST | `/v1/apps/{app}/media-libraries/{library_id}/items` | 上传，要 `write`。`multipart/form-data`，规则和返回同平台的上传（202 `[MediaItem]`）；`uploaded_by` 不看请求里的，写「API：密钥名称」 |
| GET | `/v1/apps/{app}/media-libraries/{library_id}/items/{item_id}` | `MediaItem` |
| PATCH | `/v1/apps/{app}/media-libraries/{library_id}/items/{item_id}` | 改 `description`、`tags`、`usable`、`source`，要 `write` |
| DELETE | `/v1/apps/{app}/media-libraries/{library_id}/items/{item_id}` | 204，要 `write` |
| GET | `/v1/apps/{app}/media-libraries/{library_id}/items/{item_id}/file` | 文件本身，同平台的下载，支持 `Range` |

- 上传、改、删记进操作记录，同 `/v1` 发起运行：操作人为空，`target` 里带 `key`（密钥名称）和 `library_id`、`item_id`。
- 运行包上（`AEP_MODE=runtime`）这些接口照样开，不看自检：没装完自检的应用也能先传素材。
- 下载要带密钥，所以浏览器里的页面不能直接拿它当 `<video src>`。外部页面自己的后端带着密钥转发（密钥不能放进浏览器）；以后要给手机直接播放的短期链接时另加。

### 结果里的文件

`RunResult` 加 `files`：

```jsonc
"files": [{
  "item_id": "uuid", "library_id": "uuid", "name": "成片.mp4", "kind": "video",
  "size_bytes": 31457280, "duration_seconds": 52.0 | null, "width": 1080 | null, "height": 1920 | null,
  "download_path": "/v1/apps/coach-video/media-libraries/<library_id>/items/<item_id>/file"
}]
```

- 列的是这次运行产出的素材：绑定的库里 `source.kind` 为 `run`、`source.run_id` 是这次运行的，先建的在前，最多 50 个。没有时是 `[]`。不管运行完没完成都列，页面可以先拿到已经做好的部分。
- `download_path` 接在调用 `/v1` 的地址后面：平台上是 `{AEP_WEB_URL}/api` 加它，运行包上是域名加它。
- 平台的运行页用 `GET /runs/{run_id}/media`：同样的列表，但 `download_path` 是平台自己的素材接口 `/media-libraries/{library_id}/items/{item_id}/file`（页面加 `/api` 前缀）。运行不存在 404。
- 运行怎么把文件放进媒体库，由第二批媒体处理连接器的契约定（它渲染完在库里建 `source.kind: run` 的素材）。这一批只管列出和下载。

### 运行要求清单

`Requirements`（[runtime.md](runtime.md)）加 `media_libraries: [{"library_id", "name", "access"}]`，就是配置的 `media.libraries` 加上库名，按绑定的顺序。

- 运行包装包时，这台机器上没有的库按同一个 `library_id` 和名称新建；名称被别的库占了时用「名称（应用名）」。
- 自检加必需项 `{"kind": "media_library", "name": "教练素材", "library_id": "uuid"}`：库后来被删了时不通过，「没有这个媒体库，请重新装一次流程包」（重新装会再建）。

### 测试

- 绑定：保存时库不存在、重复、`access` 不对、超过 10 个；导入时缺库进 `problems`；导出带 `media` 和 `requires.media_libraries`。
- 素材输入：保存检查各条；新建运行的四种 422 和顺序；`usable: false` 能用；开始节点产出摘要，工具参数整段引用拿到对象；运行前素材被删了以 `input_error` 失败；MCP 工具参数是字符串。
- `/v1`：没绑定 404、只读 403、入口关掉 403、别的应用的密钥 403；上传写「API：密钥名称」并记操作记录；下载支持 `Range`。
- `files`：只列绑定库里这次运行产出的；运行没完成也列。
- 清单和运行包：`media_libraries` 进清单；装包时建库（同 id，重名时改名）；库被删后自检不通过。

### 页面

- 编辑页：应用设置里加「媒体库」一节，选库和权限（`media.libraries`）；开始节点的输入类型加「素材」，选绑定的库和收哪几类。
- 新建运行页：素材输入是一个选择框，列这个库里能用的素材（新的在前，可以搜），旁边能直接上传一个。
- 运行页：输入里的素材显示名称，能点开播放；产出的文件列在结果下面，能下载。

页面另排一个 PR，先做后端。

## 后续批次（各自补契约）

- 媒体处理连接器（`connectors/media`，MCP）：探测时长和宽高、抽帧、截取、拼接、烧字幕、渲染；上传后由它把 `processing` 变成 `ready`，并给每个视频生成一张缩略图（`GET …/thumbnail`）。见 [media-processing.md](media-processing.md)。
- 编排应用：按描述检索素材的工具，供节点和智能体调用。
- 运行产出的文件进媒体库（媒体处理连接器），给手机直接播放的短期下载链接。
