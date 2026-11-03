"""The statements each tool builds and how it reads their results (fake executor)."""

import pytest

from aep_databricks.executor import Result
from aep_databricks.reader import Reader
from aep_databricks.sql import ConnectorError
from tests.conftest import FakeExecutor, columns_result, settings

ORDERS = columns_result(
    ("order_id", "bigint"),
    ("city", "string"),
    ("amount", "decimal(18,2)"),
    ("tags", "array<string>"),
    key=("order_id",),
)


def reader(respond=None, **changes) -> tuple[Reader, FakeExecutor]:
    fake = FakeExecutor(respond)
    return Reader(settings(**changes), fake), fake


async def test_schemas_come_from_every_allowed_catalog():
    found, fake = reader(
        lambda s: Result(
            [("catalog_name", "string"), ("schema_name", "string")],
            [["main", "sales"], ["prev", "sales"]],
        )
    )
    assert await found.list_schemas() == {"schemas": ["main.sales", "prev.sales"]}
    [statement] = fake.statements
    assert "FROM `main`.information_schema.schemata" in statement
    assert "UNION ALL SELECT catalog_name, schema_name FROM `prev`.information_schema" in statement


async def test_catalogs_outside_the_list_are_refused_before_any_statement():
    found, fake = reader()
    with pytest.raises(ConnectorError, match="没有允许读 catalog other"):
        await found.list_tables("other.sales")
    with pytest.raises(ConnectorError, match="没有允许读 catalog other"):
        await found.compare("main.s.t", "other.s.t", ["id"])
    nothing, _ = reader(databricks_read_catalogs=[])
    with pytest.raises(ConnectorError, match="AEP_DATABRICKS_READ_CATALOGS"):
        await nothing.list_schemas()
    assert fake.statements == []


async def test_tables_are_listed_case_insensitively():
    found, fake = reader(lambda s: Result([("table_name", "string")], [["orders"]]))
    assert await found.list_tables("Main.Sales") == {"schema": "Main.Sales", "tables": ["orders"]}
    assert "WHERE lower(table_schema) = 'sales'" in fake.statements[0]


async def test_describe_reads_columns_comments_and_keys():
    def respond(statement: str) -> Result:
        if "FOREIGN KEY" in statement:
            return Result(
                [
                    ("name", "string"),
                    ("column_name", "string"),
                    ("ref_catalog", "string"),
                    ("ref_schema", "string"),
                    ("ref_table", "string"),
                    ("ref_column", "string"),
                ],
                [["fk_city", "city", "main", "geo", "cities", "name"]],
            )
        return ORDERS

    found, fake = reader(respond)
    described = await found.describe_table("main.sales", "orders")
    assert described["comment"] == "说明"
    assert described["columns"][0] == {
        "name": "order_id",
        "data_type": "bigint",
        "nullable": True,
        "primary_key": True,
        "comment": None,
    }
    assert described["foreign_keys"] == [
        {
            "name": "fk_city",
            "columns": ["city"],
            "referenced_schema": "main.geo",
            "referenced_table": "cities",
            "referenced_columns": ["name"],
        }
    ]
    assert "lower(c.table_name) = 'orders'" in fake.statements[0]

    missing, _ = reader(lambda s: columns_result())
    with pytest.raises(ConnectorError, match="表 main.sales.nope 不存在，或者没有读权限"):
        await missing.describe_table("main.sales", "nope")


async def test_sample_rows_reads_with_the_condition():
    found, fake = reader(lambda s: Result([("city", "string")], [["上海"]]))
    rows = await found.sample_rows("main.sales", "orders", 5, "Type = 'Vol.'")
    assert rows == {"schema": "main.sales", "table": "orders", "rows": [{"city": "上海"}]}
    assert fake.statements == [
        "SELECT * FROM `main`.`sales`.`orders` WHERE (Type = 'Vol.') LIMIT 5"
    ]
    with pytest.raises(ConnectorError, match="^读取条件写法不对：不能有分号"):
        await found.sample_rows("main.sales", "orders", 5, "1 = 1; DROP TABLE x")
    assert len(fake.statements) == 1


async def test_read_rows_pages_by_the_key_or_all_columns():
    def respond(statement: str) -> Result:
        if "information_schema" in statement:
            return ORDERS
        return Result([("order_id", "bigint")], [[1], [2], [3]])

    found, fake = reader(respond)
    page = await found.read_rows("main.sales", "orders", offset=4, limit=2)
    assert page["rows"] == [{"order_id": 1}, {"order_id": 2}]
    assert page["next_offset"] == 6
    assert page["columns"] == ["order_id", "city", "amount", "tags"]
    assert fake.statements[-1].endswith("ORDER BY `order_id` LIMIT 3 OFFSET 4")

    keyless = columns_result(("city", "string"), ("tags", "array<string>"))
    found, fake = reader(lambda s: keyless if "information_schema" in s else Result([], []))
    page = await found.read_rows("main.sales", "orders", where="city <> 'x'")
    assert page["next_offset"] is None
    assert fake.statements[-1] == (
        "SELECT * FROM `main`.`sales`.`orders` WHERE (city <> 'x') "
        "ORDER BY `city`, to_json(`tags`) LIMIT 1001 OFFSET 0"
    )


STATS = [("__aep_column", "int"), ("distinct_count", "bigint"), ("nulls", "bigint")]
STATS += [("total", "bigint"), ("top", "array<struct>"), ("row_count", "bigint")]


async def test_table_stats_counts_one_sample_in_one_statement():
    def respond(statement: str) -> Result:
        if "information_schema" in statement:
            return ORDERS
        top = [
            {"value": "北京", "truncated": False, "count": 2},
            {"value": "上海", "truncated": False, "count": 5},
            {"value": "x" * 80, "truncated": True, "count": 2},
        ]
        return Result(
            STATS,
            [
                [0, 10, 0, 10, None, 1234],
                [1, 3, 1, 10, top, 1234],
                [2, 10, 0, 10, None, 1234],
                [3, 0, 10, 10, [], 1234],
            ],
        )

    found, fake = reader(respond)
    stats = await found.table_stats("main.sales", "orders", "amount > 0")
    assert stats["estimated_row_count"] == 1234
    assert (stats["sample_limit"], stats["sample_row_count"]) == (1000, 10)
    city = stats["columns"][1]
    assert city["null_fraction"] == 0.1 and city["distinct_count"] == 3
    assert city["top_values"] == [
        {"value": "上海", "count": 5},
        {"value": "x" * 80, "count": 2, "truncated": True},
        {"value": "北京", "count": 2},
    ]
    assert "top_values" not in stats["columns"][0]  # Too many distinct values.
    assert stats["columns"][3] == {"name": "tags", "null_fraction": 1.0, "distinct_count": 0}
    statement = fake.statements[-1]
    assert statement.count("WHERE (amount > 0)") == 2
    assert "LIMIT 1000" in statement
    assert "stack(4, 0, CAST(`order_id` AS STRING), 1, CAST(`city` AS STRING)" in statement

    empty, _ = reader(lambda s: ORDERS if "information_schema" in s else Result(STATS, []))
    stats = await empty.table_stats("main.sales", "orders")
    assert (stats["estimated_row_count"], stats["sample_row_count"]) == (0, 0)
    assert stats["columns"][0] == {"name": "order_id", "null_fraction": None, "distinct_count": 0}


async def test_aggregate_with_and_without_groups():
    found, fake = reader(lambda s: Result([("value", "decimal(38,2)")], [[99.5]]))
    assert await found.aggregate("main.sales.orders", "sum(amount)") == {
        "rows": [{"value": 99.5}],
        "groups": 1,
    }
    assert fake.statements[-1] == "SELECT (sum(amount)) AS value FROM `main`.`sales`.`orders`"

    grouped = Result(
        [("city", "string"), ("__aep_value", "bigint"), ("__aep_groups", "bigint")],
        [["上海", 5, 3], ["北京", 2, 3]],
    )
    found, fake = reader(lambda s: grouped)
    result = await found.aggregate("main.sales.orders", "count(*)", "amount > 0", ["city"], limit=2)
    assert result == {
        "rows": [{"city": "上海", "value": 5}, {"city": "北京", "value": 2}],
        "groups": 3,
    }
    assert fake.statements[-1] == (
        "SELECT *, count(*) OVER () AS __aep_groups FROM (SELECT `city`, (count(*)) AS "
        "__aep_value FROM `main`.`sales`.`orders` WHERE (amount > 0) GROUP BY `city`) "
        "ORDER BY `city` LIMIT 2"
    )
    with pytest.raises(ConnectorError, match="^聚合表达式写法不对：不能有 SELECT"):
        await found.aggregate("main.sales.orders", "(SELECT max(x) FROM other)")
    with pytest.raises(ConnectorError, match="分组列不能叫 value"):
        await found.aggregate("main.sales.orders", "count(*)", group_by=["Value"])


ALLOC = "main.ref.allocation"
NOT_APPLICABLE = {
    "table": ALLOC,
    "on": {"platform": "platform", "efficiency_type": "eff_type", "lane": "lane"},
    "where": "array_contains(split(months, ','), '07')",
    "found": False,
}


async def test_a_lookup_keeps_the_rows_with_or_without_a_match_in_another_table():
    found, fake = reader(lambda s: Result([("value", "bigint")], [[2]]))
    counted = await found.aggregate(
        "main.out.lanes", "count(*)", "NOT (cw_07 <=> 0)", lookup=NOT_APPLICABLE
    )
    assert counted == {"rows": [{"value": 2}], "groups": 1}
    assert fake.statements[-1] == (
        "SELECT (count(*)) AS value FROM `main`.`out`.`lanes` AS __aep_t "
        "WHERE (NOT (cw_07 <=> 0)) AND NOT EXISTS (SELECT 1 FROM `main`.`ref`.`allocation` "
        "AS __aep_l WHERE __aep_l.`platform` <=> __aep_t.`platform` AND "
        "__aep_l.`eff_type` <=> __aep_t.`efficiency_type` AND __aep_l.`lane` <=> "
        "__aep_t.`lane` AND (array_contains(split(months, ','), '07')))"
    )
    found, fake = reader(lambda s: Result([("lane", "string")], [["SEA-QY2"]]))
    sampled = await found.sample_rows(
        "main.out", "lanes", 5, lookup={"table": ALLOC, "on": {"lane": "lane"}, "found": True}
    )
    assert sampled["rows"] == [{"lane": "SEA-QY2"}]
    assert fake.statements[-1] == (
        "SELECT * FROM `main`.`out`.`lanes` AS __aep_t WHERE EXISTS (SELECT 1 FROM "
        "`main`.`ref`.`allocation` AS __aep_l WHERE __aep_l.`lane` <=> __aep_t.`lane`) LIMIT 5"
    )
    grouped = Result(
        [("lane", "string"), ("__aep_value", "bigint"), ("__aep_groups", "bigint")],
        [["SEA-QY2", 1, 1]],
    )
    found, fake = reader(lambda s: grouped)
    result = await found.aggregate(
        "main.out.lanes", "count(*)", group_by=["lane"], lookup=NOT_APPLICABLE
    )
    assert result == {"rows": [{"lane": "SEA-QY2", "value": 1}], "groups": 1}
    assert "FROM `main`.`out`.`lanes` AS __aep_t WHERE NOT EXISTS" in fake.statements[-1]
    # trim: both sides of each pair without leading and trailing spaces (jadeflow C8).
    trimmed = {"table": ALLOC, "on": {"lane": "lane"}, "found": True, "trim": True}
    await found.aggregate("main.out.lanes", "count(*)", lookup=trimmed)
    assert "WHERE trim(__aep_l.`lane`) <=> trim(__aep_t.`lane`))" in fake.statements[-1]

    for bad, message in [
        ({**NOT_APPLICABLE, "table": "other.ref.allocation"}, "没有允许读 catalog other"),
        ({**NOT_APPLICABLE, "on": {}}, "lookup.on 至少要有一对列"),
        ({**NOT_APPLICABLE, "on": {"a`b": "lane"}}, "列名"),
        ({**NOT_APPLICABLE, "where": "1 = 1; DROP TABLE x"}, "^lookup.where 写法不对：不能有分号"),
        ({**NOT_APPLICABLE, "where": "lane IN (SELECT lane FROM x)"}, "不能有 SELECT"),
        ({k: v for k, v in NOT_APPLICABLE.items() if k != "found"}, "lookup.found 要是 true"),
        ({**NOT_APPLICABLE, "trim": "yes"}, "lookup.trim 要是 true"),
    ]:
        with pytest.raises(ConnectorError, match=message):
            await found.aggregate("main.out.lanes", "count(*)", lookup=bad)
    assert len(fake.statements) == 2


async def test_duplicates_lists_repeated_groups():
    found, fake = reader(
        lambda s: Result(
            [("sku", "string"), ("__aep_count", "bigint"), ("__aep_groups", "bigint")],
            [["A", 3, 2], ["B", 2, 2]],
        )
    )
    assert await found.duplicates("main.sales.orders", ["sku"], limit=5) == {
        "groups": 2,
        "rows": [{"sku": "A", "count": 3}, {"sku": "B", "count": 2}],
    }
    assert "HAVING count(*) > 1" in fake.statements[-1]
    empty, _ = reader(lambda s: Result([], []))
    assert await empty.duplicates("main.sales.orders", ["sku"]) == {"groups": 0, "rows": []}


FORECAST = columns_result(
    ("sku", "string"), ("month", "string"), ("qty", "double"), ("note", "string")
)
GOLDEN = columns_result(("SKU", "string"), ("Month", "string"), ("Qty", "decimal(18,2)"))


def golden(respond_diff):
    def respond(statement: str) -> Result:
        if "information_schema" in statement:
            return GOLDEN if "'golden'" in statement else FORECAST
        if "__aep_side" in statement:
            return Result([], [])
        return respond_diff(statement)

    return respond


async def test_compare_stops_at_repeated_keys():
    def respond(statement: str) -> Result:
        if "information_schema" in statement:
            return GOLDEN if "'golden'" in statement else FORECAST
        return Result(
            [("__aep_side", "string"), ("sku", "string"), ("month", "string")]
            + [("__aep_count", "bigint"), ("__aep_t", "bigint"), ("__aep_e", "bigint")],
            [["expected", "A", "2026-09", 2, 0, 1]],
        )

    found, fake = reader(respond)
    result = await found.compare(
        "main.s.forecast",
        "prev.s.golden",
        ["sku", "month"],
        where="1 = 1",
        expected_where="Type = 'Vol.'",
    )
    assert result == {
        "duplicates": {"table": 0, "expected": 1},
        "missing": None,
        "extra": None,
        "different": None,
        "columns": ["qty"],
        "rows": [
            {
                "kind": "duplicate",
                "side": "expected",
                "key": {"sku": "A", "month": "2026-09"},
                "count": 2,
            }
        ],
    }
    keys = fake.statements[-1]
    assert "FROM `main`.`s`.`forecast` WHERE (1 = 1) GROUP BY `sku`, `month`" in keys
    assert "FROM `prev`.`s`.`golden` WHERE (Type = 'Vol.') GROUP BY `SKU`, `Month`" in keys


async def test_compare_counts_missing_extra_and_different_rows():
    names = [("__aep_kind", "string"), ("__aep_k0", "string"), ("__aep_a0", "double")]
    names += [("__aep_b0", "decimal(18,2)"), ("__aep_missing", "bigint")]
    names += [("__aep_extra", "bigint"), ("__aep_different", "bigint")]
    found, fake = reader(
        golden(
            lambda s: Result(
                names,
                [
                    ["different", "A", 10.0, 12.5, 1, 0, 4],
                    ["missing", "B", None, 3.0, 1, 0, 4],
                ],
            )
        )
    )
    result = await found.compare(
        "main.s.forecast", "prev.s.golden", ["sku"], ["qty"], tolerance=0.5
    )
    assert result["duplicates"] == {"table": 0, "expected": 0}
    assert (result["missing"], result["extra"], result["different"]) == (1, 0, 4)
    assert result["columns"] == ["qty"]
    assert result["rows"] == [
        {
            "kind": "different",
            "key": {"sku": "A"},
            "table": {"qty": 10.0},
            "expected": {"qty": 12.5},
            "differs": ["qty"],
        },
        {"kind": "missing", "key": {"sku": "B"}, "table": None, "expected": {"qty": 3.0}},
    ]
    joined = fake.statements[-1]
    assert "FULL OUTER JOIN b ON a.`sku` <=> b.`SKU`" in joined
    # Null on one side only is a difference, also for numbers within the tolerance.
    assert "(a.`qty` <=> b.`Qty` OR coalesce(abs(a.`qty` - b.`Qty`) <= 0.5, false))" in joined

    same, _ = reader(golden(lambda s: Result(names, [])))
    result = await same.compare("main.s.forecast", "prev.s.golden", ["sku", "month"])
    assert (result["missing"], result["extra"], result["different"]) == (0, 0, 0)
    assert result["columns"] == ["qty"]  # The columns both tables have, keys left out.


async def test_compare_needs_the_columns_on_both_sides():
    found, fake = reader(golden(lambda s: Result([], [])))
    with pytest.raises(ConnectorError, match="prev.s.golden 没有列 note"):
        await found.compare("main.s.forecast", "prev.s.golden", ["sku"], ["note"])
    with pytest.raises(ConnectorError, match="main.s.forecast 没有列 region"):
        await found.compare("main.s.forecast", "prev.s.golden", ["region"])
    with pytest.raises(ConnectorError, match="^expected_where 写法不对"):
        await found.compare("main.s.forecast", "prev.s.golden", ["sku"], expected_where="a;b")
