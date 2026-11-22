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


@pytest.fixture
def p2_engine_at(tmp_path):
    """Like ``engine_at`` for the phase 2 chain: a database at the phase 1 head with
    the tables phase 2 touches and the rows of ``helpers.seed_p2_data``."""
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'billing_p2.db'}")
    helpers.build_p1_head_database(engine)
    helpers.seed_p2_data(engine)

    def _at(revision: str) -> sa.Engine:
        helpers.migrate(engine, revision)
        return engine

    yield _at
    engine.dispose()

