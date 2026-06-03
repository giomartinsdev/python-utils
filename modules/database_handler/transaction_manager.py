from __future__ import annotations

import functools
import logging
import random
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, ClassVar, Generator, Sequence, TypeVar

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

logger = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable[..., Any])


@dataclass
class TransactionConfig:
    """Engine and session configuration for TransactionManager."""

    url: str
    echo: bool = False
    pool_size: int = 5
    max_overflow: int = 10
    pool_timeout: int = 30
    pool_pre_ping: bool = True
    pool_recycle: int = 3600
    isolation_level: str | None = None
    connect_args: dict[str, Any] = field(default_factory=dict)
    expire_on_commit: bool = False
    autoflush: bool = False
    autocommit: bool = False


class TransactionManager:
    """SQLAlchemy 2.x transaction manager with ACID best practices."""

    _instance: ClassVar[TransactionManager | None] = None

    # ── Singleton interface ──────────────────────────────────────────────────

    @classmethod
    def configure(cls, config_or_engine: TransactionConfig | Engine) -> TransactionManager:
        """Initialize the singleton. Raises if already configured."""
        if cls._instance is not None:
            raise RuntimeError(
                "TransactionManager is already configured. "
                "Call TransactionManager.reset() before reconfiguring."
            )
        cls._instance = cls(config_or_engine)
        return cls._instance

    @classmethod
    def get(cls) -> TransactionManager:
        """Return the singleton. Raises if configure() was never called."""
        if cls._instance is None:
            raise RuntimeError(
                "TransactionManager is not configured. "
                "Call TransactionManager.configure() first."
            )
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        """Clear the singleton and dispose the engine. Use in tests or on reconfiguration."""
        if cls._instance is not None:
            cls._instance.dispose()
            cls._instance = None

    # ── Instance construction ────────────────────────────────────────────────

    def __init__(self, config_or_engine: TransactionConfig | Engine) -> None:
        if isinstance(config_or_engine, TransactionConfig):
            cfg = config_or_engine
            engine_kwargs: dict[str, Any] = {"echo": cfg.echo}
            if cfg.connect_args:
                engine_kwargs["connect_args"] = cfg.connect_args
            if cfg.isolation_level is not None:
                engine_kwargs["isolation_level"] = cfg.isolation_level
            if not cfg.url.startswith("sqlite"):
                engine_kwargs.update(
                    {
                        "pool_size": cfg.pool_size,
                        "max_overflow": cfg.max_overflow,
                        "pool_timeout": cfg.pool_timeout,
                        "pool_recycle": cfg.pool_recycle,
                        "pool_pre_ping": cfg.pool_pre_ping,
                    }
                )
            self._engine = create_engine(cfg.url, **engine_kwargs)
            self._session_factory = sessionmaker(
                bind=self._engine,
                expire_on_commit=cfg.expire_on_commit,
                autoflush=cfg.autoflush,
                autocommit=cfg.autocommit,
            )
            is_sqlite = cfg.url.startswith("sqlite")
            logger.info(
                "TransactionManager initialized (engine=%s%s)",
                self._engine.url,
                "" if is_sqlite else f", pool_size={cfg.pool_size}, max_overflow={cfg.max_overflow}",
            )
        elif isinstance(config_or_engine, Engine):
            self._engine = config_or_engine
            self._session_factory = sessionmaker(
                bind=self._engine,
                expire_on_commit=False,
            )
            logger.info("TransactionManager initialized from engine (engine=%s)", self._engine.url)
        else:
            raise TypeError(
                f"Expected TransactionConfig or Engine, got {type(config_or_engine).__name__!r}"
            )

    @property
    def engine(self) -> Engine:
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

        if max_retries < 1:
            raise ValueError("max_retries must be >= 1")

        def decorator(func: F) -> F:
            @functools.wraps(func)
            def wrapper(*args: Any, **kwargs: Any) -> Any:
                for attempt in range(1, max_retries + 1):
                    try:
                        return func(*args, **kwargs)
                    except retryable_exceptions as exc:
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
