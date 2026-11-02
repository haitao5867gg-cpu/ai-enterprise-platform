"""Create the gateway's database (DATABASE_URL) when it does not exist yet.

The gateway shares the platform's Postgres server but keeps its tables in a database of
its own: its migrations only work in the public schema. The platform's database volume
may predate the gateway's database, so it is created here rather than by an init script.
"""

import os
import sys
import time
from urllib.parse import urlsplit, urlunsplit

import psycopg
from psycopg import sql

url = os.environ.get("DATABASE_URL", "")
parts = urlsplit(url)
name = parts.path.lstrip("/")
if not name:
    sys.exit(0)
admin = urlunsplit(parts._replace(path="/postgres", query=""))

for attempt in range(30):
    try:
        with psycopg.connect(admin, autocommit=True, connect_timeout=5) as conn:
            found = conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,))
            if found.fetchone() is None:
                conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
                print(f"Created the gateway's database {name}")
        break
    except psycopg.Error as exc:
        if attempt == 29:
            print(f"Could not make sure the gateway's database exists: {exc}", file=sys.stderr)
            sys.exit(1)
        time.sleep(2)
