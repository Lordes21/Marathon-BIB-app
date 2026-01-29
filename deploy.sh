#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"

# Load environment variables
if [ -f .env ]; then
    set -a
    source .env
    set +a
fi

echo "=== Marathon BIB App - Local Deployment ==="

# --- 1. PostgreSQL ---
echo "[1/4] Starting PostgreSQL..."
if pg_isready -q 2>/dev/null; then
    echo "  PostgreSQL already running."
else
    sudo service postgresql start
    for i in 1 2 3 4 5; do pg_isready -q && break; sleep 0.5; done
    if pg_isready -q; then
        echo "  PostgreSQL started."
    else
        echo "  ERROR: Could not start PostgreSQL." >&2
        exit 1
    fi
fi

# Ensure database and user exist
sudo -u postgres psql -tc "SELECT 1 FROM pg_roles WHERE rolname='${PG_USER:-marathon}'" | grep -q 1 || \
    sudo -u postgres psql -c "CREATE USER ${PG_USER:-marathon} WITH PASSWORD '${PG_PASS:-123456}';"
sudo -u postgres psql -tc "SELECT 1 FROM pg_database WHERE datname='${PG_DB:-marathon_db}'" | grep -q 1 || \
    sudo -u postgres psql -c "CREATE DATABASE ${PG_DB:-marathon_db} OWNER ${PG_USER:-marathon};"
echo "  Database ready."

# Run schema
PGPASSWORD="${PG_PASS:-123456}" psql -h "${PG_HOST:-127.0.0.1}" -p "${PG_PORT:-5432}" \
    -U "${PG_USER:-marathon}" -d "${PG_DB:-marathon_db}" -f db/schema_v1.sql -q 2>/dev/null || true
echo "  Schema applied."

# --- 2. S3 Mock Server (moto) ---
echo "[2/4] Starting S3-compatible storage (moto)..."
S3_PORT="${S3_ENDPOINT##*:}"  # extract port from endpoint URL
S3_PORT="${S3_PORT%/}"

# Kill any existing moto server on that port
if lsof -ti:"$S3_PORT" >/dev/null 2>&1; then
    echo "  Stopping existing process on port $S3_PORT..."
    kill $(lsof -ti:"$S3_PORT") 2>/dev/null || true
    sleep 1
fi

python3 -c "
import os, time, signal, boto3
from werkzeug.serving import make_server
from moto.server import create_backend_app

app = create_backend_app('s3')
server = make_server('0.0.0.0', $S3_PORT, app, threaded=True)

# Handle shutdown gracefully
def shutdown(signum, frame):
    server.shutdown()
signal.signal(signal.SIGTERM, shutdown)
signal.signal(signal.SIGINT, shutdown)

# Create the bucket before serving
import threading
t = threading.Thread(target=server.serve_forever, daemon=True)
t.start()
time.sleep(1)

s3 = boto3.client('s3',
    endpoint_url='http://localhost:$S3_PORT',
    aws_access_key_id='${S3_ACCESS_KEY:-testing}',
    aws_secret_access_key='${S3_SECRET_KEY:-testing}',
    region_name='us-east-1')
try:
    s3.create_bucket(Bucket='${S3_BUCKET:-marathon}')
    print('  Bucket created: ${S3_BUCKET:-marathon}')
except Exception as e:
    print(f'  Bucket exists or error: {e}')

print('  S3 mock server running on port $S3_PORT')
server.serve_forever()
" &
S3_PID=$!
echo "  S3 mock server PID: $S3_PID"
for i in 1 2 3 4 5; do curl -s -o /dev/null http://localhost:$S3_PORT/ && break; sleep 0.5; done

# --- 3. Start Flask App with Gunicorn ---
echo "[3/4] Starting Flask application with Gunicorn..."

# Kill any existing process on port 5000
if lsof -ti:5000 >/dev/null 2>&1; then
    echo "  Stopping existing process on port 5000..."
    kill $(lsof -ti:5000) 2>/dev/null || true
    sleep 1
fi

cd "$PROJECT_DIR"
gunicorn --bind 0.0.0.0:5000 \
    --workers 1 \
    --timeout 120 \
    --access-logfile - \
    --error-logfile - \
    --daemon \
    --pid /tmp/marathon_gunicorn.pid \
    app:app

# --- 4. Verify ---
echo "[4/4] Verifying deployment..."

FLASK_OK=false
for i in 1 2 3 4 5; do
    if curl -s -o /dev/null -w "%{http_code}" http://localhost:5000/ | grep -q "200"; then
        FLASK_OK=true
        break
    fi
    sleep 0.5
done

echo ""
echo "========================================="
echo "  Deployment Status"
echo "========================================="

if pg_isready -q; then
    echo "  [OK] PostgreSQL     : port ${PG_PORT:-5432}"
else
    echo "  [FAIL] PostgreSQL"
fi

if kill -0 "$S3_PID" 2>/dev/null; then
    echo "  [OK] S3 Storage     : port $S3_PORT"
else
    echo "  [FAIL] S3 Storage"
fi

if [ "$FLASK_OK" = true ]; then
    echo "  [OK] Flask App      : http://localhost:5000"
else
    echo "  [WARN] Flask App    : not responding yet (check logs)"
fi

echo "========================================="
echo ""
echo "Access the app at: http://localhost:5000"
echo ""
echo "To stop: bash stop.sh"
echo "Logs: gunicorn logs are printed to stdout"
echo "PID file: /tmp/marathon_gunicorn.pid"
echo "S3 PID: $S3_PID"
