#!/bin/sh
# Pornește dashboardul cu valorile păstrate local în .env.
set -a
. "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)/.env"
set +a
APP_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
exec python3 "$APP_DIR/dashboard.py"
