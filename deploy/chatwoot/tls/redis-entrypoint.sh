#!/bin/sh
set -eu
mkdir -p /run/redis-tls
cp /run/tls/server.key /run/redis-tls/server.key
chown redis:redis /run/redis-tls /run/redis-tls/server.key
chmod 700 /run/redis-tls
chmod 600 /run/redis-tls/server.key
exec /usr/local/bin/docker-entrypoint.sh "$@"
