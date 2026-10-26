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
exec litellm --config "$config" --port 4000 "$@"
