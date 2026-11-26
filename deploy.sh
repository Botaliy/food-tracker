#!/usr/bin/env bash
# Pull latest code and rebuild the container. Run on the server, inside the repo.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

[ -d .git ] && git pull --ff-only
if [ ! -f .env ]; then
    echo "ERROR: .env not found in $(pwd) — copy .env.example and fill it in." >&2
    exit 1
fi
mkdir -p data
docker compose up -d --build
docker image prune -f >/dev/null
docker compose ps
