import logging

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

from app.core.config import settings

logger = logging.getLogger(__name__)

# MySQL/MariaDB error raised when a session variable does not exist on the server.
_ER_UNKNOWN_SYSTEM_VARIABLE = 1193


def _disable_snapshot_isolation(dbapi_connection, connection_record) -> None:
    """Turn off InnoDB snapshot isolation for a new MySQL/MariaDB connection.

    MariaDB 11.6+ defaults ``innodb_snapshot_isolation`` to ON, so under REPEATABLE READ a
    ``SELECT ... FOR UPDATE`` on a row committed by another session after this transaction's
    first read fails with error 1020. The app is written for the older behaviour (the lock
    read returns the latest committed row), so keep REPEATABLE READ and switch the variable off.
    Servers that do not know the variable (MySQL, older MariaDB) are left as they are.
    """
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("SET SESSION innodb_snapshot_isolation = OFF")
    except Exception as exc:
        if not (exc.args and exc.args[0] == _ER_UNKNOWN_SYSTEM_VARIABLE):
            raise
        logger.debug("innodb_snapshot_isolation is not supported by this server; leaving it as is")
    finally:
        cursor.close()


def configure_engine(target: Engine) -> Engine:
    """Apply per-connection settings that every runtime engine (app, workers, Alembic) needs."""
    if target.dialect.name in ("mysql", "mariadb"):
        event.listen(target, "connect", _disable_snapshot_isolation)
    return target


engine = configure_engine(create_engine(settings.database_url))
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def get_db():
    """Dependency for getting database session"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
