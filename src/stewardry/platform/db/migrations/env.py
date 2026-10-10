"""Alembic environment. The engine is passed in by stewardry.platform.db.migrate."""

from alembic import context

from stewardry.platform.db.schema import metadata

engine = context.config.attributes["engine"]

with engine.begin() as connection:
    context.configure(
        connection=connection,
        target_metadata=metadata,
        render_as_batch=True,  # SQLite needs table rebuilds for most ALTERs
    )
    context.run_migrations()
