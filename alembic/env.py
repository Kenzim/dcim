from logging.config import fileConfig

import sqlalchemy as sa
from sqlalchemy import engine_from_config
from sqlalchemy import pool

from alembic import context

# Import your models and Base
from app.core.database import Base, configure_engine
from app.core.config import settings
import app.models  # This ensures all models are imported
# Explicitly import junction table to ensure it's registered
from app.models.plugin_category import plugin_categories

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# disable_existing_loggers=False prevents disabling uvicorn.access when
# env.py is imported during app startup (migrations run in lifespan).
# A caller that passes its own connection (config.attributes["connection"], as the
# migration tests do) keeps its own logging.
if config.config_file_name is not None and "connection" not in config.attributes:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# Set the database URL from settings
config.set_main_option("sqlalchemy.url", settings.database_url)

# add your model's MetaData object here
# for 'autogenerate' support
target_metadata = Base.metadata

# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


VERSION_NUM_WIDTH = 255


def _create_wide_version_table(connection) -> None:
    """Create alembic_version with a wide version_num on a fresh MySQL/MariaDB database.

    Alembic's default is VARCHAR(32) and some revision ids are longer, which MySQL
    rejects in strict mode. An existing alembic_version table is left untouched.
    """
    if connection.dialect.name not in ("mysql", "mariadb"):
        return
    if sa.inspect(connection).has_table("alembic_version"):
        return
    connection.execute(
        sa.text(
            "CREATE TABLE alembic_version ("
            f"version_num VARCHAR({VERSION_NUM_WIDTH}) NOT NULL, "
            "CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num))"
        )
    )
    connection.commit()


def run_migrations_on(connection) -> None:
    """Run the migrations on ``connection``, committing each revision with its version bump.

    MySQL/MariaDB commit DDL implicitly, so a revision that fails after an
    earlier one in the same transaction would leave that earlier revision's
    schema in place with its ``alembic_version`` update rolled back. The
    transaction the version table inspection began is ended before
    ``configure``; otherwise alembic treats the connection as inside an outer
    transaction and never commits per revision.
    """
    _create_wide_version_table(connection)
    connection.commit()
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        transaction_per_migration=True,
    )

    with context.begin_transaction():
        context.run_migrations()
    # MySQL DDL is not transactional, so nothing above commits the final
    # alembic_version update; without this the last step's bump is rolled back.
    connection.commit()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    Uses the connection in ``config.attributes["connection"]`` when the caller
    gives one, otherwise an engine built from the configured URL.
    """
    connection = config.attributes.get("connection")
    if connection is not None:
        run_migrations_on(connection)
        return

    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    configure_engine(connectable)

    with connectable.connect() as connection:
        run_migrations_on(connection)


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
