# services/vectoplan-chunk/src/bootstrap/schema_bootstrap.py
"""
Explicit schema bootstrap for the `vectoplan-chunk` service.

This module owns the controlled schema bootstrap path.

Responsibilities:
- verify SQLAlchemy extension availability
- verify model registry availability
- inspect existing database tables and critical columns
- verify Project owner columns and project-access table shapes
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
- no silent ALTER TABLE repair here; missing columns are reported explicitly

Design rule:

    Runtime startup must not call this module automatically.
    This module is for explicit DB bootstrap only.

Typical flow:

    DB is reachable
    -> models are registered
    -> existing table names and critical columns are inspected
    -> advisory lock is acquired
    -> db.create_all() is called if enabled
    -> resulting table names and critical columns are inspected
    -> session is removed
    -> result is returned

This keeps schema creation out of normal Gunicorn worker startup and prevents
parallel CREATE TABLE races.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
from types import MappingProxyType
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

SCHEMA_BOOTSTRAP_RESULT_VERSION: Final[str] = "schema-bootstrap-result.v2"

PROJECT_ACCESS_REQUIRED_TABLES: Final[tuple[str, ...]] = (
    "project_roles",
    "project_groups",
    "project_group_members",
    "project_role_assignments",
)

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
    *PROJECT_ACCESS_REQUIRED_TABLES,
)

PROJECT_OWNER_REQUIRED_COLUMNS: Final[tuple[str, ...]] = (
    "id",
    "project_id",
    "external_app_project_id",
    "source_service",
    "owner_type",
    "owner_id",
    "created_by_user_id",
    "updated_by_user_id",
    "metadata_json",
    "created_at",
    "updated_at",
    "deleted_at",
)

PROJECT_ACCESS_REQUIRED_COLUMNS: Final[Mapping[str, tuple[str, ...]]] = MappingProxyType(
    {
        "project_roles": (
            "id",
            "role_id",
            "project_db_id",
            "role_key",
            "name",
            "description",
            "permissions_json",
            "is_system",
            "status",
            "schema_version",
            "revision",
            "metadata_json",
            "created_by_user_id",
            "updated_by_user_id",
            "created_at",
            "updated_at",
            "deleted_at",
        ),
        "project_groups": (
            "id",
            "group_id",
            "project_db_id",
            "group_key",
            "name",
            "description",
            "is_system",
            "status",
            "schema_version",
            "revision",
            "metadata_json",
            "created_by_user_id",
            "updated_by_user_id",
            "created_at",
            "updated_at",
            "deleted_at",
        ),
        "project_group_members": (
            "id",
            "membership_id",
            "project_db_id",
            "group_db_id",
            "group_id",
            "user_id",
            "status",
            "added_by_user_id",
            "removed_by_user_id",
            "starts_at",
            "expires_at",
            "removed_at",
            "removal_reason",
            "schema_version",
            "revision",
            "metadata_json",
            "created_by_user_id",
            "updated_by_user_id",
            "created_at",
            "updated_at",
            "deleted_at",
        ),
        "project_role_assignments": (
            "id",
            "assignment_id",
            "project_db_id",
            "role_db_id",
            "role_id",
            "subject_type",
            "user_id",
            "group_db_id",
            "group_id",
            "subject_key",
            "permission_overrides_json",
            "status",
            "assigned_by_user_id",
            "revoked_by_user_id",
            "starts_at",
            "expires_at",
            "revoked_at",
            "revocation_reason",
            "schema_version",
            "revision",
            "metadata_json",
            "created_by_user_id",
            "updated_by_user_id",
            "created_at",
            "updated_at",
            "deleted_at",
        ),
    }
)

DEFAULT_REQUIRED_COLUMNS: Final[Mapping[str, tuple[str, ...]]] = MappingProxyType(
    {
        "projects": PROJECT_OWNER_REQUIRED_COLUMNS,
        **dict(PROJECT_ACCESS_REQUIRED_COLUMNS),
    }
)

SCHEMA_CRITICAL_MODEL_CLASSES: Final[tuple[str, ...]] = (
    "Project",
    "ProjectRole",
    "ProjectGroup",
    "ProjectGroupMember",
    "ProjectRoleAssignment",
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
    required_columns: dict[str, list[str]] = field(default_factory=dict)

    tables_before: list[str] = field(default_factory=list)
    tables_after: list[str] = field(default_factory=list)
    missing_tables_before: list[str] = field(default_factory=list)
    missing_tables_after: list[str] = field(default_factory=list)
    created_tables: list[str] = field(default_factory=list)

    columns_before: dict[str, list[str]] = field(default_factory=dict)
    columns_after: dict[str, list[str]] = field(default_factory=dict)
    missing_columns_before: dict[str, list[str]] = field(default_factory=dict)
    missing_columns_after: dict[str, list[str]] = field(default_factory=dict)

    project_owner_columns_ready: bool | None = None
    project_access_tables_ready: bool | None = None
    project_access_columns_ready: bool | None = None
    project_access_schema_ready: bool | None = None

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


@lru_cache(maxsize=128)
def _normalize_name_tuple_cached(values: tuple[str, ...]) -> tuple[str, ...]:
    """Normalize a tuple of schema identifiers without caching DB state."""
    normalized = {
        _safe_str(value, "")
        for value in values
        if _safe_str(value, "")
    }
    return tuple(sorted(normalized))


def _normalize_name_sequence(values: Sequence[Any] | None) -> list[str]:
    """Normalize a sequence of schema identifiers."""
    if not values:
        return []

    raw_values: list[str] = []
    for value in values:
        normalized = _safe_str(value, "")
        if normalized:
            raw_values.append(normalized)

    return list(_normalize_name_tuple_cached(tuple(raw_values)))


def _normalize_required_column_map(
    value: Mapping[str, Sequence[Any]] | None,
    *,
    allowed_tables: Sequence[str] | None = None,
) -> dict[str, list[str]]:
    """Normalize a table -> required column mapping."""
    if not isinstance(value, Mapping):
        return {}

    allowed = set(_normalize_name_sequence(allowed_tables)) if allowed_tables else None
    result: dict[str, list[str]] = {}

    for table_name, column_names in value.items():
        normalized_table = _safe_str(table_name, "")
        if not normalized_table:
            continue
        if allowed is not None and normalized_table not in allowed:
            continue
        if isinstance(column_names, (str, bytes)):
            normalized_columns = _normalize_name_sequence([column_names])
        else:
            try:
                normalized_columns = _normalize_name_sequence(list(column_names or []))
            except Exception:
                normalized_columns = []
        if normalized_columns:
            result[normalized_table] = normalized_columns

    return dict(sorted(result.items()))


def _missing_column_count(value: Mapping[str, Sequence[Any]] | None) -> int:
    """Count missing columns across all tables."""
    if not isinstance(value, Mapping):
        return 0

    total = 0
    for columns in value.values():
        try:
            total += len(list(columns or []))
        except Exception:
            continue
    return total


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
    """Return whether object is Flask-like, including partial test environments."""
    try:
        if Flask is not Any and isinstance(app, Flask):
            return True
    except Exception:
        pass

    required_attrs = ("extensions", "config", "logger")
    try:
        return app is not None and all(
            hasattr(app, attr_name) for attr_name in required_attrs
        )
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

    The check covers normal model registration plus the critical Project owner
    and project-access model shapes required by this schema bootstrap.
    """
    try:
        import models as model_package
    except Exception as exc:
        raise RuntimeError(
            f"Could not import model registry package: {_safe_exception_message(exc)}"
        ) from exc

    require_models_ready = getattr(model_package, "require_models_ready", None)
    if not callable(require_models_ready):
        raise RuntimeError("models.require_models_ready() is unavailable.")

    try:
        require_models_ready()
    except Exception as exc:
        raise RuntimeError(
            f"Model registry is not ready: {_safe_exception_message(exc)}"
        ) from exc

    require_expected_columns = getattr(
        model_package,
        "require_expected_model_columns",
        None,
    )
    if callable(require_expected_columns):
        try:
            require_expected_columns(class_names=SCHEMA_CRITICAL_MODEL_CLASSES)
        except Exception as exc:
            raise RuntimeError(
                "Critical Project/project-access model columns are incomplete: "
                f"{_safe_exception_message(exc)}"
            ) from exc

    get_model_debug_summary = getattr(model_package, "get_model_debug_summary", None)
    try:
        summary = get_model_debug_summary() if callable(get_model_debug_summary) else {}
    except Exception:
        summary = {}

    normalized_summary = _safe_dict(summary)
    available_classes = set(normalized_summary.get("availableClasses") or [])
    missing_critical = [
        class_name
        for class_name in SCHEMA_CRITICAL_MODEL_CLASSES
        if class_name not in available_classes
    ]
    if available_classes and missing_critical:
        raise RuntimeError(
            "Critical schema model classes are unavailable: "
            f"{missing_critical}"
        )

    if normalized_summary.get("projectAccessShapeReady") is False:
        raise RuntimeError("Project-access model shape is not ready.")
    if normalized_summary.get("appIntegrationReady") is False:
        raise RuntimeError("Project app-integration/owner model shape is not ready.")

    normalized_summary.setdefault(
        "schemaCriticalModelClasses",
        list(SCHEMA_CRITICAL_MODEL_CLASSES),
    )
    return normalized_summary


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

    Registered SQLAlchemy metadata is merged with the explicit fallback list.
    The merge prevents a partially imported model package from silently hiding
    required project-access tables from schema diagnostics.
    """
    fallback_tables = _normalize_name_sequence(fallback or DEFAULT_REQUIRED_TABLES)
    metadata_tables = _normalize_name_sequence(_get_metadata_tables(db_extension))
    return _normalize_name_sequence([*fallback_tables, *metadata_tables])


@lru_cache(maxsize=1)
def _registered_required_column_contract_cached() -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Return the code-level critical column contract; never cache DB state."""
    merged: dict[str, set[str]] = {
        table_name: set(column_names)
        for table_name, column_names in DEFAULT_REQUIRED_COLUMNS.items()
    }

    try:
        import models as model_package

        expected_columns = getattr(model_package, "EXPECTED_MODEL_COLUMNS", {})
        class_to_table = getattr(model_package, "MODEL_CLASS_TO_TABLE", {})
        if isinstance(expected_columns, Mapping) and isinstance(class_to_table, Mapping):
            for class_name in SCHEMA_CRITICAL_MODEL_CLASSES:
                table_name = _safe_str(class_to_table.get(class_name), "")
                if table_name not in merged:
                    continue
                columns = expected_columns.get(class_name) or ()
                for column_name in columns:
                    normalized_column = _safe_str(column_name, "")
                    if normalized_column:
                        merged[table_name].add(normalized_column)
    except Exception:
        pass

    return tuple(
        (table_name, tuple(sorted(column_names)))
        for table_name, column_names in sorted(merged.items())
    )


def reset_schema_bootstrap_caches() -> None:
    """Reset pure normalization/model-contract caches; no DB state is cached."""
    _normalize_name_tuple_cached.cache_clear()
    _registered_required_column_contract_cached.cache_clear()


def get_required_column_names(
    *,
    required_tables: Sequence[str] | None = None,
    fallback: Mapping[str, Sequence[Any]] | None = None,
) -> dict[str, list[str]]:
    """Return the critical table -> column contract used by readiness checks."""
    allowed_tables = _normalize_name_sequence(required_tables) if required_tables else None

    if fallback is not None:
        return _normalize_required_column_map(
            fallback,
            allowed_tables=allowed_tables,
        )

    contract = {
        table_name: list(column_names)
        for table_name, column_names in _registered_required_column_contract_cached()
    }
    return _normalize_required_column_map(
        contract,
        allowed_tables=allowed_tables,
    )


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


def inspect_existing_columns(
    app: Any = None,
    *,
    db_extension: Any = None,
    table_names: Sequence[str] | None = None,
    existing_tables: Sequence[str] | None = None,
    schema: str | None = None,
) -> dict[str, list[str]]:
    """Inspect columns for existing target tables without ORM traversal."""
    engine = _get_engine(app, db_extension)
    if engine is None:
        raise RuntimeError("SQLAlchemy engine is unavailable; cannot inspect table columns.")
    if inspect is None:
        raise RuntimeError("sqlalchemy.inspect is unavailable; cannot inspect table columns.")

    normalized_targets = _normalize_name_sequence(table_names or [])
    normalized_existing = set(
        _normalize_name_sequence(
            existing_tables
            if existing_tables is not None
            else inspect_existing_tables(
                app,
                db_extension=db_extension,
                schema=schema,
            )
        )
    )

    result: dict[str, list[str]] = {}
    try:
        inspector = inspect(engine)
    except Exception as exc:
        raise RuntimeError(
            f"Could not create SQLAlchemy inspector: {_safe_exception_message(exc)}"
        ) from exc

    for table_name in normalized_targets:
        if table_name not in normalized_existing:
            continue
        try:
            column_entries = inspector.get_columns(table_name, schema=schema)
            column_names = [
                _safe_str(entry.get("name"), "")
                for entry in column_entries
                if isinstance(entry, Mapping)
            ]
            result[table_name] = _normalize_name_sequence(column_names)
        except Exception as exc:
            raise RuntimeError(
                f"Could not inspect columns for table '{table_name}': "
                f"{_safe_exception_message(exc)}"
            ) from exc

    return dict(sorted(result.items()))


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


def calculate_missing_columns(
    required_columns: Mapping[str, Sequence[Any]],
    existing_columns: Mapping[str, Sequence[Any]],
    *,
    existing_tables: Sequence[str] | None = None,
) -> dict[str, list[str]]:
    """Return missing columns for required tables that physically exist."""
    normalized_required = _normalize_required_column_map(required_columns)
    normalized_existing = _normalize_required_column_map(existing_columns)
    existing_table_set = (
        set(_normalize_name_sequence(existing_tables))
        if existing_tables is not None
        else set(normalized_existing)
    )

    missing: dict[str, list[str]] = {}
    for table_name, required_names in normalized_required.items():
        if table_name not in existing_table_set:
            # Missing tables are reported separately; avoid duplicating every
            # column as an additional error.
            continue
        available = set(normalized_existing.get(table_name, []))
        absent = [name for name in required_names if name not in available]
        if absent:
            missing[table_name] = absent

    return dict(sorted(missing.items()))


def build_schema_readiness_flags(
    *,
    existing_tables: Sequence[str],
    missing_tables: Sequence[str],
    missing_columns: Mapping[str, Sequence[Any]],
) -> dict[str, bool]:
    """Build ownership and project-access schema readiness flags."""
    existing = set(_normalize_name_sequence(existing_tables))
    missing_table_set = set(_normalize_name_sequence(missing_tables))
    normalized_missing_columns = _normalize_required_column_map(missing_columns)

    project_owner_columns_ready = (
        "projects" in existing
        and "projects" not in missing_table_set
        and not normalized_missing_columns.get("projects")
    )
    project_access_tables_ready = all(
        table_name in existing and table_name not in missing_table_set
        for table_name in PROJECT_ACCESS_REQUIRED_TABLES
    )
    project_access_columns_ready = (
        project_access_tables_ready
        and all(
            not normalized_missing_columns.get(table_name)
            for table_name in PROJECT_ACCESS_REQUIRED_TABLES
        )
    )

    return {
        "projectOwnerColumnsReady": project_owner_columns_ready,
        "projectAccessTablesReady": project_access_tables_ready,
        "projectAccessColumnsReady": project_access_columns_ready,
        "projectAccessSchemaReady": (
            project_owner_columns_ready
            and project_access_tables_ready
            and project_access_columns_ready
        ),
    }


def inspect_schema_contract(
    app: Any = None,
    *,
    db_extension: Any = None,
    required_tables: Sequence[str] | None = None,
    required_columns: Mapping[str, Sequence[Any]] | None = None,
    schema: str | None = None,
) -> dict[str, Any]:
    """Inspect required tables and critical columns as one read-only snapshot."""
    resolved_tables = _normalize_name_sequence(
        required_tables or get_required_table_names(db_extension=db_extension)
    )
    resolved_columns = get_required_column_names(
        required_tables=resolved_tables,
        fallback=required_columns,
    )
    existing_tables = inspect_existing_tables(
        app,
        db_extension=db_extension,
        schema=schema,
    )
    missing_tables = calculate_missing_tables(resolved_tables, existing_tables)
    existing_columns = inspect_existing_columns(
        app,
        db_extension=db_extension,
        table_names=list(resolved_columns),
        existing_tables=existing_tables,
        schema=schema,
    )
    missing_columns = calculate_missing_columns(
        resolved_columns,
        existing_columns,
        existing_tables=existing_tables,
    )
    readiness = build_schema_readiness_flags(
        existing_tables=existing_tables,
        missing_tables=missing_tables,
        missing_columns=missing_columns,
    )

    return {
        "ok": not missing_tables and not missing_columns,
        "schema": schema,
        "requiredTables": resolved_tables,
        "requiredColumns": resolved_columns,
        "existingTables": existing_tables,
        "existingColumns": existing_columns,
        "missingTables": missing_tables,
        "missingColumns": missing_columns,
        "missingTableCount": len(missing_tables),
        "missingColumnCount": _missing_column_count(missing_columns),
        **readiness,
    }


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


def verify_required_columns_exist(
    app: Any = None,
    *,
    db_extension: Any = None,
    required_tables: Sequence[str] | None = None,
    required_columns: Mapping[str, Sequence[Any]] | None = None,
    schema: str | None = None,
) -> dict[str, Any]:
    """Verify critical columns for existing required tables."""
    snapshot = inspect_schema_contract(
        app,
        db_extension=db_extension,
        required_tables=required_tables,
        required_columns=required_columns,
        schema=schema,
    )
    return {
        "ok": not snapshot["missingColumns"] and not snapshot["missingTables"],
        "requiredColumns": snapshot["requiredColumns"],
        "existingColumns": snapshot["existingColumns"],
        "missingColumns": snapshot["missingColumns"],
        "missingColumnCount": snapshot["missingColumnCount"],
        "missingTables": snapshot["missingTables"],
        "schema": schema,
        "projectOwnerColumnsReady": snapshot["projectOwnerColumnsReady"],
        "projectAccessColumnsReady": snapshot["projectAccessColumnsReady"],
    }


def verify_schema_contract(
    app: Any = None,
    *,
    db_extension: Any = None,
    required_tables: Sequence[str] | None = None,
    required_columns: Mapping[str, Sequence[Any]] | None = None,
    schema: str | None = None,
) -> dict[str, Any]:
    """Verify the full required table/critical-column schema contract."""
    return inspect_schema_contract(
        app,
        db_extension=db_extension,
        required_tables=required_tables,
        required_columns=required_columns,
        schema=schema,
    )


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
    required_columns: Mapping[str, Sequence[Any]] | None = None,
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
        required_tables=_normalize_name_sequence(required_tables or DEFAULT_REQUIRED_TABLES),
        required_columns=get_required_column_names(
            required_tables=required_tables or DEFAULT_REQUIRED_TABLES,
            fallback=required_columns,
        ),
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

    # Required tables use registered metadata plus the explicit access fallback.
    result.required_tables = _normalize_name_sequence(
        required_tables or get_required_table_names(db_extension=db_extension)
    )
    result.required_columns = get_required_column_names(
        required_tables=result.required_tables,
        fallback=required_columns,
    )

    # Step 3: Inspect required tables and critical columns before.
    try:
        before_snapshot = inspect_schema_contract(
            app,
            db_extension=db_extension,
            required_tables=result.required_tables,
            required_columns=result.required_columns,
            schema=schema,
        )
        result.tables_before = list(before_snapshot["existingTables"])
        result.missing_tables_before = list(before_snapshot["missingTables"])
        result.columns_before = dict(before_snapshot["existingColumns"])
        result.missing_columns_before = dict(before_snapshot["missingColumns"])
        result.operations.append(
            _make_operation(
                name="inspect_schema_before",
                ok=True,
                status=OP_STATUS_OK,
                message="Tables and critical columns inspected before schema bootstrap.",
                data={
                    "tableCount": len(result.tables_before),
                    "missingRequiredTableCount": len(result.missing_tables_before),
                    "missingRequiredTables": result.missing_tables_before,
                    "missingRequiredColumnCount": _missing_column_count(
                        result.missing_columns_before
                    ),
                    "missingRequiredColumns": result.missing_columns_before,
                    "projectOwnerColumnsReady": before_snapshot[
                        "projectOwnerColumnsReady"
                    ],
                    "projectAccessSchemaReady": before_snapshot[
                        "projectAccessSchemaReady"
                    ],
                },
            )
        )
    except Exception as exc:
        message = _safe_exception_message(exc)
        result.errors.append(
            _make_message(
                code="inspect_schema_before_failed",
                message=message,
                details={"exceptionType": exc.__class__.__name__},
            )
        )
        result.operations.append(
            _make_operation(
                name="inspect_schema_before",
                ok=False,
                status=OP_STATUS_FAILED,
                message=message,
                data={"exceptionType": exc.__class__.__name__},
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

    # Step 5: Inspect tables and critical columns after.
    try:
        after_snapshot = inspect_schema_contract(
            app,
            db_extension=db_extension,
            required_tables=result.required_tables,
            required_columns=result.required_columns,
            schema=schema,
        )
        result.tables_after = list(after_snapshot["existingTables"])
        result.missing_tables_after = list(after_snapshot["missingTables"])
        result.columns_after = dict(after_snapshot["existingColumns"])
        result.missing_columns_after = dict(after_snapshot["missingColumns"])
        result.created_tables = calculate_created_tables(
            result.tables_before,
            result.tables_after,
        )
        result.project_owner_columns_ready = bool(
            after_snapshot["projectOwnerColumnsReady"]
        )
        result.project_access_tables_ready = bool(
            after_snapshot["projectAccessTablesReady"]
        )
        result.project_access_columns_ready = bool(
            after_snapshot["projectAccessColumnsReady"]
        )
        result.project_access_schema_ready = bool(
            after_snapshot["projectAccessSchemaReady"]
        )
        result.operations.append(
            _make_operation(
                name="inspect_schema_after",
                ok=True,
                status=OP_STATUS_OK,
                message="Tables and critical columns inspected after schema bootstrap.",
                data={
                    "tableCount": len(result.tables_after),
                    "createdTableCount": len(result.created_tables),
                    "createdTables": result.created_tables,
                    "missingRequiredTableCount": len(result.missing_tables_after),
                    "missingRequiredTables": result.missing_tables_after,
                    "missingRequiredColumnCount": _missing_column_count(
                        result.missing_columns_after
                    ),
                    "missingRequiredColumns": result.missing_columns_after,
                    "projectOwnerColumnsReady": result.project_owner_columns_ready,
                    "projectAccessTablesReady": result.project_access_tables_ready,
                    "projectAccessColumnsReady": result.project_access_columns_ready,
                    "projectAccessSchemaReady": result.project_access_schema_ready,
                },
            )
        )
    except Exception as exc:
        message = _safe_exception_message(exc)
        result.errors.append(
            _make_message(
                code="inspect_schema_after_failed",
                message=message,
                details={"exceptionType": exc.__class__.__name__},
            )
        )
        result.operations.append(
            _make_operation(
                name="inspect_schema_after",
                ok=False,
                status=OP_STATUS_FAILED,
                message=message,
                data={"exceptionType": exc.__class__.__name__},
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
                details={"missingTables": result.missing_tables_after},
            )
        )
        return _finish_or_raise(app, result, resolved_fail_on_error)

    if result.missing_columns_after:
        message = (
            "Required columns are missing after schema bootstrap. "
            "db.create_all() creates missing tables but does not alter existing "
            "tables; run the explicit missing-column repair or a migration."
        )
        result.errors.append(
            _make_message(
                code="required_columns_missing",
                message=message,
                details={
                    "missingColumns": result.missing_columns_after,
                    "missingColumnCount": _missing_column_count(
                        result.missing_columns_after
                    ),
                    "createAllExecuted": result.create_all_executed,
                    "requiresMigrationOrRepair": True,
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
    required_columns: Mapping[str, Sequence[Any]] | None = None,
    schema: str | None = None,
) -> dict[str, Any]:
    """
    Build read-only table/column schema status.

    This function never creates or repairs schema objects.
    """
    started_at = _utc_now_iso()

    try:
        ping = ping_database(app, db_extension=db_extension)
    except Exception as exc:
        ping = {"ok": False, "error": _safe_exception_message(exc)}

    model_summary: dict[str, Any] = {}
    model_registry_ready = False
    try:
        model_summary = require_schema_models_ready()
        model_registry_ready = True
    except Exception as exc:
        model_summary = {
            "ready": False,
            "error": _safe_exception_message(exc),
            "exceptionType": exc.__class__.__name__,
        }

    resolved_tables = _normalize_name_sequence(
        required_tables or get_required_table_names(db_extension=db_extension)
    )
    resolved_columns = get_required_column_names(
        required_tables=resolved_tables,
        fallback=required_columns,
    )

    snapshot: dict[str, Any] = {
        "existingTables": [],
        "existingColumns": {},
        "missingTables": resolved_tables,
        "missingColumns": {},
        "missingTableCount": len(resolved_tables),
        "missingColumnCount": 0,
        "projectOwnerColumnsReady": False,
        "projectAccessTablesReady": False,
        "projectAccessColumnsReady": False,
        "projectAccessSchemaReady": False,
    }
    inspection_error: str | None = None

    if ping.get("ok"):
        try:
            snapshot = inspect_schema_contract(
                app,
                db_extension=db_extension,
                required_tables=resolved_tables,
                required_columns=resolved_columns,
                schema=schema,
            )
        except Exception as exc:
            inspection_error = _safe_exception_message(exc)

    completed_at = _utc_now_iso()
    ok = (
        bool(ping.get("ok"))
        and model_registry_ready
        and inspection_error is None
        and not snapshot.get("missingTables")
        and not snapshot.get("missingColumns")
    )

    return {
        "ok": ok,
        "status": STATUS_COMPLETED if ok else STATUS_FAILED,
        "resultVersion": SCHEMA_BOOTSTRAP_RESULT_VERSION,
        "startedAt": started_at,
        "completedAt": completed_at,
        "durationMs": _duration_ms(started_at, completed_at),
        "database": ping,
        "modelRegistryReady": model_registry_ready,
        "modelSummary": model_summary,
        "requiredTables": resolved_tables,
        "requiredColumns": resolved_columns,
        "existingTables": snapshot.get("existingTables", []),
        "existingColumns": snapshot.get("existingColumns", {}),
        "missingTables": snapshot.get("missingTables", []),
        "missingColumns": snapshot.get("missingColumns", {}),
        "tableCount": len(snapshot.get("existingTables") or []),
        "missingTableCount": len(snapshot.get("missingTables") or []),
        "missingColumnCount": _missing_column_count(
            snapshot.get("missingColumns") or {}
        ),
        "projectOwnerColumnsReady": bool(
            snapshot.get("projectOwnerColumnsReady")
        ),
        "projectAccessTablesReady": bool(
            snapshot.get("projectAccessTablesReady")
        ),
        "projectAccessColumnsReady": bool(
            snapshot.get("projectAccessColumnsReady")
        ),
        "projectAccessSchemaReady": bool(
            snapshot.get("projectAccessSchemaReady")
        ),
        "schema": schema,
        "error": inspection_error,
        "mutated": False,
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
        "requiredColumnTableCount": len(data.get("required_columns") or {}),
        "tableCountBefore": len(data.get("tables_before") or []),
        "tableCountAfter": len(data.get("tables_after") or []),
        "missingTableCountBefore": len(data.get("missing_tables_before") or []),
        "missingTableCountAfter": len(data.get("missing_tables_after") or []),
        "missingColumnCountBefore": _missing_column_count(
            data.get("missing_columns_before") or {}
        ),
        "missingColumnCountAfter": _missing_column_count(
            data.get("missing_columns_after") or {}
        ),
        "createdTableCount": len(data.get("created_tables") or []),
        "projectOwnerColumnsReady": data.get("project_owner_columns_ready"),
        "projectAccessTablesReady": data.get("project_access_tables_ready"),
        "projectAccessColumnsReady": data.get("project_access_columns_ready"),
        "projectAccessSchemaReady": data.get("project_access_schema_ready"),
        "operationCount": len(data.get("operations") or []),
        "warningCount": len(data.get("warnings") or []),
        "errorCount": len(data.get("errors") or []),
        "durationMs": data.get("duration_ms"),
    }


# -----------------------------------------------------------------------------
# Public exports
# -----------------------------------------------------------------------------

__all__ = [
    "DEFAULT_REQUIRED_COLUMNS",
    "DEFAULT_REQUIRED_TABLES",
    "PROJECT_ACCESS_REQUIRED_COLUMNS",
    "PROJECT_ACCESS_REQUIRED_TABLES",
    "PROJECT_OWNER_REQUIRED_COLUMNS",
    "SCHEMA_CRITICAL_MODEL_CLASSES",
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
    "calculate_missing_columns",
    "calculate_missing_tables",
    "build_schema_readiness_flags",
    "get_required_column_names",
    "get_required_table_names",
    "inspect_existing_columns",
    "inspect_existing_tables",
    "inspect_schema_contract",
    "ping_database",
    "require_schema_models_ready",
    "reset_schema_bootstrap_caches",
    "run_create_all",
    "run_schema_bootstrap",
    "run_schema_bootstrap_if_enabled",
    "schema_bootstrap_result_to_dict",
    "verify_required_columns_exist",
    "verify_required_tables_exist",
    "verify_schema_contract",
]