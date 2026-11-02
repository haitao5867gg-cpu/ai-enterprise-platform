FROM python:3.12-slim

# openssl lets Prisma pick the right query engine for the gateway's database;
# libatomic1 is needed by the Node.js runtime Prisma downloads for its CLI.
RUN apt-get update && apt-get install -y --no-install-recommends openssl libatomic1 \
    && rm -rf /var/lib/apt/lists/*

# Pinned so local, CI and Azure run the same gateway build. langfuse is the SDK
# LiteLLM's Langfuse callback needs (it is not part of the proxy extra). prisma is
# the client LiteLLM keeps models added on the model page in (docs/api/models.md);
# psycopg creates the gateway's database on first start (ensure_db.py).
RUN pip install --no-cache-dir "litellm[proxy]==1.106.4" "langfuse>=4.7,<5" \
    "prisma==0.15.0" "psycopg[binary]>=3.2,<4"
# Generate the Prisma client (and fetch its engines) at build time, not on first start.
RUN cd "$(python -c 'import litellm, os; print(os.path.dirname(litellm.__file__))')/proxy" \
    && prisma generate

COPY config.yaml langfuse.yaml entrypoint.sh ensure_db.py /app/
EXPOSE 4000
ENTRYPOINT ["/app/entrypoint.sh"]
