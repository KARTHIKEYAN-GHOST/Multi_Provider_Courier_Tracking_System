#!/bin/sh
# Renders the definitions file from the template using environment variables,
# then hands over to the image's normal entrypoint.
#
# Exchanges and queues are NOT defined here: the Engine declares them at startup
# from courier-contracts/topology.yaml. This file only creates the vhost, the
# users and their permissions.
set -eu

for var in RABBITMQ_VHOST \
           RABBITMQ_ADMIN_USER RABBITMQ_ADMIN_PASSWORD \
           RABBITMQ_ENGINE_USER RABBITMQ_ENGINE_PASSWORD \
           RABBITMQ_WORKER_USER RABBITMQ_WORKER_PASSWORD; do
  eval "value=\${$var:-}"
  if [ -z "$value" ]; then
    echo "entrypoint: required variable $var is not set" >&2
    exit 1
  fi
  # Values are substituted into JSON, so restrict them to a safe alphabet.
  case "$value" in
    *[!A-Za-z0-9_-]*)
      echo "entrypoint: $var may contain only letters, digits, '-' and '_'" >&2
      exit 1
      ;;
  esac
done

target=/etc/rabbitmq/definitions.json
umask 077
sed \
  -e "s|__VHOST__|${RABBITMQ_VHOST}|g" \
  -e "s|__ADMIN_USER__|${RABBITMQ_ADMIN_USER}|g" \
  -e "s|__ADMIN_PASSWORD__|${RABBITMQ_ADMIN_PASSWORD}|g" \
  -e "s|__ENGINE_USER__|${RABBITMQ_ENGINE_USER}|g" \
  -e "s|__ENGINE_PASSWORD__|${RABBITMQ_ENGINE_PASSWORD}|g" \
  -e "s|__WORKER_USER__|${RABBITMQ_WORKER_USER}|g" \
  -e "s|__WORKER_PASSWORD__|${RABBITMQ_WORKER_PASSWORD}|g" \
  /opt/courier/definitions.template.json > "$target"
chown rabbitmq:rabbitmq "$target"
chmod 600 "$target"

exec docker-entrypoint.sh "$@"
