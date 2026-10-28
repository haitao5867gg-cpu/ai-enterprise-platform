# 代码沙箱连接器

编排应用的代码节点在这里运行用户写的 Python。MCP Streamable HTTP，默认端口 **8004**，
端点 `/mcp`，`GET /healthz` 检查进程存活。只有一个工具：

`run_python(code, inputs, timeout_seconds=10)`：运行代码里的 `main(inputs)`，返回
`{"status": "ok", "result", "logs"}`、`{"status": "error", "error", "logs"}` 或
`{"status": "timeout", "error", "logs"}`。`logs` 是代码 `print` 的最后一段；返回值里的
numpy、pandas 值会转成 JSON（DataFrame 按行转成列表），NaN 和无穷大变成 null。
可用标准库和预装的 pandas、numpy，不能安装新包。

## 隔离

每次运行起一个新的子进程（`python -I`），环境变量为空，工作目录是一个新的临时目录，运行完删除。
子进程给自己设上限：CPU 时间（`timeout_seconds`，最多 `AEP_SANDBOX_MAX_TIMEOUT_SECONDS`，默认 30）、
地址空间（`AEP_SANDBOX_MEMORY_MB`，默认 512，导入 pandas 本身约占 150）、写文件大小（16 MB）、
不能再开进程。连接器另外限制墙钟时间（超时多给 2 秒启动时间）、返回值大小（1 MB）和同时运行的个数
（`AEP_SANDBOX_PARALLEL`，默认 4）。

这些 Python 和进程层面的限制挡得住死循环、超大数据这类失误，但不是安全边界。安全边界是容器：

- 镜像以非 root 用户运行，compose 里根文件系统只读，只有 `/tmp` 是小 tmpfs；
- 去掉全部 Linux capabilities，禁止提权，限制进程数和内存；
- 独自接入 compose 的 internal 网络 `sandbox`，访问不了外网，也访问不了数据库、模型网关和 API。
  API 经 `sandbox-relay`（同一镜像里的 TCP 转发，`aep-sandbox-relay`）连到它：relay 是唯一同时接入两个
  网络的容器，只把连接转给沙箱，所以从沙箱里连 relay 也只会回到沙箱自己；
- 容器里没有模型密钥、数据库凭据等任何配置。

在 Azure 上同样做法：NetworkPolicy 禁止出站，并用 gVisor 运行时再加一层隔离。

## 开发

```bash
cd connectors/sandbox
uv sync --frozen
uv run pytest -q          # 以 root 运行时，进程数和写文件的检查会跳过
uv run aep-sandbox
```

从仓库根目录对真实 Compose 栈验证（代码节点能跑，访问不了外网和数据库）：

```bash
docker compose up --build -d --wait
scripts/smoke-sandbox.sh
```
