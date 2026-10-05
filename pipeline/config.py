"""
Runtime configuration from the environment (see .env.example).

Nothing secret has a default. POSTGRES_HOST defaults to 127.0.0.1 rather than
localhost: on Windows, localhost resolves to ::1 first and the container only
listens on IPv4, which costs ~2 s per connection.
"""
import os

from sqlalchemy.engine import URL

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SECRET_ENV_MARKERS = ('PASSWORD', 'SECRET', 'FERNET', 'TOKEN', 'KEY')
# Calendar window of the synthetic data (scripts/generate_synthetic_data.py).
DATA_START = '2025-01-01'
DATA_END = '2025-12-31'


def database_url(database=None, user=None, password=None):
    """SQLAlchemy URL from POSTGRES_* environment variables.

    user/password override POSTGRES_USER/POSTGRES_PASSWORD (the analytics API
    connects as its own read-only role); host, port and database are shared.
    """
    if password is None:
        password = os.environ.get('POSTGRES_PASSWORD')
        if not password:
            raise RuntimeError(
                'POSTGRES_PASSWORD is not set. Copy .env.example to .env and '
                'export its variables (see README "Configuration").'
            )
    return URL.create(
        'postgresql+psycopg2',
        username=user or os.environ.get('POSTGRES_USER', 'connecthub'),
        password=password,
        host=os.environ.get('POSTGRES_HOST', '127.0.0.1'),
        port=int(os.environ.get('POSTGRES_PORT', '5432')),
        database=database or os.environ.get('POSTGRES_DB', 'connecthub_analytics'),
    )


def create_engine(database=None):
    from sqlalchemy import create_engine as _create_engine
    return _create_engine(database_url(database))


def secret_values():
    """Values of secret-looking environment variables, for log redaction."""
    return [v for k, v in os.environ.items()
            if v and len(v) >= 4 and any(m in k.upper() for m in SECRET_ENV_MARKERS)]


def data_dir():
    return os.environ.get('CONNECTHUB_DATA_DIR', os.path.join(PROJECT_ROOT, 'data'))


def dbt_dir():
    return os.path.join(PROJECT_ROOT, 'dbt_project')
