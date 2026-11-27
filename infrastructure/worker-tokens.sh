#!/usr/bin/env bash
# Gives each worker that reads its token from infrastructure/worker-tokens/<adapter>.env
# (the workers of adapters the AI wrote, Phase 12) a token of its own, once.
#
#   make worker-tokens               (the automatic update runs it after each deploy)
#
# A token is created by the running Engine (admin create-worker-token) and written only to
# that file: readable by you alone, never printed, never in Git (.gitignore). A file that
# exists already is left alone. An adapter whose row is not in the database yet is skipped
# and gets its token on the next run.
set -euo pipefail
cd "$(dirname "$0")/.."
COMPOSE=${COMPOSE:-docker compose --env-file .env -f infrastructure/docker-compose.yml -p courier}
DIR=infrastructure/worker-tokens

keys=$(grep -oE 'worker-tokens/[a-z][a-z0-9_]{0,49}\.env' infrastructure/docker-compose.yml \
       | sed -E 's#.*/([a-z0-9_]+)\.env#\1#' | sort -u)
[ -n "$keys" ] || exit 0
mkdir -p "$DIR"
chmod 700 "$DIR"
for key in $keys; do
    file="$DIR/$key.env"
    [ -s "$file" ] && continue
    if ! out=$($COMPOSE exec -T engine courier-engine admin create-worker-token --adapter "$key" --name automatic 2>/dev/null); then
        echo "$key: no token yet (its adapter is not in the database yet?)"
        continue
    fi
    token=$(printf '%s\n' "$out" | sed -n 's/^worker_token: //p')
    if [ -z "$token" ]; then
        echo "$key: the Engine gave no token"
        continue
    fi
    (umask 077 && printf 'WORKER_TOKEN=%s\n' "$token" >"$file")
    echo "$key: worker token written to $file"
done
