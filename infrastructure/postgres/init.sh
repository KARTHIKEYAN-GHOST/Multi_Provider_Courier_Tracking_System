#!/bin/sh
# Runs once, when the data volume is first initialised.
# Creates only the test database; all application tables come from Engine migrations.
set -eu

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<SQL
CREATE DATABASE "${POSTGRES_TEST_DB}" OWNER "${POSTGRES_USER}";
SQL
