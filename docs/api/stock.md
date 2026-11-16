# 图库连接器

落在哪一层：**连接器（MCP）**。图库连接器在可商用的素材站上按词搜视频，把选中的下载到媒体卷；平台把下载的文件存进媒体库时记下它来自哪个站、哪个页面、什么授权（[media-processing.md](media-processing.md)「产出的文件存进媒体库」）。任何应用都能用：调用工具节点直接选它的工具，存库用 `save_media`。第一个用它的是「教练做视频」：讲知识的句子配图库画面，教练自己的素材只占 20–40%。

第一个来源是 **Pixabay**（Pexels 已经不发新的 API key）。以后加别的站，工具和结果的格式不变，`id` 前缀和 `site` 区分来源。

## 连接器（`connectors/stock`）

MCP Streamable HTTP，地址 `http://connector-stock:8009/mcp`，`GET /healthz` 检查进程存活。和媒体处理连接器挂同一个媒体卷，只写 `_work/stock/`：下载的视频留在 `cache/`，每次回答给一份自己的（`out-<随机>/` 下的硬链接，不支持硬链接时复制一份），缩略图在 `thumbs/`。超过 24 小时的自己删掉；媒体处理连接器清 `_work` 时也可能整个清掉，所以每次写之前重新建目录。

每次回答一份自己的文件，是因为运行用 `save_media` 存库时会把文件移走：两次运行同时下载同一段、存进不同的库时，互不影响。

Pixabay 的规矩照办：

- 同样的搜索 24 小时内不再问 Pixabay，用缓存的回答。
- 文件下载到自己的服务器上用，不外链。
- 只下载选中的，不批量下载。
- 搜索结果和存进库的素材都写明来自 Pixabay。

### `search_videos`

```jsonc
// 参数
{"query": "woman cooking vegetables",   // 1–100 个字符
 "lang": "en",                          // 可选，搜索词的语言：en（默认）或 zh
 "orientation": "any",                  // 可选，portrait | landscape | any（默认）
 "min_short_side": 1080,                // 可选，只要有短边至少这么多像素的版本的，默认 1080，360–4320
 "min_seconds": 3, "max_seconds": 60,   // 可选，时长范围，默认 0–不限
 "limit": 8,                            // 可选，1–20，默认 8
 "thumbnails": false}                   // 可选，true 时把每条的缩略图下载成工作文件，给看图模型用

// 结果
{"site": "Pixabay",
 "items": [
   {"id": "pixabay:12345",              // 下载时用
    "site": "Pixabay",
    "url": "https://pixabay.com/videos/id-12345/",   // 素材在站上的页面
    "author": "某某",
    "tags": ["cooking", "kitchen"],
    "seconds": 14,
    "width": 1080, "height": 1920,      // 下载时会拿到的版本（满足 min_short_side 的最小那个）
    "orientation": "portrait",          // portrait | landscape | square
    "thumbnail": {"aep_file": {"path": "_work/stock/thumbs/pixabay-12345.jpg", "name": "pixabay-12345.jpg"}} | null}
 ],
 "total": 23}                           // 过滤后一共有几条（只看 Pixabay 对这个词最热门的前 100 条）
```

- 只要实拍视频（不要动画），打开安全搜索。
- `orientation` 按要下载的那个版本的宽高判断。宽高相等的是 `square`，只在 `any` 时出现。
- 没有一条满足条件时 `items` 是空列表，不算错。
- `thumbnails` 为 false 时 `thumbnail` 是 null。缩略图用短边至少 360 像素里最小的那张；下载失败的那条 `thumbnail` 是 null，不影响别的。

### `download_video`

```jsonc
// 参数
{"id": "pixabay:12345", "min_short_side": 1080}

// 结果（长任务做完时的 result，见 connector-jobs.md）
{"file": {"aep_file": {"path": "_work/stock/out-3f9a…/pixabay-12345-1080x1920.mp4",
                       "name": "pixabay-12345.mp4",
                       "source": {"kind": "stock", "site": "Pixabay",
                                  "url": "https://pixabay.com/videos/id-12345/",
                                  "author": "某某",
                                  "license": "Pixabay Content License",
                                  "license_url": "https://pixabay.com/service/license-summary/"}}},
 "width": 1080, "height": 1920, "seconds": 14}
```

- 下载满足 `min_short_side` 的最小版本，不下 4K。没有这么清楚的版本时报工具错误「这段视频没有短边 1080 像素以上的版本」。
- 总是按长任务回答（[connector-jobs.md](connector-jobs.md)）：先等最多 `AEP_STOCK_WAIT_SECONDS`（默认 15）秒，下完了直接是 `done`，没下完交回 `running` 的任务，进度是已下载的比例。同一个任务键接上原来的任务。24 小时内下过、文件还在的，直接回答 `done`，不再下载。`aep_job_cancel` 停下下载、删掉没下完的文件。
- 任务只记在内存里：连接器重启后查不到原来的任务，节点按自己的 `on_error` 处理，重新执行时重新下载。
- 同时最多下载 `AEP_STOCK_MAX_DOWNLOADS`（默认 3）个，别的排队。
- 文件超过 `AEP_STOCK_MAX_MB`（默认 300）MB 时不下载（或停下），报「文件超过 300 MB，没有下载」。
- 下载地址只认 `https://*.pixabay.com`。

存进媒体库：调用工具节点设 `save_media`，结果里的 `file` 带着 `source`，平台记下来源；库里已有同一个页面的素材时不再存一份（[media-processing.md](media-processing.md)）。

### 错误

工具错误的说法（不会带出 key）：

| 情况 | 说法 |
| --- | --- |
| 没配置 key | 「没有配置 Pixabay 的 API key（AEP_STOCK_PIXABAY_KEY）」 |
| key 不对（Pixabay 回 400 且说 key 无效） | 「Pixabay 不认这个 API key」 |
| 请求太频繁（429） | 「Pixabay 请求太频繁，过一分钟再试」 |
| 连不上、超时、5xx | 「连不上 Pixabay：…」 |
| 回答不是预期的 JSON | 「Pixabay 的回答看不懂」 |
| `id` 格式不对 | 「id 要写成 pixabay:数字」 |
| 这个 id 在 Pixabay 上没有了 | 「Pixabay 上没有这段视频了：pixabay:12345」 |

参数超出范围由 MCP 的参数校验报错。

### 设置

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `AEP_STOCK_PIXABAY_KEY` | 空 | Pixabay 的 API key，只放在服务器的 `.env` 里 |
| `AEP_MEDIA_DIR` | `/data/media` | 媒体卷的挂载点 |
| `AEP_STOCK_MCP_HOST` / `AEP_STOCK_MCP_PORT` | `0.0.0.0` / `8009` | |
| `AEP_STOCK_PIXABAY_URL` | `https://pixabay.com/api/videos/` | |
| `AEP_STOCK_CACHE_HOURS` | 24 | 搜索结果、下载的文件、缩略图留多久 |
| `AEP_STOCK_WAIT_SECONDS` | 15 | 下载时先等几秒再交回任务，要比平台的工具调用超时（30 秒）短 |
| `AEP_STOCK_MAX_MB` | 300 | 单个文件上限 |
| `AEP_STOCK_MAX_DOWNLOADS` | 3 | 同时下载几个 |
| `AEP_STOCK_MCP_ALLOWED_HOSTS` / `AEP_STOCK_MCP_ALLOWED_ORIGINS` | 本机和 `connector-stock` | JSON 数组 |

外部网络：要能访问 `pixabay.com` 和 `cdn.pixabay.com` 的 443 端口。应用发布成运行包时，在运行要求的 `network` 里写上这两个（[runtime.md](runtime.md)）。

## 部署

- `docker-compose.yml` 和 `infra/runtime/compose.yml` 里有 `connector-stock`，挂 `media-data` 卷，`AEP_STOCK_PIXABAY_KEY` 从 `.env` 读，不把 `.env` 里别的变量交给它。平台编排的 `AEP_DEFAULT_DATASOURCES` 有「图库」。
- `AEP_CONNECTOR_HOSTS` 的默认值有 `connector-stock:8009`；运行包的 `infra/runtime/env.example` 也写上了它。已经装好的运行包，`.env` 里的 `AEP_CONNECTOR_HOSTS` 要加上它才能绑定。
- 没填 key 时连接器照常启动，健康检查通过，调用工具时报上面那条错误。

## 测试

连接器测试（`connectors/stock/tests`）用进程内的假 Pixabay（API 和 CDN 都是假的，不连外网）：

- 搜索：按短边、横竖、时长过滤，选满足条件的最小版本，`limit`、`total`；只要实拍、安全搜索、前 100 条，语言照参数；同样的搜索（大小写、空格不同也算）24 小时内只问一次，过期后再问；缩略图下载成工作文件，有一张失败时只有那条是 null。
- 下载：拿到满足条件的最小版本，结果带 `source`；同一个任务键是同一个任务；24 小时内再下直接用已有的文件，每次回答一份自己的、移走一份不影响另一份；`_work/stock` 被整个清掉后照常下载；没有够清楚的版本、文件超过上限、id 不对、Pixabay 上没有了时报错；慢的交回任务、能查到进度、做完后有结果；取消后删掉没下完的文件。
- 错误：没配 key（不问 Pixabay）、key 不对、429、5xx、回答看不懂、连不上；错误里不带 key。
