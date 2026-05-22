FROM mcr.microsoft.com/playwright/python:v1.59.0-noble

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src/ src/
RUN uv sync --frozen --no-dev

COPY config.yaml config-daily.yaml ./

VOLUME ["/app/data"]

ENTRYPOINT [".venv/bin/koker"]
CMD ["book", "--config", "config-daily.yaml", "--timeout", "30"]
