#!/usr/bin/env bash
# Verifies the running PostgreSQL and RabbitMQ containers match what the project expects.
# Usage: verify-deps.sh [docker-command]     (run via: make verify-deps)
set -euo pipefail

DOCKER="${1:-docker}"
cd "$(dirname "$0")/.."
set -a; . ./.env; set +a
COMPOSE="$DOCKER compose --env-file .env -f infrastructure/docker-compose.yml -p courier"

fail() { echo "FAIL: $*" >&2; exit 1; }
ok()   { echo "ok:   $*"; }

# --- PostgreSQL ---------------------------------------------------------------
for db in "$POSTGRES_DB" "$POSTGRES_TEST_DB"; do
  $COMPOSE exec -T --interactive=false postgres psql -U "$POSTGRES_USER" -d "$db" -tAc 'select 1' | grep -qx 1 \
    || fail "database $db is not reachable"
  ok "database $db reachable"
done

# --- RabbitMQ -----------------------------------------------------------------
rmq() { $COMPOSE exec -T --interactive=false rabbitmq "$@"; }

rmq rabbitmqctl -q list_vhosts | grep -qx "$RABBITMQ_VHOST" || fail "vhost $RABBITMQ_VHOST missing"
ok "vhost $RABBITMQ_VHOST exists"

check_user() {
  rmq rabbitmqctl -q authenticate_user "$1" "$2" >/dev/null || fail "user $1 cannot authenticate"
  ok "user $1 authenticates"
}
check_user "$RABBITMQ_ADMIN_USER"  "$RABBITMQ_ADMIN_PASSWORD"
check_user "$RABBITMQ_ENGINE_USER" "$RABBITMQ_ENGINE_PASSWORD"
check_user "$RABBITMQ_WORKER_USER" "$RABBITMQ_WORKER_PASSWORD"

perms="$(rmq rabbitmqctl -q list_permissions -p "$RABBITMQ_VHOST")"
echo "$perms" | awk -v u="$RABBITMQ_ENGINE_USER" '$1==u && $2==".*" && $3==".*" && $4==".*" {f=1} END {exit !f}' \
  || fail "engine permissions are not full within the vhost"
ok "engine has full rights in the vhost"

echo "$perms" | awk -v u="$RABBITMQ_WORKER_USER" '$1==u && $2=="^$" && $3=="^courier\\.events$" {f=1} END {exit !f}' \
  || fail "worker permissions are wider than expected"
ok "worker is limited to consuming adapter queues and publishing to courier.events"

if rmq rabbitmqctl -q list_users | awk '$1=="guest" {f=1} END {exit !f}'; then
  fail "default guest user exists"
fi
ok "no default guest user"

echo "all dependency checks passed"
