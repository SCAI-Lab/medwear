import os
from logging.config import fileConfig
from urllib.parse import quote_plus

from alembic import context
from sqlalchemy import engine_from_config, pool
from sqlmodel import SQLModel

# Import all models so Alembic detects schema changes
from medwear_med_api.models import MeasurementBatch, AuditLog, UserProject, ProjectSettings  # noqa

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = SQLModel.metadata

# Build DB URL from environment (defaults to project1)
_user = os.getenv("POSTGRES_USER", "")
_raw_pass = os.getenv("POSTGRES_PASSWORD", "")
if not _user or not _raw_pass:
    raise RuntimeError("POSTGRES_USER / POSTGRES_PASSWORD must be set to run migrations.")
_pass = quote_plus(_raw_pass)
_host = os.getenv("POSTGRES_HOST", "localhost")
_port = os.getenv("POSTGRES_PORT", "5432")
_db   = os.getenv("POSTGRES_DB", "medwear_project1")

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    f"postgresql://{_user}:{_pass}@{_host}:{_port}/{_db}"
)


def run_migrations_offline() -> None:
    context.configure(
        url=DATABASE_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    cfg = config.get_section(config.config_ini_section, {})
    cfg["sqlalchemy.url"] = DATABASE_URL
    connectable = engine_from_config(cfg, prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
