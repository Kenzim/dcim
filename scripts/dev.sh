#!/bin/bash
# Local development: run backend and frontend without Docker (except optional mysql/redis).
# Usage:
#   ./scripts/dev.sh           # start mysql+redis, print commands for app and frontend
#   ./scripts/dev.sh --infra   # only start mysql+redis
#   ./scripts/dev.sh --app     # only run backend (assumes mysql+redis available)
#   ./scripts/dev.sh --frontend # only run frontend (assumes API at :8000)
#
# For full local dev:
#   Terminal 1: docker compose up -d mysql redis
#   Terminal 2: ./scripts/dev.sh --app
#   Terminal 3: ./scripts/dev.sh --frontend
set -e
cd "$(dirname "$0")/.."

run_infra() {
  docker compose up -d mysql redis
  echo "MySQL and Redis started. DATABASE_URL=mysql+pymysql://dcim:dcim@127.0.0.1:3306/dcim REDIS_HOST=127.0.0.1"
}

run_app() {
  export DATABASE_URL="${DATABASE_URL:-mysql+pymysql://dcim:dcim@127.0.0.1:3306/dcim}"
  export REDIS_HOST="${REDIS_HOST:-127.0.0.1}"
  export REDIS_PORT="${REDIS_PORT:-6379}"
  echo "Starting backend with DATABASE_URL=$DATABASE_URL REDIS_HOST=$REDIS_HOST"
  uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
}

run_frontend() {
  echo "Starting frontend dev server (Vite)"
  cd frontend && npm run dev
}

case "${1:-}" in
  --infra) run_infra ;;
  --app) run_app ;;
  --frontend) run_frontend ;;
  *)
    run_infra
    echo ""
    echo "Next steps:"
    echo "  Terminal 2: DATABASE_URL=mysql+pymysql://dcim:dcim@127.0.0.1:3306/dcim REDIS_HOST=127.0.0.1 ./scripts/dev.sh --app"
    echo "  Terminal 3: cd frontend && npm run dev"
    echo ""
    echo "API: http://localhost:8000  |  Frontend: http://localhost:5173"
    ;;
esac
