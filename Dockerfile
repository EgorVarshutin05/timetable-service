# syntax=docker/dockerfile:1
# Multi-stage по руководству uv для Docker: builder ставит зависимости из uv.lock с кэшем uv
# (BuildKit cache mount) и bind mount'ами вместо копирования манифестов, runtime получает /app с .venv.
# Требуется BuildKit: docker compose собирает образы через buildx 0.17+ (проверка — `just doctor`).

FROM python:3.12-slim-trixie AS builder

COPY --from=ghcr.io/astral-sh/uv:0.13 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0

WORKDIR /app

# Зависимости отдельным слоем: пересобирается только при изменении uv.lock или pyproject.toml.
# --locked: сборка падает, если uv.lock не соответствует pyproject.toml.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-install-project --no-default-groups

# Код проекта: editable-установка пакета src/app (группы dev и e2e в образ не попадают).
COPY . /app
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-default-groups


FROM python:3.12-slim-trixie AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH"

RUN groupadd --system app && useradd --system --gid app --home-dir /app --shell /usr/sbin/nologin app

WORKDIR /app
COPY --from=builder --chown=app:app /app /app

USER app
EXPOSE 8000

ENTRYPOINT ["/app/docker/entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
