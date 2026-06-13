# services/vectoplan-chunk/src/bootstrap/schema_bootstrap.py
"""
Explicit schema bootstrap for the `vectoplan-chunk` service.

This module owns the controlled schema bootstrap path.

Responsibilities:
- verify SQLAlchemy extension availability
- verify model registry availability
- inspect existing database tables
- optionally run db.create_all() for local/dev bootstrap
- protect db.create_all() with a PostgreSQL advisory lock
- never seed default data
- always cleanup sessions/connections after bootstrap work
- return serializable results for scripts, logs and status/debug output

Important boundaries:
- no default seeding here
- no chunk generation here
- no command execution here
- no snapshot loading here
- no Event/Command/Object traversal here
- no request handling here
- no Alembic migration execution here

Design rule:

    Runtime startup must not call this module automatically.
    This module is for explicit DB bootstrap only.

Typical flow:

    DB is reachable
    -> models are registered
    -> existing table names are inspected
    -> advisory lock is acquired
    -> db.create_all() is called if enabled
    -> resulting table names are inspected
    -> session is removed
    -> result is returned

This keeps schema creation out of normal Gunicorn worker startup and prevents
parallel CREATE TABLE races.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Final, Mapping, Sequence

try:
    from flask import Flask
except Exception:  # pragma: no cover - partial import environment
    Flask = Any  # type: ignore[misc, assignment]

try:
    from sqlalchemy import inspect, text
except Exception:  # pragma: no cover - partial import environment
    inspect = None  # type: ignore[assignment]
    text = None  # type: ignore[assignment]

try:
    from sqlalchemy.engine import Engine
except Exception:  # pragma: no cover - partial import environment
    Engine = Any  # type: ignore[misc, assignment]

try:
    from extensions import db as default_db
except Exception:  # pragma: no cover - partial import environment
    default_db = None  # type: ignore[assignment]

try:
    from .db_locks import (
        advisory_lock_result_to_dict,
        build_lock_diagnostics,
        safe_session_cleanup,
        schema_bootstrap_lock,
    )
except Exception:  # pragma: no cover - fallback for direct import tests
    advisory_lock_result_to_dict = None  # type: ignore[assignment]
    build_lock_diagnostics = None  # type: ignore[assignment]
    safe_session_cleanup = None  # type: ignore[assignment]
    schema_bootstrap_lock = None  # type: ignore[assignment]

try:
    from .settings import (
        SchemaBootstrapSettings,
        build_bootstrap_settings,
        build_schema_bootstrap_settings,
        get_bool_setting,
    )
except Exception:  # pragma: no cover - fallback for direct import tests
    SchemaBootstrapSettings = Any  # type: ignore[misc, assignment]

    def build_bootstrap_settings(app: Any = None) -> Any:  # type: ignore[override]
        return None

    def build_schema_bootstrap_settings(app: Any = None) -> Any:  # type: ignore[override]
        return None

    def get_bool_setting(
        app: Any,
        key: str,
        default: bool = False,
        aliases: Sequence[str] | None = None,
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


# -----------------------------------------------------------------------------
# Constants
# -----------------------------------------------------------------------------

SCHEMA_BOOTSTRAP_RESULT_VERSION: Final[str] = "schema-bootstrap-result.v1"

DEFAULT_REQUIRED_TABLES: Final[tuple[str, ...]] = (
    "projects",
    "universes",
    "world_instances",
    "block_registries",
    "block_types",
    "chunk_snapshots",
    "world_command_logs",
    "chunk_events",
    "world_object_instances",
    "world_object_chunk_refs",
)

STATUS_COMPLETED: Final[str] = "completed"
STATUS_SKIPPED: Final[str] = "skipped"
STATUS_FAILED: Final[str] = "failed"
STATUS_PARTIAL: Final[str] = "partial"

OP_STATUS_OK: Final[str] = "ok"
OP_STATUS_SKIPPED: Final[str] = "skipped"
OP_STATUS_FAILED: Final[str] = "failed"
OP_STATUS_WARNING: Final[str] = "warning"


# -----------------------------------------------------------------------------
# Data structures
# -----------------------------------------------------------------------------

@dataclass(slots=True)
class SchemaBootstrapMessage:
    """Serializable schema bootstrap warning/error."""

    code: str
    message: str
    timestamp: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class SchemaBootstrapOperation:
    """Serializable schema bootstrap operation result."""

    name: str
    ok: bool
    status: str
    skipped: bool = False
    changed: bool = False
    message: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    duration_ms: int = 0
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class SchemaBootstrapResult:
    """Serializable aggregate schema bootstrap result."""

    ok: bool
    status: str
    result_version: str = SCHEMA_BOOTSTRAP_RESULT_VERSION

    started_at: str | None = None
    completed_at: str | None = None
    duration_ms: int = 0

    enabled: bool = False
    create_all_requested: bool = False
    create_all_executed: bool = False

    database_available: bool | None = None
    model_registry_ready: bool | None = None

    required_tables: list[str] = field(default_factory=list)
    tables_before: list[str] = field(default_factory=list)
    tables_after: list[str] = field(default_factory=list)
    missing_tables_before: list[str] = field(default_factory=list)
    missing_tables_after: list[str] = field(default_factory=list)
    created_tables: list[str] = field(default_factory=list)

    operations: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)

    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Return serializable dict."""
        return asdict(self)


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
    """Return UTC timestamp as ISO string."""
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
    """Normalize value as stripped string."""
    if value is None:
        return default

    try:
        result = str(value).strip()
    except Exception:
        return default

    return result or default


def _safe_bool(value: Any, default: bool = False) -> bool:
    """Normalize value as bool."""
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
    """Normalize mapping-like value to dict."""
    if isinstance(value, dict):
        return value

    if isinstance(value, Mapping):
        try:
            return dict(value)
        except Exception:
            return {}

    return {}


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


def _safe_log_exception(app: Any, message: str, *args: Any) -> None:
    """Exception-log defensively."""
    try:
        app.logger.exception(message, *args)
    except Exception:
        pass


def _make_message(
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create serializable message."""
    return asdict(
        SchemaBootstrapMessage(
            code=_safe_str(code, "schema_bootstrap_message"),
            message=_safe_str(message, ""),
            timestamp=_utc_now_iso(),
            details=details or {},
        )
    )


def _make_operation(
    name: str,
    ok: bool,
    status: str,
    *,
    skipped: bool = False,
    changed: bool = False,
    message: str | None = None,
    started_at: str | None = None,
    completed_at: str | None = None,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create serializable operation result."""
    started_at = started_at or _utc_now_iso()
    completed_at = completed_at or _utc_now_iso()

    return asdict(
        SchemaBootstrapOperation(
            name=_safe_str(name, "operation"),
            ok=bool(ok),
            status=_safe_str(status, OP_STATUS_FAILED),
            skipped=bool(skipped),
            changed=bool(changed),
            message=message,
            started_at=started_at,
            completed_at=completed_at,
            duration_ms=_duration_ms(started_at, completed_at),
            data=data or {},
        )
    )


# -----------------------------------------------------------------------------
# App / DB helpers
# -----------------------------------------------------------------------------

def _is_flask_app(app: object) -> bool:
    """Return whether object is Flask-like."""
    if isinstance(app, Flask):
        return True

    required_attrs = ("extensions", "config", "logger")
    try:
        return all(hasattr(app, attr_name) for attr_name in required_attrs)
    except Exception:
        return False


def _get_db_extension(db_extension: Any = None) -> Any:
    """Return SQLAlchemy extension."""
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


def _get_metadata_tables(db_extension: Any = None) -> list[str]:
    """Return SQLAlchemy metadata table names."""
    db_obj = _get_db_extension(db_extension)

    if db_obj is None:
        return []

    try:
        metadata = getattr(db_obj, "metadata", None)
        if metadata is None:
            return []
        tables = getattr(metadata, "tables", {})
        return sorted(str(name) for name in tables.keys())
    except Exception:
        return []


def _cleanup_db_session(
    *,
    rollback: bool = False,
    db_extension: Any = None,
) -> dict[str, bool]:
    """Cleanup SQLAlchemy session robustly."""
    if safe_session_cleanup is not None:
        try:
            return safe_session_cleanup(
                rollback=rollback,
                remove=True,
                db_extension=db_extension,
            )
        except Exception:
            pass

    db_obj = _get_db_extension(db_extension)
    result = {
        "rollback": False,
        "remove": False,
    }

    if db_obj is None:
        return result

    if rollback:
        try:
            db_obj.session.rollback()
            result["rollback"] = True
        except Exception:
            result["rollback"] = False

    try:
        db_obj.session.remove()
        result["remove"] = True
    except Exception:
        result["remove"] = False

    return result


# -----------------------------------------------------------------------------
# Model registry checks
# -----------------------------------------------------------------------------

def require_schema_models_ready() -> dict[str, Any]:
    """
    Verify model registry readiness without querying product data.

    Returns a compact model summary.
    """
    try:
        from models import get_model_debug_summary, require_models_ready
    except Exception as exc:
        raise RuntimeError(
            f"Could not import model registry helpers: {_safe_exception_message(exc)}"
        ) from exc

    try:
        require_models_ready()
    except Exception as exc:
        raise RuntimeError(
            f"Model registry is not ready: {_safe_exception_message(exc)}"
        ) from exc

    try:
        summary = get_model_debug_summary()
    except Exception:
        summary = {}

    return _safe_dict(summary)


# -----------------------------------------------------------------------------
# Table inspection
# -----------------------------------------------------------------------------

def get_required_table_names(
    *,
    db_extension: Any = None,
    fallback: Sequence[str] | None = None,
) -> list[str]:
    """
    Return required table names.

    Prefer SQLAlchemy metadata tables, fallback to known core table names.
    """
    metadata_tables = _get_metadata_tables(db_extension)
    if metadata_tables:
        return metadata_tables

    return sorted(str(name) for name in (fallback or DEFAULT_REQUIRED_TABLES))


def inspect_existing_tables(
    app: Any = None,
    *,
    db_extension: Any = None,
    schema: str | None = None,
) -> list[str]:
    """Inspect existing DB table names without ORM traversal."""
    engine = _get_engine(app, db_extension)

    if engine is None:
        raise RuntimeError("SQLAlchemy engine is unavailable; cannot inspect database tables.")

    if inspect is None:
        raise RuntimeError("sqlalchemy.inspect is unavailable; cannot inspect database tables.")

    try:
        inspector = inspect(engine)
        table_names = inspector.get_table_names(schema=schema)
        return sorted(str(name) for name in table_names)
    except Exception as exc:
        raise RuntimeError(
            f"Could not inspect database tables: {_safe_exception_message(exc)}"
        ) from exc


def calculate_missing_tables(
    required_tables: Sequence[str],
    existing_tables: Sequence[str],
) -> list[str]:
    """Return required table names missing from existing table list."""
    existing_set = {str(name) for name in existing_tables}
    return sorted(str(name) for name in required_tables if str(name) not in existing_set)


def calculate_created_tables(
    before_tables: Sequence[str],
    after_tables: Sequence[str],
) -> list[str]:
    """Return tables present after but not before."""
    before_set = {str(name) for name in before_tables}
    return sorted(str(name) for name in after_tables if str(name) not in before_set)


def verify_required_tables_exist(
    app: Any = None,
    *,
    db_extension: Any = None,
    required_tables: Sequence[str] | None = None,
    schema: str | None = None,
) -> dict[str, Any]:
    """Verify required tables exist."""
    required = list(required_tables or get_required_table_names(db_extension=db_extension))
    existing = inspect_existing_tables(app, db_extension=db_extension, schema=schema)
    missing = calculate_missing_tables(required, existing)

    return {
        "ok": not missing,
        "requiredTables": required,
        "existingTables": existing,
        "missingTables": missing,
        "schema": schema,
    }


# -----------------------------------------------------------------------------
# Database readiness
# -----------------------------------------------------------------------------

def ping_database(app: Any = None, *, db_extension: Any = None) -> dict[str, Any]:
    """Run a minimal read-only DB ping."""
    engine = _get_engine(app, db_extension)

    result: dict[str, Any] = {
        "ok": False,
        "engineAvailable": engine is not None,
        "connectionChecked": False,
        "connectionOk": None,
        "error": None,
    }

    if engine is None:
        result["connectionOk"] = False
        result["error"] = "SQLAlchemy engine is unavailable."
        return result

    if text is None:
        result["connectionOk"] = False
        result["error"] = "sqlalchemy.text is unavailable."
        return result

    connection = None

    try:
        connection = engine.connect()
        result["connectionChecked"] = True
        connection.execute(text("SELECT 1"))
        result["connectionOk"] = True
        result["ok"] = True
    except Exception as exc:
        result["connectionChecked"] = True
        result["connectionOk"] = False
        result["error"] = _safe_exception_message(exc)
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass

        _cleanup_db_session(rollback=False, db_extension=db_extension)

    return result


# -----------------------------------------------------------------------------
# Schema bootstrap
# -----------------------------------------------------------------------------

def _resolve_schema_settings(
    app: Any = None,
    settings: SchemaBootstrapSettings | None = None,
) -> SchemaBootstrapSettings | Any:
    """Resolve schema bootstrap settings."""
    if settings is not None:
        return settings

    try:
        resolved = build_schema_bootstrap_settings(app)
        if resolved is not None:
            return resolved
    except Exception:
        pass

    try:
        aggregate = build_bootstrap_settings(app)
        resolved = getattr(aggregate, "schema", None)
        if resolved is not None:
            return resolved
    except Exception:
        pass

    class FallbackSchemaSettings:
        bootstrap_enabled = get_bool_setting(
            app,
            "VECTOPLAN_CHUNK_DB_BOOTSTRAP_ENABLED",
            False,
            aliases=("DB_BOOTSTRAP_ENABLED",),
        )
        create_all = get_bool_setting(
            app,
            "VECTOPLAN_CHUNK_DB_BOOTSTRAP_CREATE_ALL",
            False,
            aliases=("DB_BOOTSTRAP_CREATE_ALL",),
        )
        advisory_lock_enabled = get_bool_setting(
            app,
            "VECTOPLAN_CHUNK_DB_BOOTSTRAP_ADVISORY_LOCKS",
            True,
            aliases=("DB_BOOTSTRAP_ADVISORY_LOCKS",),
        )
        fail_on_error = get_bool_setting(
            app,
            "VECTOPLAN_CHUNK_DB_BOOTSTRAP_FAIL_ON_ERROR",
            True,
            aliases=("DB_BOOTSTRAP_FAIL_ON_ERROR",),
        )

    return FallbackSchemaSettings()


def run_create_all(
    app: Any,
    *,
    db_extension: Any = None,
) -> dict[str, Any]:
    """
    Run db.create_all() once.

    Caller is responsible for advisory locking.
    """
    started_at = _utc_now_iso()
    db_obj = _get_db_extension(db_extension)

    if db_obj is None:
        return _make_operation(
            name="create_all",
            ok=False,
            status=OP_STATUS_FAILED,
            message="SQLAlchemy db extension is unavailable.",
            started_at=started_at,
            data={
                "dbExtensionAvailable": False,
            },
        )

    try:
        require_schema_models_ready()
    except Exception as exc:
        return _make_operation(
            name="create_all",
            ok=False,
            status=OP_STATUS_FAILED,
            message=f"Model registry is not ready: {_safe_exception_message(exc)}",
            started_at=started_at,
            data={
                "exceptionType": exc.__class__.__name__,
            },
        )

    try:
        db_obj.create_all()
        return _make_operation(
            name="create_all",
            ok=True,
            status=OP_STATUS_OK,
            changed=True,
            message="db.create_all() completed.",
            started_at=started_at,
            data={
                "dbExtensionAvailable": True,
            },
        )
    except Exception as exc:
        _cleanup_db_session(rollback=True, db_extension=db_extension)
        return _make_operation(
            name="create_all",
            ok=False,
            status=OP_STATUS_FAILED,
            message=f"db.create_all() failed: {_safe_exception_message(exc)}",
            started_at=started_at,
            data={
                "exceptionType": exc.__class__.__name__,
            },
        )
    finally:
        _cleanup_db_session(rollback=False, db_extension=db_extension)


def run_schema_bootstrap(
    app: Flask,
    *,
    settings: SchemaBootstrapSettings | None = None,
    db_extension: Any = None,
    required_tables: Sequence[str] | None = None,
    create_all: bool | None = None,
    enabled: bool | None = None,
    fail_on_error: bool | None = None,
    schema: str | None = None,
) -> SchemaBootstrapResult:
    """
    Run explicit schema bootstrap.

    This is intended for a DB-bootstrap command/container, not normal runtime
    startup.
    """
    started_at = _utc_now_iso()

    result = SchemaBootstrapResult(
        ok=False,
        status=STATUS_FAILED,
        started_at=started_at,
        enabled=False,
        create_all_requested=False,
        required_tables=list(required_tables or DEFAULT_REQUIRED_TABLES),
    )

    if not _is_flask_app(app):
        result.errors.append(
            _make_message(
                code="invalid_flask_app",
                message="run_schema_bootstrap(app) expects a Flask app or compatible object.",
            )
        )
        return _finish_result(result)

    resolved_settings = _resolve_schema_settings(app, settings)

    resolved_enabled = bool(
        enabled
        if enabled is not None
        else getattr(resolved_settings, "bootstrap_enabled", False)
    )
    resolved_create_all = bool(
        create_all
        if create_all is not None
        else getattr(resolved_settings, "create_all", False)
    )
    resolved_fail_on_error = bool(
        fail_on_error
        if fail_on_error is not None
        else getattr(resolved_settings, "fail_on_error", True)
    )
    advisory_lock_enabled = bool(getattr(resolved_settings, "advisory_lock_enabled", True))

    result.enabled = resolved_enabled
    result.create_all_requested = resolved_create_all
    result.metadata["schema"] = schema
    result.metadata["advisoryLockEnabled"] = advisory_lock_enabled
    result.metadata["failOnError"] = resolved_fail_on_error

    try:
        if build_lock_diagnostics is not None:
            result.metadata["lockDiagnostics"] = build_lock_diagnostics(app, db_extension)
    except Exception:
        pass

    if not resolved_enabled:
        result.operations.append(
            _make_operation(
                name="schema_bootstrap",
                ok=True,
                status=OP_STATUS_SKIPPED,
                skipped=True,
                message="Schema bootstrap disabled by settings.",
            )
        )
        result.ok = True
        result.status = STATUS_SKIPPED
        return _finish_result(result)

    _safe_log_info(app, "Schema bootstrap started.")

    # Step 1: DB ping.
    ping = ping_database(app, db_extension=db_extension)
    result.database_available = bool(ping.get("ok"))
    result.operations.append(
        _make_operation(
            name="database_ping",
            ok=bool(ping.get("ok")),
            status=OP_STATUS_OK if ping.get("ok") else OP_STATUS_FAILED,
            message="Database ping completed." if ping.get("ok") else _safe_str(ping.get("error"), "Database ping failed."),
            data=ping,
        )
    )

    if not ping.get("ok"):
        result.errors.append(
            _make_message(
                code="database_ping_failed",
                message=_safe_str(ping.get("error"), "Database ping failed."),
                details=ping,
            )
        )
        return _finish_or_raise(app, result, resolved_fail_on_error)

    # Step 2: Model registry check.
    try:
        model_summary = require_schema_models_ready()
        result.model_registry_ready = True
        result.metadata["modelSummary"] = model_summary
        result.operations.append(
            _make_operation(
                name="model_registry",
                ok=True,
                status=OP_STATUS_OK,
                message="Model registry is ready.",
                data=model_summary,
            )
        )
    except Exception as exc:
        result.model_registry_ready = False
        message = _safe_exception_message(exc)
        result.errors.append(
            _make_message(
                code="model_registry_failed",
                message=message,
                details={
                    "exceptionType": exc.__class__.__name__,
                },
            )
        )
        result.operations.append(
            _make_operation(
                name="model_registry",
                ok=False,
                status=OP_STATUS_FAILED,
                message=message,
                data={
                    "exceptionType": exc.__class__.__name__,
                },
            )
        )
        return _finish_or_raise(app, result, resolved_fail_on_error)

    # Required tables should be based on actual registered metadata if possible.
    result.required_tables = list(
        required_tables or get_required_table_names(db_extension=db_extension)
    )

    # Step 3: Inspect before.
    try:
        result.tables_before = inspect_existing_tables(
            app,
            db_extension=db_extension,
            schema=schema,
        )
        result.missing_tables_before = calculate_missing_tables(
            result.required_tables,
            result.tables_before,
        )
        result.operations.append(
            _make_operation(
                name="inspect_tables_before",
                ok=True,
                status=OP_STATUS_OK,
                message="Existing tables inspected before schema bootstrap.",
                data={
                    "tableCount": len(result.tables_before),
                    "missingRequiredCount": len(result.missing_tables_before),
                    "missingRequiredTables": result.missing_tables_before,
                },
            )
        )
    except Exception as exc:
        message = _safe_exception_message(exc)
        result.errors.append(
            _make_message(
                code="inspect_tables_before_failed",
                message=message,
                details={
                    "exceptionType": exc.__class__.__name__,
                },
            )
        )
        result.operations.append(
            _make_operation(
                name="inspect_tables_before",
                ok=False,
                status=OP_STATUS_FAILED,
                message=message,
                data={
                    "exceptionType": exc.__class__.__name__,
                },
            )
        )
        return _finish_or_raise(app, result, resolved_fail_on_error)

    # Step 4: Optional create_all.
    if not resolved_create_all:
        result.operations.append(
            _make_operation(
                name="create_all",
                ok=True,
                status=OP_STATUS_SKIPPED,
                skipped=True,
                message="create_all disabled by settings.",
            )
        )
    else:
        try:
            if schema_bootstrap_lock is None:
                create_op = run_create_all(app, db_extension=db_extension)
                result.operations.append(create_op)
                result.create_all_executed = bool(create_op.get("ok"))
                if not create_op.get("ok"):
                    result.errors.append(
                        _make_message(
                            code="create_all_failed",
                            message=_safe_str(create_op.get("message"), "create_all failed."),
                            details=create_op,
                        )
                    )
            else:
                with schema_bootstrap_lock(
                    app,
                    enabled=advisory_lock_enabled,
                    db_extension=db_extension,
                    fail_if_not_acquired=True,
                ) as lock_result:
                    if advisory_lock_result_to_dict is not None:
                        lock_data = advisory_lock_result_to_dict(lock_result)
                    else:
                        lock_data = asdict(lock_result)

                    result.operations.append(
                        _make_operation(
                            name="schema_bootstrap_lock",
                            ok=bool(lock_result.ok),
                            status=OP_STATUS_OK if lock_result.ok else OP_STATUS_FAILED,
                            skipped=bool(getattr(lock_result, "skipped", False)),
                            message="Schema bootstrap advisory lock acquired or skipped.",
                            data=lock_data,
                        )
                    )

                    create_op = run_create_all(app, db_extension=db_extension)
                    result.operations.append(create_op)
                    result.create_all_executed = bool(create_op.get("ok"))

                    if not create_op.get("ok"):
                        result.errors.append(
                            _make_message(
                                code="create_all_failed",
                                message=_safe_str(create_op.get("message"), "create_all failed."),
                                details=create_op,
                            )
                        )

        except Exception as exc:
            message = _safe_exception_message(exc)
            result.errors.append(
                _make_message(
                    code="create_all_exception",
                    message=message,
                    details={
                        "exceptionType": exc.__class__.__name__,
                    },
                )
            )
            result.operations.append(
                _make_operation(
                    name="create_all",
                    ok=False,
                    status=OP_STATUS_FAILED,
                    message=message,
                    data={
                        "exceptionType": exc.__class__.__name__,
                    },
                )
            )
            _cleanup_db_session(rollback=True, db_extension=db_extension)

        if result.errors:
            return _finish_or_raise(app, result, resolved_fail_on_error)

    # Step 5: Inspect after.
    try:
        result.tables_after = inspect_existing_tables(
            app,
            db_extension=db_extension,
            schema=schema,
        )
        result.missing_tables_after = calculate_missing_tables(
            result.required_tables,
            result.tables_after,
        )
        result.created_tables = calculate_created_tables(
            result.tables_before,
            result.tables_after,
        )
        result.operations.append(
            _make_operation(
                name="inspect_tables_after",
                ok=True,
                status=OP_STATUS_OK,
                message="Existing tables inspected after schema bootstrap.",
                data={
                    "tableCount": len(result.tables_after),
                    "createdTableCount": len(result.created_tables),
                    "createdTables": result.created_tables,
                    "missingRequiredCount": len(result.missing_tables_after),
                    "missingRequiredTables": result.missing_tables_after,
                },
            )
        )
    except Exception as exc:
        message = _safe_exception_message(exc)
        result.errors.append(
            _make_message(
                code="inspect_tables_after_failed",
                message=message,
                details={
                    "exceptionType": exc.__class__.__name__,
                },
            )
        )
        result.operations.append(
            _make_operation(
                name="inspect_tables_after",
                ok=False,
                status=OP_STATUS_FAILED,
                message=message,
                data={
                    "exceptionType": exc.__class__.__name__,
                },
            )
        )
        return _finish_or_raise(app, result, resolved_fail_on_error)

    # Step 6: Determine final state.
    if result.missing_tables_after:
        message = "Required tables are still missing after schema bootstrap."
        result.errors.append(
            _make_message(
                code="required_tables_missing",
                message=message,
                details={
                    "missingTables": result.missing_tables_after,
                },
            )
        )
        return _finish_or_raise(app, result, resolved_fail_on_error)

    result.ok = True
    result.status = STATUS_COMPLETED
    _safe_log_info(
        app,
        "Schema bootstrap completed successfully. tableCount=%s createdTableCount=%s",
        len(result.tables_after),
        len(result.created_tables),
    )
    return _finish_result(result)


def _finish_result(result: SchemaBootstrapResult) -> SchemaBootstrapResult:
    """Finalize result timestamps/status."""
    result.completed_at = _utc_now_iso()
    result.duration_ms = _duration_ms(result.started_at, result.completed_at)

    if result.errors:
        result.ok = False
        result.status = STATUS_FAILED
    elif result.status not in {STATUS_SKIPPED, STATUS_PARTIAL}:
        result.ok = True
        result.status = STATUS_COMPLETED

    return result


def _finish_or_raise(
    app: Any,
    result: SchemaBootstrapResult,
    fail_on_error: bool,
) -> SchemaBootstrapResult:
    """Finish result and optionally raise."""
    _finish_result(result)

    if fail_on_error and not result.ok:
        first_error = result.errors[0] if result.errors else {}
        message = _safe_str(
            first_error.get("message") if isinstance(first_error, Mapping) else None,
            "Schema bootstrap failed.",
        )
        _safe_log_exception(app, "Schema bootstrap failed: %s", message)
        raise RuntimeError(message)

    if not result.ok:
        _safe_log_warning(app, "Schema bootstrap failed but fail_on_error=false.")

    return result


# -----------------------------------------------------------------------------
# Convenience APIs
# -----------------------------------------------------------------------------

def run_schema_bootstrap_if_enabled(
    app: Flask,
    *,
    db_extension: Any = None,
) -> SchemaBootstrapResult:
    """Run schema bootstrap using configured settings."""
    settings = _resolve_schema_settings(app, None)

    return run_schema_bootstrap(
        app,
        settings=settings,
        db_extension=db_extension,
    )


def build_schema_status(
    app: Any = None,
    *,
    db_extension: Any = None,
    required_tables: Sequence[str] | None = None,
    schema: str | None = None,
) -> dict[str, Any]:
    """
    Build read-only schema status.

    This does not create tables.
    """
    started_at = _utc_now_iso()

    try:
        ping = ping_database(app, db_extension=db_extension)
    except Exception as exc:
        ping = {
            "ok": False,
            "error": _safe_exception_message(exc),
        }

    try:
        required = list(required_tables or get_required_table_names(db_extension=db_extension))
    except Exception:
        required = list(required_tables or DEFAULT_REQUIRED_TABLES)

    existing: list[str] = []
    missing: list[str] = []

    if ping.get("ok"):
        try:
            existing = inspect_existing_tables(
                app,
                db_extension=db_extension,
                schema=schema,
            )
            missing = calculate_missing_tables(required, existing)
        except Exception as exc:
            return {
                "ok": False,
                "status": STATUS_FAILED,
                "startedAt": started_at,
                "completedAt": _utc_now_iso(),
                "database": ping,
                "requiredTables": required,
                "existingTables": existing,
                "missingTables": missing,
                "error": _safe_exception_message(exc),
            }

    completed_at = _utc_now_iso()

    return {
        "ok": bool(ping.get("ok")) and not missing,
        "status": STATUS_COMPLETED if bool(ping.get("ok")) and not missing else STATUS_FAILED,
        "startedAt": started_at,
        "completedAt": completed_at,
        "durationMs": _duration_ms(started_at, completed_at),
        "database": ping,
        "requiredTables": required,
        "existingTables": existing,
        "missingTables": missing,
        "tableCount": len(existing),
        "missingTableCount": len(missing),
        "schema": schema,
    }


def schema_bootstrap_result_to_dict(
    result: SchemaBootstrapResult | Mapping[str, Any] | Any,
) -> dict[str, Any]:
    """Serialize schema bootstrap result to dict."""
    if isinstance(result, SchemaBootstrapResult):
        return result.to_dict()

    if isinstance(result, Mapping):
        try:
            return dict(result)
        except Exception:
            return {}

    return {}


def build_schema_bootstrap_summary(
    result: SchemaBootstrapResult | Mapping[str, Any] | Any,
) -> dict[str, Any]:
    """Build compact schema bootstrap summary."""
    data = schema_bootstrap_result_to_dict(result)

    return {
        "ok": bool(data.get("ok")),
        "status": _safe_str(data.get("status"), "unknown"),
        "enabled": bool(data.get("enabled")),
        "createAllRequested": bool(data.get("create_all_requested")),
        "createAllExecuted": bool(data.get("create_all_executed")),
        "databaseAvailable": data.get("database_available"),
        "modelRegistryReady": data.get("model_registry_ready"),
        "requiredTableCount": len(data.get("required_tables") or []),
        "tableCountBefore": len(data.get("tables_before") or []),
        "tableCountAfter": len(data.get("tables_after") or []),
        "missingTableCountBefore": len(data.get("missing_tables_before") or []),
        "missingTableCountAfter": len(data.get("missing_tables_after") or []),
        "createdTableCount": len(data.get("created_tables") or []),
        "operationCount": len(data.get("operations") or []),
        "warningCount": len(data.get("warnings") or []),
        "errorCount": len(data.get("errors") or []),
        "durationMs": data.get("duration_ms"),
    }


# -----------------------------------------------------------------------------
# Public exports
# -----------------------------------------------------------------------------

__all__ = [
    "DEFAULT_REQUIRED_TABLES",
    "OP_STATUS_FAILED",
    "OP_STATUS_OK",
    "OP_STATUS_SKIPPED",
    "OP_STATUS_WARNING",
    "SCHEMA_BOOTSTRAP_RESULT_VERSION",
    "STATUS_COMPLETED",
    "STATUS_FAILED",
    "STATUS_PARTIAL",
    "STATUS_SKIPPED",
    "SchemaBootstrapMessage",
    "SchemaBootstrapOperation",
    "SchemaBootstrapResult",
    "build_schema_bootstrap_summary",
    "build_schema_status",
    "calculate_created_tables",
    "calculate_missing_tables",
    "get_required_table_names",
    "inspect_existing_tables",
    "ping_database",
    "require_schema_models_ready",
    "run_create_all",
    "run_schema_bootstrap",
    "run_schema_bootstrap_if_enabled",
    "schema_bootstrap_result_to_dict",
    "verify_required_tables_exist",
]