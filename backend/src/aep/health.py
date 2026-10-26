"""Liveness and readiness checks for the platform's backing services."""

import psycopg
import redis.asyncio as redis

from aep.config import Settings


async def check_postgres(settings: Settings) -> str | None:
    """Return None when Postgres answers, otherwise the error message."""
    try:
        async with await psycopg.AsyncConnection.connect(
            settings.database_url, connect_timeout=2
        ) as conn:
            await conn.execute("SELECT 1")
    except Exception as exc:  # noqa: BLE001 - any failure means "not ready"
        return str(exc)
    return None


async def check_redis(settings: Settings) -> str | None:
    """Return None when Redis answers, otherwise the error message."""
    client = redis.from_url(settings.redis_url, socket_connect_timeout=2)
    try:
        await client.ping()
    except Exception as exc:  # noqa: BLE001
        return str(exc)
    finally:
        await client.aclose()
    return None
