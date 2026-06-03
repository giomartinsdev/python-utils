"""
Tests for TransactionManager.

Uses an in-memory SQLite database to verify commit, rollback,
savepoint, retry, read-only, and bulk operation behavior.
"""

import pytest
from sqlalchemy import Column, Integer, String, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import DeclarativeBase

from modules.database_handler import TransactionConfig, TransactionManager


# ──────────────────────────────────────────────
# Test model
# ──────────────────────────────────────────────

class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(50), nullable=False)


# ──────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────

@pytest.fixture
def manager():
    """Configure the singleton with an in-memory SQLite DB and reset after each test."""
    TransactionManager.reset()  # clear any prior instance (e.g. from app module import)
    mgr = TransactionManager.configure(TransactionConfig(url="sqlite:///:memory:"))
    Base.metadata.create_all(mgr.engine)
    yield mgr
    TransactionManager.reset()


# ──────────────────────────────────────────────
# Session lifecycle tests
# ──────────────────────────────────────────────

class TestSessionLifecycle:
    def test_commit_on_success(self, manager: TransactionManager):
        """Session auto-commits when context exits cleanly."""
        with manager.session() as session:
            session.add(User(name="Alice"))

        # Verify data persisted
        with manager.session() as session:
            result = session.execute(select(User)).scalars().all()
            assert len(result) == 1
            assert result[0].name == "Alice"

    def test_rollback_on_exception(self, manager: TransactionManager):
        """Session auto-rolls back when an exception is raised."""
        with pytest.raises(ValueError, match="intentional"):
            with manager.session() as session:
                session.add(User(name="Bob"))
                raise ValueError("intentional error")

        # Verify nothing persisted
        with manager.session() as session:
            result = session.execute(select(User)).scalars().all()
            assert len(result) == 0

    def test_multiple_operations_in_session(self, manager: TransactionManager):
        """Multiple adds within one session all commit together."""
        with manager.session() as session:
            session.add(User(name="Alice"))
            session.add(User(name="Bob"))
            session.add(User(name="Charlie"))

        with manager.session() as session:
            count = session.execute(
                text("SELECT COUNT(*) FROM users")
            ).scalar()
            assert count == 3


# ──────────────────────────────────────────────
# Nested transaction (savepoint) tests
# ──────────────────────────────────────────────

class TestNestedTransactions:
    def test_savepoint_commit(self, manager: TransactionManager):
        """Nested transaction commits when clean."""
        with manager.session() as session:
            session.add(User(name="Outer"))
            with manager.nested(session):
                session.add(User(name="Inner"))

        with manager.session() as session:
            users = session.execute(select(User)).scalars().all()
            names = {u.name for u in users}
            assert names == {"Outer", "Inner"}

    def test_savepoint_rollback_preserves_outer(self, manager: TransactionManager):
        """Nested failure rolls back only the savepoint, not the outer transaction."""
        with manager.session() as session:
            session.add(User(name="Outer"))
            session.flush()  # Ensure outer is flushed

            try:
                with manager.nested(session):
                    session.add(User(name="Inner-fail"))
                    session.flush()
                    raise RuntimeError("inner failure")
            except RuntimeError:
                pass  # Caught — outer transaction continues

            session.add(User(name="After-nested"))

        with manager.session() as session:
            users = session.execute(select(User)).scalars().all()
            names = {u.name for u in users}
            assert "Outer" in names
            assert "After-nested" in names
            assert "Inner-fail" not in names


# ──────────────────────────────────────────────
# Retry decorator tests
# ──────────────────────────────────────────────

class TestRetry:
    def test_retry_succeeds_after_failures(self, manager: TransactionManager):
        """Function succeeds after transient failures within retry limit."""
        call_count = 0

        @manager.with_retry(max_retries=3, backoff=0.01)
        def flaky_function():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise OperationalError("transient", None, None)
            return "success"

        result = flaky_function()
        assert result == "success"
        assert call_count == 3

    def test_retry_exhausted_raises(self, manager: TransactionManager):
        """Raises after all retries are exhausted."""

        @manager.with_retry(max_retries=2, backoff=0.01)
        def always_fails():
            raise OperationalError("persistent", None, None)

        with pytest.raises(OperationalError):
            always_fails()

    def test_non_retryable_exception_not_retried(self, manager: TransactionManager):
        """Non-retryable exceptions are raised immediately."""
        call_count = 0

        @manager.with_retry(max_retries=3, backoff=0.01)
        def raises_value_error():
            nonlocal call_count
            call_count += 1
            raise ValueError("not retryable")

        with pytest.raises(ValueError):
            raises_value_error()

        assert call_count == 1  # No retry attempted


# ──────────────────────────────────────────────
# Read-only session tests
# ──────────────────────────────────────────────

class TestReadOnly:
    def test_read_only_can_query(self, manager: TransactionManager):
        """Read-only session can execute SELECT statements."""
        # Seed data
        with manager.session() as session:
            session.add(User(name="ReadMe"))

        with manager.read_only() as session:
            users = session.execute(select(User)).scalars().all()
            assert len(users) == 1
            assert users[0].name == "ReadMe"


# ──────────────────────────────────────────────
# Bulk operation tests
# ──────────────────────────────────────────────

class TestBulkOperation:
    def test_bulk_processes_all_items(self, manager: TransactionManager):
        """All items are added and flushed."""
        users = [User(name=f"User-{i}") for i in range(50)]

        with manager.session() as session:
            count = manager.bulk_operation(session, users, batch_size=10)

        assert count == 50

        with manager.session() as session:
            total = session.execute(
                text("SELECT COUNT(*) FROM users")
            ).scalar()
            assert total == 50

    def test_bulk_with_batch_not_multiple(self, manager: TransactionManager):
        """Works correctly when item count is not a multiple of batch_size."""
        users = [User(name=f"User-{i}") for i in range(17)]

        with manager.session() as session:
            count = manager.bulk_operation(session, users, batch_size=5)

        assert count == 17


# ──────────────────────────────────────────────
# Engine / repr tests
# ──────────────────────────────────────────────

class TestMisc:
    def test_repr(self, manager: TransactionManager):
        r = repr(manager)
        assert "TransactionManager" in r
        assert "sqlite" in r

    def test_engine_property(self, manager: TransactionManager):
        assert manager.engine is not None

    def test_session_factory_property(self, manager: TransactionManager):
        assert manager.session_factory is not None

    def test_get_returns_singleton(self, manager: TransactionManager):
        """TransactionManager.get() returns the same configured instance."""
        assert TransactionManager.get() is manager

    def test_configure_from_engine(self, manager: TransactionManager):
        """Can reconfigure the singleton from an existing engine after reset."""
        engine = manager.engine
        TransactionManager.reset()
        mgr2 = TransactionManager.configure(engine)
        with mgr2.session() as session:
            session.execute(text("SELECT 1"))
