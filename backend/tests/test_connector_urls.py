"""The rules for where connectors may be registered (docs/api/datasources.md, 地址检查)."""

import pytest
from pydantic import ValidationError

from aep.config import Settings
from aep.connector_urls import (
    TEXT,
    HostRule,
    Rejected,
    check_url,
    internal_targets,
    mask_url,
    parse_rule,
    same_url,
)

SETTINGS = Settings(
    connector_hosts=["connector.test", "*.connector.test:*", "127.0.0.1:1"],
    files_connector_url="http://files.test:8003",
    default_datasources=[
        {"name": "演示库", "kind": "mysql", "connector_url": "http://demo.test:8002/mcp"}
    ],
)


def rejected(url: str, s: Settings = SETTINGS) -> Rejected:
    with pytest.raises(Rejected) as caught:
        check_url(url, s)
    return caught.value


def test_rules_parse():
    assert parse_rule("connector-postgres:8001") == HostRule("connector-postgres", 8001)
    assert parse_rule(" Connector-Postgres:* ") == HostRule("connector-postgres", any_port=True)
    assert parse_rule("connector-postgres") == HostRule("connector-postgres")
    assert parse_rule("[FD00::5]:8001") == HostRule("fd00::5", 8001)
    assert parse_rule("*.x.local:*") == HostRule("x.local", any_port=True, suffix=True)
    assert parse_rule("*.x.local:9") == HostRule("x.local", 9, suffix=True)
    for bad in ("*", "*:8001", "", "host:0", "host:70000", "host:x", "fd00::5", "a*b:1", "[x]:1"):
        with pytest.raises(ValueError):
            parse_rule(bad)
    with pytest.raises(ValidationError, match="写法不对"):
        Settings(connector_hosts=["*"])
    assert Settings().connector_hosts == [
        "connector-postgres:8001",
        "connector-mysql:8002",
        "connector-files:8003",
    ]


def test_allowlist():
    assert check_url("http://connector.test/mcp", SETTINGS) == "http://connector.test/mcp"
    # A bare rule is the scheme's default port.
    assert check_url("https://connector.test/mcp", SETTINGS) == "https://connector.test/mcp"
    assert "connector.test:8080" in rejected("http://connector.test:8080/mcp").message
    assert check_url("http://a.connector.test:9/mcp", SETTINGS) == "http://a.connector.test:9/mcp"
    for url in ("http://evil-connector.test/mcp", "http://connector.test.evil/mcp"):
        assert rejected(url).code == "host_not_allowed"
    # The files connector and the default sources need no rule.
    assert check_url("http://files.test:8003/datasets/x/mcp", SETTINGS)
    assert check_url("http://demo.test:8002/mcp", SETTINGS)
    assert rejected("http://files.test:8004/mcp").code == "host_not_allowed"
    elsewhere = rejected("http://elsewhere.example/mcp")
    assert elsewhere.code == "host_not_allowed"
    assert elsewhere.message == TEXT["host_not_allowed"].format(host="elsewhere.example:80")
    assert "AEP_CONNECTOR_HOSTS" in elsewhere.message
    # Saved normalised: scheme and host in lowercase, no default port, path and query as written.
    normalised = check_url(" HTTP://Connector.Test:80/MCP?schema=Sales ", SETTINGS)
    assert normalised == "http://connector.test/MCP?schema=Sales"
    v6 = Settings(connector_hosts=["[fd00::5]:8001"])
    assert check_url("http://[FD00:0::5]:8001/mcp", v6) == "http://[fd00::5]:8001/mcp"
    assert "[fd00::6]:8001" in rejected("http://[fd00::6]:8001/mcp", v6).message


def test_loopback_needs_its_host_and_port_written():
    assert check_url("http://127.0.0.1:1/mcp", SETTINGS) == "http://127.0.0.1:1/mcp"
    wildcards = Settings(connector_hosts=["localhost:*", "127.0.0.1", "*.localhost:*"])
    for url in ("http://localhost:8001/mcp", "http://127.0.0.1/mcp", "http://a.localhost:1/"):
        assert rejected(url, wildcards).code == "host_not_allowed"


@pytest.mark.parametrize(
    "url",
    [
        "http://u:p@connector.test/mcp",
        "http://u@connector.test/mcp",
        "http://@connector.test/mcp",
        "http://connector.test/mcp?token=x",
        "http://connector.test/mcp?api_key=x",
        "http://connector.test/mcp?Password=",
        "http://connector.test/mcp?client_secret=1",
        "http://connector.test/mcp?x-api-key=1",
        "http://connector.test/mcp?access_token",
        "http://connector.test/mcp?signature=1",
        "http://connector.test/mcp?a=1;auth=2",
        "http://connector.test/mcp?%74oken=1",
        "http://connector.test/mcp?sig=1",
        "http://connector.test/mcp?code=1",
        "http://connector.test/mcp?pwd=1",
        "http://connector.test/mcp?credential=1",
    ],
)
def test_credentials_rejected(url):
    found = rejected(url)
    assert found.code == "credentials"
    assert found.message == TEXT["credentials"]
    assert "数据库账号密码配在连接器上，平台只保存地址。" in found.message


def test_harmless_parameters_pass():
    url = "http://connector.test/mcp?schema=sales&mode=ro;db=x"
    assert check_url(url, SETTINGS) == url


def test_internal_hosts_rejected_even_if_allowed():
    hosts = [
        "sandbox-relay:*",
        "connector-sandbox:*",
        "litellm:*",
        "langfuse-web:*",
        "postgres:*",
        "redis:*",
        "source-mysql:*",
        "source-postgres:*",
        "api:*",
        "web:*",
        "127.0.0.1:4000",
        "127.0.0.1:1",
        "localhost:3100",
        "db-b:*",
        "aep.example.com",
    ]
    s = Settings(
        connector_hosts=hosts,
        llm_gateway_url="http://localhost:4000",
        web_url="http://aep.example.com",
        database_url="postgresql://aep:aep@db-a:5432,db-b:5433/aep",
    )
    for url in (
        "http://sandbox-relay:8004/mcp",
        "http://connector-sandbox:8004/mcp",
        "http://litellm:4000/mcp",
        "http://langfuse-web:3000/mcp",
        "http://postgres:5432/mcp",
        "http://redis:6379/mcp",
        "http://source-mysql:3306/mcp",
        "http://source-postgres:5432/mcp",
        "http://api:8000/mcp",
        "http://web:3000/mcp",
        "http://127.0.0.1:4000/mcp",  # the gateway, as localhost:4000
        "http://[::1]:4000/mcp",
        "http://localhost:3100/mcp",  # Langfuse
        "http://db-b:8001/mcp",  # every host of a multi-host database URL
        "http://aep.example.com/mcp",  # the web app
    ):
        found = rejected(url, s)
        assert found.code == "internal_host", url
        assert found.message == TEXT["internal_host"]
    # A loopback service only takes its own port.
    assert check_url("http://127.0.0.1:1/mcp", s) == "http://127.0.0.1:1/mcp"
    targets = internal_targets(s)
    assert ("127.0.0.1", 4000) not in targets and ("localhost", 4000) in targets
    assert ("db-a", None) in targets and ("db-b", None) in targets


def test_cluster_names_of_internal_services_rejected():
    # In Kubernetes a Service also answers as <name>.<namespace>, <name>.<namespace>.svc and
    # <name>.<namespace>.svc.cluster.local: a suffix rule over the platform's namespace
    # must not let the internal services in.
    s = Settings(
        connector_hosts=["*.aep.svc.cluster.local:*", "*.aep.svc:*", "*.aep:*", "cache:*"],
        sandbox_url="http://sandbox-relay:8004/mcp",
        llm_gateway_url="http://litellm:4000",
        database_url="postgresql://aep:aep@aep-db:5432/aep",
        redis_url="redis://cache.aep.svc.cluster.local:6379/0",
    )
    for url in (
        "http://sandbox-relay.aep.svc.cluster.local:8004/mcp",
        "http://sandbox-relay.aep.svc:8004/mcp",
        "http://sandbox-relay.aep:8004/mcp",
        "http://litellm.aep.svc.cluster.local:4000/",
        "http://postgres.aep.svc.cluster.local:5432/",
        "http://api.aep.svc.cluster.local:8000/",
        "http://web.aep.svc.cluster.local:3000/",
        "http://langfuse-web.aep.svc.cluster.local:3000/",
        "http://aep-db.aep.svc.cluster.local:5432/",  # the database URL's host
        "http://cache.aep:6379/",  # the Redis URL names the Service by its long name
        "http://cache:6379/",
    ):
        assert rejected(url, s).code == "internal_host", url
    tickets = "http://connector-tickets.aep.svc.cluster.local:8010/mcp"
    assert check_url(tickets, s) == tickets


def test_internal_services_configured_with_a_trailing_dot_are_still_denied():
    s = Settings(
        connector_hosts=["*.aep.svc.cluster.local:*"],
        sandbox_url="http://aep-sandbox-relay.aep.svc.cluster.local.:8004/mcp",
        llm_gateway_url="http://aep-litellm.aep.svc.cluster.local.:4000",
    )
    for url in (
        "http://aep-sandbox-relay.aep.svc.cluster.local:8004/mcp",
        "http://aep-litellm.aep.svc.cluster.local:4000/",
        "http://aep-litellm.aep:4000/",
    ):
        assert rejected(url, s).code == "internal_host", url


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data",
        "http://[::ffff:169.254.169.254]/",
        "http://2852039166/",
        "http://0xa9fea9fe/",
        "http://0251.0376.0251.0376/",
        "http://169.254.43518/",
        "http://[fd00:ec2::254]/",
        "http://[fe80::1]:8001/",
        "http://168.63.129.16/",
        "http://100.100.100.200/",
    ],
)
def test_metadata_rejected(url):
    found = rejected(url)
    assert found.code == "metadata"
    assert found.message == TEXT["metadata"]


@pytest.mark.parametrize(
    "url",
    [
        "ftp://connector.test/mcp",
        "connector:9000",
        "connector.test/mcp",
        "http://connector.test/mcp#frag",
        "http://connector.test/\tmcp",
        "http://connector.test/m cp",
        "http://connector.test:99999/mcp",
        "http://connector.test:0/mcp",
        "http://connector.test/" + "a" * 2048,
        "http://0.0.0.0:1/",
        "http://[::]/",
        "http://224.0.0.1/",
        "http://255.255.255.255/",
        "http://127.1:1/",  # 127.0.0.1 written another way
        "http://例子.connector.test/mcp",
        "http://a..connector.test/mcp",
        "http://connector.test\\@evil.test/mcp",
        "http:///mcp",
        "",
        "postgres:sales",  # not a database address: that has ://
    ],
)
def test_bad_url(url):
    found = rejected(url)
    assert found.code == "bad_url"
    assert found.message == TEXT["bad_url"]
    assert "http://connector-postgres:8001/mcp" in found.message


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://reader:secret@db:5432/sales",
        "postgres://db/sales",
        "MySQL://db:3306/sales",
        "jdbc:sqlserver://db:1433;databaseName=sales",
        "mongodb+srv://cluster.example/sales",
        "oracle://db:1521/orcl",
        # With a driver, as SQLAlchemy and Spark write them.
        "postgresql+psycopg2://reader:secret@db/sales",
        "mysql+pymysql://db/sales",
        "MSSQL+pyodbc://db/sales",
        "oracle+oracledb://db:1521/orcl",
        "mariadb+mariadbconnector://db/sales",
    ],
)
def test_database_addresses_get_their_own_message(url):
    found = rejected(url)
    assert found.code == "bad_url"
    assert found.message == TEXT["database_url"]
    assert found.message.startswith("这是数据库的地址。")


def test_mask_and_same_url():
    assert mask_url("http://u:p@h/mcp?token=a&x=1") == "http://***@h/mcp?token=***&x=1"
    assert mask_url("http://h/mcp?a=1;Password=2#access_token=3") == (
        "http://h/mcp?a=1;Password=***#access_token=***"
    )
    assert mask_url("http://h/mcp?%74oken=a") == "http://h/mcp?%74oken=***"
    assert mask_url("http://connector.test/mcp") == "http://connector.test/mcp"
    # Stored text that does not parse is masked as well as it can be, never an error.
    assert mask_url("http://[::1/mcp") == "http://[::1/mcp"
    assert mask_url("http://u:p@h:99999/mcp?token=a") == "http://***@h:99999/mcp?token=***"
    assert mask_url("u:p@h") == "***@h"
    # Passwords stored before addresses were checked may hold / ? or # unencoded.
    for url in ("http://u:pa/ss@h/mcp", "http://u:pa?ss@h/mcp", "http://u:Pa/ss#1@h/mcp"):
        assert mask_url(url) == "http://***@h/mcp", url
    assert mask_url("http://h;password=Secret1/mcp") == "http://h;password=***/mcp"
    assert mask_url("") == ""
    assert same_url("HTTP://Connector.test:80/mcp/", "http://connector.test/mcp")
    assert same_url("https://[FD00::5]:443/mcp", "https://[fd00::5]/mcp")
    assert not same_url("http://connector.test/mcp", "http://connector.test:8080/mcp")
    assert not same_url("http://u:p@h/mcp", "http://h/mcp")
    assert not same_url("http://[::1/mcp", "http://x/mcp")
    assert not same_url("http://h:99999/mcp", "http://h/mcp")
    assert same_url("http://[::1/mcp", "http://[::1/mcp")
