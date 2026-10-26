FROM python:3.12-slim

# Pinned so local, CI and Azure run the same gateway build.
RUN pip install --no-cache-dir "litellm[proxy]==1.106.4"

COPY config.yaml /app/config.yaml
EXPOSE 4000
CMD ["litellm", "--config", "/app/config.yaml", "--port", "4000"]
