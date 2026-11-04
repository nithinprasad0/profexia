#!/bin/sh
set -e

# Migrations are additive (`alembic upgrade head`) and never drop or reset data.
# Set RUN_MIGRATIONS_ON_STARTUP=false if you run them as a separate release step.
case "${RUN_MIGRATIONS_ON_STARTUP:-true}" in
  true|True|TRUE|1|yes)
    echo "==> Applying Alembic migrations"
    alembic upgrade head

    echo "==> Seeding required roles"
    python -m app.infrastructure.seed
    ;;
  *)
    echo "==> Skipping migrations (RUN_MIGRATIONS_ON_STARTUP=${RUN_MIGRATIONS_ON_STARTUP})"
    ;;
esac

echo "==> Starting API on 0.0.0.0:${PORT:-8000}"
# --proxy-headers lets FastAPI see the original https/wss scheme behind the
# platform's TLS-terminating load balancer. --ws-max-size bounds WebSocket frames.
exec uvicorn app.main:app \
  --host 0.0.0.0 \
  --port "${PORT:-8000}" \
  --proxy-headers \
  --forwarded-allow-ips="*" \
  --ws-max-size "${WS_MAX_SIZE_BYTES:-1048576}" \
  ${UVICORN_EXTRA_ARGS}
