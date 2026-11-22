import pytest
import sqlalchemy as sa

from tests.migrations import helpers


@pytest.fixture
def a0_engine(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'billing_p1.db'}")
    helpers.build_a0_database(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def engine_at(a0_engine):
    """Return a function that migrates the a0 database to a revision and returns the engine."""

    def _at(revision: str) -> sa.Engine:
        helpers.migrate(a0_engine, revision)
        return a0_engine

    return _at

