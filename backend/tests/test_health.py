import asyncio

import pytest
from fastapi.testclient import TestClient

from aep import health
from aep.config import Settings
from aep.main import app

client = TestClient(app)

CHECKS = ("check_postgres", "check_redis", "check_llm_gateway")


async def ok(_settings):
    return None


async def down(_settings):
    return "connection refused"


def test_healthz():
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_readyz_all_ok(monkeypatch):
    for name in CHECKS:
        monkeypatch.setattr(f"aep.main.{name}", ok)
    resp = client.get("/readyz")
    assert resp.status_code == 200
    assert resp.json()["checks"] == {"postgres": "ok", "redis": "ok", "llm_gateway": "ok"}


@pytest.mark.parametrize(
    ("failing", "service"),
    [("check_postgres", "postgres"), ("check_llm_gateway", "llm_gateway")],
)
def test_readyz_reports_failed_service(monkeypatch, failing, service):
    for name in CHECKS:
        monkeypatch.setattr(f"aep.main.{name}", down if name == failing else ok)
    resp = client.get("/readyz")
    assert resp.status_code == 503
    body = resp.json()
    assert body["checks"][service] == "error"
    assert body["errors"] == {service: "connection refused"}


def test_checks_return_error_when_services_unreachable():
    settings = Settings(
        database_url="postgresql://aep:aep@127.0.0.1:1/aep",
        redis_url="redis://127.0.0.1:1/0",
        llm_gateway_url="http://127.0.0.1:1",
    )
    assert asyncio.run(health.check_postgres(settings)) is not None
    assert asyncio.run(health.check_redis(settings)) is not None
    assert asyncio.run(health.check_llm_gateway(settings)) is not None
