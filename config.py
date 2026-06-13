# services/vectoplan-chunk/config.py
"""
Central configuration for the `vectoplan-chunk` service.

This configuration supports the current transition from the copied editor shell
to a PostgreSQL-backed chunk/world service.

Core semantics:

    projectId       = dev-project
    universeId      = dev-universe
    worldId         = world_spawn
    templateId      = flat
    providerWorldId = flat

Meaning:

    Project        = top-level container
    Universe       = container for one or more concrete worlds
    world_spawn    = concrete editable world instance
    flat           = provider/template world
    PostgreSQL     = persistence for projects, universes, worlds, snapshots,
                     commands, events and later multi-block objects

Important:
- This file contains configuration and defensive helpers only.
- No business logic.
- No database connection is opened here.
- No tables are created here.
- No migrations are run here.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Final
from urllib.parse import quote_plus


# -----------------------------------------------------------------------------
# Internal constants
# -----------------------------------------------------------------------------

_TRUE_VALUES: Final[set[str]] = {"1", "true", "t", "yes", "y", "on", "enabled"}
_FALSE_VALUES: Final[set[str]] = {"0", "false", "f", "no", "n", "off", "disabled"}

DEFAULT_SERVICE_NAME: Final[str] = "vectoplan-chunk"
DEFAULT_APP_DISPLAY_NAME: Final[str] = "VECTOPLAN Chunk Service"
DEFAULT_EXTENSION_NAMESPACE: Final[str] = "vectoplan_chunk"

DEFAULT_PROJECT_ID: Final[str] = "dev-project"
DEFAULT_UNIVERSE_ID: Final[str] = "dev-universe"
DEFAULT_INSTANCE_WORLD_ID: Final[str] = "world_spawn"
DEFAULT_WORLD_TEMPLATE_ID: Final[str] = "flat"
DEFAULT_PROVIDER_WORLD_ID: Final[str] = "flat"
DEFAULT_PROVIDER_ID: Final[str] = "flat"

DEFAULT_CHUNK_SIZE: Final[int] = 16
DEFAULT_CELL_SIZE: Final[float] = 1.0
DEFAULT_SURFACE_Y: Final[int] = 0
DEFAULT_MIN_Y: Final[int] = -8
DEFAULT_MAX_Y: Final[int] = 64

DEFAULT_GENERATOR_TYPE: Final[str] = "flat-world"
DEFAULT_GENERATOR_VERSION: Final[str] = "1"
DEFAULT_PROJECTION_TYPE: Final[str] = "flat-local-v1"
DEFAULT_TOPOLOGY_TYPE: Final[str] = "flat-unbounded-v1"
DEFAULT_COORDINATE_SYSTEM: Final[str] = "vectoplan-world-y-up-v1"
DEFAULT_BLOCK_REGISTRY_ID: Final[str] = "debug-blocks"
DEFAULT_BLOCK_REGISTRY_VERSION: Final[str] = "1"

DEFAULT_DATABASE_DRIVER: Final[str] = "postgresql+psycopg"
DEFAULT_DATABASE_HOST: Final[str] = "vectoplan-chunk-db"
DEFAULT_DATABASE_PORT: Final[int] = 5432
DEFAULT_DATABASE_NAME: Final[str] = "vectoplan_chunk"
DEFAULT_DATABASE_USER: Final[str] = "vectoplan_chunk"
DEFAULT_DATABASE_PASSWORD: Final[str] = "vectoplan_chunk"


# -----------------------------------------------------------------------------
# Defensive environment helpers
# -----------------------------------------------------------------------------

def _safe_getenv(name: str) -> str | None:
    """Read an environment variable defensively."""
    try:
        return os.getenv(name)
    except Exception:
        return None


def _normalize_text(value: Any) -> str | None:
    """Normalize text-like values."""
    if value is None:
        return None

    try:
        normalized = str(value).strip()
    except Exception:
        return None

    return normalized or None


def _read_str_env(name: str, default: str) -> str:
    """Read string env var with fallback."""
    value = _normalize_text(_safe_getenv(name))
    return value if value is not None else default


def _read_optional_str_env(name: str, default: str | None = None) -> str | None:
    """Read optional string env var."""
    value = _normalize_text(_safe_getenv(name))
    return value if value is not None else default


def _read_bool_env(name: str, default: bool = False) -> bool:
    """Read boolean env var with fallback."""
    raw_value = _normalize_text(_safe_getenv(name))
    if raw_value is None:
        return default

    normalized = raw_value.lower()

    if normalized in _TRUE_VALUES:
        return True

    if normalized in _FALSE_VALUES:
        return False

    return default


def _read_int_env(
    name: str,
    default: int,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    """Read integer env var with optional clamping."""
    raw_value = _normalize_text(_safe_getenv(name))

    if raw_value is None:
        value = default
    else:
        try:
            value = int(raw_value)
        except (TypeError, ValueError):
            value = default

    if minimum is not None:
        value = max(minimum, value)

    if maximum is not None:
        value = min(maximum, value)

    return value


def _read_float_env(
    name: str,
    default: float,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    """Read float env var with optional clamping."""
    raw_value = _normalize_text(_safe_getenv(name))

    if raw_value is None:
        value = default
    else:
        try:
            value = float(raw_value)
        except (TypeError, ValueError):
            value = default

    if minimum is not None:
        value = max(minimum, value)

    if maximum is not None:
        value = min(maximum, value)

    return value


def _read_str_env_any(names: tuple[str, ...], default: str) -> str:
    """Read the first available string env var from a priority list."""
    for name in names:
        value = _normalize_text(_safe_getenv(name))
        if value is not None:
            return value

    return default


def _read_optional_str_env_any(
    names: tuple[str, ...],
    default: str | None = None,
) -> str | None:
    """Read the first available optional string env var from a priority list."""
    for name in names:
        value = _normalize_text(_safe_getenv(name))
        if value is not None:
            return value

    return default


def _read_bool_env_any(names: tuple[str, ...], default: bool = False) -> bool:
    """Read the first available boolean env var from a priority list."""
    for name in names:
        raw_value = _normalize_text(_safe_getenv(name))
        if raw_value is None:
            continue

        normalized = raw_value.lower()

        if normalized in _TRUE_VALUES:
            return True

        if normalized in _FALSE_VALUES:
            return False

        return default

    return default


def _read_int_env_any(
    names: tuple[str, ...],
    default: int,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    """Read the first available integer env var from a priority list."""
    value = default

    for name in names:
        raw_value = _normalize_text(_safe_getenv(name))
        if raw_value is None:
            continue

        try:
            value = int(raw_value)
        except (TypeError, ValueError):
            value = default

        break

    if minimum is not None:
        value = max(minimum, value)

    if maximum is not None:
        value = min(maximum, value)

    return value


def _read_float_env_any(
    names: tuple[str, ...],
    default: float,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    """Read the first available float env var from a priority list."""
    value = default

    for name in names:
        raw_value = _normalize_text(_safe_getenv(name))
        if raw_value is None:
            continue

        try:
            value = float(raw_value)
        except (TypeError, ValueError):
            value = default

        break

    if minimum is not None:
        value = max(minimum, value)

    if maximum is not None:
        value = min(maximum, value)

    return value


def _resolve_service_root() -> Path:
    """Resolve service root directory defensively."""
    try:
        return Path(__file__).resolve().parent
    except Exception:
        return Path(".").resolve()


SERVICE_ROOT: Final[Path] = _resolve_service_root()


def _build_path(*parts: str) -> Path:
    """Build path relative to service root."""
    try:
        return SERVICE_ROOT.joinpath(*parts)
    except Exception:
        return SERVICE_ROOT


def _quote_database_part(value: str) -> str:
    """Quote one URI component for database URI construction."""
    try:
        return quote_plus(value)
    except Exception:
        return value


def _build_postgres_uri_from_parts() -> str:
    """
    Build PostgreSQL URI from individual env values.

    Priority for complete URI is handled elsewhere. This function only builds
    a fallback URI from host/user/password/db parts.
    """
    driver = _read_str_env_any(
        (
            "VECTOPLAN_CHUNK_DATABASE_DRIVER",
            "DATABASE_DRIVER",
        ),
        DEFAULT_DATABASE_DRIVER,
    )

    host = _read_str_env_any(
        (
            "VECTOPLAN_CHUNK_DB_HOST",
            "VECTOPLAN_CHUNK_POSTGRES_HOST",
            "POSTGRES_HOST",
            "DB_HOST",
        ),
        DEFAULT_DATABASE_HOST,
    )

    port = _read_int_env_any(
        (
            "VECTOPLAN_CHUNK_DB_PORT",
            "VECTOPLAN_CHUNK_POSTGRES_PORT",
            "POSTGRES_PORT",
            "DB_PORT",
        ),
        DEFAULT_DATABASE_PORT,
        minimum=1,
        maximum=65535,
    )

    database = _read_str_env_any(
        (
            "VECTOPLAN_CHUNK_DB_NAME",
            "VECTOPLAN_CHUNK_POSTGRES_DB",
            "POSTGRES_DB",
            "DB_NAME",
        ),
        DEFAULT_DATABASE_NAME,
    )

    user = _read_str_env_any(
        (
            "VECTOPLAN_CHUNK_DB_USER",
            "VECTOPLAN_CHUNK_POSTGRES_USER",
            "POSTGRES_USER",
            "DB_USER",
        ),
        DEFAULT_DATABASE_USER,
    )

    password = _read_str_env_any(
        (
            "VECTOPLAN_CHUNK_DB_PASSWORD",
            "VECTOPLAN_CHUNK_POSTGRES_PASSWORD",
            "POSTGRES_PASSWORD",
            "DB_PASSWORD",
        ),
        DEFAULT_DATABASE_PASSWORD,
    )

    safe_user = _quote_database_part(user)
    safe_password = _quote_database_part(password)
    safe_database = _quote_database_part(database)

    return f"{driver}://{safe_user}:{safe_password}@{host}:{port}/{safe_database}"


def _resolve_database_uri(*, testing: bool = False) -> str:
    """
    Resolve database URI.

    Priority:
    1. VECTOPLAN_CHUNK_DATABASE_URL
    2. VECTOPLAN_CHUNK_SQLALCHEMY_DATABASE_URI
    3. DATABASE_URL
    4. SQLALCHEMY_DATABASE_URI
    5. Testing sqlite fallback if testing and explicitly enabled
    6. PostgreSQL URI built from individual parts
    """
    explicit_uri = _read_optional_str_env_any(
        (
            "VECTOPLAN_CHUNK_DATABASE_URL",
            "VECTOPLAN_CHUNK_SQLALCHEMY_DATABASE_URI",
            "DATABASE_URL",
            "SQLALCHEMY_DATABASE_URI",
        ),
        None,
    )

    if explicit_uri:
        return explicit_uri

    if testing and _read_bool_env("VECTOPLAN_CHUNK_TEST_USE_SQLITE", False):
        return "sqlite:///:memory:"

    return _build_postgres_uri_from_parts()


def _mask_database_uri(uri: str | None) -> str | None:
    """Mask password part of a database URI for status output."""
    if not uri:
        return None

    try:
        if "://" not in uri or "@" not in uri:
            return uri

        scheme, rest = uri.split("://", 1)
        credentials, host_part = rest.split("@", 1)

        if ":" not in credentials:
            return f"{scheme}://{credentials}@{host_part}"

        username, _password = credentials.split(":", 1)
        return f"{scheme}://{username}:***@{host_part}"
    except Exception:
        return "<masked>"


def _build_sqlalchemy_engine_options() -> dict[str, Any]:
    """
    Build SQLAlchemy engine options.

    These are conservative defaults for PostgreSQL-backed local/container usage.
    """
    pool_size = _read_int_env(
        "VECTOPLAN_CHUNK_DB_POOL_SIZE",
        default=5,
        minimum=1,
        maximum=100,
    )

    max_overflow = _read_int_env(
        "VECTOPLAN_CHUNK_DB_MAX_OVERFLOW",
        default=10,
        minimum=0,
        maximum=200,
    )

    pool_timeout = _read_int_env(
        "VECTOPLAN_CHUNK_DB_POOL_TIMEOUT",
        default=30,
        minimum=1,
        maximum=300,
    )

    pool_recycle = _read_int_env(
        "VECTOPLAN_CHUNK_DB_POOL_RECYCLE",
        default=1800,
        minimum=30,
        maximum=86400,
    )

    pool_pre_ping = _read_bool_env(
        "VECTOPLAN_CHUNK_DB_POOL_PRE_PING",
        default=True,
    )

    connect_timeout = _read_int_env(
        "VECTOPLAN_CHUNK_DB_CONNECT_TIMEOUT",
        default=10,
        minimum=1,
        maximum=300,
    )

    options: dict[str, Any] = {
        "pool_pre_ping": pool_pre_ping,
        "pool_recycle": pool_recycle,
        "pool_timeout": pool_timeout,
    }

    database_uri = _resolve_database_uri(testing=False)
    is_sqlite = database_uri.startswith("sqlite:")

    if not is_sqlite:
        options.update(
            {
                "pool_size": pool_size,
                "max_overflow": max_overflow,
                "connect_args": {
                    "connect_timeout": connect_timeout,
                },
            }
        )

    return options


# -----------------------------------------------------------------------------
# Base configuration
# -----------------------------------------------------------------------------

class BaseConfig:
    """
    Shared configuration for all environments.
    """

    # -------------------------------------------------------------------------
    # Service metadata
    # -------------------------------------------------------------------------

    SERVICE_NAME = _read_str_env_any(
        (
            "VECTOPLAN_CHUNK_SERVICE_NAME",
            "VECTOPLAN_SERVICE_NAME",
            "SERVICE_NAME",
            "APP_NAME",
        ),
        DEFAULT_SERVICE_NAME,
    )

    APP_NAME = _read_str_env_any(
        (
            "VECTOPLAN_CHUNK_APP_NAME",
            "VECTOPLAN_CHUNK_SERVICE_NAME",
            "APP_NAME",
        ),
        DEFAULT_SERVICE_NAME,
    )

    APP_DISPLAY_NAME = _read_str_env_any(
        (
            "VECTOPLAN_CHUNK_APP_DISPLAY_NAME",
            "VECTOPLAN_CHUNK_DISPLAY_NAME",
            "APP_DISPLAY_NAME",
        ),
        DEFAULT_APP_DISPLAY_NAME,
    )

    APP_ENV = _read_str_env_any(
        (
            "VECTOPLAN_CHUNK_ENV",
            "VECTOPLAN_EDITOR_ENV",
            "APP_ENV",
            "FLASK_ENV",
        ),
        "development",
    )

    VECTOPLAN_SERVICE_NAME = SERVICE_NAME

    VECTOPLAN_EXTENSION_NAMESPACE = _read_str_env_any(
        (
            "VECTOPLAN_EXTENSION_NAMESPACE",
            "VECTOPLAN_CHUNK_EXTENSION_NAMESPACE",
            "SERVICE_EXTENSION_NAMESPACE",
            "ROUTES_EXTENSION_NAMESPACE",
        ),
        DEFAULT_EXTENSION_NAMESPACE,
    )

    SERVICE_EXTENSION_NAMESPACE = VECTOPLAN_EXTENSION_NAMESPACE
    ROUTES_EXTENSION_NAMESPACE = VECTOPLAN_EXTENSION_NAMESPACE

    SERVICE_VERSION = _read_str_env_any(
        (
            "VECTOPLAN_CHUNK_VERSION",
            "SERVICE_VERSION",
            "APP_VERSION",
        ),
        "0.1.0",
    )

    # -------------------------------------------------------------------------
    # Flask base config
    # -------------------------------------------------------------------------

    SECRET_KEY = _read_str_env_any(
        (
            "VECTOPLAN_CHUNK_SECRET_KEY",
            "VECTOPLAN_EDITOR_SECRET_KEY",
            "SECRET_KEY",
        ),
        "dev-secret-key-change-me",
    )

    DEBUG = _read_bool_env_any(
        (
            "VECTOPLAN_CHUNK_DEBUG",
            "VECTOPLAN_EDITOR_DEBUG",
            "DEBUG",
            "FLASK_DEBUG",
        ),
        False,
    )

    TESTING = _read_bool_env_any(
        (
            "VECTOPLAN_CHUNK_TESTING",
            "VECTOPLAN_EDITOR_TESTING",
            "TESTING",
        ),
        False,
    )

    TEMPLATES_AUTO_RELOAD = _read_bool_env_any(
        (
            "VECTOPLAN_CHUNK_TEMPLATES_AUTO_RELOAD",
            "VECTOPLAN_EDITOR_TEMPLATES_AUTO_RELOAD",
        ),
        True,
    )

    EXPLAIN_TEMPLATE_LOADING = _read_bool_env_any(
        (
            "VECTOPLAN_CHUNK_EXPLAIN_TEMPLATE_LOADING",
            "VECTOPLAN_EDITOR_EXPLAIN_TEMPLATE_LOADING",
        ),
        False,
    )

    SEND_FILE_MAX_AGE_DEFAULT = _read_int_env_any(
        (
            "VECTOPLAN_CHUNK_SEND_FILE_MAX_AGE_DEFAULT",
            "VECTOPLAN_EDITOR_SEND_FILE_MAX_AGE_DEFAULT",
        ),
        default=0,
        minimum=0,
    )

    PREFERRED_URL_SCHEME = _read_str_env_any(
        (
            "VECTOPLAN_CHUNK_PREFERRED_URL_SCHEME",
            "VECTOPLAN_EDITOR_PREFERRED_URL_SCHEME",
        ),
        "http",
    )

    SERVER_NAME = _read_optional_str_env_any(
        (
            "VECTOPLAN_CHUNK_SERVER_NAME",
            "VECTOPLAN_EDITOR_SERVER_NAME",
        ),
        None,
    )

    APPLICATION_ROOT = _read_str_env_any(
        (
            "VECTOPLAN_CHUNK_APPLICATION_ROOT",
            "VECTOPLAN_EDITOR_APPLICATION_ROOT",
        ),
        "/",
    )

    MAX_CONTENT_LENGTH = _read_int_env_any(
        (
            "VECTOPLAN_CHUNK_MAX_CONTENT_LENGTH",
            "VECTOPLAN_EDITOR_MAX_CONTENT_LENGTH",
        ),
        default=32 * 1024 * 1024,
        minimum=1024,
    )

    JSON_SORT_KEYS = False
    JSONIFY_PRETTYPRINT_REGULAR = True

    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = _read_bool_env_any(
        (
            "VECTOPLAN_CHUNK_SESSION_COOKIE_SECURE",
            "VECTOPLAN_EDITOR_SESSION_COOKIE_SECURE",
        ),
        False,
    )

    # -------------------------------------------------------------------------
    # Service paths
    # -------------------------------------------------------------------------

    SERVICE_ROOT = SERVICE_ROOT
    BOOTSTRAP_ROOT = _build_path("bootstrap")
    ROUTES_ROOT = _build_path("routes")
    TEMPLATES_ROOT = _build_path("templates")
    STATIC_ROOT = _build_path("static")
    FRONTEND_ROOT = _build_path("frontend")
    SRC_ROOT = _build_path("src")
    MODELS_ROOT = _build_path("models")
    TESTS_ROOT = _build_path("tests")
    MIGRATIONS_ROOT = _build_path("migrations")

    WORLD_SRC_ROOT = _build_path("src", "world")
    WORLD_STATE_SRC_ROOT = _build_path("src", "world_state")
    FLAT_WORLD_ROOT = _build_path("src", "world", "flat")

    # -------------------------------------------------------------------------
    # Database / SQLAlchemy / Migration config
    # -------------------------------------------------------------------------

    DATABASE_URL = _resolve_database_uri(testing=False)
    SQLALCHEMY_DATABASE_URI = DATABASE_URL
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ECHO = _read_bool_env("VECTOPLAN_CHUNK_SQLALCHEMY_ECHO", False)
    SQLALCHEMY_RECORD_QUERIES = _read_bool_env(
        "VECTOPLAN_CHUNK_SQLALCHEMY_RECORD_QUERIES",
        False,
    )
    SQLALCHEMY_ENGINE_OPTIONS = _build_sqlalchemy_engine_options()

    VECTOPLAN_CHUNK_DATABASE_URL_MASKED = _mask_database_uri(DATABASE_URL)
    VECTOPLAN_CHUNK_DB_CHECK_ON_STARTUP = _read_bool_env(
        "VECTOPLAN_CHUNK_DB_CHECK_ON_STARTUP",
        False,
    )
    VECTOPLAN_CHUNK_DB_REQUIRE_ON_STARTUP = _read_bool_env(
        "VECTOPLAN_CHUNK_DB_REQUIRE_ON_STARTUP",
        True,
    )

    VECTOPLAN_CHUNK_REQUIRE_MIGRATIONS = _read_bool_env(
        "VECTOPLAN_CHUNK_REQUIRE_MIGRATIONS",
        False,
    )
    VECTOPLAN_CHUNK_AUTO_CREATE_ALL = _read_bool_env(
        "VECTOPLAN_CHUNK_AUTO_CREATE_ALL",
        False,
    )
    VECTOPLAN_CHUNK_AUTO_SEED_DEFAULTS = _read_bool_env(
        "VECTOPLAN_CHUNK_AUTO_SEED_DEFAULTS",
        True,
    )
    VECTOPLAN_CHUNK_SEED_DEBUG_BLOCKS = _read_bool_env(
        "VECTOPLAN_CHUNK_SEED_DEBUG_BLOCKS",
        True,
    )
    VECTOPLAN_CHUNK_SEED_DEV_PROJECT = _read_bool_env(
        "VECTOPLAN_CHUNK_SEED_DEV_PROJECT",
        True,
    )

    # -------------------------------------------------------------------------
    # Chunk-service flags
    # -------------------------------------------------------------------------

    VECTOPLAN_CHUNK_CONFIG = _read_str_env_any(
        (
            "VECTOPLAN_CHUNK_CONFIG",
            "VECTOPLAN_EDITOR_CONFIG",
            "APP_CONFIG",
        ),
        "development",
    )

    VECTOPLAN_CHUNK_DEBUG = DEBUG

    VECTOPLAN_CHUNK_ENABLE_DEV_ROUTES = _read_bool_env(
        "VECTOPLAN_CHUNK_ENABLE_DEV_ROUTES",
        True,
    )

    VECTOPLAN_CHUNK_ENABLE_LEGACY_ROUTES = _read_bool_env(
        "VECTOPLAN_CHUNK_ENABLE_LEGACY_ROUTES",
        True,
    )

    VECTOPLAN_CHUNK_ROUTE_DEBUG_ERRORS = _read_bool_env(
        "VECTOPLAN_CHUNK_ROUTE_DEBUG_ERRORS",
        DEBUG,
    )

    VECTOPLAN_CHUNK_API_PREFIX = _read_str_env(
        "VECTOPLAN_CHUNK_API_PREFIX",
        "",
    )

    VECTOPLAN_CHUNK_ROUTE_MAX_BATCH_CHUNKS = _read_int_env(
        "VECTOPLAN_CHUNK_ROUTE_MAX_BATCH_CHUNKS",
        default=256,
        minimum=1,
        maximum=4096,
    )

    VECTOPLAN_CHUNK_MAX_BATCH_CHUNKS = _read_int_env(
        "VECTOPLAN_CHUNK_MAX_BATCH_CHUNKS",
        default=VECTOPLAN_CHUNK_ROUTE_MAX_BATCH_CHUNKS,
        minimum=1,
        maximum=4096,
    )

    VECTOPLAN_CHUNK_MAX_COMMAND_AFFECTED_CELLS = _read_int_env(
        "VECTOPLAN_CHUNK_MAX_COMMAND_AFFECTED_CELLS",
        default=65536,
        minimum=1,
    )

    VECTOPLAN_CHUNK_MAX_OBJECT_SIZE_X = _read_int_env(
        "VECTOPLAN_CHUNK_MAX_OBJECT_SIZE_X",
        default=256,
        minimum=1,
    )

    VECTOPLAN_CHUNK_MAX_OBJECT_SIZE_Y = _read_int_env(
        "VECTOPLAN_CHUNK_MAX_OBJECT_SIZE_Y",
        default=256,
        minimum=1,
    )

    VECTOPLAN_CHUNK_MAX_OBJECT_SIZE_Z = _read_int_env(
        "VECTOPLAN_CHUNK_MAX_OBJECT_SIZE_Z",
        default=256,
        minimum=1,
    )

    # -------------------------------------------------------------------------
    # World-state defaults
    # -------------------------------------------------------------------------

    VECTOPLAN_CHUNK_DEFAULT_PROJECT_ID = _read_str_env(
        "VECTOPLAN_CHUNK_DEFAULT_PROJECT_ID",
        DEFAULT_PROJECT_ID,
    )

    VECTOPLAN_CHUNK_DEFAULT_PROJECT_SLUG = _read_str_env(
        "VECTOPLAN_CHUNK_DEFAULT_PROJECT_SLUG",
        DEFAULT_PROJECT_ID,
    )

    VECTOPLAN_CHUNK_DEFAULT_PROJECT_NAME = _read_str_env(
        "VECTOPLAN_CHUNK_DEFAULT_PROJECT_NAME",
        "Dev Project",
    )

    VECTOPLAN_CHUNK_DEFAULT_UNIVERSE_ID = _read_str_env(
        "VECTOPLAN_CHUNK_DEFAULT_UNIVERSE_ID",
        DEFAULT_UNIVERSE_ID,
    )

    VECTOPLAN_CHUNK_DEFAULT_UNIVERSE_SLUG = _read_str_env(
        "VECTOPLAN_CHUNK_DEFAULT_UNIVERSE_SLUG",
        DEFAULT_UNIVERSE_ID,
    )

    VECTOPLAN_CHUNK_DEFAULT_UNIVERSE_NAME = _read_str_env(
        "VECTOPLAN_CHUNK_DEFAULT_UNIVERSE_NAME",
        "Dev Universe",
    )

    VECTOPLAN_CHUNK_DEFAULT_INSTANCE_WORLD_ID = _read_str_env(
        "VECTOPLAN_CHUNK_DEFAULT_INSTANCE_WORLD_ID",
        DEFAULT_INSTANCE_WORLD_ID,
    )

    VECTOPLAN_CHUNK_DEFAULT_INSTANCE_WORLD_SLUG = _read_str_env(
        "VECTOPLAN_CHUNK_DEFAULT_INSTANCE_WORLD_SLUG",
        "spawn",
    )

    VECTOPLAN_CHUNK_DEFAULT_INSTANCE_WORLD_NAME = _read_str_env(
        "VECTOPLAN_CHUNK_DEFAULT_INSTANCE_WORLD_NAME",
        "Flat Spawn World",
    )

    # Legacy provider default for /world-test and provider layer.
    VECTOPLAN_CHUNK_DEFAULT_WORLD_ID = _read_str_env(
        "VECTOPLAN_CHUNK_DEFAULT_WORLD_ID",
        DEFAULT_PROVIDER_WORLD_ID,
    )

    VECTOPLAN_CHUNK_DEFAULT_WORLD_TEMPLATE_ID = _read_str_env(
        "VECTOPLAN_CHUNK_DEFAULT_WORLD_TEMPLATE_ID",
        DEFAULT_WORLD_TEMPLATE_ID,
    )

    VECTOPLAN_CHUNK_DEFAULT_PROVIDER_WORLD_ID = _read_str_env(
        "VECTOPLAN_CHUNK_DEFAULT_PROVIDER_WORLD_ID",
        DEFAULT_PROVIDER_WORLD_ID,
    )

    VECTOPLAN_CHUNK_DEFAULT_PROVIDER_ID = _read_str_env(
        "VECTOPLAN_CHUNK_DEFAULT_PROVIDER_ID",
        DEFAULT_PROVIDER_ID,
    )

    VECTOPLAN_CHUNK_DEFAULT_WORLD_TYPE = _read_str_env(
        "VECTOPLAN_CHUNK_DEFAULT_WORLD_TYPE",
        "runtime-world",
    )

    VECTOPLAN_CHUNK_DEFAULT_WORLD_ROLE = _read_str_env(
        "VECTOPLAN_CHUNK_DEFAULT_WORLD_ROLE",
        "default_spawn",
    )

    VECTOPLAN_CHUNK_DEFAULT_WORLD_SCOPE = _read_str_env(
        "VECTOPLAN_CHUNK_DEFAULT_WORLD_SCOPE",
        "project",
    )

    VECTOPLAN_CHUNK_DEFAULT_WORLD_OWNER_TYPE = _read_str_env(
        "VECTOPLAN_CHUNK_DEFAULT_WORLD_OWNER_TYPE",
        "project",
    )

    # -------------------------------------------------------------------------
    # Flat/generator defaults
    # -------------------------------------------------------------------------

    VECTOPLAN_CHUNK_DEFAULT_GENERATOR_TYPE = _read_str_env(
        "VECTOPLAN_CHUNK_DEFAULT_GENERATOR_TYPE",
        DEFAULT_GENERATOR_TYPE,
    )

    VECTOPLAN_CHUNK_DEFAULT_GENERATOR_VERSION = _read_str_env(
        "VECTOPLAN_CHUNK_DEFAULT_GENERATOR_VERSION",
        DEFAULT_GENERATOR_VERSION,
    )

    VECTOPLAN_CHUNK_DEFAULT_PROJECTION_TYPE = _read_str_env(
        "VECTOPLAN_CHUNK_DEFAULT_PROJECTION_TYPE",
        DEFAULT_PROJECTION_TYPE,
    )

    VECTOPLAN_CHUNK_DEFAULT_TOPOLOGY_TYPE = _read_str_env(
        "VECTOPLAN_CHUNK_DEFAULT_TOPOLOGY_TYPE",
        DEFAULT_TOPOLOGY_TYPE,
    )

    VECTOPLAN_CHUNK_DEFAULT_COORDINATE_SYSTEM = _read_str_env(
        "VECTOPLAN_CHUNK_DEFAULT_COORDINATE_SYSTEM",
        DEFAULT_COORDINATE_SYSTEM,
    )

    VECTOPLAN_CHUNK_DEFAULT_CHUNK_SIZE = _read_int_env(
        "VECTOPLAN_CHUNK_DEFAULT_CHUNK_SIZE",
        default=DEFAULT_CHUNK_SIZE,
        minimum=1,
        maximum=256,
    )

    VECTOPLAN_CHUNK_DEFAULT_CELL_SIZE = _read_float_env(
        "VECTOPLAN_CHUNK_DEFAULT_CELL_SIZE",
        default=DEFAULT_CELL_SIZE,
        minimum=0.0001,
    )

    VECTOPLAN_CHUNK_DEFAULT_SURFACE_Y = _read_int_env(
        "VECTOPLAN_CHUNK_DEFAULT_SURFACE_Y",
        default=DEFAULT_SURFACE_Y,
    )

    VECTOPLAN_CHUNK_DEFAULT_MIN_Y = _read_int_env(
        "VECTOPLAN_CHUNK_DEFAULT_MIN_Y",
        default=DEFAULT_MIN_Y,
    )

    VECTOPLAN_CHUNK_DEFAULT_MAX_Y = _read_int_env(
        "VECTOPLAN_CHUNK_DEFAULT_MAX_Y",
        default=DEFAULT_MAX_Y,
    )

    VECTOPLAN_CHUNK_DEFAULT_SEED = _read_str_env(
        "VECTOPLAN_CHUNK_DEFAULT_SEED",
        "dev-seed",
    )

    VECTOPLAN_CHUNK_DEFAULT_BLOCK_REGISTRY_ID = _read_str_env(
        "VECTOPLAN_CHUNK_DEFAULT_BLOCK_REGISTRY_ID",
        DEFAULT_BLOCK_REGISTRY_ID,
    )

    VECTOPLAN_CHUNK_DEFAULT_BLOCK_REGISTRY_VERSION = _read_str_env(
        "VECTOPLAN_CHUNK_DEFAULT_BLOCK_REGISTRY_VERSION",
        DEFAULT_BLOCK_REGISTRY_VERSION,
    )

    # -------------------------------------------------------------------------
    # Spawn defaults
    # -------------------------------------------------------------------------

    VECTOPLAN_CHUNK_DEFAULT_SPAWN_X = _read_int_env(
        "VECTOPLAN_CHUNK_DEFAULT_SPAWN_X",
        default=0,
    )

    VECTOPLAN_CHUNK_DEFAULT_SPAWN_Y = _read_int_env(
        "VECTOPLAN_CHUNK_DEFAULT_SPAWN_Y",
        default=2,
    )

    VECTOPLAN_CHUNK_DEFAULT_SPAWN_Z = _read_int_env(
        "VECTOPLAN_CHUNK_DEFAULT_SPAWN_Z",
        default=0,
    )

    VECTOPLAN_CHUNK_DEFAULT_SPAWN_YAW = _read_float_env(
        "VECTOPLAN_CHUNK_DEFAULT_SPAWN_YAW",
        default=0.0,
    )

    VECTOPLAN_CHUNK_DEFAULT_SPAWN_PITCH = _read_float_env(
        "VECTOPLAN_CHUNK_DEFAULT_SPAWN_PITCH",
        default=0.0,
    )

    # -------------------------------------------------------------------------
    # Bootstrap/provider options
    # -------------------------------------------------------------------------

    VECTOPLAN_CHUNK_BOOTSTRAP_ALLOW_DEFAULT_PROJECT = _read_bool_env(
        "VECTOPLAN_CHUNK_BOOTSTRAP_ALLOW_DEFAULT_PROJECT",
        False,
    )

    VECTOPLAN_CHUNK_BOOTSTRAP_INCLUDE_PROVIDER_CHECKS = _read_bool_env(
        "VECTOPLAN_CHUNK_BOOTSTRAP_INCLUDE_PROVIDER_CHECKS",
        False,
    )

    VECTOPLAN_CHUNK_BOOTSTRAP_REQUIRE_PROVIDER_WORLDS = _read_bool_env(
        "VECTOPLAN_CHUNK_BOOTSTRAP_REQUIRE_PROVIDER_WORLDS",
        False,
    )

    VECTOPLAN_CHUNK_DISABLE_PROVIDER_ENRICHMENT = _read_bool_env(
        "VECTOPLAN_CHUNK_DISABLE_PROVIDER_ENRICHMENT",
        False,
    )

    # -------------------------------------------------------------------------
    # Legacy editor-shell config
    # -------------------------------------------------------------------------

    EDITOR_ROUTE_PATH = _read_str_env(
        "VECTOPLAN_EDITOR_ROUTE_PATH",
        "/editor",
    )

    EDITOR_TEMPLATE_NAME = "editor/index.html"

    EDITOR_PAGE_TITLE = _read_str_env(
        "VECTOPLAN_EDITOR_PAGE_TITLE",
        "VECTOPLAN Editor",
    )

    EDITOR_BRAND_NAME = _read_str_env(
        "VECTOPLAN_EDITOR_BRAND_NAME",
        "VECTOPLAN Editor",
    )

    EDITOR_STATUS_INITIAL = _read_str_env(
        "VECTOPLAN_EDITOR_STATUS_INITIAL",
        "Initialisierung...",
    )

    EDITOR_STATUS_READY = _read_str_env(
        "VECTOPLAN_EDITOR_STATUS_READY",
        "Editor Runtime gestartet",
    )

    EDITOR_VIEWPORT_PLACEHOLDER = _read_str_env(
        "VECTOPLAN_EDITOR_VIEWPORT_PLACEHOLDER",
        "3D-Viewport wird hier aufgebaut",
    )

    EDITOR_LEFT_PANEL_TITLE = _read_str_env(
        "VECTOPLAN_EDITOR_LEFT_PANEL_TITLE",
        "Werkzeuge",
    )

    EDITOR_LEFT_PANEL_TEXT = _read_str_env(
        "VECTOPLAN_EDITOR_LEFT_PANEL_TEXT",
        "Platzhalter für Tools",
    )

    EDITOR_RIGHT_PANEL_TITLE = _read_str_env(
        "VECTOPLAN_EDITOR_RIGHT_PANEL_TITLE",
        "Inspector",
    )

    EDITOR_RIGHT_PANEL_TEXT = _read_str_env(
        "VECTOPLAN_EDITOR_RIGHT_PANEL_TEXT",
        "Platzhalter für Eigenschaften",
    )

    EDITOR_HOTBAR_SLOTS = _read_int_env(
        "VECTOPLAN_EDITOR_HOTBAR_SLOTS",
        default=5,
        minimum=1,
        maximum=20,
    )

    EDITOR_MAIN_CSS_FILE = "editor/css/editor.css"
    EDITOR_MAIN_JS_FILE = "editor/js/main.js"

    # -------------------------------------------------------------------------
    # Helper methods
    # -------------------------------------------------------------------------

    @classmethod
    def get_editor_slot_labels(cls) -> list[str]:
        """Return legacy editor hotbar slot labels."""
        try:
            slot_count = int(cls.EDITOR_HOTBAR_SLOTS)
        except (TypeError, ValueError):
            slot_count = 5

        slot_count = max(1, min(slot_count, 20))
        return [str(index) for index in range(1, slot_count + 1)]

    @classmethod
    def build_editor_template_context(cls) -> dict[str, Any]:
        """Build legacy editor template context."""
        return {
            "page_title": cls.EDITOR_PAGE_TITLE,
            "brand_name": cls.EDITOR_BRAND_NAME,
            "initial_status": cls.EDITOR_STATUS_INITIAL,
            "runtime_ready_status": cls.EDITOR_STATUS_READY,
            "viewport_placeholder": cls.EDITOR_VIEWPORT_PLACEHOLDER,
            "left_panel_title": cls.EDITOR_LEFT_PANEL_TITLE,
            "left_panel_text": cls.EDITOR_LEFT_PANEL_TEXT,
            "right_panel_title": cls.EDITOR_RIGHT_PANEL_TITLE,
            "right_panel_text": cls.EDITOR_RIGHT_PANEL_TEXT,
            "hotbar_slots": cls.get_editor_slot_labels(),
            "editor_css_file": cls.EDITOR_MAIN_CSS_FILE,
            "editor_js_file": cls.EDITOR_MAIN_JS_FILE,
        }

    @classmethod
    def build_database_config(cls) -> dict[str, Any]:
        """Build database config metadata for status/debug output."""
        return {
            "databaseUrlMasked": _mask_database_uri(getattr(cls, "DATABASE_URL", None)),
            "sqlalchemyDatabaseUriMasked": _mask_database_uri(
                getattr(cls, "SQLALCHEMY_DATABASE_URI", None)
            ),
            "trackModifications": getattr(cls, "SQLALCHEMY_TRACK_MODIFICATIONS", False),
            "echo": getattr(cls, "SQLALCHEMY_ECHO", False),
            "recordQueries": getattr(cls, "SQLALCHEMY_RECORD_QUERIES", False),
            "engineOptions": {
                key: value
                for key, value in getattr(cls, "SQLALCHEMY_ENGINE_OPTIONS", {}).items()
                if key != "connect_args"
            },
            "connectArgs": {
                key: value
                for key, value in getattr(cls, "SQLALCHEMY_ENGINE_OPTIONS", {})
                .get("connect_args", {})
                .items()
            },
            "checkOnStartup": cls.VECTOPLAN_CHUNK_DB_CHECK_ON_STARTUP,
            "requireOnStartup": cls.VECTOPLAN_CHUNK_DB_REQUIRE_ON_STARTUP,
            "requireMigrations": cls.VECTOPLAN_CHUNK_REQUIRE_MIGRATIONS,
            "autoCreateAll": cls.VECTOPLAN_CHUNK_AUTO_CREATE_ALL,
            "autoSeedDefaults": cls.VECTOPLAN_CHUNK_AUTO_SEED_DEFAULTS,
            "seedDebugBlocks": cls.VECTOPLAN_CHUNK_SEED_DEBUG_BLOCKS,
            "seedDevProject": cls.VECTOPLAN_CHUNK_SEED_DEV_PROJECT,
        }

    @classmethod
    def build_world_state_defaults(cls) -> dict[str, Any]:
        """Build default project/universe/world/provider config."""
        return {
            "projectId": cls.VECTOPLAN_CHUNK_DEFAULT_PROJECT_ID,
            "projectSlug": cls.VECTOPLAN_CHUNK_DEFAULT_PROJECT_SLUG,
            "projectName": cls.VECTOPLAN_CHUNK_DEFAULT_PROJECT_NAME,
            "universeId": cls.VECTOPLAN_CHUNK_DEFAULT_UNIVERSE_ID,
            "universeSlug": cls.VECTOPLAN_CHUNK_DEFAULT_UNIVERSE_SLUG,
            "universeName": cls.VECTOPLAN_CHUNK_DEFAULT_UNIVERSE_NAME,
            "worldId": cls.VECTOPLAN_CHUNK_DEFAULT_INSTANCE_WORLD_ID,
            "worldSlug": cls.VECTOPLAN_CHUNK_DEFAULT_INSTANCE_WORLD_SLUG,
            "worldName": cls.VECTOPLAN_CHUNK_DEFAULT_INSTANCE_WORLD_NAME,
            "templateId": cls.VECTOPLAN_CHUNK_DEFAULT_WORLD_TEMPLATE_ID,
            "providerWorldId": cls.VECTOPLAN_CHUNK_DEFAULT_PROVIDER_WORLD_ID,
            "providerId": cls.VECTOPLAN_CHUNK_DEFAULT_PROVIDER_ID,
            "legacyDefaultWorldId": cls.VECTOPLAN_CHUNK_DEFAULT_WORLD_ID,
            "worldType": cls.VECTOPLAN_CHUNK_DEFAULT_WORLD_TYPE,
            "worldRole": cls.VECTOPLAN_CHUNK_DEFAULT_WORLD_ROLE,
            "worldScope": cls.VECTOPLAN_CHUNK_DEFAULT_WORLD_SCOPE,
            "generatorType": cls.VECTOPLAN_CHUNK_DEFAULT_GENERATOR_TYPE,
            "generatorVersion": cls.VECTOPLAN_CHUNK_DEFAULT_GENERATOR_VERSION,
            "projectionType": cls.VECTOPLAN_CHUNK_DEFAULT_PROJECTION_TYPE,
            "topologyType": cls.VECTOPLAN_CHUNK_DEFAULT_TOPOLOGY_TYPE,
            "coordinateSystem": cls.VECTOPLAN_CHUNK_DEFAULT_COORDINATE_SYSTEM,
            "chunkSize": cls.VECTOPLAN_CHUNK_DEFAULT_CHUNK_SIZE,
            "cellSize": cls.VECTOPLAN_CHUNK_DEFAULT_CELL_SIZE,
            "surfaceY": cls.VECTOPLAN_CHUNK_DEFAULT_SURFACE_Y,
            "minY": cls.VECTOPLAN_CHUNK_DEFAULT_MIN_Y,
            "maxY": cls.VECTOPLAN_CHUNK_DEFAULT_MAX_Y,
            "seed": cls.VECTOPLAN_CHUNK_DEFAULT_SEED,
            "blockRegistryId": cls.VECTOPLAN_CHUNK_DEFAULT_BLOCK_REGISTRY_ID,
            "blockRegistryVersion": cls.VECTOPLAN_CHUNK_DEFAULT_BLOCK_REGISTRY_VERSION,
            "spawn": {
                "position": {
                    "x": cls.VECTOPLAN_CHUNK_DEFAULT_SPAWN_X,
                    "y": cls.VECTOPLAN_CHUNK_DEFAULT_SPAWN_Y,
                    "z": cls.VECTOPLAN_CHUNK_DEFAULT_SPAWN_Z,
                },
                "rotation": {
                    "yaw": cls.VECTOPLAN_CHUNK_DEFAULT_SPAWN_YAW,
                    "pitch": cls.VECTOPLAN_CHUNK_DEFAULT_SPAWN_PITCH,
                },
            },
        }

    @classmethod
    def build_service_status_context(cls) -> dict[str, Any]:
        """Build compact service/config metadata for health/status output."""
        return {
            "serviceName": cls.SERVICE_NAME,
            "appName": cls.APP_NAME,
            "appDisplayName": cls.APP_DISPLAY_NAME,
            "appEnv": cls.APP_ENV,
            "serviceVersion": cls.SERVICE_VERSION,
            "extensionNamespace": cls.VECTOPLAN_EXTENSION_NAMESPACE,
            "debug": cls.DEBUG,
            "testing": cls.TESTING,
            "devRoutesEnabled": cls.VECTOPLAN_CHUNK_ENABLE_DEV_ROUTES,
            "legacyRoutesEnabled": cls.VECTOPLAN_CHUNK_ENABLE_LEGACY_ROUTES,
            "projectScopedApiEnabled": True,
            "database": cls.build_database_config(),
            "worldStateDefaults": cls.build_world_state_defaults(),
        }

    @classmethod
    def validate(cls) -> list[str]:
        """
        Validate configuration.

        This intentionally returns errors instead of raising so app startup can
        decide whether to fail fast or only report status.
        """
        errors: list[str] = []

        service_name = getattr(cls, "SERVICE_NAME", None)
        if not isinstance(service_name, str) or not service_name:
            errors.append("SERVICE_NAME must be set.")

        extension_namespace = getattr(cls, "VECTOPLAN_EXTENSION_NAMESPACE", None)
        if not isinstance(extension_namespace, str) or not extension_namespace:
            errors.append("VECTOPLAN_EXTENSION_NAMESPACE must be set.")

        database_uri = getattr(cls, "SQLALCHEMY_DATABASE_URI", None)
        if not isinstance(database_uri, str) or not database_uri:
            errors.append("SQLALCHEMY_DATABASE_URI or DATABASE_URL must be set.")

        project_id = getattr(cls, "VECTOPLAN_CHUNK_DEFAULT_PROJECT_ID", None)
        if not isinstance(project_id, str) or not project_id:
            errors.append("VECTOPLAN_CHUNK_DEFAULT_PROJECT_ID must be set.")

        universe_id = getattr(cls, "VECTOPLAN_CHUNK_DEFAULT_UNIVERSE_ID", None)
        if not isinstance(universe_id, str) or not universe_id:
            errors.append("VECTOPLAN_CHUNK_DEFAULT_UNIVERSE_ID must be set.")

        instance_world_id = getattr(cls, "VECTOPLAN_CHUNK_DEFAULT_INSTANCE_WORLD_ID", None)
        if not isinstance(instance_world_id, str) or not instance_world_id:
            errors.append("VECTOPLAN_CHUNK_DEFAULT_INSTANCE_WORLD_ID must be set.")

        template_id = getattr(cls, "VECTOPLAN_CHUNK_DEFAULT_WORLD_TEMPLATE_ID", None)
        if not isinstance(template_id, str) or not template_id:
            errors.append("VECTOPLAN_CHUNK_DEFAULT_WORLD_TEMPLATE_ID must be set.")

        provider_world_id = getattr(cls, "VECTOPLAN_CHUNK_DEFAULT_PROVIDER_WORLD_ID", None)
        if not isinstance(provider_world_id, str) or not provider_world_id:
            errors.append("VECTOPLAN_CHUNK_DEFAULT_PROVIDER_WORLD_ID must be set.")

        if instance_world_id == provider_world_id:
            errors.append(
                "VECTOPLAN_CHUNK_DEFAULT_INSTANCE_WORLD_ID should not equal "
                "VECTOPLAN_CHUNK_DEFAULT_PROVIDER_WORLD_ID. Use world_spawn for "
                "the concrete project world and flat for the provider world."
            )

        chunk_size = getattr(cls, "VECTOPLAN_CHUNK_DEFAULT_CHUNK_SIZE", None)
        if not isinstance(chunk_size, int) or chunk_size <= 0:
            errors.append("VECTOPLAN_CHUNK_DEFAULT_CHUNK_SIZE must be an integer > 0.")

        cell_size = getattr(cls, "VECTOPLAN_CHUNK_DEFAULT_CELL_SIZE", None)
        if not isinstance(cell_size, (int, float)) or float(cell_size) <= 0:
            errors.append("VECTOPLAN_CHUNK_DEFAULT_CELL_SIZE must be a number > 0.")

        min_y = getattr(cls, "VECTOPLAN_CHUNK_DEFAULT_MIN_Y", None)
        max_y = getattr(cls, "VECTOPLAN_CHUNK_DEFAULT_MAX_Y", None)
        surface_y = getattr(cls, "VECTOPLAN_CHUNK_DEFAULT_SURFACE_Y", None)

        if not all(isinstance(value, int) for value in (min_y, max_y, surface_y)):
            errors.append("VECTOPLAN_CHUNK_DEFAULT_MIN_Y/MAX_Y/SURFACE_Y must be integers.")
        else:
            if min_y > max_y:
                errors.append("VECTOPLAN_CHUNK_DEFAULT_MIN_Y must not be greater than MAX_Y.")
            if surface_y < min_y or surface_y > max_y:
                errors.append("VECTOPLAN_CHUNK_DEFAULT_SURFACE_Y must be between MIN_Y and MAX_Y.")

        route_path = getattr(cls, "EDITOR_ROUTE_PATH", None)
        if not isinstance(route_path, str) or not route_path.startswith("/"):
            errors.append("EDITOR_ROUTE_PATH must be a string starting with '/'.")

        template_name = getattr(cls, "EDITOR_TEMPLATE_NAME", None)
        if not isinstance(template_name, str) or not template_name:
            errors.append("EDITOR_TEMPLATE_NAME must be set.")

        hotbar_slots = getattr(cls, "EDITOR_HOTBAR_SLOTS", None)
        if not isinstance(hotbar_slots, int):
            errors.append("EDITOR_HOTBAR_SLOTS must be an integer.")

        for attribute_name in (
            "SERVICE_ROOT",
            "BOOTSTRAP_ROOT",
            "ROUTES_ROOT",
            "TEMPLATES_ROOT",
            "STATIC_ROOT",
            "FRONTEND_ROOT",
            "SRC_ROOT",
            "MODELS_ROOT",
            "TESTS_ROOT",
            "MIGRATIONS_ROOT",
            "WORLD_SRC_ROOT",
            "WORLD_STATE_SRC_ROOT",
            "FLAT_WORLD_ROOT",
        ):
            value = getattr(cls, attribute_name, None)
            if not isinstance(value, Path):
                errors.append(f"{attribute_name} must be pathlib.Path.")

        return errors


# -----------------------------------------------------------------------------
# Environment-specific configurations
# -----------------------------------------------------------------------------

class Config(BaseConfig):
    """Default local/development-oriented configuration."""

    APP_ENV = _read_str_env_any(
        (
            "VECTOPLAN_CHUNK_ENV",
            "VECTOPLAN_EDITOR_ENV",
            "APP_ENV",
            "FLASK_ENV",
        ),
        "development",
    )
    DEBUG = _read_bool_env_any(
        (
            "VECTOPLAN_CHUNK_DEBUG",
            "VECTOPLAN_EDITOR_DEBUG",
            "DEBUG",
            "FLASK_DEBUG",
        ),
        True,
    )
    TESTING = _read_bool_env_any(
        (
            "VECTOPLAN_CHUNK_TESTING",
            "VECTOPLAN_EDITOR_TESTING",
            "TESTING",
        ),
        False,
    )
    TEMPLATES_AUTO_RELOAD = _read_bool_env_any(
        (
            "VECTOPLAN_CHUNK_TEMPLATES_AUTO_RELOAD",
            "VECTOPLAN_EDITOR_TEMPLATES_AUTO_RELOAD",
        ),
        True,
    )


class DevelopmentConfig(BaseConfig):
    """Explicit development configuration."""

    APP_ENV = "development"
    DEBUG = True
    TESTING = False
    TEMPLATES_AUTO_RELOAD = True
    SEND_FILE_MAX_AGE_DEFAULT = _read_int_env_any(
        (
            "VECTOPLAN_CHUNK_SEND_FILE_MAX_AGE_DEFAULT",
            "VECTOPLAN_EDITOR_SEND_FILE_MAX_AGE_DEFAULT",
        ),
        default=0,
        minimum=0,
    )
    VECTOPLAN_CHUNK_ENABLE_DEV_ROUTES = True
    VECTOPLAN_CHUNK_ROUTE_DEBUG_ERRORS = _read_bool_env(
        "VECTOPLAN_CHUNK_ROUTE_DEBUG_ERRORS",
        True,
    )


class TestingConfig(BaseConfig):
    """Testing configuration."""

    APP_ENV = "testing"
    DEBUG = True
    TESTING = True
    SECRET_KEY = _read_str_env_any(
        (
            "VECTOPLAN_CHUNK_TEST_SECRET_KEY",
            "VECTOPLAN_EDITOR_TEST_SECRET_KEY",
            "TEST_SECRET_KEY",
        ),
        "test-secret-key",
    )
    TEMPLATES_AUTO_RELOAD = True
    SEND_FILE_MAX_AGE_DEFAULT = 0
    VECTOPLAN_CHUNK_ENABLE_DEV_ROUTES = True
    VECTOPLAN_CHUNK_ROUTE_DEBUG_ERRORS = True

    DATABASE_URL = _resolve_database_uri(testing=True)
    SQLALCHEMY_DATABASE_URI = DATABASE_URL
    SQLALCHEMY_ENGINE_OPTIONS = {}
    VECTOPLAN_CHUNK_DATABASE_URL_MASKED = _mask_database_uri(DATABASE_URL)

    VECTOPLAN_CHUNK_AUTO_CREATE_ALL = _read_bool_env(
        "VECTOPLAN_CHUNK_TEST_AUTO_CREATE_ALL",
        True,
    )
    VECTOPLAN_CHUNK_AUTO_SEED_DEFAULTS = _read_bool_env(
        "VECTOPLAN_CHUNK_TEST_AUTO_SEED_DEFAULTS",
        True,
    )


class ProductionConfig(BaseConfig):
    """Production-oriented configuration."""

    APP_ENV = "production"
    DEBUG = False
    TESTING = False
    TEMPLATES_AUTO_RELOAD = False
    SESSION_COOKIE_SECURE = _read_bool_env_any(
        (
            "VECTOPLAN_CHUNK_SESSION_COOKIE_SECURE",
            "VECTOPLAN_EDITOR_SESSION_COOKIE_SECURE",
        ),
        True,
    )
    SEND_FILE_MAX_AGE_DEFAULT = _read_int_env_any(
        (
            "VECTOPLAN_CHUNK_SEND_FILE_MAX_AGE_DEFAULT",
            "VECTOPLAN_EDITOR_SEND_FILE_MAX_AGE_DEFAULT",
        ),
        default=3600,
        minimum=0,
    )
    VECTOPLAN_CHUNK_ROUTE_DEBUG_ERRORS = False
    VECTOPLAN_CHUNK_BOOTSTRAP_REQUIRE_PROVIDER_WORLDS = True

    VECTOPLAN_CHUNK_AUTO_CREATE_ALL = _read_bool_env(
        "VECTOPLAN_CHUNK_AUTO_CREATE_ALL",
        False,
    )
    VECTOPLAN_CHUNK_DB_REQUIRE_ON_STARTUP = _read_bool_env(
        "VECTOPLAN_CHUNK_DB_REQUIRE_ON_STARTUP",
        True,
    )
    VECTOPLAN_CHUNK_REQUIRE_MIGRATIONS = _read_bool_env(
        "VECTOPLAN_CHUNK_REQUIRE_MIGRATIONS",
        True,
    )


CONFIG_BY_NAME: Final[dict[str, type[BaseConfig]]] = {
    "default": Config,
    "config": Config,
    "development": DevelopmentConfig,
    "dev": DevelopmentConfig,
    "local": DevelopmentConfig,
    "testing": TestingConfig,
    "test": TestingConfig,
    "production": ProductionConfig,
    "prod": ProductionConfig,
}


def get_config_class(name: str | None = None) -> type[BaseConfig]:
    """
    Return config class.

    Priority:
    1. explicit name
    2. VECTOPLAN_CHUNK_CONFIG
    3. VECTOPLAN_EDITOR_CONFIG
    4. APP_CONFIG
    5. Config
    """
    requested_name = _normalize_text(name)
    if requested_name is None:
        requested_name = _read_str_env_any(
            (
                "VECTOPLAN_CHUNK_CONFIG",
                "VECTOPLAN_EDITOR_CONFIG",
                "APP_CONFIG",
            ),
            "default",
        )

    key = requested_name.lower()
    return CONFIG_BY_NAME.get(key, Config)


__all__ = [
    "DEFAULT_SERVICE_NAME",
    "DEFAULT_APP_DISPLAY_NAME",
    "DEFAULT_EXTENSION_NAMESPACE",
    "DEFAULT_PROJECT_ID",
    "DEFAULT_UNIVERSE_ID",
    "DEFAULT_INSTANCE_WORLD_ID",
    "DEFAULT_WORLD_TEMPLATE_ID",
    "DEFAULT_PROVIDER_WORLD_ID",
    "DEFAULT_PROVIDER_ID",
    "DEFAULT_CHUNK_SIZE",
    "DEFAULT_CELL_SIZE",
    "DEFAULT_SURFACE_Y",
    "DEFAULT_MIN_Y",
    "DEFAULT_MAX_Y",
    "DEFAULT_GENERATOR_TYPE",
    "DEFAULT_GENERATOR_VERSION",
    "DEFAULT_PROJECTION_TYPE",
    "DEFAULT_TOPOLOGY_TYPE",
    "DEFAULT_COORDINATE_SYSTEM",
    "DEFAULT_BLOCK_REGISTRY_ID",
    "DEFAULT_BLOCK_REGISTRY_VERSION",
    "DEFAULT_DATABASE_DRIVER",
    "DEFAULT_DATABASE_HOST",
    "DEFAULT_DATABASE_PORT",
    "DEFAULT_DATABASE_NAME",
    "DEFAULT_DATABASE_USER",
    "DEFAULT_DATABASE_PASSWORD",
    "BaseConfig",
    "Config",
    "DevelopmentConfig",
    "TestingConfig",
    "ProductionConfig",
    "CONFIG_BY_NAME",
    "get_config_class",
]