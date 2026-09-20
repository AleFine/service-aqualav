#!/bin/sh
# Migrate, seed, then serve. Any failure stops the container.
set -e

echo "[aqualav] applying migrations..."
alembic upgrade head

if [ "${SEED_ENABLED:-true}" = "true" ]; then
  echo "[aqualav] running seed..."
  python -m app.seed
else
  echo "[aqualav] seed disabled (SEED_ENABLED=${SEED_ENABLED})."
fi

echo "[aqualav] starting API on :8000"
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
