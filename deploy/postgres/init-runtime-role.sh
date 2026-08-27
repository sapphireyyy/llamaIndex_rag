#!/bin/sh
set -eu

runtime_password_file="${RAG_RUNTIME_DB_PASSWORD_FILE:-/run/secrets/postgres_runtime_password}"
if [ ! -r "$runtime_password_file" ]; then
  echo "RAG_RUNTIME_DB_PASSWORD_FILE must point to the runtime database password secret" >&2
  exit 1
fi

runtime_password="$(cat "$runtime_password_file")"
if [ -z "$runtime_password" ]; then
  echo "The runtime database password secret must not be empty" >&2
  exit 1
fi

psql \
  --set=ON_ERROR_STOP=1 \
  --username="$POSTGRES_USER" \
  --dbname="$POSTGRES_DB" \
  --set=runtime_password="$runtime_password" \
  --file=/docker-entrypoint-initdb.d/10-runtime-role.sql
