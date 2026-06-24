# services/vectoplan-chunk/config.py
"""
Central configuration for the `vectoplan-chunk` service.

This file is configuration only.

It deliberately does not:
- open database connections,
- create tables,
- seed default data,
- run migrations,
- create projects,
- create worlds,
- call other services.

Core runtime semantics:

    App Project     = owned by vectoplan-app
    Chunk Project   = owned by vectoplan-chunk
    Universe        = container for one or more chunk worlds
    WorldInstance   = concrete editable runtime world
    Provider World  = template/generator source, e.g. flat

Default local/dev seed semantics:

    appProjectId      = optional external app project id
    chunkProjectId    = dev-project
    universeId        = dev-universe
    worldId           = world_spawn
    templateId        = flat
    providerWorldId   = flat

Production/runtime rule:

    Runtime startup is read-only.
    db.create_all() and default seeding are not part of normal Gunicorn startup.
    Schema/bootstrap/seeding must run through explicit bootstrap paths.

Future app integration rule:

    vectoplan-app creates the app project.
    vectoplan-app calls vectoplan-chunk via INTERNAL_URL.
    vectoplan-chunk creates or returns a chunk project by external app project id.
    vectoplan-app stores only returned references such as chunk_project_id,
    chunk_universe_id and chunk_world_id.
"""

from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Final
from urllib.parse import quote_plus


# -----------------------------------------------------------------------------
# Internal constants
# -----------------------------------------------------------------------------

_TRUE_VALUES: Final[set[str]] = {
    "1",
    "true",
    "t",
    "yes",
    "y",
    "on",
    "enabled",
    "enable",
}

_FALSE_VALUES: Final[set[str]] = {
    "0",
    "false",
    "f",
    "no",
    "n",
    "off",
    "disabled",
    "disable",
}

_BOOTSTRAP_MODE_ALIASES: Final[set[str]] = {
    "bootstrap",
    "db-bootstrap",
    "db_bootstrap",
    "db-init",
    "db_init",
    "init",
    "database-bootstrap",
    "database_bootstrap",
}

_CHECK_ONLY_MODE_ALIASES: Final[set[str]] = {
    "check",
    "check-only",
    "check_only",
    "db-check",
    "db_check",
    "schema-check",
    "schema_check",
    "readiness-check",
    "readiness_check",
}

_RUNTIME_MODE_ALIASES: Final[set[str]] = {
    "runtime",
    "gunicorn",
    "server",
    "serve",
    "wsgi",
    "app",
}

DEFAULT_SERVICE_NAME: Final[str] = "vectoplan-chunk"
DEFAULT_APP_DISPLAY_NAME: Final[str] = "VECTOPLAN Chunk Service"
DEFAULT_EXTENSION_NAMESPACE: Final[str] = "vectoplan_chunk"

DEFAULT_PROJECT_ID: Final[str] = "dev-project"
DEFAULT_UNIVERSE_ID: Final[str] = "dev-universe"

# Concrete editable default world.
DEFAULT_INSTANCE_WORLD_ID: Final[str] = "world_spawn"

# Template/provider source.
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

DEFAULT_PROVISIONING_SOURCE_SERVICE: Final[str] = "vectoplan-app"
DEFAULT_PROVISIONING_PROJECT_PREFIX: Final[str] = "chk_prj_"
DEFAULT_PROVISIONING_UNIVERSE_PREFIX: Final[str] = "chk_uni_"
DEFAULT_PROVISIONING_WORLD_PREFIX: Final[str] = "chk_wld_"

_SAFE_ID_RE: Final[re.Pattern[str]] = re.compile(r"[^a-zA-Z0-9_\-:.]+")
_MULTI_DASH_RE: Final[re.Pattern[str]] = re.compile(r"-+")


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


def _safe_bool(value: Any, default: bool = False) -> bool:
    """Convert value to bool."""
    if isinstance(value, bool):
        return value

    if isinstance(value, int):
        return bool(value)

    text = _normalize_text(value)
    if text is None:
        return default

    lowered = text.lower()
    if lowered in _TRUE_VALUES:
        return True

    if lowered in _FALSE_VALUES:
        return False

    return default


def _safe_identifier(value: Any, default: str) -> str:
    """
    Normalize a service identifier.

    This does not replace model validation. It avoids accidental whitespace and
    obvious path/URL characters in generated defaults.
    """
    normalized = _normalize_text(value)
    if normalized is None:
        return default

    try:
        cleaned = _SAFE_ID_RE.sub("-", normalized)
        cleaned = _MULTI_DASH_RE.sub("-", cleaned).strip("-")
    except Exception:
        return default

    return cleaned or default


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
    return _safe_bool(_safe_getenv(name), default)


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

        return _safe_bool(raw_value, default)

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


def _normalize_mode(value: Any, default: str = "runtime") -> str:
    """Normalize startup/run mode."""
    normalized = _normalize_text(value)
    if normalized is None:
        return default

    key = normalized.lower().replace("_", "-")

    if key in _BOOTSTRAP_MODE_ALIASES:
        return "db-bootstrap"

    if key in _CHECK_ONLY_MODE_ALIASES:
        return "check-only"

    if key in _RUNTIME_MODE_ALIASES:
        return "runtime"

    return key or default


def _current_startup_mode(default: str = "runtime") -> str:
    """Read current startup mode from env aliases."""
    return _normalize_mode(
        _read_optional_str_env_any(
            (
                "VECTOPLAN_CHUNK_MODE",
                "VECTOPLAN_CHUNK_STARTUP_MODE",
                "VECTOPLAN_CHUNK_RUNTIME_MODE",
                "SERVICE_STARTUP_MODE",
                "APP_STARTUP_MODE",
                "STARTUP_MODE",
            ),
            default,
        ),
        default,
    )


def _is_bootstrap_mode(value: Any) -> bool:
    """Return whether the mode is a bootstrap/init mode."""
    return _normalize_mode(value) == "db-bootstrap"


def _is_check_only_mode(value: Any) -> bool:
    """Return whether the mode is check-only."""
    return _normalize_mode(value) == "check-only"


def _is_runtime_mode(value: Any) -> bool:
    """Return whether the mode is normal runtime."""
    return _normalize_mode(value) == "runtime"


@lru_cache(maxsize=1)
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

    Complete URI env vars are resolved elsewhere. This only builds a fallback
    URI from host/user/password/db parts.
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
    2. VECTOPLAN_CHUNK_DATABASE_URI
    3. VECTOPLAN_CHUNK_SQLALCHEMY_DATABASE_URI
    4. DATABASE_URL
    5. SQLALCHEMY_DATABASE_URI
    6. Testing sqlite fallback if explicitly enabled
    7. PostgreSQL URI built from individual parts
    """
    explicit_uri = _read_optional_str_env_any(
        (
            "VECTOPLAN_CHUNK_DATABASE_URL",
            "VECTOPLAN_CHUNK_DATABASE_URI",
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

    Defaults are conservative for a local/containerized PostgreSQL service.
    """
    database_uri = _resolve_database_uri(testing=False)
    is_sqlite = database_uri.startswith("sqlite:")

    pool_pre_ping = _read_bool_env(
        "VECTOPLAN_CHUNK_DB_POOL_PRE_PING",
        default=True,
    )

    pool_recycle = _read_int_env(
        "VECTOPLAN_CHUNK_DB_POOL_RECYCLE",
        default=1800,
        minimum=30,
        maximum=86400,
    )

    pool_timeout = _read_int_env(
        "VECTOPLAN_CHUNK_DB_POOL_TIMEOUT",
        default=30,
        minimum=1,
        maximum=300,
    )

    options: dict[str, Any] = {
        "pool_pre_ping": pool_pre_ping,
        "pool_recycle": pool_recycle,
        "pool_timeout": pool_timeout,
    }

    if is_sqlite:
        return options

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

    connect_timeout = _read_int_env_any(
        (
            "VECTOPLAN_CHUNK_DB_CONNECT_TIMEOUT",
            "VECTOPLAN_CHUNK_DATABASE_CONNECT_TIMEOUT",
        ),
        default=10,
        minimum=1,
        maximum=300,
    )

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


def _resolve_template_id() -> str:
    """Resolve default template id."""
    return _safe_identifier(
        _read_str_env_any(
            (
                "VECTOPLAN_CHUNK_DEFAULT_TEMPLATE_ID",
                "VECTOPLAN_CHUNK_DEFAULT_WORLD_TEMPLATE_ID",
                "VECTOPLAN_CHUNK_PROJECT_PROVISIONING_DEFAULT_TEMPLATE_ID",
            ),
            DEFAULT_WORLD_TEMPLATE_ID,
        ),
        DEFAULT_WORLD_TEMPLATE_ID,
    )


def _resolve_provider_world_id() -> str:
    """Resolve provider world id."""
    return _safe_identifier(
        _read_str_env_any(
            (
                "VECTOPLAN_CHUNK_DEFAULT_PROVIDER_WORLD_ID",
                "VECTOPLAN_CHUNK_PROVIDER_WORLD_ID",
                "VECTOPLAN_CHUNK_LEGACY_PROVIDER_WORLD_ID",
            ),
            DEFAULT_PROVIDER_WORLD_ID,
        ),
        DEFAULT_PROVIDER_WORLD_ID,
    )


def _looks_like_provider_or_template_world_id(value: str | None) -> bool:
    """Return true if value looks like a provider/template id instead of instance world id."""
    if not value:
        return False

    cleaned = _safe_identifier(value, "").lower()
    if not cleaned:
        return False

    template_id = _resolve_template_id().lower()
    provider_world_id = _resolve_provider_world_id().lower()
    provider_id = _safe_identifier(
        _read_str_env("VECTOPLAN_CHUNK_DEFAULT_PROVIDER_ID", DEFAULT_PROVIDER_ID),
        DEFAULT_PROVIDER_ID,
    ).lower()

    return cleaned in {
        template_id,
        provider_world_id,
        provider_id,
        DEFAULT_WORLD_TEMPLATE_ID,
        DEFAULT_PROVIDER_WORLD_ID,
        DEFAULT_PROVIDER_ID,
    }


def _resolve_instance_world_id() -> str:
    """
    Resolve the concrete editable default world id.

    Canonical rule:
        VECTOPLAN_CHUNK_DEFAULT_WORLD_ID points to the concrete editable world.

    Defensive compatibility rule:
        If only old/default env says DEFAULT_WORLD_ID=flat, treat it as legacy
        provider/template drift and fall back to world_spawn. This prevents the
        bootstrap from creating/expecting a concrete world named "flat".
    """
    explicit_instance_value = _read_optional_str_env_any(
        (
            "VECTOPLAN_CHUNK_DEFAULT_INSTANCE_WORLD_ID",
            "VECTOPLAN_CHUNK_DEFAULT_WORLD_INSTANCE_ID",
            "VECTOPLAN_CHUNK_DEFAULT_SPAWN_WORLD_ID",
        ),
        None,
    )

    if explicit_instance_value:
        return _safe_identifier(explicit_instance_value, DEFAULT_INSTANCE_WORLD_ID)

    default_world_value = _read_optional_str_env("VECTOPLAN_CHUNK_DEFAULT_WORLD_ID", None)
    if default_world_value:
        candidate = _safe_identifier(default_world_value, DEFAULT_INSTANCE_WORLD_ID)
        if not _looks_like_provider_or_template_world_id(candidate):
            return candidate

    return DEFAULT_INSTANCE_WORLD_ID


def _resolve_provisioning_world_id(default_world_id: str) -> str:
    """Resolve app-project provisioning default world id defensively."""
    raw_value = _read_optional_str_env(
        "VECTOPLAN_CHUNK_PROJECT_PROVISIONING_DEFAULT_WORLD_ID",
        None,
    )

    if raw_value is None:
        return default_world_id

    candidate = _safe_identifier(raw_value, default_world_id)

    if _looks_like_provider_or_template_world_id(candidate):
        return default_world_id

    return candidate


def refresh_env_cache() -> None:
    """
    Clear small internal caches.

    Config class attributes are evaluated at import time. This helper exists for
    diagnostics/tests that reload the module or call path helpers repeatedly.
    """
    try:
        _resolve_service_root.cache_clear()
    except Exception:
        pass


# -----------------------------------------------------------------------------
# Base configuration
# -----------------------------------------------------------------------------

class BaseConfig:
    """
    Shared configuration for all environments.

    Runtime defaults are intentionally read-only. Bootstrap modes override the
    relevant flags explicitly.
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
        "0.3.0",
    )

    # -------------------------------------------------------------------------
    # Runtime/startup mode
    # -------------------------------------------------------------------------

    VECTOPLAN_CHUNK_MODE = _current_startup_mode("runtime")
    VECTOPLAN_CHUNK_STARTUP_MODE = VECTOPLAN_CHUNK_MODE

    VECTOPLAN_CHUNK_RUNTIME_MODE = _normalize_mode(
        _read_optional_str_env(
            "VECTOPLAN_CHUNK_RUNTIME_MODE",
            VECTOPLAN_CHUNK_STARTUP_MODE,
        ),
        VECTOPLAN_CHUNK_STARTUP_MODE,
    )

    VECTOPLAN_CHUNK_RUN_MODE = _read_str_env_any(
        (
            "VECTOPLAN_CHUNK_RUN_MODE",
            "RUN_MODE",
        ),
        "gunicorn",
    )

    VECTOPLAN_CHUNK_RUNTIME_IS_READ_ONLY = _read_bool_env(
        "VECTOPLAN_CHUNK_RUNTIME_IS_READ_ONLY",
        True,
    )

    VECTOPLAN_CHUNK_ALLOW_RUNTIME_DB_MUTATIONS = _read_bool_env(
        "VECTOPLAN_CHUNK_ALLOW_RUNTIME_DB_MUTATIONS",
        False,
    )

    VECTOPLAN_CHUNK_RUN_STARTUP_HOOKS = _read_bool_env(
        "VECTOPLAN_CHUNK_RUN_STARTUP_HOOKS",
        True,
    )

    # -------------------------------------------------------------------------
    # Flask base config
    # -------------------------------------------------------------------------

    SECRET_KEY = _read_str_env_any(
        (
            "VECTOPLAN_CHUNK_SECRET_KEY",
            "SECRET_KEY",
        ),
        "dev-secret-key-change-me",
    )

    DEBUG = _read_bool_env_any(
        (
            "VECTOPLAN_CHUNK_DEBUG",
            "DEBUG",
            "FLASK_DEBUG",
        ),
        False,
    )

    TESTING = _read_bool_env_any(
        (
            "VECTOPLAN_CHUNK_TESTING",
            "TESTING",
        ),
        False,
    )

    TEMPLATES_AUTO_RELOAD = _read_bool_env(
        "VECTOPLAN_CHUNK_TEMPLATES_AUTO_RELOAD",
        True,
    )

    EXPLAIN_TEMPLATE_LOADING = _read_bool_env(
        "VECTOPLAN_CHUNK_EXPLAIN_TEMPLATE_LOADING",
        False,
    )

    SEND_FILE_MAX_AGE_DEFAULT = _read_int_env(
        "VECTOPLAN_CHUNK_SEND_FILE_MAX_AGE_DEFAULT",
        default=0,
        minimum=0,
    )

    PREFERRED_URL_SCHEME = _read_str_env(
        "VECTOPLAN_CHUNK_PREFERRED_URL_SCHEME",
        "http",
    )

    SERVER_NAME = _read_optional_str_env(
        "VECTOPLAN_CHUNK_SERVER_NAME",
        None,
    )

    APPLICATION_ROOT = _read_str_env(
        "VECTOPLAN_CHUNK_APPLICATION_ROOT",
        "/",
    )

    MAX_CONTENT_LENGTH = _read_int_env(
        "VECTOPLAN_CHUNK_MAX_CONTENT_LENGTH",
        default=32 * 1024 * 1024,
        minimum=1024,
    )

    JSON_SORT_KEYS = False
    JSONIFY_PRETTYPRINT_REGULAR = True

    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = _read_bool_env(
        "VECTOPLAN_CHUNK_SESSION_COOKIE_SECURE",
        False,
    )

    # -------------------------------------------------------------------------
    # Service URLs
    # -------------------------------------------------------------------------

    VECTOPLAN_CHUNK_PUBLIC_URL = _read_str_env(
        "VECTOPLAN_CHUNK_PUBLIC_URL",
        "http://localhost:5102",
    )

    VECTOPLAN_CHUNK_PUBLIC_BASE_URL = _read_str_env_any(
        (
            "VECTOPLAN_CHUNK_PUBLIC_BASE_URL",
            "VECTOPLAN_CHUNK_PUBLIC_URL",
        ),
        "http://localhost:5102",
    )

    VECTOPLAN_APP_PUBLIC_URL = _read_str_env(
        "VECTOPLAN_APP_PUBLIC_URL",
        "http://localhost:5103",
    )

    VECTOPLAN_APP_INTERNAL_URL = _read_optional_str_env(
        "VECTOPLAN_APP_INTERNAL_URL",
        None,
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
    BOOTSTRAP_SCRIPT = _build_path("scripts", "bootstrap_db.py")

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

    VECTOPLAN_CHUNK_DB_HOST = _read_str_env_any(
        (
            "VECTOPLAN_CHUNK_DB_HOST",
            "VECTOPLAN_CHUNK_POSTGRES_HOST",
            "POSTGRES_HOST",
            "DB_HOST",
        ),
        DEFAULT_DATABASE_HOST,
    )

    VECTOPLAN_CHUNK_DB_PORT = _read_int_env_any(
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

    VECTOPLAN_CHUNK_DB_NAME = _read_str_env_any(
        (
            "VECTOPLAN_CHUNK_DB_NAME",
            "VECTOPLAN_CHUNK_POSTGRES_DB",
            "POSTGRES_DB",
            "DB_NAME",
        ),
        DEFAULT_DATABASE_NAME,
    )

    VECTOPLAN_CHUNK_DB_USER = _read_str_env_any(
        (
            "VECTOPLAN_CHUNK_DB_USER",
            "VECTOPLAN_CHUNK_POSTGRES_USER",
            "POSTGRES_USER",
            "DB_USER",
        ),
        DEFAULT_DATABASE_USER,
    )

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

    VECTOPLAN_CHUNK_MIGRATIONS_DIRECTORY = _read_str_env_any(
        (
            "VECTOPLAN_CHUNK_MIGRATIONS_DIRECTORY",
            "ALEMBIC_MIGRATIONS_DIRECTORY",
            "MIGRATIONS_DIRECTORY",
        ),
        "migrations",
    )

    # Runtime defaults are deliberately non-mutating.
    VECTOPLAN_CHUNK_DB_BOOTSTRAP_ENABLED = _read_bool_env(
        "VECTOPLAN_CHUNK_DB_BOOTSTRAP_ENABLED",
        False,
    )

    VECTOPLAN_CHUNK_AUTO_CREATE_ALL = _read_bool_env(
        "VECTOPLAN_CHUNK_AUTO_CREATE_ALL",
        False,
    )

    VECTOPLAN_CHUNK_AUTO_SEED_DEFAULTS = _read_bool_env(
        "VECTOPLAN_CHUNK_AUTO_SEED_DEFAULTS",
        False,
    )

    VECTOPLAN_CHUNK_SEED_DEBUG_BLOCKS = _read_bool_env(
        "VECTOPLAN_CHUNK_SEED_DEBUG_BLOCKS",
        False,
    )

    VECTOPLAN_CHUNK_SEED_DEV_PROJECT = _read_bool_env(
        "VECTOPLAN_CHUNK_SEED_DEV_PROJECT",
        False,
    )

    VECTOPLAN_CHUNK_DB_BOOTSTRAP_CREATE_ALL = _read_bool_env(
        "VECTOPLAN_CHUNK_DB_BOOTSTRAP_CREATE_ALL",
        VECTOPLAN_CHUNK_AUTO_CREATE_ALL,
    )

    VECTOPLAN_CHUNK_DB_BOOTSTRAP_SEED_DEFAULTS = _read_bool_env(
        "VECTOPLAN_CHUNK_DB_BOOTSTRAP_SEED_DEFAULTS",
        VECTOPLAN_CHUNK_AUTO_SEED_DEFAULTS,
    )

    VECTOPLAN_CHUNK_DB_BOOTSTRAP_SEED_DEBUG_BLOCKS = _read_bool_env(
        "VECTOPLAN_CHUNK_DB_BOOTSTRAP_SEED_DEBUG_BLOCKS",
        VECTOPLAN_CHUNK_SEED_DEBUG_BLOCKS,
    )

    VECTOPLAN_CHUNK_DB_BOOTSTRAP_SEED_DEV_PROJECT = _read_bool_env(
        "VECTOPLAN_CHUNK_DB_BOOTSTRAP_SEED_DEV_PROJECT",
        VECTOPLAN_CHUNK_SEED_DEV_PROJECT,
    )

    VECTOPLAN_CHUNK_BOOTSTRAP_REPAIR_MISSING_COLUMNS = _read_bool_env(
        "VECTOPLAN_CHUNK_BOOTSTRAP_REPAIR_MISSING_COLUMNS",
        False,
    )

    VECTOPLAN_CHUNK_DB_BOOTSTRAP_FAIL_ON_ERROR = _read_bool_env(
        "VECTOPLAN_CHUNK_DB_BOOTSTRAP_FAIL_ON_ERROR",
        True,
    )

    VECTOPLAN_CHUNK_SEED_ON_EMPTY_ONLY = _read_bool_env(
        "VECTOPLAN_CHUNK_SEED_ON_EMPTY_ONLY",
        True,
    )

    VECTOPLAN_CHUNK_BOOTSTRAP_USE_ADVISORY_LOCK = _read_bool_env_any(
        (
            "VECTOPLAN_CHUNK_BOOTSTRAP_USE_ADVISORY_LOCK",
            "VECTOPLAN_CHUNK_DB_BOOTSTRAP_ADVISORY_LOCKS",
        ),
        True,
    )

    VECTOPLAN_CHUNK_BOOTSTRAP_ADVISORY_LOCK_KEY = _read_int_env(
        "VECTOPLAN_CHUNK_BOOTSTRAP_ADVISORY_LOCK_KEY",
        default=5102001,
        minimum=1,
    )

    VECTOPLAN_CHUNK_BOOTSTRAP_CONNECT_MAX_ATTEMPTS = _read_int_env_any(
        (
            "VECTOPLAN_CHUNK_BOOTSTRAP_CONNECT_MAX_ATTEMPTS",
            "VECTOPLAN_CHUNK_INIT_MAX_ATTEMPTS",
        ),
        default=20,
        minimum=1,
        maximum=500,
    )

    VECTOPLAN_CHUNK_BOOTSTRAP_CONNECT_RETRY_SECONDS = _read_int_env_any(
        (
            "VECTOPLAN_CHUNK_BOOTSTRAP_CONNECT_RETRY_SECONDS",
            "VECTOPLAN_CHUNK_INIT_RETRY_SECONDS",
        ),
        default=2,
        minimum=1,
        maximum=300,
    )

    VECTOPLAN_CHUNK_BOOTSTRAP_CONNECT_TIMEOUT_SECONDS = _read_int_env_any(
        (
            "VECTOPLAN_CHUNK_BOOTSTRAP_CONNECT_TIMEOUT_SECONDS",
            "VECTOPLAN_CHUNK_INIT_CONNECT_TIMEOUT_SECONDS",
        ),
        default=10,
        minimum=1,
        maximum=300,
    )

    # -------------------------------------------------------------------------
    # Chunk-service route flags
    # -------------------------------------------------------------------------

    VECTOPLAN_CHUNK_CONFIG = _read_str_env_any(
        (
            "VECTOPLAN_CHUNK_CONFIG",
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

    VECTOPLAN_CHUNK_DEFAULT_PROJECT_ID = _safe_identifier(
        _read_str_env(
            "VECTOPLAN_CHUNK_DEFAULT_PROJECT_ID",
            DEFAULT_PROJECT_ID,
        ),
        DEFAULT_PROJECT_ID,
    )

    VECTOPLAN_CHUNK_DEFAULT_PROJECT_SLUG = _safe_identifier(
        _read_str_env(
            "VECTOPLAN_CHUNK_DEFAULT_PROJECT_SLUG",
            VECTOPLAN_CHUNK_DEFAULT_PROJECT_ID,
        ),
        VECTOPLAN_CHUNK_DEFAULT_PROJECT_ID,
    )

    VECTOPLAN_CHUNK_DEFAULT_PROJECT_NAME = _read_str_env(
        "VECTOPLAN_CHUNK_DEFAULT_PROJECT_NAME",
        "Dev Project",
    )

    VECTOPLAN_CHUNK_DEFAULT_UNIVERSE_ID = _safe_identifier(
        _read_str_env(
            "VECTOPLAN_CHUNK_DEFAULT_UNIVERSE_ID",
            DEFAULT_UNIVERSE_ID,
        ),
        DEFAULT_UNIVERSE_ID,
    )

    VECTOPLAN_CHUNK_DEFAULT_UNIVERSE_SLUG = _safe_identifier(
        _read_str_env(
            "VECTOPLAN_CHUNK_DEFAULT_UNIVERSE_SLUG",
            VECTOPLAN_CHUNK_DEFAULT_UNIVERSE_ID,
        ),
        VECTOPLAN_CHUNK_DEFAULT_UNIVERSE_ID,
    )

    VECTOPLAN_CHUNK_DEFAULT_UNIVERSE_NAME = _read_str_env(
        "VECTOPLAN_CHUNK_DEFAULT_UNIVERSE_NAME",
        "Dev Universe",
    )

    VECTOPLAN_CHUNK_DEFAULT_INSTANCE_WORLD_ID = _resolve_instance_world_id()

    # Canonical alias: default world id = concrete editable world id.
    VECTOPLAN_CHUNK_DEFAULT_WORLD_ID = VECTOPLAN_CHUNK_DEFAULT_INSTANCE_WORLD_ID

    VECTOPLAN_CHUNK_DEFAULT_INSTANCE_WORLD_SLUG = _safe_identifier(
        _read_str_env(
            "VECTOPLAN_CHUNK_DEFAULT_INSTANCE_WORLD_SLUG",
            "spawn",
        ),
        "spawn",
    )

    VECTOPLAN_CHUNK_DEFAULT_INSTANCE_WORLD_NAME = _read_str_env(
        "VECTOPLAN_CHUNK_DEFAULT_INSTANCE_WORLD_NAME",
        "Flat Spawn World",
    )

    VECTOPLAN_CHUNK_DEFAULT_TEMPLATE_ID = _resolve_template_id()
    VECTOPLAN_CHUNK_DEFAULT_WORLD_TEMPLATE_ID = VECTOPLAN_CHUNK_DEFAULT_TEMPLATE_ID

    VECTOPLAN_CHUNK_DEFAULT_PROVIDER_WORLD_ID = _resolve_provider_world_id()

    VECTOPLAN_CHUNK_LEGACY_DEFAULT_PROVIDER_WORLD_ID = (
        VECTOPLAN_CHUNK_DEFAULT_PROVIDER_WORLD_ID
    )

    VECTOPLAN_CHUNK_DEFAULT_PROVIDER_ID = _safe_identifier(
        _read_str_env(
            "VECTOPLAN_CHUNK_DEFAULT_PROVIDER_ID",
            DEFAULT_PROVIDER_ID,
        ),
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

    VECTOPLAN_CHUNK_DEFAULT_BLOCK_REGISTRY_ID = _safe_identifier(
        _read_str_env(
            "VECTOPLAN_CHUNK_DEFAULT_BLOCK_REGISTRY_ID",
            DEFAULT_BLOCK_REGISTRY_ID,
        ),
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
    # App-project provisioning config
    # -------------------------------------------------------------------------

    VECTOPLAN_CHUNK_PROJECT_PROVISIONING_ENABLED = _read_bool_env(
        "VECTOPLAN_CHUNK_PROJECT_PROVISIONING_ENABLED",
        True,
    )

    VECTOPLAN_CHUNK_PROJECT_PROVISIONING_IDEMPOTENT = _read_bool_env(
        "VECTOPLAN_CHUNK_PROJECT_PROVISIONING_IDEMPOTENT",
        True,
    )

    VECTOPLAN_CHUNK_PROJECT_PROVISIONING_SOURCE_SERVICE = _read_str_env(
        "VECTOPLAN_CHUNK_PROJECT_PROVISIONING_SOURCE_SERVICE",
        DEFAULT_PROVISIONING_SOURCE_SERVICE,
    )

    VECTOPLAN_CHUNK_PROJECT_PROVISIONING_REQUIRE_EXTERNAL_APP_PROJECT_ID = (
        _read_bool_env(
            "VECTOPLAN_CHUNK_PROJECT_PROVISIONING_REQUIRE_EXTERNAL_APP_PROJECT_ID",
            True,
        )
    )

    VECTOPLAN_CHUNK_PROJECT_PROVISIONING_ALLOW_EXISTING_BY_EXTERNAL_ID = (
        _read_bool_env(
            "VECTOPLAN_CHUNK_PROJECT_PROVISIONING_ALLOW_EXISTING_BY_EXTERNAL_ID",
            True,
        )
    )

    VECTOPLAN_CHUNK_PROJECT_PROVISIONING_ALLOW_NAME_UPDATE = _read_bool_env(
        "VECTOPLAN_CHUNK_PROJECT_PROVISIONING_ALLOW_NAME_UPDATE",
        True,
    )

    VECTOPLAN_CHUNK_PROJECT_PROVISIONING_ALLOW_METADATA_UPDATE = _read_bool_env(
        "VECTOPLAN_CHUNK_PROJECT_PROVISIONING_ALLOW_METADATA_UPDATE",
        True,
    )

    VECTOPLAN_CHUNK_PROJECT_PROVISIONING_CREATE_UNIVERSE = _read_bool_env(
        "VECTOPLAN_CHUNK_PROJECT_PROVISIONING_CREATE_UNIVERSE",
        True,
    )

    VECTOPLAN_CHUNK_PROJECT_PROVISIONING_CREATE_WORLD = _read_bool_env(
        "VECTOPLAN_CHUNK_PROJECT_PROVISIONING_CREATE_WORLD",
        True,
    )

    VECTOPLAN_CHUNK_PROJECT_PROVISIONING_CREATE_BLOCK_REGISTRY_REF = (
        _read_bool_env(
            "VECTOPLAN_CHUNK_PROJECT_PROVISIONING_CREATE_BLOCK_REGISTRY_REF",
            True,
        )
    )

    VECTOPLAN_CHUNK_PROJECT_PROVISIONING_PROJECT_ID_PREFIX = _read_str_env(
        "VECTOPLAN_CHUNK_PROJECT_PROVISIONING_PROJECT_ID_PREFIX",
        DEFAULT_PROVISIONING_PROJECT_PREFIX,
    )

    VECTOPLAN_CHUNK_PROJECT_PROVISIONING_UNIVERSE_ID_PREFIX = _read_str_env(
        "VECTOPLAN_CHUNK_PROJECT_PROVISIONING_UNIVERSE_ID_PREFIX",
        DEFAULT_PROVISIONING_UNIVERSE_PREFIX,
    )

    VECTOPLAN_CHUNK_PROJECT_PROVISIONING_WORLD_ID_PREFIX = _read_str_env(
        "VECTOPLAN_CHUNK_PROJECT_PROVISIONING_WORLD_ID_PREFIX",
        DEFAULT_PROVISIONING_WORLD_PREFIX,
    )

    VECTOPLAN_CHUNK_PROJECT_PROVISIONING_DEFAULT_TEMPLATE_ID = _safe_identifier(
        _read_str_env(
            "VECTOPLAN_CHUNK_PROJECT_PROVISIONING_DEFAULT_TEMPLATE_ID",
            VECTOPLAN_CHUNK_DEFAULT_TEMPLATE_ID,
        ),
        VECTOPLAN_CHUNK_DEFAULT_TEMPLATE_ID,
    )

    VECTOPLAN_CHUNK_PROJECT_PROVISIONING_DEFAULT_WORLD_ID = (
        _resolve_provisioning_world_id(VECTOPLAN_CHUNK_DEFAULT_INSTANCE_WORLD_ID)
    )

    VECTOPLAN_CHUNK_PROJECT_PROVISIONING_DEFAULT_WORLD_NAME = _read_str_env(
        "VECTOPLAN_CHUNK_PROJECT_PROVISIONING_DEFAULT_WORLD_NAME",
        "Spawn World",
    )

    VECTOPLAN_CHUNK_PROJECT_PROVISIONING_DEFAULT_UNIVERSE_NAME = _read_str_env(
        "VECTOPLAN_CHUNK_PROJECT_PROVISIONING_DEFAULT_UNIVERSE_NAME",
        "Project Universe",
    )

    VECTOPLAN_CHUNK_PROJECT_PROVISIONING_ROUTE_BY_APP_ENABLED = _read_bool_env(
        "VECTOPLAN_CHUNK_PROJECT_PROVISIONING_ROUTE_BY_APP_ENABLED",
        True,
    )

    VECTOPLAN_CHUNK_PROJECT_PROVISIONING_ROUTE_ENSURE_ENABLED = _read_bool_env(
        "VECTOPLAN_CHUNK_PROJECT_PROVISIONING_ROUTE_ENSURE_ENABLED",
        True,
    )

    VECTOPLAN_CHUNK_PROJECT_PROVISIONING_MAX_METADATA_BYTES = _read_int_env(
        "VECTOPLAN_CHUNK_PROJECT_PROVISIONING_MAX_METADATA_BYTES",
        default=64 * 1024,
        minimum=1024,
        maximum=1024 * 1024,
    )

    # -------------------------------------------------------------------------
    # Bootstrap/provider options
    # -------------------------------------------------------------------------

    VECTOPLAN_CHUNK_BOOTSTRAP_ALLOW_DEFAULT_PROJECT = _read_bool_env(
        "VECTOPLAN_CHUNK_BOOTSTRAP_ALLOW_DEFAULT_PROJECT",
        True,
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
    # Health/readiness options
    # -------------------------------------------------------------------------

    VECTOPLAN_CHUNK_HEALTHCHECK_PATH = _read_str_env(
        "VECTOPLAN_CHUNK_HEALTHCHECK_PATH",
        "/projects/_status",
    )

    VECTOPLAN_CHUNK_HEALTHCHECK_REQUIRE_OK = _read_bool_env(
        "VECTOPLAN_CHUNK_HEALTHCHECK_REQUIRE_OK",
        True,
    )

    VECTOPLAN_CHUNK_SCHEMA_READY_REQUIRED = _read_bool_env(
        "VECTOPLAN_CHUNK_SCHEMA_READY_REQUIRED",
        True,
    )

    VECTOPLAN_CHUNK_SEED_READY_REQUIRED = _read_bool_env(
        "VECTOPLAN_CHUNK_SEED_READY_REQUIRED",
        True,
    )

    VECTOPLAN_CHUNK_DEFAULT_WORLD_READY_REQUIRED = _read_bool_env(
        "VECTOPLAN_CHUNK_DEFAULT_WORLD_READY_REQUIRED",
        True,
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
        engine_options = getattr(cls, "SQLALCHEMY_ENGINE_OPTIONS", {}) or {}
        connect_args = engine_options.get("connect_args", {}) or {}

        return {
            "databaseUrlMasked": _mask_database_uri(getattr(cls, "DATABASE_URL", None)),
            "sqlalchemyDatabaseUriMasked": _mask_database_uri(
                getattr(cls, "SQLALCHEMY_DATABASE_URI", None)
            ),
            "host": getattr(cls, "VECTOPLAN_CHUNK_DB_HOST", None),
            "port": getattr(cls, "VECTOPLAN_CHUNK_DB_PORT", None),
            "name": getattr(cls, "VECTOPLAN_CHUNK_DB_NAME", None),
            "user": getattr(cls, "VECTOPLAN_CHUNK_DB_USER", None),
            "trackModifications": getattr(cls, "SQLALCHEMY_TRACK_MODIFICATIONS", False),
            "echo": getattr(cls, "SQLALCHEMY_ECHO", False),
            "recordQueries": getattr(cls, "SQLALCHEMY_RECORD_QUERIES", False),
            "engineOptions": {
                key: value
                for key, value in engine_options.items()
                if key != "connect_args"
            },
            "connectArgs": dict(connect_args),
            "checkOnStartup": cls.VECTOPLAN_CHUNK_DB_CHECK_ON_STARTUP,
            "requireOnStartup": cls.VECTOPLAN_CHUNK_DB_REQUIRE_ON_STARTUP,
            "requireMigrations": cls.VECTOPLAN_CHUNK_REQUIRE_MIGRATIONS,
            "bootstrapEnabled": cls.VECTOPLAN_CHUNK_DB_BOOTSTRAP_ENABLED,
            "bootstrapCreateAll": cls.VECTOPLAN_CHUNK_DB_BOOTSTRAP_CREATE_ALL,
            "bootstrapSeedDefaults": cls.VECTOPLAN_CHUNK_DB_BOOTSTRAP_SEED_DEFAULTS,
            "bootstrapSeedDebugBlocks": cls.VECTOPLAN_CHUNK_DB_BOOTSTRAP_SEED_DEBUG_BLOCKS,
            "bootstrapSeedDevProject": cls.VECTOPLAN_CHUNK_DB_BOOTSTRAP_SEED_DEV_PROJECT,
            "bootstrapRepairMissingColumns": (
                cls.VECTOPLAN_CHUNK_BOOTSTRAP_REPAIR_MISSING_COLUMNS
            ),
            "bootstrapFailOnError": cls.VECTOPLAN_CHUNK_DB_BOOTSTRAP_FAIL_ON_ERROR,
            "seedOnEmptyOnly": cls.VECTOPLAN_CHUNK_SEED_ON_EMPTY_ONLY,
            "allowRuntimeDbMutations": cls.VECTOPLAN_CHUNK_ALLOW_RUNTIME_DB_MUTATIONS,
            "runtimeIsReadOnly": cls.VECTOPLAN_CHUNK_RUNTIME_IS_READ_ONLY,
            "autoCreateAll": cls.VECTOPLAN_CHUNK_AUTO_CREATE_ALL,
            "autoSeedDefaults": cls.VECTOPLAN_CHUNK_AUTO_SEED_DEFAULTS,
            "seedDebugBlocks": cls.VECTOPLAN_CHUNK_SEED_DEBUG_BLOCKS,
            "seedDevProject": cls.VECTOPLAN_CHUNK_SEED_DEV_PROJECT,
            "useAdvisoryLock": cls.VECTOPLAN_CHUNK_BOOTSTRAP_USE_ADVISORY_LOCK,
            "advisoryLockKey": cls.VECTOPLAN_CHUNK_BOOTSTRAP_ADVISORY_LOCK_KEY,
            "connectMaxAttempts": cls.VECTOPLAN_CHUNK_BOOTSTRAP_CONNECT_MAX_ATTEMPTS,
            "connectRetrySeconds": cls.VECTOPLAN_CHUNK_BOOTSTRAP_CONNECT_RETRY_SECONDS,
            "connectTimeoutSeconds": (
                cls.VECTOPLAN_CHUNK_BOOTSTRAP_CONNECT_TIMEOUT_SECONDS
            ),
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
            "defaultWorldId": cls.VECTOPLAN_CHUNK_DEFAULT_WORLD_ID,
            "instanceWorldId": cls.VECTOPLAN_CHUNK_DEFAULT_INSTANCE_WORLD_ID,
            "worldSlug": cls.VECTOPLAN_CHUNK_DEFAULT_INSTANCE_WORLD_SLUG,
            "worldName": cls.VECTOPLAN_CHUNK_DEFAULT_INSTANCE_WORLD_NAME,
            "templateId": cls.VECTOPLAN_CHUNK_DEFAULT_TEMPLATE_ID,
            "worldTemplateId": cls.VECTOPLAN_CHUNK_DEFAULT_WORLD_TEMPLATE_ID,
            "providerWorldId": cls.VECTOPLAN_CHUNK_DEFAULT_PROVIDER_WORLD_ID,
            "legacyProviderWorldId": cls.VECTOPLAN_CHUNK_LEGACY_DEFAULT_PROVIDER_WORLD_ID,
            "providerId": cls.VECTOPLAN_CHUNK_DEFAULT_PROVIDER_ID,
            "worldType": cls.VECTOPLAN_CHUNK_DEFAULT_WORLD_TYPE,
            "worldRole": cls.VECTOPLAN_CHUNK_DEFAULT_WORLD_ROLE,
            "worldScope": cls.VECTOPLAN_CHUNK_DEFAULT_WORLD_SCOPE,
            "worldOwnerType": cls.VECTOPLAN_CHUNK_DEFAULT_WORLD_OWNER_TYPE,
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
    def build_project_provisioning_config(cls) -> dict[str, Any]:
        """Build project provisioning config for status/debug output."""
        return {
            "enabled": cls.VECTOPLAN_CHUNK_PROJECT_PROVISIONING_ENABLED,
            "idempotent": cls.VECTOPLAN_CHUNK_PROJECT_PROVISIONING_IDEMPOTENT,
            "sourceService": cls.VECTOPLAN_CHUNK_PROJECT_PROVISIONING_SOURCE_SERVICE,
            "requireExternalAppProjectId": (
                cls.VECTOPLAN_CHUNK_PROJECT_PROVISIONING_REQUIRE_EXTERNAL_APP_PROJECT_ID
            ),
            "allowExistingByExternalId": (
                cls.VECTOPLAN_CHUNK_PROJECT_PROVISIONING_ALLOW_EXISTING_BY_EXTERNAL_ID
            ),
            "allowNameUpdate": cls.VECTOPLAN_CHUNK_PROJECT_PROVISIONING_ALLOW_NAME_UPDATE,
            "allowMetadataUpdate": (
                cls.VECTOPLAN_CHUNK_PROJECT_PROVISIONING_ALLOW_METADATA_UPDATE
            ),
            "createUniverse": cls.VECTOPLAN_CHUNK_PROJECT_PROVISIONING_CREATE_UNIVERSE,
            "createWorld": cls.VECTOPLAN_CHUNK_PROJECT_PROVISIONING_CREATE_WORLD,
            "createBlockRegistryRef": (
                cls.VECTOPLAN_CHUNK_PROJECT_PROVISIONING_CREATE_BLOCK_REGISTRY_REF
            ),
            "projectIdPrefix": cls.VECTOPLAN_CHUNK_PROJECT_PROVISIONING_PROJECT_ID_PREFIX,
            "universeIdPrefix": cls.VECTOPLAN_CHUNK_PROJECT_PROVISIONING_UNIVERSE_ID_PREFIX,
            "worldIdPrefix": cls.VECTOPLAN_CHUNK_PROJECT_PROVISIONING_WORLD_ID_PREFIX,
            "defaultTemplateId": (
                cls.VECTOPLAN_CHUNK_PROJECT_PROVISIONING_DEFAULT_TEMPLATE_ID
            ),
            "defaultWorldId": cls.VECTOPLAN_CHUNK_PROJECT_PROVISIONING_DEFAULT_WORLD_ID,
            "defaultWorldName": cls.VECTOPLAN_CHUNK_PROJECT_PROVISIONING_DEFAULT_WORLD_NAME,
            "defaultUniverseName": (
                cls.VECTOPLAN_CHUNK_PROJECT_PROVISIONING_DEFAULT_UNIVERSE_NAME
            ),
            "routeByAppEnabled": (
                cls.VECTOPLAN_CHUNK_PROJECT_PROVISIONING_ROUTE_BY_APP_ENABLED
            ),
            "routeEnsureEnabled": (
                cls.VECTOPLAN_CHUNK_PROJECT_PROVISIONING_ROUTE_ENSURE_ENABLED
            ),
            "maxMetadataBytes": cls.VECTOPLAN_CHUNK_PROJECT_PROVISIONING_MAX_METADATA_BYTES,
        }

    @classmethod
    def build_readiness_config(cls) -> dict[str, Any]:
        """Build readiness/health config."""
        return {
            "healthcheckPath": cls.VECTOPLAN_CHUNK_HEALTHCHECK_PATH,
            "healthcheckRequireOk": cls.VECTOPLAN_CHUNK_HEALTHCHECK_REQUIRE_OK,
            "schemaReadyRequired": cls.VECTOPLAN_CHUNK_SCHEMA_READY_REQUIRED,
            "seedReadyRequired": cls.VECTOPLAN_CHUNK_SEED_READY_REQUIRED,
            "defaultWorldReadyRequired": (
                cls.VECTOPLAN_CHUNK_DEFAULT_WORLD_READY_REQUIRED
            ),
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
            "mode": cls.VECTOPLAN_CHUNK_MODE,
            "startupMode": cls.VECTOPLAN_CHUNK_STARTUP_MODE,
            "runtimeMode": cls.VECTOPLAN_CHUNK_RUNTIME_MODE,
            "runMode": cls.VECTOPLAN_CHUNK_RUN_MODE,
            "runStartupHooks": cls.VECTOPLAN_CHUNK_RUN_STARTUP_HOOKS,
            "runtimeIsReadOnly": cls.VECTOPLAN_CHUNK_RUNTIME_IS_READ_ONLY,
            "allowRuntimeDbMutations": cls.VECTOPLAN_CHUNK_ALLOW_RUNTIME_DB_MUTATIONS,
            "devRoutesEnabled": cls.VECTOPLAN_CHUNK_ENABLE_DEV_ROUTES,
            "legacyRoutesEnabled": cls.VECTOPLAN_CHUNK_ENABLE_LEGACY_ROUTES,
            "projectScopedApiEnabled": True,
            "database": cls.build_database_config(),
            "worldStateDefaults": cls.build_world_state_defaults(),
            "projectProvisioning": cls.build_project_provisioning_config(),
            "readiness": cls.build_readiness_config(),
        }

    @classmethod
    def validate(cls) -> list[str]:
        """
        Validate configuration.

        Returns errors instead of raising so app startup can decide whether to
        fail fast or only report status.
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

        startup_mode = _normalize_mode(getattr(cls, "VECTOPLAN_CHUNK_STARTUP_MODE", None))
        run_mode = _normalize_text(getattr(cls, "VECTOPLAN_CHUNK_RUN_MODE", None))

        if not startup_mode:
            errors.append("VECTOPLAN_CHUNK_STARTUP_MODE must be set.")

        if not run_mode:
            errors.append("VECTOPLAN_CHUNK_RUN_MODE must be set.")

        project_id = getattr(cls, "VECTOPLAN_CHUNK_DEFAULT_PROJECT_ID", None)
        if not isinstance(project_id, str) or not project_id:
            errors.append("VECTOPLAN_CHUNK_DEFAULT_PROJECT_ID must be set.")

        universe_id = getattr(cls, "VECTOPLAN_CHUNK_DEFAULT_UNIVERSE_ID", None)
        if not isinstance(universe_id, str) or not universe_id:
            errors.append("VECTOPLAN_CHUNK_DEFAULT_UNIVERSE_ID must be set.")

        instance_world_id = getattr(cls, "VECTOPLAN_CHUNK_DEFAULT_INSTANCE_WORLD_ID", None)
        if not isinstance(instance_world_id, str) or not instance_world_id:
            errors.append("VECTOPLAN_CHUNK_DEFAULT_INSTANCE_WORLD_ID must be set.")

        default_world_id = getattr(cls, "VECTOPLAN_CHUNK_DEFAULT_WORLD_ID", None)
        if default_world_id != instance_world_id:
            errors.append(
                "VECTOPLAN_CHUNK_DEFAULT_WORLD_ID should equal "
                "VECTOPLAN_CHUNK_DEFAULT_INSTANCE_WORLD_ID for the concrete editable world."
            )

        template_id = getattr(cls, "VECTOPLAN_CHUNK_DEFAULT_TEMPLATE_ID", None)
        if not isinstance(template_id, str) or not template_id:
            errors.append("VECTOPLAN_CHUNK_DEFAULT_TEMPLATE_ID must be set.")

        provider_world_id = getattr(cls, "VECTOPLAN_CHUNK_DEFAULT_PROVIDER_WORLD_ID", None)
        if not isinstance(provider_world_id, str) or not provider_world_id:
            errors.append("VECTOPLAN_CHUNK_DEFAULT_PROVIDER_WORLD_ID must be set.")

        if isinstance(instance_world_id, str) and isinstance(provider_world_id, str):
            if instance_world_id == provider_world_id:
                errors.append(
                    "Concrete instance world id must not equal provider world id. "
                    "Use world_spawn for the concrete project world and flat for provider."
                )

        if isinstance(instance_world_id, str) and _looks_like_provider_or_template_world_id(instance_world_id):
            errors.append(
                "VECTOPLAN_CHUNK_DEFAULT_INSTANCE_WORLD_ID looks like a provider/template id. "
                "Use world_spawn for the concrete editable world."
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

        if cls.VECTOPLAN_CHUNK_RUNTIME_IS_READ_ONLY and cls.VECTOPLAN_CHUNK_ALLOW_RUNTIME_DB_MUTATIONS:
            errors.append(
                "Runtime cannot be both read-only and allow runtime DB mutations. "
                "Set VECTOPLAN_CHUNK_ALLOW_RUNTIME_DB_MUTATIONS=false for runtime."
            )

        if _is_runtime_mode(startup_mode):
            if cls.VECTOPLAN_CHUNK_AUTO_CREATE_ALL:
                errors.append("Runtime startup must not enable VECTOPLAN_CHUNK_AUTO_CREATE_ALL.")
            if cls.VECTOPLAN_CHUNK_AUTO_SEED_DEFAULTS:
                errors.append("Runtime startup must not enable VECTOPLAN_CHUNK_AUTO_SEED_DEFAULTS.")
            if cls.VECTOPLAN_CHUNK_DB_BOOTSTRAP_CREATE_ALL:
                errors.append(
                    "Runtime startup must not enable "
                    "VECTOPLAN_CHUNK_DB_BOOTSTRAP_CREATE_ALL."
                )
            if cls.VECTOPLAN_CHUNK_DB_BOOTSTRAP_SEED_DEFAULTS:
                errors.append(
                    "Runtime startup must not enable "
                    "VECTOPLAN_CHUNK_DB_BOOTSTRAP_SEED_DEFAULTS."
                )

        if _is_bootstrap_mode(startup_mode):
            if cls.VECTOPLAN_CHUNK_RUNTIME_IS_READ_ONLY:
                errors.append("Bootstrap mode must not be read-only.")
            if not cls.VECTOPLAN_CHUNK_ALLOW_RUNTIME_DB_MUTATIONS:
                errors.append("Bootstrap mode must allow DB mutations.")

        if cls.VECTOPLAN_CHUNK_PROJECT_PROVISIONING_ENABLED:
            if not cls.VECTOPLAN_CHUNK_PROJECT_PROVISIONING_PROJECT_ID_PREFIX:
                errors.append("Provisioning project id prefix must not be empty.")
            if not cls.VECTOPLAN_CHUNK_PROJECT_PROVISIONING_UNIVERSE_ID_PREFIX:
                errors.append("Provisioning universe id prefix must not be empty.")
            if not cls.VECTOPLAN_CHUNK_PROJECT_PROVISIONING_WORLD_ID_PREFIX:
                errors.append("Provisioning world id prefix must not be empty.")
            if not cls.VECTOPLAN_CHUNK_PROJECT_PROVISIONING_DEFAULT_TEMPLATE_ID:
                errors.append("Provisioning default template id must not be empty.")
            if not cls.VECTOPLAN_CHUNK_PROJECT_PROVISIONING_DEFAULT_WORLD_ID:
                errors.append("Provisioning default world id must not be empty.")

        provisioning_world_id = getattr(
            cls,
            "VECTOPLAN_CHUNK_PROJECT_PROVISIONING_DEFAULT_WORLD_ID",
            None,
        )
        if isinstance(provisioning_world_id, str):
            if _looks_like_provider_or_template_world_id(provisioning_world_id):
                errors.append(
                    "Provisioning default world id looks like a provider/template id. "
                    "Use world_spawn or a concrete project world id."
                )

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
            "APP_ENV",
            "FLASK_ENV",
        ),
        "development",
    )

    DEBUG = _read_bool_env_any(
        (
            "VECTOPLAN_CHUNK_DEBUG",
            "DEBUG",
            "FLASK_DEBUG",
        ),
        True,
    )

    TESTING = _read_bool_env_any(
        (
            "VECTOPLAN_CHUNK_TESTING",
            "TESTING",
        ),
        False,
    )

    TEMPLATES_AUTO_RELOAD = _read_bool_env(
        "VECTOPLAN_CHUNK_TEMPLATES_AUTO_RELOAD",
        True,
    )


class DevelopmentConfig(BaseConfig):
    """Explicit development configuration."""

    APP_ENV = "development"
    DEBUG = True
    TESTING = False
    TEMPLATES_AUTO_RELOAD = True

    SEND_FILE_MAX_AGE_DEFAULT = _read_int_env(
        "VECTOPLAN_CHUNK_SEND_FILE_MAX_AGE_DEFAULT",
        default=0,
        minimum=0,
    )

    VECTOPLAN_CHUNK_ENABLE_DEV_ROUTES = True
    VECTOPLAN_CHUNK_ROUTE_DEBUG_ERRORS = _read_bool_env(
        "VECTOPLAN_CHUNK_ROUTE_DEBUG_ERRORS",
        True,
    )


class BootstrapConfig(BaseConfig):
    """
    Explicit DB bootstrap configuration.

    This class is used by bootstrap code paths. It allows DB mutations and
    defaults create/seed switches to true unless explicitly overridden.
    """

    APP_ENV = "bootstrap"
    DEBUG = _read_bool_env("VECTOPLAN_CHUNK_DEBUG", True)
    TESTING = False

    VECTOPLAN_CHUNK_MODE = "db-bootstrap"
    VECTOPLAN_CHUNK_STARTUP_MODE = "db-bootstrap"
    VECTOPLAN_CHUNK_RUNTIME_MODE = "db-bootstrap"
    VECTOPLAN_CHUNK_RUN_MODE = "db-bootstrap"
    VECTOPLAN_CHUNK_RUNTIME_IS_READ_ONLY = False
    VECTOPLAN_CHUNK_ALLOW_RUNTIME_DB_MUTATIONS = True
    VECTOPLAN_CHUNK_RUN_STARTUP_HOOKS = _read_bool_env(
        "VECTOPLAN_CHUNK_RUN_STARTUP_HOOKS",
        False,
    )

    VECTOPLAN_CHUNK_DB_BOOTSTRAP_ENABLED = _read_bool_env(
        "VECTOPLAN_CHUNK_DB_BOOTSTRAP_ENABLED",
        True,
    )

    VECTOPLAN_CHUNK_AUTO_CREATE_ALL = _read_bool_env(
        "VECTOPLAN_CHUNK_AUTO_CREATE_ALL",
        True,
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

    VECTOPLAN_CHUNK_DB_BOOTSTRAP_CREATE_ALL = _read_bool_env(
        "VECTOPLAN_CHUNK_DB_BOOTSTRAP_CREATE_ALL",
        VECTOPLAN_CHUNK_AUTO_CREATE_ALL,
    )

    VECTOPLAN_CHUNK_DB_BOOTSTRAP_SEED_DEFAULTS = _read_bool_env(
        "VECTOPLAN_CHUNK_DB_BOOTSTRAP_SEED_DEFAULTS",
        VECTOPLAN_CHUNK_AUTO_SEED_DEFAULTS,
    )

    VECTOPLAN_CHUNK_DB_BOOTSTRAP_SEED_DEBUG_BLOCKS = _read_bool_env(
        "VECTOPLAN_CHUNK_DB_BOOTSTRAP_SEED_DEBUG_BLOCKS",
        VECTOPLAN_CHUNK_SEED_DEBUG_BLOCKS,
    )

    VECTOPLAN_CHUNK_DB_BOOTSTRAP_SEED_DEV_PROJECT = _read_bool_env(
        "VECTOPLAN_CHUNK_DB_BOOTSTRAP_SEED_DEV_PROJECT",
        VECTOPLAN_CHUNK_SEED_DEV_PROJECT,
    )

    VECTOPLAN_CHUNK_BOOTSTRAP_REPAIR_MISSING_COLUMNS = _read_bool_env(
        "VECTOPLAN_CHUNK_BOOTSTRAP_REPAIR_MISSING_COLUMNS",
        True,
    )

    VECTOPLAN_CHUNK_DB_BOOTSTRAP_FAIL_ON_ERROR = _read_bool_env(
        "VECTOPLAN_CHUNK_DB_BOOTSTRAP_FAIL_ON_ERROR",
        True,
    )

    VECTOPLAN_CHUNK_SEED_ON_EMPTY_ONLY = _read_bool_env(
        "VECTOPLAN_CHUNK_SEED_ON_EMPTY_ONLY",
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

    VECTOPLAN_CHUNK_MODE = "testing"
    VECTOPLAN_CHUNK_STARTUP_MODE = "testing"
    VECTOPLAN_CHUNK_RUNTIME_IS_READ_ONLY = False
    VECTOPLAN_CHUNK_ALLOW_RUNTIME_DB_MUTATIONS = True

    VECTOPLAN_CHUNK_AUTO_CREATE_ALL = _read_bool_env(
        "VECTOPLAN_CHUNK_TEST_AUTO_CREATE_ALL",
        True,
    )

    VECTOPLAN_CHUNK_AUTO_SEED_DEFAULTS = _read_bool_env(
        "VECTOPLAN_CHUNK_TEST_AUTO_SEED_DEFAULTS",
        True,
    )

    VECTOPLAN_CHUNK_SEED_DEBUG_BLOCKS = _read_bool_env(
        "VECTOPLAN_CHUNK_TEST_SEED_DEBUG_BLOCKS",
        True,
    )

    VECTOPLAN_CHUNK_SEED_DEV_PROJECT = _read_bool_env(
        "VECTOPLAN_CHUNK_TEST_SEED_DEV_PROJECT",
        True,
    )

    VECTOPLAN_CHUNK_DB_BOOTSTRAP_CREATE_ALL = VECTOPLAN_CHUNK_AUTO_CREATE_ALL
    VECTOPLAN_CHUNK_DB_BOOTSTRAP_SEED_DEFAULTS = VECTOPLAN_CHUNK_AUTO_SEED_DEFAULTS
    VECTOPLAN_CHUNK_DB_BOOTSTRAP_SEED_DEBUG_BLOCKS = VECTOPLAN_CHUNK_SEED_DEBUG_BLOCKS
    VECTOPLAN_CHUNK_DB_BOOTSTRAP_SEED_DEV_PROJECT = VECTOPLAN_CHUNK_SEED_DEV_PROJECT
    VECTOPLAN_CHUNK_BOOTSTRAP_REPAIR_MISSING_COLUMNS = True


class ProductionConfig(BaseConfig):
    """Production-oriented configuration."""

    APP_ENV = "production"
    DEBUG = False
    TESTING = False
    TEMPLATES_AUTO_RELOAD = False

    SESSION_COOKIE_SECURE = _read_bool_env(
        "VECTOPLAN_CHUNK_SESSION_COOKIE_SECURE",
        True,
    )

    SEND_FILE_MAX_AGE_DEFAULT = _read_int_env(
        "VECTOPLAN_CHUNK_SEND_FILE_MAX_AGE_DEFAULT",
        default=3600,
        minimum=0,
    )

    VECTOPLAN_CHUNK_ROUTE_DEBUG_ERRORS = False
    VECTOPLAN_CHUNK_BOOTSTRAP_REQUIRE_PROVIDER_WORLDS = True

    VECTOPLAN_CHUNK_RUNTIME_IS_READ_ONLY = True
    VECTOPLAN_CHUNK_ALLOW_RUNTIME_DB_MUTATIONS = False
    VECTOPLAN_CHUNK_AUTO_CREATE_ALL = False
    VECTOPLAN_CHUNK_AUTO_SEED_DEFAULTS = False
    VECTOPLAN_CHUNK_SEED_DEBUG_BLOCKS = False
    VECTOPLAN_CHUNK_SEED_DEV_PROJECT = False
    VECTOPLAN_CHUNK_DB_BOOTSTRAP_CREATE_ALL = False
    VECTOPLAN_CHUNK_DB_BOOTSTRAP_SEED_DEFAULTS = False
    VECTOPLAN_CHUNK_DB_BOOTSTRAP_SEED_DEBUG_BLOCKS = False
    VECTOPLAN_CHUNK_DB_BOOTSTRAP_SEED_DEV_PROJECT = False
    VECTOPLAN_CHUNK_BOOTSTRAP_REPAIR_MISSING_COLUMNS = False

    VECTOPLAN_CHUNK_DB_REQUIRE_ON_STARTUP = _read_bool_env(
        "VECTOPLAN_CHUNK_DB_REQUIRE_ON_STARTUP",
        True,
    )

    VECTOPLAN_CHUNK_REQUIRE_MIGRATIONS = _read_bool_env(
        "VECTOPLAN_CHUNK_REQUIRE_MIGRATIONS",
        False,
    )


CONFIG_BY_NAME: Final[dict[str, type[BaseConfig]]] = {
    "default": Config,
    "config": Config,
    "development": DevelopmentConfig,
    "dev": DevelopmentConfig,
    "local": DevelopmentConfig,
    "bootstrap": BootstrapConfig,
    "db-bootstrap": BootstrapConfig,
    "db_bootstrap": BootstrapConfig,
    "db-init": BootstrapConfig,
    "db_init": BootstrapConfig,
    "init": BootstrapConfig,
    "database-bootstrap": BootstrapConfig,
    "database_bootstrap": BootstrapConfig,
    "testing": TestingConfig,
    "test": TestingConfig,
    "production": ProductionConfig,
    "prod": ProductionConfig,
}


def get_config_class(name: str | None = None) -> type[BaseConfig]:
    """
    Return config class.

    Bootstrap mode has priority over a generic development config name because
    bootstrap scripts often pass --config development while setting
    VECTOPLAN_CHUNK_STARTUP_MODE=db-bootstrap.
    """
    startup_mode = _current_startup_mode("runtime")
    requested_name = _normalize_text(name)

    if requested_name is None:
        requested_name = _read_str_env_any(
            (
                "VECTOPLAN_CHUNK_CONFIG",
                "APP_CONFIG",
            ),
            "default",
        )

    key = requested_name.lower().replace("_", "-") if requested_name else "default"

    if _is_bootstrap_mode(startup_mode) and key not in {"testing", "test"}:
        return BootstrapConfig

    if _is_check_only_mode(startup_mode) and key not in {"testing", "test", "production", "prod"}:
        return Config

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
    "DEFAULT_PROVISIONING_SOURCE_SERVICE",
    "DEFAULT_PROVISIONING_PROJECT_PREFIX",
    "DEFAULT_PROVISIONING_UNIVERSE_PREFIX",
    "DEFAULT_PROVISIONING_WORLD_PREFIX",
    "BaseConfig",
    "Config",
    "DevelopmentConfig",
    "BootstrapConfig",
    "TestingConfig",
    "ProductionConfig",
    "CONFIG_BY_NAME",
    "get_config_class",
    "refresh_env_cache",
]