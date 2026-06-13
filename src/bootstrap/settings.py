# services/vectoplan-chunk/src/bootstrap/settings.py
"""
Central startup/bootstrap settings for the `vectoplan-chunk` service.

This module is intentionally read-only.

Responsibilities:
- normalize Flask config and environment variables
- provide stable runtime-startup settings
- provide stable database-bootstrap settings
- keep dangerous DB mutation flags out of normal Gunicorn runtime by default
- centralize aliases and defaults for startup, schema bootstrap and seed bootstrap
- expose compact serializable summaries for status/debug endpoints

Important boundaries:
- no Flask app creation here
- no DB queries here
- no db.create_all() here
- no seeding here
- no model imports here
- no chunk generation here
- no ORM object traversal here

Design rule:

    Runtime startup must be cheap and read-only.
    Database bootstrap must be explicit and controlled.

The service may still read old legacy flags like:

    VECTOPLAN_CHUNK_AUTO_CREATE_ALL
    VECTOPLAN_CHUNK_AUTO_SEED_DEFAULTS

but runtime execution must only mutate the DB when the explicit runtime mutation
guard is enabled:

    VECTOPLAN_CHUNK_ALLOW_RUNTIME_DB_MUTATIONS=true

Default:

    Runtime DB mutations are disabled.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, is_dataclass
from pathlib import Path
from typing import Any, Final, Iterable, Mapping, MutableMapping, Sequence


# -----------------------------------------------------------------------------
# Constants
# -----------------------------------------------------------------------------

DEFAULT_SERVICE_NAME: Final[str] = "vectoplan-chunk"
DEFAULT_DISPLAY_NAME: Final[str] = "VECTOPLAN Chunk Service"
DEFAULT_CONFIG_NAME: Final[str] = "development"

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

DEFAULT_BLOCK_REGISTRY_ID: Final[str] = "debug-blocks"
DEFAULT_BLOCK_REGISTRY_VERSION: Final[str] = "1"

DEFAULT_SPAWN_X: Final[int] = 0
DEFAULT_SPAWN_Y: Final[int] = 2
DEFAULT_SPAWN_Z: Final[int] = 0
DEFAULT_SPAWN_YAW: Final[float] = 0.0
DEFAULT_SPAWN_PITCH: Final[float] = 0.0

DEFAULT_DB_DRIVER: Final[str] = "postgresql+psycopg"
DEFAULT_DB_HOST: Final[str] = "vectoplan-chunk-db"
DEFAULT_DB_PORT: Final[int] = 5432
DEFAULT_DB_NAME: Final[str] = "vectoplan_chunk"
DEFAULT_DB_USER: Final[str] = "vectoplan_chunk"
DEFAULT_DB_PASSWORD: Final[str] = "vectoplan_chunk"

DEFAULT_DB_URI: Final[str] = (
    "postgresql+psycopg://vectoplan_chunk:vectoplan_chunk"
    "@vectoplan-chunk-db:5432/vectoplan_chunk"
)

TRUE_VALUES: Final[set[str]] = {
    "1",
    "true",
    "t",
    "yes",
    "y",
    "on",
    "enabled",
    "enable",
}

FALSE_VALUES: Final[set[str]] = {
    "0",
    "false",
    "f",
    "no",
    "n",
    "off",
    "disabled",
    "disable",
}

RUNTIME_MODES: Final[set[str]] = {
    "runtime",
    "server",
    "gunicorn",
    "flask",
    "web",
}

DB_BOOTSTRAP_MODES: Final[set[str]] = {
    "db-bootstrap",
    "bootstrap-db",
    "schema-bootstrap",
    "init-db",
    "db-init",
    "migrate-dev",
}

ALL_KNOWN_MODES: Final[set[str]] = RUNTIME_MODES | DB_BOOTSTRAP_MODES

MISSING: Final[object] = object()


# -----------------------------------------------------------------------------
# Dataclasses
# -----------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ServiceIdentitySettings:
    """Service identity and high-level runtime metadata."""

    service_name: str
    app_name: str
    display_name: str
    config_name: str
    app_home: str
    service_root: str
    mode: str
    is_runtime_mode: bool
    is_db_bootstrap_mode: bool


@dataclass(frozen=True, slots=True)
class RuntimeStartupSettings:
    """
    Runtime startup settings.

    These settings describe what the normal Flask/Gunicorn startup is allowed to
    do. By default runtime startup is read-only.
    """

    run_startup_hooks: bool
    startup_strict: bool

    check_paths: bool
    check_files: bool
    check_routes: bool
    check_models: bool
    check_database: bool
    require_database: bool

    route_debug_errors: bool
    enable_dev_routes: bool
    enable_legacy_routes: bool

    startup_module: str
    print_startup_summary: bool

    allow_runtime_db_mutations: bool

    auto_create_all_requested: bool
    auto_seed_defaults_requested: bool
    seed_debug_blocks_requested: bool
    seed_dev_project_requested: bool

    auto_create_all_in_runtime: bool
    auto_seed_defaults_in_runtime: bool
    seed_debug_blocks_in_runtime: bool
    seed_dev_project_in_runtime: bool


@dataclass(frozen=True, slots=True)
class DatabaseSettings:
    """Database connection and SQLAlchemy settings."""

    driver: str
    host: str
    port: int
    name: str
    user: str
    password_set: bool

    database_uri: str
    sqlalchemy_database_uri: str
    database_url: str

    track_modifications: bool
    echo: bool
    record_queries: bool

    pool_pre_ping: bool
    pool_recycle: int
    pool_size: int
    max_overflow: int
    pool_timeout: int
    connect_timeout: int

    wait_for_ready: bool
    wait_timeout: int
    wait_interval: float

    check_on_startup: bool
    require_on_startup: bool

    is_postgresql: bool
    is_sqlite: bool


@dataclass(frozen=True, slots=True)
class SchemaBootstrapSettings:
    """
    Schema bootstrap settings.

    These settings are meant for the explicit DB bootstrap path, not normal
    Gunicorn runtime startup.
    """

    bootstrap_enabled: bool
    create_all: bool
    require_migrations: bool
    advisory_lock_enabled: bool
    advisory_lock_key: int
    fail_on_error: bool


@dataclass(frozen=True, slots=True)
class SeedBootstrapSettings:
    """
    Default seed settings.

    These settings are meant for the explicit DB bootstrap path.
    """

    seed_defaults: bool
    seed_debug_blocks: bool
    seed_dev_project: bool
    seed_on_empty_only: bool

    advisory_lock_enabled: bool
    advisory_lock_key: int
    fail_on_error: bool


@dataclass(frozen=True, slots=True)
class WorldDefaultsSettings:
    """Default project/universe/world settings used by the dev seed."""

    project_id: str
    project_slug: str
    project_name: str

    universe_id: str
    universe_slug: str
    universe_name: str

    world_id: str
    world_slug: str
    world_name: str

    template_id: str
    provider_id: str
    provider_world_id: str

    world_type: str
    world_role: str
    world_scope: str
    world_owner_type: str

    generator_type: str
    generator_version: str
    projection_type: str
    topology_type: str
    coordinate_system: str

    chunk_size: int
    cell_size: float
    surface_y: int
    min_y: int
    max_y: int
    seed: str

    block_registry_id: str
    block_registry_version: str

    spawn_x: int
    spawn_y: int
    spawn_z: int
    spawn_yaw: float
    spawn_pitch: float


@dataclass(frozen=True, slots=True)
class BlockDefaultsSettings:
    """Default debug block registry settings."""

    registry_id: str
    registry_version: str
    seed_debug_grass: bool
    seed_debug_dirt: bool


@dataclass(frozen=True, slots=True)
class ApiSettings:
    """API route and payload guard settings."""

    api_prefix: str
    healthcheck_path: str
    max_batch_chunks: int
    route_max_batch_chunks: int
    max_command_affected_cells: int

    max_object_size_x: int
    max_object_size_y: int
    max_object_size_z: int

    world_test_enabled: bool
    default_world_id: str


@dataclass(frozen=True, slots=True)
class BootstrapSettings:
    """Aggregate settings object for startup and DB bootstrap."""

    identity: ServiceIdentitySettings
    runtime: RuntimeStartupSettings
    database: DatabaseSettings
    schema: SchemaBootstrapSettings
    seed: SeedBootstrapSettings
    world_defaults: WorldDefaultsSettings
    block_defaults: BlockDefaultsSettings
    api: ApiSettings

    warnings: tuple[str, ...]


# -----------------------------------------------------------------------------
# Primitive safe helpers
# -----------------------------------------------------------------------------

def _safe_str(value: Any, default: str = "") -> str:
    """Normalize any value to a stripped string."""
    if value is None:
        return default

    try:
        result = str(value).strip()
    except Exception:
        return default

    return result if result else default


def _safe_lower(value: Any, default: str = "") -> str:
    """Normalize any value to lowercase string."""
    text = _safe_str(value, default)
    try:
        return text.lower()
    except Exception:
        return default.lower() if isinstance(default, str) else ""


def _safe_bool(value: Any, default: bool = False) -> bool:
    """Normalize bool-like values robustly."""
    if isinstance(value, bool):
        return value

    if isinstance(value, int) and not isinstance(value, bool):
        return bool(value)

    text = _safe_lower(value, "")

    if not text:
        return default

    if text in TRUE_VALUES:
        return True

    if text in FALSE_VALUES:
        return False

    return default


def _safe_int(
    value: Any,
    default: int = 0,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    """Normalize integer values robustly."""
    try:
        result = int(value)
    except (TypeError, ValueError):
        result = default
    except Exception:
        result = default

    if minimum is not None:
        try:
            result = max(minimum, result)
        except Exception:
            result = minimum

    if maximum is not None:
        try:
            result = min(maximum, result)
        except Exception:
            result = maximum

    return result


def _safe_float(
    value: Any,
    default: float = 0.0,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    """Normalize float values robustly."""
    try:
        result = float(value)
    except (TypeError, ValueError):
        result = default
    except Exception:
        result = default

    if minimum is not None:
        try:
            result = max(minimum, result)
        except Exception:
            result = minimum

    if maximum is not None:
        try:
            result = min(maximum, result)
        except Exception:
            result = maximum

    return result


def _safe_list(value: Any, default: Sequence[str] | None = None) -> list[str]:
    """Normalize a string/list/tuple/set into a list of non-empty strings."""
    if default is None:
        default = []

    if value is None:
        return list(default)

    if isinstance(value, str):
        raw_items = value.split(",")
    elif isinstance(value, Iterable):
        try:
            raw_items = list(value)
        except Exception:
            return list(default)
    else:
        return list(default)

    result: list[str] = []
    for item in raw_items:
        text = _safe_str(item, "")
        if text:
            result.append(text)

    return result


def _safe_path_string(value: Any, default: str = "") -> str:
    """Normalize a path-like value to a string."""
    if value is None:
        return default

    try:
        if isinstance(value, Path):
            return str(value)
    except Exception:
        pass

    return _safe_str(value, default)


def _dedupe_keys(primary_key: str, aliases: Sequence[str] | None = None) -> tuple[str, ...]:
    """Return ordered unique config/env keys."""
    keys: list[str] = []

    for key in (primary_key, *(aliases or ())):
        text = _safe_str(key, "")
        if text and text not in keys:
            keys.append(text)

    return tuple(keys)


def _safe_mapping_get(mapping: Mapping[str, Any] | MutableMapping[str, Any], key: str) -> Any:
    """Read a mapping value defensively."""
    try:
        return mapping[key]
    except KeyError:
        return MISSING
    except Exception:
        return MISSING


def _safe_has_mapping_key(mapping: Mapping[str, Any] | MutableMapping[str, Any], key: str) -> bool:
    """Check mapping key defensively."""
    try:
        return key in mapping
    except Exception:
        return False


# -----------------------------------------------------------------------------
# Config/env read helpers
# -----------------------------------------------------------------------------

def get_env_value(key: str, default: Any = MISSING) -> Any:
    """Read environment variable safely."""
    key = _safe_str(key, "")
    if not key:
        return default

    try:
        value = os.getenv(key)
    except Exception:
        return default

    if value is None:
        return default

    return value


def get_config_value(app: Any, key: str, default: Any = MISSING) -> Any:
    """Read Flask app.config value safely."""
    key = _safe_str(key, "")
    if not key or app is None:
        return default

    try:
        config = getattr(app, "config", None)
    except Exception:
        return default

    if config is None:
        return default

    try:
        if hasattr(config, "get"):
            value = config.get(key, MISSING)
            if value is not MISSING:
                return value
    except Exception:
        pass

    try:
        if _safe_has_mapping_key(config, key):
            value = _safe_mapping_get(config, key)
            if value is not MISSING:
                return value
    except Exception:
        pass

    return default


def get_raw_setting(
    app: Any,
    key: str,
    default: Any = None,
    aliases: Sequence[str] | None = None,
    prefer_env: bool = True,
) -> Any:
    """
    Read a raw setting from environment and Flask config.

    Environment values are preferred by default because Docker Compose and local
    PowerShell test sessions should be able to override stale config defaults.
    """
    keys = _dedupe_keys(key, aliases)

    if prefer_env:
        for candidate in keys:
            value = get_env_value(candidate, MISSING)
            if value is not MISSING:
                return value

        for candidate in keys:
            value = get_config_value(app, candidate, MISSING)
            if value is not MISSING:
                return value

        return default

    for candidate in keys:
        value = get_config_value(app, candidate, MISSING)
        if value is not MISSING:
            return value

    for candidate in keys:
        value = get_env_value(candidate, MISSING)
        if value is not MISSING:
            return value

    return default


def get_str_setting(
    app: Any,
    key: str,
    default: str = "",
    aliases: Sequence[str] | None = None,
    prefer_env: bool = True,
) -> str:
    """Read a string setting."""
    value = get_raw_setting(app, key, default, aliases, prefer_env=prefer_env)
    return _safe_str(value, default)


def get_bool_setting(
    app: Any,
    key: str,
    default: bool = False,
    aliases: Sequence[str] | None = None,
    prefer_env: bool = True,
) -> bool:
    """Read a bool setting."""
    value = get_raw_setting(app, key, default, aliases, prefer_env=prefer_env)
    return _safe_bool(value, default)


def get_int_setting(
    app: Any,
    key: str,
    default: int = 0,
    aliases: Sequence[str] | None = None,
    minimum: int | None = None,
    maximum: int | None = None,
    prefer_env: bool = True,
) -> int:
    """Read an integer setting."""
    value = get_raw_setting(app, key, default, aliases, prefer_env=prefer_env)
    return _safe_int(value, default, minimum=minimum, maximum=maximum)


def get_float_setting(
    app: Any,
    key: str,
    default: float = 0.0,
    aliases: Sequence[str] | None = None,
    minimum: float | None = None,
    maximum: float | None = None,
    prefer_env: bool = True,
) -> float:
    """Read a float setting."""
    value = get_raw_setting(app, key, default, aliases, prefer_env=prefer_env)
    return _safe_float(value, default, minimum=minimum, maximum=maximum)


def get_list_setting(
    app: Any,
    key: str,
    default: Sequence[str] | None = None,
    aliases: Sequence[str] | None = None,
    prefer_env: bool = True,
) -> list[str]:
    """Read a comma-separated/list setting."""
    value = get_raw_setting(app, key, default, aliases, prefer_env=prefer_env)
    return _safe_list(value, default)


# -----------------------------------------------------------------------------
# URI helpers
# -----------------------------------------------------------------------------

def build_database_uri_from_parts(
    driver: str,
    user: str,
    password: str,
    host: str,
    port: int,
    database: str,
) -> str:
    """Build a database URI from normalized parts."""
    driver = _safe_str(driver, DEFAULT_DB_DRIVER)
    user = _safe_str(user, DEFAULT_DB_USER)
    password = _safe_str(password, DEFAULT_DB_PASSWORD)
    host = _safe_str(host, DEFAULT_DB_HOST)
    port = _safe_int(port, DEFAULT_DB_PORT, minimum=1, maximum=65535)
    database = _safe_str(database, DEFAULT_DB_NAME)

    if driver.startswith("sqlite"):
        if database in {":memory:", "memory"}:
            return "sqlite:///:memory:"
        return f"sqlite:///{database}"

    if password:
        return f"{driver}://{user}:{password}@{host}:{port}/{database}"

    return f"{driver}://{user}@{host}:{port}/{database}"


def is_postgresql_uri(uri: str) -> bool:
    """Return whether a DB URI looks like PostgreSQL."""
    lowered = _safe_lower(uri, "")
    return lowered.startswith("postgresql://") or lowered.startswith("postgresql+")


def is_sqlite_uri(uri: str) -> bool:
    """Return whether a DB URI looks like SQLite."""
    lowered = _safe_lower(uri, "")
    return lowered.startswith("sqlite://") or lowered == "sqlite"


def normalize_mode(value: Any, default: str = "runtime") -> str:
    """Normalize service mode."""
    mode = _safe_lower(value, default)

    if not mode:
        return default

    mode = mode.replace("_", "-").strip()

    if mode in ALL_KNOWN_MODES:
        return mode

    return mode


def is_runtime_mode(mode: str) -> bool:
    """Return whether mode is a runtime/server mode."""
    return normalize_mode(mode, "runtime") in RUNTIME_MODES


def is_db_bootstrap_mode(mode: str) -> bool:
    """Return whether mode is a DB-bootstrap mode."""
    return normalize_mode(mode, "runtime") in DB_BOOTSTRAP_MODES


# -----------------------------------------------------------------------------
# Settings builders
# -----------------------------------------------------------------------------

def build_service_identity_settings(app: Any = None) -> ServiceIdentitySettings:
    """Build service identity settings."""
    mode = normalize_mode(
        get_str_setting(
            app,
            "VECTOPLAN_CHUNK_MODE",
            "runtime",
            aliases=(
                "VECTOPLAN_CHUNK_RUN_MODE",
                "VECTOPLAN_RUN_MODE",
                "RUN_MODE",
            ),
        ),
        "runtime",
    )

    service_root = get_str_setting(
        app,
        "SERVICE_ROOT",
        "",
        aliases=(
            "VECTOPLAN_CHUNK_SERVICE_ROOT",
            "APP_HOME",
            "VECTOPLAN_CHUNK_APP_HOME",
        ),
    )

    app_home = get_str_setting(
        app,
        "APP_HOME",
        service_root,
        aliases=(
            "VECTOPLAN_CHUNK_APP_HOME",
            "VECTOPLAN_EDITOR_APP_HOME",
        ),
    )

    return ServiceIdentitySettings(
        service_name=get_str_setting(
            app,
            "SERVICE_NAME",
            DEFAULT_SERVICE_NAME,
            aliases=(
                "VECTOPLAN_SERVICE_NAME",
                "VECTOPLAN_CHUNK_SERVICE_NAME",
                "APP_NAME",
            ),
        ),
        app_name=get_str_setting(
            app,
            "APP_NAME",
            DEFAULT_SERVICE_NAME,
            aliases=("VECTOPLAN_CHUNK_APP_NAME",),
        ),
        display_name=get_str_setting(
            app,
            "APP_DISPLAY_NAME",
            DEFAULT_DISPLAY_NAME,
            aliases=("VECTOPLAN_CHUNK_DISPLAY_NAME",),
        ),
        config_name=get_str_setting(
            app,
            "VECTOPLAN_CHUNK_CONFIG",
            DEFAULT_CONFIG_NAME,
            aliases=(
                "APP_ENV",
                "FLASK_ENV",
                "VECTOPLAN_EDITOR_CONFIG",
            ),
        ),
        app_home=app_home,
        service_root=service_root or app_home,
        mode=mode,
        is_runtime_mode=is_runtime_mode(mode),
        is_db_bootstrap_mode=is_db_bootstrap_mode(mode),
    )


def build_runtime_startup_settings(app: Any = None) -> RuntimeStartupSettings:
    """Build read-only runtime startup settings."""
    allow_runtime_db_mutations = get_bool_setting(
        app,
        "VECTOPLAN_CHUNK_ALLOW_RUNTIME_DB_MUTATIONS",
        False,
        aliases=(
            "CHUNK_ALLOW_RUNTIME_DB_MUTATIONS",
            "ALLOW_RUNTIME_DB_MUTATIONS",
        ),
    )

    auto_create_all_requested = get_bool_setting(
        app,
        "VECTOPLAN_CHUNK_AUTO_CREATE_ALL",
        False,
        aliases=("CHUNK_AUTO_CREATE_ALL",),
    )

    auto_seed_defaults_requested = get_bool_setting(
        app,
        "VECTOPLAN_CHUNK_AUTO_SEED_DEFAULTS",
        False,
        aliases=("CHUNK_AUTO_SEED_DEFAULTS",),
    )

    seed_debug_blocks_requested = get_bool_setting(
        app,
        "VECTOPLAN_CHUNK_SEED_DEBUG_BLOCKS",
        False,
        aliases=("CHUNK_SEED_DEBUG_BLOCKS",),
    )

    seed_dev_project_requested = get_bool_setting(
        app,
        "VECTOPLAN_CHUNK_SEED_DEV_PROJECT",
        False,
        aliases=("CHUNK_SEED_DEV_PROJECT",),
    )

    auto_create_all_in_runtime = bool(
        allow_runtime_db_mutations and auto_create_all_requested
    )
    auto_seed_defaults_in_runtime = bool(
        allow_runtime_db_mutations and auto_seed_defaults_requested
    )
    seed_debug_blocks_in_runtime = bool(
        auto_seed_defaults_in_runtime and seed_debug_blocks_requested
    )
    seed_dev_project_in_runtime = bool(
        auto_seed_defaults_in_runtime and seed_dev_project_requested
    )

    return RuntimeStartupSettings(
        run_startup_hooks=get_bool_setting(
            app,
            "VECTOPLAN_CHUNK_RUN_STARTUP_HOOKS",
            True,
            aliases=(
                "CHUNK_RUN_STARTUP_HOOKS",
                "RUN_STARTUP_HOOKS",
            ),
        ),
        startup_strict=get_bool_setting(
            app,
            "VECTOPLAN_CHUNK_STARTUP_STRICT",
            False,
            aliases=(
                "CHUNK_STARTUP_STRICT",
                "VECTOPLAN_EDITOR_STARTUP_STRICT",
            ),
        ),
        check_paths=get_bool_setting(
            app,
            "VECTOPLAN_CHUNK_STARTUP_CHECK_PATHS",
            True,
            aliases=("CHUNK_STARTUP_CHECK_PATHS",),
        ),
        check_files=get_bool_setting(
            app,
            "VECTOPLAN_CHUNK_STARTUP_CHECK_FILES",
            True,
            aliases=("CHUNK_STARTUP_CHECK_FILES",),
        ),
        check_routes=get_bool_setting(
            app,
            "VECTOPLAN_CHUNK_STARTUP_CHECK_ROUTES",
            True,
            aliases=("CHUNK_STARTUP_CHECK_ROUTES",),
        ),
        check_models=get_bool_setting(
            app,
            "VECTOPLAN_CHUNK_STARTUP_CHECK_MODELS",
            True,
            aliases=("CHUNK_STARTUP_CHECK_MODELS",),
        ),
        check_database=get_bool_setting(
            app,
            "VECTOPLAN_CHUNK_DB_CHECK_ON_STARTUP",
            True,
            aliases=("CHUNK_DB_CHECK_ON_STARTUP",),
        ),
        require_database=get_bool_setting(
            app,
            "VECTOPLAN_CHUNK_DB_REQUIRE_ON_STARTUP",
            True,
            aliases=("CHUNK_DB_REQUIRE_ON_STARTUP",),
        ),
        route_debug_errors=get_bool_setting(
            app,
            "VECTOPLAN_CHUNK_ROUTE_DEBUG_ERRORS",
            False,
            aliases=("CHUNK_ROUTE_DEBUG_ERRORS",),
        ),
        enable_dev_routes=get_bool_setting(
            app,
            "VECTOPLAN_CHUNK_ENABLE_DEV_ROUTES",
            True,
            aliases=("CHUNK_ENABLE_DEV_ROUTES",),
        ),
        enable_legacy_routes=get_bool_setting(
            app,
            "VECTOPLAN_CHUNK_ENABLE_LEGACY_ROUTES",
            True,
            aliases=("CHUNK_ENABLE_LEGACY_ROUTES",),
        ),
        startup_module=get_str_setting(
            app,
            "VECTOPLAN_CHUNK_STARTUP_MODULE",
            "src.bootstrap.startup",
            aliases=("CHUNK_STARTUP_MODULE",),
        ),
        print_startup_summary=get_bool_setting(
            app,
            "VECTOPLAN_CHUNK_PRINT_STARTUP_SUMMARY",
            True,
            aliases=(
                "CHUNK_PRINT_STARTUP_SUMMARY",
                "VECTOPLAN_EDITOR_PRINT_STARTUP_SUMMARY",
            ),
        ),
        allow_runtime_db_mutations=allow_runtime_db_mutations,
        auto_create_all_requested=auto_create_all_requested,
        auto_seed_defaults_requested=auto_seed_defaults_requested,
        seed_debug_blocks_requested=seed_debug_blocks_requested,
        seed_dev_project_requested=seed_dev_project_requested,
        auto_create_all_in_runtime=auto_create_all_in_runtime,
        auto_seed_defaults_in_runtime=auto_seed_defaults_in_runtime,
        seed_debug_blocks_in_runtime=seed_debug_blocks_in_runtime,
        seed_dev_project_in_runtime=seed_dev_project_in_runtime,
    )


def build_database_settings(app: Any = None) -> DatabaseSettings:
    """Build database settings."""
    driver = get_str_setting(
        app,
        "VECTOPLAN_CHUNK_DATABASE_DRIVER",
        DEFAULT_DB_DRIVER,
        aliases=("CHUNK_DATABASE_DRIVER",),
    )
    host = get_str_setting(
        app,
        "VECTOPLAN_CHUNK_DB_HOST",
        DEFAULT_DB_HOST,
        aliases=("CHUNK_DB_HOST",),
    )
    port = get_int_setting(
        app,
        "VECTOPLAN_CHUNK_DB_PORT",
        DEFAULT_DB_PORT,
        aliases=("CHUNK_DB_PORT",),
        minimum=1,
        maximum=65535,
    )
    name = get_str_setting(
        app,
        "VECTOPLAN_CHUNK_DB_NAME",
        DEFAULT_DB_NAME,
        aliases=("CHUNK_DB_NAME",),
    )
    user = get_str_setting(
        app,
        "VECTOPLAN_CHUNK_DB_USER",
        DEFAULT_DB_USER,
        aliases=("CHUNK_DB_USER",),
    )
    password = get_str_setting(
        app,
        "VECTOPLAN_CHUNK_DB_PASSWORD",
        DEFAULT_DB_PASSWORD,
        aliases=("CHUNK_DB_PASSWORD",),
    )

    uri_from_parts = build_database_uri_from_parts(
        driver=driver,
        user=user,
        password=password,
        host=host,
        port=port,
        database=name,
    )

    sqlalchemy_database_uri = get_str_setting(
        app,
        "SQLALCHEMY_DATABASE_URI",
        uri_from_parts,
        aliases=(
            "VECTOPLAN_CHUNK_SQLALCHEMY_DATABASE_URI",
            "VECTOPLAN_CHUNK_DATABASE_URI",
            "VECTOPLAN_CHUNK_DATABASE_URL",
            "DATABASE_URL",
        ),
    )
    database_uri = get_str_setting(
        app,
        "VECTOPLAN_CHUNK_DATABASE_URI",
        sqlalchemy_database_uri,
        aliases=(
            "VECTOPLAN_CHUNK_SQLALCHEMY_DATABASE_URI",
            "SQLALCHEMY_DATABASE_URI",
            "DATABASE_URL",
        ),
    )
    database_url = get_str_setting(
        app,
        "DATABASE_URL",
        database_uri,
        aliases=(
            "VECTOPLAN_CHUNK_DATABASE_URL",
            "VECTOPLAN_CHUNK_DATABASE_URI",
            "SQLALCHEMY_DATABASE_URI",
        ),
    )

    return DatabaseSettings(
        driver=driver,
        host=host,
        port=port,
        name=name,
        user=user,
        password_set=bool(password),
        database_uri=database_uri,
        sqlalchemy_database_uri=sqlalchemy_database_uri,
        database_url=database_url,
        track_modifications=get_bool_setting(
            app,
            "SQLALCHEMY_TRACK_MODIFICATIONS",
            False,
            aliases=("VECTOPLAN_CHUNK_SQLALCHEMY_TRACK_MODIFICATIONS",),
        ),
        echo=get_bool_setting(
            app,
            "VECTOPLAN_CHUNK_SQLALCHEMY_ECHO",
            False,
            aliases=("SQLALCHEMY_ECHO",),
        ),
        record_queries=get_bool_setting(
            app,
            "VECTOPLAN_CHUNK_SQLALCHEMY_RECORD_QUERIES",
            False,
            aliases=("SQLALCHEMY_RECORD_QUERIES",),
        ),
        pool_pre_ping=get_bool_setting(
            app,
            "VECTOPLAN_CHUNK_SQLALCHEMY_POOL_PRE_PING",
            True,
            aliases=("VECTOPLAN_CHUNK_DB_POOL_PRE_PING",),
        ),
        pool_recycle=get_int_setting(
            app,
            "VECTOPLAN_CHUNK_SQLALCHEMY_POOL_RECYCLE",
            1800,
            aliases=("VECTOPLAN_CHUNK_DB_POOL_RECYCLE",),
            minimum=0,
        ),
        pool_size=get_int_setting(
            app,
            "VECTOPLAN_CHUNK_SQLALCHEMY_POOL_SIZE",
            5,
            aliases=("VECTOPLAN_CHUNK_DB_POOL_SIZE",),
            minimum=1,
        ),
        max_overflow=get_int_setting(
            app,
            "VECTOPLAN_CHUNK_SQLALCHEMY_MAX_OVERFLOW",
            10,
            aliases=("VECTOPLAN_CHUNK_DB_MAX_OVERFLOW",),
            minimum=0,
        ),
        pool_timeout=get_int_setting(
            app,
            "VECTOPLAN_CHUNK_SQLALCHEMY_POOL_TIMEOUT",
            30,
            aliases=("VECTOPLAN_CHUNK_DB_POOL_TIMEOUT",),
            minimum=1,
        ),
        connect_timeout=get_int_setting(
            app,
            "VECTOPLAN_CHUNK_DB_CONNECT_TIMEOUT",
            15,
            aliases=("DATABASE_CONNECT_TIMEOUT",),
            minimum=1,
        ),
        wait_for_ready=get_bool_setting(
            app,
            "VECTOPLAN_CHUNK_DB_WAIT_FOR_READY",
            True,
            aliases=("CHUNK_DB_WAIT_FOR_READY",),
        ),
        wait_timeout=get_int_setting(
            app,
            "VECTOPLAN_CHUNK_DB_WAIT_TIMEOUT",
            60,
            aliases=("CHUNK_DB_WAIT_TIMEOUT",),
            minimum=1,
        ),
        wait_interval=get_float_setting(
            app,
            "VECTOPLAN_CHUNK_DB_WAIT_INTERVAL",
            2.0,
            aliases=("CHUNK_DB_WAIT_INTERVAL",),
            minimum=0.1,
        ),
        check_on_startup=get_bool_setting(
            app,
            "VECTOPLAN_CHUNK_DB_CHECK_ON_STARTUP",
            True,
            aliases=("CHUNK_DB_CHECK_ON_STARTUP",),
        ),
        require_on_startup=get_bool_setting(
            app,
            "VECTOPLAN_CHUNK_DB_REQUIRE_ON_STARTUP",
            True,
            aliases=("CHUNK_DB_REQUIRE_ON_STARTUP",),
        ),
        is_postgresql=is_postgresql_uri(sqlalchemy_database_uri),
        is_sqlite=is_sqlite_uri(sqlalchemy_database_uri),
    )


def build_schema_bootstrap_settings(app: Any = None) -> SchemaBootstrapSettings:
    """Build explicit schema-bootstrap settings."""
    bootstrap_enabled = get_bool_setting(
        app,
        "VECTOPLAN_CHUNK_DB_BOOTSTRAP_ENABLED",
        False,
        aliases=(
            "CHUNK_DB_BOOTSTRAP_ENABLED",
            "DB_BOOTSTRAP_ENABLED",
        ),
    )

    create_all = get_bool_setting(
        app,
        "VECTOPLAN_CHUNK_DB_BOOTSTRAP_CREATE_ALL",
        bootstrap_enabled,
        aliases=(
            "CHUNK_DB_BOOTSTRAP_CREATE_ALL",
            "DB_BOOTSTRAP_CREATE_ALL",
        ),
    )

    return SchemaBootstrapSettings(
        bootstrap_enabled=bootstrap_enabled,
        create_all=create_all,
        require_migrations=get_bool_setting(
            app,
            "VECTOPLAN_CHUNK_REQUIRE_MIGRATIONS",
            False,
            aliases=("CHUNK_REQUIRE_MIGRATIONS",),
        ),
        advisory_lock_enabled=get_bool_setting(
            app,
            "VECTOPLAN_CHUNK_DB_BOOTSTRAP_ADVISORY_LOCKS",
            True,
            aliases=(
                "CHUNK_DB_BOOTSTRAP_ADVISORY_LOCKS",
                "DB_BOOTSTRAP_ADVISORY_LOCKS",
            ),
        ),
        advisory_lock_key=get_int_setting(
            app,
            "VECTOPLAN_CHUNK_SCHEMA_BOOTSTRAP_LOCK_KEY",
            50020001,
            aliases=("CHUNK_SCHEMA_BOOTSTRAP_LOCK_KEY",),
            minimum=1,
        ),
        fail_on_error=get_bool_setting(
            app,
            "VECTOPLAN_CHUNK_DB_BOOTSTRAP_FAIL_ON_ERROR",
            True,
            aliases=(
                "CHUNK_DB_BOOTSTRAP_FAIL_ON_ERROR",
                "DB_BOOTSTRAP_FAIL_ON_ERROR",
            ),
        ),
    )


def build_seed_bootstrap_settings(app: Any = None) -> SeedBootstrapSettings:
    """Build explicit seed-bootstrap settings."""
    bootstrap_enabled = get_bool_setting(
        app,
        "VECTOPLAN_CHUNK_DB_BOOTSTRAP_ENABLED",
        False,
        aliases=(
            "CHUNK_DB_BOOTSTRAP_ENABLED",
            "DB_BOOTSTRAP_ENABLED",
        ),
    )

    seed_defaults = get_bool_setting(
        app,
        "VECTOPLAN_CHUNK_DB_BOOTSTRAP_SEED_DEFAULTS",
        bootstrap_enabled,
        aliases=(
            "CHUNK_DB_BOOTSTRAP_SEED_DEFAULTS",
            "DB_BOOTSTRAP_SEED_DEFAULTS",
        ),
    )

    return SeedBootstrapSettings(
        seed_defaults=seed_defaults,
        seed_debug_blocks=get_bool_setting(
            app,
            "VECTOPLAN_CHUNK_DB_BOOTSTRAP_SEED_DEBUG_BLOCKS",
            seed_defaults,
            aliases=(
                "CHUNK_DB_BOOTSTRAP_SEED_DEBUG_BLOCKS",
                "VECTOPLAN_CHUNK_SEED_DEBUG_BLOCKS",
            ),
        ),
        seed_dev_project=get_bool_setting(
            app,
            "VECTOPLAN_CHUNK_DB_BOOTSTRAP_SEED_DEV_PROJECT",
            seed_defaults,
            aliases=(
                "CHUNK_DB_BOOTSTRAP_SEED_DEV_PROJECT",
                "VECTOPLAN_CHUNK_SEED_DEV_PROJECT",
            ),
        ),
        seed_on_empty_only=get_bool_setting(
            app,
            "VECTOPLAN_CHUNK_SEED_ON_EMPTY_ONLY",
            True,
            aliases=("CHUNK_SEED_ON_EMPTY_ONLY",),
        ),
        advisory_lock_enabled=get_bool_setting(
            app,
            "VECTOPLAN_CHUNK_DB_BOOTSTRAP_ADVISORY_LOCKS",
            True,
            aliases=(
                "CHUNK_DB_BOOTSTRAP_ADVISORY_LOCKS",
                "DB_BOOTSTRAP_ADVISORY_LOCKS",
            ),
        ),
        advisory_lock_key=get_int_setting(
            app,
            "VECTOPLAN_CHUNK_SEED_BOOTSTRAP_LOCK_KEY",
            50020002,
            aliases=("CHUNK_SEED_BOOTSTRAP_LOCK_KEY",),
            minimum=1,
        ),
        fail_on_error=get_bool_setting(
            app,
            "VECTOPLAN_CHUNK_DB_BOOTSTRAP_FAIL_ON_ERROR",
            True,
            aliases=(
                "CHUNK_DB_BOOTSTRAP_FAIL_ON_ERROR",
                "DB_BOOTSTRAP_FAIL_ON_ERROR",
            ),
        ),
    )


def build_world_defaults_settings(app: Any = None) -> WorldDefaultsSettings:
    """Build world/project/universe default settings."""
    return WorldDefaultsSettings(
        project_id=get_str_setting(
            app,
            "VECTOPLAN_CHUNK_DEFAULT_PROJECT_ID",
            DEFAULT_PROJECT_ID,
        ),
        project_slug=get_str_setting(
            app,
            "VECTOPLAN_CHUNK_DEFAULT_PROJECT_SLUG",
            DEFAULT_PROJECT_SLUG,
        ),
        project_name=get_str_setting(
            app,
            "VECTOPLAN_CHUNK_DEFAULT_PROJECT_NAME",
            DEFAULT_PROJECT_NAME,
        ),
        universe_id=get_str_setting(
            app,
            "VECTOPLAN_CHUNK_DEFAULT_UNIVERSE_ID",
            DEFAULT_UNIVERSE_ID,
        ),
        universe_slug=get_str_setting(
            app,
            "VECTOPLAN_CHUNK_DEFAULT_UNIVERSE_SLUG",
            DEFAULT_UNIVERSE_SLUG,
        ),
        universe_name=get_str_setting(
            app,
            "VECTOPLAN_CHUNK_DEFAULT_UNIVERSE_NAME",
            DEFAULT_UNIVERSE_NAME,
        ),
        world_id=get_str_setting(
            app,
            "VECTOPLAN_CHUNK_DEFAULT_INSTANCE_WORLD_ID",
            DEFAULT_WORLD_ID,
        ),
        world_slug=get_str_setting(
            app,
            "VECTOPLAN_CHUNK_DEFAULT_INSTANCE_WORLD_SLUG",
            DEFAULT_WORLD_SLUG,
        ),
        world_name=get_str_setting(
            app,
            "VECTOPLAN_CHUNK_DEFAULT_INSTANCE_WORLD_NAME",
            DEFAULT_WORLD_NAME,
        ),
        template_id=get_str_setting(
            app,
            "VECTOPLAN_CHUNK_DEFAULT_WORLD_TEMPLATE_ID",
            DEFAULT_TEMPLATE_ID,
        ),
        provider_id=get_str_setting(
            app,
            "VECTOPLAN_CHUNK_DEFAULT_PROVIDER_ID",
            DEFAULT_PROVIDER_ID,
        ),
        provider_world_id=get_str_setting(
            app,
            "VECTOPLAN_CHUNK_DEFAULT_PROVIDER_WORLD_ID",
            DEFAULT_PROVIDER_WORLD_ID,
        ),
        world_type=get_str_setting(
            app,
            "VECTOPLAN_CHUNK_DEFAULT_WORLD_TYPE",
            "runtime-world",
        ),
        world_role=get_str_setting(
            app,
            "VECTOPLAN_CHUNK_DEFAULT_WORLD_ROLE",
            "default_spawn",
        ),
        world_scope=get_str_setting(
            app,
            "VECTOPLAN_CHUNK_DEFAULT_WORLD_SCOPE",
            "project",
        ),
        world_owner_type=get_str_setting(
            app,
            "VECTOPLAN_CHUNK_DEFAULT_WORLD_OWNER_TYPE",
            "project",
        ),
        generator_type=get_str_setting(
            app,
            "VECTOPLAN_CHUNK_DEFAULT_GENERATOR_TYPE",
            DEFAULT_GENERATOR_TYPE,
        ),
        generator_version=get_str_setting(
            app,
            "VECTOPLAN_CHUNK_DEFAULT_GENERATOR_VERSION",
            DEFAULT_GENERATOR_VERSION,
        ),
        projection_type=get_str_setting(
            app,
            "VECTOPLAN_CHUNK_DEFAULT_PROJECTION_TYPE",
            DEFAULT_PROJECTION_TYPE,
        ),
        topology_type=get_str_setting(
            app,
            "VECTOPLAN_CHUNK_DEFAULT_TOPOLOGY_TYPE",
            DEFAULT_TOPOLOGY_TYPE,
        ),
        coordinate_system=get_str_setting(
            app,
            "VECTOPLAN_CHUNK_DEFAULT_COORDINATE_SYSTEM",
            DEFAULT_COORDINATE_SYSTEM,
        ),
        chunk_size=get_int_setting(
            app,
            "VECTOPLAN_CHUNK_DEFAULT_CHUNK_SIZE",
            DEFAULT_CHUNK_SIZE,
            minimum=1,
            maximum=4096,
        ),
        cell_size=get_float_setting(
            app,
            "VECTOPLAN_CHUNK_DEFAULT_CELL_SIZE",
            DEFAULT_CELL_SIZE,
            minimum=0.000001,
        ),
        surface_y=get_int_setting(
            app,
            "VECTOPLAN_CHUNK_DEFAULT_SURFACE_Y",
            DEFAULT_SURFACE_Y,
        ),
        min_y=get_int_setting(
            app,
            "VECTOPLAN_CHUNK_DEFAULT_MIN_Y",
            DEFAULT_MIN_Y,
        ),
        max_y=get_int_setting(
            app,
            "VECTOPLAN_CHUNK_DEFAULT_MAX_Y",
            DEFAULT_MAX_Y,
        ),
        seed=get_str_setting(
            app,
            "VECTOPLAN_CHUNK_DEFAULT_SEED",
            DEFAULT_SEED,
        ),
        block_registry_id=get_str_setting(
            app,
            "VECTOPLAN_CHUNK_DEFAULT_BLOCK_REGISTRY_ID",
            DEFAULT_BLOCK_REGISTRY_ID,
        ),
        block_registry_version=get_str_setting(
            app,
            "VECTOPLAN_CHUNK_DEFAULT_BLOCK_REGISTRY_VERSION",
            DEFAULT_BLOCK_REGISTRY_VERSION,
        ),
        spawn_x=get_int_setting(
            app,
            "VECTOPLAN_CHUNK_DEFAULT_SPAWN_X",
            DEFAULT_SPAWN_X,
        ),
        spawn_y=get_int_setting(
            app,
            "VECTOPLAN_CHUNK_DEFAULT_SPAWN_Y",
            DEFAULT_SPAWN_Y,
        ),
        spawn_z=get_int_setting(
            app,
            "VECTOPLAN_CHUNK_DEFAULT_SPAWN_Z",
            DEFAULT_SPAWN_Z,
        ),
        spawn_yaw=get_float_setting(
            app,
            "VECTOPLAN_CHUNK_DEFAULT_SPAWN_YAW",
            DEFAULT_SPAWN_YAW,
        ),
        spawn_pitch=get_float_setting(
            app,
            "VECTOPLAN_CHUNK_DEFAULT_SPAWN_PITCH",
            DEFAULT_SPAWN_PITCH,
        ),
    )


def build_block_defaults_settings(app: Any = None) -> BlockDefaultsSettings:
    """Build debug block default settings."""
    return BlockDefaultsSettings(
        registry_id=get_str_setting(
            app,
            "VECTOPLAN_CHUNK_DEFAULT_BLOCK_REGISTRY_ID",
            DEFAULT_BLOCK_REGISTRY_ID,
        ),
        registry_version=get_str_setting(
            app,
            "VECTOPLAN_CHUNK_DEFAULT_BLOCK_REGISTRY_VERSION",
            DEFAULT_BLOCK_REGISTRY_VERSION,
        ),
        seed_debug_grass=get_bool_setting(
            app,
            "VECTOPLAN_CHUNK_SEED_DEBUG_GRASS",
            True,
        ),
        seed_debug_dirt=get_bool_setting(
            app,
            "VECTOPLAN_CHUNK_SEED_DEBUG_DIRT",
            True,
        ),
    )


def build_api_settings(app: Any = None) -> ApiSettings:
    """Build API guard and route settings."""
    return ApiSettings(
        api_prefix=get_str_setting(
            app,
            "VECTOPLAN_CHUNK_API_PREFIX",
            "",
        ),
        healthcheck_path=get_str_setting(
            app,
            "VECTOPLAN_CHUNK_HEALTHCHECK_PATH",
            "/projects/_status",
        ),
        max_batch_chunks=get_int_setting(
            app,
            "VECTOPLAN_CHUNK_MAX_BATCH_CHUNKS",
            256,
            minimum=1,
            maximum=8192,
        ),
        route_max_batch_chunks=get_int_setting(
            app,
            "VECTOPLAN_CHUNK_ROUTE_MAX_BATCH_CHUNKS",
            256,
            minimum=1,
            maximum=8192,
        ),
        max_command_affected_cells=get_int_setting(
            app,
            "VECTOPLAN_CHUNK_MAX_COMMAND_AFFECTED_CELLS",
            65536,
            minimum=1,
        ),
        max_object_size_x=get_int_setting(
            app,
            "VECTOPLAN_CHUNK_MAX_OBJECT_SIZE_X",
            256,
            minimum=1,
        ),
        max_object_size_y=get_int_setting(
            app,
            "VECTOPLAN_CHUNK_MAX_OBJECT_SIZE_Y",
            256,
            minimum=1,
        ),
        max_object_size_z=get_int_setting(
            app,
            "VECTOPLAN_CHUNK_MAX_OBJECT_SIZE_Z",
            256,
            minimum=1,
        ),
        world_test_enabled=get_bool_setting(
            app,
            "VECTOPLAN_CHUNK_WORLD_TEST_ENABLED",
            True,
        ),
        default_world_id=get_str_setting(
            app,
            "VECTOPLAN_CHUNK_DEFAULT_WORLD_ID",
            DEFAULT_TEMPLATE_ID,
        ),
    )


def build_bootstrap_settings(app: Any = None) -> BootstrapSettings:
    """Build aggregate startup/bootstrap settings."""
    identity = build_service_identity_settings(app)
    runtime = build_runtime_startup_settings(app)
    database = build_database_settings(app)
    schema = build_schema_bootstrap_settings(app)
    seed = build_seed_bootstrap_settings(app)
    world_defaults = build_world_defaults_settings(app)
    block_defaults = build_block_defaults_settings(app)
    api = build_api_settings(app)

    warnings: list[str] = []

    if runtime.allow_runtime_db_mutations:
        warnings.append(
            "Runtime DB mutations are enabled. This should only be used for local one-worker development."
        )

    if runtime.auto_create_all_requested and not runtime.auto_create_all_in_runtime:
        warnings.append(
            "VECTOPLAN_CHUNK_AUTO_CREATE_ALL was requested but ignored in runtime because "
            "VECTOPLAN_CHUNK_ALLOW_RUNTIME_DB_MUTATIONS is false."
        )

    if runtime.auto_seed_defaults_requested and not runtime.auto_seed_defaults_in_runtime:
        warnings.append(
            "VECTOPLAN_CHUNK_AUTO_SEED_DEFAULTS was requested but ignored in runtime because "
            "VECTOPLAN_CHUNK_ALLOW_RUNTIME_DB_MUTATIONS is false."
        )

    if identity.is_runtime_mode and schema.bootstrap_enabled:
        warnings.append(
            "DB bootstrap is enabled while service mode is runtime. Prefer a dedicated db-bootstrap mode/container."
        )

    if identity.is_db_bootstrap_mode and not schema.bootstrap_enabled:
        warnings.append(
            "Service mode indicates DB bootstrap, but VECTOPLAN_CHUNK_DB_BOOTSTRAP_ENABLED is false."
        )

    if database.is_sqlite and schema.advisory_lock_enabled:
        warnings.append(
            "SQLite does not support PostgreSQL advisory locks; bootstrap lock will be a no-op."
        )

    return BootstrapSettings(
        identity=identity,
        runtime=runtime,
        database=database,
        schema=schema,
        seed=seed,
        world_defaults=world_defaults,
        block_defaults=block_defaults,
        api=api,
        warnings=tuple(warnings),
    )


# -----------------------------------------------------------------------------
# Convenience policy helpers
# -----------------------------------------------------------------------------

def should_run_startup_hooks(app: Any = None) -> bool:
    """Return whether runtime startup hooks should run."""
    try:
        return build_runtime_startup_settings(app).run_startup_hooks
    except Exception:
        return True


def is_startup_strict(app: Any = None) -> bool:
    """Return whether startup strict mode is enabled."""
    try:
        return build_runtime_startup_settings(app).startup_strict
    except Exception:
        return False


def should_check_database_on_startup(app: Any = None) -> bool:
    """Return whether runtime startup should ping/check DB."""
    try:
        return build_runtime_startup_settings(app).check_database
    except Exception:
        return True


def should_require_database_on_startup(app: Any = None) -> bool:
    """Return whether runtime startup should fail if DB is unavailable."""
    try:
        return build_runtime_startup_settings(app).require_database
    except Exception:
        return True


def should_run_create_all_in_runtime(app: Any = None) -> bool:
    """
    Return whether db.create_all() is allowed during normal runtime startup.

    Default is false, even when legacy AUTO_CREATE_ALL is true.
    """
    try:
        return build_runtime_startup_settings(app).auto_create_all_in_runtime
    except Exception:
        return False


def should_run_seed_in_runtime(app: Any = None) -> bool:
    """
    Return whether default seed is allowed during normal runtime startup.

    Default is false, even when legacy AUTO_SEED_DEFAULTS is true.
    """
    try:
        return build_runtime_startup_settings(app).auto_seed_defaults_in_runtime
    except Exception:
        return False


def should_run_db_bootstrap(app: Any = None) -> bool:
    """Return whether explicit DB bootstrap is enabled."""
    try:
        settings = build_bootstrap_settings(app)
        return bool(settings.schema.bootstrap_enabled)
    except Exception:
        return False


def should_run_schema_bootstrap(app: Any = None) -> bool:
    """Return whether explicit schema bootstrap should run."""
    try:
        settings = build_bootstrap_settings(app)
        return bool(settings.schema.bootstrap_enabled and settings.schema.create_all)
    except Exception:
        return False


def should_run_seed_bootstrap(app: Any = None) -> bool:
    """Return whether explicit seed bootstrap should run."""
    try:
        settings = build_bootstrap_settings(app)
        return bool(settings.schema.bootstrap_enabled and settings.seed.seed_defaults)
    except Exception:
        return False


# -----------------------------------------------------------------------------
# Serialization helpers
# -----------------------------------------------------------------------------

def settings_to_dict(value: Any) -> dict[str, Any]:
    """Serialize dataclass settings recursively to a plain dict."""
    try:
        if is_dataclass(value):
            return asdict(value)
    except Exception:
        pass

    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            safe_key = _safe_str(key, "")
            if not safe_key:
                continue
            if is_dataclass(item):
                try:
                    result[safe_key] = asdict(item)
                except Exception:
                    result[safe_key] = _safe_str(item, "")
            else:
                result[safe_key] = item
        return result

    return {}


def build_settings_summary(app: Any = None) -> dict[str, Any]:
    """Return compact serializable settings summary for status endpoints."""
    try:
        settings = build_bootstrap_settings(app)
    except Exception as exc:
        return {
            "ok": False,
            "error": _safe_str(exc, exc.__class__.__name__),
        }

    return {
        "ok": True,
        "identity": {
            "serviceName": settings.identity.service_name,
            "appName": settings.identity.app_name,
            "displayName": settings.identity.display_name,
            "configName": settings.identity.config_name,
            "mode": settings.identity.mode,
            "isRuntimeMode": settings.identity.is_runtime_mode,
            "isDbBootstrapMode": settings.identity.is_db_bootstrap_mode,
        },
        "runtime": {
            "runStartupHooks": settings.runtime.run_startup_hooks,
            "startupStrict": settings.runtime.startup_strict,
            "checkPaths": settings.runtime.check_paths,
            "checkFiles": settings.runtime.check_files,
            "checkRoutes": settings.runtime.check_routes,
            "checkModels": settings.runtime.check_models,
            "checkDatabase": settings.runtime.check_database,
            "requireDatabase": settings.runtime.require_database,
            "allowRuntimeDbMutations": settings.runtime.allow_runtime_db_mutations,
            "autoCreateAllRequested": settings.runtime.auto_create_all_requested,
            "autoSeedDefaultsRequested": settings.runtime.auto_seed_defaults_requested,
            "autoCreateAllInRuntime": settings.runtime.auto_create_all_in_runtime,
            "autoSeedDefaultsInRuntime": settings.runtime.auto_seed_defaults_in_runtime,
        },
        "database": {
            "driver": settings.database.driver,
            "host": settings.database.host,
            "port": settings.database.port,
            "name": settings.database.name,
            "user": settings.database.user,
            "passwordSet": settings.database.password_set,
            "checkOnStartup": settings.database.check_on_startup,
            "requireOnStartup": settings.database.require_on_startup,
            "isPostgresql": settings.database.is_postgresql,
            "isSqlite": settings.database.is_sqlite,
        },
        "schemaBootstrap": {
            "bootstrapEnabled": settings.schema.bootstrap_enabled,
            "createAll": settings.schema.create_all,
            "requireMigrations": settings.schema.require_migrations,
            "advisoryLockEnabled": settings.schema.advisory_lock_enabled,
            "advisoryLockKey": settings.schema.advisory_lock_key,
            "failOnError": settings.schema.fail_on_error,
        },
        "seedBootstrap": {
            "seedDefaults": settings.seed.seed_defaults,
            "seedDebugBlocks": settings.seed.seed_debug_blocks,
            "seedDevProject": settings.seed.seed_dev_project,
            "seedOnEmptyOnly": settings.seed.seed_on_empty_only,
            "advisoryLockEnabled": settings.seed.advisory_lock_enabled,
            "advisoryLockKey": settings.seed.advisory_lock_key,
            "failOnError": settings.seed.fail_on_error,
        },
        "worldDefaults": {
            "projectId": settings.world_defaults.project_id,
            "universeId": settings.world_defaults.universe_id,
            "worldId": settings.world_defaults.world_id,
            "templateId": settings.world_defaults.template_id,
            "providerId": settings.world_defaults.provider_id,
            "providerWorldId": settings.world_defaults.provider_world_id,
            "chunkSize": settings.world_defaults.chunk_size,
            "cellSize": settings.world_defaults.cell_size,
            "surfaceY": settings.world_defaults.surface_y,
            "minY": settings.world_defaults.min_y,
            "maxY": settings.world_defaults.max_y,
        },
        "api": {
            "apiPrefix": settings.api.api_prefix,
            "healthcheckPath": settings.api.healthcheck_path,
            "maxBatchChunks": settings.api.max_batch_chunks,
            "routeMaxBatchChunks": settings.api.route_max_batch_chunks,
            "maxCommandAffectedCells": settings.api.max_command_affected_cells,
            "worldTestEnabled": settings.api.world_test_enabled,
            "defaultWorldId": settings.api.default_world_id,
        },
        "warnings": list(settings.warnings),
    }


def build_env_debug_snapshot(keys: Sequence[str] | None = None) -> dict[str, str]:
    """
    Build a safe debug snapshot of selected environment variables.

    Secret/password values are masked.
    """
    if keys is None:
        keys = (
            "VECTOPLAN_CHUNK_MODE",
            "VECTOPLAN_CHUNK_RUN_MODE",
            "VECTOPLAN_CHUNK_RUN_STARTUP_HOOKS",
            "VECTOPLAN_CHUNK_ALLOW_RUNTIME_DB_MUTATIONS",
            "VECTOPLAN_CHUNK_AUTO_CREATE_ALL",
            "VECTOPLAN_CHUNK_AUTO_SEED_DEFAULTS",
            "VECTOPLAN_CHUNK_DB_BOOTSTRAP_ENABLED",
            "VECTOPLAN_CHUNK_DB_BOOTSTRAP_CREATE_ALL",
            "VECTOPLAN_CHUNK_DB_BOOTSTRAP_SEED_DEFAULTS",
            "VECTOPLAN_CHUNK_DB_HOST",
            "VECTOPLAN_CHUNK_DB_PORT",
            "VECTOPLAN_CHUNK_DB_NAME",
            "VECTOPLAN_CHUNK_DB_USER",
            "VECTOPLAN_CHUNK_DB_PASSWORD",
            "SQLALCHEMY_DATABASE_URI",
            "DATABASE_URL",
        )

    snapshot: dict[str, str] = {}

    for key in keys:
        safe_key = _safe_str(key, "")
        if not safe_key:
            continue

        value = get_env_value(safe_key, MISSING)
        if value is MISSING:
            continue

        lowered = safe_key.lower()
        if "password" in lowered or "secret" in lowered or "token" in lowered:
            snapshot[safe_key] = "***"
        else:
            snapshot[safe_key] = _safe_str(value, "")

    return snapshot


# -----------------------------------------------------------------------------
# Public exports
# -----------------------------------------------------------------------------

__all__ = [
    "ApiSettings",
    "BlockDefaultsSettings",
    "BootstrapSettings",
    "DatabaseSettings",
    "SchemaBootstrapSettings",
    "SeedBootstrapSettings",
    "ServiceIdentitySettings",
    "RuntimeStartupSettings",
    "WorldDefaultsSettings",
    "build_api_settings",
    "build_block_defaults_settings",
    "build_bootstrap_settings",
    "build_database_settings",
    "build_database_uri_from_parts",
    "build_env_debug_snapshot",
    "build_schema_bootstrap_settings",
    "build_seed_bootstrap_settings",
    "build_service_identity_settings",
    "build_runtime_startup_settings",
    "build_settings_summary",
    "build_world_defaults_settings",
    "get_bool_setting",
    "get_config_value",
    "get_env_value",
    "get_float_setting",
    "get_int_setting",
    "get_list_setting",
    "get_raw_setting",
    "get_str_setting",
    "is_db_bootstrap_mode",
    "is_postgresql_uri",
    "is_runtime_mode",
    "is_sqlite_uri",
    "normalize_mode",
    "settings_to_dict",
    "should_check_database_on_startup",
    "should_require_database_on_startup",
    "should_run_create_all_in_runtime",
    "should_run_db_bootstrap",
    "should_run_schema_bootstrap",
    "should_run_seed_bootstrap",
    "should_run_seed_in_runtime",
    "should_run_startup_hooks",
    "is_startup_strict",
]