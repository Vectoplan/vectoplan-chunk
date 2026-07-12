# services/vectoplan-chunk/src/bootstrap/default_seed.py
"""
Explicit default seed bootstrap for the `vectoplan-chunk` service.

This module owns the controlled default seed path.

Responsibilities:
- seed the default development Project,
- seed the default development Universe,
- seed the default editable WorldInstance,
- seed the default runtime BlockRegistry,
- optionally seed the default debug BlockType entries,
- reconcile built-in system blocks into the runtime BlockRegistry,
- enforce the Air persistence invariant,
- keep seeding idempotent,
- repair partial default seed state,
- avoid loading chunks, snapshots, events, commands or object refs,
- protect seed operations with PostgreSQL advisory locks,
- cleanup SQLAlchemy sessions after seed work,
- return serializable results for scripts/logs/status output.

Important boundaries:
- no db.create_all() here,
- no Alembic migrations here,
- no chunk generation here,
- no ChunkSnapshot reads here,
- no ChunkEvent reads here,
- no WorldCommandLog reads here,
- no WorldObjectInstance reads here,
- no WorldObjectChunkRef reads here,
- no request handling here.

Design rule:

    Runtime startup must not call this module automatically.
    This module is for explicit DB bootstrap only.

Target default graph:

    Project(project_id="dev-project")
      -> Universe(universe_id="dev-universe")
          -> WorldInstance(world_id="world_spawn", provider_world_id="flat")

Target default block registry:

    BlockRegistry(registry_id="debug-blocks", registry_version="1")
      -> BlockType(block_type_id="debug_grass")       # optional debug seed
      -> BlockType(block_type_id="debug_dirt")        # optional debug seed
      -> BlockType(block_type_id="system_railing")    # required system mirror

Air invariant:

    cellValue = 0
    system_air must not exist as a BlockType row.

World-id rule:

    world_spawn = concrete editable WorldInstance.
    flat        = provider/template id only.
"""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any, Final, Mapping, Sequence

try:
    from flask import Flask, has_app_context
except Exception:  # pragma: no cover - partial import environment
    Flask = Any  # type: ignore[misc, assignment]

    def has_app_context() -> bool:  # type: ignore[no-redef]
        return False

try:
    from extensions import db as default_db
except Exception:  # pragma: no cover - partial import environment
    default_db = None  # type: ignore[assignment]

try:
    from .db_locks import (
        advisory_lock_result_to_dict,
        build_lock_diagnostics,
        safe_session_cleanup,
        seed_bootstrap_lock,
    )
except Exception:  # pragma: no cover - fallback for direct import tests
    advisory_lock_result_to_dict = None  # type: ignore[assignment]
    build_lock_diagnostics = None  # type: ignore[assignment]
    safe_session_cleanup = None  # type: ignore[assignment]
    seed_bootstrap_lock = None  # type: ignore[assignment]

try:
    from .settings import (
        BlockDefaultsSettings,
        SeedBootstrapSettings,
        WorldDefaultsSettings,
        build_block_defaults_settings,
        build_bootstrap_settings,
        build_seed_bootstrap_settings,
        build_world_defaults_settings,
        get_bool_setting,
    )
except Exception:  # pragma: no cover - fallback for direct import tests
    BlockDefaultsSettings = Any  # type: ignore[misc, assignment]
    SeedBootstrapSettings = Any  # type: ignore[misc, assignment]
    WorldDefaultsSettings = Any  # type: ignore[misc, assignment]

    def build_bootstrap_settings(app: Any = None) -> Any:  # type: ignore[override]
        return None

    def build_seed_bootstrap_settings(app: Any = None) -> Any:  # type: ignore[override]
        return None

    def build_world_defaults_settings(app: Any = None) -> Any:  # type: ignore[override]
        return None

    def build_block_defaults_settings(app: Any = None) -> Any:  # type: ignore[override]
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

DEFAULT_SEED_RESULT_VERSION: Final[str] = "default-seed-result.v3"

DEFAULT_PROJECT_ID: Final[str] = "dev-project"
DEFAULT_PROJECT_SLUG: Final[str] = "dev-project"
DEFAULT_PROJECT_NAME: Final[str] = "Dev Project"

DEFAULT_UNIVERSE_ID: Final[str] = "dev-universe"
DEFAULT_UNIVERSE_SLUG: Final[str] = "dev-universe"
DEFAULT_UNIVERSE_NAME: Final[str] = "Dev Universe"

DEFAULT_WORLD_ID: Final[str] = "world_spawn"
DEFAULT_WORLD_SLUG: Final[str] = "spawn"
DEFAULT_WORLD_NAME: Final[str] = "Flat Spawn World"

DEFAULT_TEMPLATE_ID: Final[str] = "flat"
DEFAULT_PROVIDER_ID: Final[str] = "flat"
DEFAULT_PROVIDER_WORLD_ID: Final[str] = "flat"

DEFAULT_BLOCK_REGISTRY_ID: Final[str] = "debug-blocks"
DEFAULT_BLOCK_REGISTRY_VERSION: Final[str] = "1"

DEFAULT_SYSTEM_BLOCK_BOOTSTRAP_USER_ID: Final[str] = "bootstrap"
DEFAULT_SYSTEM_RAILING_BLOCK_TYPE_ID: Final[str] = "system_railing"
DEFAULT_SYSTEM_AIR_BLOCK_ID: Final[str] = "system_air"

DEFAULT_GENERATOR_TYPE: Final[str] = "flat-world"
DEFAULT_GENERATOR_VERSION: Final[str] = "1"
DEFAULT_PROJECTION_TYPE: Final[str] = "flat-local-v1"
DEFAULT_TOPOLOGY_TYPE: Final[str] = "flat-unbounded-v1"
DEFAULT_COORDINATE_SYSTEM: Final[str] = "vectoplan-world-y-up-v1"

DEFAULT_CHUNK_SIZE: Final[int] = 16
DEFAULT_CELL_SIZE: Final[float] = 1.0
DEFAULT_SURFACE_Y: Final[int] = 0
DEFAULT_MIN_Y: Final[int] = -8
DEFAULT_MAX_Y: Final[int] = 64
DEFAULT_SEED: Final[str] = "dev-seed"

DEFAULT_SPAWN_X: Final[int] = 0
DEFAULT_SPAWN_Y: Final[int] = 2
DEFAULT_SPAWN_Z: Final[int] = 0
DEFAULT_SPAWN_YAW: Final[float] = 0.0
DEFAULT_SPAWN_PITCH: Final[float] = 0.0

STATUS_COMPLETED: Final[str] = "completed"
STATUS_SKIPPED: Final[str] = "skipped"
STATUS_FAILED: Final[str] = "failed"
STATUS_PARTIAL: Final[str] = "partial"
STATUS_READY: Final[str] = "ready"

OP_STATUS_OK: Final[str] = "ok"
OP_STATUS_SKIPPED: Final[str] = "skipped"
OP_STATUS_FAILED: Final[str] = "failed"
OP_STATUS_WARNING: Final[str] = "warning"


# -----------------------------------------------------------------------------
# Data structures
# -----------------------------------------------------------------------------

@dataclass(slots=True)
class DefaultSeedMessage:
    """Serializable default seed warning/error."""

    code: str
    message: str
    timestamp: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class DefaultSeedOperation:
    """Serializable default seed operation result."""

    name: str
    ok: bool
    status: str
    created: bool = False
    updated: bool = False
    skipped: bool = False
    changed: bool = False
    message: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    duration_ms: int = 0
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class DefaultSeedResult:
    """Serializable aggregate default seed result."""

    ok: bool
    status: str
    result_version: str = DEFAULT_SEED_RESULT_VERSION

    started_at: str | None = None
    completed_at: str | None = None
    duration_ms: int = 0

    enabled: bool = False
    seed_defaults_requested: bool = False
    seed_debug_blocks_requested: bool = False
    seed_system_blocks_requested: bool = False
    seed_dev_project_requested: bool = False
    seed_on_empty_only: bool = True

    lock_used: bool = False
    seed_skipped_because_complete: bool = False

    project_id: str | None = None
    universe_id: str | None = None
    world_id: str | None = None
    template_id: str | None = None
    provider_id: str | None = None
    provider_world_id: str | None = None
    block_registry_id: str | None = None
    block_registry_version: str | None = None

    default_project_ready: bool | None = None
    default_universe_ready: bool | None = None
    default_world_ready: bool | None = None
    block_registry_ready: bool | None = None
    debug_blocks_ready: bool | None = None

    system_blocks_ready: bool | None = None
    system_railing_ready: bool | None = None
    air_invariant_ready: bool | None = None

    system_block_count: int = 0
    system_blocks_created: int = 0
    system_blocks_updated: int = 0
    system_blocks_missing: int = 0

    operations: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)

    pre_status: dict[str, Any] = field(default_factory=dict)
    post_status: dict[str, Any] = field(default_factory=dict)
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
    """Normalize value as stripped string."""
    if value is None:
        return default

    try:
        result = str(value).strip()
    except Exception:
        return default

    return result or default


def _safe_int(value: Any, default: int = 0, minimum: int | None = None) -> int:
    """Normalize value as int."""
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


def _safe_float(value: Any, default: float = 0.0, minimum: float | None = None) -> float:
    """Normalize value as float."""
    try:
        result = float(value)
    except Exception:
        result = default

    if minimum is not None:
        try:
            result = max(minimum, result)
        except Exception:
            result = minimum

    return result


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

    try:
        if is_dataclass(value):
            return asdict(value)
    except Exception:
        pass

    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        try:
            result = to_dict()
            if isinstance(result, dict):
                return result
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
    """Create serializable seed message."""
    return asdict(
        DefaultSeedMessage(
            code=_safe_str(code, "default_seed_message"),
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
    created: bool = False,
    updated: bool = False,
    skipped: bool = False,
    changed: bool = False,
    message: str | None = None,
    started_at: str | None = None,
    completed_at: str | None = None,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create serializable seed operation."""
    started_at = started_at or _utc_now_iso()
    completed_at = completed_at or _utc_now_iso()

    if changed is False:
        changed = bool(created or updated)

    return asdict(
        DefaultSeedOperation(
            name=_safe_str(name, "operation"),
            ok=bool(ok),
            status=_safe_str(status, OP_STATUS_FAILED),
            created=bool(created),
            updated=bool(updated),
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
# App / DB / model helpers
# -----------------------------------------------------------------------------

def _is_flask_app(app: object) -> bool:
    """Return whether object is Flask-like."""
    try:
        if isinstance(app, Flask):
            return True
    except Exception:
        pass

    required_attrs = ("extensions", "config", "logger")
    try:
        return all(hasattr(app, attr_name) for attr_name in required_attrs)
    except Exception:
        return False


def _app_context(app: Any) -> Any:
    """Return app context when no context is active."""
    try:
        if has_app_context():
            return nullcontext()
    except Exception:
        return nullcontext()

    try:
        return app.app_context()
    except Exception:
        return nullcontext()


def _get_db_extension(db_extension: Any = None) -> Any:
    """Return SQLAlchemy extension."""
    if db_extension is not None:
        return db_extension
    return default_db


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


def _flush_session(db_extension: Any = None) -> None:
    """Flush SQLAlchemy session."""
    db_obj = _get_db_extension(db_extension)
    if db_obj is None:
        raise RuntimeError("SQLAlchemy db extension is unavailable.")
    db_obj.session.flush()


def _commit_session(db_extension: Any = None) -> None:
    """Commit SQLAlchemy session."""
    db_obj = _get_db_extension(db_extension)
    if db_obj is None:
        raise RuntimeError("SQLAlchemy db extension is unavailable.")
    db_obj.session.commit()


def _add_to_session(obj: Any, db_extension: Any = None) -> None:
    """Add object to SQLAlchemy session."""
    db_obj = _get_db_extension(db_extension)
    if db_obj is None:
        raise RuntimeError("SQLAlchemy db extension is unavailable.")
    db_obj.session.add(obj)


def _get_model_column_names(model_class: Any) -> set[str]:
    """Return SQLAlchemy model column names."""
    try:
        table = getattr(model_class, "__table__", None)
        columns = getattr(table, "columns", None)
        if columns is None:
            return set()
        return {str(column.name) for column in columns}
    except Exception:
        return set()


def _model_supports_column(model_class: Any, column_name: str) -> bool:
    """Return whether model supports a column."""
    return column_name in _get_model_column_names(model_class)


def _object_supports_attr_or_column(obj: Any, name: str) -> bool:
    """Return whether an object can receive an attribute assignment."""
    if obj is None:
        return False

    try:
        if hasattr(obj, name):
            return True
    except Exception:
        pass

    try:
        return _model_supports_column(obj.__class__, name)
    except Exception:
        return False


def _set_attr_if_supported(
    obj: Any,
    name: str,
    value: Any,
    *,
    overwrite: bool = True,
) -> bool:
    """Set attribute if object supports it. Return whether value changed."""
    if not _object_supports_attr_or_column(obj, name):
        return False

    try:
        current_value = getattr(obj, name, None)
    except Exception:
        current_value = None

    if not overwrite and current_value not in (None, "", {}, []):
        return False

    if current_value == value:
        return False

    try:
        setattr(obj, name, value)
        return True
    except Exception:
        return False


def _call_if_available(obj: Any, method_name: str, *args: Any, **kwargs: Any) -> bool:
    """Call method if available."""
    try:
        method = getattr(obj, method_name, None)
    except Exception:
        method = None

    if not callable(method):
        return False

    try:
        method(*args, **kwargs)
        return True
    except Exception:
        return False


def _merge_metadata_json(obj: Any, values: Mapping[str, Any]) -> bool:
    """Merge values into metadata_json if supported."""
    if not _object_supports_attr_or_column(obj, "metadata_json"):
        return False

    try:
        existing = getattr(obj, "metadata_json", None)
    except Exception:
        existing = None

    if isinstance(existing, Mapping):
        current = dict(existing)
    else:
        current = {}

    changed = False

    for key, value in values.items():
        safe_key = _safe_str(key, "")
        if not safe_key:
            continue
        if current.get(safe_key) != value:
            current[safe_key] = value
            changed = True

    if not changed:
        return False

    try:
        setattr(obj, "metadata_json", current)
        return True
    except Exception:
        return False


def _instantiate_model(model_class: Any, values: dict[str, Any]) -> Any:
    """
    Instantiate SQLAlchemy model robustly.

    Prefer kwargs filtered by declared columns; fallback to empty instance and
    setattr.
    """
    column_names = _get_model_column_names(model_class)
    filtered = {
        key: value
        for key, value in values.items()
        if not column_names or key in column_names
    }

    try:
        return model_class(**filtered)
    except Exception:
        pass

    try:
        obj = model_class()
    except Exception as exc:
        raise RuntimeError(
            f"Could not instantiate model {model_class!r}: {_safe_exception_message(exc)}"
        ) from exc

    for key, value in filtered.items():
        _set_attr_if_supported(obj, key, value, overwrite=True)

    return obj


def _query_one_by(model_class: Any, **filters: Any) -> Any | None:
    """Run one_or_none query by supported filters."""
    if model_class is None:
        return None

    supported_filters = {
        key: value
        for key, value in filters.items()
        if value is not None and _model_supports_column(model_class, key)
    }

    if not supported_filters:
        return None

    try:
        query = model_class.query
        for key, value in supported_filters.items():
            query = query.filter(getattr(model_class, key) == value)
        return query.one_or_none()
    except Exception as exc:
        raise RuntimeError(
            f"Could not query {getattr(model_class, '__name__', model_class)} by "
            f"{supported_filters}: {_safe_exception_message(exc)}"
        ) from exc


def _query_first_by(model_class: Any, **filters: Any) -> Any | None:
    """Run first query by supported filters."""
    if model_class is None:
        return None

    supported_filters = {
        key: value
        for key, value in filters.items()
        if value is not None and _model_supports_column(model_class, key)
    }

    if not supported_filters:
        return None

    try:
        query = model_class.query
        for key, value in supported_filters.items():
            query = query.filter(getattr(model_class, key) == value)
        return query.first()
    except Exception:
        try:
            return model_class.query.filter_by(**supported_filters).first()
        except Exception:
            return None


def _exists_by(model_class: Any, **filters: Any) -> bool:
    """Return whether a row exists for filter."""
    try:
        return _query_first_by(model_class, **filters) is not None
    except Exception:
        return False


def _safe_model_id(obj: Any) -> Any:
    """Return model primary key id if present."""
    try:
        return getattr(obj, "id", None)
    except Exception:
        return None


def load_seed_model_classes() -> dict[str, Any]:
    """Load required model classes lazily from model registry."""
    try:
        from models import require_model_class, require_models_ready

        try:
            require_models_ready()
        except Exception as exc:
            raise RuntimeError(
                f"Model registry is not ready: {_safe_exception_message(exc)}"
            ) from exc

        model_names = (
            "Project",
            "Universe",
            "WorldInstance",
            "BlockRegistry",
            "BlockType",
        )

        result: dict[str, Any] = {}

        for model_name in model_names:
            try:
                result[model_name] = require_model_class(model_name)
            except Exception as exc:
                raise RuntimeError(
                    f"Required seed model class is unavailable: {model_name}: "
                    f"{_safe_exception_message(exc)}"
                ) from exc

        return result

    except Exception:
        try:
            from models import BlockRegistry, BlockType, Project, Universe, WorldInstance

            return {
                "Project": Project,
                "Universe": Universe,
                "WorldInstance": WorldInstance,
                "BlockRegistry": BlockRegistry,
                "BlockType": BlockType,
            }
        except Exception as exc:
            raise RuntimeError(
                f"Could not import seed model classes: {_safe_exception_message(exc)}"
            ) from exc


# -----------------------------------------------------------------------------
# Built-in system-block bootstrap adapter
# -----------------------------------------------------------------------------

@lru_cache(maxsize=1)
def load_system_block_bootstrap_api() -> Mapping[str, Any]:
    """
    Load the system-block bootstrap API lazily.

    The default seed module remains importable in partial migration/test
    environments where the new system-block package may not yet be available.
    A real seed run still fails clearly when the required API cannot be loaded.

    Successful resolution is cached because these exports are immutable process
    references. Database rows and query results are never cached here.
    """
    import_errors: list[str] = []

    candidates = (
        "src.system_blocks.bootstrap",
        "system_blocks.bootstrap",
    )

    module = None

    for import_path in candidates:
        try:
            module = __import__(
                import_path,
                fromlist=(
                    "build_system_block_bootstrap_status_for_registry",
                    "ensure_system_blocks_for_registry",
                    "get_default_system_block_bootstrap_policy",
                    "get_read_only_system_block_bootstrap_policy",
                ),
            )
            break
        except Exception as exc:
            import_errors.append(
                f"{import_path}: {exc.__class__.__name__}: "
                f"{_safe_exception_message(exc)}"
            )

    if module is None:
        raise RuntimeError(
            "Could not import the system-block bootstrap API. "
            + " | ".join(import_errors)
        )

    required_exports = (
        "build_system_block_bootstrap_status_for_registry",
        "ensure_system_blocks_for_registry",
        "get_default_system_block_bootstrap_policy",
        "get_read_only_system_block_bootstrap_policy",
    )

    exports: dict[str, Any] = {}

    for export_name in required_exports:
        try:
            value = getattr(module, export_name)
        except Exception as exc:
            raise RuntimeError(
                f"System-block bootstrap export '{export_name}' is unavailable: "
                f"{_safe_exception_message(exc)}"
            ) from exc

        if not callable(value):
            raise RuntimeError(
                f"System-block bootstrap export '{export_name}' is not callable."
            )

        exports[export_name] = value

    exports["module"] = module
    exports["moduleName"] = _safe_str(
        getattr(module, "__name__", None),
        "",
    )
    exports["modulePath"] = _safe_str(
        getattr(module, "__file__", None),
        "",
    )

    return exports


def clear_default_seed_system_block_caches() -> None:
    """
    Clear only default-seed integration caches.

    The child package exposes its own cache-clear functions. This helper avoids
    importing or resetting them unless they have already been resolved.
    """
    try:
        api = load_system_block_bootstrap_api()
        module = api.get("module")

        clear_function = getattr(
            module,
            "clear_system_block_bootstrap_caches",
            None,
        )

        if callable(clear_function):
            clear_function()
    except Exception:
        pass

    load_system_block_bootstrap_api.cache_clear()


def _empty_system_block_status(
    *,
    registry: Any = None,
    error: str | None = None,
    exception_type: str | None = None,
) -> dict[str, Any]:
    """Build a stable unavailable/not-ready system-block status."""
    registry_id = (
        _safe_str(getattr(registry, "registry_id", None), "")
        if registry is not None
        else ""
    )
    registry_version = (
        _safe_str(getattr(registry, "registry_version", None), "")
        if registry is not None
        else ""
    )

    return {
        "ready": False,
        "repairable": False,
        "registryDbId": _safe_model_id(registry),
        "registryId": registry_id or None,
        "registryVersion": registry_version or None,
        "registryKey": (
            f"{registry_id}@{registry_version}"
            if registry_id and registry_version
            else None
        ),
        "air": {
            "ready": False,
            "systemBlockId": DEFAULT_SYSTEM_AIR_BLOCK_ID,
            "illegalRowCount": None,
        },
        "mirrors": [],
        "counts": {
            "mirrors": 0,
            "readyMirrors": 0,
            "created": 0,
            "updated": 0,
            "drifted": 0,
            "missing": 0,
        },
        "errors": [error] if error else [],
        "errorType": exception_type,
        "error": error,
    }


def build_default_system_blocks_status(
    registry: Any,
) -> dict[str, Any]:
    """
    Build non-mutating built-in system-block readiness for one registry.

    This helper never creates or updates rows.
    """
    if registry is None:
        return _empty_system_block_status(
            error="Default BlockRegistry does not exist.",
            exception_type="RegistryMissing",
        )

    try:
        api = load_system_block_bootstrap_api()
        status_factory = api[
            "build_system_block_bootstrap_status_for_registry"
        ]
        status = status_factory(registry)
    except Exception as exc:
        return _empty_system_block_status(
            registry=registry,
            error=_safe_exception_message(exc),
            exception_type=exc.__class__.__name__,
        )

    normalized = _safe_dict(status)

    if not normalized:
        return _empty_system_block_status(
            registry=registry,
            error="System-block status factory returned no mapping.",
            exception_type="InvalidStatusPayload",
        )

    return normalized


def _system_block_status_counts(
    status: Mapping[str, Any] | None,
) -> dict[str, int]:
    """Extract stable aggregate counts from a system-block status payload."""
    status_dict = _safe_dict(status)
    mirrors = status_dict.get("mirrors") or []

    if not isinstance(mirrors, Sequence) or isinstance(
        mirrors,
        (str, bytes, bytearray),
    ):
        mirrors = []

    created = 0
    updated = 0
    missing = 0
    ready = 0
    drifted = 0

    for raw_mirror in mirrors:
        mirror = _safe_dict(raw_mirror)

        if _safe_bool(mirror.get("created"), False):
            created += 1

        if _safe_bool(mirror.get("updated"), False):
            updated += 1

        if _safe_bool(mirror.get("ready"), False):
            ready += 1

        action = _safe_str(mirror.get("action"), "").lower()

        if action in {
            "missing",
            "would_create",
        }:
            missing += 1

        if mirror.get("driftBefore") or action in {
            "drifted",
            "would_update",
            "updated",
        }:
            drifted += 1

    counts = _safe_dict(status_dict.get("counts"))

    return {
        "mirrors": _safe_int(
            counts.get("mirrors"),
            len(mirrors),
            minimum=0,
        ),
        "readyMirrors": _safe_int(
            counts.get("readyMirrors"),
            ready,
            minimum=0,
        ),
        "created": _safe_int(
            counts.get("created"),
            created,
            minimum=0,
        ),
        "updated": _safe_int(
            counts.get("updated"),
            updated,
            minimum=0,
        ),
        "missing": _safe_int(
            counts.get("missing"),
            missing,
            minimum=0,
        ),
        "drifted": _safe_int(
            counts.get("drifted"),
            drifted,
            minimum=0,
        ),
    }


def _system_railing_ready(
    status: Mapping[str, Any] | None,
) -> bool:
    """Return whether the canonical persistent Railing mirror is ready."""
    status_dict = _safe_dict(status)
    mirrors = status_dict.get("mirrors") or []

    if not isinstance(mirrors, Sequence) or isinstance(
        mirrors,
        (str, bytes, bytearray),
    ):
        return False

    for raw_mirror in mirrors:
        mirror = _safe_dict(raw_mirror)

        system_block_id = _safe_str(
            mirror.get("systemBlockId"),
            "",
        ).lower()

        runtime_block_type_id = _safe_str(
            mirror.get("runtimeBlockTypeId"),
            "",
        ).lower()

        if (
            system_block_id
            == DEFAULT_SYSTEM_RAILING_BLOCK_TYPE_ID
            or runtime_block_type_id
            == DEFAULT_SYSTEM_RAILING_BLOCK_TYPE_ID
        ):
            return _safe_bool(
                mirror.get("ready"),
                False,
            )

    return False


def default_system_blocks_exist(
    registry: Any,
) -> bool:
    """Return whether Air and all persistent system mirrors are ready."""
    status = build_default_system_blocks_status(
        registry
    )

    return _safe_bool(
        status.get("ready"),
        False,
    )


def seed_system_blocks(
    app: Flask,
    models: dict[str, Any],
    block_defaults: Any,
    *,
    db_extension: Any = None,
) -> list[dict[str, Any]]:
    """
    Ensure the default registry and reconcile built-in system blocks.

    This function does not commit. The surrounding default-seed transaction
    owns the final commit or rollback.
    """
    operations: list[dict[str, Any]] = []

    BlockRegistry = models["BlockRegistry"]

    registry_id = _safe_str(
        getattr(
            block_defaults,
            "registry_id",
            DEFAULT_BLOCK_REGISTRY_ID,
        ),
        DEFAULT_BLOCK_REGISTRY_ID,
    )
    registry_version = _safe_str(
        getattr(
            block_defaults,
            "registry_version",
            DEFAULT_BLOCK_REGISTRY_VERSION,
        ),
        DEFAULT_BLOCK_REGISTRY_VERSION,
    )

    registry = find_default_block_registry(
        models,
        block_defaults,
    )

    if registry is None:
        registry_started_at = _utc_now_iso()

        registry = create_block_registry_object(
            BlockRegistry,
            block_defaults,
        )
        _add_to_session(
            registry,
            db_extension,
        )
        _flush_session(
            db_extension
        )

        operations.append(
            _make_operation(
                name="block_registry:system_blocks",
                ok=True,
                status=OP_STATUS_OK,
                created=True,
                message=(
                    "Block registry created for built-in system blocks."
                ),
                started_at=registry_started_at,
                data={
                    "registryId": registry_id,
                    "registryVersion": registry_version,
                    "registryDbId": _safe_model_id(
                        registry
                    ),
                },
            )
        )

    else:
        registry_started_at = _utc_now_iso()

        registry_updated = (
            apply_block_registry_defaults_to_object(
                registry,
                block_defaults,
            )
        )

        if registry_updated:
            _flush_session(
                db_extension
            )

        operations.append(
            _make_operation(
                name="block_registry:system_blocks",
                ok=True,
                status=(
                    OP_STATUS_OK
                    if registry_updated
                    else OP_STATUS_SKIPPED
                ),
                updated=registry_updated,
                skipped=not registry_updated,
                message=(
                    "Block registry updated for built-in system blocks."
                    if registry_updated
                    else (
                        "Block registry for built-in system blocks "
                        "is already ready."
                    )
                ),
                started_at=registry_started_at,
                data={
                    "registryId": registry_id,
                    "registryVersion": registry_version,
                    "registryDbId": _safe_model_id(
                        registry
                    ),
                },
            )
        )

    registry_db_id = _safe_model_id(
        registry
    )

    if registry_db_id is None:
        raise RuntimeError(
            "Block registry has no database id before system-block bootstrap."
        )

    op_started_at = _utc_now_iso()

    try:
        api = load_system_block_bootstrap_api()

        policy_factory = api[
            "get_default_system_block_bootstrap_policy"
        ]
        ensure_function = api[
            "ensure_system_blocks_for_registry"
        ]

        policy = policy_factory()

        bootstrap_result = ensure_function(
            registry,
            policy=policy,
            created_by_user_id=(
                DEFAULT_SYSTEM_BLOCK_BOOTSTRAP_USER_ID
            ),
            updated_by_user_id=(
                DEFAULT_SYSTEM_BLOCK_BOOTSTRAP_USER_ID
            ),
        )

        bootstrap_data = _safe_dict(
            bootstrap_result
        )

        if not bootstrap_data:
            to_dict = getattr(
                bootstrap_result,
                "to_dict",
                None,
            )

            if callable(to_dict):
                bootstrap_data = _safe_dict(
                    to_dict()
                )

        ready = _safe_bool(
            bootstrap_data.get("ready"),
            False,
        )
        changed = _safe_bool(
            bootstrap_data.get("changed"),
            False,
        )

        counts = _system_block_status_counts(
            bootstrap_data
        )

        created = counts["created"] > 0
        updated = bool(
            counts["updated"] > 0
            or (
                changed
                and not created
            )
        )

        if not ready:
            raise RuntimeError(
                "Built-in system-block reconciliation returned ready=false."
            )

        operations.append(
            _make_operation(
                name="system_blocks",
                ok=True,
                status=(
                    OP_STATUS_OK
                    if changed
                    else OP_STATUS_SKIPPED
                ),
                created=created,
                updated=updated,
                skipped=not changed,
                changed=changed,
                message=(
                    "Built-in system blocks reconciled."
                    if changed
                    else "Built-in system blocks are already ready."
                ),
                started_at=op_started_at,
                data=bootstrap_data,
            )
        )

    except Exception as exc:
        message = _safe_exception_message(
            exc
        )

        operations.append(
            _make_operation(
                name="system_blocks",
                ok=False,
                status=OP_STATUS_FAILED,
                message=message,
                started_at=op_started_at,
                data={
                    "registryId": registry_id,
                    "registryVersion": registry_version,
                    "registryDbId": registry_db_id,
                    "exceptionType": (
                        exc.__class__.__name__
                    ),
                },
            )
        )

        raise RuntimeError(
            f"Built-in system-block bootstrap failed: {message}"
        ) from exc

    return operations


# -----------------------------------------------------------------------------
# Settings/default resolution
# -----------------------------------------------------------------------------

def _config_get(app: Any, key: str, default: Any = None) -> Any:
    """Read app.config robustly."""
    try:
        return app.config.get(key, default)
    except Exception:
        return default


def _first_attr(obj: Any, names: Sequence[str], default: Any = None) -> Any:
    """Return first non-empty attribute from object."""
    for name in names:
        try:
            value = getattr(obj, name)
        except Exception:
            value = None
        if value not in (None, ""):
            return value
    return default


def _provider_like_id(value: Any, *, template_id: str, provider_id: str, provider_world_id: str) -> bool:
    """Return whether value looks like provider/template id."""
    text = _safe_str(value, "").lower()
    if not text:
        return False

    return text in {
        DEFAULT_TEMPLATE_ID,
        DEFAULT_PROVIDER_ID,
        DEFAULT_PROVIDER_WORLD_ID,
        template_id.lower(),
        provider_id.lower(),
        provider_world_id.lower(),
    }


def _resolve_template_id(world_defaults: Any) -> str:
    return _safe_str(
        _first_attr(
            world_defaults,
            ("template_id", "world_template_id", "default_template_id", "templateId"),
            DEFAULT_TEMPLATE_ID,
        ),
        DEFAULT_TEMPLATE_ID,
    )


def _resolve_provider_id(world_defaults: Any) -> str:
    return _safe_str(
        _first_attr(
            world_defaults,
            ("provider_id", "default_provider_id", "providerId"),
            DEFAULT_PROVIDER_ID,
        ),
        DEFAULT_PROVIDER_ID,
    )


def _resolve_provider_world_id(world_defaults: Any) -> str:
    return _safe_str(
        _first_attr(
            world_defaults,
            ("provider_world_id", "default_provider_world_id", "providerWorldId"),
            DEFAULT_PROVIDER_WORLD_ID,
        ),
        DEFAULT_PROVIDER_WORLD_ID,
    )


def _resolve_world_id(world_defaults: Any) -> str:
    """Resolve concrete editable default world id."""
    template_id = _resolve_template_id(world_defaults)
    provider_id = _resolve_provider_id(world_defaults)
    provider_world_id = _resolve_provider_world_id(world_defaults)

    candidate = _first_attr(
        world_defaults,
        (
            "world_id",
            "default_world_id",
            "instance_world_id",
            "default_instance_world_id",
            "spawn_world_id",
            "worldId",
            "defaultWorldId",
        ),
        DEFAULT_WORLD_ID,
    )
    resolved = _safe_str(candidate, DEFAULT_WORLD_ID)

    if _provider_like_id(
        resolved,
        template_id=template_id,
        provider_id=provider_id,
        provider_world_id=provider_world_id,
    ):
        return DEFAULT_WORLD_ID

    return resolved or DEFAULT_WORLD_ID


def _resolve_project_id(world_defaults: Any) -> str:
    return _safe_str(
        _first_attr(world_defaults, ("project_id", "default_project_id", "projectId"), DEFAULT_PROJECT_ID),
        DEFAULT_PROJECT_ID,
    )


def _resolve_project_slug(world_defaults: Any) -> str:
    return _safe_str(
        _first_attr(world_defaults, ("project_slug", "default_project_slug", "projectSlug"), DEFAULT_PROJECT_SLUG),
        DEFAULT_PROJECT_SLUG,
    )


def _resolve_project_name(world_defaults: Any) -> str:
    return _safe_str(
        _first_attr(world_defaults, ("project_name", "default_project_name", "projectName"), DEFAULT_PROJECT_NAME),
        DEFAULT_PROJECT_NAME,
    )


def _resolve_universe_id(world_defaults: Any) -> str:
    return _safe_str(
        _first_attr(world_defaults, ("universe_id", "default_universe_id", "universeId"), DEFAULT_UNIVERSE_ID),
        DEFAULT_UNIVERSE_ID,
    )


def _resolve_universe_slug(world_defaults: Any) -> str:
    return _safe_str(
        _first_attr(world_defaults, ("universe_slug", "default_universe_slug", "universeSlug"), DEFAULT_UNIVERSE_SLUG),
        DEFAULT_UNIVERSE_SLUG,
    )


def _resolve_universe_name(world_defaults: Any) -> str:
    return _safe_str(
        _first_attr(world_defaults, ("universe_name", "default_universe_name", "universeName"), DEFAULT_UNIVERSE_NAME),
        DEFAULT_UNIVERSE_NAME,
    )


def _resolve_world_slug(world_defaults: Any) -> str:
    return _safe_str(
        _first_attr(world_defaults, ("world_slug", "default_world_slug", "worldSlug"), DEFAULT_WORLD_SLUG),
        DEFAULT_WORLD_SLUG,
    )


def _resolve_world_name(world_defaults: Any) -> str:
    return _safe_str(
        _first_attr(world_defaults, ("world_name", "default_world_name", "worldName"), DEFAULT_WORLD_NAME),
        DEFAULT_WORLD_NAME,
    )


def _resolve_block_registry_id_from_world(world_defaults: Any) -> str:
    return _safe_str(
        _first_attr(
            world_defaults,
            ("block_registry_id", "default_block_registry_id", "blockRegistryId"),
            DEFAULT_BLOCK_REGISTRY_ID,
        ),
        DEFAULT_BLOCK_REGISTRY_ID,
    )


def _resolve_block_registry_version_from_world(world_defaults: Any) -> str:
    return _safe_str(
        _first_attr(
            world_defaults,
            ("block_registry_version", "default_block_registry_version", "blockRegistryVersion"),
            DEFAULT_BLOCK_REGISTRY_VERSION,
        ),
        DEFAULT_BLOCK_REGISTRY_VERSION,
    )


def _fallback_world_defaults(app: Any = None) -> Any:
    """Create fallback world defaults object."""

    class FallbackWorldDefaults:
        project_id = _safe_str(_config_get(app, "VECTOPLAN_CHUNK_DEFAULT_PROJECT_ID", DEFAULT_PROJECT_ID), DEFAULT_PROJECT_ID)
        project_slug = _safe_str(_config_get(app, "VECTOPLAN_CHUNK_DEFAULT_PROJECT_SLUG", project_id), project_id)
        project_name = _safe_str(_config_get(app, "VECTOPLAN_CHUNK_DEFAULT_PROJECT_NAME", DEFAULT_PROJECT_NAME), DEFAULT_PROJECT_NAME)

        universe_id = _safe_str(_config_get(app, "VECTOPLAN_CHUNK_DEFAULT_UNIVERSE_ID", DEFAULT_UNIVERSE_ID), DEFAULT_UNIVERSE_ID)
        universe_slug = _safe_str(_config_get(app, "VECTOPLAN_CHUNK_DEFAULT_UNIVERSE_SLUG", universe_id), universe_id)
        universe_name = _safe_str(_config_get(app, "VECTOPLAN_CHUNK_DEFAULT_UNIVERSE_NAME", DEFAULT_UNIVERSE_NAME), DEFAULT_UNIVERSE_NAME)

        template_id = _safe_str(_config_get(app, "VECTOPLAN_CHUNK_DEFAULT_TEMPLATE_ID", DEFAULT_TEMPLATE_ID), DEFAULT_TEMPLATE_ID)
        provider_id = _safe_str(_config_get(app, "VECTOPLAN_CHUNK_DEFAULT_PROVIDER_ID", DEFAULT_PROVIDER_ID), DEFAULT_PROVIDER_ID)
        provider_world_id = _safe_str(_config_get(app, "VECTOPLAN_CHUNK_DEFAULT_PROVIDER_WORLD_ID", DEFAULT_PROVIDER_WORLD_ID), DEFAULT_PROVIDER_WORLD_ID)

        raw_world_id = _safe_str(
            _config_get(
                app,
                "VECTOPLAN_CHUNK_DEFAULT_INSTANCE_WORLD_ID",
                _config_get(app, "VECTOPLAN_CHUNK_DEFAULT_WORLD_ID", DEFAULT_WORLD_ID),
            ),
            DEFAULT_WORLD_ID,
        )
        world_id = DEFAULT_WORLD_ID if _provider_like_id(
            raw_world_id,
            template_id=template_id,
            provider_id=provider_id,
            provider_world_id=provider_world_id,
        ) else raw_world_id

        world_slug = _safe_str(_config_get(app, "VECTOPLAN_CHUNK_DEFAULT_INSTANCE_WORLD_SLUG", DEFAULT_WORLD_SLUG), DEFAULT_WORLD_SLUG)
        world_name = _safe_str(_config_get(app, "VECTOPLAN_CHUNK_DEFAULT_INSTANCE_WORLD_NAME", DEFAULT_WORLD_NAME), DEFAULT_WORLD_NAME)

        world_type = _safe_str(_config_get(app, "VECTOPLAN_CHUNK_DEFAULT_WORLD_TYPE", "runtime-world"), "runtime-world")
        world_role = _safe_str(_config_get(app, "VECTOPLAN_CHUNK_DEFAULT_WORLD_ROLE", "default_spawn"), "default_spawn")
        world_scope = _safe_str(_config_get(app, "VECTOPLAN_CHUNK_DEFAULT_WORLD_SCOPE", "project"), "project")
        world_owner_type = _safe_str(_config_get(app, "VECTOPLAN_CHUNK_DEFAULT_WORLD_OWNER_TYPE", "project"), "project")

        generator_type = _safe_str(_config_get(app, "VECTOPLAN_CHUNK_DEFAULT_GENERATOR_TYPE", DEFAULT_GENERATOR_TYPE), DEFAULT_GENERATOR_TYPE)
        generator_version = _safe_str(_config_get(app, "VECTOPLAN_CHUNK_DEFAULT_GENERATOR_VERSION", DEFAULT_GENERATOR_VERSION), DEFAULT_GENERATOR_VERSION)
        projection_type = _safe_str(_config_get(app, "VECTOPLAN_CHUNK_DEFAULT_PROJECTION_TYPE", DEFAULT_PROJECTION_TYPE), DEFAULT_PROJECTION_TYPE)
        topology_type = _safe_str(_config_get(app, "VECTOPLAN_CHUNK_DEFAULT_TOPOLOGY_TYPE", DEFAULT_TOPOLOGY_TYPE), DEFAULT_TOPOLOGY_TYPE)
        coordinate_system = _safe_str(_config_get(app, "VECTOPLAN_CHUNK_DEFAULT_COORDINATE_SYSTEM", DEFAULT_COORDINATE_SYSTEM), DEFAULT_COORDINATE_SYSTEM)

        chunk_size = _safe_int(_config_get(app, "VECTOPLAN_CHUNK_DEFAULT_CHUNK_SIZE", DEFAULT_CHUNK_SIZE), DEFAULT_CHUNK_SIZE, minimum=1)
        cell_size = _safe_float(_config_get(app, "VECTOPLAN_CHUNK_DEFAULT_CELL_SIZE", DEFAULT_CELL_SIZE), DEFAULT_CELL_SIZE, minimum=0.000001)
        surface_y = _safe_int(_config_get(app, "VECTOPLAN_CHUNK_DEFAULT_SURFACE_Y", DEFAULT_SURFACE_Y), DEFAULT_SURFACE_Y)
        min_y = _safe_int(_config_get(app, "VECTOPLAN_CHUNK_DEFAULT_MIN_Y", DEFAULT_MIN_Y), DEFAULT_MIN_Y)
        max_y = _safe_int(_config_get(app, "VECTOPLAN_CHUNK_DEFAULT_MAX_Y", DEFAULT_MAX_Y), DEFAULT_MAX_Y)
        seed = _safe_str(_config_get(app, "VECTOPLAN_CHUNK_DEFAULT_SEED", DEFAULT_SEED), DEFAULT_SEED)

        block_registry_id = _safe_str(_config_get(app, "VECTOPLAN_CHUNK_DEFAULT_BLOCK_REGISTRY_ID", DEFAULT_BLOCK_REGISTRY_ID), DEFAULT_BLOCK_REGISTRY_ID)
        block_registry_version = _safe_str(_config_get(app, "VECTOPLAN_CHUNK_DEFAULT_BLOCK_REGISTRY_VERSION", DEFAULT_BLOCK_REGISTRY_VERSION), DEFAULT_BLOCK_REGISTRY_VERSION)

        spawn_x = _safe_int(_config_get(app, "VECTOPLAN_CHUNK_DEFAULT_SPAWN_X", DEFAULT_SPAWN_X), DEFAULT_SPAWN_X)
        spawn_y = _safe_int(_config_get(app, "VECTOPLAN_CHUNK_DEFAULT_SPAWN_Y", DEFAULT_SPAWN_Y), DEFAULT_SPAWN_Y)
        spawn_z = _safe_int(_config_get(app, "VECTOPLAN_CHUNK_DEFAULT_SPAWN_Z", DEFAULT_SPAWN_Z), DEFAULT_SPAWN_Z)
        spawn_yaw = _safe_float(_config_get(app, "VECTOPLAN_CHUNK_DEFAULT_SPAWN_YAW", DEFAULT_SPAWN_YAW), DEFAULT_SPAWN_YAW)
        spawn_pitch = _safe_float(_config_get(app, "VECTOPLAN_CHUNK_DEFAULT_SPAWN_PITCH", DEFAULT_SPAWN_PITCH), DEFAULT_SPAWN_PITCH)

    return FallbackWorldDefaults()


def _fallback_block_defaults(app: Any = None) -> Any:
    """Create fallback block defaults object."""

    class FallbackBlockDefaults:
        registry_id = _safe_str(_config_get(app, "VECTOPLAN_CHUNK_DEFAULT_BLOCK_REGISTRY_ID", DEFAULT_BLOCK_REGISTRY_ID), DEFAULT_BLOCK_REGISTRY_ID)
        registry_version = _safe_str(_config_get(app, "VECTOPLAN_CHUNK_DEFAULT_BLOCK_REGISTRY_VERSION", DEFAULT_BLOCK_REGISTRY_VERSION), DEFAULT_BLOCK_REGISTRY_VERSION)
        seed_debug_grass = True
        seed_debug_dirt = True

    return FallbackBlockDefaults()


def _fallback_seed_settings(app: Any = None) -> Any:
    """Create fallback seed settings object."""

    class FallbackSeedSettings:
        seed_defaults = _safe_bool(_config_get(app, "VECTOPLAN_CHUNK_AUTO_SEED_DEFAULTS", False), False)
        seed_debug_blocks = _safe_bool(_config_get(app, "VECTOPLAN_CHUNK_SEED_DEBUG_BLOCKS", seed_defaults), seed_defaults)
        seed_dev_project = _safe_bool(_config_get(app, "VECTOPLAN_CHUNK_SEED_DEV_PROJECT", seed_defaults), seed_defaults)
        seed_on_empty_only = _safe_bool(_config_get(app, "VECTOPLAN_CHUNK_SEED_ON_EMPTY_ONLY", True), True)
        advisory_lock_enabled = _safe_bool(_config_get(app, "VECTOPLAN_CHUNK_BOOTSTRAP_USE_ADVISORY_LOCK", True), True)
        fail_on_error = _safe_bool(_config_get(app, "VECTOPLAN_CHUNK_DB_BOOTSTRAP_FAIL_ON_ERROR", True), True)

    return FallbackSeedSettings()


def resolve_world_defaults(app: Any = None, world_defaults: WorldDefaultsSettings | None = None) -> Any:
    """Resolve world default settings."""
    if world_defaults is not None:
        return world_defaults

    try:
        resolved = build_world_defaults_settings(app)
        if resolved is not None:
            return resolved
    except Exception:
        pass

    try:
        aggregate = build_bootstrap_settings(app)
        resolved = getattr(aggregate, "world_defaults", None)
        if resolved is not None:
            return resolved
    except Exception:
        pass

    return _fallback_world_defaults(app)


def resolve_block_defaults(app: Any = None, block_defaults: BlockDefaultsSettings | None = None) -> Any:
    """Resolve block default settings."""
    if block_defaults is not None:
        return block_defaults

    try:
        resolved = build_block_defaults_settings(app)
        if resolved is not None:
            return resolved
    except Exception:
        pass

    try:
        aggregate = build_bootstrap_settings(app)
        resolved = getattr(aggregate, "block_defaults", None)
        if resolved is not None:
            return resolved
    except Exception:
        pass

    return _fallback_block_defaults(app)


def resolve_seed_settings(app: Any = None, seed_settings: SeedBootstrapSettings | None = None) -> Any:
    """Resolve seed bootstrap settings."""
    if seed_settings is not None:
        return seed_settings

    try:
        resolved = build_seed_bootstrap_settings(app)
        if resolved is not None:
            return resolved
    except Exception:
        pass

    try:
        aggregate = build_bootstrap_settings(app)
        resolved = getattr(aggregate, "seed", None)
        if resolved is not None:
            return resolved
    except Exception:
        pass

    return _fallback_seed_settings(app)


# -----------------------------------------------------------------------------
# Target state checks
# -----------------------------------------------------------------------------

def find_default_block_registry(
    models: dict[str, Any],
    block_defaults: Any,
) -> Any | None:
    """Find default block registry by stable unique keys."""
    BlockRegistry = models["BlockRegistry"]

    registry_id = _safe_str(
        getattr(block_defaults, "registry_id", DEFAULT_BLOCK_REGISTRY_ID),
        DEFAULT_BLOCK_REGISTRY_ID,
    )
    registry_version = _safe_str(
        getattr(block_defaults, "registry_version", DEFAULT_BLOCK_REGISTRY_VERSION),
        DEFAULT_BLOCK_REGISTRY_VERSION,
    )

    registry = _query_first_by(
        BlockRegistry,
        registry_id=registry_id,
        registry_version=registry_version,
    )
    if registry is not None:
        return registry

    return _query_first_by(BlockRegistry, registry_id=registry_id)


def find_default_project(
    models: dict[str, Any],
    world_defaults: Any,
) -> Any | None:
    """Find default project by stable project_id."""
    Project = models["Project"]
    project_id = _resolve_project_id(world_defaults)
    return _query_first_by(Project, project_id=project_id)


def find_default_universe(
    models: dict[str, Any],
    project: Any,
    world_defaults: Any,
) -> Any | None:
    """Find default universe by project_db_id + universe_id."""
    Universe = models["Universe"]
    project_db_id = _safe_model_id(project)
    universe_id = _resolve_universe_id(world_defaults)

    if project_db_id is None:
        return None

    universe = _query_first_by(
        Universe,
        project_db_id=project_db_id,
        universe_id=universe_id,
    )
    if universe is not None:
        return universe

    return _query_first_by(Universe, universe_id=universe_id)


def find_default_world(
    models: dict[str, Any],
    universe: Any,
    world_defaults: Any,
) -> Any | None:
    """Find default world by universe_db_id + world_id."""
    WorldInstance = models["WorldInstance"]
    universe_db_id = _safe_model_id(universe)
    world_id = _resolve_world_id(world_defaults)

    if universe_db_id is None:
        return None

    world = _query_first_by(
        WorldInstance,
        universe_db_id=universe_db_id,
        world_id=world_id,
    )
    if world is not None:
        return world

    return _query_first_by(WorldInstance, world_id=world_id)


def default_debug_blocks_exist(
    models: dict[str, Any],
    registry: Any,
    block_defaults: Any,
) -> bool:
    """Return whether default debug block types exist."""
    if registry is None:
        return False

    BlockType = models["BlockType"]
    registry_db_id = _safe_model_id(registry)

    registry_id = _safe_str(getattr(registry, "registry_id", DEFAULT_BLOCK_REGISTRY_ID), DEFAULT_BLOCK_REGISTRY_ID)
    registry_version = _safe_str(getattr(registry, "registry_version", DEFAULT_BLOCK_REGISTRY_VERSION), DEFAULT_BLOCK_REGISTRY_VERSION)

    expected_blocks: list[str] = []

    if _safe_bool(getattr(block_defaults, "seed_debug_grass", True), True):
        expected_blocks.append("debug_grass")
    if _safe_bool(getattr(block_defaults, "seed_debug_dirt", True), True):
        expected_blocks.append("debug_dirt")

    for block_type_id in expected_blocks:
        if registry_db_id is not None and _exists_by(
            BlockType,
            registry_db_id=registry_db_id,
            block_type_id=block_type_id,
        ):
            continue

        if _exists_by(
            BlockType,
            registry_id=registry_id,
            registry_version=registry_version,
            block_type_id=block_type_id,
        ):
            continue

        return False

    return True


def is_default_seed_complete(
    models: dict[str, Any],
    world_defaults: Any,
    block_defaults: Any,
    *,
    require_blocks: bool = True,
    require_system_blocks: bool = True,
    require_project: bool = True,
) -> bool:
    """
    Return whether the target default seed graph is complete.

    Built-in system blocks are checked independently from optional debug block
    seeding. A complete runtime registry must satisfy the Air invariant and
    contain every persistent built-in system mirror.
    """
    try:
        registry = None

        if require_blocks or require_system_blocks:
            registry = find_default_block_registry(
                models,
                block_defaults,
            )

            if registry is None:
                return False

        if require_blocks:
            if not default_debug_blocks_exist(
                models,
                registry,
                block_defaults,
            ):
                return False

        if require_system_blocks:
            if not default_system_blocks_exist(
                registry
            ):
                return False

        if require_project:
            project = find_default_project(
                models,
                world_defaults,
            )

            if project is None:
                return False

            universe = find_default_universe(
                models,
                project,
                world_defaults,
            )

            if universe is None:
                return False

            world = find_default_world(
                models,
                universe,
                world_defaults,
            )

            if world is None:
                return False

        return True

    except Exception:
        return False


# -----------------------------------------------------------------------------
# Creation/update helpers
# -----------------------------------------------------------------------------

def create_project_object(model_class: Any, world_defaults: Any) -> Any:
    """Create Project instance using model factory if available."""
    project_id = _resolve_project_id(world_defaults)
    project_slug = _resolve_project_slug(world_defaults)
    project_name = _resolve_project_name(world_defaults)
    universe_id = _resolve_universe_id(world_defaults)
    world_id = _resolve_world_id(world_defaults)

    metadata_json = {
        "seededBy": "vectoplan-chunk.default_seed",
        "seededAt": _utc_now_iso(),
        "defaultUniverseId": universe_id,
        "defaultWorldId": world_id,
        "spawnWorldId": world_id,
    }

    for method_name in ("create_dev_project", "create"):
        create_method = getattr(model_class, method_name, None)
        if not callable(create_method):
            continue

        attempts = (
            {
                "project_id": project_id,
                "slug": project_slug,
                "name": project_name,
                "default_universe_id": universe_id,
                "default_world_id": world_id,
                "spawn_world_id": world_id,
                "created_by_user_id": "bootstrap",
                "metadata_json": metadata_json,
            },
            {
                "project_id": project_id,
                "slug": project_slug,
                "name": project_name,
                "default_universe_id": universe_id,
                "metadata_json": metadata_json,
            },
            {
                "project_id": project_id,
                "default_universe_id": universe_id,
                "default_world_id": world_id,
                "created_by_user_id": "bootstrap",
            },
            {
                "project_id": project_id,
                "default_universe_id": universe_id,
                "created_by_user_id": "bootstrap",
            },
        )

        for kwargs in attempts:
            try:
                obj = create_method(**kwargs)
                apply_project_defaults_to_object(obj, world_defaults)
                return obj
            except TypeError:
                continue
            except Exception:
                break

    values = {
        "project_id": project_id,
        "slug": project_slug,
        "name": project_name,
        "description": "Default development project for VECTOPLAN Chunk Service.",
        "status": "active",
        "schema_version": "project.schema.v2",
        "revision": 1,
        "default_universe_id": universe_id,
        "default_world_id": world_id,
        "spawn_world_id": world_id,
        "owner_type": "system",
        "owner_id": "vectoplan-chunk",
        "created_by_user_id": "bootstrap",
        "updated_by_user_id": "bootstrap",
        "metadata_json": metadata_json,
        "created_at": _utc_now(),
        "updated_at": _utc_now(),
    }
    obj = _instantiate_model(model_class, values)
    apply_project_defaults_to_object(obj, world_defaults)
    return obj


def apply_project_defaults_to_object(project: Any, world_defaults: Any) -> bool:
    """Apply config-driven project defaults to Project object."""
    changed = False
    universe_id = _resolve_universe_id(world_defaults)
    world_id = _resolve_world_id(world_defaults)

    if _call_if_available(
        project,
        "set_world_refs",
        default_universe_id=universe_id,
        default_world_id=world_id,
        spawn_world_id=world_id,
        updated_by_user_id="bootstrap",
    ):
        changed = True
    else:
        changed = _set_attr_if_supported(project, "default_universe_id", universe_id, overwrite=True) or changed
        changed = _set_attr_if_supported(project, "default_world_id", world_id, overwrite=True) or changed
        changed = _set_attr_if_supported(project, "spawn_world_id", world_id, overwrite=True) or changed

    changed = _set_attr_if_supported(project, "status", "active", overwrite=False) or changed
    changed = _set_attr_if_supported(project, "updated_by_user_id", "bootstrap", overwrite=True) or changed
    changed = _merge_metadata_json(
        project,
        {
            "seededBy": "vectoplan-chunk.default_seed",
            "defaultUniverseId": universe_id,
            "defaultWorldId": world_id,
            "spawnWorldId": world_id,
        },
    ) or changed

    return changed


def create_universe_object(model_class: Any, project: Any, world_defaults: Any) -> Any:
    """Create Universe instance using model factory if available."""
    project_db_id = _safe_model_id(project)
    universe_id = _resolve_universe_id(world_defaults)
    universe_slug = _resolve_universe_slug(world_defaults)
    universe_name = _resolve_universe_name(world_defaults)
    world_id = _resolve_world_id(world_defaults)

    metadata_json = {
        "seededBy": "vectoplan-chunk.default_seed",
        "seededAt": _utc_now_iso(),
        "defaultWorldId": world_id,
        "spawnWorldId": world_id,
    }

    for method_name in ("create_for_project", "create"):
        create_method = getattr(model_class, method_name, None)
        if not callable(create_method):
            continue

        attempts = (
            {
                "project": project,
                "universe_id": universe_id,
                "slug": universe_slug,
                "name": universe_name,
                "default_world_id": world_id,
                "spawn_world_id": world_id,
                "created_by_user_id": "bootstrap",
                "metadata_json": metadata_json,
            },
            {
                "project_db_id": project_db_id,
                "universe_id": universe_id,
                "slug": universe_slug,
                "name": universe_name,
                "default_world_id": world_id,
                "spawn_world_id": world_id,
                "metadata_json": metadata_json,
            },
            {
                "project_db_id": project_db_id,
                "universe_id": universe_id,
                "name": universe_name,
                "default_world_id": world_id,
            },
        )

        for kwargs in attempts:
            try:
                obj = create_method(**kwargs)
                apply_universe_defaults_to_object(obj, project, world_defaults)
                return obj
            except TypeError:
                continue
            except Exception:
                break

    values = {
        "project_db_id": project_db_id,
        "universe_id": universe_id,
        "slug": universe_slug,
        "name": universe_name,
        "description": "Default development universe for VECTOPLAN Chunk Service.",
        "status": "active",
        "schema_version": "universe.schema.v2",
        "revision": 1,
        "universe_role": "default",
        "universe_scope": "project",
        "default_world_id": world_id,
        "spawn_world_id": world_id,
        "created_by_user_id": "bootstrap",
        "updated_by_user_id": "bootstrap",
        "metadata_json": metadata_json,
        "created_at": _utc_now(),
        "updated_at": _utc_now(),
    }
    obj = _instantiate_model(model_class, values)
    apply_universe_defaults_to_object(obj, project, world_defaults)
    return obj


def apply_universe_defaults_to_object(universe: Any, project: Any, world_defaults: Any) -> bool:
    """Apply config-driven universe defaults to Universe object."""
    changed = False
    project_db_id = _safe_model_id(project)
    world_id = _resolve_world_id(world_defaults)

    changed = _set_attr_if_supported(universe, "project_db_id", project_db_id, overwrite=True) or changed

    if _call_if_available(
        universe,
        "set_world_defaults",
        default_world_id=world_id,
        spawn_world_id=world_id,
        updated_by_user_id="bootstrap",
    ):
        changed = True
    else:
        changed = _set_attr_if_supported(universe, "default_world_id", world_id, overwrite=True) or changed
        changed = _set_attr_if_supported(universe, "spawn_world_id", world_id, overwrite=True) or changed

    changed = _set_attr_if_supported(universe, "status", "active", overwrite=False) or changed
    changed = _set_attr_if_supported(universe, "updated_by_user_id", "bootstrap", overwrite=True) or changed
    changed = _merge_metadata_json(
        universe,
        {
            "seededBy": "vectoplan-chunk.default_seed",
            "defaultWorldId": world_id,
            "spawnWorldId": world_id,
        },
    ) or changed

    return changed


def create_world_object(model_class: Any, project: Any, universe: Any, world_defaults: Any) -> Any:
    """Create WorldInstance using model factory if available."""
    project_db_id = _safe_model_id(project)
    universe_db_id = _safe_model_id(universe)

    world_id = _resolve_world_id(world_defaults)
    world_slug = _resolve_world_slug(world_defaults)
    world_name = _resolve_world_name(world_defaults)

    template_id = _resolve_template_id(world_defaults)
    provider_id = _resolve_provider_id(world_defaults)
    provider_world_id = _resolve_provider_world_id(world_defaults)

    metadata_json = {
        "seededBy": "vectoplan-chunk.default_seed",
        "seededAt": _utc_now_iso(),
        "chunkProjectId": _safe_str(getattr(project, "project_id", DEFAULT_PROJECT_ID), DEFAULT_PROJECT_ID),
        "chunkUniverseId": _safe_str(getattr(universe, "universe_id", DEFAULT_UNIVERSE_ID), DEFAULT_UNIVERSE_ID),
        "chunkWorldId": world_id,
        "templateId": template_id,
        "providerId": provider_id,
        "providerWorldId": provider_world_id,
    }

    create_flat_spawn = getattr(model_class, "create_flat_spawn", None)

    if callable(create_flat_spawn):
        attempts = (
            {
                "project_db_id": project_db_id,
                "universe_db_id": universe_db_id,
                "world_id": world_id,
                "slug": world_slug,
                "name": world_name,
                "created_by_user_id": "bootstrap",
                "metadata_json": metadata_json,
                "source_service": "vectoplan-chunk-default-seed",
                "external_ref": world_id,
            },
            {
                "project": project,
                "universe": universe,
                "world_id": world_id,
                "slug": world_slug,
                "name": world_name,
                "created_by_user_id": "bootstrap",
                "metadata_json": metadata_json,
                "source_service": "vectoplan-chunk-default-seed",
                "external_ref": world_id,
            },
            {
                "project_db_id": project_db_id,
                "universe_db_id": universe_db_id,
                "world_id": world_id,
                "slug": world_slug,
                "name": world_name,
                "created_by_user_id": "bootstrap",
                "metadata_json": metadata_json,
            },
        )

        for kwargs in attempts:
            try:
                world = create_flat_spawn(**kwargs)
                apply_world_defaults_to_object(world, world_defaults)
                return world
            except TypeError:
                continue
            except Exception:
                break

    values = {
        "project_db_id": project_db_id,
        "universe_db_id": universe_db_id,
        "world_id": world_id,
        "slug": world_slug,
        "name": world_name,
        "description": "Default flat spawn world for VECTOPLAN Chunk Service.",
        "status": "active",
        "schema_version": "world-instance.schema.v2",
        "revision": 1,
        "template_id": template_id,
        "provider_id": provider_id,
        "provider_world_id": provider_world_id,
        "world_type": _safe_str(getattr(world_defaults, "world_type", "runtime-world"), "runtime-world"),
        "world_role": _safe_str(getattr(world_defaults, "world_role", "default_spawn"), "default_spawn"),
        "world_scope": _safe_str(getattr(world_defaults, "world_scope", "project"), "project"),
        "owner_type": _safe_str(getattr(world_defaults, "world_owner_type", "project"), "project"),
        "generator_type": _safe_str(getattr(world_defaults, "generator_type", DEFAULT_GENERATOR_TYPE), DEFAULT_GENERATOR_TYPE),
        "generator_version": _safe_str(getattr(world_defaults, "generator_version", DEFAULT_GENERATOR_VERSION), DEFAULT_GENERATOR_VERSION),
        "projection_type": _safe_str(getattr(world_defaults, "projection_type", DEFAULT_PROJECTION_TYPE), DEFAULT_PROJECTION_TYPE),
        "topology_type": _safe_str(getattr(world_defaults, "topology_type", DEFAULT_TOPOLOGY_TYPE), DEFAULT_TOPOLOGY_TYPE),
        "coordinate_system": _safe_str(getattr(world_defaults, "coordinate_system", DEFAULT_COORDINATE_SYSTEM), DEFAULT_COORDINATE_SYSTEM),
        "chunk_size": _safe_int(getattr(world_defaults, "chunk_size", DEFAULT_CHUNK_SIZE), DEFAULT_CHUNK_SIZE, minimum=1),
        "cell_size": _safe_float(getattr(world_defaults, "cell_size", DEFAULT_CELL_SIZE), DEFAULT_CELL_SIZE, minimum=0.000001),
        "surface_y": _safe_int(getattr(world_defaults, "surface_y", DEFAULT_SURFACE_Y), DEFAULT_SURFACE_Y),
        "min_y": _safe_int(getattr(world_defaults, "min_y", DEFAULT_MIN_Y), DEFAULT_MIN_Y),
        "max_y": _safe_int(getattr(world_defaults, "max_y", DEFAULT_MAX_Y), DEFAULT_MAX_Y),
        "seed": _safe_str(getattr(world_defaults, "seed", DEFAULT_SEED), DEFAULT_SEED),
        "block_registry_id": _resolve_block_registry_id_from_world(world_defaults),
        "block_registry_version": _resolve_block_registry_version_from_world(world_defaults),
        "spawn_x": _safe_int(getattr(world_defaults, "spawn_x", DEFAULT_SPAWN_X), DEFAULT_SPAWN_X),
        "spawn_y": _safe_int(getattr(world_defaults, "spawn_y", DEFAULT_SPAWN_Y), DEFAULT_SPAWN_Y),
        "spawn_z": _safe_int(getattr(world_defaults, "spawn_z", DEFAULT_SPAWN_Z), DEFAULT_SPAWN_Z),
        "spawn_yaw": _safe_float(getattr(world_defaults, "spawn_yaw", DEFAULT_SPAWN_YAW), DEFAULT_SPAWN_YAW),
        "spawn_pitch": _safe_float(getattr(world_defaults, "spawn_pitch", DEFAULT_SPAWN_PITCH), DEFAULT_SPAWN_PITCH),
        "source_service": "vectoplan-chunk-default-seed",
        "external_ref": world_id,
        "created_by_user_id": "bootstrap",
        "updated_by_user_id": "bootstrap",
        "metadata_json": metadata_json,
        "created_at": _utc_now(),
        "updated_at": _utc_now(),
    }
    world = _instantiate_model(model_class, values)
    apply_world_defaults_to_object(world, world_defaults)
    return world


def apply_world_defaults_to_object(world: Any, world_defaults: Any) -> bool:
    """Apply config-driven world defaults to an existing/new WorldInstance object."""
    template_id = _resolve_template_id(world_defaults)
    provider_id = _resolve_provider_id(world_defaults)
    provider_world_id = _resolve_provider_world_id(world_defaults)
    world_id = _resolve_world_id(world_defaults)

    assignments = {
        "template_id": template_id,
        "provider_id": provider_id,
        "provider_world_id": provider_world_id,
        "generator_type": _safe_str(getattr(world_defaults, "generator_type", DEFAULT_GENERATOR_TYPE), DEFAULT_GENERATOR_TYPE),
        "generator_version": _safe_str(getattr(world_defaults, "generator_version", DEFAULT_GENERATOR_VERSION), DEFAULT_GENERATOR_VERSION),
        "projection_type": _safe_str(getattr(world_defaults, "projection_type", DEFAULT_PROJECTION_TYPE), DEFAULT_PROJECTION_TYPE),
        "topology_type": _safe_str(getattr(world_defaults, "topology_type", DEFAULT_TOPOLOGY_TYPE), DEFAULT_TOPOLOGY_TYPE),
        "coordinate_system": _safe_str(getattr(world_defaults, "coordinate_system", DEFAULT_COORDINATE_SYSTEM), DEFAULT_COORDINATE_SYSTEM),
        "chunk_size": _safe_int(getattr(world_defaults, "chunk_size", DEFAULT_CHUNK_SIZE), DEFAULT_CHUNK_SIZE, minimum=1),
        "cell_size": _safe_float(getattr(world_defaults, "cell_size", DEFAULT_CELL_SIZE), DEFAULT_CELL_SIZE, minimum=0.000001),
        "surface_y": _safe_int(getattr(world_defaults, "surface_y", DEFAULT_SURFACE_Y), DEFAULT_SURFACE_Y),
        "min_y": _safe_int(getattr(world_defaults, "min_y", DEFAULT_MIN_Y), DEFAULT_MIN_Y),
        "max_y": _safe_int(getattr(world_defaults, "max_y", DEFAULT_MAX_Y), DEFAULT_MAX_Y),
        "seed": _safe_str(getattr(world_defaults, "seed", DEFAULT_SEED), DEFAULT_SEED),
        "block_registry_id": _resolve_block_registry_id_from_world(world_defaults),
        "block_registry_version": _resolve_block_registry_version_from_world(world_defaults),
        "spawn_x": _safe_int(getattr(world_defaults, "spawn_x", DEFAULT_SPAWN_X), DEFAULT_SPAWN_X),
        "spawn_y": _safe_int(getattr(world_defaults, "spawn_y", DEFAULT_SPAWN_Y), DEFAULT_SPAWN_Y),
        "spawn_z": _safe_int(getattr(world_defaults, "spawn_z", DEFAULT_SPAWN_Z), DEFAULT_SPAWN_Z),
        "spawn_yaw": _safe_float(getattr(world_defaults, "spawn_yaw", DEFAULT_SPAWN_YAW), DEFAULT_SPAWN_YAW),
        "spawn_pitch": _safe_float(getattr(world_defaults, "spawn_pitch", DEFAULT_SPAWN_PITCH), DEFAULT_SPAWN_PITCH),
        "source_service": "vectoplan-chunk-default-seed",
        "external_ref": world_id,
        "updated_by_user_id": "bootstrap",
    }

    changed = False
    for name, value in assignments.items():
        changed = _set_attr_if_supported(world, name, value, overwrite=True) or changed

    changed = _merge_metadata_json(
        world,
        {
            "seededBy": "vectoplan-chunk.default_seed",
            "chunkWorldId": world_id,
            "templateId": template_id,
            "providerId": provider_id,
            "providerWorldId": provider_world_id,
            "blockRegistryId": assignments["block_registry_id"],
            "blockRegistryVersion": assignments["block_registry_version"],
        },
    ) or changed

    return changed


def create_block_registry_object(model_class: Any, block_defaults: Any) -> Any:
    """Create BlockRegistry object using model factory if available."""
    registry_id = _safe_str(getattr(block_defaults, "registry_id", DEFAULT_BLOCK_REGISTRY_ID), DEFAULT_BLOCK_REGISTRY_ID)
    registry_version = _safe_str(
        getattr(block_defaults, "registry_version", DEFAULT_BLOCK_REGISTRY_VERSION),
        DEFAULT_BLOCK_REGISTRY_VERSION,
    )

    create_debug_registry = getattr(model_class, "create_debug_registry", None)

    if callable(create_debug_registry) and registry_id == DEFAULT_BLOCK_REGISTRY_ID and registry_version == DEFAULT_BLOCK_REGISTRY_VERSION:
        try:
            registry = create_debug_registry(is_default=True)
            apply_block_registry_defaults_to_object(registry, block_defaults)
            return registry
        except Exception:
            pass

    create_method = getattr(model_class, "create", None)

    if callable(create_method):
        try:
            registry = create_method(
                registry_id=registry_id,
                registry_version=registry_version,
                label=f"{registry_id} {registry_version}",
                is_default=True,
            )
            apply_block_registry_defaults_to_object(registry, block_defaults)
            return registry
        except Exception:
            pass

    values = {
        "registry_id": registry_id,
        "registry_version": registry_version,
        "label": f"{registry_id} {registry_version}",
        "description": "Default debug block registry for VECTOPLAN Chunk Service.",
        "status": "active",
        "is_default": True,
        "schema_version": "block-registry.schema.v1",
        "revision": 1,
        "metadata_json": {
            "seededBy": "vectoplan-chunk.default_seed",
            "seededAt": _utc_now_iso(),
        },
        "created_by_user_id": "bootstrap",
        "updated_by_user_id": "bootstrap",
        "created_at": _utc_now(),
        "updated_at": _utc_now(),
    }
    registry = _instantiate_model(model_class, values)
    apply_block_registry_defaults_to_object(registry, block_defaults)
    return registry


def apply_block_registry_defaults_to_object(registry: Any, block_defaults: Any) -> bool:
    """Apply default registry values."""
    changed = False
    registry_id = _safe_str(getattr(block_defaults, "registry_id", DEFAULT_BLOCK_REGISTRY_ID), DEFAULT_BLOCK_REGISTRY_ID)
    registry_version = _safe_str(getattr(block_defaults, "registry_version", DEFAULT_BLOCK_REGISTRY_VERSION), DEFAULT_BLOCK_REGISTRY_VERSION)

    changed = _set_attr_if_supported(registry, "registry_id", registry_id, overwrite=True) or changed
    changed = _set_attr_if_supported(registry, "registry_version", registry_version, overwrite=True) or changed
    changed = _set_attr_if_supported(registry, "label", f"{registry_id} {registry_version}", overwrite=False) or changed

    registry_status = _safe_str(
        getattr(registry, "status", ""),
        "",
    ).lower()
    registry_deleted_at = getattr(
        registry,
        "deleted_at",
        None,
    )

    if registry_status != "active" or registry_deleted_at is not None:
        if _call_if_available(
            registry,
            "restore",
            updated_by_user_id="bootstrap",
        ):
            changed = True
        else:
            changed = _set_attr_if_supported(
                registry,
                "status",
                "active",
                overwrite=True,
            ) or changed
            changed = _set_attr_if_supported(
                registry,
                "archived_at",
                None,
                overwrite=True,
            ) or changed
            changed = _set_attr_if_supported(
                registry,
                "deleted_at",
                None,
                overwrite=True,
            ) or changed

    changed = _set_attr_if_supported(registry, "is_default", True, overwrite=True) or changed
    changed = _set_attr_if_supported(registry, "updated_by_user_id", "bootstrap", overwrite=True) or changed
    changed = _merge_metadata_json(
        registry,
        {
            "seededBy": "vectoplan-chunk.default_seed",
            "registryId": registry_id,
            "registryVersion": registry_version,
        },
    ) or changed

    return changed


def create_debug_block_object(model_class: Any, registry: Any, block_type_id: str) -> Any:
    """Create debug BlockType object using model factory if available."""
    block_type_id = _safe_str(block_type_id, "")

    if block_type_id == "debug_grass":
        factory = getattr(model_class, "create_debug_grass", None)
        if callable(factory):
            try:
                block = factory(registry)
                apply_debug_block_defaults_to_object(block, registry, block_type_id)
                return block
            except Exception:
                pass

        label = "Debug Grass"
        color = "#54b948"

    elif block_type_id == "debug_dirt":
        factory = getattr(model_class, "create_debug_dirt", None)
        if callable(factory):
            try:
                block = factory(registry)
                apply_debug_block_defaults_to_object(block, registry, block_type_id)
                return block
            except Exception:
                pass

        label = "Debug Dirt"
        color = "#8b5a2b"

    else:
        label = block_type_id.replace("_", " ").title()
        color = "#cccccc"

    registry_db_id = _safe_model_id(registry)
    registry_id = _safe_str(getattr(registry, "registry_id", DEFAULT_BLOCK_REGISTRY_ID), DEFAULT_BLOCK_REGISTRY_ID)
    registry_version = _safe_str(
        getattr(registry, "registry_version", DEFAULT_BLOCK_REGISTRY_VERSION),
        DEFAULT_BLOCK_REGISTRY_VERSION,
    )

    create_method = getattr(model_class, "create", None)

    if callable(create_method):
        try:
            block = create_method(
                registry=registry,
                block_type_id=block_type_id,
                label=label,
                solid=True,
                placeable=True,
                breakable=True,
                metadata_json={
                    "debug": True,
                    "color": color,
                    "seededBy": "vectoplan-chunk.default_seed",
                    "seededAt": _utc_now_iso(),
                },
            )
            apply_debug_block_defaults_to_object(block, registry, block_type_id)
            return block
        except Exception:
            pass

    values = {
        "registry_db_id": registry_db_id,
        "registry_id": registry_id,
        "registry_version": registry_version,
        "block_type_id": block_type_id,
        "label": label,
        "name": label,
        "status": "active",
        "solid": True,
        "placeable": True,
        "breakable": True,
        "metadata_json": {
            "debug": True,
            "color": color,
            "seededBy": "vectoplan-chunk.default_seed",
            "seededAt": _utc_now_iso(),
        },
        "created_by_user_id": "bootstrap",
        "updated_by_user_id": "bootstrap",
        "created_at": _utc_now(),
        "updated_at": _utc_now(),
    }
    block = _instantiate_model(model_class, values)
    apply_debug_block_defaults_to_object(block, registry, block_type_id)
    return block


def apply_debug_block_defaults_to_object(block: Any, registry: Any, block_type_id: str) -> bool:
    """Apply debug block default values."""
    changed = False
    block_type_id = _safe_str(block_type_id, "")
    registry_db_id = _safe_model_id(registry)
    registry_id = _safe_str(getattr(registry, "registry_id", DEFAULT_BLOCK_REGISTRY_ID), DEFAULT_BLOCK_REGISTRY_ID)
    registry_version = _safe_str(getattr(registry, "registry_version", DEFAULT_BLOCK_REGISTRY_VERSION), DEFAULT_BLOCK_REGISTRY_VERSION)

    if block_type_id == "debug_grass":
        label = "Debug Grass"
        color = "#54b948"
    elif block_type_id == "debug_dirt":
        label = "Debug Dirt"
        color = "#8b5a2b"
    else:
        label = block_type_id.replace("_", " ").title()
        color = "#cccccc"

    changed = _set_attr_if_supported(block, "registry_db_id", registry_db_id, overwrite=True) or changed
    changed = _set_attr_if_supported(block, "registry_id", registry_id, overwrite=True) or changed
    changed = _set_attr_if_supported(block, "registry_version", registry_version, overwrite=True) or changed
    changed = _set_attr_if_supported(block, "block_type_id", block_type_id, overwrite=True) or changed
    changed = _set_attr_if_supported(block, "label", label, overwrite=False) or changed
    changed = _set_attr_if_supported(block, "name", label, overwrite=False) or changed
    changed = _set_attr_if_supported(block, "status", "active", overwrite=False) or changed
    changed = _set_attr_if_supported(block, "solid", True, overwrite=True) or changed
    changed = _set_attr_if_supported(block, "placeable", True, overwrite=True) or changed
    changed = _set_attr_if_supported(block, "breakable", True, overwrite=True) or changed
    changed = _set_attr_if_supported(block, "updated_by_user_id", "bootstrap", overwrite=True) or changed
    changed = _merge_metadata_json(
        block,
        {
            "debug": True,
            "color": color,
            "seededBy": "vectoplan-chunk.default_seed",
        },
    ) or changed

    return changed


# -----------------------------------------------------------------------------
# Seed operations
# -----------------------------------------------------------------------------

def seed_debug_blocks(
    app: Flask,
    models: dict[str, Any],
    block_defaults: Any,
    *,
    db_extension: Any = None,
) -> list[dict[str, Any]]:
    """Seed default debug block registry and block types idempotently."""
    operations: list[dict[str, Any]] = []

    BlockRegistry = models["BlockRegistry"]
    BlockType = models["BlockType"]

    registry_id = _safe_str(getattr(block_defaults, "registry_id", DEFAULT_BLOCK_REGISTRY_ID), DEFAULT_BLOCK_REGISTRY_ID)
    registry_version = _safe_str(
        getattr(block_defaults, "registry_version", DEFAULT_BLOCK_REGISTRY_VERSION),
        DEFAULT_BLOCK_REGISTRY_VERSION,
    )

    started_at = _utc_now_iso()
    registry = find_default_block_registry(models, block_defaults)

    if registry is None:
        registry = create_block_registry_object(BlockRegistry, block_defaults)
        _add_to_session(registry, db_extension)
        _flush_session(db_extension)

        operations.append(
            _make_operation(
                name="block_registry",
                ok=True,
                status=OP_STATUS_OK,
                created=True,
                message="Block registry created.",
                started_at=started_at,
                data={
                    "registryId": registry_id,
                    "registryVersion": registry_version,
                    "registryDbId": _safe_model_id(registry),
                },
            )
        )
    else:
        updated = apply_block_registry_defaults_to_object(registry, block_defaults)
        if updated:
            _flush_session(db_extension)

        operations.append(
            _make_operation(
                name="block_registry",
                ok=True,
                status=OP_STATUS_OK if updated else OP_STATUS_SKIPPED,
                updated=updated,
                skipped=not updated,
                message="Block registry updated." if updated else "Block registry already exists.",
                started_at=started_at,
                data={
                    "registryId": registry_id,
                    "registryVersion": registry_version,
                    "registryDbId": _safe_model_id(registry),
                },
            )
        )

    registry_db_id = _safe_model_id(registry)
    if registry_db_id is None:
        raise RuntimeError("Block registry has no database id after flush.")

    expected_block_ids: list[str] = []
    if _safe_bool(getattr(block_defaults, "seed_debug_grass", True), True):
        expected_block_ids.append("debug_grass")
    if _safe_bool(getattr(block_defaults, "seed_debug_dirt", True), True):
        expected_block_ids.append("debug_dirt")

    for block_type_id in expected_block_ids:
        op_started = _utc_now_iso()
        block = _query_first_by(
            BlockType,
            registry_db_id=registry_db_id,
            block_type_id=block_type_id,
        )
        if block is None:
            block = _query_first_by(
                BlockType,
                registry_id=registry_id,
                registry_version=registry_version,
                block_type_id=block_type_id,
            )

        if block is None:
            block = create_debug_block_object(BlockType, registry, block_type_id)
            _add_to_session(block, db_extension)
            _flush_session(db_extension)

            operations.append(
                _make_operation(
                    name=f"block_type:{block_type_id}",
                    ok=True,
                    status=OP_STATUS_OK,
                    created=True,
                    message="Debug block type created.",
                    started_at=op_started,
                    data={
                        "blockTypeId": block_type_id,
                        "registryId": registry_id,
                        "registryVersion": registry_version,
                        "blockDbId": _safe_model_id(block),
                    },
                )
            )
        else:
            updated = apply_debug_block_defaults_to_object(block, registry, block_type_id)
            if updated:
                _flush_session(db_extension)

            operations.append(
                _make_operation(
                    name=f"block_type:{block_type_id}",
                    ok=True,
                    status=OP_STATUS_OK if updated else OP_STATUS_SKIPPED,
                    updated=updated,
                    skipped=not updated,
                    message="Debug block type updated." if updated else "Debug block type already exists.",
                    started_at=op_started,
                    data={
                        "blockTypeId": block_type_id,
                        "registryId": registry_id,
                        "registryVersion": registry_version,
                        "blockDbId": _safe_model_id(block),
                    },
                )
            )

    return operations


def seed_dev_project_universe_world(
    app: Flask,
    models: dict[str, Any],
    world_defaults: Any,
    *,
    db_extension: Any = None,
) -> list[dict[str, Any]]:
    """Seed default Project, Universe and WorldInstance idempotently."""
    operations: list[dict[str, Any]] = []

    Project = models["Project"]
    Universe = models["Universe"]
    WorldInstance = models["WorldInstance"]

    project_id = _resolve_project_id(world_defaults)
    universe_id = _resolve_universe_id(world_defaults)
    world_id = _resolve_world_id(world_defaults)

    # Project
    started_at = _utc_now_iso()
    project = _query_first_by(Project, project_id=project_id)

    if project is None:
        project = create_project_object(Project, world_defaults)
        _add_to_session(project, db_extension)
        _flush_session(db_extension)

        operations.append(
            _make_operation(
                name="project",
                ok=True,
                status=OP_STATUS_OK,
                created=True,
                message="Default project created.",
                started_at=started_at,
                data={
                    "projectId": project_id,
                    "projectDbId": _safe_model_id(project),
                    "defaultUniverseId": universe_id,
                    "defaultWorldId": world_id,
                },
            )
        )
    else:
        updated = apply_project_defaults_to_object(project, world_defaults)
        if updated:
            _flush_session(db_extension)

        operations.append(
            _make_operation(
                name="project",
                ok=True,
                status=OP_STATUS_OK if updated else OP_STATUS_SKIPPED,
                updated=updated,
                skipped=not updated,
                message="Project updated." if updated else "Project already exists.",
                started_at=started_at,
                data={
                    "projectId": project_id,
                    "projectDbId": _safe_model_id(project),
                    "defaultUniverseId": universe_id,
                    "defaultWorldId": world_id,
                },
            )
        )

    project_db_id = _safe_model_id(project)
    if project_db_id is None:
        raise RuntimeError("Project has no database id after flush.")

    # Universe
    started_at = _utc_now_iso()
    universe = _query_first_by(
        Universe,
        project_db_id=project_db_id,
        universe_id=universe_id,
    )
    if universe is None:
        universe = _query_first_by(Universe, universe_id=universe_id)

    if universe is None:
        universe = create_universe_object(Universe, project, world_defaults)
        _add_to_session(universe, db_extension)
        _flush_session(db_extension)

        operations.append(
            _make_operation(
                name="universe",
                ok=True,
                status=OP_STATUS_OK,
                created=True,
                message="Default universe created.",
                started_at=started_at,
                data={
                    "projectId": project_id,
                    "universeId": universe_id,
                    "universeDbId": _safe_model_id(universe),
                    "defaultWorldId": world_id,
                },
            )
        )
    else:
        updated = apply_universe_defaults_to_object(universe, project, world_defaults)
        if updated:
            _flush_session(db_extension)

        operations.append(
            _make_operation(
                name="universe",
                ok=True,
                status=OP_STATUS_OK if updated else OP_STATUS_SKIPPED,
                updated=updated,
                skipped=not updated,
                message="Universe updated." if updated else "Universe already exists.",
                started_at=started_at,
                data={
                    "projectId": project_id,
                    "universeId": universe_id,
                    "universeDbId": _safe_model_id(universe),
                    "defaultWorldId": world_id,
                },
            )
        )

    universe_db_id = _safe_model_id(universe)
    if universe_db_id is None:
        raise RuntimeError("Universe has no database id after flush.")

    # World
    started_at = _utc_now_iso()
    world = _query_first_by(
        WorldInstance,
        universe_db_id=universe_db_id,
        world_id=world_id,
    )
    if world is None:
        world = _query_first_by(WorldInstance, world_id=world_id)

    if world is None:
        world = create_world_object(WorldInstance, project, universe, world_defaults)
        _add_to_session(world, db_extension)
        _flush_session(db_extension)

        operations.append(
            _make_operation(
                name="world",
                ok=True,
                status=OP_STATUS_OK,
                created=True,
                message="Default world created.",
                started_at=started_at,
                data={
                    "projectId": project_id,
                    "universeId": universe_id,
                    "worldId": world_id,
                    "worldDbId": _safe_model_id(world),
                    "templateId": _safe_str(getattr(world, "template_id", ""), ""),
                    "providerWorldId": _safe_str(getattr(world, "provider_world_id", ""), ""),
                },
            )
        )
    else:
        updated = False
        updated = _set_attr_if_supported(world, "project_db_id", project_db_id, overwrite=True) or updated
        updated = _set_attr_if_supported(world, "universe_db_id", universe_db_id, overwrite=True) or updated
        updated = apply_world_defaults_to_object(world, world_defaults) or updated

        if updated:
            _flush_session(db_extension)

        operations.append(
            _make_operation(
                name="world",
                ok=True,
                status=OP_STATUS_OK if updated else OP_STATUS_SKIPPED,
                updated=updated,
                skipped=not updated,
                message="World updated." if updated else "World already exists.",
                started_at=started_at,
                data={
                    "projectId": project_id,
                    "universeId": universe_id,
                    "worldId": world_id,
                    "worldDbId": _safe_model_id(world),
                    "templateId": _safe_str(getattr(world, "template_id", ""), ""),
                    "providerWorldId": _safe_str(getattr(world, "provider_world_id", ""), ""),
                },
            )
        )

    return operations


# -----------------------------------------------------------------------------
# Seed runner
# -----------------------------------------------------------------------------

def run_default_seed(
    app: Flask,
    *,
    seed_settings: SeedBootstrapSettings | None = None,
    world_defaults: WorldDefaultsSettings | None = None,
    block_defaults: BlockDefaultsSettings | None = None,
    db_extension: Any = None,
    enabled: bool | None = None,
    seed_debug_blocks_enabled: bool | None = None,
    seed_dev_project_enabled: bool | None = None,
    seed_on_empty_only: bool | None = None,
    fail_on_error: bool | None = None,
) -> DefaultSeedResult:
    """
    Run explicit default seed bootstrap.

    This is intended for a DB-bootstrap command/container, not normal runtime
    startup.
    """
    started_at = _utc_now_iso()

    result = DefaultSeedResult(
        ok=False,
        status=STATUS_FAILED,
        started_at=started_at,
    )

    if not _is_flask_app(app):
        result.errors.append(
            _make_message(
                code="invalid_flask_app",
                message="run_default_seed(app) expects a Flask app or compatible object.",
            )
        )
        return _finish_result(result)

    with _app_context(app):
        db_obj = _get_db_extension(db_extension)
        if db_obj is None:
            result.errors.append(
                _make_message(
                    code="db_extension_unavailable",
                    message="SQLAlchemy db extension is unavailable.",
                )
            )
            return _finish_or_raise(app, result, True)

        resolved_seed_settings = resolve_seed_settings(app, seed_settings)
        resolved_world_defaults = resolve_world_defaults(app, world_defaults)
        resolved_block_defaults = resolve_block_defaults(app, block_defaults)

        resolved_enabled = bool(
            enabled
            if enabled is not None
            else _safe_bool(getattr(resolved_seed_settings, "seed_defaults", False), False)
        )
        resolved_seed_blocks = bool(
            seed_debug_blocks_enabled
            if seed_debug_blocks_enabled is not None
            else _safe_bool(
                getattr(resolved_seed_settings, "seed_debug_blocks", resolved_enabled),
                resolved_enabled,
            )
        )
        resolved_seed_project = bool(
            seed_dev_project_enabled
            if seed_dev_project_enabled is not None
            else _safe_bool(
                getattr(resolved_seed_settings, "seed_dev_project", resolved_enabled),
                resolved_enabled,
            )
        )
        resolved_seed_on_empty_only = bool(
            seed_on_empty_only
            if seed_on_empty_only is not None
            else _safe_bool(getattr(resolved_seed_settings, "seed_on_empty_only", True), True)
        )
        resolved_fail_on_error = bool(
            fail_on_error
            if fail_on_error is not None
            else _safe_bool(getattr(resolved_seed_settings, "fail_on_error", True), True)
        )
        advisory_lock_enabled = _safe_bool(
            getattr(resolved_seed_settings, "advisory_lock_enabled", True),
            True,
        )

        result.enabled = resolved_enabled
        resolved_seed_system_blocks = bool(resolved_enabled)

        result.seed_defaults_requested = resolved_enabled
        result.seed_debug_blocks_requested = resolved_seed_blocks
        result.seed_system_blocks_requested = resolved_seed_system_blocks
        result.seed_dev_project_requested = resolved_seed_project
        result.seed_on_empty_only = resolved_seed_on_empty_only

        result.project_id = _resolve_project_id(resolved_world_defaults)
        result.universe_id = _resolve_universe_id(resolved_world_defaults)
        result.world_id = _resolve_world_id(resolved_world_defaults)
        result.template_id = _resolve_template_id(resolved_world_defaults)
        result.provider_id = _resolve_provider_id(resolved_world_defaults)
        result.provider_world_id = _resolve_provider_world_id(resolved_world_defaults)
        result.block_registry_id = _safe_str(
            getattr(resolved_block_defaults, "registry_id", DEFAULT_BLOCK_REGISTRY_ID),
            DEFAULT_BLOCK_REGISTRY_ID,
        )
        result.block_registry_version = _safe_str(
            getattr(resolved_block_defaults, "registry_version", DEFAULT_BLOCK_REGISTRY_VERSION),
            DEFAULT_BLOCK_REGISTRY_VERSION,
        )

        result.metadata["advisoryLockEnabled"] = advisory_lock_enabled
        result.metadata["failOnError"] = resolved_fail_on_error
        result.metadata["seedSystemBlocks"] = resolved_seed_system_blocks
        result.metadata["defaults"] = {
            "projectId": result.project_id,
            "universeId": result.universe_id,
            "worldId": result.world_id,
            "templateId": result.template_id,
            "providerId": result.provider_id,
            "providerWorldId": result.provider_world_id,
            "blockRegistryId": result.block_registry_id,
            "blockRegistryVersion": result.block_registry_version,
        }

        try:
            if build_lock_diagnostics is not None:
                result.metadata["lockDiagnostics"] = build_lock_diagnostics(app, db_extension)
        except Exception:
            pass

        if not resolved_enabled:
            result.operations.append(
                _make_operation(
                    name="default_seed",
                    ok=True,
                    status=OP_STATUS_SKIPPED,
                    skipped=True,
                    message="Default seed disabled by settings.",
                )
            )
            result.ok = True
            result.status = STATUS_SKIPPED
            return _finish_result(result)

        if (
            not resolved_seed_blocks
            and not resolved_seed_system_blocks
            and not resolved_seed_project
        ):
            result.operations.append(
                _make_operation(
                    name="default_seed",
                    ok=True,
                    status=OP_STATUS_SKIPPED,
                    skipped=True,
                    message="Default seed enabled, but no seed group is enabled.",
                )
            )
            result.ok = True
            result.status = STATUS_SKIPPED
            return _finish_result(result)

        _safe_log_info(app, "Default seed bootstrap started.")

        try:
            models = load_seed_model_classes()
        except Exception as exc:
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
                )
            )
            return _finish_or_raise(app, result, resolved_fail_on_error)

        try:
            result.pre_status = build_default_seed_status(app, db_extension=db_extension)
        except Exception:
            result.pre_status = {}

        def run_seed_body() -> None:
            if resolved_seed_on_empty_only and is_default_seed_complete(
                models,
                resolved_world_defaults,
                resolved_block_defaults,
                require_blocks=resolved_seed_blocks,
                require_system_blocks=resolved_seed_system_blocks,
                require_project=resolved_seed_project,
            ):
                result.seed_skipped_because_complete = True
                result.operations.append(
                    _make_operation(
                        name="default_seed",
                        ok=True,
                        status=OP_STATUS_SKIPPED,
                        skipped=True,
                        message="Default seed skipped because seed-on-empty-only is enabled and target defaults already exist.",
                        data={
                            "seedOnEmptyOnly": True,
                            "seedDebugBlocks": resolved_seed_blocks,
                            "seedSystemBlocks": resolved_seed_system_blocks,
                            "seedDevProject": resolved_seed_project,
                        },
                    )
                )
                return

            if resolved_seed_blocks:
                result.operations.extend(
                    seed_debug_blocks(
                        app,
                        models,
                        resolved_block_defaults,
                        db_extension=db_extension,
                    )
                )
            else:
                result.operations.append(
                    _make_operation(
                        name="debug_blocks",
                        ok=True,
                        status=OP_STATUS_SKIPPED,
                        skipped=True,
                        message="Debug block seeding disabled.",
                    )
                )

            if resolved_seed_system_blocks:
                result.operations.extend(
                    seed_system_blocks(
                        app,
                        models,
                        resolved_block_defaults,
                        db_extension=db_extension,
                    )
                )
            else:
                result.operations.append(
                    _make_operation(
                        name="system_blocks",
                        ok=True,
                        status=OP_STATUS_SKIPPED,
                        skipped=True,
                        message="Built-in system-block seeding disabled.",
                    )
                )

            if resolved_seed_project:
                result.operations.extend(
                    seed_dev_project_universe_world(
                        app,
                        models,
                        resolved_world_defaults,
                        db_extension=db_extension,
                    )
                )
            else:
                result.operations.append(
                    _make_operation(
                        name="dev_project",
                        ok=True,
                        status=OP_STATUS_SKIPPED,
                        skipped=True,
                        message="Dev project seeding disabled.",
                    )
                )

        try:
            if seed_bootstrap_lock is None:
                run_seed_body()
            else:
                with seed_bootstrap_lock(
                    app,
                    enabled=advisory_lock_enabled,
                    db_extension=db_extension,
                    fail_if_not_acquired=True,
                ) as lock_result:
                    result.lock_used = bool(not getattr(lock_result, "skipped", False))

                    if advisory_lock_result_to_dict is not None:
                        lock_data = advisory_lock_result_to_dict(lock_result)
                    else:
                        lock_data = asdict(lock_result)

                    result.operations.append(
                        _make_operation(
                            name="seed_bootstrap_lock",
                            ok=bool(lock_result.ok),
                            status=OP_STATUS_OK if lock_result.ok else OP_STATUS_FAILED,
                            skipped=bool(getattr(lock_result, "skipped", False)),
                            message="Seed bootstrap advisory lock acquired or skipped.",
                            data=lock_data,
                        )
                    )

                    run_seed_body()

            if result.errors:
                _cleanup_db_session(rollback=True, db_extension=db_extension)
                return _finish_or_raise(app, result, resolved_fail_on_error)

            _commit_session(db_extension)

            result.post_status = build_default_seed_status(app, db_extension=db_extension)
            _apply_status_to_result(result, result.post_status)

            if resolved_seed_project and not result.default_world_ready:
                result.errors.append(
                    _make_message(
                        code="default_world_not_ready_after_seed",
                        message="Default concrete world is not ready after seed.",
                        details=result.post_status,
                    )
                )

            if resolved_seed_blocks and not result.debug_blocks_ready:
                result.errors.append(
                    _make_message(
                        code="debug_blocks_not_ready_after_seed",
                        message="Default debug blocks are not ready after seed.",
                        details=result.post_status,
                    )
                )

            if resolved_seed_system_blocks and not result.system_blocks_ready:
                result.errors.append(
                    _make_message(
                        code="system_blocks_not_ready_after_seed",
                        message=(
                            "Built-in system blocks are not ready after seed."
                        ),
                        details=result.post_status,
                    )
                )

            if resolved_seed_system_blocks and not result.air_invariant_ready:
                result.errors.append(
                    _make_message(
                        code="air_invariant_not_ready_after_seed",
                        message=(
                            "Air persistence invariant is not ready after seed."
                        ),
                        details=result.post_status,
                    )
                )

            if resolved_seed_system_blocks and not result.system_railing_ready:
                result.errors.append(
                    _make_message(
                        code="system_railing_not_ready_after_seed",
                        message=(
                            "Built-in Railing mirror is not ready after seed."
                        ),
                        details=result.post_status,
                    )
                )

            if result.errors:
                result.ok = False
                result.status = STATUS_FAILED
                return _finish_or_raise(app, result, resolved_fail_on_error)

            result.ok = True
            result.status = STATUS_SKIPPED if result.seed_skipped_because_complete else STATUS_COMPLETED
            _safe_log_info(app, "Default seed bootstrap completed successfully.")
            return _finish_result(result)

        except Exception as exc:
            _cleanup_db_session(rollback=True, db_extension=db_extension)

            message = _safe_exception_message(exc)
            result.errors.append(
                _make_message(
                    code="default_seed_exception",
                    message=message,
                    details={
                        "exceptionType": exc.__class__.__name__,
                    },
                )
            )
            result.operations.append(
                _make_operation(
                    name="default_seed",
                    ok=False,
                    status=OP_STATUS_FAILED,
                    message=message,
                    data={
                        "exceptionType": exc.__class__.__name__,
                    },
                )
            )

            return _finish_or_raise(app, result, resolved_fail_on_error)

        finally:
            _cleanup_db_session(rollback=False, db_extension=db_extension)


def _apply_status_to_result(
    result: DefaultSeedResult,
    status: Mapping[str, Any],
) -> None:
    """Apply readiness and system-block counts from a status payload."""
    try:
        result.default_project_ready = _safe_bool(
            (status.get("project") or {}).get("exists"),
            False,
        )
        result.default_universe_ready = _safe_bool(
            (status.get("universe") or {}).get("exists"),
            False,
        )
        result.default_world_ready = _safe_bool(
            (status.get("world") or {}).get("exists"),
            False,
        )
        result.block_registry_ready = _safe_bool(
            (status.get("blockRegistry") or {}).get("exists"),
            False,
        )
        result.debug_blocks_ready = _safe_bool(
            (status.get("debugBlocks") or {}).get("complete"),
            False,
        )

        system_status = _safe_dict(
            status.get("systemBlocks")
        )

        result.system_blocks_ready = _safe_bool(
            system_status.get("ready"),
            False,
        )

        result.air_invariant_ready = _safe_bool(
            (_safe_dict(system_status.get("air"))).get("ready"),
            False,
        )

        result.system_railing_ready = _system_railing_ready(
            system_status
        )

        counts = _system_block_status_counts(
            system_status
        )

        operation_created = 0
        operation_updated = 0

        for raw_operation in result.operations:
            operation = _safe_dict(
                raw_operation
            )

            if _safe_str(
                operation.get("name"),
                "",
            ) != "system_blocks":
                continue

            operation_data = _safe_dict(
                operation.get("data")
            )

            operation_counts = (
                _system_block_status_counts(
                    operation_data
                )
            )

            operation_created = max(
                operation_created,
                operation_counts["created"],
            )
            operation_updated = max(
                operation_updated,
                operation_counts["updated"],
            )

        result.system_block_count = counts[
            "mirrors"
        ]
        result.system_blocks_created = max(
            result.system_blocks_created,
            operation_created,
            counts["created"],
        )
        result.system_blocks_updated = max(
            result.system_blocks_updated,
            operation_updated,
            counts["updated"],
        )
        result.system_blocks_missing = counts[
            "missing"
        ]

    except Exception:
        pass


def _finish_result(result: DefaultSeedResult) -> DefaultSeedResult:
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
    result: DefaultSeedResult,
    fail_on_error: bool,
) -> DefaultSeedResult:
    """Finish result and optionally raise."""
    _finish_result(result)

    if fail_on_error and not result.ok:
        first_error = result.errors[0] if result.errors else {}
        message = _safe_str(
            first_error.get("message") if isinstance(first_error, Mapping) else None,
            "Default seed failed.",
        )
        _safe_log_exception(app, "Default seed bootstrap failed: %s", message)
        raise RuntimeError(message)

    if not result.ok:
        _safe_log_warning(app, "Default seed bootstrap failed but fail_on_error=false.")

    return result


# -----------------------------------------------------------------------------
# Convenience APIs
# -----------------------------------------------------------------------------

def run_default_seed_if_enabled(
    app: Flask,
    *,
    db_extension: Any = None,
) -> DefaultSeedResult:
    """Run default seed using configured settings."""
    seed_settings = resolve_seed_settings(app, None)
    world_defaults = resolve_world_defaults(app, None)
    block_defaults = resolve_block_defaults(app, None)

    return run_default_seed(
        app,
        seed_settings=seed_settings,
        world_defaults=world_defaults,
        block_defaults=block_defaults,
        db_extension=db_extension,
    )


def build_default_seed_status(
    app: Flask,
    *,
    db_extension: Any = None,
) -> dict[str, Any]:
    """
    Build read-only default seed status.

    This does not create or update anything. Built-in system-block status is
    evaluated against the same default BlockRegistry used by the concrete world.
    """
    started_at = _utc_now_iso()

    with _app_context(app):
        try:
            models = load_seed_model_classes()
            world_defaults = resolve_world_defaults(
                app,
                None,
            )
            block_defaults = resolve_block_defaults(
                app,
                None,
            )

            registry = find_default_block_registry(
                models,
                block_defaults,
            )
            project = find_default_project(
                models,
                world_defaults,
            )
            universe = (
                find_default_universe(
                    models,
                    project,
                    world_defaults,
                )
                if project is not None
                else None
            )
            world = (
                find_default_world(
                    models,
                    universe,
                    world_defaults,
                )
                if universe is not None
                else None
            )

            debug_blocks_ok = (
                default_debug_blocks_exist(
                    models,
                    registry,
                    block_defaults,
                )
            )

            system_blocks_status = (
                build_default_system_blocks_status(
                    registry
                )
            )

            system_blocks_ready = _safe_bool(
                system_blocks_status.get("ready"),
                False,
            )

            air_invariant_ready = _safe_bool(
                (
                    _safe_dict(
                        system_blocks_status.get("air")
                    )
                ).get("ready"),
                False,
            )

            system_railing_ready = (
                _system_railing_ready(
                    system_blocks_status
                )
            )

            system_counts = (
                _system_block_status_counts(
                    system_blocks_status
                )
            )

            project_exists = project is not None
            universe_exists = universe is not None
            world_exists = world is not None
            registry_exists = registry is not None

            complete = bool(
                registry_exists
                and debug_blocks_ok
                and system_blocks_ready
                and air_invariant_ready
                and system_railing_ready
                and project_exists
                and universe_exists
                and world_exists
            )

            completed_at = _utc_now_iso()

            project_id = _resolve_project_id(
                world_defaults
            )
            universe_id = _resolve_universe_id(
                world_defaults
            )
            world_id = _resolve_world_id(
                world_defaults
            )
            template_id = _resolve_template_id(
                world_defaults
            )
            provider_id = _resolve_provider_id(
                world_defaults
            )
            provider_world_id = (
                _resolve_provider_world_id(
                    world_defaults
                )
            )
            registry_id = _safe_str(
                getattr(
                    block_defaults,
                    "registry_id",
                    DEFAULT_BLOCK_REGISTRY_ID,
                ),
                DEFAULT_BLOCK_REGISTRY_ID,
            )
            registry_version = _safe_str(
                getattr(
                    block_defaults,
                    "registry_version",
                    DEFAULT_BLOCK_REGISTRY_VERSION,
                ),
                DEFAULT_BLOCK_REGISTRY_VERSION,
            )

            return {
                "ok": complete,
                "status": (
                    STATUS_READY
                    if complete
                    else STATUS_PARTIAL
                ),
                "startedAt": started_at,
                "completedAt": completed_at,
                "durationMs": _duration_ms(
                    started_at,
                    completed_at,
                ),
                "defaults": {
                    "projectId": project_id,
                    "universeId": universe_id,
                    "worldId": world_id,
                    "templateId": template_id,
                    "providerId": provider_id,
                    "providerWorldId": provider_world_id,
                    "blockRegistryId": registry_id,
                    "blockRegistryVersion": (
                        registry_version
                    ),
                    "systemRailingBlockTypeId": (
                        DEFAULT_SYSTEM_RAILING_BLOCK_TYPE_ID
                    ),
                    "airSystemBlockId": (
                        DEFAULT_SYSTEM_AIR_BLOCK_ID
                    ),
                },
                "project": {
                    "exists": project_exists,
                    "projectId": project_id,
                    "dbId": _safe_model_id(project),
                    "defaultUniverseId": (
                        _safe_str(
                            getattr(
                                project,
                                "default_universe_id",
                                "",
                            ),
                            "",
                        )
                        if project is not None
                        else None
                    ),
                    "defaultWorldId": (
                        _safe_str(
                            getattr(
                                project,
                                "default_world_id",
                                "",
                            ),
                            "",
                        )
                        if project is not None
                        else None
                    ),
                    "spawnWorldId": (
                        _safe_str(
                            getattr(
                                project,
                                "spawn_world_id",
                                "",
                            ),
                            "",
                        )
                        if project is not None
                        else None
                    ),
                },
                "universe": {
                    "exists": universe_exists,
                    "universeId": universe_id,
                    "dbId": _safe_model_id(universe),
                    "defaultWorldId": (
                        _safe_str(
                            getattr(
                                universe,
                                "default_world_id",
                                "",
                            ),
                            "",
                        )
                        if universe is not None
                        else None
                    ),
                    "spawnWorldId": (
                        _safe_str(
                            getattr(
                                universe,
                                "spawn_world_id",
                                "",
                            ),
                            "",
                        )
                        if universe is not None
                        else None
                    ),
                },
                "world": {
                    "exists": world_exists,
                    "worldId": world_id,
                    "dbId": _safe_model_id(world),
                    "templateId": (
                        _safe_str(
                            getattr(
                                world,
                                "template_id",
                                "",
                            ),
                            "",
                        )
                        if world is not None
                        else None
                    ),
                    "providerId": (
                        _safe_str(
                            getattr(
                                world,
                                "provider_id",
                                "",
                            ),
                            "",
                        )
                        if world is not None
                        else None
                    ),
                    "providerWorldId": (
                        _safe_str(
                            getattr(
                                world,
                                "provider_world_id",
                                "",
                            ),
                            "",
                        )
                        if world is not None
                        else None
                    ),
                    "blockRegistryId": (
                        _safe_str(
                            getattr(
                                world,
                                "block_registry_id",
                                "",
                            ),
                            "",
                        )
                        if world is not None
                        else None
                    ),
                    "blockRegistryVersion": (
                        _safe_str(
                            getattr(
                                world,
                                "block_registry_version",
                                "",
                            ),
                            "",
                        )
                        if world is not None
                        else None
                    ),
                },
                "blockRegistry": {
                    "exists": registry_exists,
                    "registryId": registry_id,
                    "registryVersion": registry_version,
                    "dbId": _safe_model_id(registry),
                },
                "debugBlocks": {
                    "complete": debug_blocks_ok,
                },
                "systemBlocks": {
                    **system_blocks_status,
                    "summary": {
                        "ready": system_blocks_ready,
                        "airInvariantReady": (
                            air_invariant_ready
                        ),
                        "systemRailingReady": (
                            system_railing_ready
                        ),
                        "mirrorCount": system_counts[
                            "mirrors"
                        ],
                        "readyMirrorCount": system_counts[
                            "readyMirrors"
                        ],
                        "missingCount": system_counts[
                            "missing"
                        ],
                        "driftedCount": system_counts[
                            "drifted"
                        ],
                    },
                },
                "ready": {
                    "project": project_exists,
                    "universe": universe_exists,
                    "world": world_exists,
                    "blockRegistry": registry_exists,
                    "debugBlocks": debug_blocks_ok,
                    "systemBlocks": system_blocks_ready,
                    "airInvariant": air_invariant_ready,
                    "systemRailing": system_railing_ready,
                },
            }

        except Exception as exc:
            completed_at = _utc_now_iso()

            return {
                "ok": False,
                "status": STATUS_FAILED,
                "startedAt": started_at,
                "completedAt": completed_at,
                "durationMs": _duration_ms(
                    started_at,
                    completed_at,
                ),
                "error": _safe_exception_message(exc),
                "exceptionType": (
                    exc.__class__.__name__
                ),
            }


def default_seed_result_to_dict(
    result: DefaultSeedResult | Mapping[str, Any] | Any,
) -> dict[str, Any]:
    """Serialize default seed result to dict."""
    if isinstance(result, DefaultSeedResult):
        return result.to_dict()

    if isinstance(result, Mapping):
        try:
            return dict(result)
        except Exception:
            return {}

    return _safe_dict(result)


def build_default_seed_summary(
    result: DefaultSeedResult | Mapping[str, Any] | Any,
) -> dict[str, Any]:
    """Build compact default seed summary."""
    data = default_seed_result_to_dict(result)

    operations = data.get("operations") or []
    created_count = 0
    updated_count = 0
    skipped_count = 0

    try:
        created_count = sum(1 for op in operations if bool(op.get("created")))
        updated_count = sum(1 for op in operations if bool(op.get("updated")))
        skipped_count = sum(1 for op in operations if bool(op.get("skipped")))
    except Exception:
        pass

    return {
        "ok": bool(data.get("ok")),
        "status": _safe_str(data.get("status"), "unknown"),
        "enabled": bool(data.get("enabled")),
        "seedDefaultsRequested": bool(data.get("seed_defaults_requested")),
        "seedDebugBlocksRequested": bool(data.get("seed_debug_blocks_requested")),
        "seedSystemBlocksRequested": bool(data.get("seed_system_blocks_requested")),
        "seedDevProjectRequested": bool(data.get("seed_dev_project_requested")),
        "seedOnEmptyOnly": bool(data.get("seed_on_empty_only")),
        "seedSkippedBecauseComplete": bool(data.get("seed_skipped_because_complete")),
        "lockUsed": bool(data.get("lock_used")),
        "projectId": data.get("project_id"),
        "universeId": data.get("universe_id"),
        "worldId": data.get("world_id"),
        "templateId": data.get("template_id"),
        "providerId": data.get("provider_id"),
        "providerWorldId": data.get("provider_world_id"),
        "blockRegistryId": data.get("block_registry_id"),
        "blockRegistryVersion": data.get("block_registry_version"),
        "defaultProjectReady": data.get("default_project_ready"),
        "defaultUniverseReady": data.get("default_universe_ready"),
        "defaultWorldReady": data.get("default_world_ready"),
        "blockRegistryReady": data.get("block_registry_ready"),
        "debugBlocksReady": data.get("debug_blocks_ready"),
        "systemBlocksReady": data.get("system_blocks_ready"),
        "systemRailingReady": data.get("system_railing_ready"),
        "airInvariantReady": data.get("air_invariant_ready"),
        "systemBlockCount": data.get("system_block_count"),
        "systemBlocksCreated": data.get("system_blocks_created"),
        "systemBlocksUpdated": data.get("system_blocks_updated"),
        "systemBlocksMissing": data.get("system_blocks_missing"),
        "operationCount": len(operations),
        "createdCount": created_count,
        "updatedCount": updated_count,
        "skippedCount": skipped_count,
        "warningCount": len(data.get("warnings") or []),
        "errorCount": len(data.get("errors") or []),
        "durationMs": data.get("duration_ms"),
    }


# -----------------------------------------------------------------------------
# Public exports
# -----------------------------------------------------------------------------

__all__ = [
    "DEFAULT_SEED_RESULT_VERSION",
    "OP_STATUS_FAILED",
    "OP_STATUS_OK",
    "OP_STATUS_SKIPPED",
    "OP_STATUS_WARNING",
    "STATUS_COMPLETED",
    "STATUS_FAILED",
    "STATUS_PARTIAL",
    "STATUS_READY",
    "STATUS_SKIPPED",
    "DefaultSeedMessage",
    "DefaultSeedOperation",
    "DefaultSeedResult",
    "apply_block_registry_defaults_to_object",
    "apply_debug_block_defaults_to_object",
    "apply_project_defaults_to_object",
    "apply_universe_defaults_to_object",
    "apply_world_defaults_to_object",
    "build_default_seed_status",
    "build_default_system_blocks_status",
    "build_default_seed_summary",
    "create_block_registry_object",
    "create_debug_block_object",
    "create_project_object",
    "create_universe_object",
    "create_world_object",
    "default_debug_blocks_exist",
    "default_system_blocks_exist",
    "default_seed_result_to_dict",
    "find_default_block_registry",
    "find_default_project",
    "find_default_universe",
    "find_default_world",
    "is_default_seed_complete",
    "load_seed_model_classes",
    "load_system_block_bootstrap_api",
    "clear_default_seed_system_block_caches",
    "resolve_block_defaults",
    "resolve_seed_settings",
    "resolve_world_defaults",
    "run_default_seed",
    "run_default_seed_if_enabled",
    "seed_debug_blocks",
    "seed_system_blocks",
    "seed_dev_project_universe_world",
]