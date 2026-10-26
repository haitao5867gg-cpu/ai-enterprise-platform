from fastapi.testclient import TestClient

from aep import health
from aep.main import app

client = TestClient(app)


def test_healthz():
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_readyz_all_ok(monkeypatch):
    async def ok(_settings):
        return None

    monkeypatch.setattr("aep.main.check_postgres", ok)
    monkeypatch.setattr("aep.main.check_redis", ok)
    resp = client.get("/readyz")
    assert resp.status_code == 200
    assert resp.json()["checks"] == {"postgres": "ok", "redis": "ok"}


def test_readyz_reports_failed_service(monkeypatch):
    async def ok(_settings):
        return None

    async def down(_settings):
        return "connection refused"

    monkeypatch.setattr("aep.main.check_postgres", down)
    monkeypatch.setattr("aep.main.check_redis", ok)
    resp = client.get("/readyz")
    assert resp.status_code == 503
    body = resp.json()
    assert body["checks"] == {"postgres": "error", "redis": "ok"}
    assert body["errors"] == {"postgres": "connection refused"}


def test_checks_return_error_when_services_unreachable():
    import asyncio

    from aep.config import Settings

    settings = Settings(
        database_url="postgresql://aep:aep@127.0.0.1:1/aep", redis_url="redis://127.0.0.1:1/0"
    )
    assert asyncio.run(health.check_postgres(settings)) is not None
    assert asyncio.run(health.check_redis(settings)) is not None
