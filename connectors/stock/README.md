# 图库连接器

MCP Streamable HTTP，默认端口 **8009**，端点 `/mcp`，`GET /healthz` 检查进程存活。在 Pixabay 上搜可商用的视频，把选中的下载到媒体卷，给编排应用的调用工具节点和智能体用。契约见 [docs/api/stock.md](../../docs/api/stock.md)。

| 工具 | 做什么 |
| --- | --- |
| `search_videos` | 按词搜实拍视频，按清晰度、横竖、时长过滤，可以顺带下载缩略图给看图模型看 |
| `download_video` | 下载一段，拿满足清晰度要求的最小版本；结果带来源（站、页面、作者、授权）；长任务 |
| `aep_job_status`、`aep_job_cancel` | 下载任务查进度、取消（[connector-jobs.md](../../docs/api/connector-jobs.md)） |

## 文件和 key

- 和平台挂同一个媒体卷（`AEP_MEDIA_DIR`，默认 `/data/media`），只写 `_work/stock/`，超过 24 小时的文件删掉。平台按节点的 `save_media` 把要留的移进媒体库。
- 镜像里的进程先以 root 启动，只为把 `_work/stock` 交给 65532 用户，然后用 `setpriv` 降到这个用户运行。
- Pixabay 的 API key 只放在服务器的 `.env`（`AEP_STOCK_PIXABAY_KEY`）。key 是请求地址里的参数，所以连接器不记录请求地址，错误里也不带 key。
- 搜索结果缓存 24 小时；任务只记在内存里。

## 设置

环境变量统一 `AEP_` 前缀，集中在 `aep_stock.config.Settings`，见 [docs/api/stock.md](../../docs/api/stock.md)「设置」。

## 开发

测试用进程内的假 Pixabay，不连网。

```bash
cd connectors/stock
uv sync --frozen
uv run pytest -q
AEP_MEDIA_DIR=/tmp/media AEP_STOCK_PIXABAY_KEY=... uv run aep-stock
```
