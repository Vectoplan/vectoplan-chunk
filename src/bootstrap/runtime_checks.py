# services/vectoplan-chunk/src/bootstrap/runtime_checks.py
"""
Read-only runtime checks for the `vectoplan-chunk` service.

This module contains cheap startup/runtime checks only.

Responsibilities:
- verify important directories
- verify important files
- verify registered Flask routes
- collect compact app metadata
- verify model registry availability without querying world/chunk data
- optionally perform a cheap DB connectivity check
- return serializable check results for startup state and status routes

Important boundaries:
- no db.create_all()
- no schema migration
- no default seeding
- no chunk generation
- no snapshot loading
- no command execution
- no ORM relationship traversal
- no deep serialization of Project/Universe/World/Chunk/Event/Object graphs

Design rule:

    Runtime checks must be cheap, bounded and read-only.

The checks in this module are safe to run from Gunicorn workers during normal
runtime startup, because they do not mutate database state.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Final, Mapping, Sequence

from flask import Flask

try:
    from sqlalchemy import text
except Exception:  # pragma: no cover - fallback for partial environments
    text = None  # type: ignore[assignment]

try:
    from extensions import db, get_database_status
except Exception:  # pragma: no cover - fallback for isolated import tests
    db = None  # type: ignore[assignment]
    get_database_status = None  # type: ignore[assignment]

try:
    from .settings import (
        BootstrapSettings,
        build_bootstrap_settings,
        get_bool_setting,
        get_str_setting,
        should_check_database_on_startup,
        should_require_database_on_startup,
    )
except Exception:  # pragma: no cover - fallback for direct script-style imports
    BootstrapSettings = Any  # type: ignore[misc, assignment]

    def build_bootstrap_settings(app: Any = None) -> Any:  # type: ignore[override]
        return None

    def get_bool_setting(
        app: Any,
        key: str,
        default: bool = False,
        aliases: Sequence[str] | None = None,
        prefer_env: bool = True,
    ) -> bool:
        value = None
        try:
            value = getattr(app, "config", {}).get(key, None)
        except Exception:
            value = None
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "yes", "on"}

    def get_str_setting(
        app: Any,
        key: str,
        default: str = "",
        aliases: Sequence[str] | None = None,
        prefer_env: bool = True,
    ) -> str:
        try:
            value = getattr(app, "config", {}).get(key, default)
        except Exception:
            value = default
        try:
            return str(value).strip() or default
        except Exception:
            return default

    def should_check_database_on_startup(app: Any = None) -> bool:
        return True

    def should_require_database_on_startup(app: Any = None) -> bool:
        return True


# -----------------------------------------------------------------------------
# Constants
# -----------------------------------------------------------------------------

DEFAULT_SERVICE_NAME: Final[str] = "vectoplan-chunk"
DEFAULT_DISPLAY_NAME: Final[str] = "VECTOPLAN Chunk Service"

CHECK_STATUS_OK: Final[str] = "ok"
CHECK_STATUS_MISSING: Final[str] = "missing"
CHECK_STATUS_INVALID_TYPE: Final[str] = "invalid-type"
CHECK_STATUS_SKIPPED: Final[str] = "skipped"
CHECK_STATUS_FAILED: Final[str] = "failed"
CHECK_STATUS_WARNING: Final[str] = "warning"

MODEL_CHECK_IMPORT_ERROR_CODE: Final[str] = "model_import_failed"
MODEL_CHECK_REGISTRY_ERROR_CODE: Final[str] = "model_registry_not_ready"
DATABASE_CHECK_UNAVAILABLE_CODE: Final[str] = "database_unavailable"
DATABASE_CHECK_FAILED_CODE: Final[str] = "database_check_failed"


# -----------------------------------------------------------------------------
# Dataclasses
# -----------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class PathCheckSpec:
    """Specification for one startup path check."""

    name: str
    config_key: str
    fallback_relative_path: str
    required: bool
    description: str


@dataclass(frozen=True, slots=True)
class FileCheckSpec:
    """Specification for one startup file check."""

    name: str
    fallback_relative_path: str
    required: bool
    description: str


@dataclass(frozen=True, slots=True)
class RouteCheckSpec:
    """Specification for one registered route check."""

    name: str
    rule: str
    required: bool
    description: str


@dataclass(frozen=True, slots=True)
class CheckMessage:
    """Small serializable warning/error message."""

    code: str
    message: str
    timestamp: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RuntimeCheckItem:
    """Generic serializable check item."""

    name: str
    status: str
    ok: bool
    required: bool
    description: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RuntimeChecksResult:
    """Aggregate result for all read-only runtime checks."""

    ok: bool
    status: str
    started_at: str
    completed_at: str
    duration_ms: int

    metadata: dict[str, Any]
    paths: list[dict[str, Any]]
    files: list[dict[str, Any]]
    routes: list[dict[str, Any]]
    route_summary: dict[str, Any]
    models: dict[str, Any]
    database: dict[str, Any]

    warnings: list[dict[str, Any]]
    errors: list[dict[str, Any]]


# -----------------------------------------------------------------------------
# Primitive safe helpers
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


def _duration_ms(started_at: datetime, completed_at: datetime) -> int:
    """Return duration in milliseconds."""
    try:
        return max(0, int((completed_at - started_at).total_seconds() * 1000))
    except Exception:
        return 0


def _safe_str(value: Any, default: str = "") -> str:
    """Convert a value to a stripped string."""
    if value is None:
        return default

    try:
        result = str(value).strip()
    except Exception:
        return default

    return result or default


def _safe_bool(value: Any, default: bool = False) -> bool:
    """Convert a value to bool robustly."""
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


def _safe_int(value: Any, default: int = 0, minimum: int | None = None) -> int:
    """Convert a value to int robustly."""
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


def _safe_exception_message(exc: BaseException | Any) -> str:
    """Return robust exception message."""
    try:
        message = str(exc)
    except Exception:
        message = exc.__class__.__name__

    return message or exc.__class__.__name__


def _safe_dict(value: Any) -> dict[str, Any]:
    """Return value as dict if possible."""
    if isinstance(value, dict):
        return value

    if isinstance(value, Mapping):
        try:
            return dict(value)
        except Exception:
            return {}

    return {}


def _safe_path_to_string(path: Path | None) -> str:
    """Convert Path to string robustly."""
    if path is None:
        return ""

    try:
        return str(path)
    except Exception:
        return ""


def _safe_path_exists(path: Path) -> bool:
    """Return whether path exists robustly."""
    try:
        return path.exists()
    except Exception:
        return False


def _safe_is_dir(path: Path) -> bool:
    """Return whether path is directory robustly."""
    try:
        return path.is_dir()
    except Exception:
        return False


def _safe_is_file(path: Path) -> bool:
    """Return whether path is file robustly."""
    try:
        return path.is_file()
    except Exception:
        return False


def _is_flask_app(app: object) -> bool:
    """Return whether object can be treated like a Flask app."""
    if isinstance(app, Flask):
        return True

    required_attrs = ("extensions", "config", "logger", "url_map")
    try:
        return all(hasattr(app, attr_name) for attr_name in required_attrs)
    except Exception:
        return False


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


def _make_message(
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create serializable warning/error message dict."""
    item = CheckMessage(
        code=_safe_str(code, "runtime_check_message"),
        message=_safe_str(message, ""),
        timestamp=_utc_now_iso(),
        details=details or {},
    )
    return asdict(item)


def _make_check_item(
    name: str,
    status: str,
    ok: bool,
    required: bool,
    description: str,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a serializable check item."""
    item = RuntimeCheckItem(
        name=_safe_str(name, "unknown"),
        status=_safe_str(status, CHECK_STATUS_FAILED),
        ok=bool(ok),
        required=bool(required),
        description=_safe_str(description, ""),
        details=details or {},
    )
    return asdict(item)


# -----------------------------------------------------------------------------
# Service root/path resolution
# -----------------------------------------------------------------------------

def resolve_service_root_from_file() -> Path:
    """
    Resolve service root relative to this file.

    Expected:
        services/vectoplan-chunk/src/bootstrap/runtime_checks.py

    parents[0] -> bootstrap
    parents[1] -> src
    parents[2] -> vectoplan-chunk
    """
    try:
        return Path(__file__).resolve().parents[2]
    except Exception:
        try:
            return Path(".").resolve()
        except Exception:
            return Path(".")


def resolve_configured_path(
    app: Any,
    config_key: str,
    fallback_relative_path: str,
) -> Path:
    """Resolve path from app config/env with service-root fallback."""
    configured_value = None

    try:
        configured_value = get_str_setting(app, config_key, "", prefer_env=True)
    except Exception:
        configured_value = ""

    if configured_value:
        try:
            configured_path = Path(configured_value)
            if configured_path.is_absolute():
                return configured_path
            return resolve_service_root(app).joinpath(configured_path)
        except Exception:
            pass

    try:
        return resolve_service_root(app).joinpath(fallback_relative_path)
    except Exception:
        return resolve_service_root_from_file().joinpath(fallback_relative_path)


def resolve_service_root(app: Any) -> Path:
    """Resolve service root robustly."""
    candidate = ""

    for key in (
        "SERVICE_ROOT",
        "VECTOPLAN_CHUNK_SERVICE_ROOT",
        "APP_HOME",
        "VECTOPLAN_CHUNK_APP_HOME",
    ):
        try:
            candidate = get_str_setting(app, key, "", prefer_env=True)
        except Exception:
            candidate = ""

        if candidate:
            break

    if candidate:
        try:
            return Path(candidate).resolve()
        except Exception:
            try:
                return Path(candidate)
            except Exception:
                pass

    return resolve_service_root_from_file()


# -----------------------------------------------------------------------------
# Default check specs
# -----------------------------------------------------------------------------

@lru_cache(maxsize=1)
def get_default_path_check_specs() -> tuple[PathCheckSpec, ...]:
    """Return default directory checks."""
    return (
        PathCheckSpec(
            name="service_root",
            config_key="SERVICE_ROOT",
            fallback_relative_path=".",
            required=True,
            description="Service root directory.",
        ),
        PathCheckSpec(
            name="routes_root",
            config_key="ROUTES_ROOT",
            fallback_relative_path="routes",
            required=True,
            description="HTTP route directory.",
        ),
        PathCheckSpec(
            name="src_root",
            config_key="SRC_ROOT",
            fallback_relative_path="src",
            required=True,
            description="Source logic directory.",
        ),
        PathCheckSpec(
            name="models_root",
            config_key="MODELS_ROOT",
            fallback_relative_path="models",
            required=True,
            description="SQLAlchemy model directory.",
        ),
        PathCheckSpec(
            name="world_src_root",
            config_key="WORLD_SRC_ROOT",
            fallback_relative_path="src/world",
            required=True,
            description="Provider/template world source directory.",
        ),
        PathCheckSpec(
            name="world_state_src_root",
            config_key="WORLD_STATE_SRC_ROOT",
            fallback_relative_path="src/world_state",
            required=True,
            description="Project/universe/world-state source directory.",
        ),
        PathCheckSpec(
            name="flat_world_root",
            config_key="FLAT_WORLD_ROOT",
            fallback_relative_path="src/world/flat",
            required=True,
            description="Flat provider world directory.",
        ),
        PathCheckSpec(
            name="bootstrap_src_root",
            config_key="BOOTSTRAP_SRC_ROOT",
            fallback_relative_path="src/bootstrap",
            required=True,
            description="Startup/bootstrap source directory.",
        ),
        PathCheckSpec(
            name="migrations_root",
            config_key="MIGRATIONS_ROOT",
            fallback_relative_path="migrations",
            required=False,
            description="Optional Alembic migration directory.",
        ),
        PathCheckSpec(
            name="templates_root",
            config_key="TEMPLATES_ROOT",
            fallback_relative_path="templates",
            required=False,
            description="Optional legacy template directory.",
        ),
        PathCheckSpec(
            name="static_root",
            config_key="STATIC_ROOT",
            fallback_relative_path="static",
            required=False,
            description="Optional legacy static asset directory.",
        ),
        PathCheckSpec(
            name="tests_root",
            config_key="TESTS_ROOT",
            fallback_relative_path="tests",
            required=False,
            description="Optional test directory.",
        ),
        PathCheckSpec(
            name="scripts_root",
            config_key="SCRIPTS_ROOT",
            fallback_relative_path="scripts",
            required=False,
            description="Optional service scripts directory.",
        ),
    )


@lru_cache(maxsize=1)
def get_default_file_check_specs() -> tuple[FileCheckSpec, ...]:
    """Return default file checks."""
    return (
        FileCheckSpec(
            name="app_factory",
            fallback_relative_path="app.py",
            required=True,
            description="Flask app factory.",
        ),
        FileCheckSpec(
            name="wsgi_entrypoint",
            fallback_relative_path="wsgi.py",
            required=True,
            description="WSGI entrypoint.",
        ),
        FileCheckSpec(
            name="service_config",
            fallback_relative_path="config.py",
            required=True,
            description="Central service configuration.",
        ),
        FileCheckSpec(
            name="extensions",
            fallback_relative_path="extensions.py",
            required=True,
            description="Shared Flask extension setup.",
        ),
        FileCheckSpec(
            name="requirements",
            fallback_relative_path="requirements.txt",
            required=True,
            description="Python dependency specification.",
        ),
        FileCheckSpec(
            name="models_init",
            fallback_relative_path="models/__init__.py",
            required=True,
            description="Model registration package.",
        ),
        FileCheckSpec(
            name="project_model",
            fallback_relative_path="models/project.py",
            required=True,
            description="Project model.",
        ),
        FileCheckSpec(
            name="universe_model",
            fallback_relative_path="models/universe.py",
            required=True,
            description="Universe model.",
        ),
        FileCheckSpec(
            name="world_model",
            fallback_relative_path="models/world.py",
            required=True,
            description="WorldInstance model.",
        ),
        FileCheckSpec(
            name="block_model",
            fallback_relative_path="models/block.py",
            required=True,
            description="BlockRegistry and BlockType models.",
        ),
        FileCheckSpec(
            name="chunk_model",
            fallback_relative_path="models/chunk.py",
            required=True,
            description="ChunkSnapshot model.",
        ),
        FileCheckSpec(
            name="event_model",
            fallback_relative_path="models/event.py",
            required=True,
            description="WorldCommandLog and ChunkEvent models.",
        ),
        FileCheckSpec(
            name="object_model",
            fallback_relative_path="models/object.py",
            required=True,
            description="WorldObjectInstance and WorldObjectChunkRef models.",
        ),
        FileCheckSpec(
            name="settings_module",
            fallback_relative_path="src/bootstrap/settings.py",
            required=True,
            description="Central startup/bootstrap settings module.",
        ),
        FileCheckSpec(
            name="runtime_checks_module",
            fallback_relative_path="src/bootstrap/runtime_checks.py",
            required=True,
            description="Read-only runtime checks module.",
        ),
        FileCheckSpec(
            name="flat_world_config",
            fallback_relative_path="src/world/flat/world.json",
            required=True,
            description="Flat provider world configuration.",
        ),
        FileCheckSpec(
            name="routes_projects",
            fallback_relative_path="routes/projects.py",
            required=True,
            description="Project/bootstrap routes.",
        ),
        FileCheckSpec(
            name="routes_worlds",
            fallback_relative_path="routes/worlds.py",
            required=True,
            description="World routes.",
        ),
        FileCheckSpec(
            name="routes_blocks",
            fallback_relative_path="routes/blocks.py",
            required=True,
            description="Block routes.",
        ),
        FileCheckSpec(
            name="routes_chunks",
            fallback_relative_path="routes/chunks.py",
            required=True,
            description="Chunk load routes.",
        ),
        FileCheckSpec(
            name="routes_commands",
            fallback_relative_path="routes/commands.py",
            required=True,
            description="Command routes.",
        ),
        FileCheckSpec(
            name="routes_editor_legacy",
            fallback_relative_path="routes/editor.py",
            required=False,
            description="Optional legacy editor route module.",
        ),
    )


@lru_cache(maxsize=1)
def get_default_route_check_specs() -> tuple[RouteCheckSpec, ...]:
    """Return default route checks."""
    return (
        RouteCheckSpec(
            name="root",
            rule="/",
            required=False,
            description="Root probe route.",
        ),
        RouteCheckSpec(
            name="projects_status",
            rule="/projects/_status",
            required=True,
            description="Project service status route.",
        ),
        RouteCheckSpec(
            name="worlds_status",
            rule="/worlds/_status",
            required=True,
            description="World service status route.",
        ),
        RouteCheckSpec(
            name="blocks_status",
            rule="/blocks/_status",
            required=True,
            description="Block service status route.",
        ),
        RouteCheckSpec(
            name="chunks_status",
            rule="/chunks/_status",
            required=True,
            description="Chunk service status route.",
        ),
        RouteCheckSpec(
            name="commands_status",
            rule="/commands/_status",
            required=True,
            description="Command service status route.",
        ),
        RouteCheckSpec(
            name="project_bootstrap",
            rule="/projects/<project_id>/bootstrap",
            required=True,
            description="Project bootstrap route.",
        ),
        RouteCheckSpec(
            name="project_worlds",
            rule="/projects/<project_id>/worlds",
            required=True,
            description="Project-scoped world list route.",
        ),
        RouteCheckSpec(
            name="project_world_metadata",
            rule="/projects/<project_id>/worlds/<world_id>",
            required=True,
            description="Project-scoped world metadata route.",
        ),
        RouteCheckSpec(
            name="project_world_blocks",
            rule="/projects/<project_id>/worlds/<world_id>/blocks",
            required=True,
            description="Project-scoped block route.",
        ),
        RouteCheckSpec(
            name="project_world_chunk",
            rule="/projects/<project_id>/worlds/<world_id>/chunks",
            required=True,
            description="Project-scoped single chunk route.",
        ),
        RouteCheckSpec(
            name="project_world_chunk_batch",
            rule="/projects/<project_id>/worlds/<world_id>/chunks/batch",
            required=True,
            description="Project-scoped chunk batch route.",
        ),
        RouteCheckSpec(
            name="project_world_commands",
            rule="/projects/<project_id>/worlds/<world_id>/commands",
            required=True,
            description="Project-scoped command route.",
        ),
        RouteCheckSpec(
            name="world_test",
            rule="/world-test",
            required=False,
            description="Debug world-test UI route.",
        ),
        RouteCheckSpec(
            name="world_test_health",
            rule="/world-test/api/health",
            required=False,
            description="Debug world-test health route.",
        ),
    )


def get_default_path_check_spec_data() -> list[dict[str, Any]]:
    """Return default path specs as dicts."""
    return [asdict(spec) for spec in get_default_path_check_specs()]


def get_default_file_check_spec_data() -> list[dict[str, Any]]:
    """Return default file specs as dicts."""
    return [asdict(spec) for spec in get_default_file_check_specs()]


def get_default_route_check_spec_data() -> list[dict[str, Any]]:
    """Return default route specs as dicts."""
    return [asdict(spec) for spec in get_default_route_check_specs()]


# -----------------------------------------------------------------------------
# Metadata collection
# -----------------------------------------------------------------------------

def collect_app_metadata(app: Flask, settings: BootstrapSettings | None = None) -> dict[str, Any]:
    """
    Collect compact app metadata.

    This function must not query product data.
    """
    metadata: dict[str, Any] = {}

    try:
        if settings is None:
            settings = build_bootstrap_settings(app)
    except Exception:
        settings = None

    try:
        template_folder = app.template_folder
    except Exception:
        template_folder = None

    try:
        static_folder = app.static_folder
    except Exception:
        static_folder = None

    try:
        static_url_path = app.static_url_path
    except Exception:
        static_url_path = None

    try:
        instance_path = app.instance_path
    except Exception:
        instance_path = None

    try:
        app_name = app.name
    except Exception:
        app_name = ""

    metadata.update(
        {
            "serviceName": DEFAULT_SERVICE_NAME,
            "displayName": DEFAULT_DISPLAY_NAME,
            "flaskAppName": _safe_str(app_name, ""),
            "templateFolder": _safe_str(template_folder, ""),
            "staticFolder": _safe_str(static_folder, ""),
            "staticUrlPath": _safe_str(static_url_path, ""),
            "instancePath": _safe_str(instance_path, ""),
            "serviceRoot": _safe_path_to_string(resolve_service_root(app)),
            "collectedAt": _utc_now_iso(),
        }
    )

    if settings is not None:
        try:
            metadata.update(
                {
                    "serviceName": settings.identity.service_name,
                    "displayName": settings.identity.display_name,
                    "configName": settings.identity.config_name,
                    "mode": settings.identity.mode,
                    "isRuntimeMode": settings.identity.is_runtime_mode,
                    "isDbBootstrapMode": settings.identity.is_db_bootstrap_mode,
                    "runStartupHooks": settings.runtime.run_startup_hooks,
                    "startupStrict": settings.runtime.startup_strict,
                    "checkDatabase": settings.runtime.check_database,
                    "requireDatabase": settings.runtime.require_database,
                    "allowRuntimeDbMutations": settings.runtime.allow_runtime_db_mutations,
                    "autoCreateAllInRuntime": settings.runtime.auto_create_all_in_runtime,
                    "autoSeedDefaultsInRuntime": settings.runtime.auto_seed_defaults_in_runtime,
                    "projectId": settings.world_defaults.project_id,
                    "universeId": settings.world_defaults.universe_id,
                    "worldId": settings.world_defaults.world_id,
                    "templateId": settings.world_defaults.template_id,
                    "providerWorldId": settings.world_defaults.provider_world_id,
                    "blockRegistryId": settings.world_defaults.block_registry_id,
                    "blockRegistryVersion": settings.world_defaults.block_registry_version,
                }
            )
        except Exception:
            pass
    else:
        metadata.update(
            {
                "serviceName": get_str_setting(app, "SERVICE_NAME", DEFAULT_SERVICE_NAME),
                "displayName": get_str_setting(app, "APP_DISPLAY_NAME", DEFAULT_DISPLAY_NAME),
                "configName": get_str_setting(app, "VECTOPLAN_CHUNK_CONFIG", "development"),
            }
        )

    return metadata


# -----------------------------------------------------------------------------
# Path/file checks
# -----------------------------------------------------------------------------

def run_path_checks(
    app: Flask,
    strict: bool = False,
    specs: Sequence[PathCheckSpec] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """
    Run configured path checks.

    Returns:
        (results, warnings, errors)
    """
    results: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    for spec in specs or get_default_path_check_specs():
        resolved_path = resolve_configured_path(
            app,
            spec.config_key,
            spec.fallback_relative_path,
        )

        exists = _safe_path_exists(resolved_path)
        is_dir = _safe_is_dir(resolved_path)
        ok = exists and is_dir

        if ok:
            status = CHECK_STATUS_OK
        elif not exists:
            status = CHECK_STATUS_MISSING
        else:
            status = CHECK_STATUS_INVALID_TYPE

        result = _make_check_item(
            name=spec.name,
            status=status,
            ok=ok or not spec.required,
            required=spec.required,
            description=spec.description,
            details={
                "configKey": spec.config_key,
                "fallbackRelativePath": spec.fallback_relative_path,
                "path": _safe_path_to_string(resolved_path),
                "exists": exists,
                "isDir": is_dir,
            },
        )
        results.append(result)

        if spec.required and not ok:
            message = (
                f"Required startup directory not available: "
                f"{spec.name} ({_safe_path_to_string(resolved_path)})"
            )
            error = _make_message(
                code="required_path_missing",
                message=message,
                details={
                    "name": spec.name,
                    "path": _safe_path_to_string(resolved_path),
                    "status": status,
                },
            )
            errors.append(error)
            if strict:
                break

        elif not spec.required and not ok:
            warnings.append(
                _make_message(
                    code="optional_path_missing",
                    message=(
                        f"Optional startup directory not fully available: "
                        f"{spec.name} ({_safe_path_to_string(resolved_path)})"
                    ),
                    details={
                        "name": spec.name,
                        "path": _safe_path_to_string(resolved_path),
                        "status": status,
                    },
                )
            )

    return results, warnings, errors


def run_file_checks(
    app: Flask,
    strict: bool = False,
    specs: Sequence[FileCheckSpec] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """
    Run configured file checks.

    Returns:
        (results, warnings, errors)
    """
    results: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    service_root = resolve_service_root(app)

    for spec in specs or get_default_file_check_specs():
        try:
            file_path = service_root.joinpath(spec.fallback_relative_path)
        except Exception:
            file_path = resolve_service_root_from_file().joinpath(spec.fallback_relative_path)

        exists = _safe_path_exists(file_path)
        is_file = _safe_is_file(file_path)
        ok = exists and is_file

        if ok:
            status = CHECK_STATUS_OK
        elif not exists:
            status = CHECK_STATUS_MISSING
        else:
            status = CHECK_STATUS_INVALID_TYPE

        result = _make_check_item(
            name=spec.name,
            status=status,
            ok=ok or not spec.required,
            required=spec.required,
            description=spec.description,
            details={
                "fallbackRelativePath": spec.fallback_relative_path,
                "path": _safe_path_to_string(file_path),
                "exists": exists,
                "isFile": is_file,
            },
        )
        results.append(result)

        if spec.required and not ok:
            message = (
                f"Required startup file not available: "
                f"{spec.name} ({_safe_path_to_string(file_path)})"
            )
            error = _make_message(
                code="required_file_missing",
                message=message,
                details={
                    "name": spec.name,
                    "path": _safe_path_to_string(file_path),
                    "status": status,
                },
            )
            errors.append(error)
            if strict:
                break

        elif not spec.required and not ok:
            warnings.append(
                _make_message(
                    code="optional_file_missing",
                    message=(
                        f"Optional startup file not fully available: "
                        f"{spec.name} ({_safe_path_to_string(file_path)})"
                    ),
                    details={
                        "name": spec.name,
                        "path": _safe_path_to_string(file_path),
                        "status": status,
                    },
                )
            )

    return results, warnings, errors


# -----------------------------------------------------------------------------
# Route checks
# -----------------------------------------------------------------------------

def collect_route_rules(app: Flask) -> list[str]:
    """Collect all Flask route rules."""
    try:
        return sorted(str(rule.rule) for rule in app.url_map.iter_rules())
    except Exception:
        return []


def run_route_checks(
    app: Flask,
    strict: bool = False,
    specs: Sequence[RouteCheckSpec] | None = None,
) -> tuple[
    list[dict[str, Any]],
    dict[str, Any],
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    """
    Run route checks.

    Returns:
        (results, route_summary, warnings, errors)
    """
    route_rules = collect_route_rules(app)
    route_rule_set = set(route_rules)

    results: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    required_missing: list[str] = []
    optional_missing: list[str] = []

    for spec in specs or get_default_route_check_specs():
        exists = spec.rule in route_rule_set
        ok = exists or not spec.required
        status = CHECK_STATUS_OK if exists else CHECK_STATUS_MISSING

        result = _make_check_item(
            name=spec.name,
            status=status,
            ok=ok,
            required=spec.required,
            description=spec.description,
            details={
                "rule": spec.rule,
                "exists": exists,
            },
        )
        results.append(result)

        if spec.required and not exists:
            required_missing.append(spec.rule)
            error = _make_message(
                code="required_route_missing",
                message=f"Required route is missing: {spec.rule}",
                details={
                    "name": spec.name,
                    "rule": spec.rule,
                },
            )
            errors.append(error)
            if strict:
                break

        elif not spec.required and not exists:
            optional_missing.append(spec.rule)
            warnings.append(
                _make_message(
                    code="optional_route_missing",
                    message=f"Optional route is missing: {spec.rule}",
                    details={
                        "name": spec.name,
                        "rule": spec.rule,
                    },
                )
            )

    route_summary = {
        "count": len(route_rules),
        "requiredMissing": required_missing,
        "optionalMissing": optional_missing,
        "rules": route_rules,
    }

    return results, route_summary, warnings, errors


# -----------------------------------------------------------------------------
# Model checks
# -----------------------------------------------------------------------------

def run_model_registry_check(
    app: Flask,
    strict: bool = False,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    """
    Check model registry readiness.

    This must not query application data.
    """
    warnings: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    try:
        from models import get_model_debug_summary, require_models_ready
    except Exception as exc:
        message = f"Could not import model registry helpers: {_safe_exception_message(exc)}"
        error = _make_message(
            code=MODEL_CHECK_IMPORT_ERROR_CODE,
            message=message,
            details={
                "exceptionType": exc.__class__.__name__,
            },
        )
        errors.append(error)

        result = {
            "ok": False,
            "status": CHECK_STATUS_FAILED,
            "errorCode": MODEL_CHECK_IMPORT_ERROR_CODE,
            "error": message,
            "summary": {},
        }

        return result, warnings, errors

    try:
        require_models_ready()
    except Exception as exc:
        message = f"Model registry is not ready: {_safe_exception_message(exc)}"
        error = _make_message(
            code=MODEL_CHECK_REGISTRY_ERROR_CODE,
            message=message,
            details={
                "exceptionType": exc.__class__.__name__,
            },
        )
        errors.append(error)

        result = {
            "ok": False,
            "status": CHECK_STATUS_FAILED,
            "errorCode": MODEL_CHECK_REGISTRY_ERROR_CODE,
            "error": message,
            "summary": {},
        }

        return result, warnings, errors

    summary: dict[str, Any] = {}
    try:
        summary_value = get_model_debug_summary()
        summary = _safe_dict(summary_value)
    except Exception as exc:
        warnings.append(
            _make_message(
                code="model_summary_unavailable",
                message=f"Model registry is ready, but summary could not be built: {_safe_exception_message(exc)}",
                details={
                    "exceptionType": exc.__class__.__name__,
                },
            )
        )

    result = {
        "ok": True,
        "status": CHECK_STATUS_OK,
        "summary": summary,
    }

    return result, warnings, errors


# -----------------------------------------------------------------------------
# Database checks
# -----------------------------------------------------------------------------

def _run_extensions_database_status_check(
    app: Flask,
    check_connection: bool,
) -> dict[str, Any] | None:
    """Try extensions.get_database_status() if available."""
    if get_database_status is None:
        return None

    try:
        status = get_database_status(app, check_connection=check_connection)
    except Exception as exc:
        return {
            "available": False,
            "configured": False,
            "connectionChecked": check_connection,
            "connectionOk": False,
            "connectionError": _safe_exception_message(exc),
            "source": "extensions.get_database_status",
        }

    if isinstance(status, Mapping):
        result = dict(status)
    else:
        result = {
            "available": bool(status),
            "configured": bool(status),
            "connectionChecked": check_connection,
            "connectionOk": bool(status),
        }

    result.setdefault("source", "extensions.get_database_status")
    return result


def _run_direct_database_ping(app: Flask, check_connection: bool) -> dict[str, Any]:
    """
    Run a minimal direct DB ping.

    This is read-only and does not use ORM queries.
    """
    configured = False
    uri = ""

    try:
        uri = get_str_setting(
            app,
            "SQLALCHEMY_DATABASE_URI",
            "",
            aliases=(
                "VECTOPLAN_CHUNK_SQLALCHEMY_DATABASE_URI",
                "VECTOPLAN_CHUNK_DATABASE_URI",
                "DATABASE_URL",
            ),
        )
        configured = bool(uri)
    except Exception:
        configured = False

    result: dict[str, Any] = {
        "available": db is not None,
        "configured": configured,
        "connectionChecked": bool(check_connection),
        "connectionOk": None,
        "connectionError": None,
        "source": "runtime_checks.direct_ping",
    }

    if not check_connection:
        return result

    if db is None:
        result["connectionOk"] = False
        result["connectionError"] = "SQLAlchemy db extension is unavailable."
        return result

    if text is None:
        result["connectionOk"] = False
        result["connectionError"] = "sqlalchemy.text is unavailable."
        return result

    connection = None

    try:
        connection = db.engine.connect()
        connection.execute(text("SELECT 1"))
        result["connectionOk"] = True
    except Exception as exc:
        result["connectionOk"] = False
        result["connectionError"] = _safe_exception_message(exc)
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass

        try:
            db.session.remove()
        except Exception:
            pass

    return result


def run_database_check(
    app: Flask,
    check_connection: bool | None = None,
    require_ok: bool | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    """
    Run cheap DB availability check.

    This function does not create tables and does not query product data.
    """
    warnings: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    if check_connection is None:
        try:
            check_connection = should_check_database_on_startup(app)
        except Exception:
            check_connection = True

    if require_ok is None:
        try:
            require_ok = should_require_database_on_startup(app)
        except Exception:
            require_ok = True

    try:
        status = _run_extensions_database_status_check(
            app,
            check_connection=bool(check_connection),
        )
        if status is None:
            status = _run_direct_database_ping(
                app,
                check_connection=bool(check_connection),
            )
    except Exception as exc:
        status = {
            "available": False,
            "configured": False,
            "connectionChecked": bool(check_connection),
            "connectionOk": False,
            "connectionError": _safe_exception_message(exc),
            "source": "runtime_checks.exception",
        }

    connection_checked = bool(status.get("connectionChecked"))
    connection_ok = status.get("connectionOk")

    if connection_checked and connection_ok is False:
        message = _safe_str(
            status.get("connectionError"),
            "Database connection check failed.",
        )

        item = _make_message(
            code=DATABASE_CHECK_FAILED_CODE if status.get("configured") else DATABASE_CHECK_UNAVAILABLE_CODE,
            message=message,
            details={
                "source": status.get("source"),
                "requireOk": bool(require_ok),
            },
        )

        if require_ok:
            errors.append(item)
        else:
            warnings.append(item)

    return status, warnings, errors


# -----------------------------------------------------------------------------
# Aggregate runner
# -----------------------------------------------------------------------------

def run_runtime_checks(
    app: Flask,
    settings: BootstrapSettings | None = None,
    *,
    check_paths: bool | None = None,
    check_files: bool | None = None,
    check_routes: bool | None = None,
    check_models: bool | None = None,
    check_database: bool | None = None,
    strict: bool | None = None,
) -> RuntimeChecksResult:
    """
    Run all read-only runtime checks.

    This is safe to run during normal app startup.
    """
    started = _utc_now()
    started_iso = started.isoformat()

    if not _is_flask_app(app):
        completed = _utc_now()
        error = _make_message(
            code="invalid_flask_app",
            message="run_runtime_checks(app) expects a Flask app or compatible object.",
            details={},
        )
        return RuntimeChecksResult(
            ok=False,
            status=CHECK_STATUS_FAILED,
            started_at=started_iso,
            completed_at=completed.isoformat(),
            duration_ms=_duration_ms(started, completed),
            metadata={},
            paths=[],
            files=[],
            routes=[],
            route_summary={
                "count": 0,
                "requiredMissing": [],
                "optionalMissing": [],
                "rules": [],
            },
            models={
                "ok": False,
                "status": CHECK_STATUS_FAILED,
            },
            database={
                "available": False,
                "configured": False,
                "connectionChecked": False,
                "connectionOk": None,
            },
            warnings=[],
            errors=[error],
        )

    warnings: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    paths: list[dict[str, Any]] = []
    files: list[dict[str, Any]] = []
    routes: list[dict[str, Any]] = []
    route_summary: dict[str, Any] = {
        "count": 0,
        "requiredMissing": [],
        "optionalMissing": [],
        "rules": [],
    }
    models: dict[str, Any] = {
        "ok": None,
        "status": CHECK_STATUS_SKIPPED,
        "summary": {},
    }
    database: dict[str, Any] = {
        "available": None,
        "configured": None,
        "connectionChecked": False,
        "connectionOk": None,
        "status": CHECK_STATUS_SKIPPED,
    }

    try:
        if settings is None:
            settings = build_bootstrap_settings(app)
    except Exception as exc:
        settings = None
        warnings.append(
            _make_message(
                code="settings_unavailable",
                message=f"Could not build bootstrap settings: {_safe_exception_message(exc)}",
                details={
                    "exceptionType": exc.__class__.__name__,
                },
            )
        )

    if strict is None:
        if settings is not None:
            try:
                strict = bool(settings.runtime.startup_strict)
            except Exception:
                strict = False
        else:
            strict = get_bool_setting(
                app,
                "VECTOPLAN_CHUNK_STARTUP_STRICT",
                False,
                aliases=("CHUNK_STARTUP_STRICT",),
            )

    if check_paths is None:
        check_paths = bool(settings.runtime.check_paths) if settings is not None else True
    if check_files is None:
        check_files = bool(settings.runtime.check_files) if settings is not None else True
    if check_routes is None:
        check_routes = bool(settings.runtime.check_routes) if settings is not None else True
    if check_models is None:
        check_models = bool(settings.runtime.check_models) if settings is not None else True
    if check_database is None:
        check_database = bool(settings.runtime.check_database) if settings is not None else True

    metadata = collect_app_metadata(app, settings=settings)

    try:
        if check_paths:
            path_results, path_warnings, path_errors = run_path_checks(
                app,
                strict=bool(strict),
            )
            paths = path_results
            warnings.extend(path_warnings)
            errors.extend(path_errors)
        else:
            paths.append(
                _make_check_item(
                    name="path_checks",
                    status=CHECK_STATUS_SKIPPED,
                    ok=True,
                    required=False,
                    description="Path checks skipped by settings.",
                )
            )
    except Exception as exc:
        errors.append(
            _make_message(
                code="path_checks_failed",
                message=f"Path checks failed: {_safe_exception_message(exc)}",
                details={
                    "exceptionType": exc.__class__.__name__,
                },
            )
        )

    try:
        if check_files:
            file_results, file_warnings, file_errors = run_file_checks(
                app,
                strict=bool(strict),
            )
            files = file_results
            warnings.extend(file_warnings)
            errors.extend(file_errors)
        else:
            files.append(
                _make_check_item(
                    name="file_checks",
                    status=CHECK_STATUS_SKIPPED,
                    ok=True,
                    required=False,
                    description="File checks skipped by settings.",
                )
            )
    except Exception as exc:
        errors.append(
            _make_message(
                code="file_checks_failed",
                message=f"File checks failed: {_safe_exception_message(exc)}",
                details={
                    "exceptionType": exc.__class__.__name__,
                },
            )
        )

    try:
        if check_routes:
            route_results, summary, route_warnings, route_errors = run_route_checks(
                app,
                strict=bool(strict),
            )
            routes = route_results
            route_summary = summary
            warnings.extend(route_warnings)
            errors.extend(route_errors)
        else:
            routes.append(
                _make_check_item(
                    name="route_checks",
                    status=CHECK_STATUS_SKIPPED,
                    ok=True,
                    required=False,
                    description="Route checks skipped by settings.",
                )
            )
    except Exception as exc:
        errors.append(
            _make_message(
                code="route_checks_failed",
                message=f"Route checks failed: {_safe_exception_message(exc)}",
                details={
                    "exceptionType": exc.__class__.__name__,
                },
            )
        )

    try:
        if check_models:
            model_result, model_warnings, model_errors = run_model_registry_check(
                app,
                strict=bool(strict),
            )
            models = model_result
            warnings.extend(model_warnings)
            errors.extend(model_errors)
        else:
            models = {
                "ok": True,
                "status": CHECK_STATUS_SKIPPED,
                "summary": {},
                "message": "Model checks skipped by settings.",
            }
    except Exception as exc:
        errors.append(
            _make_message(
                code="model_checks_failed",
                message=f"Model checks failed: {_safe_exception_message(exc)}",
                details={
                    "exceptionType": exc.__class__.__name__,
                },
            )
        )

    try:
        if check_database:
            require_db = None
            if settings is not None:
                try:
                    require_db = bool(settings.runtime.require_database)
                except Exception:
                    require_db = None

            database_result, database_warnings, database_errors = run_database_check(
                app,
                check_connection=True,
                require_ok=require_db,
            )
            database = database_result
            database["status"] = (
                CHECK_STATUS_OK
                if database.get("connectionOk") is True
                else CHECK_STATUS_FAILED
                if database.get("connectionOk") is False
                else CHECK_STATUS_WARNING
            )
            warnings.extend(database_warnings)
            errors.extend(database_errors)
        else:
            database = {
                "available": None,
                "configured": None,
                "connectionChecked": False,
                "connectionOk": None,
                "status": CHECK_STATUS_SKIPPED,
                "message": "Database check skipped by settings.",
            }
    except Exception as exc:
        errors.append(
            _make_message(
                code="database_check_exception",
                message=f"Database check failed unexpectedly: {_safe_exception_message(exc)}",
                details={
                    "exceptionType": exc.__class__.__name__,
                },
            )
        )

    completed = _utc_now()
    ok = len(errors) == 0
    status = CHECK_STATUS_OK if ok else CHECK_STATUS_FAILED

    if warnings and ok:
        status = CHECK_STATUS_WARNING

    return RuntimeChecksResult(
        ok=ok,
        status=status,
        started_at=started_iso,
        completed_at=completed.isoformat(),
        duration_ms=_duration_ms(started, completed),
        metadata=metadata,
        paths=paths,
        files=files,
        routes=routes,
        route_summary=route_summary,
        models=models,
        database=database,
        warnings=warnings,
        errors=errors,
    )


# -----------------------------------------------------------------------------
# Serialization and summaries
# -----------------------------------------------------------------------------

def runtime_checks_result_to_dict(result: RuntimeChecksResult | Mapping[str, Any] | Any) -> dict[str, Any]:
    """Serialize runtime check result to dict."""
    if isinstance(result, RuntimeChecksResult):
        return asdict(result)

    if isinstance(result, Mapping):
        try:
            return dict(result)
        except Exception:
            return {}

    return {}


def build_runtime_checks_summary(result: RuntimeChecksResult | Mapping[str, Any] | Any) -> dict[str, Any]:
    """Build compact runtime-check summary."""
    data = runtime_checks_result_to_dict(result)

    paths = data.get("paths") or []
    files = data.get("files") or []
    routes = data.get("routes") or []
    warnings = data.get("warnings") or []
    errors = data.get("errors") or []

    def count_failed(items: Any) -> int:
        try:
            return sum(1 for item in items if not bool(item.get("ok")))
        except Exception:
            return 0

    return {
        "ok": bool(data.get("ok")),
        "status": _safe_str(data.get("status"), "unknown"),
        "startedAt": data.get("started_at"),
        "completedAt": data.get("completed_at"),
        "durationMs": _safe_int(data.get("duration_ms"), 0, minimum=0),
        "pathCount": len(paths),
        "fileCount": len(files),
        "routeCount": _safe_int(
            (data.get("route_summary") or {}).get("count", len(routes)),
            len(routes),
            minimum=0,
        ),
        "failedPathChecks": count_failed(paths),
        "failedFileChecks": count_failed(files),
        "failedRouteChecks": count_failed(routes),
        "modelsOk": bool((data.get("models") or {}).get("ok")),
        "databaseChecked": bool((data.get("database") or {}).get("connectionChecked")),
        "databaseOk": (data.get("database") or {}).get("connectionOk"),
        "warningCount": len(warnings),
        "errorCount": len(errors),
        "requiredMissingRoutes": list(
            (data.get("route_summary") or {}).get("requiredMissing", []) or []
        ),
        "optionalMissingRoutes": list(
            (data.get("route_summary") or {}).get("optionalMissing", []) or []
        ),
    }


def raise_if_runtime_checks_failed(result: RuntimeChecksResult) -> None:
    """Raise RuntimeError if runtime checks contain errors."""
    if result.ok:
        return

    summary = build_runtime_checks_summary(result)
    errors = result.errors or []

    if errors:
        first_error = errors[0]
        message = _safe_str(first_error.get("message"), "Runtime checks failed.")
    else:
        message = "Runtime checks failed."

    raise RuntimeError(
        f"{message} "
        f"(errorCount={summary.get('errorCount')}, "
        f"warningCount={summary.get('warningCount')})"
    )


def log_runtime_checks_result(app: Flask, result: RuntimeChecksResult) -> None:
    """Write a compact runtime-check result to app logs."""
    summary = build_runtime_checks_summary(result)

    if result.ok:
        if summary.get("warningCount"):
            _safe_log_warning(
                app,
                "Runtime checks completed with warnings: %s",
                summary,
            )
        else:
            _safe_log_info(
                app,
                "Runtime checks completed successfully: %s",
                summary,
            )
    else:
        _safe_log_warning(
            app,
            "Runtime checks failed: %s",
            summary,
        )


# -----------------------------------------------------------------------------
# Public exports
# -----------------------------------------------------------------------------

__all__ = [
    "CHECK_STATUS_FAILED",
    "CHECK_STATUS_INVALID_TYPE",
    "CHECK_STATUS_MISSING",
    "CHECK_STATUS_OK",
    "CHECK_STATUS_SKIPPED",
    "CHECK_STATUS_WARNING",
    "CheckMessage",
    "FileCheckSpec",
    "PathCheckSpec",
    "RouteCheckSpec",
    "RuntimeCheckItem",
    "RuntimeChecksResult",
    "build_runtime_checks_summary",
    "collect_app_metadata",
    "collect_route_rules",
    "get_default_file_check_spec_data",
    "get_default_file_check_specs",
    "get_default_path_check_spec_data",
    "get_default_path_check_specs",
    "get_default_route_check_spec_data",
    "get_default_route_check_specs",
    "log_runtime_checks_result",
    "raise_if_runtime_checks_failed",
    "resolve_configured_path",
    "resolve_service_root",
    "resolve_service_root_from_file",
    "run_database_check",
    "run_file_checks",
    "run_model_registry_check",
    "run_path_checks",
    "run_route_checks",
    "run_runtime_checks",
    "runtime_checks_result_to_dict",
]