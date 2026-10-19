#!/bin/sh
set -eu
mkdir -p /run/postgres-tls
cp /run/tls/server.key /run/postgres-tls/server.key
cp /run/tls/server.crt /run/postgres-tls/server.crt
cp /run/pg_hba.conf /run/postgres-tls/pg_hba.conf
chown -R postgres:postgres /run/postgres-tls
chmod 700 /run/postgres-tls
chmod 600 /run/postgres-tls/server.key
chmod 600 /run/postgres-tls/server.crt /run/postgres-tls/pg_hba.conf
exec /usr/local/bin/docker-entrypoint.sh "$@"
