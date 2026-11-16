FROM python:3.12-slim

# ffmpeg with libass for burned subtitles, and a CJK font for them.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/* \
    && fc-cache -f
RUN pip install --no-cache-dir "uv>=0.11,<0.12"
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/opt/venv
ENV PATH="/opt/venv/bin:$PATH" PYTHONDONTWRITEBYTECODE=1 HOME=/tmp

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev
COPY entrypoint.sh /usr/local/bin/aep-media-entrypoint

EXPOSE 8008
# Starts as root only to hand the work folder to 65532, then runs as that user.
ENTRYPOINT ["aep-media-entrypoint"]
CMD ["aep-media"]
