FROM python:3.12-slim

RUN pip install --no-cache-dir "uv>=0.11,<0.12"
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/opt/venv
ENV PATH="/opt/venv/bin:$PATH" PYTHONDONTWRITEBYTECODE=1 HOME=/tmp

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev
COPY entrypoint.sh /usr/local/bin/aep-stock-entrypoint

EXPOSE 8009
# Starts as root only to hand its work folder to 65532, then runs as that user.
ENTRYPOINT ["aep-stock-entrypoint"]
CMD ["aep-stock"]
