"""
API configuration from the environment (see .env.example, "Analytics API").

Every setting is read here and validated when the app starts; an invalid or
missing value stops startup with a message that never contains secret values.
Like the pipeline, nothing secret has a default and no .env file is read: the
process environment is the only source.
"""
import re
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

_ORIGIN = re.compile(r'^https?://[A-Za-z0-9.-]+(:\d{1,5})?$')


def _split(value):
    if isinstance(value, str):
        return [v.strip() for v in value.split(',') if v.strip()]
    return value


class Settings(BaseSettings):
    model_config = SettingsConfigDict(case_sensitive=False, extra='ignore')

    # --- warehouse (shared with the pipeline); the API logs in as its own role
    postgres_host: str = Field('127.0.0.1', validation_alias='POSTGRES_HOST')
    postgres_port: int = Field(5432, ge=1, le=65535, validation_alias='POSTGRES_PORT')
    postgres_db: str = Field('connecthub_analytics', min_length=1, validation_alias='POSTGRES_DB')
    api_db_user: str = Field('connecthub_api', pattern=r'^[a-z_][a-z0-9_]{0,62}$')
    api_db_password: SecretStr = Field(min_length=8)

    # --- server
    api_host: str = '127.0.0.1'
    api_port: int = Field(8000, ge=1, le=65535)
    api_workers: int = Field(1, ge=1, le=16)
    api_env: Literal['development', 'production'] = 'development'
    api_log_level: Literal['DEBUG', 'INFO', 'WARNING', 'ERROR'] = 'INFO'

    # --- HTTP surface
    api_cors_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ['http://127.0.0.1:8000', 'http://localhost:8000'])
    api_docs_enabled: bool | None = None          # None: on in development, off in production
    api_auth_mode: Literal['none', 'api_key'] = 'none'
    api_keys: Annotated[list[SecretStr], NoDecode] = Field(default_factory=list)
    api_dataset_label: str = Field('synthetic', min_length=1, max_length=64)
    api_max_query_string: int = Field(2048, ge=256, le=16384)
    api_dashboard_path: Path | None = None        # set: serve this index.html at GET /

    # --- database pool and session limits (PHASE_4_PLAN.md §6)
    api_db_pool_size: int = Field(5, ge=1, le=50)
    api_db_max_overflow: int = Field(5, ge=0, le=50)
    api_db_pool_timeout_s: float = Field(5, gt=0, le=60)
    api_db_pool_recycle_s: int = Field(1800, ge=60)
    api_db_connect_timeout_s: int = Field(5, ge=1, le=60)
    api_statement_timeout_ms: int = Field(5000, ge=100, le=60000)
    api_lock_timeout_ms: int = Field(2000, ge=100, le=60000)
    api_ready_timeout_ms: int = Field(1000, ge=100, le=10000)

    # --- response cache and HTTP validation (PHASE_4_PLAN.md §8.2; docs/api.md "Caching")
    api_cache_enabled: bool = True
    api_cache_ttl_s: int = Field(300, ge=1, le=86400)
    api_cache_max_entries: int = Field(512, ge=1, le=100000)
    api_cache_max_bytes: int = Field(64 * 1024 * 1024, ge=1024 * 1024, le=2 * 1024 ** 3)
    api_data_version_ttl_s: float = Field(30, ge=0, le=3600)
    api_cache_max_age_s: int = Field(60, ge=0, le=3600)

    @field_validator('api_cors_origins', mode='before')
    @classmethod
    def _parse_origins(cls, value):
        origins = _split(value)
        for origin in origins:
            if origin in ('*', 'null'):
                raise ValueError(f'CORS origin {origin!r} is not allowed; list explicit origins')
            if not _ORIGIN.match(origin):
                raise ValueError(f'invalid CORS origin {origin!r} (expected scheme://host[:port])')
        return origins

    @field_validator('api_dashboard_path', mode='before')
    @classmethod
    def _blank_dashboard_path(cls, value):
        return None if isinstance(value, str) and not value.strip() else value

    @field_validator('api_keys', mode='before')
    @classmethod
    def _parse_keys(cls, value):
        keys = _split(value)
        if any(len(str(k)) < 16 for k in keys):
            raise ValueError('every API key must be at least 16 characters')
        return keys

    @model_validator(mode='after')
    def _check_modes(self):
        if self.api_auth_mode == 'api_key' and not self.api_keys:
            raise ValueError('API_AUTH_MODE=api_key needs at least one key in API_KEYS')
        if self.api_env == 'production' and self.api_auth_mode == 'none':
            raise ValueError('API_ENV=production requires API_AUTH_MODE=api_key')
        return self

    @property
    def docs_enabled(self) -> bool:
        if self.api_docs_enabled is None:
            return self.api_env == 'development'
        return self.api_docs_enabled

    def summary(self) -> dict:
        """Settings safe to log: secrets masked."""
        out = self.model_dump(mode='json')
        out['api_db_password'] = '***'
        out['api_keys'] = ['***'] * len(self.api_keys)
        out['docs_enabled'] = self.docs_enabled
        return out


def describe_validation_error(exc) -> str:
    """One line per problem, naming the environment variable; values are never echoed."""
    lines = []
    for err in exc.errors():
        field = str(err['loc'][0]) if err['loc'] else 'settings'
        env = {'postgres_host': 'POSTGRES_HOST', 'postgres_port': 'POSTGRES_PORT',
               'postgres_db': 'POSTGRES_DB'}.get(field, field.upper())
        message = err['msg'] if err['type'] != 'missing' else 'is required but not set'
        lines.append(f'{env}: {message}')
    return '; '.join(lines)
