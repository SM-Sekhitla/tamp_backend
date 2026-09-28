#!/bin/sh
set -eu

# Run this on a single API instance; use a separate migration job when scaling.
python -m alembic upgrade head
exec python -m uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-5000}" --no-proxy-headers
