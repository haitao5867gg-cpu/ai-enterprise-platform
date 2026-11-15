# 媒体库接口

后端和前端按这份约定并行开发。字段名 snake_case，时间是 ISO 8601 字符串，错误格式同 [apps-and-skills.md](apps-and-skills.md)：`{"detail": "中文说明"}`，校验错误 422。

落在哪一层：**平台资源**。媒体库不属于某个应用，任何应用都能用；第一个用它的是「教练做视频」（教练自己的素材、从可商用素材站下载的网络素材）。

## 概念

- **媒体库**：一组视频、音频和图片，比如「教练素材」「网络素材缓存」。平台记每个素材的描述、标签、来源这些记录；文件放在哪由存储方式决定。本文是第一种 `local`：文件在平台服务器本地硬盘（`AEP_MEDIA_DIR`，默认 `/data/media`），布局是 `<library_id>/<item_id>/<文件名>`，上传下载经过平台接口，用于本地开发和单机部署。
- **执行节点存储**（下一批，契约另补）：文件放在外部应用自己的执行节点上（对象存储），平台只发签名链接，上传下载都不经过平台服务器。记录和检索接口不变，只是上传改成先要链接再直传、`…/file` 改成跳转到签名链接。
- **素材**：库里的一个文件，带画面描述、标签、来源和授权。描述和标签决定流程能不能按一句话找到它，所以都能改。
- **用在哪里**（后续批次）：媒体处理连接器读这些文件做探测、抽帧、剪辑和渲染；编排应用的节点按描述检索素材；应用 API（`/v1`）让外部页面往绑定的库里上传素材。这几项各有自己的契约，本文只管库和素材本身。

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

## 后续批次（各自补契约）

- 媒体处理连接器（`connectors/media`，MCP）：探测时长和宽高、抽帧、截取、拼接、烧字幕、渲染；上传后由它把 `processing` 变成 `ready`，并给每个视频生成一张缩略图（`GET …/thumbnail`）。
- 应用 API：应用设置里绑定媒体库，`/v1` 的密钥可以往绑定的库里上传、列出素材；运行的输入可以引用素材，结果里的文件给下载链接。
- 编排应用：按描述检索素材的工具，供节点和智能体调用。
