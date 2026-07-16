FROM ghcr.io/astral-sh/uv:0.11.28 AS uv

FROM python:3.12.13-slim

COPY --from=uv /uv /uvx /bin/
WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

COPY pyproject.toml uv.lock README.md ./
COPY apps ./apps
COPY packages ./packages
RUN uv sync --frozen --no-dev

COPY alembic.ini ./
COPY alembic ./alembic

RUN useradd --create-home --uid 10001 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000
CMD ["uvicorn", "enterprise_rag_api.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
