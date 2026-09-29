#!/bin/sh
# Запуск api. Миграции — только при AUTO_MIGRATE=true (локально); на проде — вручную и по разрешению (DEPLOY.md).
set -e
if [ "$AUTO_MIGRATE" = "true" ]; then
    alembic upgrade head
fi
exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers
