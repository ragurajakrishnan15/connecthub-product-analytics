"""
Create or update the API's read-only database role (idempotent).

    python -m api.provision [--database DB]

Runs with the warehouse OWNER credentials (POSTGRES_USER / POSTGRES_PASSWORD);
the API process itself only ever receives API_DB_USER / API_DB_PASSWORD.
The role (PHASE_4_PLAN.md §8.6):

- LOGIN, no superuser / createdb / createrole / replication
- default_transaction_read_only = on, statement_timeout = API_STATEMENT_TIMEOUT_MS
- CONNECT on the database; USAGE on gold, analytics, ops
- SELECT on every table in gold and analytics, and on ops.pipeline_runs only
- nothing on bronze, staging, intermediate or experiments
- ALTER DEFAULT PRIVILEGES for the owner in gold and analytics, so tables that
  dbt or the analytics step create later (including dbt's table swaps and full
  refreshes) are readable without re-running this command

Exit codes: 0 ok, 1 error, 2 invalid configuration.
"""
import argparse
import json
import sys

from psycopg2 import sql
from pydantic import ValidationError
from sqlalchemy import text

READ_SCHEMAS = ('gold', 'analytics')       # SELECT on all tables, now and future
OPS_TABLES = ('ops.pipeline_runs',)        # the only ops table the API reads


def provision(engine, api_user, api_password, statement_timeout_ms=5000):
    """Create or update the role and its grants in one transaction. Returns a summary."""
    from pipeline.__main__ import OPS_DDL

    with engine.begin() as conn:
        owner, database = conn.execute(text('SELECT current_user, current_database()')).one()
        if api_user == owner:
            raise ValueError('API_DB_USER must differ from the warehouse owner (POSTGRES_USER)')
        existed = conn.execute(text('SELECT 1 FROM pg_roles WHERE rolname = :u'),
                               {'u': api_user}).first() is not None
        role = sql.Identifier(api_user)
        statements = [
            sql.SQL('{} ROLE {} WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION '
                    'NOBYPASSRLS PASSWORD {}').format(
                sql.SQL('ALTER' if existed else 'CREATE'), role, sql.Literal(api_password)),
            sql.SQL('ALTER ROLE {} SET default_transaction_read_only = on').format(role),
            sql.SQL('ALTER ROLE {} SET statement_timeout = {}').format(
                role, sql.Literal(int(statement_timeout_ms))),
            sql.SQL('GRANT CONNECT ON DATABASE {} TO {}').format(sql.Identifier(database), role),
        ]
        for schema in (*READ_SCHEMAS, 'ops'):
            statements += [
                sql.SQL('CREATE SCHEMA IF NOT EXISTS {}').format(sql.Identifier(schema)),
                sql.SQL('GRANT USAGE ON SCHEMA {} TO {}').format(sql.Identifier(schema), role),
            ]
        for schema in READ_SCHEMAS:
            statements += [
                sql.SQL('GRANT SELECT ON ALL TABLES IN SCHEMA {} TO {}').format(
                    sql.Identifier(schema), role),
                sql.SQL('ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA {} '
                        'GRANT SELECT ON TABLES TO {}').format(
                    sql.Identifier(owner), sql.Identifier(schema), role),
            ]
        for stmt in OPS_DDL.split(';'):           # the pipeline's own DDL, IF NOT EXISTS
            if stmt.strip():
                conn.execute(text(stmt))
        for table in OPS_TABLES:
            schema, name = table.split('.')
            statements.append(sql.SQL('GRANT SELECT ON {}.{} TO {}').format(
                sql.Identifier(schema), sql.Identifier(name), role))

        # psycopg2 executes Composed statements on the same transaction without
        # parameter interpolation, so the password literal is quoted safely.
        cursor = conn.connection.dbapi_connection.cursor()
        try:
            for stmt in statements:
                cursor.execute(stmt)
        finally:
            cursor.close()
        readable = conn.execute(text("""
            SELECT COUNT(*) FROM information_schema.role_table_grants
            WHERE grantee = :u AND privilege_type = 'SELECT'"""), {'u': api_user}).scalar()
    return {'role': api_user, 'action': 'updated' if existed else 'created',
            'database': database, 'owner': owner, 'schemas': [*READ_SCHEMAS, 'ops'],
            'readable_tables': readable}


def main(argv=None):
    from api.settings import Settings, describe_validation_error
    from pipeline.config import create_engine
    from pipeline.log import redact

    parser = argparse.ArgumentParser(prog='python -m api.provision',
                                     description='Create or update the read-only API role.')
    parser.add_argument('--database', help='Database to provision (default: POSTGRES_DB)')
    args = parser.parse_args(argv)
    try:
        settings = Settings()
    except ValidationError as exc:
        print(f'error: invalid API configuration: {describe_validation_error(exc)}',
              file=sys.stderr)
        return 2
    try:
        engine = create_engine(args.database or settings.postgres_db)
        summary = provision(engine, settings.api_db_user,
                            settings.api_db_password.get_secret_value(),
                            settings.api_statement_timeout_ms)
        engine.dispose()
    except Exception as exc:
        print(f'error: provisioning failed: {redact(exc)}', file=sys.stderr)
        return 1
    print(json.dumps(summary))
    return 0


if __name__ == '__main__':
    sys.exit(main())
