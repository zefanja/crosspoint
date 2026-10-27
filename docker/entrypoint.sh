#!/bin/sh
set -eu

# Arguments given (e.g. `docker run <image> python spike/check_anki.py`): run them directly.
if [ "$#" -gt 0 ]; then
    exec "$@"
fi

missing=""
for var in SECRET_KEY BASE_URL; do
    eval "value=\${$var:-}"
    [ -n "$value" ] || missing="$missing $var"
done
if [ -n "$missing" ]; then
    echo "ERROR: missing required environment variable(s):$missing" >&2
    exit 1
fi

alembic upgrade head

# exec: uvicorn becomes PID 1 and receives SIGTERM -> lifespan shutdown runs.
exec uvicorn app.main:app \
    --host 0.0.0.0 --port 8000 --workers 1 \
    --proxy-headers --forwarded-allow-ips "${FORWARDED_ALLOW_IPS:-127.0.0.1}"
