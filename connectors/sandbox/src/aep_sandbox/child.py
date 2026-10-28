"""The child process of one run: sets its own limits, then runs the user's code.

Reads ``{"code", "inputs"}`` as JSON on stdin and writes ``{"result": ...}`` or
``{"error": "..."}`` as JSON on stdout. Whatever the code prints goes to stderr,
which the runner keeps as logs. The limits here keep honest mistakes (an endless
loop, a huge DataFrame) in check; they are not the security boundary, the
container and its network are (see the README).
"""

import json
import math
import os
import resource
import sys
import traceback
from typing import Any

FILE = "<code>"


def limit(cpu_seconds: int, memory_bytes: int, file_bytes: int) -> None:
    resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds + 1))
    resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))
    resource.setrlimit(resource.RLIMIT_FSIZE, (file_bytes, file_bytes))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    # No new processes (not enforced for root, which the image never runs as).
    resource.setrlimit(resource.RLIMIT_NPROC, (0, 0))


def plain(value: Any) -> Any:
    """``value`` as JSON data: numpy and pandas values converted, NaN and infinity
    as null."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if value is None or isinstance(value, str | int | bool):
        return value
    if isinstance(value, dict):
        return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [plain(v) for v in value]
    for name in ("to_dict", "tolist", "item", "isoformat"):
        convert = getattr(value, name, None)
        if callable(convert):
            if name == "to_dict":
                try:
                    return plain(convert(orient="records"))
                except TypeError:
                    return plain(convert())
            return plain(convert())
    raise TypeError(f"返回值里有不能转成 JSON 的 {type(value).__name__}")


def describe(exc: BaseException) -> str:
    """The error with the traceback lines from the user's code only."""
    if isinstance(exc, MemoryError):
        return "内存不够：超过了沙箱的内存上限"
    if isinstance(exc, SyntaxError) and exc.filename == FILE:
        return f"第 {exc.lineno} 行语法错误：{exc.msg}"
    frames = [f for f in traceback.extract_tb(exc.__traceback__) if f.filename == FILE]
    lines = [f"第 {f.lineno} 行，在 {f.name} 里" for f in frames]
    lines.append("".join(traceback.format_exception_only(exc)).strip())
    return "\n".join(lines)


def run(request: dict[str, Any]) -> dict[str, Any]:
    namespace: dict[str, Any] = {"__name__": "__sandbox__"}
    try:
        exec(compile(request["code"], FILE, "exec"), namespace)
        main = namespace.get("main")
        if not callable(main):
            return {"error": "代码里要定义 main(inputs) 函数"}
        result = main(request["inputs"])
    except BaseException as exc:  # noqa: BLE001 - every failure of user code is reported
        return {"error": describe(exc)}
    try:
        return {"result": plain(result)}
    except (TypeError, RecursionError) as exc:
        return {"error": str(exc)}


def entry() -> None:
    cpu, memory, files = (int(a) for a in sys.argv[1:4])
    request = json.loads(sys.stdin.read())
    out = os.fdopen(os.dup(1), "w", encoding="utf-8")
    os.dup2(2, 1)  # print() in user code ends up in the logs.
    sys.stdout = sys.stderr
    limit(cpu, memory, files)
    reply = run(request)
    try:
        text = json.dumps(reply, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError, RecursionError) as exc:
        text = json.dumps({"error": f"返回值不能转成 JSON：{exc}"}, ensure_ascii=False)
    out.write(text)
    out.flush()


if __name__ == "__main__":
    entry()
