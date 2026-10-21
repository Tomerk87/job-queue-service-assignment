"""Alembic environment: online migrations against DATABASE_URL."""

from logging.config import fileConfig

from sqlalchemy import create_engine

from alembic import context
from app.config import settings

if context.config.config_file_name is not None:
    fileConfig(context.config.config_file_name, disable_existing_loggers=False)

if context.is_offline_mode():
    raise RuntimeError("offline migrations are not supported; run against a live database")

engine = create_engine(settings.database_url)
with engine.connect() as connection:
    context.configure(connection=connection)
    with context.begin_transaction():
        context.run_migrations()
engine.dispose()
