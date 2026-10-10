"""Run Alembic migrations programmatically; no alembic.ini is needed."""

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine

SCRIPT_LOCATION = "stewardry.platform.db:migrations"


def _config(engine: Engine) -> Config:
    config = Config()
    config.set_main_option("script_location", SCRIPT_LOCATION)
    config.attributes["engine"] = engine
    return config


def upgrade(engine: Engine, revision: str = "head") -> None:
    command.upgrade(_config(engine), revision)


def downgrade(engine: Engine, revision: str) -> None:
    command.downgrade(_config(engine), revision)
