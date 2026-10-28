FROM python:3.12-slim

RUN pip install --no-cache-dir "uv>=0.11,<0.12"
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/opt/venv
ENV PATH="/opt/venv/bin:$PATH" PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev
# Code runs as this user: it owns nothing in the image, and compose mounts the root
# file system read-only with a small tmpfs at /tmp for each run's work directory.
USER 65532:65532

EXPOSE 8004
CMD ["aep-sandbox"]
