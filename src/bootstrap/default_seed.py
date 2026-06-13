# services/vectoplan-chunk/src/bootstrap/default_seed.py
"""
Explicit default seed bootstrap for the `vectoplan-chunk` service.

This module owns the controlled default seed path.

Responsibilities:
- seed the default development Project
- seed the default development Universe
- seed the default editable WorldInstance
- seed the default debug BlockRegistry
- seed the default debug BlockType entries
- keep seeding idempotent
- avoid ORM relationship traversal
- avoid loading chunks, snapshots, events, commands or object refs
- protect seed operations with PostgreSQL advisory locks
- cleanup SQLAlchemy sessions after seed work
- return serializable results for scripts/logs/status output

Important boundaries:
- no db.create_all() here
- no Alembic migrations here
- no chunk generation here
- no ChunkSnapshot reads here
- no ChunkEvent reads here
- no WorldCommandLog reads here
- no WorldObjectInstance reads here
- no WorldObjectChunkRef reads here
- no request handling here

Design rule:

    Runtime startup must not call this module automatically.
    This module is for explicit DB bootstrap only.

Target default graph:

    Project(project_id="dev-project")
      -> Universe(universe_id="dev-universe")
          -> WorldInstance(world_id="world_spawn", provider_world_id="flat")

Target default block registry:

    BlockRegistry(registry_id="debug-blocks", registry_version="1")
      -> BlockType(block_type_id="debug_grass")
      -> BlockType(block_type_id="debug_dirt")
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

DEFAULT_SEED_RESULT_VERSION: Final[str] = "default-seed-result.v1"

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
    seed_dev_project_requested: bool = False
    seed_on_empty_only: bool = True

    lock_used: bool = False
    seed_skipped_because_complete: bool = False

    project_id: str | None = None
    universe_id: str | None = None
    world_id: str | None = None
    block_registry_id: str | None = None
    block_registry_version: str | None = None

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


def _set_attr_if_supported(
    obj: Any,
    name: str,
    value: Any,
    *,
    overwrite: bool = True,
) -> bool:
    """Set attribute if object supports it."""
    if obj is None:
        return False

    try:
        if not hasattr(obj, name):
            return False
    except Exception:
        return False

    try:
        current_value = getattr(obj, name, None)
    except Exception:
        current_value = None

    if not overwrite and current_value not in (None, ""):
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
    """Run bounded one_or_none query by filter_by."""
    try:
        return model_class.query.filter_by(**filters).one_or_none()
    except Exception as exc:
        raise RuntimeError(
            f"Could not query {getattr(model_class, '__name__', model_class)} by {filters}: "
            f"{_safe_exception_message(exc)}"
        ) from exc


def _exists_by(model_class: Any, **filters: Any) -> bool:
    """Return whether a row exists for filter."""
    try:
        return _query_one_by(model_class, **filters) is not None
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


# -----------------------------------------------------------------------------
# Settings resolution
# -----------------------------------------------------------------------------

def _fallback_world_defaults() -> Any:
    """Create fallback world defaults object."""

    class FallbackWorldDefaults:
        project_id = DEFAULT_PROJECT_ID
        project_slug = DEFAULT_PROJECT_SLUG
        project_name = DEFAULT_PROJECT_NAME

        universe_id = DEFAULT_UNIVERSE_ID
        universe_slug = DEFAULT_UNIVERSE_SLUG
        universe_name = DEFAULT_UNIVERSE_NAME

        world_id = DEFAULT_WORLD_ID
        world_slug = DEFAULT_WORLD_SLUG
        world_name = DEFAULT_WORLD_NAME

        template_id = DEFAULT_TEMPLATE_ID
        provider_id = DEFAULT_PROVIDER_ID
        provider_world_id = DEFAULT_PROVIDER_WORLD_ID

        world_type = "runtime-world"
        world_role = "default_spawn"
        world_scope = "project"
        world_owner_type = "project"

        generator_type = DEFAULT_GENERATOR_TYPE
        generator_version = DEFAULT_GENERATOR_VERSION
        projection_type = DEFAULT_PROJECTION_TYPE
        topology_type = DEFAULT_TOPOLOGY_TYPE
        coordinate_system = DEFAULT_COORDINATE_SYSTEM

        chunk_size = DEFAULT_CHUNK_SIZE
        cell_size = DEFAULT_CELL_SIZE
        surface_y = DEFAULT_SURFACE_Y
        min_y = DEFAULT_MIN_Y
        max_y = DEFAULT_MAX_Y
        seed = DEFAULT_SEED

        block_registry_id = DEFAULT_BLOCK_REGISTRY_ID
        block_registry_version = DEFAULT_BLOCK_REGISTRY_VERSION

        spawn_x = 0
        spawn_y = 2
        spawn_z = 0
        spawn_yaw = 0.0
        spawn_pitch = 0.0

    return FallbackWorldDefaults()


def _fallback_block_defaults() -> Any:
    """Create fallback block defaults object."""

    class FallbackBlockDefaults:
        registry_id = DEFAULT_BLOCK_REGISTRY_ID
        registry_version = DEFAULT_BLOCK_REGISTRY_VERSION
        seed_debug_grass = True
        seed_debug_dirt = True

    return FallbackBlockDefaults()


def _fallback_seed_settings() -> Any:
    """Create fallback seed settings object."""

    class FallbackSeedSettings:
        seed_defaults = False
        seed_debug_blocks = False
        seed_dev_project = False
        seed_on_empty_only = True
        advisory_lock_enabled = True
        fail_on_error = True

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

    return _fallback_world_defaults()


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

    return _fallback_block_defaults()


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

    return _fallback_seed_settings()


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

    return _query_one_by(
        BlockRegistry,
        registry_id=registry_id,
        registry_version=registry_version,
    )


def find_default_project(
    models: dict[str, Any],
    world_defaults: Any,
) -> Any | None:
    """Find default project by stable project_id."""
    Project = models["Project"]
    project_id = _safe_str(
        getattr(world_defaults, "project_id", DEFAULT_PROJECT_ID),
        DEFAULT_PROJECT_ID,
    )
    return _query_one_by(Project, project_id=project_id)


def find_default_universe(
    models: dict[str, Any],
    project: Any,
    world_defaults: Any,
) -> Any | None:
    """Find default universe by project_db_id + universe_id."""
    Universe = models["Universe"]
    project_id = _safe_model_id(project)
    universe_id = _safe_str(
        getattr(world_defaults, "universe_id", DEFAULT_UNIVERSE_ID),
        DEFAULT_UNIVERSE_ID,
    )

    if project_id is None:
        return None

    return _query_one_by(
        Universe,
        project_db_id=project_id,
        universe_id=universe_id,
    )


def find_default_world(
    models: dict[str, Any],
    universe: Any,
    world_defaults: Any,
) -> Any | None:
    """Find default world by universe_db_id + world_id."""
    WorldInstance = models["WorldInstance"]
    universe_id = _safe_model_id(universe)
    world_id = _safe_str(
        getattr(world_defaults, "world_id", DEFAULT_WORLD_ID),
        DEFAULT_WORLD_ID,
    )

    if universe_id is None:
        return None

    return _query_one_by(
        WorldInstance,
        universe_db_id=universe_id,
        world_id=world_id,
    )


def default_debug_blocks_exist(
    models: dict[str, Any],
    registry: Any,
    block_defaults: Any,
) -> bool:
    """Return whether default debug block types exist."""
    if registry is None:
        return False

    BlockType = models["BlockType"]
    registry_id = _safe_model_id(registry)

    if registry_id is None:
        return False

    expected_blocks: list[str] = []

    if _safe_bool(getattr(block_defaults, "seed_debug_grass", True), True):
        expected_blocks.append("debug_grass")
    if _safe_bool(getattr(block_defaults, "seed_debug_dirt", True), True):
        expected_blocks.append("debug_dirt")

    for block_type_id in expected_blocks:
        if not _exists_by(
            BlockType,
            registry_db_id=registry_id,
            block_type_id=block_type_id,
        ):
            return False

    return True


def is_default_seed_complete(
    models: dict[str, Any],
    world_defaults: Any,
    block_defaults: Any,
    *,
    require_blocks: bool = True,
    require_project: bool = True,
) -> bool:
    """Return whether target default seed graph is complete."""
    try:
        if require_blocks:
            registry = find_default_block_registry(models, block_defaults)
            if registry is None:
                return False
            if not default_debug_blocks_exist(models, registry, block_defaults):
                return False

        if require_project:
            project = find_default_project(models, world_defaults)
            if project is None:
                return False

            universe = find_default_universe(models, project, world_defaults)
            if universe is None:
                return False

            world = find_default_world(models, universe, world_defaults)
            if world is None:
                return False

        return True
    except Exception:
        return False


# -----------------------------------------------------------------------------
# Creation helpers
# -----------------------------------------------------------------------------

def create_project_object(model_class: Any, world_defaults: Any) -> Any:
    """Create Project instance using model factory if available."""
    project_id = _safe_str(getattr(world_defaults, "project_id", DEFAULT_PROJECT_ID), DEFAULT_PROJECT_ID)
    project_slug = _safe_str(getattr(world_defaults, "project_slug", DEFAULT_PROJECT_SLUG), DEFAULT_PROJECT_SLUG)
    project_name = _safe_str(getattr(world_defaults, "project_name", DEFAULT_PROJECT_NAME), DEFAULT_PROJECT_NAME)
    universe_id = _safe_str(getattr(world_defaults, "universe_id", DEFAULT_UNIVERSE_ID), DEFAULT_UNIVERSE_ID)

    metadata_json = {
        "seededBy": "vectoplan-chunk.default_seed",
        "seededAt": _utc_now_iso(),
    }

    create_method = getattr(model_class, "create", None)

    if callable(create_method):
        try:
            return create_method(
                project_id=project_id,
                slug=project_slug,
                name=project_name,
                default_universe_id=universe_id,
                metadata_json=metadata_json,
            )
        except Exception:
            pass

    values = {
        "project_id": project_id,
        "slug": project_slug,
        "name": project_name,
        "description": "Default development project for VECTOPLAN Chunk Service.",
        "status": "active",
        "schema_version": "1",
        "revision": 1,
        "default_universe_id": universe_id,
        "owner_type": "system",
        "owner_id": "vectoplan-chunk",
        "metadata_json": metadata_json,
        "created_at": _utc_now(),
        "updated_at": _utc_now(),
    }
    return _instantiate_model(model_class, values)


def create_universe_object(model_class: Any, project: Any, world_defaults: Any) -> Any:
    """Create Universe instance using model factory if available."""
    project_db_id = _safe_model_id(project)
    universe_id = _safe_str(getattr(world_defaults, "universe_id", DEFAULT_UNIVERSE_ID), DEFAULT_UNIVERSE_ID)
    universe_slug = _safe_str(getattr(world_defaults, "universe_slug", DEFAULT_UNIVERSE_SLUG), DEFAULT_UNIVERSE_SLUG)
    universe_name = _safe_str(getattr(world_defaults, "universe_name", DEFAULT_UNIVERSE_NAME), DEFAULT_UNIVERSE_NAME)
    world_id = _safe_str(getattr(world_defaults, "world_id", DEFAULT_WORLD_ID), DEFAULT_WORLD_ID)

    metadata_json = {
        "seededBy": "vectoplan-chunk.default_seed",
        "seededAt": _utc_now_iso(),
    }

    create_method = getattr(model_class, "create", None)

    if callable(create_method):
        try:
            return create_method(
                project_db_id=project_db_id,
                universe_id=universe_id,
                slug=universe_slug,
                name=universe_name,
                default_world_id=world_id,
                spawn_world_id=world_id,
                metadata_json=metadata_json,
            )
        except Exception:
            pass

    values = {
        "project_db_id": project_db_id,
        "universe_id": universe_id,
        "slug": universe_slug,
        "name": universe_name,
        "description": "Default development universe for VECTOPLAN Chunk Service.",
        "status": "active",
        "schema_version": "1",
        "revision": 1,
        "default_world_id": world_id,
        "spawn_world_id": world_id,
        "metadata_json": metadata_json,
        "created_at": _utc_now(),
        "updated_at": _utc_now(),
    }
    return _instantiate_model(model_class, values)


def create_world_object(model_class: Any, project: Any, universe: Any, world_defaults: Any) -> Any:
    """Create WorldInstance using model factory if available."""
    project_db_id = _safe_model_id(project)
    universe_db_id = _safe_model_id(universe)

    world_id = _safe_str(getattr(world_defaults, "world_id", DEFAULT_WORLD_ID), DEFAULT_WORLD_ID)
    world_slug = _safe_str(getattr(world_defaults, "world_slug", DEFAULT_WORLD_SLUG), DEFAULT_WORLD_SLUG)
    world_name = _safe_str(getattr(world_defaults, "world_name", DEFAULT_WORLD_NAME), DEFAULT_WORLD_NAME)

    template_id = _safe_str(getattr(world_defaults, "template_id", DEFAULT_TEMPLATE_ID), DEFAULT_TEMPLATE_ID)
    provider_id = _safe_str(getattr(world_defaults, "provider_id", DEFAULT_PROVIDER_ID), DEFAULT_PROVIDER_ID)
    provider_world_id = _safe_str(
        getattr(world_defaults, "provider_world_id", DEFAULT_PROVIDER_WORLD_ID),
        DEFAULT_PROVIDER_WORLD_ID,
    )

    metadata_json = {
        "seededBy": "vectoplan-chunk.default_seed",
        "seededAt": _utc_now_iso(),
        "templateId": template_id,
        "providerWorldId": provider_world_id,
    }

    create_flat_spawn = getattr(model_class, "create_flat_spawn", None)

    if callable(create_flat_spawn):
        try:
            world = create_flat_spawn(
                project_db_id=project_db_id,
                universe_db_id=universe_db_id,
                world_id=world_id,
                slug=world_slug,
                name=world_name,
                metadata_json=metadata_json,
            )
            apply_world_defaults_to_object(world, world_defaults)
            return world
        except Exception:
            pass

    values = {
        "project_db_id": project_db_id,
        "universe_db_id": universe_db_id,
        "world_id": world_id,
        "slug": world_slug,
        "name": world_name,
        "description": "Default flat spawn world for VECTOPLAN Chunk Service.",
        "status": "active",
        "schema_version": "1",
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
        "block_registry_id": _safe_str(getattr(world_defaults, "block_registry_id", DEFAULT_BLOCK_REGISTRY_ID), DEFAULT_BLOCK_REGISTRY_ID),
        "block_registry_version": _safe_str(
            getattr(world_defaults, "block_registry_version", DEFAULT_BLOCK_REGISTRY_VERSION),
            DEFAULT_BLOCK_REGISTRY_VERSION,
        ),
        "spawn_x": _safe_int(getattr(world_defaults, "spawn_x", 0), 0),
        "spawn_y": _safe_int(getattr(world_defaults, "spawn_y", 2), 2),
        "spawn_z": _safe_int(getattr(world_defaults, "spawn_z", 0), 0),
        "spawn_yaw": _safe_float(getattr(world_defaults, "spawn_yaw", 0.0), 0.0),
        "spawn_pitch": _safe_float(getattr(world_defaults, "spawn_pitch", 0.0), 0.0),
        "metadata_json": metadata_json,
        "created_at": _utc_now(),
        "updated_at": _utc_now(),
    }
    world = _instantiate_model(model_class, values)
    apply_world_defaults_to_object(world, world_defaults)
    return world


def apply_world_defaults_to_object(world: Any, world_defaults: Any) -> None:
    """Apply config-driven world defaults to an existing/new WorldInstance object."""
    assignments = {
        "template_id": _safe_str(getattr(world_defaults, "template_id", DEFAULT_TEMPLATE_ID), DEFAULT_TEMPLATE_ID),
        "provider_id": _safe_str(getattr(world_defaults, "provider_id", DEFAULT_PROVIDER_ID), DEFAULT_PROVIDER_ID),
        "provider_world_id": _safe_str(
            getattr(world_defaults, "provider_world_id", DEFAULT_PROVIDER_WORLD_ID),
            DEFAULT_PROVIDER_WORLD_ID,
        ),
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
        "block_registry_id": _safe_str(getattr(world_defaults, "block_registry_id", DEFAULT_BLOCK_REGISTRY_ID), DEFAULT_BLOCK_REGISTRY_ID),
        "block_registry_version": _safe_str(
            getattr(world_defaults, "block_registry_version", DEFAULT_BLOCK_REGISTRY_VERSION),
            DEFAULT_BLOCK_REGISTRY_VERSION,
        ),
        "spawn_x": _safe_int(getattr(world_defaults, "spawn_x", 0), 0),
        "spawn_y": _safe_int(getattr(world_defaults, "spawn_y", 2), 2),
        "spawn_z": _safe_int(getattr(world_defaults, "spawn_z", 0), 0),
        "spawn_yaw": _safe_float(getattr(world_defaults, "spawn_yaw", 0.0), 0.0),
        "spawn_pitch": _safe_float(getattr(world_defaults, "spawn_pitch", 0.0), 0.0),
    }

    for name, value in assignments.items():
        _set_attr_if_supported(world, name, value, overwrite=True)


def create_block_registry_object(model_class: Any, block_defaults: Any) -> Any:
    """Create BlockRegistry object using model factory if available."""
    registry_id = _safe_str(getattr(block_defaults, "registry_id", DEFAULT_BLOCK_REGISTRY_ID), DEFAULT_BLOCK_REGISTRY_ID)
    registry_version = _safe_str(
        getattr(block_defaults, "registry_version", DEFAULT_BLOCK_REGISTRY_VERSION),
        DEFAULT_BLOCK_REGISTRY_VERSION,
    )

    create_debug_registry = getattr(model_class, "create_debug_registry", None)

    if callable(create_debug_registry) and registry_id == "debug-blocks" and registry_version == "1":
        try:
            return create_debug_registry(is_default=True)
        except Exception:
            pass

    create_method = getattr(model_class, "create", None)

    if callable(create_method):
        try:
            return create_method(
                registry_id=registry_id,
                registry_version=registry_version,
                label=f"{registry_id} {registry_version}",
                is_default=True,
            )
        except Exception:
            pass

    values = {
        "registry_id": registry_id,
        "registry_version": registry_version,
        "label": f"{registry_id} {registry_version}",
        "description": "Default debug block registry for VECTOPLAN Chunk Service.",
        "status": "active",
        "is_default": True,
        "schema_version": "1",
        "metadata_json": {
            "seededBy": "vectoplan-chunk.default_seed",
            "seededAt": _utc_now_iso(),
        },
        "created_at": _utc_now(),
        "updated_at": _utc_now(),
    }
    return _instantiate_model(model_class, values)


def create_debug_block_object(model_class: Any, registry: Any, block_type_id: str) -> Any:
    """Create debug BlockType object using model factory if available."""
    block_type_id = _safe_str(block_type_id, "")

    if block_type_id == "debug_grass":
        factory = getattr(model_class, "create_debug_grass", None)
        if callable(factory):
            try:
                return factory(registry)
            except Exception:
                pass

        label = "Debug Grass"
        color = "#54b948"

    elif block_type_id == "debug_dirt":
        factory = getattr(model_class, "create_debug_dirt", None)
        if callable(factory):
            try:
                return factory(registry)
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
            return create_method(
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
        "created_at": _utc_now(),
        "updated_at": _utc_now(),
    }
    return _instantiate_model(model_class, values)


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
    registry = _query_one_by(
        BlockRegistry,
        registry_id=registry_id,
        registry_version=registry_version,
    )

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
        operations.append(
            _make_operation(
                name="block_registry",
                ok=True,
                status=OP_STATUS_SKIPPED,
                skipped=True,
                message="Block registry already exists.",
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
        block = _query_one_by(
            BlockType,
            registry_db_id=registry_db_id,
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
            operations.append(
                _make_operation(
                    name=f"block_type:{block_type_id}",
                    ok=True,
                    status=OP_STATUS_SKIPPED,
                    skipped=True,
                    message="Debug block type already exists.",
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

    project_id = _safe_str(getattr(world_defaults, "project_id", DEFAULT_PROJECT_ID), DEFAULT_PROJECT_ID)
    universe_id = _safe_str(getattr(world_defaults, "universe_id", DEFAULT_UNIVERSE_ID), DEFAULT_UNIVERSE_ID)
    world_id = _safe_str(getattr(world_defaults, "world_id", DEFAULT_WORLD_ID), DEFAULT_WORLD_ID)

    # Project
    started_at = _utc_now_iso()
    project = _query_one_by(Project, project_id=project_id)

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
                },
            )
        )
    else:
        updated = False

        if not _safe_str(getattr(project, "default_universe_id", ""), ""):
            if not _call_if_available(project, "set_default_universe_id", universe_id):
                updated = _set_attr_if_supported(
                    project,
                    "default_universe_id",
                    universe_id,
                    overwrite=True,
                )
            else:
                updated = True

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
                },
            )
        )

    project_db_id = _safe_model_id(project)
    if project_db_id is None:
        raise RuntimeError("Project has no database id after flush.")

    # Universe
    started_at = _utc_now_iso()
    universe = _query_one_by(
        Universe,
        project_db_id=project_db_id,
        universe_id=universe_id,
    )

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
                },
            )
        )
    else:
        updated = False

        if not _safe_str(getattr(universe, "default_world_id", ""), ""):
            if not _call_if_available(universe, "set_default_world_id", world_id):
                updated = _set_attr_if_supported(
                    universe,
                    "default_world_id",
                    world_id,
                    overwrite=True,
                )
            else:
                updated = True

        if not _safe_str(getattr(universe, "spawn_world_id", ""), ""):
            if not _call_if_available(universe, "set_spawn_world_id", world_id):
                updated = _set_attr_if_supported(
                    universe,
                    "spawn_world_id",
                    world_id,
                    overwrite=True,
                ) or updated
            else:
                updated = True

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
                },
            )
        )

    universe_db_id = _safe_model_id(universe)
    if universe_db_id is None:
        raise RuntimeError("Universe has no database id after flush.")

    # World
    started_at = _utc_now_iso()
    world = _query_one_by(
        WorldInstance,
        universe_db_id=universe_db_id,
        world_id=world_id,
    )

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
        # Keep existing world aligned with config-driven runtime constants.
        apply_world_defaults_to_object(world, world_defaults)

        operations.append(
            _make_operation(
                name="world",
                ok=True,
                status=OP_STATUS_SKIPPED,
                skipped=True,
                message="World already exists.",
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
        else getattr(resolved_seed_settings, "seed_defaults", False)
    )
    resolved_seed_blocks = bool(
        seed_debug_blocks_enabled
        if seed_debug_blocks_enabled is not None
        else getattr(resolved_seed_settings, "seed_debug_blocks", False)
    )
    resolved_seed_project = bool(
        seed_dev_project_enabled
        if seed_dev_project_enabled is not None
        else getattr(resolved_seed_settings, "seed_dev_project", False)
    )
    resolved_seed_on_empty_only = bool(
        seed_on_empty_only
        if seed_on_empty_only is not None
        else getattr(resolved_seed_settings, "seed_on_empty_only", True)
    )
    resolved_fail_on_error = bool(
        fail_on_error
        if fail_on_error is not None
        else getattr(resolved_seed_settings, "fail_on_error", True)
    )
    advisory_lock_enabled = bool(getattr(resolved_seed_settings, "advisory_lock_enabled", True))

    result.enabled = resolved_enabled
    result.seed_defaults_requested = resolved_enabled
    result.seed_debug_blocks_requested = resolved_seed_blocks
    result.seed_dev_project_requested = resolved_seed_project
    result.seed_on_empty_only = resolved_seed_on_empty_only

    result.project_id = _safe_str(getattr(resolved_world_defaults, "project_id", DEFAULT_PROJECT_ID), DEFAULT_PROJECT_ID)
    result.universe_id = _safe_str(getattr(resolved_world_defaults, "universe_id", DEFAULT_UNIVERSE_ID), DEFAULT_UNIVERSE_ID)
    result.world_id = _safe_str(getattr(resolved_world_defaults, "world_id", DEFAULT_WORLD_ID), DEFAULT_WORLD_ID)
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

    if not resolved_seed_blocks and not resolved_seed_project:
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

    def run_seed_body() -> None:
        if resolved_seed_on_empty_only and is_default_seed_complete(
            models,
            resolved_world_defaults,
            resolved_block_defaults,
            require_blocks=resolved_seed_blocks,
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

    This does not create or update anything.
    """
    started_at = _utc_now_iso()

    try:
        models = load_seed_model_classes()
        world_defaults = resolve_world_defaults(app, None)
        block_defaults = resolve_block_defaults(app, None)

        registry = find_default_block_registry(models, block_defaults)
        project = find_default_project(models, world_defaults)
        universe = find_default_universe(models, project, world_defaults) if project is not None else None
        world = find_default_world(models, universe, world_defaults) if universe is not None else None

        debug_blocks_ok = default_debug_blocks_exist(models, registry, block_defaults)
        complete = bool(registry and debug_blocks_ok and project and universe and world)

        completed_at = _utc_now_iso()

        return {
            "ok": complete,
            "status": STATUS_COMPLETED if complete else STATUS_PARTIAL,
            "startedAt": started_at,
            "completedAt": completed_at,
            "durationMs": _duration_ms(started_at, completed_at),
            "project": {
                "exists": project is not None,
                "projectId": _safe_str(getattr(world_defaults, "project_id", DEFAULT_PROJECT_ID), DEFAULT_PROJECT_ID),
                "dbId": _safe_model_id(project),
            },
            "universe": {
                "exists": universe is not None,
                "universeId": _safe_str(getattr(world_defaults, "universe_id", DEFAULT_UNIVERSE_ID), DEFAULT_UNIVERSE_ID),
                "dbId": _safe_model_id(universe),
            },
            "world": {
                "exists": world is not None,
                "worldId": _safe_str(getattr(world_defaults, "world_id", DEFAULT_WORLD_ID), DEFAULT_WORLD_ID),
                "dbId": _safe_model_id(world),
            },
            "blockRegistry": {
                "exists": registry is not None,
                "registryId": _safe_str(getattr(block_defaults, "registry_id", DEFAULT_BLOCK_REGISTRY_ID), DEFAULT_BLOCK_REGISTRY_ID),
                "registryVersion": _safe_str(
                    getattr(block_defaults, "registry_version", DEFAULT_BLOCK_REGISTRY_VERSION),
                    DEFAULT_BLOCK_REGISTRY_VERSION,
                ),
                "dbId": _safe_model_id(registry),
            },
            "debugBlocks": {
                "complete": debug_blocks_ok,
            },
        }

    except Exception as exc:
        completed_at = _utc_now_iso()

        return {
            "ok": False,
            "status": STATUS_FAILED,
            "startedAt": started_at,
            "completedAt": completed_at,
            "durationMs": _duration_ms(started_at, completed_at),
            "error": _safe_exception_message(exc),
            "exceptionType": exc.__class__.__name__,
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

    return {}


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
        "seedDevProjectRequested": bool(data.get("seed_dev_project_requested")),
        "seedOnEmptyOnly": bool(data.get("seed_on_empty_only")),
        "seedSkippedBecauseComplete": bool(data.get("seed_skipped_because_complete")),
        "lockUsed": bool(data.get("lock_used")),
        "projectId": data.get("project_id"),
        "universeId": data.get("universe_id"),
        "worldId": data.get("world_id"),
        "blockRegistryId": data.get("block_registry_id"),
        "blockRegistryVersion": data.get("block_registry_version"),
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
    "STATUS_SKIPPED",
    "DefaultSeedMessage",
    "DefaultSeedOperation",
    "DefaultSeedResult",
    "apply_world_defaults_to_object",
    "build_default_seed_status",
    "build_default_seed_summary",
    "create_block_registry_object",
    "create_debug_block_object",
    "create_project_object",
    "create_universe_object",
    "create_world_object",
    "default_debug_blocks_exist",
    "default_seed_result_to_dict",
    "find_default_block_registry",
    "find_default_project",
    "find_default_universe",
    "find_default_world",
    "is_default_seed_complete",
    "load_seed_model_classes",
    "resolve_block_defaults",
    "resolve_seed_settings",
    "resolve_world_defaults",
    "run_default_seed",
    "run_default_seed_if_enabled",
    "seed_debug_blocks",
    "seed_dev_project_universe_world",
]