"""Alembic environment.

The URL comes from ``app.config.settings`` and the target metadata from
``app.database.Base`` — importing ``app.models`` is what populates it.
"""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

from app.config import settings
from app.database import Base

import app.models  # noqa: F401  (registers every table on Base.metadata)

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Escape "%" so ConfigParser interpolation never chokes on a password.
config.set_main_option("sqlalchemy.url", settings.database_url.replace("%", "%%"))

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Emit SQL to stdout without a live connection."""
    context.configure(
        url=settings.database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live connection.

    A caller may hand one over in ``config.attributes["connection"]`` - the
    pattern Alembic documents for running the chain from inside a program.
    ``tests/test_migraciones.py`` uses it to walk ``upgrade head`` and
    ``downgrade base`` over a throwaway SQLite file, so the chain is exercised
    by the suite instead of by hand. Without it nothing changes: the engine is
    built from ``settings.database_url`` exactly as before.
    """
    compartida = config.attributes.get("connection")
    if compartida is not None:
        context.configure(
            connection=compartida,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()
        return

    connectable = create_engine(settings.database_url, poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()
    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
