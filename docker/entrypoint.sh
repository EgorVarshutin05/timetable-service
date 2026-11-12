#!/bin/sh
# Старт контейнера app: миграции → процесс из CMD; seed выполняет lifespan приложения.
# PostgreSQL к этому моменту готов: compose ждёт его healthcheck (depends_on: service_healthy).
set -eu

echo "[entrypoint] alembic upgrade head"
alembic upgrade head

exec "$@"
