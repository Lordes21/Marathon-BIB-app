#!/usr/bin/env bash
set -euo pipefail

echo "=== Stopping Marathon BIB App ==="

# Stop Gunicorn
if [ -f /tmp/marathon_gunicorn.pid ]; then
    PID=$(cat /tmp/marathon_gunicorn.pid)
    if kill -0 "$PID" 2>/dev/null; then
        kill "$PID"
        echo "[OK] Gunicorn stopped (PID $PID)"
    else
        echo "[--] Gunicorn was not running"
    fi
    rm -f /tmp/marathon_gunicorn.pid
else
    # Try to find and kill gunicorn
    pkill -f "gunicorn.*app:app" 2>/dev/null && echo "[OK] Gunicorn stopped" || echo "[--] Gunicorn was not running"
fi

# Stop moto S3 mock server
pkill -f "moto.server" 2>/dev/null && echo "[OK] S3 mock server stopped" || echo "[--] S3 mock was not running"
pkill -f "create_backend_app" 2>/dev/null || true

echo ""
echo "Note: PostgreSQL is left running. Stop with: sudo service postgresql stop"
