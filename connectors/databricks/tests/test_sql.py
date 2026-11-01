import pytest

from aep_databricks.sql import (
    ConnectorError,
    Schema,
    Table,
    columns,
    expression,
    expression_problem,
    ident,
    literal,
)


def test_names_are_two_or_three_parts_and_quoted():
    assert Schema.parse("icsd-ace-dev.sales") == Schema("icsd-ace-dev", "sales")
    assert Table.parse("main.sales.orders").sql == "`main`.`sales`.`orders`"
    assert Table.of("main.sales", "orders").name == "main.sales.orders"
    for bad in ["sales", "a.b.c", "a..b", ".b", "a.b ", "a`.b", "a.\nb", 3, None]:
        with pytest.raises(ConnectorError):
            Schema.parse(bad)
    for bad in ["a.b", "a.b.c.d", "a.b.`c`"]:
        with pytest.raises(ConnectorError):
            Table.parse(bad)
    assert ident("my col") == "`my col`"
    with pytest.raises(ConnectorError):
        ident("a`b")
    assert columns(["sku", "Month"], "keys ") == ["sku", "Month"]
    with pytest.raises(ConnectorError, match="重复"):
        columns(["sku", "SKU"], "keys ")
    with pytest.raises(ConnectorError, match="至少"):
        columns([], "keys ")


def test_literals_escape_quotes_and_backslashes():
    assert literal("it's") == "'it\\'s'"
    assert literal("a\\'; x") == "'a\\\\\\'; x'"
    with pytest.raises(ConnectorError):
        literal("a\nb")


@pytest.mark.parametrize(
    "text",
    [
        "Type = 'Vol.'",
        "a > 1 AND b IN ('x;y', \"--\", '/* no */')",
        "extract(YEAR FROM order_date) = 2024",
        "trim(BOTH 'x' FROM s) = ''",
        "`weird col; --` IS NOT NULL",
        "name RLIKE r'\\d+' OR note = 'it\\'s'",
        "sum(CASE WHEN status = 'paid' THEN amount ELSE 0 END)",
        "count(DISTINCT sku)",
        "transform(items, x -> x + 1) IS NOT NULL",
    ],
)
def test_expressions_that_pass(text):
    assert expression_problem(text) is None
    assert expression(text, "读取条件") == text


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        ("", "不能为空"),
        ("1 = 1; DROP TABLE x", "不能有分号"),
        ("a = 1 -- c", "不能有注释"),
        ("/* x */ a = 1", "不能有注释"),
        ("a = 'open", "引号要配对"),
        ("`open = 1", "反引号要配对"),
        ("(a = 1", "括号要配对"),
        ("a = 1)", "括号要配对"),
        # Spark ends this string after the escaped quote: the rest is outside it.
        ("a = '\\'' ; DROP TABLE x --'", "不能有分号"),
        ("a = '\\\\' ; x", "不能有分号"),
        ("a IN (SELECT id FROM other)", "SELECT"),
        ("x IN (TABLE other.s.t)", "TABLE"),
        ("EXISTS (FROM other.s.t)", "FROM"),
        ("reflect('java.lang.Runtime', 'getRuntime') IS NULL", "函数 reflect"),
        ("`java_method`('a', 'b') IS NULL", "函数 java_method"),
        ("secret('scope', 'key') = 'x'", "函数 secret"),
        ("ai_query('model', note) = 'yes'", "函数 ai_query"),
        ("IDENTIFIER('reflect')('a', 'b') IS NULL", "函数 IDENTIFIER"),
        ("main.default.my_udf(x) = 1", "带 catalog 或 schema 的函数"),
        ("x" * 2001, "最多 2000 个字符"),
    ],
)
def test_expressions_that_fail(text, problem):
    found = expression_problem(text)
    assert found is not None and problem in found
    with pytest.raises(ConnectorError, match="^读取条件写法不对："):
        expression(text, "读取条件")
