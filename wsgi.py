# services/vectoplan-chunk/wsgi.py
"""
WSGI entrypoint for the `vectoplan-chunk` service.

Responsibilities:
- expose the Flask app for WSGI servers such as Gunicorn
- provide a stable `app` and `application` export
- resolve config name defensively
- create the app once per process
- provide a local direct-start fallback for development

Configuration priority:
1. VECTOPLAN_CHUNK_CONFIG
2. VECTOPLAN_EDITOR_CONFIG
3. APP_CONFIG
4. None -> create_app() uses its own default

Important:
- app.py remains the actual Flask app factory
- wsgi.py is only the standardized runtime entrypoint
- no business logic here
- no direct database writes here
- no direct migrations here
- no direct chunk/world/command logic here
"""

from __future__ import annotations

import importlib
import os
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any, Final


# -----------------------------------------------------------------------------
# Constants
# -----------------------------------------------------------------------------

_TRUE_VALUES: Final[set[str]] = {"1", "true", "t", "yes", "y", "on", "enabled"}
_FALSE_VALUES: Final[set[str]] = {"0", "false", "f", "no", "n", "off", "disabled"}

DEFAULT_SERVICE_NAME: Final[str] = "vectoplan-chunk"
DEFAULT_HOST: Final[str] = "127.0.0.1"
DEFAULT_PORT: Final[int] = 5000

APP_FACTORY_MODULE_NAME: Final[str] = "app"
APP_FACTORY_FUNCTION_NAME: Final[str] = "create_app"


# -----------------------------------------------------------------------------
# Path helpers
# -----------------------------------------------------------------------------

def _resolve_service_root() -> Path:
    """
    Resolve service root directory.

    Normal case:
    wsgi.py is located directly in the service root.

    Fallback:
    current working directory.
    """
    try:
        return Path(__file__).resolve().parent
    except Exception:
        return Path(".").resolve()


SERVICE_ROOT: Final[Path] = _resolve_service_root()


@lru_cache(maxsize=1)
def _ensure_service_root_on_sys_path() -> bool:
    """
    Ensure service root is available on sys.path.

    This keeps imports stable for:
    - app
    - config
    - extensions
    - models
    - routes
    - src.*
    """
    try:
        service_root_str = str(SERVICE_ROOT)
    except Exception:
        return False

    if not service_root_str:
        return False

    try:
        if service_root_str not in sys.path:
            sys.path.insert(0, service_root_str)
        return True
    except Exception:
        return False


# -----------------------------------------------------------------------------
# Defensive env helpers
# -----------------------------------------------------------------------------

def _safe_getenv(name: str, default: str | None = None) -> str | None:
    """Read environment variable defensively."""
    try:
        return os.getenv(name, default)
    except Exception:
        return default


def _normalize_text(value: Any, default: str | None = None) -> str | None:
    """
    Normalize text input.

    Behavior:
    - None -> default
    - strip whitespace
    - empty string -> default
    """
    if value is None:
        return default

    try:
        normalized = str(value).strip()
    except Exception:
        return default

    return normalized or default


def _read_str_env_any(names: tuple[str, ...], default: str | None = None) -> str | None:
    """Read first available string env variable from priority list."""
    for name in names:
        value = _normalize_text(_safe_getenv(name), default=None)
        if value is not None:
            return value

    return default


def _read_bool_env_any(names: tuple[str, ...], default: bool = False) -> bool:
    """Read first available boolean env variable from priority list."""
    for name in names:
        raw_value = _normalize_text(_safe_getenv(name), default=None)

        if raw_value is None:
            continue

        lowered = raw_value.lower()

        if lowered in _TRUE_VALUES:
            return True

        if lowered in _FALSE_VALUES:
            return False

        return default

    return default


def _read_int_env_any(
    names: tuple[str, ...],
    default: int,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    """Read first available integer env variable from priority list."""
    value = default

    for name in names:
        raw_value = _normalize_text(_safe_getenv(name), default=None)

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


def _safe_exception_message(exc: BaseException | Any) -> str:
    """Return robust exception message."""
    try:
        message = str(exc)
    except Exception:
        message = exc.__class__.__name__

    return message or exc.__class__.__name__


# -----------------------------------------------------------------------------
# Config resolution
# -----------------------------------------------------------------------------

def _resolve_config_name() -> str | None:
    """
    Resolve config name for app factory.

    Priority:
    1. VECTOPLAN_CHUNK_CONFIG
    2. VECTOPLAN_EDITOR_CONFIG
    3. APP_CONFIG
    4. None -> create_app() uses own default
    """
    return _read_str_env_any(
        (
            "VECTOPLAN_CHUNK_CONFIG",
            "VECTOPLAN_EDITOR_CONFIG",
            "APP_CONFIG",
        ),
        default=None,
    )


# -----------------------------------------------------------------------------
# App factory loading
# -----------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _load_create_app():
    """
    Import and return the create_app factory.

    This is cached so repeated WSGI access does not repeatedly import the
    factory module.
    """
    _ensure_service_root_on_sys_path()

    try:
        module = importlib.import_module(APP_FACTORY_MODULE_NAME)
    except Exception as exc:
        raise RuntimeError(
            f"Could not import `{APP_FACTORY_MODULE_NAME}` while building "
            f"`{DEFAULT_SERVICE_NAME}` WSGI app. "
            f"Cause: {_safe_exception_message(exc)}"
        ) from exc

    create_app = getattr(module, APP_FACTORY_FUNCTION_NAME, None)

    if not callable(create_app):
        raise RuntimeError(
            f"Module `{APP_FACTORY_MODULE_NAME}` does not expose callable "
            f"`{APP_FACTORY_FUNCTION_NAME}`."
        )

    return create_app


# -----------------------------------------------------------------------------
# Cached WSGI app creation
# -----------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _build_wsgi_app():
    """
    Create the Flask app exactly once per process.

    Why cache?
    - avoids repeated extension/db/model initialization in one worker process
    - matches the usual WSGI lifecycle
    - provides stable access for tests and diagnostics
    """
    config_name = _resolve_config_name()
    create_app = _load_create_app()

    try:
        if config_name is not None:
            return create_app(config_name)

        return create_app()

    except Exception as exc:
        raise RuntimeError(
            f"The WSGI application for `{DEFAULT_SERVICE_NAME}` could not be created. "
            f"Config name: {config_name!r}. "
            f"Service root: {str(SERVICE_ROOT)!r}. "
            f"Cause: {_safe_exception_message(exc)}"
        ) from exc


def get_wsgi_app():
    """
    Public access point for the WSGI application.

    Useful for tests or tooling that intentionally load the app through
    wsgi.py instead of app.py.
    """
    return _build_wsgi_app()


def reset_wsgi_app_cache() -> None:
    """
    Clear local WSGI app cache.

    Intended for tests. Not needed in normal server operation.
    """
    _build_wsgi_app.cache_clear()
    _load_create_app.cache_clear()
    _ensure_service_root_on_sys_path.cache_clear()


def get_wsgi_status() -> dict[str, Any]:
    """
    Return lightweight WSGI bootstrap status.

    This avoids touching Flask internals except for checking whether the cached
    app has already been built.
    """
    build_cache_info = _build_wsgi_app.cache_info()
    factory_cache_info = _load_create_app.cache_info()

    return {
        "service": DEFAULT_SERVICE_NAME,
        "serviceRoot": str(SERVICE_ROOT),
        "serviceRootOnSysPath": str(SERVICE_ROOT) in sys.path,
        "configName": _resolve_config_name(),
        "appFactoryModule": APP_FACTORY_MODULE_NAME,
        "appFactoryFunction": APP_FACTORY_FUNCTION_NAME,
        "wsgiAppCache": {
            "hits": build_cache_info.hits,
            "misses": build_cache_info.misses,
            "maxsize": build_cache_info.maxsize,
            "currsize": build_cache_info.currsize,
        },
        "factoryCache": {
            "hits": factory_cache_info.hits,
            "misses": factory_cache_info.misses,
            "maxsize": factory_cache_info.maxsize,
            "currsize": factory_cache_info.currsize,
        },
    }


# -----------------------------------------------------------------------------
# WSGI exports
# -----------------------------------------------------------------------------

app = get_wsgi_app()
application = app


# -----------------------------------------------------------------------------
# Optional direct start for local development
# -----------------------------------------------------------------------------

if __name__ == "__main__":
    """
    Local fallback start.

    This is not the preferred production entrypoint. Gunicorn remains the
    expected server path in Docker/staging/production.
    """
    host = _read_str_env_any(
        (
            "VECTOPLAN_CHUNK_HOST",
            "VECTOPLAN_EDITOR_HOST",
            "HOST",
        ),
        default=DEFAULT_HOST,
    ) or DEFAULT_HOST

    port = _read_int_env_any(
        (
            "VECTOPLAN_CHUNK_PORT",
            "VECTOPLAN_EDITOR_PORT",
            "PORT",
        ),
        default=DEFAULT_PORT,
        minimum=1,
        maximum=65535,
    )

    debug = _read_bool_env_any(
        (
            "VECTOPLAN_CHUNK_DEBUG",
            "VECTOPLAN_EDITOR_DEBUG",
            "DEBUG",
            "FLASK_DEBUG",
        ),
        default=False,
    )

    try:
        app.run(host=host, port=port, debug=debug)
    except Exception as exc:
        raise RuntimeError(
            f"Local direct start of `{DEFAULT_SERVICE_NAME}` through wsgi.py failed. "
            f"host={host!r}, port={port!r}, debug={debug!r}. "
            f"Cause: {_safe_exception_message(exc)}"
        ) from exc


__all__ = [
    "app",
    "application",
    "get_wsgi_app",
    "reset_wsgi_app_cache",
    "get_wsgi_status",
]