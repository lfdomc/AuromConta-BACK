#!/usr/bin/env sh
# Aplica las migraciones en orden a una base (Supabase o local).
# Uso: ./aplicar.sh "postgresql://postgres:<clave>@db.<proyecto>.supabase.co:5432/postgres"
set -e
DSN="$1"
for f in "$(dirname "$0")"/migrations/*.sql; do
  echo "== $f"
  psql "$DSN" -v ON_ERROR_STOP=1 -q -f "$f"
done
