#!/bin/sh
set -eu

echo "Applying database migrations..."
alembic upgrade head

echo "Starting TAMP API..."
exec uvicorn app.main:app --host 0.0.0.0 --port "${BACKEND_PORT:-5000}" --no-proxy-headers
