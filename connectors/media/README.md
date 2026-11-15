# 媒体处理连接器

MCP Streamable HTTP，默认端口 **8008**，端点 `/mcp`，`GET /healthz` 检查进程存活。封装 ffmpeg，给编排应用的调用工具节点和智能体用：探测素材、抽帧给看图模型看、把一组镜头渲染成一条带配音和字幕的 MP4。契约见 [docs/api/media-processing.md](../../docs/api/media-processing.md)。

| 工具 | 做什么 |
| --- | --- |
| `probe` | 类型、时长、宽高（手机竖拍按显示方向）、帧率、编码、大小；可以顺带出一张缩略图 |
| `frames` | 按时间点、按间隔或均匀取几帧，JPG |
| `render` | 镜头首尾相接（三种铺法：黑边、裁满、模糊铺底），叠配音和背景音乐，烧字幕，导出 H.264/AAC 的 MP4；长任务 |
| `aep_job_status`、`aep_job_cancel` | 长任务查进度、取消（[connector-jobs.md](../../docs/api/connector-jobs.md)） |

## 文件

和平台挂同一个媒体卷（`AEP_MEDIA_DIR`，默认 `/data/media`）。参数里的路径都相对这个目录：库里的文件只读，连接器做出来的东西都写在 `_work/<任务>/`，24 小时后删掉。结果里的文件写成 `{"aep_file": {"path", "name"}}`，平台按节点设置把要留的移进媒体库。

- 绝对路径、`..`、软链接和跑出目录的路径一律拒绝。
- 镜像里的进程先以 root 启动，只为把 `_work` 交给 65532 用户，然后用 `setpriv` 降到这个用户运行，库里的目录它只能读。
- 渲染好的镜头按参数和素材文件缓存在 `_work/.cache`，换一个镜头再渲染时别的镜头不重做。
- 做完的任务记在 `_work/.jobs`，重启后还认得；重启时没做完的记为失败，运行重试会用同一个任务键开新任务。

## 设置

环境变量统一 `AEP_` 前缀，集中在 `aep_media.config.Settings`：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `AEP_MEDIA_DIR` | `/data/media` | 媒体卷 |
| `AEP_MEDIA_MCP_HOST` / `AEP_MEDIA_MCP_PORT` | `0.0.0.0` / `8008` | |
| `AEP_MEDIA_MAX_JOBS` | 1 | 同时渲染几个，别的排队 |
| `AEP_MEDIA_CHECK_SECONDS` | 20 | 渲染前检查素材最多几秒，要比平台的工具调用超时短 |
| `AEP_MEDIA_MAX_SECONDS` | 600 | 成片最长几秒 |
| `AEP_MEDIA_WORK_HOURS` | 24 | 工作文件、缓存、任务记录留多久 |
| `AEP_MEDIA_FFMPEG_THREADS` | 0 | 每个 ffmpeg 进程的线程数，0 让 ffmpeg 自己定 |
| `AEP_MEDIA_MCP_ALLOWED_HOSTS` / `AEP_MEDIA_MCP_ALLOWED_ORIGINS` | 本机和 `connector-media` | JSON 数组 |

## 开发

要本机装有 ffmpeg（带 libass）。测试用 ffmpeg 现做几秒的视频、音频和图片，不连网。

```bash
cd connectors/media
uv sync --frozen
uv run pytest -q
AEP_MEDIA_DIR=/tmp/media uv run aep-media
```
