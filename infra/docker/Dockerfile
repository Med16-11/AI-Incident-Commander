FROM ghcr.io/astral-sh/uv:0.12.22 AS uv

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

COPY --from=uv /uv /uvx /bin/

WORKDIR /workspace

COPY pyproject.toml uv.lock README.md alembic.ini ./
COPY apps ./apps
COPY packages ./packages
COPY migrations ./migrations
RUN uv sync --locked --no-dev --no-editable

RUN useradd --create-home --uid 10001 opsmind
USER 10001

EXPOSE 8000
