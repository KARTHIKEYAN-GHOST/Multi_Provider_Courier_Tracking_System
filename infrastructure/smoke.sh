#!/usr/bin/env bash
# Checks the containerized stack started by `make up`: creates a user, tracks one dummy
# shipment and one demo-site (scraped) shipment through the real Engine and worker containers,
# and expects both to be delivered. Needs ENABLE_DUMMY_PROVIDER=true.
# The user and shipments it creates stay in the development database.
set -euo pipefail

DOCKER="${1:-docker}"
cd "$(dirname "$0")/.."
set -a; . ./.env; set +a
COMPOSE="$DOCKER compose --env-file .env -f infrastructure/docker-compose.yml -p courier"
BASE="http://127.0.0.1:${ENGINE_HOST_PORT:-8080}"

echo "1. Engine is ready (database and RabbitMQ reachable)"
curl -fsS "$BASE/readyz" >/dev/null

echo "2. Create a user"
EMAIL="smoke-$(date +%s)@example.com"
KEY=$($COMPOSE exec -T --interactive=false engine courier-engine admin create-user --email "$EMAIL" | sed -n 's/^api_key: //p')
test -n "$KEY"

echo "3. Track a dummy shipment"
NUMBER="DUMMY-OK-SMOKE$(date +%s)"
ID=$(curl -fsS -H "Authorization: Bearer $KEY" -H 'content-type: application/json' \
     -d "{\"provider_code\":\"dummy\",\"tracking_number\":\"$NUMBER\"}" \
     "$BASE/v1/tracking-requests" | sed -E 's/^\{"id":"([^"]+)".*/\1/')

echo "4. Wait for the worker's result"
for _ in $(seq 1 40); do
    BODY=$(curl -fsS -H "Authorization: Bearer $KEY" "$BASE/v1/tracking-requests/$ID")
    case "$BODY" in
        *'"shipment_status":"DELIVERED"'*) echo "OK: $NUMBER is DELIVERED (user $EMAIL)"; DONE=1; break ;;
    esac
    sleep 0.5
done
if [ -z "${DONE:-}" ]; then
    echo "FAILED: shipment did not reach DELIVERED. Last response:" >&2
    echo "$BODY" >&2
    exit 1
fi

echo "5. Scrape the demo courier website through the demo-site worker"
NUMBER="DEMO-OK-SMOKE$(date +%s)"
ID=$(curl -fsS -H "Authorization: Bearer $KEY" -H 'content-type: application/json' \
     -d "{\"provider_code\":\"demosite\",\"tracking_number\":\"$NUMBER\"}" \
     "$BASE/v1/tracking-requests" | sed -E 's/^\{"id":"([^"]+)".*/\1/')
for _ in $(seq 1 40); do
    BODY=$(curl -fsS -H "Authorization: Bearer $KEY" "$BASE/v1/tracking-requests/$ID")
    case "$BODY" in
        *'"shipment_status":"DELIVERED"'*) echo "OK: $NUMBER is DELIVERED"; exit 0 ;;
    esac
    sleep 0.5
done
echo "FAILED: scraped shipment did not reach DELIVERED. Last response:" >&2
echo "$BODY" >&2
exit 1
