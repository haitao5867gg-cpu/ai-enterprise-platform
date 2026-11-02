"""Loads the synthetic demo source (seed/*.sql) as the server administrator.

Used by docker compose and CI only; never point it at a customer server. Each file is
split into batches on lines that hold only ``GO``. ``001-source.sql`` is skipped when the
database already exists; the reader file runs every time so a changed password applies.
"""

import argparse
import re
from pathlib import Path

import pymssql
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from aep_sqlserver.config import Settings

BATCH = re.compile(r"^\s*GO\s*$", re.IGNORECASE | re.MULTILINE)


class SeedSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AEP_SOURCE_", env_file=".env", extra="ignore")
    sqlserver_admin_user: str = "sa"
    sqlserver_admin_password: SecretStr = SecretStr("Local-Source-Admin1")


def batches(text: str) -> list[str]:
    return [batch.strip() for batch in BATCH.split(text) if batch.strip()]


def run(seed_dir: Path) -> list[str]:
    settings = Settings()
    admin = SeedSettings()
    password = settings.sqlserver_password.get_secret_value().replace("'", "''")
    done = []
    conn = pymssql.connect(
        server=settings.sqlserver_host,
        port=str(settings.sqlserver_port),
        user=admin.sqlserver_admin_user,
        password=admin.sqlserver_admin_password.get_secret_value(),
        login_timeout=settings.sqlserver_connect_timeout_seconds,
        autocommit=True,
    )
    try:
        cursor = conn.cursor()
        for path in sorted(seed_dir.glob("*.sql")):
            if path.name.startswith("001-"):
                cursor.execute("SELECT DB_ID(%s)", (settings.sqlserver_database,))
                if cursor.fetchone()[0] is not None:
                    continue
            text = path.read_text().replace("$(READER_PASSWORD)", password)
            for batch in batches(text):
                cursor.execute(batch)
            done.append(path.name)
    finally:
        conn.close()
    return done


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("seed_dir", nargs="?", default="seed", type=Path)
    args = parser.parse_args()
    print("ran:", ", ".join(run(args.seed_dir)) or "nothing")


if __name__ == "__main__":
    main()
