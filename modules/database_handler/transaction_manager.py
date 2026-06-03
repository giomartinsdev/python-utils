from __future__ import annotations

import functools
import logging
import random
import time
from contextlib import contextmanager
from typing import Any, Callable, Generator, Sequence, TypeVar

from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

logger = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable[..., Any])


class TransactionManager:
    """SQLAlchemy 2.x transaction manager with ACID best practices."""

    def __init__(
        self,
        engine_or_url: Any,
        *,
        echo: bool = False,
        pool_size: int = 5,
        max_overflow: int = 10,
        pool_timeout: int = 30,
        pool_recycle: int = 3600,
        expire_on_commit: bool = False,
    ) -> None:
        if isinstance(engine_or_url, str):
            engine_kwargs: dict[str, Any] = {"echo": echo}
            if not engine_or_url.startswith("sqlite"):
                engine_kwargs.update(
                    {
                        "pool_size": pool_size,
                        "max_overflow": max_overflow,
                        "pool_timeout": pool_timeout,
                        "pool_recycle": pool_recycle,
                        "pool_pre_ping": True,
                    }
                )
            self._engine = create_engine(engine_or_url, **engine_kwargs)
        else:
            self._engine = engine_or_url

        self._session_factory = sessionmaker(
            bind=self._engine,
            expire_on_commit=expire_on_commit,
        )

        logger.info(
            "TransactionManager initialized (engine=%s, pool_size=%s)",
            self._engine.url,
            pool_size,
        )

    @property
    def engine(self):
        return self._engine

    @property
    def session_factory(self) -> sessionmaker:
        return self._session_factory

    @contextmanager
    def session(self) -> Generator[Session, None, None]:
        """Yield a session that auto-commits on success, rolls back on error, and always closes."""
        session: Session = self._session_factory()
        logger.debug("Session opened (id=%s)", id(session))
        try:
            yield session
            session.commit()
            logger.debug("Session committed (id=%s)", id(session))
        except Exception:
            session.rollback()
            logger.exception("Session rolled back (id=%s)", id(session))
            raise
        finally:
            session.close()
            logger.debug("Session closed (id=%s)", id(session))

    @contextmanager
    def nested(self, session: Session) -> Generator[Session, None, None]:
        """Begin a SAVEPOINT-based nested transaction. Only the savepoint rolls back on failure."""
        nested_txn = session.begin_nested()
        logger.debug("Savepoint started (session=%s)", id(session))
        try:
            yield session
            nested_txn.commit()
            logger.debug("Savepoint committed (session=%s)", id(session))
        except Exception:
            nested_txn.rollback()
            logger.warning("Savepoint rolled back (session=%s)", id(session))
            raise

    @contextmanager
    def read_only(self) -> Generator[Session, None, None]:
        """Provide a read-only session (autoflush disabled, SET TRANSACTION READ ONLY where supported)."""
        session: Session = self._session_factory()
        session.autoflush = False
        logger.debug("Read-only session opened (id=%s)", id(session))

        try:
            try:
                session.execute(text("SET TRANSACTION READ ONLY"))
            except Exception:
                logger.debug("SET TRANSACTION READ ONLY not supported, skipping.")

            yield session
        except Exception:
            session.rollback()
            logger.exception("Read-only session error (id=%s)", id(session))
            raise
        finally:
            session.close()
            logger.debug("Read-only session closed (id=%s)", id(session))

    def with_retry(
        self,
        max_retries: int = 3,
        backoff: float = 0.5,
        retryable_exceptions: tuple[type[Exception], ...] = (OperationalError,),
    ) -> Callable[[F], F]:
        """Decorator that retries on transient DB errors with exponential backoff + jitter."""

        def decorator(func: F) -> F:
            @functools.wraps(func)
            def wrapper(*args: Any, **kwargs: Any) -> Any:
                last_exception: Exception | None = None
                for attempt in range(1, max_retries + 1):
                    try:
                        return func(*args, **kwargs)
                    except retryable_exceptions as exc:
                        last_exception = exc
                        if attempt == max_retries:
                            logger.error(
                                "All %d retries exhausted for %s: %s",
                                max_retries,
                                func.__name__,
                                exc,
                            )
                            raise
                        delay = backoff * (2 ** (attempt - 1)) + random.uniform(0, 0.1)
                        logger.warning(
                            "Retry %d/%d for %s after %.2fs (error: %s)",
                            attempt,
                            max_retries,
                            func.__name__,
                            delay,
                            exc,
                        )
                        time.sleep(delay)
                raise last_exception  # type: ignore[misc]

            return wrapper  # type: ignore[return-value]

        return decorator

    def bulk_operation(
        self,
        session: Session,
        items: Sequence[Any],
        batch_size: int = 1000,
    ) -> int:
        """Add items in batches, flushing every `batch_size` to manage memory."""
        total = 0
        for i, item in enumerate(items, start=1):
            session.add(item)
            if i % batch_size == 0:
                session.flush()
                logger.debug("Flushed batch (%d items so far)", i)
            total = i

        if total % batch_size != 0:
            session.flush()

        logger.info("Bulk operation complete: %d items processed", total)
        return total

    def dispose(self) -> None:
        """Dispose of the connection pool, releasing all connections."""
        self._engine.dispose()
        logger.info("Engine disposed, all connections released.")

    def __repr__(self) -> str:
        return f"TransactionManager(engine={self._engine.url!r})"
