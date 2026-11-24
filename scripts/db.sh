#!/usr/bin/env bash
# Project-local Postgres cluster (no Docker, no system service). Data lives in ./.pgdata.
set -euo pipefail
cd "$(dirname "$0")/.."
PGBIN="${PGBIN:-/opt/homebrew/opt/postgresql@15/bin}"
PGDATA="$PWD/.pgdata"
PORT="${PGPORT:-5433}"
case "${1:-}" in
  init)
    if [ ! -d "$PGDATA" ]; then
      "$PGBIN/initdb" -D "$PGDATA" -U xmatch --auth=trust -E UTF8 >/dev/null
      echo "listen_addresses = 'localhost'" >> "$PGDATA/postgresql.conf"
      echo "port = $PORT" >> "$PGDATA/postgresql.conf"
    fi
    "$0" start
    "$PGBIN/createdb" -h localhost -p "$PORT" -U xmatch xmatch 2>/dev/null || true
    "$PGBIN/createdb" -h localhost -p "$PORT" -U xmatch xmatch_test 2>/dev/null || true
    echo "ready: postgresql://xmatch@localhost:$PORT/xmatch" ;;
  start)
    "$PGBIN/pg_ctl" -D "$PGDATA" -l "$PWD/.pglog" -w status >/dev/null 2>&1 || "$PGBIN/pg_ctl" -D "$PGDATA" -l "$PWD/.pglog" -w start >/dev/null
    echo "postgres running on port $PORT" ;;
  stop) "$PGBIN/pg_ctl" -D "$PGDATA" -w stop ;;
  status) "$PGBIN/pg_ctl" -D "$PGDATA" status ;;
  psql) shift; "$PGBIN/psql" -h localhost -p "$PORT" -U xmatch "${@:-xmatch}" ;;
  *) echo "usage: $0 init|start|stop|status|psql [db]"; exit 1 ;;
esac
