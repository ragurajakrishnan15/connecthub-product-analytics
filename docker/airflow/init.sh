#!/usr/bin/env bash
# One-shot initialisation for the Airflow services (idempotent):
#  1. create the Airflow metadata database (separate from the analytics DB)
#  2. migrate it
#  3. create the admin user from .env (no-op if it exists)
set -euo pipefail

python - <<'PY'
import os
import psycopg2

name = os.environ.get('AIRFLOW_DB', 'airflow')
conn = psycopg2.connect(host=os.environ['POSTGRES_HOST'], port=os.environ.get('POSTGRES_PORT', 5432),
                        user=os.environ['POSTGRES_USER'], password=os.environ['POSTGRES_PASSWORD'],
                        dbname=os.environ['POSTGRES_DB'])
conn.autocommit = True
with conn.cursor() as cur:
    cur.execute('SELECT 1 FROM pg_database WHERE datname = %s', (name,))
    if cur.fetchone():
        print(f'metadata database {name} exists')
    else:
        cur.execute(f'CREATE DATABASE "{name}"')
        print(f'created metadata database {name}')
conn.close()
PY

airflow db migrate

if airflow users list --output json | grep -q "\"username\": \"${AIRFLOW_ADMIN_USER}\""; then
    echo "admin user ${AIRFLOW_ADMIN_USER} exists"
else
    airflow users create --role Admin --username "${AIRFLOW_ADMIN_USER}" \
        --password "${AIRFLOW_ADMIN_PASSWORD}" --firstname Admin --lastname User \
        --email admin@connecthub.local
fi
