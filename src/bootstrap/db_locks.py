# services/vectoplan-chunk/src/bootstrap/db_locks.py
"""
PostgreSQL advisory locks for `vectoplan-chunk` bootstrap operations.

This module centralizes lock handling for database bootstrap code.

Responsibilities:
- provide safe PostgreSQL advisory locks for schema bootstrap
- provide safe PostgreSQL advisory locks for default seed bootstrap
- serialize db.create_all() across workers/processes
- serialize default seed operations across workers/processes
- avoid leaving connections open when startup/worker errors occur
- gracefully become a no-op on non-PostgreSQL backends

Important boundaries:
- no db.create_all() here
- no default seed logic here
- no model imports here
- no chunk loading here
- no ORM relationship traversal here

Why this exists:

    Gunicorn workers can import the Flask app in parallel.
    If db.create_all() or seed code runs in each worker, PostgreSQL can hit
    concurrent CREATE TABLE races, for example:

        duplicate key value violates unique constraint "pg_type_typname_nsp_index"

    Therefore schema/bootstrap mutation must be protected by a database-level
    lock whenever it can run in more than one process.

Design:

    - Runtime startup should not mutate DB at all.
    - Explicit DB bootstrap may mutate DB.
    - If explicit DB bootstrap runs twice, advisory locks serialize it.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Final, Generator, Mapping

try:
    from sqlalchemy import text
except Exception:  # pragma: no cover - partial import environment
    text = None  # type: ignore[assignment]

try:
    from sqlalchemy.engine import Connection, Engine
except Exception:  # pragma: no cover - partial import environment
    Connection = Any  # type: ignore[misc, assignment]
    Engine = Any  # type: ignore[misc, assignment]

try:
    from flask import Flask
except Exception:  # pragma: no cover - partial import environment
    Flask = Any  # type: ignore[misc, assignment]

try:
    from extensions import db as default_db
except Exception:  # pragma: no cover - partial import environment
    default_db = None  # type: ignore[assignment]

try:
    from .settings import (
        build_database_settings,
        get_bool_setting,
        get_int_setting,
        is_postgresql_uri,
    )
except Exception:  # pragma: no cover - fallback for direct import tests
    build_database_settings = None  # type: ignore[assignment]

    def get_bool_setting(
        app: Any,
        key: str,
        default: bool = False,
        aliases: tuple[str, ...] | None = None,
        prefer_env: bool = True,
    ) -> bool:
        try:
            value = getattr(app, "config", {}).get(key, default)
        except Exception:
            value = default
        if isinstance(value, bool):
            return value
        text_value = str(value).strip().lower()
        if text_value in {"1", "true", "yes", "on"}:
            return True
        if text_value in {"0", "false", "no", "off"}:
            return False
        return default

    def get_int_setting(
        app: Any,
        key: str,
        default: int = 0,
        aliases: tuple[str, ...] | None = None,
        minimum: int | None = None,
        maximum: int | None = None,
        prefer_env: bool = True,
    ) -> int:
        try:
            result = int(getattr(app, "config", {}).get(key, default))
        except Exception:
            result = default
        if minimum is not None:
            result = max(minimum, result)
        if maximum is not None:
            result = min(maximum, result)
        return result

    def is_postgresql_uri(uri: str) -> bool:
        lowered = str(uri or "").strip().lower()
        return lowered.startswith("postgresql://") or lowered.startswith("postgresql+")


# -----------------------------------------------------------------------------
# Constants
# -----------------------------------------------------------------------------

DEFAULT_SCHEMA_BOOTSTRAP_LOCK_KEY: Final[int] = 50020001
DEFAULT_SEED_BOOTSTRAP_LOCK_KEY: Final[int] = 50020002

DEFAULT_LOCK_NAMESPACE: Final[str] = "vectoplan-chunk"
DEFAULT_SCHEMA_LOCK_NAME: Final[str] = "schema_bootstrap"
DEFAULT_SEED_LOCK_NAME: Final[str] = "default_seed"

LOCK_STATUS_ACQUIRED: Final[str] = "acquired"
LOCK_STATUS_RELEASED: Final[str] = "released"
LOCK_STATUS_SKIPPED: Final[str] = "skipped"
LOCK_STATUS_FAILED: Final[str] = "failed"
LOCK_STATUS_NOT_ACQUIRED: Final[str] = "not-acquired"


# -----------------------------------------------------------------------------
# Data structures
# -----------------------------------------------------------------------------

@dataclass(slots=True)
class AdvisoryLockResult:
    """Serializable advisory-lock operation result."""

    ok: bool
    status: str

    lock_name: str
    lock_key: int
    lock_namespace: str

    backend: str
    enabled: bool
    blocking: bool

    acquired: bool = False
    released: bool = False
    skipped: bool = False

    started_at: str | None = None
    acquired_at: str | None = None
    released_at: str | None = None
    completed_at: str | None = None
    duration_ms: int = 0

    error: str | None = None
    details: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return serializable dict."""
        return asdict(self)


@dataclass(slots=True)
class AdvisoryLockHandle:
    """
    Held advisory-lock handle.

    The same PostgreSQL connection must be used to release the lock.
    """

    result: AdvisoryLockResult
    connection: Connection | None = None
    owns_connection: bool = True

    def to_dict(self) -> dict[str, Any]:
        """Return serializable dict."""
        return {
            "result": self.result.to_dict(),
            "hasConnection": self.connection is not None,
            "ownsConnection": self.owns_connection,
        }


# -----------------------------------------------------------------------------
# Primitive helpers
# -----------------------------------------------------------------------------

def _utc_now() -> datetime:
    """Return current UTC datetime robustly."""
    try:
        return datetime.now(timezone.utc)
    except Exception:
        return datetime.fromtimestamp(0, tz=timezone.utc)


def _utc_now_iso() -> str:
    """Return current UTC timestamp as ISO string."""
    try:
        return _utc_now().isoformat()
    except Exception:
        return "1970-01-01T00:00:00+00:00"


def _duration_ms(started_at_iso: str | None, completed_at_iso: str | None) -> int:
    """Return duration in milliseconds from ISO timestamps."""
    if not started_at_iso or not completed_at_iso:
        return 0

    try:
        started = datetime.fromisoformat(started_at_iso)
        completed = datetime.fromisoformat(completed_at_iso)
        return max(0, int((completed - started).total_seconds() * 1000))
    except Exception:
        return 0


def _safe_str(value: Any, default: str = "") -> str:
    """Convert value to stripped string."""
    if value is None:
        return default

    try:
        result = str(value).strip()
    except Exception:
        return default

    return result or default


def _safe_int(value: Any, default: int = 0, minimum: int | None = None) -> int:
    """Convert value to int robustly."""
    try:
        result = int(value)
    except Exception:
        result = default

    if minimum is not None:
        try:
            result = max(minimum, result)
        except Exception:
            result = minimum

    return result


def _safe_bool(value: Any, default: bool = False) -> bool:
    """Convert value to bool robustly."""
    if isinstance(value, bool):
        return value

    if isinstance(value, int):
        return bool(value)

    text_value = _safe_str(value, "").lower()

    if text_value in {"1", "true", "t", "yes", "y", "on", "enabled"}:
        return True

    if text_value in {"0", "false", "f", "no", "n", "off", "disabled"}:
        return False

    return default


def _safe_exception_message(exc: BaseException | Any) -> str:
    """Return robust exception message."""
    try:
        message = str(exc)
    except Exception:
        message = exc.__class__.__name__

    return message or exc.__class__.__name__


def _safe_dict(value: Any) -> dict[str, Any]:
    """Convert mapping-like value to dict."""
    if isinstance(value, dict):
        return value

    if isinstance(value, Mapping):
        try:
            return dict(value)
        except Exception:
            return {}

    return {}


def _safe_log_debug(app: Any, message: str, *args: Any) -> None:
    """Debug-log defensively."""
    try:
        app.logger.debug(message, *args)
    except Exception:
        pass


def _safe_log_info(app: Any, message: str, *args: Any) -> None:
    """Info-log defensively."""
    try:
        app.logger.info(message, *args)
    except Exception:
        pass


def _safe_log_warning(app: Any, message: str, *args: Any) -> None:
    """Warning-log defensively."""
    try:
        app.logger.warning(message, *args)
    except Exception:
        pass


# -----------------------------------------------------------------------------
# Backend / config helpers
# -----------------------------------------------------------------------------

def _get_app_config_value(app: Any, key: str, default: Any = None) -> Any:
    """Read app.config value defensively."""
    try:
        config = getattr(app, "config", None)
        if config is None:
            return default
        if hasattr(config, "get"):
            return config.get(key, default)
        return config[key]
    except Exception:
        return default


def _get_db_extension(db_extension: Any = None) -> Any:
    """Return DB extension object."""
    if db_extension is not None:
        return db_extension
    return default_db


def _get_engine(app: Any = None, db_extension: Any = None) -> Engine | None:
    """Return SQLAlchemy engine robustly."""
    db_obj = _get_db_extension(db_extension)

    if db_obj is None:
        return None

    try:
        engine = db_obj.engine
        if engine is not None:
            return engine
    except Exception:
        pass

    try:
        get_engine = getattr(db_obj, "get_engine", None)
        if callable(get_engine):
            return get_engine(app)
    except Exception:
        pass

    return None


def _get_database_uri(app: Any = None, db_extension: Any = None) -> str:
    """Return configured database URI."""
    try:
        if build_database_settings is not None:
            settings = build_database_settings(app)
            uri = _safe_str(getattr(settings, "sqlalchemy_database_uri", ""), "")
            if uri:
                return uri
    except Exception:
        pass

    for key in (
        "SQLALCHEMY_DATABASE_URI",
        "VECTOPLAN_CHUNK_SQLALCHEMY_DATABASE_URI",
        "VECTOPLAN_CHUNK_DATABASE_URI",
        "DATABASE_URL",
    ):
        value = _safe_str(_get_app_config_value(app, key, ""), "")
        if value:
            return value

    try:
        engine = _get_engine(app, db_extension)
        if engine is not None:
            return _safe_str(getattr(engine, "url", ""), "")
    except Exception:
        pass

    return ""


def is_postgresql_backend(app: Any = None, db_extension: Any = None) -> bool:
    """Return whether configured backend is PostgreSQL."""
    uri = _get_database_uri(app, db_extension)
    if uri and is_postgresql_uri(uri):
        return True

    try:
        engine = _get_engine(app, db_extension)
        dialect_name = _safe_str(getattr(getattr(engine, "dialect", None), "name", ""), "")
        return dialect_name.lower().startswith("postgres")
    except Exception:
        return False


def are_advisory_locks_enabled(app: Any = None, default: bool = True) -> bool:
    """Return whether bootstrap advisory locks are enabled."""
    try:
        return get_bool_setting(
            app,
            "VECTOPLAN_CHUNK_DB_BOOTSTRAP_ADVISORY_LOCKS",
            default,
            aliases=(
                "CHUNK_DB_BOOTSTRAP_ADVISORY_LOCKS",
                "DB_BOOTSTRAP_ADVISORY_LOCKS",
            ),
        )
    except Exception:
        return default


def get_schema_bootstrap_lock_key(app: Any = None) -> int:
    """Return configured schema bootstrap advisory lock key."""
    try:
        return get_int_setting(
            app,
            "VECTOPLAN_CHUNK_SCHEMA_BOOTSTRAP_LOCK_KEY",
            DEFAULT_SCHEMA_BOOTSTRAP_LOCK_KEY,
            aliases=("CHUNK_SCHEMA_BOOTSTRAP_LOCK_KEY",),
            minimum=1,
        )
    except Exception:
        return DEFAULT_SCHEMA_BOOTSTRAP_LOCK_KEY


def get_seed_bootstrap_lock_key(app: Any = None) -> int:
    """Return configured seed bootstrap advisory lock key."""
    try:
        return get_int_setting(
            app,
            "VECTOPLAN_CHUNK_SEED_BOOTSTRAP_LOCK_KEY",
            DEFAULT_SEED_BOOTSTRAP_LOCK_KEY,
            aliases=("CHUNK_SEED_BOOTSTRAP_LOCK_KEY",),
            minimum=1,
        )
    except Exception:
        return DEFAULT_SEED_BOOTSTRAP_LOCK_KEY


# -----------------------------------------------------------------------------
# Lock result helpers
# -----------------------------------------------------------------------------

def _new_lock_result(
    lock_name: str,
    lock_key: int,
    lock_namespace: str = DEFAULT_LOCK_NAMESPACE,
    backend: str = "unknown",
    enabled: bool = True,
    blocking: bool = True,
) -> AdvisoryLockResult:
    """Create base lock result."""
    return AdvisoryLockResult(
        ok=False,
        status=LOCK_STATUS_NOT_ACQUIRED,
        lock_name=_safe_str(lock_name, "unnamed_lock"),
        lock_key=_safe_int(lock_key, 0, minimum=0),
        lock_namespace=_safe_str(lock_namespace, DEFAULT_LOCK_NAMESPACE),
        backend=_safe_str(backend, "unknown"),
        enabled=bool(enabled),
        blocking=bool(blocking),
        started_at=_utc_now_iso(),
        details={},
    )


def _complete_result(result: AdvisoryLockResult, ok: bool, status: str) -> AdvisoryLockResult:
    """Complete result timestamps/status."""
    result.ok = bool(ok)
    result.status = _safe_str(status, LOCK_STATUS_FAILED)
    result.completed_at = _utc_now_iso()
    result.duration_ms = _duration_ms(result.started_at, result.completed_at)
    return result


def _mark_skipped(
    result: AdvisoryLockResult,
    reason: str,
    details: dict[str, Any] | None = None,
) -> AdvisoryLockResult:
    """Mark lock result as skipped."""
    result.skipped = True
    result.acquired = False
    result.released = False
    result.error = None
    result.details = {
        **_safe_dict(result.details),
        "reason": reason,
        **(details or {}),
    }
    return _complete_result(result, ok=True, status=LOCK_STATUS_SKIPPED)


def _mark_failed(
    result: AdvisoryLockResult,
    error: str,
    details: dict[str, Any] | None = None,
) -> AdvisoryLockResult:
    """Mark lock result as failed."""
    result.error = _safe_str(error, "Unknown advisory lock error.")
    result.details = {
        **_safe_dict(result.details),
        **(details or {}),
    }
    return _complete_result(result, ok=False, status=LOCK_STATUS_FAILED)


# -----------------------------------------------------------------------------
# Low-level lock operations
# -----------------------------------------------------------------------------

def acquire_advisory_lock(
    app: Any,
    lock_key: int,
    *,
    lock_name: str = "bootstrap",
    lock_namespace: str = DEFAULT_LOCK_NAMESPACE,
    blocking: bool = True,
    enabled: bool | None = None,
    connection: Connection | None = None,
    db_extension: Any = None,
) -> AdvisoryLockHandle:
    """
    Acquire PostgreSQL advisory lock.

    If the backend is not PostgreSQL or locks are disabled, returns a skipped
    successful handle without a connection.

    Args:
        app: Flask app or compatible object.
        lock_key: PostgreSQL advisory lock key.
        lock_name: Human-readable lock name for logs/results.
        lock_namespace: Human-readable namespace.
        blocking: If true, use pg_advisory_lock. If false, use pg_try_advisory_lock.
        enabled: Optional explicit enable flag.
        connection: Optional existing connection. If provided, caller owns it.
        db_extension: Optional SQLAlchemy extension override.

    Returns:
        AdvisoryLockHandle.
    """
    lock_key = _safe_int(lock_key, 0, minimum=1)
    lock_name = _safe_str(lock_name, "bootstrap")
    lock_namespace = _safe_str(lock_namespace, DEFAULT_LOCK_NAMESPACE)

    if enabled is None:
        enabled = are_advisory_locks_enabled(app, default=True)

    backend = "postgresql" if is_postgresql_backend(app, db_extension) else "non-postgresql"

    result = _new_lock_result(
        lock_name=lock_name,
        lock_key=lock_key,
        lock_namespace=lock_namespace,
        backend=backend,
        enabled=bool(enabled),
        blocking=bool(blocking),
    )

    if not enabled:
        _safe_log_info(
            app,
            "Skipping advisory lock `%s` because advisory locks are disabled.",
            lock_name,
        )
        _mark_skipped(result, "advisory_locks_disabled")
        return AdvisoryLockHandle(result=result, connection=None, owns_connection=False)

    if not is_postgresql_backend(app, db_extension):
        _safe_log_info(
            app,
            "Skipping advisory lock `%s` because backend is not PostgreSQL.",
            lock_name,
        )
        _mark_skipped(
            result,
            "non_postgresql_backend",
            details={
                "backend": backend,
            },
        )
        return AdvisoryLockHandle(result=result, connection=None, owns_connection=False)

    if text is None:
        _mark_failed(result, "sqlalchemy.text is unavailable.")
        return AdvisoryLockHandle(result=result, connection=None, owns_connection=False)

    owns_connection = connection is None
    active_connection = connection

    try:
        if active_connection is None:
            engine = _get_engine(app, db_extension)
            if engine is None:
                _mark_failed(result, "SQLAlchemy engine is unavailable.")
                return AdvisoryLockHandle(result=result, connection=None, owns_connection=False)

            active_connection = engine.connect()

        _safe_log_info(
            app,
            "Acquiring PostgreSQL advisory lock `%s` (key=%s, blocking=%s).",
            lock_name,
            lock_key,
            bool(blocking),
        )

        if blocking:
            active_connection.execute(
                text("SELECT pg_advisory_lock(:lock_key)"),
                {"lock_key": lock_key},
            )
            acquired = True
        else:
            acquired_result = active_connection.execute(
                text("SELECT pg_try_advisory_lock(:lock_key)"),
                {"lock_key": lock_key},
            )
            acquired = bool(acquired_result.scalar())

        result.acquired = bool(acquired)
        result.acquired_at = _utc_now_iso()

        if acquired:
            result.ok = True
            result.status = LOCK_STATUS_ACQUIRED
            result.details = {
                **_safe_dict(result.details),
                "acquireFunction": "pg_advisory_lock" if blocking else "pg_try_advisory_lock",
            }
            return AdvisoryLockHandle(
                result=result,
                connection=active_connection,
                owns_connection=owns_connection,
            )

        result.ok = False
        result.status = LOCK_STATUS_NOT_ACQUIRED
        result.details = {
            **_safe_dict(result.details),
            "acquireFunction": "pg_try_advisory_lock",
            "reason": "lock_not_available",
        }

        if owns_connection and active_connection is not None:
            try:
                active_connection.close()
            except Exception:
                pass

        return AdvisoryLockHandle(
            result=result,
            connection=None,
            owns_connection=False,
        )

    except Exception as exc:
        error = _safe_exception_message(exc)
        _safe_log_warning(
            app,
            "Could not acquire PostgreSQL advisory lock `%s` (key=%s): %s",
            lock_name,
            lock_key,
            error,
        )

        _mark_failed(
            result,
            error,
            details={
                "exceptionType": exc.__class__.__name__,
            },
        )

        if owns_connection and active_connection is not None:
            try:
                active_connection.close()
            except Exception:
                pass

        return AdvisoryLockHandle(
            result=result,
            connection=None,
            owns_connection=False,
        )


def release_advisory_lock(
    app: Any,
    handle: AdvisoryLockHandle,
) -> AdvisoryLockResult:
    """
    Release a previously acquired advisory lock.

    The lock must be released on the same connection used to acquire it.
    """
    result = handle.result

    if result.skipped:
        result.released = False
        result.released_at = _utc_now_iso()
        return _complete_result(result, ok=True, status=LOCK_STATUS_SKIPPED)

    if not result.acquired:
        result.released = False
        result.released_at = _utc_now_iso()
        return _complete_result(result, ok=result.ok, status=result.status)

    connection = handle.connection

    if connection is None:
        result.released = False
        result.released_at = _utc_now_iso()
        return _mark_failed(
            result,
            "Cannot release advisory lock because connection is missing.",
        )

    if text is None:
        result.released = False
        result.released_at = _utc_now_iso()
        return _mark_failed(
            result,
            "Cannot release advisory lock because sqlalchemy.text is unavailable.",
        )

    try:
        unlock_result = connection.execute(
            text("SELECT pg_advisory_unlock(:lock_key)"),
            {"lock_key": result.lock_key},
        )
        released = bool(unlock_result.scalar())

        result.released = released
        result.released_at = _utc_now_iso()
        result.details = {
            **_safe_dict(result.details),
            "releaseFunction": "pg_advisory_unlock",
            "postgresReleased": released,
        }

        if released:
            _safe_log_info(
                app,
                "Released PostgreSQL advisory lock `%s` (key=%s).",
                result.lock_name,
                result.lock_key,
            )
            return _complete_result(result, ok=True, status=LOCK_STATUS_RELEASED)

        _safe_log_warning(
            app,
            "PostgreSQL advisory lock `%s` (key=%s) was not released by pg_advisory_unlock().",
            result.lock_name,
            result.lock_key,
        )
        return _complete_result(result, ok=False, status=LOCK_STATUS_FAILED)

    except Exception as exc:
        error = _safe_exception_message(exc)
        _safe_log_warning(
            app,
            "Could not release PostgreSQL advisory lock `%s` (key=%s): %s",
            result.lock_name,
            result.lock_key,
            error,
        )
        return _mark_failed(
            result,
            error,
            details={
                "exceptionType": exc.__class__.__name__,
            },
        )

    finally:
        if handle.owns_connection and connection is not None:
            try:
                connection.close()
            except Exception:
                pass


@contextmanager
def advisory_lock(
    app: Any,
    lock_key: int,
    *,
    lock_name: str = "bootstrap",
    lock_namespace: str = DEFAULT_LOCK_NAMESPACE,
    blocking: bool = True,
    enabled: bool | None = None,
    db_extension: Any = None,
    fail_if_not_acquired: bool = True,
) -> Generator[AdvisoryLockResult, None, None]:
    """
    Context manager for PostgreSQL advisory lock.

    Usage:

        with advisory_lock(app, 50020001, lock_name="schema_bootstrap") as lock:
            if lock.acquired or lock.skipped:
                db.create_all()

    If backend is non-PostgreSQL, lock result is `skipped=True` and the block
    still runs. That keeps local SQLite or isolated tests usable.

    If `blocking=False` and the lock is unavailable:
        - fail_if_not_acquired=True  -> raises RuntimeError
        - fail_if_not_acquired=False -> yields result with acquired=False
    """
    handle = acquire_advisory_lock(
        app,
        lock_key,
        lock_name=lock_name,
        lock_namespace=lock_namespace,
        blocking=blocking,
        enabled=enabled,
        db_extension=db_extension,
    )

    result = handle.result

    if not result.ok and not result.skipped:
        if fail_if_not_acquired:
            raise RuntimeError(
                f"Could not acquire advisory lock `{result.lock_name}` "
                f"(key={result.lock_key}): {result.error or result.status}"
            )

    try:
        yield result
    finally:
        release_advisory_lock(app, handle)


# -----------------------------------------------------------------------------
# Specialized lock helpers
# -----------------------------------------------------------------------------

@contextmanager
def schema_bootstrap_lock(
    app: Any,
    *,
    blocking: bool = True,
    enabled: bool | None = None,
    db_extension: Any = None,
    fail_if_not_acquired: bool = True,
) -> Generator[AdvisoryLockResult, None, None]:
    """Context manager for schema/bootstrap advisory lock."""
    lock_key = get_schema_bootstrap_lock_key(app)

    with advisory_lock(
        app,
        lock_key,
        lock_name=DEFAULT_SCHEMA_LOCK_NAME,
        lock_namespace=DEFAULT_LOCK_NAMESPACE,
        blocking=blocking,
        enabled=enabled,
        db_extension=db_extension,
        fail_if_not_acquired=fail_if_not_acquired,
    ) as result:
        yield result


@contextmanager
def seed_bootstrap_lock(
    app: Any,
    *,
    blocking: bool = True,
    enabled: bool | None = None,
    db_extension: Any = None,
    fail_if_not_acquired: bool = True,
) -> Generator[AdvisoryLockResult, None, None]:
    """Context manager for default-seed advisory lock."""
    lock_key = get_seed_bootstrap_lock_key(app)

    with advisory_lock(
        app,
        lock_key,
        lock_name=DEFAULT_SEED_LOCK_NAME,
        lock_namespace=DEFAULT_LOCK_NAMESPACE,
        blocking=blocking,
        enabled=enabled,
        db_extension=db_extension,
        fail_if_not_acquired=fail_if_not_acquired,
    ) as result:
        yield result


def try_acquire_schema_bootstrap_lock(
    app: Any,
    *,
    enabled: bool | None = None,
    db_extension: Any = None,
) -> AdvisoryLockHandle:
    """Try to acquire schema bootstrap lock without blocking."""
    return acquire_advisory_lock(
        app,
        get_schema_bootstrap_lock_key(app),
        lock_name=DEFAULT_SCHEMA_LOCK_NAME,
        lock_namespace=DEFAULT_LOCK_NAMESPACE,
        blocking=False,
        enabled=enabled,
        db_extension=db_extension,
    )


def try_acquire_seed_bootstrap_lock(
    app: Any,
    *,
    enabled: bool | None = None,
    db_extension: Any = None,
) -> AdvisoryLockHandle:
    """Try to acquire seed bootstrap lock without blocking."""
    return acquire_advisory_lock(
        app,
        get_seed_bootstrap_lock_key(app),
        lock_name=DEFAULT_SEED_LOCK_NAME,
        lock_namespace=DEFAULT_LOCK_NAMESPACE,
        blocking=False,
        enabled=enabled,
        db_extension=db_extension,
    )


# -----------------------------------------------------------------------------
# DB cleanup helpers
# -----------------------------------------------------------------------------

def safe_session_rollback(db_extension: Any = None) -> bool:
    """Rollback current SQLAlchemy session defensively."""
    db_obj = _get_db_extension(db_extension)

    if db_obj is None:
        return False

    try:
        db_obj.session.rollback()
        return True
    except Exception:
        return False


def safe_session_remove(db_extension: Any = None) -> bool:
    """Remove current SQLAlchemy session defensively."""
    db_obj = _get_db_extension(db_extension)

    if db_obj is None:
        return False

    try:
        db_obj.session.remove()
        return True
    except Exception:
        return False


def safe_session_cleanup(
    *,
    rollback: bool = False,
    remove: bool = True,
    db_extension: Any = None,
) -> dict[str, bool]:
    """Rollback/remove SQLAlchemy session defensively."""
    result = {
        "rollback": False,
        "remove": False,
    }

    if rollback:
        result["rollback"] = safe_session_rollback(db_extension)

    if remove:
        result["remove"] = safe_session_remove(db_extension)

    return result


# -----------------------------------------------------------------------------
# Diagnostics
# -----------------------------------------------------------------------------

def build_lock_diagnostics(app: Any = None, db_extension: Any = None) -> dict[str, Any]:
    """Build safe advisory-lock diagnostics."""
    uri = _get_database_uri(app, db_extension)

    return {
        "backend": "postgresql" if is_postgresql_backend(app, db_extension) else "non-postgresql",
        "isPostgresql": is_postgresql_backend(app, db_extension),
        "advisoryLocksEnabled": are_advisory_locks_enabled(app, default=True),
        "schemaBootstrapLockKey": get_schema_bootstrap_lock_key(app),
        "seedBootstrapLockKey": get_seed_bootstrap_lock_key(app),
        "databaseUriConfigured": bool(uri),
        "sqlalchemyTextAvailable": text is not None,
        "dbExtensionAvailable": _get_db_extension(db_extension) is not None,
        "engineAvailable": _get_engine(app, db_extension) is not None,
    }


def advisory_lock_result_to_dict(result: AdvisoryLockResult | AdvisoryLockHandle | Any) -> dict[str, Any]:
    """Serialize lock result or handle to dict."""
    if isinstance(result, AdvisoryLockHandle):
        return result.to_dict()

    if isinstance(result, AdvisoryLockResult):
        return result.to_dict()

    if isinstance(result, Mapping):
        try:
            return dict(result)
        except Exception:
            return {}

    return {}


# -----------------------------------------------------------------------------
# Public exports
# -----------------------------------------------------------------------------

__all__ = [
    "AdvisoryLockHandle",
    "AdvisoryLockResult",
    "DEFAULT_LOCK_NAMESPACE",
    "DEFAULT_SCHEMA_BOOTSTRAP_LOCK_KEY",
    "DEFAULT_SCHEMA_LOCK_NAME",
    "DEFAULT_SEED_BOOTSTRAP_LOCK_KEY",
    "DEFAULT_SEED_LOCK_NAME",
    "LOCK_STATUS_ACQUIRED",
    "LOCK_STATUS_FAILED",
    "LOCK_STATUS_NOT_ACQUIRED",
    "LOCK_STATUS_RELEASED",
    "LOCK_STATUS_SKIPPED",
    "acquire_advisory_lock",
    "advisory_lock",
    "advisory_lock_result_to_dict",
    "are_advisory_locks_enabled",
    "build_lock_diagnostics",
    "get_schema_bootstrap_lock_key",
    "get_seed_bootstrap_lock_key",
    "is_postgresql_backend",
    "release_advisory_lock",
    "safe_session_cleanup",
    "safe_session_remove",
    "safe_session_rollback",
    "schema_bootstrap_lock",
    "seed_bootstrap_lock",
    "try_acquire_schema_bootstrap_lock",
    "try_acquire_seed_bootstrap_lock",
]