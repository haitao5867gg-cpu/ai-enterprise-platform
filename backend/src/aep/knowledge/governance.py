"""Knowledge governance: retrieval regression cases and definition-document templates
(docs/api/knowledge-governance.md)."""

from datetime import UTC, datetime
from typing import Any

from openai import AsyncOpenAI

from aep.config import Settings
from aep.knowledge.retrieval import defaults
from aep.knowledge.search import search
from aep.knowledge.store import KnowledgeBase, KnowledgeStore, Regression

MAX_CASES = 50


def is_stale(record: Regression, base: KnowledgeBase) -> bool:
    """True when the cases or the documents changed after the last run, or when there are
    cases that never ran."""
    if not record.cases:
        return False
    if not record.last_run:
        return True
    ran = datetime.fromisoformat(record.last_run["at"])
    return any(
        changed is not None and changed > ran
        for changed in (record.updated_at, base.docs_changed_at)
    )


def regression_out(record: Regression | None, base: KnowledgeBase) -> dict[str, Any]:
    record = record or Regression(base.kb_id)
    return {
        "kb_id": base.kb_id,
        "cases": record.cases,
        "retrieval": record.retrieval or defaults(),
        "top_k": record.top_k,
        "min_score": record.min_score,
        "updated_by": record.updated_by,
        "updated_at": record.updated_at,
        "last_run": record.last_run,
        "stale": is_stale(record, base),
    }


def quality(record: Regression | None, base: KnowledgeBase) -> tuple[float, str] | None:
    """The asset catalog's quality from the last regression: (score, detail)."""
    if record is None or not record.last_run or not record.last_run.get("total"):
        return None
    run = record.last_run
    detail = f"{run['total']} 条用例，通过 {run['passed']} 条"
    if is_stale(record, base):
        detail += "，文档改过还没重跑"
    return run["score"], detail


async def run_regression(
    store: KnowledgeStore,
    client: AsyncOpenAI,
    record: Regression,
    *,
    settings: Settings,
    by: str | None,
) -> dict[str, Any]:
    """Runs every case against the documents in effect now. A case passes when each
    expected document is among the top ``top_k`` hits; a case that fails to search records
    its error and does not stop the others. Hits are not counted as use by a run."""
    results = []
    for case in record.cases:
        try:
            hits = await search(
                store,
                client,
                [record.kb_id],
                case["query"],
                record.top_k,
                record.min_score,
                record.retrieval or defaults(),
                settings=settings,
            )
        except Exception as exc:  # noqa: BLE001 - one failing case does not stop the others
            results.append(
                {
                    "id": case["id"],
                    "passed": False,
                    "found": [],
                    "missing": list(case["expected"]),
                    "top": [],
                    "error": str(exc) or type(exc).__name__,
                }
            )
            continue
        top = list(dict.fromkeys(hit.document_name for hit in hits))
        found = [name for name in case["expected"] if name in top]
        missing = [name for name in case["expected"] if name not in top]
        results.append(
            {
                "id": case["id"],
                "passed": not missing,
                "found": found,
                "missing": missing,
                "top": top,
                "error": None,
            }
        )
    passed = sum(result["passed"] for result in results)
    return {
        "at": datetime.now(UTC).isoformat(),
        "by": by,
        "passed": passed,
        "total": len(results),
        "score": passed / len(results) if results else None,
        "results": results,
    }


DOC_TEMPLATES: list[dict[str, str]] = [
    {
        "id": "metric",
        "title": "指标口径",
        "filename": "指标口径-{名称}.md",
        "body": """# {名称}

## 定义
一句话说明这个指标衡量什么。

## 计算公式
例如：{名称} = 分子 / 分母。写清分子、分母各自的取数逻辑。

## 统计粒度
按天 / 周 / 月；按门店 / 区域 / 产品线……

## 时间口径
以下单时间 / 支付时间 / 发货时间为准；时区；是否含当天。

## 包含和排除
- 包含：
- 排除：测试订单、内部订单、已取消订单……

## 数据来源
表名（catalog.schema.table）和关键字段。

## 负责人
业务负责人：
数据负责人：

## 生效日期
从 YYYY-MM-DD 起按本口径统计。
""",
    },
    {
        "id": "table",
        "title": "表说明",
        "filename": "表说明-{名称}.md",
        "body": """# {名称}

## 用途
这张表回答什么问题，谁在用。

## 粒度
一行代表什么（例如：一个订单的一个商品）。

## 主键
字段名；是否全局唯一。

## 字段说明
| 字段 | 类型 | 含义 | 取值或示例 |
| --- | --- | --- | --- |
|  |  |  |  |

## 更新频率
每天几点更新；全量还是增量；延迟多久。

## 上游
来源系统或上游表；生成这张表的作业。
""",
    },
    {
        "id": "rule",
        "title": "业务规则",
        "filename": "业务规则-{名称}.md",
        "body": """# {名称}

## 规则
用一两句话写清规则本身。

## 适用范围
适用于哪些业务、地区、客户或产品。

## 例外
哪些情况不适用，怎么处理。

## 依据
制度文件、会议纪要或审批单号。

## 生效日期
从 YYYY-MM-DD 起执行。
""",
    },
]
