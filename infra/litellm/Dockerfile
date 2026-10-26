FROM python:3.12-slim

# Pinned so local, CI and Azure run the same gateway build. langfuse is the SDK
# LiteLLM's Langfuse callback needs (it is not part of the proxy extra).
RUN pip install --no-cache-dir "litellm[proxy]==1.106.4" "langfuse>=4.7,<5"

COPY config.yaml langfuse.yaml entrypoint.sh /app/
EXPOSE 4000
ENTRYPOINT ["/app/entrypoint.sh"]
