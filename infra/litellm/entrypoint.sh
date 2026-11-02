#!/bin/sh
# Report to Langfuse only when it is configured. Without keys LiteLLM's
# callback would still try to send data to Langfuse Cloud.
set -e
config=/app/config.yaml
if [ -n "$LANGFUSE_PUBLIC_KEY" ] && [ -n "$LANGFUSE_SECRET_KEY" ]; then
  cat /app/config.yaml /app/langfuse.yaml > /tmp/config.yaml
  config=/tmp/config.yaml
  echo "Reporting model calls to Langfuse at ${LANGFUSE_HOST:-https://cloud.langfuse.com}"
fi
# Models added on the model page live in the gateway's database (docs/api/models.md).
# Without DATABASE_URL the gateway only serves the models in config.yaml.
if [ -n "$DATABASE_URL" ]; then
  python /app/ensure_db.py
else
  echo "No DATABASE_URL: serving config.yaml models only; the model page cannot add models"
fi
exec litellm --config "$config" --port 4000 "$@"
