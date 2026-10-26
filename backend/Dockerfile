FROM python:3.12-slim

# Databricks CLI, with the Terraform and provider it deploys bundles with, for
# dev validation of generated pipelines (aep.databricks).
COPY --from=ghcr.io/databricks/cli:0.298.0 /app /opt/databricks
RUN sed -i 's#/app/#/opt/databricks/#g' /opt/databricks/config/config.tfrc
ENV DATABRICKS_TF_EXEC_PATH=/opt/databricks/bin/terraform \
    DATABRICKS_TF_CLI_CONFIG_FILE=/opt/databricks/config/config.tfrc \
    PATH="/opt/databricks:$PATH"

RUN pip install --no-cache-dir "uv>=0.11,<0.12"
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev

EXPOSE 8000
CMD ["uvicorn", "aep.main:app", "--host", "0.0.0.0", "--port", "8000"]
