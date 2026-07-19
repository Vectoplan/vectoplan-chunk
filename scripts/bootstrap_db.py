# services/vectoplan-chunk/scripts/bootstrap_db.py
"""
Explicit database bootstrap command for the `vectoplan-chunk` service.

This script is the controlled entrypoint for local/dev DB initialization.

Responsibilities:
- create a Flask app safely,
- explicitly disable normal runtime startup hooks while the app is being created,
- run schema bootstrap when requested,
- optionally repair missing columns for local/dev schema drift,
- run default seed bootstrap when requested,
- repair missing default seed invariants when requested,
- ensure the concrete editable default world `world_spawn` exists in init mode,
- verify the reserved Air invariant and persistent `system_railing` mirror,
- expose system-block readiness and reconciliation counters in every output mode,
- run read-only check-only diagnostics,
- print a JSON or human-readable result,
- return a useful process exit code.

Important boundaries:
- this script is not the normal Gunicorn runtime,
- this script should not serve HTTP requests,
- this script should not generate chunks,
- this script should not execute chunk commands,
- this script should not load ChunkSnapshots/ChunkEvents/ObjectRefs,
- this script does not replace Alembic for production-grade migrations.

World-id rule:
- world_spawn = concrete editable WorldInstance.
- flat        = template/provider id.
- A concrete default world must not silently become "flat".

Typical local usage from service root:

    python scripts/bootstrap_db.py --create-all --seed --json

Typical container usage:

    python /opt/vectoplan/services/vectoplan-chunk/scripts/bootstrap_db.py --create-all --seed --json

Typical runtime readiness check:

    python scripts/bootstrap_db.py --check-only --json

Exit codes:
    0 = bootstrap/check succeeded or was intentionally skipped
    1 = bootstrap/check failed
    2 = app creation/import failed
    3 = invalid arguments
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import sys
import traceback
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence


# -----------------------------------------------------------------------------
# Constants
# -----------------------------------------------------------------------------

SCRIPT_RESULT_VERSION = "bootstrap-db-script-result.v6"

EXIT_OK = 0
EXIT_BOOTSTRAP_FAILED = 1
EXIT_APP_FAILED = 2
EXIT_INVALID_ARGS = 3

DEFAULT_CREATE_ALL = True
DEFAULT_SEED = True
DEFAULT_JSON = False

TRUE_VALUES = {"1", "true", "t", "yes", "y", "on", "enabled", "enable"}
FALSE_VALUES = {"0", "false", "f", "no", "n", "off", "disabled", "disable"}

DEFAULT_PROJECT_ID = "dev-project"
DEFAULT_UNIVERSE_ID = "dev-universe"
DEFAULT_WORLD_ID = "world_spawn"
DEFAULT_TEMPLATE_ID = "flat"
DEFAULT_PROVIDER_ID = "flat"
DEFAULT_PROVIDER_WORLD_ID = "flat"
DEFAULT_BLOCK_REGISTRY_ID = "debug-blocks"
DEFAULT_BLOCK_REGISTRY_VERSION = "1"
DEFAULT_SYSTEM_RAILING_BLOCK_TYPE_ID = "system_railing"
DEFAULT_SYSTEM_BLOCK_BOOTSTRAP_USER_ID = "vectoplan-system-block-bootstrap"

DEFAULT_PROJECT_OWNER_AUTH_USER_ID = "auth_dev_owner"
DEFAULT_PROJECT_ACCESS_SOURCE_SERVICE = "vectoplan-app"
DEFAULT_PROJECT_ACCESS_SERVICE_ID = "vectoplan-chunk-init"
DEFAULT_PROJECT_ACCESS_PROJECTION_VERSION = "app-project-access-v1"
DEFAULT_PROJECT_ACCESS_ROLE = "owner"
DEFAULT_PROJECT_ACCESS_ASSIGNMENT_TYPE = "direct"


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


def _duration_ms(started_at: str | None, completed_at: str | None) -> int:
    """Return duration in milliseconds from ISO timestamps."""
    if not started_at or not completed_at:
        return 0

    try:
        started = datetime.fromisoformat(started_at)
        completed = datetime.fromisoformat(completed_at)
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


def _safe_bool(value: Any, default: bool = False) -> bool:
    """Convert value to bool robustly."""
    if isinstance(value, bool):
        return value

    if isinstance(value, int):
        return bool(value)

    text = _safe_str(value, "").lower()

    if text in TRUE_VALUES:
        return True

    if text in FALSE_VALUES:
        return False

    return default


def _safe_int(value: Any, default: int = 0) -> int:
    """Convert value to int robustly."""
    if isinstance(value, bool):
        return int(value)

    if isinstance(value, int):
        return value

    try:
        return int(value)
    except Exception:
        return default


def _safe_float(value: Any, default: float = 0.0) -> float:
    """Convert value to float robustly."""
    if isinstance(value, (int, float)):
        return float(value)

    try:
        return float(value)
    except Exception:
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    """Read boolean environment variable."""
    return _safe_bool(os.environ.get(name), default)


def _env_str(name: str, default: str = "") -> str:
    """Read string environment variable."""
    return _safe_str(os.environ.get(name), default)


def _env_str_any(names: Sequence[str], default: str = "") -> str:
    """Read first non-empty env var from a list."""
    for name in names:
        value = _env_str(name, "")
        if value:
            return value

    return default


def _looks_like_unsafe_auth_user_id(value: Any) -> bool:
    """Return whether a value is a local/anonymous identity, not a canonical auth id."""
    text = _safe_str(value, "")
    if not text:
        return True
    lowered = text.lower()
    if text.isdigit() or lowered in {
        "none",
        "null",
        "undefined",
        "anonymous",
        "guest",
        "bootstrap",
        "system",
    }:
        return True
    if "@" in text or "://" in text:
        return True
    if any(character.isspace() or ord(character) < 32 for character in text):
        return True
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.:-")
    return len(text) > 255 or any(character not in allowed for character in text)


def _normalize_auth_user_id(
    value: Any,
    default: str = DEFAULT_PROJECT_OWNER_AUTH_USER_ID,
) -> str:
    """Normalize a cross-service auth id and replace legacy numeric placeholders."""
    candidate = _safe_str(value, "")
    if not _looks_like_unsafe_auth_user_id(candidate):
        return candidate

    fallback = _safe_str(default, "")
    if fallback and not _looks_like_unsafe_auth_user_id(fallback):
        return fallback
    return ""


def _resolve_project_owner_auth_user_id() -> str:
    """Resolve the canonical development-project owner from compatible env aliases."""
    names = (
        "VECTOPLAN_CHUNK_DEFAULT_PROJECT_OWNER_AUTH_USER_ID",
        "VECTOPLAN_CHUNK_DEFAULT_OWNER_AUTH_USER_ID",
        "VECTOPLAN_CHUNK_PROJECT_OWNER_AUTH_USER_ID",
        "VECTOPLAN_CHUNK_DEV_PROJECT_OWNER_AUTH_USER_ID",
        "VECTOPLAN_CHUNK_DEFAULT_PROJECT_OWNER_USER_ID",
        "VECTOPLAN_CHUNK_DEFAULT_OWNER_USER_ID",
        "VECTOPLAN_CHUNK_PROJECT_OWNER_USER_ID",
    )
    for name in names:
        raw_value = _env_str(name, "")
        if raw_value:
            return _normalize_auth_user_id(
                raw_value,
                DEFAULT_PROJECT_OWNER_AUTH_USER_ID,
            )
    return DEFAULT_PROJECT_OWNER_AUTH_USER_ID


def _ensure_project_owner_env_defaults(
    *,
    seed_project_access: bool | None = None,
) -> str:
    """Publish one canonical owner/access policy to old and new bootstrap readers."""
    owner_auth_user_id = _resolve_project_owner_auth_user_id()
    for name in (
        "VECTOPLAN_CHUNK_DEFAULT_PROJECT_OWNER_AUTH_USER_ID",
        "VECTOPLAN_CHUNK_DEFAULT_OWNER_AUTH_USER_ID",
        "VECTOPLAN_CHUNK_PROJECT_OWNER_AUTH_USER_ID",
        "VECTOPLAN_CHUNK_DEV_PROJECT_OWNER_AUTH_USER_ID",
        "VECTOPLAN_CHUNK_DEFAULT_PROJECT_OWNER_USER_ID",
        "VECTOPLAN_CHUNK_DEFAULT_OWNER_USER_ID",
        "VECTOPLAN_CHUNK_PROJECT_OWNER_USER_ID",
    ):
        set_env_value(name, owner_auth_user_id, override=True)

    if seed_project_access is not None:
        enabled = "true" if seed_project_access else "false"
        for name in (
            "VECTOPLAN_CHUNK_SEED_PROJECT_ACCESS",
            "VECTOPLAN_CHUNK_DB_BOOTSTRAP_SEED_PROJECT_ACCESS",
            "VECTOPLAN_CHUNK_SEED_PROJECT_ACCESS_PROJECTION",
            "VECTOPLAN_CHUNK_DB_BOOTSTRAP_SEED_PROJECT_ACCESS_PROJECTION",
        ):
            set_env_value(name, enabled, override=True)

    return owner_auth_user_id


def _project_access_required() -> bool:
    """Return whether a seeded development project requires a ready access projection."""
    return _env_bool(
        "VECTOPLAN_CHUNK_PROJECT_ACCESS_REQUIRED",
        _env_bool("VECTOPLAN_CHUNK_ACCESS_CONTROL_ENABLED", True),
    )


def _debug_blocks_required() -> bool:
    """Return whether debug blocks are an explicit seed/readiness requirement."""
    return _env_bool(
        "VECTOPLAN_CHUNK_DB_BOOTSTRAP_SEED_DEBUG_BLOCKS",
        _env_bool("VECTOPLAN_CHUNK_SEED_DEBUG_BLOCKS", False),
    )


def _safe_exception_message(exc: BaseException | Any) -> str:
    """Return robust exception message."""
    try:
        message = str(exc)
    except Exception:
        message = exc.__class__.__name__

    return message or exc.__class__.__name__


def _to_plain_dict(value: Any) -> dict[str, Any]:
    """Convert dataclass/mapping/object result to plain dict."""
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


def _json_default(value: Any) -> Any:
    """JSON serializer fallback."""
    try:
        if is_dataclass(value):
            return asdict(value)
    except Exception:
        pass

    if isinstance(value, Path):
        return str(value)

    if isinstance(value, datetime):
        return value.isoformat()

    try:
        return str(value)
    except Exception:
        return repr(value)


def _print_json(value: Any, *, pretty: bool = True) -> None:
    """Print JSON to stdout."""
    if pretty:
        print(json.dumps(value, indent=2, sort_keys=True, default=_json_default))
    else:
        print(json.dumps(value, separators=(",", ":"), sort_keys=True, default=_json_default))


def _print_human_result(result: dict[str, Any]) -> None:
    """Print compact human-readable result."""
    print("")
    print("VECTOPLAN Chunk DB Bootstrap")
    print("=" * 32)
    print(f"ok:                    {result.get('ok')}")
    print(f"status:                {result.get('status')}")
    print(f"durationMs:            {result.get('durationMs')}")
    print(f"schema ready:          {result.get('schemaReady')}")
    print(f"seed ready:            {result.get('seedReady')}")
    print(f"default project ready: {result.get('defaultProjectReady')}")
    print(f"default universe ready:{result.get('defaultUniverseReady')}")
    print(f"default world ready:   {result.get('defaultWorldReady')}")
    print(f"block registry ready:  {result.get('blockRegistryReady')}")
    print(f"debug blocks ready:    {result.get('debugBlocksReady')}")
    print(f"project access ready:  {result.get('projectAccessReady')}")
    print(f"project owner:         {result.get('projectOwnerAuthUserId')}")
    print(f"system blocks ready:   {result.get('systemBlocksReady')}")
    print(f"Air invariant ready:   {result.get('airInvariantReady')}")
    print(f"system railing ready:  {result.get('systemRailingReady')}")
    print(f"system block count:    {result.get('systemBlockCount')}")
    print(f"system blocks created: {result.get('systemBlocksCreated')}")
    print(f"system blocks updated: {result.get('systemBlocksUpdated')}")
    print(f"system blocks missing: {result.get('systemBlocksMissing')}")
    print(f"system blocks drifted: {result.get('systemBlocksDrifted')}")
    print(f"schema requested:      {result.get('schemaBootstrapRequested')}")
    print(f"schema executed:       {result.get('schemaBootstrapExecuted')}")
    print(f"schema ok:             {result.get('schemaBootstrapOk')}")
    print(f"repair requested:      {result.get('schemaRepairRequested')}")
    print(f"repair executed:       {result.get('schemaRepairExecuted')}")
    print(f"repair ok:             {result.get('schemaRepairOk')}")
    print(f"seed requested:        {result.get('seedBootstrapRequested')}")
    print(f"seed executed:         {result.get('seedBootstrapExecuted')}")
    print(f"seed ok:               {result.get('seedBootstrapOk')}")
    print(f"seed repair executed:  {result.get('seedInvariantRepairExecuted')}")
    print(f"seed repair ok:        {result.get('seedInvariantRepairOk')}")
    print(f"warnings:              {result.get('warningCount')}")
    print(f"errors:                {result.get('errorCount')}")
    print("")

    errors = result.get("errors") or []
    if errors:
        print("Errors:")
        for item in errors:
            if isinstance(item, Mapping):
                message = _safe_str(item.get("message"), "")
                code = _safe_str(item.get("code"), "")
            else:
                message = _safe_str(item, "")
                code = ""
            print(f"  - {code}: {message}")
        print("")

    warnings = result.get("warnings") or []
    if warnings:
        print("Warnings:")
        for item in warnings:
            if isinstance(item, Mapping):
                message = _safe_str(item.get("message"), "")
                code = _safe_str(item.get("code"), "")
            else:
                message = _safe_str(item, "")
                code = ""
            print(f"  - {code}: {message}")
        print("")


# -----------------------------------------------------------------------------
# Path/bootstrap helpers
# -----------------------------------------------------------------------------

def resolve_service_root() -> Path:
    """
    Resolve service root.

    Expected:
        services/vectoplan-chunk/scripts/bootstrap_db.py

    parents[0] -> scripts
    parents[1] -> vectoplan-chunk
    """
    try:
        return Path(__file__).resolve().parents[1]
    except Exception:
        return Path.cwd()


def configure_python_path(service_root: Path) -> None:
    """Ensure service root is importable."""
    try:
        root_text = str(service_root)
    except Exception:
        root_text = ""

    if root_text and root_text not in sys.path:
        sys.path.insert(0, root_text)


def set_env_value(name: str, value: str, *, override: bool = False) -> None:
    """Set environment value with optional override."""
    if override or name not in os.environ:
        os.environ[name] = value


def _looks_like_provider_or_template_world_id(value: str) -> bool:
    """Return whether a world id looks like provider/template id rather than concrete world id."""
    text = _safe_str(value, "").lower()
    if not text:
        return False

    template_id = _env_str("VECTOPLAN_CHUNK_DEFAULT_TEMPLATE_ID", DEFAULT_TEMPLATE_ID).lower()
    provider_id = _env_str("VECTOPLAN_CHUNK_DEFAULT_PROVIDER_ID", DEFAULT_PROVIDER_ID).lower()
    provider_world_id = _env_str(
        "VECTOPLAN_CHUNK_DEFAULT_PROVIDER_WORLD_ID",
        DEFAULT_PROVIDER_WORLD_ID,
    ).lower()

    return text in {
        DEFAULT_TEMPLATE_ID,
        DEFAULT_PROVIDER_ID,
        DEFAULT_PROVIDER_WORLD_ID,
        template_id,
        provider_id,
        provider_world_id,
    }


def _resolve_concrete_default_world_id() -> str:
    """
    Resolve concrete editable default world id.

    Defensive rule:
    If VECTOPLAN_CHUNK_DEFAULT_WORLD_ID is accidentally "flat", ignore it and
    use VECTOPLAN_CHUNK_DEFAULT_INSTANCE_WORLD_ID or world_spawn.
    """
    explicit_instance = _env_str_any(
        (
            "VECTOPLAN_CHUNK_DEFAULT_INSTANCE_WORLD_ID",
            "VECTOPLAN_CHUNK_DEFAULT_WORLD_INSTANCE_ID",
            "VECTOPLAN_CHUNK_DEFAULT_SPAWN_WORLD_ID",
        ),
        "",
    )
    if explicit_instance:
        return explicit_instance

    default_world = _env_str("VECTOPLAN_CHUNK_DEFAULT_WORLD_ID", "")
    if default_world and not _looks_like_provider_or_template_world_id(default_world):
        return default_world

    return DEFAULT_WORLD_ID


def _ensure_world_id_env_defaults() -> None:
    """Ensure concrete-world/template/provider env values are internally consistent."""
    concrete_world_id = _resolve_concrete_default_world_id()

    set_env_value("VECTOPLAN_CHUNK_DEFAULT_INSTANCE_WORLD_ID", concrete_world_id, override=True)
    set_env_value("VECTOPLAN_CHUNK_DEFAULT_WORLD_ID", concrete_world_id, override=True)
    set_env_value("VECTOPLAN_CHUNK_DEFAULT_TEMPLATE_ID", _env_str("VECTOPLAN_CHUNK_DEFAULT_TEMPLATE_ID", DEFAULT_TEMPLATE_ID), override=False)
    set_env_value("VECTOPLAN_CHUNK_DEFAULT_PROVIDER_ID", _env_str("VECTOPLAN_CHUNK_DEFAULT_PROVIDER_ID", DEFAULT_PROVIDER_ID), override=False)
    set_env_value("VECTOPLAN_CHUNK_DEFAULT_PROVIDER_WORLD_ID", _env_str("VECTOPLAN_CHUNK_DEFAULT_PROVIDER_WORLD_ID", DEFAULT_PROVIDER_WORLD_ID), override=False)
    set_env_value("VECTOPLAN_CHUNK_DEFAULT_PROJECT_ID", _env_str("VECTOPLAN_CHUNK_DEFAULT_PROJECT_ID", DEFAULT_PROJECT_ID), override=False)
    set_env_value("VECTOPLAN_CHUNK_DEFAULT_UNIVERSE_ID", _env_str("VECTOPLAN_CHUNK_DEFAULT_UNIVERSE_ID", DEFAULT_UNIVERSE_ID), override=False)
    set_env_value("VECTOPLAN_CHUNK_DEFAULT_BLOCK_REGISTRY_ID", _env_str("VECTOPLAN_CHUNK_DEFAULT_BLOCK_REGISTRY_ID", DEFAULT_BLOCK_REGISTRY_ID), override=False)
    set_env_value("VECTOPLAN_CHUNK_DEFAULT_BLOCK_REGISTRY_VERSION", _env_str("VECTOPLAN_CHUNK_DEFAULT_BLOCK_REGISTRY_VERSION", DEFAULT_BLOCK_REGISTRY_VERSION), override=False)


def set_default_env(
    *,
    create_all: bool,
    seed: bool,
    check_only: bool,
    mode: str,
    force_runtime_hooks_off: bool,
    repair_missing_columns: bool,
    repair_seed_invariants: bool,
) -> None:
    """
    Set safe default env values before app import.

    Existing environment values are respected unless safety-critical.

    Safety rule:
        normal runtime startup hooks must not perform DB mutation while this
        script creates the Flask app. Bootstrap happens explicitly after app
        creation.
    """
    effective_mode = "check-only" if check_only else mode or "db-bootstrap"

    _ensure_world_id_env_defaults()
    _ensure_project_owner_env_defaults(
        seed_project_access=bool(seed and not check_only),
    )

    set_env_value("VECTOPLAN_CHUNK_MODE", effective_mode, override=True)
    set_env_value("VECTOPLAN_CHUNK_STARTUP_MODE", effective_mode, override=True)
    set_env_value("VECTOPLAN_CHUNK_RUNTIME_MODE", effective_mode, override=True)
    set_env_value("VECTOPLAN_CHUNK_RUN_MODE", effective_mode, override=True)
    set_env_value("SERVICE_STARTUP_MODE", effective_mode, override=False)
    set_env_value("APP_STARTUP_MODE", effective_mode, override=False)
    set_env_value("STARTUP_MODE", effective_mode, override=False)

    if check_only:
        set_env_value("VECTOPLAN_CHUNK_DB_BOOTSTRAP_ENABLED", "false", override=True)
        set_env_value("VECTOPLAN_CHUNK_ALLOW_RUNTIME_DB_MUTATIONS", "false", override=True)
        set_env_value("VECTOPLAN_CHUNK_RUNTIME_IS_READ_ONLY", "true", override=True)
        set_env_value("VECTOPLAN_CHUNK_AUTO_CREATE_ALL", "false", override=True)
        set_env_value("VECTOPLAN_CHUNK_AUTO_SEED_DEFAULTS", "false", override=True)
        set_env_value("VECTOPLAN_CHUNK_SEED_DEBUG_BLOCKS", "false", override=True)
        set_env_value("VECTOPLAN_CHUNK_SEED_SYSTEM_BLOCKS", "false", override=True)
        set_env_value("VECTOPLAN_CHUNK_SEED_DEV_PROJECT", "false", override=True)
        set_env_value("VECTOPLAN_CHUNK_SEED_PROJECT_ACCESS", "false", override=True)
        set_env_value("VECTOPLAN_CHUNK_DB_BOOTSTRAP_SEED_PROJECT_ACCESS", "false", override=True)
        set_env_value("VECTOPLAN_CHUNK_DB_BOOTSTRAP_SEED_SYSTEM_BLOCKS", "false", override=True)
        set_env_value("VECTOPLAN_CHUNK_DB_BOOTSTRAP_REPAIR_SEED_INVARIANTS", "false", override=True)
    else:
        set_env_value("VECTOPLAN_CHUNK_DB_BOOTSTRAP_ENABLED", "true", override=True)
        set_env_value("VECTOPLAN_CHUNK_ALLOW_RUNTIME_DB_MUTATIONS", "true", override=True)
        set_env_value("VECTOPLAN_CHUNK_RUNTIME_IS_READ_ONLY", "false", override=True)
        set_env_value("VECTOPLAN_CHUNK_AUTO_CREATE_ALL", "true" if create_all else "false", override=True)
        set_env_value("VECTOPLAN_CHUNK_AUTO_SEED_DEFAULTS", "true" if seed else "false", override=True)
        set_env_value("VECTOPLAN_CHUNK_SEED_DEBUG_BLOCKS", "true" if seed else "false", override=True)
        set_env_value("VECTOPLAN_CHUNK_SEED_SYSTEM_BLOCKS", "true" if seed else "false", override=True)
        set_env_value("VECTOPLAN_CHUNK_SEED_DEV_PROJECT", "true" if seed else "false", override=True)
        set_env_value("VECTOPLAN_CHUNK_SEED_PROJECT_ACCESS", "true" if seed else "false", override=True)
        set_env_value("VECTOPLAN_CHUNK_DB_BOOTSTRAP_SEED_PROJECT_ACCESS", "true" if seed else "false", override=True)
        set_env_value("VECTOPLAN_CHUNK_DB_BOOTSTRAP_SEED_SYSTEM_BLOCKS", "true" if seed else "false", override=True)
        set_env_value("VECTOPLAN_CHUNK_DB_BOOTSTRAP_REPAIR_SEED_INVARIANTS", "true" if repair_seed_invariants else "false", override=True)

    set_env_value("VECTOPLAN_CHUNK_DB_BOOTSTRAP_CREATE_ALL", "true" if create_all else "false", override=True)
    set_env_value("VECTOPLAN_CHUNK_DB_BOOTSTRAP_SEED_DEFAULTS", "true" if seed else "false", override=True)
    set_env_value("VECTOPLAN_CHUNK_DB_BOOTSTRAP_SEED_DEBUG_BLOCKS", "true" if seed else "false", override=True)
    set_env_value("VECTOPLAN_CHUNK_DB_BOOTSTRAP_SEED_SYSTEM_BLOCKS", "true" if seed else "false", override=True)
    set_env_value("VECTOPLAN_CHUNK_DB_BOOTSTRAP_SEED_DEV_PROJECT", "true" if seed else "false", override=True)
    set_env_value("VECTOPLAN_CHUNK_DB_BOOTSTRAP_SEED_PROJECT_ACCESS", "true" if seed else "false", override=True)
    set_env_value("VECTOPLAN_CHUNK_BOOTSTRAP_REPAIR_MISSING_COLUMNS", "true" if repair_missing_columns else "false", override=False)

    set_env_value("VECTOPLAN_CHUNK_SEED_ON_EMPTY_ONLY", "true", override=False)
    set_env_value("VECTOPLAN_CHUNK_DB_BOOTSTRAP_ADVISORY_LOCKS", "true", override=False)
    set_env_value("VECTOPLAN_CHUNK_DB_BOOTSTRAP_FAIL_ON_ERROR", "true", override=False)

    if force_runtime_hooks_off:
        os.environ["VECTOPLAN_CHUNK_RUN_STARTUP_HOOKS"] = "false"
    else:
        set_env_value("VECTOPLAN_CHUNK_RUN_STARTUP_HOOKS", "false", override=False)


def create_flask_app(
    *,
    app_factory: str,
    config_name: str | None = None,
) -> Any:
    """
    Import and create Flask app.

    app_factory syntax:
        app:create_app
        wsgi:app
        wsgi:application

    If the target is callable, it is called.
    If it is already an app object, it is returned.
    """
    module_name, sep, attr_name = app_factory.partition(":")

    if not sep or not module_name or not attr_name:
        raise ValueError(
            f"Invalid app factory '{app_factory}'. Expected format 'module:attribute'."
        )

    module = importlib.import_module(module_name)
    target = getattr(module, attr_name)

    if callable(target):
        try:
            if config_name:
                return target(config_name)
        except TypeError:
            pass

        try:
            return target()
        except TypeError:
            return target

    return target


def _has_app_context() -> bool:
    """Return whether a Flask app context is currently active."""
    try:
        from flask import has_app_context

        return bool(has_app_context())
    except Exception:
        return False


# -----------------------------------------------------------------------------
# SQLAlchemy schema helpers
# -----------------------------------------------------------------------------

def _get_db() -> Any:
    """Import and return Flask-SQLAlchemy db."""
    from extensions import db

    return db


def _get_model_classes() -> dict[str, Any]:
    """Return model classes from models registry."""
    try:
        from models import get_model_class_map

        result = get_model_class_map()
        if isinstance(result, Mapping):
            return dict(result)
    except Exception:
        pass

    from models import (
        BlockRegistry,
        BlockType,
        ChunkEvent,
        ChunkSnapshot,
        Project,
        ProjectAccessAssignment,
        ProjectGroup,
        ProjectGroupMember,
        ProjectRole,
        ProjectRoleAssignment,
        Universe,
        WorldCommandLog,
        WorldInstance,
        WorldObjectChunkRef,
        WorldObjectInstance,
    )

    return {
        "Project": Project,
        "ProjectAccessAssignment": ProjectAccessAssignment,
        "ProjectRole": ProjectRole,
        "ProjectGroup": ProjectGroup,
        "ProjectGroupMember": ProjectGroupMember,
        "ProjectRoleAssignment": ProjectRoleAssignment,
        "Universe": Universe,
        "WorldInstance": WorldInstance,
        "BlockRegistry": BlockRegistry,
        "BlockType": BlockType,
        "ChunkSnapshot": ChunkSnapshot,
        "WorldCommandLog": WorldCommandLog,
        "ChunkEvent": ChunkEvent,
        "WorldObjectInstance": WorldObjectInstance,
        "WorldObjectChunkRef": WorldObjectChunkRef,
    }


def _model_table(model_class: Any) -> Any | None:
    """Return SQLAlchemy table from model class."""
    try:
        return getattr(model_class, "__table__", None)
    except Exception:
        return None


def _model_table_name(model_class: Any) -> str | None:
    """Return table name from model class."""
    try:
        table = _model_table(model_class)
        if table is not None:
            return str(table.name)
    except Exception:
        pass

    try:
        return str(getattr(model_class, "__tablename__"))
    except Exception:
        return None


def _model_columns(model_class: Any) -> set[str]:
    """Return model column names."""
    table = _model_table(model_class)
    if table is None:
        return set()

    try:
        return {str(column.name) for column in table.columns}
    except Exception:
        return set()


def _model_has_column(model_class: Any, column_name: str) -> bool:
    """Return whether model has a mapped column."""
    return column_name in _model_columns(model_class)


def _set_attr_if_supported(instance: Any, name: str, value: Any) -> bool:
    """Set attribute if supported by model."""
    if instance is None:
        return False

    if not hasattr(instance, name) and not _model_has_column(instance.__class__, name):
        return False

    try:
        setattr(instance, name, value)
        return True
    except Exception:
        return False


def _set_attr_if_empty(instance: Any, name: str, value: Any) -> bool:
    """Set attribute if currently empty."""
    try:
        current = getattr(instance, name, None)
    except Exception:
        current = None

    if current not in (None, "", {}, []):
        return False

    return _set_attr_if_supported(instance, name, value)


def _merge_metadata_json(instance: Any, payload: Mapping[str, Any]) -> None:
    """Merge metadata_json if supported."""
    if instance is None:
        return

    if not hasattr(instance, "metadata_json") and not _model_has_column(instance.__class__, "metadata_json"):
        return

    try:
        existing = getattr(instance, "metadata_json", None)
    except Exception:
        existing = None

    if isinstance(existing, Mapping):
        merged = dict(existing)
    else:
        merged = {}

    merged.update(dict(payload))

    try:
        setattr(instance, "metadata_json", merged)
    except Exception:
        pass


def _inspect_database_schema_inner() -> dict[str, Any]:
    """Inspect database schema. Requires an active app context."""
    db = _get_db()
    model_classes = _get_model_classes()

    try:
        from sqlalchemy import inspect

        inspector = inspect(db.engine)
        db_table_names = set(inspector.get_table_names())
    except Exception as exc:
        return {
            "ok": False,
            "status": "inspection_failed",
            "error": _safe_exception_message(exc),
            "tables": {},
            "missingTables": [],
            "missingColumns": {},
        }

    tables: dict[str, Any] = {}
    missing_tables: list[str] = []
    missing_columns: dict[str, list[str]] = {}

    for class_name, model_class in model_classes.items():
        if model_class is None:
            continue

        table = _model_table(model_class)
        table_name = _model_table_name(model_class)

        if table is None or table_name is None:
            continue

        model_columns = [str(column.name) for column in table.columns]

        if table_name not in db_table_names:
            missing_tables.append(table_name)
            tables[table_name] = {
                "modelClass": class_name,
                "exists": False,
                "modelColumns": model_columns,
                "databaseColumns": [],
                "missingColumns": model_columns,
            }
            continue

        try:
            database_columns = [
                str(column["name"])
                for column in inspector.get_columns(table_name)
            ]
        except Exception:
            database_columns = []

        database_column_set = set(database_columns)
        missing = [
            column_name
            for column_name in model_columns
            if column_name not in database_column_set
        ]

        if missing:
            missing_columns[table_name] = missing

        tables[table_name] = {
            "modelClass": class_name,
            "exists": True,
            "modelColumns": model_columns,
            "databaseColumns": database_columns,
            "missingColumns": missing,
        }

    ok = not missing_tables and not missing_columns

    return {
        "ok": ok,
        "status": "ready" if ok else "schema_drift",
        "tables": tables,
        "missingTables": sorted(set(missing_tables)),
        "missingColumns": missing_columns,
    }


def _inspect_database_schema(app: Any) -> dict[str, Any]:
    """Inspect database tables/columns against SQLAlchemy model metadata."""
    if _has_app_context():
        return _inspect_database_schema_inner()

    with app.app_context():
        return _inspect_database_schema_inner()


def _compile_column_type(column: Any, dialect: Any) -> str:
    """Compile SQLAlchemy column type for DDL."""
    try:
        return column.type.compile(dialect=dialect)
    except Exception:
        return str(column.type)


def _quote_identifier(dialect: Any, name: str) -> str:
    """Quote SQL identifier for current dialect."""
    try:
        return dialect.identifier_preparer.quote(name)
    except Exception:
        return f'"{name}"'


def _scalar_default_value(column: Any) -> Any:
    """Return scalar Python default value for a column when safe."""
    try:
        default = getattr(column, "default", None)
        if default is None:
            return None

        if getattr(default, "is_scalar", False):
            return default.arg
    except Exception:
        return None

    return None


def _repair_missing_columns_inner(*, dry_run: bool = False) -> dict[str, Any]:
    """
    Best-effort local/dev schema repair.

    Requires active app context.
    """
    db = _get_db()
    model_classes = _get_model_classes()

    result: dict[str, Any] = {
        "ok": True,
        "dryRun": bool(dry_run),
        "executed": False,
        "addedColumns": [],
        "skippedColumns": [],
        "errors": [],
        "warnings": [],
    }

    before = _inspect_database_schema_inner()
    missing_columns = before.get("missingColumns") or {}

    if not missing_columns:
        result["status"] = "nothing_to_repair"
        return result

    try:
        from sqlalchemy import inspect, text
    except Exception as exc:
        result["ok"] = False
        result["status"] = "sqlalchemy_import_failed"
        result["errors"].append(
            {
                "code": "sqlalchemy_import_failed",
                "message": _safe_exception_message(exc),
            }
        )
        return result

    try:
        engine = db.engine
        dialect = engine.dialect
        inspector = inspect(engine)
        existing_tables = set(inspector.get_table_names())
    except Exception as exc:
        result["ok"] = False
        result["status"] = "engine_unavailable"
        result["errors"].append(
            {
                "code": "engine_unavailable",
                "message": _safe_exception_message(exc),
            }
        )
        return result

    table_to_model: dict[str, Any] = {}
    for model_class in model_classes.values():
        if model_class is None:
            continue
        table_name = _model_table_name(model_class)
        if table_name:
            table_to_model[table_name] = model_class

    try:
        with engine.begin() as connection:
            for table_name, column_names in missing_columns.items():
                model_class = table_to_model.get(table_name)
                table = _model_table(model_class)

                if table is None:
                    continue

                if table_name not in existing_tables:
                    result["skippedColumns"].append(
                        {
                            "table": table_name,
                            "reason": "table_missing",
                            "columns": list(column_names),
                        }
                    )
                    continue

                quoted_table = _quote_identifier(dialect, table_name)

                for column_name in column_names:
                    column = table.columns.get(column_name)
                    if column is None:
                        result["skippedColumns"].append(
                            {
                                "table": table_name,
                                "column": column_name,
                                "reason": "column_not_in_model_table",
                            }
                        )
                        continue

                    if getattr(column, "primary_key", False):
                        result["skippedColumns"].append(
                            {
                                "table": table_name,
                                "column": column_name,
                                "reason": "primary_key_not_repaired",
                            }
                        )
                        continue

                    column_type_sql = _compile_column_type(column, dialect)
                    quoted_column = _quote_identifier(dialect, column_name)
                    add_sql = f"ALTER TABLE {quoted_table} ADD COLUMN {quoted_column} {column_type_sql}"

                    if dry_run:
                        result["addedColumns"].append(
                            {
                                "table": table_name,
                                "column": column_name,
                                "ddl": add_sql,
                                "dryRun": True,
                            }
                        )
                        continue

                    try:
                        connection.execute(text(add_sql))
                        result["executed"] = True

                        default_value = _scalar_default_value(column)
                        default_applied = False

                        if default_value is not None and isinstance(default_value, (str, int, float, bool)):
                            update_sql = f"UPDATE {quoted_table} SET {quoted_column} = :value WHERE {quoted_column} IS NULL"
                            connection.execute(text(update_sql), {"value": default_value})
                            default_applied = True

                        not_null_applied = False
                        if not getattr(column, "nullable", True):
                            null_count_sql = f"SELECT COUNT(*) FROM {quoted_table} WHERE {quoted_column} IS NULL"
                            null_count = connection.execute(text(null_count_sql)).scalar()
                            if int(null_count or 0) == 0:
                                try:
                                    connection.execute(
                                        text(
                                            f"ALTER TABLE {quoted_table} ALTER COLUMN {quoted_column} SET NOT NULL"
                                        )
                                    )
                                    not_null_applied = True
                                except Exception as exc:
                                    result["warnings"].append(
                                        {
                                            "code": "set_not_null_failed",
                                            "message": _safe_exception_message(exc),
                                            "table": table_name,
                                            "column": column_name,
                                        }
                                    )

                        result["addedColumns"].append(
                            {
                                "table": table_name,
                                "column": column_name,
                                "type": column_type_sql,
                                "defaultApplied": default_applied,
                                "notNullApplied": not_null_applied,
                            }
                        )

                    except Exception as exc:
                        result["ok"] = False
                        result["errors"].append(
                            {
                                "code": "add_column_failed",
                                "message": _safe_exception_message(exc),
                                "table": table_name,
                                "column": column_name,
                                "ddl": add_sql,
                            }
                        )

    except Exception as exc:
        result["ok"] = False
        result["errors"].append(
            {
                "code": "schema_repair_transaction_failed",
                "message": _safe_exception_message(exc),
            }
        )

    after = _inspect_database_schema_inner()
    result["before"] = before
    result["after"] = after
    result["remainingMissingColumns"] = after.get("missingColumns") or {}
    result["status"] = "repaired" if result["ok"] and not result["remainingMissingColumns"] else "repair_incomplete"

    if result["remainingMissingColumns"]:
        result["warnings"].append(
            {
                "code": "remaining_missing_columns",
                "message": "Some model columns are still missing after schema repair.",
                "details": result["remainingMissingColumns"],
            }
        )

    return result


def _repair_missing_columns(app: Any, *, dry_run: bool = False) -> dict[str, Any]:
    """Best-effort local/dev schema repair."""
    if _has_app_context():
        return _repair_missing_columns_inner(dry_run=dry_run)

    with app.app_context():
        return _repair_missing_columns_inner(dry_run=dry_run)


def _direct_create_all_inner() -> dict[str, Any]:
    """Run direct db.create_all() fallback. Requires active app context."""
    db = _get_db()

    db.create_all()
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise

    return {
        "ok": True,
        "status": "create_all_completed",
        "executed": True,
    }


def _direct_create_all(app: Any) -> dict[str, Any]:
    """Run direct db.create_all() fallback."""
    if _has_app_context():
        return _direct_create_all_inner()

    with app.app_context():
        return _direct_create_all_inner()


def _query_first_by_fields(session: Any, model: Any, **fields: Any) -> Any | None:
    """Best-effort multi-field query helper."""
    if session is None or model is None:
        return None

    filters = {
        key: value
        for key, value in fields.items()
        if value is not None and _model_has_column(model, key)
    }

    if not filters:
        return None

    try:
        query = session.query(model)
        for key, value in filters.items():
            query = query.filter(getattr(model, key) == value)
        return query.first()
    except Exception:
        try:
            return session.query(model).filter_by(**filters).first()
        except Exception:
            return None


def _query_first_by_field(session: Any, model: Any, field_name: str, value: Any) -> Any | None:
    """Best-effort query helper."""
    return _query_first_by_fields(session, model, **{field_name: value})


def _project_owner_from_model(project: Any, fallback: str) -> str:
    """Return a canonical persisted project owner, falling back from legacy placeholders."""
    for name in (
        "owner_auth_user_id",
        "owner_id",
        "owner_user_id",
        "created_by_auth_user_id",
        "created_by_user_id",
    ):
        try:
            value = getattr(project, name, None)
        except Exception:
            value = None
        normalized = _normalize_auth_user_id(value, "")
        if normalized:
            return normalized
    return _normalize_auth_user_id(fallback, DEFAULT_PROJECT_OWNER_AUTH_USER_ID)


def _apply_project_owner_fields(project: Any, owner_auth_user_id: str) -> str:
    """Repair all compatible owner/actor fields to one canonical auth identity."""
    owner = _normalize_auth_user_id(
        owner_auth_user_id,
        DEFAULT_PROJECT_OWNER_AUTH_USER_ID,
    )
    setter = getattr(project, "set_owner_user", None)
    if callable(setter):
        try:
            setter(owner, updated_by_user_id=owner)
        except TypeError:
            try:
                setter(owner)
            except Exception:
                pass
        except Exception:
            pass

    _set_attr_if_supported(project, "owner_type", "user")
    for field_name in (
        "owner_auth_user_id",
        "owner_id",
        "owner_user_id",
        "created_by_auth_user_id",
        "updated_by_auth_user_id",
        "created_by_user_id",
        "updated_by_user_id",
    ):
        _set_attr_if_supported(project, field_name, owner)
    return owner


def _project_access_projection_fingerprint(
    project_id: str,
    owner_auth_user_id: str,
) -> str:
    """Build the stable one-owner bootstrap projection fingerprint."""
    canonical = json.dumps(
        {
            "assignments": [
                {
                    "active": True,
                    "assignment_type": DEFAULT_PROJECT_ACCESS_ASSIGNMENT_TYPE,
                    "auth_user_id": owner_auth_user_id,
                    "role": DEFAULT_PROJECT_ACCESS_ROLE,
                }
            ],
            "chunk_project_id": project_id,
            "projection_version": DEFAULT_PROJECT_ACCESS_PROJECTION_VERSION,
            "source_service": DEFAULT_PROJECT_ACCESS_SOURCE_SERVICE,
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _result_to_dict_with_variants(value: Any) -> dict[str, Any]:
    """Serialize service result objects whose to_dict signatures differ by generation."""
    data = _to_plain_dict(value)
    if data:
        return data
    serializer = getattr(value, "to_dict", None)
    if not callable(serializer):
        return {}
    for kwargs in (
        {"include_internal": True, "include_metadata": False},
        {"include_private": False},
        {},
    ):
        try:
            candidate = serializer(**kwargs)
        except TypeError:
            continue
        except Exception:
            return {}
        if isinstance(candidate, Mapping):
            return dict(candidate)
    return {}


def _initialize_canonical_project_access_inner(
    project: Any,
    owner_auth_user_id: str,
) -> dict[str, Any]:
    """Initialize the canonical ProjectAccessAssignment projection without committing."""
    db = _get_db()
    models = _get_model_classes()
    assignment_model = models.get("ProjectAccessAssignment")
    project_model = models.get("Project") or project.__class__
    project_id = _safe_str(getattr(project, "project_id", None), DEFAULT_PROJECT_ID)
    owner = _normalize_auth_user_id(owner_auth_user_id)
    fingerprint = _project_access_projection_fingerprint(project_id, owner)
    request_id = f"bootstrap-{project_id}"
    correlation_id = request_id
    service_errors: list[str] = []

    if assignment_model is None:
        raise RuntimeError("ProjectAccessAssignment model is unavailable.")

    for module_name in (
        "src.services.project_access_service",
        "services.project_access_service",
        "project_access_service",
    ):
        try:
            module = importlib.import_module(module_name)
            initialize = getattr(module, "initialize_project_access", None)
            if not callable(initialize):
                raise RuntimeError("initialize_project_access export is unavailable")
            payload = {
                "owner_auth_user_id": owner,
                "assignments": [
                    {
                        "auth_user_id": owner,
                        "role": DEFAULT_PROJECT_ACCESS_ROLE,
                        "assignment_type": DEFAULT_PROJECT_ACCESS_ASSIGNMENT_TYPE,
                        "active": True,
                        "managed": True,
                        "source_service": DEFAULT_PROJECT_ACCESS_SOURCE_SERVICE,
                    }
                ],
                "source_service": DEFAULT_PROJECT_ACCESS_SOURCE_SERVICE,
                "projection_version": DEFAULT_PROJECT_ACCESS_PROJECTION_VERSION,
            }
            principal = {
                "service_id": DEFAULT_PROJECT_ACCESS_SERVICE_ID,
                "authenticated": True,
                "exempt": False,
                "request_id": request_id,
                "correlation_id": correlation_id,
            }
            config = {
                "VECTOPLAN_CHUNK_ACCESS_CONTROL_ENABLED": True,
                "VECTOPLAN_CHUNK_ACCESS_DEFAULT_DENY": True,
                "VECTOPLAN_CHUNK_ACCESS_SOURCE_SERVICE": DEFAULT_PROJECT_ACCESS_SOURCE_SERVICE,
                "VECTOPLAN_CHUNK_ACCESS_PROJECTION_VERSION": DEFAULT_PROJECT_ACCESS_PROJECTION_VERSION,
                "VECTOPLAN_CHUNK_ACCESS_PRUNE_STALE_DIRECT_ASSIGNMENTS": True,
                "VECTOPLAN_CHUNK_ACCESS_PRESERVE_GROUP_ASSIGNMENTS": True,
                "VECTOPLAN_CHUNK_ACCESS_VERIFY_AFTER_SYNC": True,
                "VECTOPLAN_CHUNK_RUNTIME_BUSINESS_MUTATIONS_ENABLED": True,
                "VECTOPLAN_CHUNK_SERVICE_AUTH_REQUIRED": True,
            }
            result = initialize(
                project_id,
                payload,
                session=db.session,
                assignment_model=assignment_model,
                project_model=project_model,
                principal=principal,
                request_id=request_id,
                correlation_id=correlation_id,
                idempotency_key=f"bootstrap:{project_id}:{fingerprint}",
                commit=False,
                dry_run=False,
                force=True,
                raise_on_error=True,
                config=config,
            )
            data = _result_to_dict_with_variants(result)
            if data and not _safe_bool(data.get("ok"), False):
                raise RuntimeError(
                    _safe_str(data.get("error"), data.get("code") or "canonical access sync failed")
                )
            db.session.flush()
            return {
                "ok": True,
                "backend": module_name,
                "result": data,
                "projectionFingerprint": fingerprint,
                "ownerAuthUserId": owner,
            }
        except Exception as exc:
            service_errors.append(
                f"{module_name}: {exc.__class__.__name__}: {_safe_exception_message(exc)}"
            )

    # Direct ORM fallback: preserve groups and non-owner direct members, but ensure
    # exactly one active canonical owner for the development project.
    query = db.session.query(assignment_model)
    try:
        rows = query.filter(
            getattr(assignment_model, "chunk_project_id") == project_id
        ).all()
    except Exception:
        rows = query.all()
        rows = [
            row
            for row in rows
            if _safe_str(getattr(row, "chunk_project_id", ""), "") == project_id
        ]

    owner_row = None
    demoted_count = 0
    for row in rows:
        assignment_type = _safe_str(
            getattr(row, "assignment_type", DEFAULT_PROJECT_ACCESS_ASSIGNMENT_TYPE),
            DEFAULT_PROJECT_ACCESS_ASSIGNMENT_TYPE,
        ).lower()
        if assignment_type == "group":
            continue
        row_owner = _normalize_auth_user_id(getattr(row, "auth_user_id", None), "")
        row_role = _safe_str(getattr(row, "role", ""), "").lower()
        row_active = _safe_bool(getattr(row, "active", True), True)
        if row_owner == owner:
            owner_row = row
            continue
        if row_role == DEFAULT_PROJECT_ACCESS_ROLE and row_active:
            _set_attr_if_supported(row, "role", "admin")
            _set_attr_if_supported(row, "updated_at", _utc_now())
            db.session.add(row)
            demoted_count += 1

    created = False
    if owner_row is None:
        factory = getattr(assignment_model, "create_direct", None)
        if callable(factory):
            owner_row = factory(
                chunk_project_id=project_id,
                auth_user_id=owner,
                role=DEFAULT_PROJECT_ACCESS_ROLE,
                active=True,
                managed=True,
                source_service=DEFAULT_PROJECT_ACCESS_SOURCE_SERVICE,
                projection_version=DEFAULT_PROJECT_ACCESS_PROJECTION_VERSION,
                projection_fingerprint=fingerprint,
                request_id=request_id,
                correlation_id=correlation_id,
                metadata_json={
                    "seededBy": "bootstrap_db.py",
                    "bootstrapService": DEFAULT_PROJECT_ACCESS_SERVICE_ID,
                },
            )
        else:
            owner_row = assignment_model(
                chunk_project_id=project_id,
                auth_user_id=owner,
                group_id=None,
                role=DEFAULT_PROJECT_ACCESS_ROLE,
                assignment_type=DEFAULT_PROJECT_ACCESS_ASSIGNMENT_TYPE,
                active=True,
                managed=True,
                source_service=DEFAULT_PROJECT_ACCESS_SOURCE_SERVICE,
                projection_version=DEFAULT_PROJECT_ACCESS_PROJECTION_VERSION,
                projection_fingerprint=fingerprint,
                request_id=request_id,
                correlation_id=correlation_id,
                metadata_json={"seededBy": "bootstrap_db.py"},
            )
        db.session.add(owner_row)
        created = True
    else:
        for field_name, value in (
            ("role", DEFAULT_PROJECT_ACCESS_ROLE),
            ("assignment_type", DEFAULT_PROJECT_ACCESS_ASSIGNMENT_TYPE),
            ("active", True),
            ("managed", True),
            ("source_service", DEFAULT_PROJECT_ACCESS_SOURCE_SERVICE),
            ("projection_version", DEFAULT_PROJECT_ACCESS_PROJECTION_VERSION),
            ("projection_fingerprint", fingerprint),
            ("request_id", request_id),
            ("correlation_id", correlation_id),
            ("deactivated_at", None),
            ("updated_at", _utc_now()),
        ):
            _set_attr_if_supported(owner_row, field_name, value)
        db.session.add(owner_row)

    db.session.flush()
    now = _utc_now()
    for field_name, value in (
        ("access_sync_status", "ready"),
        ("access_projection_version", DEFAULT_PROJECT_ACCESS_PROJECTION_VERSION),
        ("access_projection_fingerprint", fingerprint),
        ("access_sync_request_id", request_id),
        ("access_sync_correlation_id", correlation_id),
        ("access_sync_error_code", None),
        ("access_sync_retryable", False),
        ("access_sync_repair_required", False),
        ("access_synced_at", now),
        ("access_sync_updated_at", now),
    ):
        _set_attr_if_supported(project, field_name, value)
    db.session.add(project)
    db.session.flush()
    return {
        "ok": True,
        "backend": "direct_project_access_assignment",
        "created": created,
        "demotedExtraOwners": demoted_count,
        "projectionFingerprint": fingerprint,
        "ownerAuthUserId": owner,
        "serviceErrors": service_errors,
    }


def _initialize_legacy_project_access_inner(
    project: Any,
    owner_auth_user_id: str,
) -> dict[str, Any]:
    """Maintain legacy role/group rows while the canonical projection is adopted."""
    models = _get_model_classes()
    legacy_names = (
        "ProjectRole",
        "ProjectGroup",
        "ProjectGroupMember",
        "ProjectRoleAssignment",
    )
    legacy_models_present = all(models.get(name) is not None for name in legacy_names)
    if not legacy_models_present:
        return {
            "ok": True,
            "skipped": True,
            "reason": "legacy access models are not installed",
        }

    import_errors: list[str] = []
    for module_name in ("src.project_access", "project_access"):
        try:
            module = importlib.import_module(module_name)
            initialize = getattr(module, "ensure_project_access_initialized", None)
            if not callable(initialize):
                raise RuntimeError("ensure_project_access_initialized export is unavailable")
            result = initialize(
                project=project,
                owner_user_id=owner_auth_user_id,
                actor_user_id=owner_auth_user_id,
                session=_get_db().session,
                synchronize_default_roles=True,
                restore_deleted_roles=True,
                replace_existing_owner=True,
                allow_missing_owner=False,
                lock_project=True,
                flush=True,
            )
            data = _result_to_dict_with_variants(result)
            ready = _safe_bool(
                data.get("accessInitialized", data.get("ok", True)),
                True,
            )
            if not ready:
                raise RuntimeError("legacy project access initialization returned not ready")
            return {
                "ok": True,
                "backend": module_name,
                "result": data,
            }
        except Exception as exc:
            import_errors.append(
                f"{module_name}: {exc.__class__.__name__}: {_safe_exception_message(exc)}"
            )

    raise RuntimeError(
        "Legacy project-access compatibility initialization failed: "
        + " | ".join(import_errors)
    )


def _build_project_access_status_inner(
    project: Any | None = None,
    owner_auth_user_id: str | None = None,
) -> dict[str, Any]:
    """Build read-only canonical and legacy access readiness for the default project."""
    db = _get_db()
    models = _get_model_classes()
    Project = models.get("Project")
    project_id = _env_str("VECTOPLAN_CHUNK_DEFAULT_PROJECT_ID", DEFAULT_PROJECT_ID)
    if project is None and Project is not None:
        project = _query_first_by_field(db.session, Project, "project_id", project_id)
    if project is None:
        return {
            "ready": False,
            "projectExists": False,
            "projectId": project_id,
            "canonicalReady": False,
            "legacyReady": False,
        }

    project_id = _safe_str(getattr(project, "project_id", None), project_id)
    configured_owner = _normalize_auth_user_id(
        owner_auth_user_id or _resolve_project_owner_auth_user_id()
    )
    owner = _project_owner_from_model(project, configured_owner)
    owner_ready = owner == configured_owner or bool(owner)

    assignment_model = models.get("ProjectAccessAssignment")
    canonical_owner_count = 0
    canonical_owner_total = 0
    canonical_assignment_count = 0
    if assignment_model is not None:
        try:
            rows = db.session.query(assignment_model).filter(
                getattr(assignment_model, "chunk_project_id") == project_id
            ).all()
        except Exception:
            try:
                rows = db.session.query(assignment_model).all()
            except Exception:
                rows = []
        for row in rows:
            if _safe_str(getattr(row, "chunk_project_id", ""), "") != project_id:
                continue
            if not _safe_bool(getattr(row, "active", True), True):
                continue
            canonical_assignment_count += 1
            assignment_type = _safe_str(
                getattr(row, "assignment_type", DEFAULT_PROJECT_ACCESS_ASSIGNMENT_TYPE),
                DEFAULT_PROJECT_ACCESS_ASSIGNMENT_TYPE,
            ).lower()
            role = _safe_str(getattr(row, "role", ""), "").lower()
            if assignment_type == "group" or role != DEFAULT_PROJECT_ACCESS_ROLE:
                continue
            canonical_owner_total += 1
            if _normalize_auth_user_id(getattr(row, "auth_user_id", None), "") == owner:
                canonical_owner_count += 1

    canonical_ready = bool(
        assignment_model is not None
        and owner_ready
        and canonical_owner_total == 1
        and canonical_owner_count == 1
    )

    legacy_names = (
        "ProjectRole",
        "ProjectGroup",
        "ProjectGroupMember",
        "ProjectRoleAssignment",
    )
    legacy_models_present = all(models.get(name) is not None for name in legacy_names)
    legacy_ready = not legacy_models_present
    legacy_role_keys: set[str] = set()
    legacy_owner_assignment_count = 0
    if legacy_models_present:
        ProjectRole = models["ProjectRole"]
        ProjectRoleAssignment = models["ProjectRoleAssignment"]
        project_db_id = getattr(project, "id", None)
        try:
            roles = db.session.query(ProjectRole).filter(
                getattr(ProjectRole, "project_db_id") == project_db_id
            ).all()
        except Exception:
            roles = []
        owner_role_db_ids: set[Any] = set()
        owner_role_ids: set[str] = set()
        for role_row in roles:
            if _safe_str(getattr(role_row, "status", "active"), "active").lower() != "active":
                continue
            role_key = _safe_str(getattr(role_row, "role_key", ""), "").lower()
            if role_key:
                legacy_role_keys.add(role_key)
            if role_key == "owner":
                owner_role_db_ids.add(getattr(role_row, "id", None))
                owner_role_ids.add(_safe_str(getattr(role_row, "role_id", ""), ""))
        try:
            assignments = db.session.query(ProjectRoleAssignment).filter(
                getattr(ProjectRoleAssignment, "project_db_id") == project_db_id
            ).all()
        except Exception:
            assignments = []
        for assignment in assignments:
            if _safe_str(getattr(assignment, "status", "active"), "active").lower() != "active":
                continue
            if _safe_str(getattr(assignment, "subject_type", ""), "").lower() != "user":
                continue
            assignment_owner = _normalize_auth_user_id(
                getattr(assignment, "user_id", None),
                "",
            )
            if assignment_owner != owner:
                continue
            role_db_id = getattr(assignment, "role_db_id", None)
            role_id = _safe_str(getattr(assignment, "role_id", ""), "")
            if role_db_id in owner_role_db_ids or role_id in owner_role_ids:
                legacy_owner_assignment_count += 1
        legacy_ready = bool(
            {"owner", "admin", "editor", "viewer"}.issubset(legacy_role_keys)
            and legacy_owner_assignment_count == 1
        )

    ready = bool(owner_ready and canonical_ready and legacy_ready)
    return {
        "ready": ready,
        "projectExists": True,
        "projectId": project_id,
        "projectDbId": getattr(project, "id", None),
        "ownerAuthUserId": owner,
        "ownerReady": owner_ready,
        "canonicalReady": canonical_ready,
        "canonicalAssignmentCount": canonical_assignment_count,
        "canonicalOwnerAssignmentCount": canonical_owner_count,
        "canonicalOwnerTotal": canonical_owner_total,
        "legacyModelsPresent": legacy_models_present,
        "legacyReady": legacy_ready,
        "legacyRoleKeys": sorted(legacy_role_keys),
        "legacyOwnerAssignmentCount": legacy_owner_assignment_count,
    }


def _build_project_access_status(app: Any) -> dict[str, Any]:
    """Build project-access readiness with the required Flask app context."""
    if _has_app_context():
        return _build_project_access_status_inner()
    with app.app_context():
        return _build_project_access_status_inner()


def _ensure_project_access_inner(
    project: Any,
    owner_auth_user_id: str,
) -> dict[str, Any]:
    """Initialize canonical and legacy access rows in the surrounding seed transaction."""
    canonical = _initialize_canonical_project_access_inner(
        project,
        owner_auth_user_id,
    )
    legacy = _initialize_legacy_project_access_inner(
        project,
        owner_auth_user_id,
    )
    status = _build_project_access_status_inner(project, owner_auth_user_id)
    if not _safe_bool(status.get("ready"), False):
        raise RuntimeError(
            "Project access is not ready after canonical and legacy initialization."
        )
    return {
        "ok": True,
        "canonical": canonical,
        "legacy": legacy,
        "status": status,
    }


def _repair_project_access_if_needed_inner(*, commit: bool = True) -> dict[str, Any]:
    """Repair missing canonical/legacy access rows during explicit bootstrap only."""
    status_before = _build_project_access_status_inner()
    if _safe_bool(status_before.get("ready"), False) or not _project_access_required():
        return {
            "ok": True,
            "changed": False,
            "status": status_before,
        }

    db = _get_db()
    models = _get_model_classes()
    Project = models.get("Project")
    project_id = _env_str("VECTOPLAN_CHUNK_DEFAULT_PROJECT_ID", DEFAULT_PROJECT_ID)
    project = None
    if Project is not None:
        project = _query_first_by_field(db.session, Project, "project_id", project_id)
    if project is None:
        return {
            "ok": False,
            "changed": False,
            "status": status_before,
            "error": "Default project does not exist for project-access repair.",
        }

    configured_owner = _resolve_project_owner_auth_user_id()
    owner = _project_owner_from_model(project, configured_owner)
    owner = _apply_project_owner_fields(project, owner)
    try:
        repair = _ensure_project_access_inner(project, owner)
        if commit:
            db.session.commit()
        else:
            db.session.flush()
    except Exception:
        try:
            db.session.rollback()
        except Exception:
            pass
        raise

    status_after = _build_project_access_status_inner(project, owner)
    return {
        "ok": _safe_bool(status_after.get("ready"), False),
        "changed": True,
        "ownerAuthUserId": owner,
        "repair": repair,
        "statusBefore": status_before,
        "status": status_after,
    }


def _direct_seed_defaults_inner() -> dict[str, Any]:
    """
    Seed minimal dev Project/Universe/WorldInstance fallback.

    Requires active app context.
    """
    db = _get_db()

    from models import Project, Universe, WorldInstance

    created: list[str] = []
    reused: list[str] = []
    warnings: list[dict[str, Any]] = []

    project_id = _env_str("VECTOPLAN_CHUNK_DEFAULT_PROJECT_ID", DEFAULT_PROJECT_ID)
    universe_id = _env_str("VECTOPLAN_CHUNK_DEFAULT_UNIVERSE_ID", DEFAULT_UNIVERSE_ID)
    world_id = _resolve_concrete_default_world_id()
    template_id = _env_str("VECTOPLAN_CHUNK_DEFAULT_TEMPLATE_ID", DEFAULT_TEMPLATE_ID)
    provider_id = _env_str("VECTOPLAN_CHUNK_DEFAULT_PROVIDER_ID", DEFAULT_PROVIDER_ID)
    provider_world_id = _env_str("VECTOPLAN_CHUNK_DEFAULT_PROVIDER_WORLD_ID", DEFAULT_PROVIDER_WORLD_ID)
    block_registry_id = _env_str("VECTOPLAN_CHUNK_DEFAULT_BLOCK_REGISTRY_ID", DEFAULT_BLOCK_REGISTRY_ID)
    block_registry_version = _env_str("VECTOPLAN_CHUNK_DEFAULT_BLOCK_REGISTRY_VERSION", DEFAULT_BLOCK_REGISTRY_VERSION)
    owner_auth_user_id = _ensure_project_owner_env_defaults(seed_project_access=True)

    project = _query_first_by_field(db.session, Project, "project_id", project_id)

    if project is None:
        if hasattr(Project, "create_dev_project"):
            try:
                project = Project.create_dev_project(
                    project_id=project_id,
                    default_universe_id=universe_id,
                    default_world_id=world_id,
                    spawn_world_id=world_id,
                    owner_user_id=owner_auth_user_id,
                    created_by_user_id=owner_auth_user_id,
                )
            except TypeError:
                try:
                    project = Project.create_dev_project(
                        project_id=project_id,
                        default_universe_id=universe_id,
                        default_world_id=world_id,
                        owner_user_id=owner_auth_user_id,
                        created_by_user_id=owner_auth_user_id,
                    )
                except TypeError:
                    project = Project.create_dev_project(
                        project_id=project_id,
                        default_universe_id=universe_id,
                        owner_user_id=owner_auth_user_id,
                        created_by_user_id=owner_auth_user_id,
                    )
        else:
            project = Project(
                project_id=project_id,
                slug=project_id,
                name="Dev Project",
                default_universe_id=universe_id,
                default_world_id=world_id,
                spawn_world_id=world_id,
                owner_type="user",
                owner_id=owner_auth_user_id,
                owner_auth_user_id=owner_auth_user_id,
                created_by_user_id=owner_auth_user_id,
                updated_by_user_id=owner_auth_user_id,
                metadata_json={"seed": True, "createdBy": "bootstrap_db.py"},
            )

        db.session.add(project)
        db.session.flush()
        created.append("Project")
    else:
        reused.append("Project")

    _set_attr_if_empty(project, "slug", project_id)
    _set_attr_if_empty(project, "name", "Dev Project")
    _set_attr_if_empty(project, "status", "active")
    owner_auth_user_id = _project_owner_from_model(project, owner_auth_user_id)
    owner_auth_user_id = _apply_project_owner_fields(project, owner_auth_user_id)
    _set_attr_if_supported(project, "default_universe_id", universe_id)
    _set_attr_if_supported(project, "default_world_id", world_id)
    _set_attr_if_supported(project, "spawn_world_id", world_id)
    _set_attr_if_supported(project, "updated_by_user_id", owner_auth_user_id)
    _merge_metadata_json(
        project,
        {
            "seed": True,
            "createdBy": "bootstrap_db.py",
            "defaultUniverseId": universe_id,
            "defaultWorldId": world_id,
            "spawnWorldId": world_id,
            "ownerAuthUserId": owner_auth_user_id,
        },
    )

    universe = None
    try:
        universe = (
            db.session.query(Universe)
            .filter(Universe.project_db_id == project.id)
            .filter(Universe.universe_id == universe_id)
            .first()
        )
    except Exception:
        universe = _query_first_by_field(db.session, Universe, "universe_id", universe_id)

    if universe is None:
        if hasattr(Universe, "create_for_project"):
            try:
                universe = Universe.create_for_project(
                    project,
                    universe_id=universe_id,
                    name="Dev Universe",
                    slug=universe_id,
                    default_world_id=world_id,
                    spawn_world_id=world_id,
                    created_by_user_id=owner_auth_user_id,
                    metadata_json={"seed": True, "createdBy": "bootstrap_db.py"},
                )
            except TypeError:
                universe = Universe(
                    project_db_id=project.id,
                    universe_id=universe_id,
                    slug=universe_id,
                    name="Dev Universe",
                    default_world_id=world_id,
                    spawn_world_id=world_id,
                    metadata_json={"seed": True, "createdBy": "bootstrap_db.py"},
                )
        else:
            universe = Universe(
                project_db_id=project.id,
                universe_id=universe_id,
                slug=universe_id,
                name="Dev Universe",
                default_world_id=world_id,
                spawn_world_id=world_id,
                metadata_json={"seed": True, "createdBy": "bootstrap_db.py"},
            )

        db.session.add(universe)
        db.session.flush()
        created.append("Universe")
    else:
        reused.append("Universe")

    _set_attr_if_supported(universe, "project_db_id", getattr(project, "id", None))
    _set_attr_if_empty(universe, "slug", universe_id)
    _set_attr_if_empty(universe, "name", "Dev Universe")
    _set_attr_if_empty(universe, "status", "active")
    _set_attr_if_supported(universe, "default_world_id", world_id)
    _set_attr_if_supported(universe, "spawn_world_id", world_id)
    _set_attr_if_supported(universe, "created_by_user_id", owner_auth_user_id)
    _set_attr_if_supported(universe, "updated_by_user_id", owner_auth_user_id)
    _merge_metadata_json(
        universe,
        {
            "seed": True,
            "createdBy": "bootstrap_db.py",
            "defaultWorldId": world_id,
            "spawnWorldId": world_id,
        },
    )

    world = None
    try:
        world = (
            db.session.query(WorldInstance)
            .filter(WorldInstance.universe_db_id == universe.id)
            .filter(WorldInstance.world_id == world_id)
            .first()
        )
    except Exception:
        world = _query_first_by_field(db.session, WorldInstance, "world_id", world_id)

    if world is None:
        if hasattr(WorldInstance, "create_flat_spawn"):
            try:
                world = WorldInstance.create_flat_spawn(
                    project_db_id=project.id,
                    universe_db_id=universe.id,
                    world_id=world_id,
                    slug="spawn",
                    name="Flat Spawn World",
                    created_by_user_id=owner_auth_user_id,
                    metadata_json={"seed": True, "createdBy": "bootstrap_db.py"},
                )
            except TypeError:
                world = WorldInstance.create_flat_spawn(
                    project=project,
                    universe=universe,
                    world_id=world_id,
                    slug="spawn",
                    name="Flat Spawn World",
                    created_by_user_id=owner_auth_user_id,
                    metadata_json={"seed": True, "createdBy": "bootstrap_db.py"},
                )
        else:
            world = WorldInstance(
                project_db_id=project.id,
                universe_db_id=universe.id,
                world_id=world_id,
                slug="spawn",
                name="Flat Spawn World",
                template_id=template_id,
                provider_id=provider_id,
                provider_world_id=provider_world_id,
                generator_type="flat-world",
                generator_version="1",
                projection_type="flat-local-v1",
                topology_type="flat-unbounded-v1",
                coordinate_system="vectoplan-world-y-up-v1",
                chunk_size=16,
                cell_size=1.0,
                surface_y=0,
                min_y=-8,
                max_y=64,
                block_registry_id=block_registry_id,
                block_registry_version=block_registry_version,
                spawn_x=0,
                spawn_y=2,
                spawn_z=0,
                spawn_yaw=0.0,
                spawn_pitch=0.0,
                source_service="vectoplan-chunk-bootstrap",
                external_ref=world_id,
                metadata_json={"seed": True, "createdBy": "bootstrap_db.py"},
            )

        db.session.add(world)
        db.session.flush()
        created.append("WorldInstance")
    else:
        reused.append("WorldInstance")

    _set_attr_if_supported(world, "project_db_id", getattr(project, "id", None))
    _set_attr_if_supported(world, "universe_db_id", getattr(universe, "id", None))
    _set_attr_if_empty(world, "slug", "spawn")
    _set_attr_if_empty(world, "name", "Flat Spawn World")
    _set_attr_if_empty(world, "status", "active")
    _set_attr_if_supported(world, "template_id", template_id)
    _set_attr_if_supported(world, "provider_id", provider_id)
    _set_attr_if_supported(world, "provider_world_id", provider_world_id)
    _set_attr_if_supported(world, "block_registry_id", block_registry_id)
    _set_attr_if_supported(world, "block_registry_version", block_registry_version)
    _set_attr_if_supported(world, "spawn_x", _safe_int(_env_str("VECTOPLAN_CHUNK_DEFAULT_SPAWN_X", "0"), 0))
    _set_attr_if_supported(world, "spawn_y", _safe_int(_env_str("VECTOPLAN_CHUNK_DEFAULT_SPAWN_Y", "2"), 2))
    _set_attr_if_supported(world, "spawn_z", _safe_int(_env_str("VECTOPLAN_CHUNK_DEFAULT_SPAWN_Z", "0"), 0))
    _set_attr_if_supported(world, "spawn_yaw", _safe_float(_env_str("VECTOPLAN_CHUNK_DEFAULT_SPAWN_YAW", "0.0"), 0.0))
    _set_attr_if_supported(world, "spawn_pitch", _safe_float(_env_str("VECTOPLAN_CHUNK_DEFAULT_SPAWN_PITCH", "0.0"), 0.0))
    _set_attr_if_supported(world, "source_service", "vectoplan-chunk-bootstrap")
    _set_attr_if_supported(world, "external_ref", world_id)
    _set_attr_if_supported(world, "created_by_user_id", owner_auth_user_id)
    _set_attr_if_supported(world, "updated_by_user_id", owner_auth_user_id)
    _merge_metadata_json(
        world,
        {
            "seed": True,
            "createdBy": "bootstrap_db.py",
            "worldId": world_id,
            "templateId": template_id,
            "providerId": provider_id,
            "providerWorldId": provider_world_id,
        },
    )

    try:
        if hasattr(project, "set_world_refs"):
            project.set_world_refs(
                default_universe_id=universe_id,
                default_world_id=world_id,
                spawn_world_id=world_id,
                updated_by_user_id=owner_auth_user_id,
            )
        else:
            _set_attr_if_supported(project, "default_universe_id", universe_id)
            _set_attr_if_supported(project, "default_world_id", world_id)
            _set_attr_if_supported(project, "spawn_world_id", world_id)

        if hasattr(universe, "set_world_defaults"):
            universe.set_world_defaults(
                default_world_id=world_id,
                spawn_world_id=world_id,
                updated_by_user_id=owner_auth_user_id,
            )
        else:
            _set_attr_if_supported(universe, "default_world_id", world_id)
            _set_attr_if_supported(universe, "spawn_world_id", world_id)
    except Exception as exc:
        warnings.append(
            {
                "code": "reference_backfill_warning",
                "message": _safe_exception_message(exc),
            }
        )

    project_access = _ensure_project_access_inner(
        project,
        owner_auth_user_id,
    )

    system_blocks = _run_system_block_repair_inner(commit=False)
    system_readiness = _extract_system_block_readiness(system_blocks)

    if not system_readiness["systemBlocksReady"]:
        raise RuntimeError(
            "Direct seed fallback could not produce a ready Air/Railing "
            "system-block state."
        )

    db.session.commit()

    return {
        "ok": True,
        "status": "seed_completed",
        "executed": True,
        "created": created,
        "reused": reused,
        "warnings": warnings,
        "defaultProjectReady": True,
        "defaultUniverseReady": True,
        "defaultWorldReady": True,
        "blockRegistryReady": True,
        "projectAccessReady": True,
        "projectOwnerAuthUserId": owner_auth_user_id,
        "projectAccess": project_access,
        "systemBlocksReady": system_readiness["systemBlocksReady"],
        "systemRailingReady": system_readiness["systemRailingReady"],
        "airInvariantReady": system_readiness["airInvariantReady"],
        "systemBlockCount": system_readiness["systemBlockCount"],
        "systemBlocksCreated": system_readiness["systemBlocksCreated"],
        "systemBlocksUpdated": system_readiness["systemBlocksUpdated"],
        "systemBlocksMissing": system_readiness["systemBlocksMissing"],
        "systemBlocksDrifted": system_readiness["systemBlocksDrifted"],
        "systemBlocks": system_blocks,
        "defaults": {
            "projectId": project_id,
            "universeId": universe_id,
            "worldId": world_id,
            "templateId": template_id,
            "providerId": provider_id,
            "providerWorldId": provider_world_id,
            "blockRegistryId": block_registry_id,
            "blockRegistryVersion": block_registry_version,
            "ownerAuthUserId": owner_auth_user_id,
            "seedProjectAccess": True,
        },
    }


def _direct_seed_defaults(app: Any) -> dict[str, Any]:
    """
    Seed minimal dev Project/Universe/WorldInstance fallback.

    The preferred seed path is src.bootstrap.db_bootstrap. This fallback exists
    so local bootstrap remains usable even if that module is temporarily broken.
    """
    if _has_app_context():
        return _direct_seed_defaults_inner()

    with app.app_context():
        return _direct_seed_defaults_inner()


def _run_seed_invariant_repair(app: Any) -> dict[str, Any]:
    """Run preferred seed-invariant repair if available, otherwise direct seed fallback."""
    try:
        from src.bootstrap.db_bootstrap import repair_default_world_invariant

        result = repair_default_world_invariant(app, commit=True)
        data = _to_plain_dict(result)
        data.setdefault("backend", "src.bootstrap.db_bootstrap.repair_default_world_invariant")
        return data
    except Exception as exc:
        fallback = _direct_seed_defaults(app)
        fallback.setdefault("warnings", []).append(
            {
                "code": "preferred_seed_invariant_repair_unavailable",
                "message": _safe_exception_message(exc),
            }
        )
        fallback["backend"] = "direct_seed_defaults"
        return fallback


def _fallback_bootstrap(
    app: Any,
    *,
    run_schema: bool,
    run_seed: bool,
    repair_missing_columns: bool,
    dry_run_repair: bool,
    repair_seed_invariants: bool,
) -> dict[str, Any]:
    """Fallback bootstrap when src.bootstrap.db_bootstrap is unavailable or returned not-ok."""
    warnings: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    schema_result: dict[str, Any] = {
        "ok": True,
        "executed": False,
        "status": "not_requested",
    }
    repair_result: dict[str, Any] = {
        "ok": True,
        "executed": False,
        "status": "not_requested",
    }
    seed_result: dict[str, Any] = {
        "ok": True,
        "executed": False,
        "status": "not_requested",
    }

    pre_status = _inspect_database_schema(app)

    if run_schema:
        try:
            schema_result = _direct_create_all(app)
        except Exception as exc:
            schema_result = {
                "ok": False,
                "executed": True,
                "status": "create_all_failed",
                "error": _safe_exception_message(exc),
            }
            errors.append(
                {
                    "code": "create_all_failed",
                    "message": _safe_exception_message(exc),
                }
            )

    if repair_missing_columns and schema_result.get("ok"):
        repair_result = _repair_missing_columns(app, dry_run=dry_run_repair)
        for item in repair_result.get("warnings") or []:
            warnings.append(item)
        for item in repair_result.get("errors") or []:
            errors.append(item)

    if run_seed and schema_result.get("ok") and repair_result.get("ok"):
        try:
            if repair_seed_invariants:
                seed_result = _run_seed_invariant_repair(app)
            else:
                seed_result = _direct_seed_defaults(app)

            for item in seed_result.get("warnings") or []:
                warnings.append(item)
            for item in seed_result.get("errors") or []:
                errors.append(item)
        except Exception as exc:
            seed_result = {
                "ok": False,
                "executed": True,
                "status": "seed_failed",
                "error": _safe_exception_message(exc),
            }
            errors.append(
                {
                    "code": "seed_failed",
                    "message": _safe_exception_message(exc),
                }
            )

    post_status = _inspect_database_schema(app)
    invariant_status = _build_invariant_status_if_available(app)
    system_status = _extract_system_block_status(seed_result)
    if not system_status:
        system_status = _extract_system_block_status(invariant_status)
    if not system_status:
        system_status = _build_system_block_status_if_available(app)
    system_readiness = _extract_system_block_readiness(
        {
            "seed": seed_result,
            "seedInvariant": invariant_status,
            "systemBlocks": system_status,
        }
    )
    project_access_status = _build_project_access_status(app)
    project_access_ready = _safe_bool(project_access_status.get("ready"), False)

    default_project_ready = _nested_bool(invariant_status, ("ready", "project"))
    default_universe_ready = _nested_bool(invariant_status, ("ready", "universe"))
    default_world_ready = _nested_bool(invariant_status, ("ready", "world"))
    seed_ready = bool(
        bool(seed_result.get("ok"))
        and (default_world_ready is not False)
        and (
            not run_seed
            or (
                system_readiness["systemBlocksReady"]
                and (not _project_access_required() or project_access_ready)
                and (
                    not _debug_blocks_required()
                    or _nested_bool(invariant_status, ("ready", "debugBlocks")) is not False
                )
            )
        )
    )

    ok = (
        bool(schema_result.get("ok"))
        and bool(repair_result.get("ok"))
        and bool(seed_result.get("ok"))
        and bool(post_status.get("ok"))
        and (default_world_ready is not False)
        and (
            not run_seed
            or (
                system_readiness["systemBlocksReady"]
                and (not _project_access_required() or project_access_ready)
                and (
                    not _debug_blocks_required()
                    or _nested_bool(invariant_status, ("ready", "debugBlocks")) is not False
                )
            )
        )
    )

    if not post_status.get("ok"):
        errors.append(
            {
                "code": "post_bootstrap_schema_not_ready",
                "message": "Database schema is not ready after bootstrap.",
                "details": {
                    "missingTables": post_status.get("missingTables"),
                    "missingColumns": post_status.get("missingColumns"),
                },
            }
        )

    if run_seed and default_world_ready is False:
        errors.append(
            {
                "code": "post_bootstrap_default_world_not_ready",
                "message": "Default concrete world is not ready after bootstrap.",
                "details": invariant_status,
            }
        )

    if run_seed and _project_access_required() and not project_access_ready:
        errors.append(
            {
                "code": "post_bootstrap_project_access_not_ready",
                "message": "Default project owner/access projection is not ready after bootstrap.",
                "details": project_access_status,
            }
        )

    if run_seed and not system_readiness["systemBlocksReady"]:
        errors.append(
            {
                "code": "post_bootstrap_system_blocks_not_ready",
                "message": (
                    "Built-in Air/Railing system-block state is not ready "
                    "after bootstrap."
                ),
                "details": system_status,
            }
        )

    result = {
        "ok": ok,
        "status": "completed" if ok else "failed",
        "enabled": True,
        "backend": "fallback",
        "schemaReady": bool(post_status.get("ok")),
        "seedReady": seed_ready,
        "defaultProjectReady": default_project_ready,
        "defaultUniverseReady": default_universe_ready,
        "defaultWorldReady": default_world_ready,
        "blockRegistryReady": _nested_bool(invariant_status, ("ready", "blockRegistry")),
        "debugBlocksReady": _nested_bool(invariant_status, ("ready", "debugBlocks")),
        "projectAccessReady": project_access_ready,
        "projectOwnerAuthUserId": project_access_status.get("ownerAuthUserId"),
        "projectAccess": project_access_status,
        "systemBlocksReady": system_readiness["systemBlocksReady"],
        "systemRailingReady": system_readiness["systemRailingReady"],
        "airInvariantReady": system_readiness["airInvariantReady"],
        "systemBlockCount": system_readiness["systemBlockCount"],
        "systemBlocksCreated": system_readiness["systemBlocksCreated"],
        "systemBlocksUpdated": system_readiness["systemBlocksUpdated"],
        "systemBlocksMissing": system_readiness["systemBlocksMissing"],
        "systemBlocksDrifted": system_readiness["systemBlocksDrifted"],
        "systemBlocks": system_status,
        "schema_bootstrap_requested": bool(run_schema),
        "schema_bootstrap_executed": bool(schema_result.get("executed")),
        "schema_bootstrap_ok": bool(schema_result.get("ok")),
        "schema": schema_result,
        "schema_repair_requested": bool(repair_missing_columns),
        "schema_repair_executed": bool(repair_result.get("executed")),
        "schema_repair_ok": bool(repair_result.get("ok")),
        "schemaRepair": repair_result,
        "seed_bootstrap_requested": bool(run_seed),
        "seed_bootstrap_executed": bool(seed_result.get("executed", True if run_seed else False)),
        "seed_bootstrap_ok": seed_ready,
        "seed_invariant_repair_requested": bool(repair_seed_invariants),
        "seed_invariant_repair_executed": bool(seed_result.get("executed", False)),
        "seed_invariant_repair_ok": seed_result.get("ok"),
        "seed": seed_result,
        "seedInvariant": invariant_status,
        "warnings": warnings,
        "errors": errors,
        "pre_status": pre_status,
        "post_status": post_status,
    }
    result["summary"] = summarize_bootstrap_result(result)

    return result


# -----------------------------------------------------------------------------
# Result helpers
# -----------------------------------------------------------------------------

def _nested_value(payload: Mapping[str, Any] | Any, path: Sequence[str], default: Any = None) -> Any:
    """Read nested mapping value."""
    current: Any = payload

    for part in path:
        if not isinstance(current, Mapping):
            return default
        current = current.get(part)

    return current if current is not None else default


def _nested_bool(payload: Mapping[str, Any] | Any, path: Sequence[str]) -> bool | None:
    """Read nested bool value."""
    value = _nested_value(payload, path, None)

    if value is None:
        return None

    return _safe_bool(value, False)


def _mapping_value_by_names(
    payload: Mapping[str, Any] | Any,
    names: Sequence[str],
    *,
    max_depth: int = 6,
) -> Any:
    """Find the first matching key in a bounded, cycle-safe mapping walk."""
    if not isinstance(payload, Mapping):
        return None

    wanted = tuple(str(name) for name in names)
    queue: list[tuple[Mapping[str, Any], int]] = [(payload, 0)]
    seen: set[int] = set()

    while queue:
        current, depth = queue.pop(0)
        current_id = id(current)

        if current_id in seen:
            continue
        seen.add(current_id)

        for name in wanted:
            try:
                if name in current:
                    value = current.get(name)
                    if value is not None:
                        return value
            except Exception:
                continue

        if depth >= max_depth:
            continue

        preferred_children = (
            "summary",
            "post_status",
            "postStatus",
            "seed",
            "seedInvariant",
            "seed_invariant",
            "defaultWorldInvariant",
            "invariantAfterRepair",
            "seedInvariantFinal",
            "bootstrap",
            "systemBlocks",
            "system_blocks",
        )

        for child_name in preferred_children:
            try:
                child = current.get(child_name)
            except Exception:
                child = None

            if isinstance(child, Mapping):
                queue.append((child, depth + 1))

    return None


def _extract_system_block_status(payload: Mapping[str, Any] | Any) -> dict[str, Any]:
    """Extract the canonical registry-scoped system-block status mapping."""
    if not isinstance(payload, Mapping):
        return {}

    queue: list[tuple[Mapping[str, Any], int]] = [(payload, 0)]
    seen: set[int] = set()

    while queue:
        current, depth = queue.pop(0)
        current_id = id(current)

        if current_id in seen:
            continue
        seen.add(current_id)

        keys = set(current.keys())
        if (
            "air" in keys
            and "mirrors" in keys
            and ("ready" in keys or "registryId" in keys or "registry_id" in keys)
        ):
            return dict(current)

        for key in ("systemBlocks", "system_blocks"):
            candidate = current.get(key)
            if isinstance(candidate, Mapping):
                candidate_keys = set(candidate.keys())
                if "air" in candidate_keys or "mirrors" in candidate_keys:
                    return dict(candidate)
                if depth < 6:
                    queue.append((candidate, depth + 1))

        if depth >= 6:
            continue

        for key in (
            "seedInvariant",
            "seed_invariant",
            "defaultWorldInvariant",
            "invariantAfterRepair",
            "seedInvariantFinal",
            "post_status",
            "postStatus",
            "seed",
            "bootstrap",
        ):
            child = current.get(key)
            if isinstance(child, Mapping):
                queue.append((child, depth + 1))

    return {}


def _system_block_status_counts(status: Mapping[str, Any] | None) -> dict[str, int]:
    """Normalize persistent system-block mirror counters."""
    status_dict = _to_plain_dict(status)
    mirrors = status_dict.get("mirrors") or []

    if not isinstance(mirrors, Sequence) or isinstance(mirrors, (str, bytes, bytearray)):
        mirrors = []

    inferred = {
        "mirrors": len(mirrors),
        "readyMirrors": 0,
        "created": 0,
        "updated": 0,
        "missing": 0,
        "drifted": 0,
    }

    for raw_mirror in mirrors:
        mirror = _to_plain_dict(raw_mirror)
        action = _safe_str(mirror.get("action"), "").lower()

        if _safe_bool(mirror.get("ready"), False):
            inferred["readyMirrors"] += 1
        if _safe_bool(mirror.get("created"), False):
            inferred["created"] += 1
        if _safe_bool(mirror.get("updated"), False):
            inferred["updated"] += 1
        if action in {"missing", "would_create"}:
            inferred["missing"] += 1
        if mirror.get("driftBefore") or action in {"drifted", "would_update", "updated"}:
            inferred["drifted"] += 1

    counts = _to_plain_dict(status_dict.get("counts"))

    return {
        key: max(0, _safe_int(counts.get(key), default))
        for key, default in inferred.items()
    }


def _system_railing_ready(status: Mapping[str, Any] | None) -> bool:
    """Return whether the canonical Railing mirror is active and drift-free."""
    status_dict = _to_plain_dict(status)
    direct = status_dict.get("systemRailingReady")
    if direct is not None:
        return _safe_bool(direct, False)

    mirrors = status_dict.get("mirrors") or []
    if not isinstance(mirrors, Sequence) or isinstance(mirrors, (str, bytes, bytearray)):
        return False

    for raw_mirror in mirrors:
        mirror = _to_plain_dict(raw_mirror)
        system_id = _safe_str(mirror.get("systemBlockId"), "").lower()
        runtime_id = _safe_str(mirror.get("runtimeBlockTypeId"), "").lower()

        if DEFAULT_SYSTEM_RAILING_BLOCK_TYPE_ID in {system_id, runtime_id}:
            return bool(
                _safe_bool(mirror.get("ready"), False)
                and not mirror.get("driftAfter")
                and _safe_str(mirror.get("action"), "").lower()
                not in {"missing", "drifted", "invalid", "conflict", "error", "rolled_back"}
            )

    return False


def _air_invariant_ready(status: Mapping[str, Any] | None) -> bool:
    """Return whether Air remains reserved and absent from BlockType storage."""
    status_dict = _to_plain_dict(status)
    direct = status_dict.get("airInvariantReady")
    if direct is not None:
        return _safe_bool(direct, False)

    air = _to_plain_dict(status_dict.get("air"))
    return bool(
        _safe_bool(air.get("ready"), False)
        and _safe_int(air.get("illegalRowCount"), 0) == 0
    )


def _system_blocks_ready(status: Mapping[str, Any] | None) -> bool:
    """Require aggregate, Air and Railing readiness together."""
    status_dict = _to_plain_dict(status)
    aggregate = status_dict.get("systemBlocksReady", status_dict.get("ready"))
    return bool(
        _safe_bool(aggregate, False)
        and _air_invariant_ready(status_dict)
        and _system_railing_ready(status_dict)
    )


def _extract_system_block_readiness(payload: Mapping[str, Any] | Any) -> dict[str, Any]:
    """Normalize system-block readiness from script, bootstrap or seed payloads."""
    data = _to_plain_dict(payload)
    status = _extract_system_block_status(data)
    counts = _system_block_status_counts(status)

    def first_bool(*names: str) -> bool | None:
        value = _mapping_value_by_names(data, names)
        if value is None:
            return None
        return _safe_bool(value, False)

    def first_int(*names: str) -> int | None:
        value = _mapping_value_by_names(data, names)
        if value is None:
            return None
        return max(0, _safe_int(value, 0))

    system_ready = first_bool("systemBlocksReady", "system_blocks_ready")
    air_ready = first_bool("airInvariantReady", "air_invariant_ready")
    railing_ready = first_bool("systemRailingReady", "system_railing_ready")

    if system_ready is None:
        system_ready = _system_blocks_ready(status)
    if air_ready is None:
        air_ready = _air_invariant_ready(status)
    if railing_ready is None:
        railing_ready = _system_railing_ready(status)

    mirror_count = first_int("systemBlockCount", "system_block_count")
    created = first_int("systemBlocksCreated", "system_blocks_created")
    updated = first_int("systemBlocksUpdated", "system_blocks_updated")
    missing = first_int("systemBlocksMissing", "system_blocks_missing")
    drifted = first_int("systemBlocksDrifted", "system_blocks_drifted")

    return {
        "systemBlocksReady": bool(system_ready and air_ready and railing_ready),
        "airInvariantReady": bool(air_ready),
        "systemRailingReady": bool(railing_ready),
        "systemBlockCount": counts["mirrors"] if mirror_count is None else mirror_count,
        "systemBlocksCreated": counts["created"] if created is None else created,
        "systemBlocksUpdated": counts["updated"] if updated is None else updated,
        "systemBlocksMissing": counts["missing"] if missing is None else missing,
        "systemBlocksDrifted": counts["drifted"] if drifted is None else drifted,
        "systemBlocks": status,
    }


def _find_default_block_registry_inner() -> Any | None:
    """Find the configured default BlockRegistry inside the active app context."""
    db = _get_db()
    models = _get_model_classes()
    BlockRegistry = models.get("BlockRegistry")

    if BlockRegistry is None:
        return None

    registry_id = _env_str(
        "VECTOPLAN_CHUNK_DEFAULT_BLOCK_REGISTRY_ID",
        DEFAULT_BLOCK_REGISTRY_ID,
    )
    registry_version = _env_str(
        "VECTOPLAN_CHUNK_DEFAULT_BLOCK_REGISTRY_VERSION",
        DEFAULT_BLOCK_REGISTRY_VERSION,
    )

    registry = _query_first_by_fields(
        db.session,
        BlockRegistry,
        registry_id=registry_id,
        registry_version=registry_version,
    )

    if registry is None:
        registry = _query_first_by_fields(
            db.session,
            BlockRegistry,
            registry_id=registry_id,
        )

    return registry


def _ensure_default_block_registry_inner() -> Any:
    """Create or restore the configured default registry for fallback seeding."""
    db = _get_db()
    models = _get_model_classes()
    BlockRegistry = models.get("BlockRegistry")

    if BlockRegistry is None:
        raise RuntimeError("BlockRegistry model is unavailable.")

    registry = _find_default_block_registry_inner()
    registry_id = _env_str(
        "VECTOPLAN_CHUNK_DEFAULT_BLOCK_REGISTRY_ID",
        DEFAULT_BLOCK_REGISTRY_ID,
    )
    registry_version = _env_str(
        "VECTOPLAN_CHUNK_DEFAULT_BLOCK_REGISTRY_VERSION",
        DEFAULT_BLOCK_REGISTRY_VERSION,
    )

    if registry is None:
        factory = getattr(BlockRegistry, "create_debug_registry", None)
        if callable(factory) and registry_id == DEFAULT_BLOCK_REGISTRY_ID and registry_version == DEFAULT_BLOCK_REGISTRY_VERSION:
            try:
                registry = factory(is_default=True)
            except TypeError:
                registry = factory()
        else:
            create = getattr(BlockRegistry, "create", None)
            if callable(create):
                registry = create(
                    registry_id=registry_id,
                    registry_version=registry_version,
                    label=f"{registry_id} {registry_version}",
                    status="active",
                    is_default=True,
                    created_by_user_id=DEFAULT_SYSTEM_BLOCK_BOOTSTRAP_USER_ID,
                    metadata_json={
                        "seededBy": "bootstrap_db.py.system_blocks",
                        "createdAt": _utc_now_iso(),
                    },
                )
            else:
                registry = BlockRegistry(
                    registry_id=registry_id,
                    registry_version=registry_version,
                    label=f"{registry_id} {registry_version}",
                    status="active",
                    is_default=True,
                    metadata_json={
                        "seededBy": "bootstrap_db.py.system_blocks",
                        "createdAt": _utc_now_iso(),
                    },
                )

        db.session.add(registry)
        db.session.flush()
    else:
        restore = getattr(registry, "restore", None)
        is_deleted = _safe_bool(getattr(registry, "is_deleted", False), False)
        status = _safe_str(getattr(registry, "status", ""), "").lower()

        if callable(restore) and (is_deleted or status != "active"):
            restore(updated_by_user_id=DEFAULT_SYSTEM_BLOCK_BOOTSTRAP_USER_ID)
        else:
            _set_attr_if_supported(registry, "status", "active")
            _set_attr_if_supported(registry, "deleted_at", None)
            _set_attr_if_supported(registry, "archived_at", None)

        _set_attr_if_supported(registry, "registry_version", registry_version)
        _set_attr_if_supported(registry, "is_default", True)
        _set_attr_if_supported(
            registry,
            "updated_by_user_id",
            DEFAULT_SYSTEM_BLOCK_BOOTSTRAP_USER_ID,
        )
        _merge_metadata_json(
            registry,
            {
                "seededBy": "bootstrap_db.py.system_blocks",
                "updatedAt": _utc_now_iso(),
            },
        )
        db.session.flush()

    if getattr(registry, "id", None) is None:
        raise RuntimeError("Default BlockRegistry has no database id after flush.")

    return registry


def _build_system_block_status_inner() -> dict[str, Any]:
    """Build read-only registry-scoped Air/Railing status."""
    registry = _find_default_block_registry_inner()

    if registry is None:
        return {
            "ready": False,
            "status": "registry_missing",
            "air": {"ready": False, "illegalRowCount": 0},
            "mirrors": [],
            "counts": {
                "mirrors": 0,
                "readyMirrors": 0,
                "created": 0,
                "updated": 0,
                "missing": 1,
                "drifted": 0,
            },
            "errors": ["Default BlockRegistry is missing."],
        }

    try:
        from src.system_blocks.bootstrap import (
            build_system_block_bootstrap_status_for_registry,
        )

        return _to_plain_dict(
            build_system_block_bootstrap_status_for_registry(registry)
        )
    except Exception as exc:
        return {
            "ready": False,
            "status": "unavailable",
            "registryId": _safe_str(getattr(registry, "registry_id", ""), ""),
            "registryVersion": _safe_str(getattr(registry, "registry_version", ""), ""),
            "air": {"ready": False, "illegalRowCount": 0},
            "mirrors": [],
            "errors": [
                f"System-block status unavailable: {_safe_exception_message(exc)}"
            ],
        }


def _build_system_block_status_if_available(app: Any) -> dict[str, Any]:
    """Build system-block status with the required Flask app context."""
    if _has_app_context():
        return _build_system_block_status_inner()

    with app.app_context():
        return _build_system_block_status_inner()


def _run_system_block_repair_inner(*, commit: bool = True) -> dict[str, Any]:
    """Reconcile Air/Railing in the default registry without duplicating domain logic."""
    db = _get_db()

    try:
        registry = _ensure_default_block_registry_inner()

        from src.system_blocks.bootstrap import ensure_system_blocks_for_registry

        raw_result = ensure_system_blocks_for_registry(
            registry,
            created_by_user_id=DEFAULT_SYSTEM_BLOCK_BOOTSTRAP_USER_ID,
            updated_by_user_id=DEFAULT_SYSTEM_BLOCK_BOOTSTRAP_USER_ID,
        )
        data = _to_plain_dict(raw_result)

        if not data:
            to_dict = getattr(raw_result, "to_dict", None)
            if callable(to_dict):
                data = _to_plain_dict(to_dict())

        readiness = _extract_system_block_readiness(data)
        if not readiness["systemBlocksReady"]:
            raise RuntimeError(
                "System-block reconciliation did not produce a ready Air/Railing state."
            )

        db.session.flush()
        if commit:
            db.session.commit()

        data.setdefault("executed", True)
        data.setdefault("status", "completed")
        return data
    except Exception:
        try:
            db.session.rollback()
        except Exception:
            pass
        raise


def _run_system_block_repair(app: Any, *, commit: bool = True) -> dict[str, Any]:
    """Run system-block repair with an app context."""
    if _has_app_context():
        return _run_system_block_repair_inner(commit=commit)

    with app.app_context():
        return _run_system_block_repair_inner(commit=commit)


def _append_unique_errors(target: list[dict[str, Any]], items: Sequence[Any]) -> None:
    """Append error/warning items without obvious duplicates."""
    seen = set()
    for existing in target:
        if isinstance(existing, Mapping):
            seen.add((_safe_str(existing.get("code"), ""), _safe_str(existing.get("message"), "")))

    for item in items:
        if isinstance(item, Mapping):
            payload = dict(item)
        else:
            payload = {"code": "message", "message": _safe_str(item, "")}

        key = (_safe_str(payload.get("code"), ""), _safe_str(payload.get("message"), ""))
        if key in seen:
            continue
        seen.add(key)
        target.append(payload)


def _build_invariant_status_if_available(app: Any) -> dict[str, Any]:
    """Build default-world invariant status if preferred helper is available."""
    try:
        from src.bootstrap.db_bootstrap import build_default_world_invariant_status

        result = build_default_world_invariant_status(app)
        return _to_plain_dict(result)
    except Exception as exc:
        return {
            "ok": None,
            "status": "unavailable",
            "error": _safe_exception_message(exc),
            "ready": {
                "project": None,
                "universe": None,
                "world": None,
            },
        }


def _bootstrap_indicates_seed_not_ready(result: Mapping[str, Any] | Any) -> bool:
    """Return whether a bootstrap result indicates seed/default-world not ready."""
    data = _to_plain_dict(result)

    if data.get("seed_ready") is False or data.get("seedReady") is False:
        return True

    if data.get("default_world_ready") is False or data.get("defaultWorldReady") is False:
        return True

    if data.get("project_access_ready") is False or data.get("projectAccessReady") is False:
        return True
    if data.get("project_owner_ready") is False or data.get("projectOwnerReady") is False:
        return True

    for key in (
        "system_blocks_ready",
        "systemBlocksReady",
        "system_railing_ready",
        "systemRailingReady",
        "air_invariant_ready",
        "airInvariantReady",
    ):
        if key in data and data.get(key) is False:
            return True

    seed = data.get("seed")
    if isinstance(seed, Mapping):
        if seed.get("ok") is False:
            return True
        if _safe_str(seed.get("status"), "").lower() in {"partial", "failed", "not_ready"}:
            return True

        world = seed.get("world")
        if isinstance(world, Mapping) and world.get("exists") is False:
            return True

        system_blocks = seed.get("systemBlocks") or seed.get("system_blocks")
        if isinstance(system_blocks, Mapping) and not _system_blocks_ready(system_blocks):
            return True

        invariant = seed.get("defaultWorldInvariant") or seed.get("invariantAfterRepair")
        if isinstance(invariant, Mapping):
            if invariant.get("ok") is False:
                return True
            ready = invariant.get("ready")
            if isinstance(ready, Mapping) and ready.get("world") is False:
                return True

    post_status = data.get("post_status") or data.get("postStatus")
    if isinstance(post_status, Mapping):
        if post_status.get("seedReady") is False:
            return True
        if post_status.get("defaultWorldReady") is False:
            return True
        if post_status.get("systemBlocksReady") is False:
            return True
        if post_status.get("systemRailingReady") is False:
            return True
        if post_status.get("airInvariantReady") is False:
            return True

        seed_payload = post_status.get("seed")
        if isinstance(seed_payload, Mapping):
            invariant = seed_payload.get("defaultWorldInvariant")
            if isinstance(invariant, Mapping):
                ready = invariant.get("ready")
                if isinstance(ready, Mapping) and ready.get("world") is False:
                    return True

    errors = data.get("errors") or []
    for item in errors:
        if not isinstance(item, Mapping):
            continue
        code = _safe_str(item.get("code"), "").lower()
        message = _safe_str(item.get("message"), "").lower()
        if "seed" in code or "world" in code or "invariant" in code:
            return True
        if "world" in message or "seed" in message or "invariant" in message:
            return True

    return False


def make_script_result(
    *,
    ok: bool,
    status: str,
    started_at: str,
    completed_at: str,
    args: argparse.Namespace,
    bootstrap_result: dict[str, Any] | None = None,
    error: str | None = None,
    traceback_text: str | None = None,
    service_root: Path | None = None,
) -> dict[str, Any]:
    """Build serializable script result."""
    bootstrap_result = bootstrap_result or {}
    summary = bootstrap_result.get("summary") or {}

    result = {
        "ok": bool(ok),
        "status": _safe_str(status, "unknown"),
        "resultVersion": SCRIPT_RESULT_VERSION,
        "startedAt": started_at,
        "completedAt": completed_at,
        "durationMs": _duration_ms(started_at, completed_at),
        "serviceRoot": str(service_root) if service_root is not None else "",
        "appFactory": args.app_factory,
        "configName": args.config,
        "createAll": bool(args.create_all),
        "seed": bool(args.seed),
        "checkOnly": bool(args.check_only),
        "mode": args.mode,
        "repairMissingColumns": bool(args.repair_missing_columns),
        "repairSeedInvariants": bool(args.repair_seed_invariants),
        "dryRunRepair": bool(args.dry_run_repair),
        "bootstrap": bootstrap_result,
        "summary": summary,
        "error": error,
    }

    if traceback_text:
        result["traceback"] = traceback_text

    if bootstrap_result:
        warnings = bootstrap_result.get("warnings") or []
        errors = bootstrap_result.get("errors") or []

        result.update(
            {
                "schemaReady": summary.get("schemaReady", bootstrap_result.get("schemaReady")),
                "seedReady": summary.get("seedReady", bootstrap_result.get("seedReady")),
                "defaultProjectReady": summary.get("defaultProjectReady", bootstrap_result.get("defaultProjectReady")),
                "defaultUniverseReady": summary.get("defaultUniverseReady", bootstrap_result.get("defaultUniverseReady")),
                "defaultWorldReady": summary.get("defaultWorldReady", bootstrap_result.get("defaultWorldReady")),
                "blockRegistryReady": summary.get("blockRegistryReady", bootstrap_result.get("blockRegistryReady")),
                "debugBlocksReady": summary.get("debugBlocksReady", bootstrap_result.get("debugBlocksReady")),
                "projectAccessReady": summary.get("projectAccessReady", bootstrap_result.get("projectAccessReady")),
                "projectOwnerAuthUserId": summary.get("projectOwnerAuthUserId", bootstrap_result.get("projectOwnerAuthUserId")),
                "systemBlocksReady": summary.get("systemBlocksReady", bootstrap_result.get("systemBlocksReady")),
                "systemRailingReady": summary.get("systemRailingReady", bootstrap_result.get("systemRailingReady")),
                "airInvariantReady": summary.get("airInvariantReady", bootstrap_result.get("airInvariantReady")),
                "systemBlockCount": summary.get("systemBlockCount", bootstrap_result.get("systemBlockCount")),
                "systemBlocksCreated": summary.get("systemBlocksCreated", bootstrap_result.get("systemBlocksCreated")),
                "systemBlocksUpdated": summary.get("systemBlocksUpdated", bootstrap_result.get("systemBlocksUpdated")),
                "systemBlocksMissing": summary.get("systemBlocksMissing", bootstrap_result.get("systemBlocksMissing")),
                "systemBlocksDrifted": summary.get("systemBlocksDrifted", bootstrap_result.get("systemBlocksDrifted")),
                "schemaBootstrapRequested": summary.get("schemaBootstrapRequested"),
                "schemaBootstrapExecuted": summary.get("schemaBootstrapExecuted"),
                "schemaBootstrapOk": summary.get("schemaBootstrapOk"),
                "schemaRepairRequested": summary.get("schemaRepairRequested"),
                "schemaRepairExecuted": summary.get("schemaRepairExecuted"),
                "schemaRepairOk": summary.get("schemaRepairOk"),
                "seedBootstrapRequested": summary.get("seedBootstrapRequested"),
                "seedBootstrapExecuted": summary.get("seedBootstrapExecuted"),
                "seedBootstrapOk": summary.get("seedBootstrapOk"),
                "seedInvariantRepairExecuted": summary.get("seedInvariantRepairExecuted", bootstrap_result.get("seed_invariant_repair_executed")),
                "seedInvariantRepairOk": summary.get("seedInvariantRepairOk", bootstrap_result.get("seed_invariant_repair_ok")),
                "warningCount": summary.get("warningCount", len(warnings)),
                "errorCount": summary.get("errorCount", len(errors)),
                "warnings": warnings,
                "errors": errors,
            }
        )

    return result


def summarize_bootstrap_result(result: Any) -> dict[str, Any]:
    """Build compact summary from bootstrap result."""
    try:
        from src.bootstrap.db_bootstrap import build_db_bootstrap_summary

        summary = build_db_bootstrap_summary(result)
        if isinstance(summary, dict):
            plain = _to_plain_dict(result)
            summary.setdefault("schemaReady", plain.get("schema_ready", plain.get("schemaReady")))
            summary.setdefault("seedReady", plain.get("seed_ready", plain.get("seedReady")))
            summary.setdefault("defaultProjectReady", plain.get("default_project_ready", plain.get("defaultProjectReady")))
            summary.setdefault("defaultUniverseReady", plain.get("default_universe_ready", plain.get("defaultUniverseReady")))
            summary.setdefault("defaultWorldReady", plain.get("default_world_ready", plain.get("defaultWorldReady")))
            summary.setdefault("blockRegistryReady", plain.get("block_registry_ready", plain.get("blockRegistryReady")))
            summary.setdefault("debugBlocksReady", plain.get("debug_blocks_ready", plain.get("debugBlocksReady")))
            summary.setdefault("projectAccessReady", plain.get("project_access_ready", plain.get("projectAccessReady")))
            summary.setdefault("projectOwnerAuthUserId", plain.get("project_owner_auth_user_id", plain.get("projectOwnerAuthUserId")))
            system_readiness = _extract_system_block_readiness(plain)
            for key, value in system_readiness.items():
                if key != "systemBlocks":
                    summary.setdefault(key, value)
            summary.setdefault("schemaRepairRequested", bool(plain.get("schema_repair_requested")))
            summary.setdefault("schemaRepairExecuted", bool(plain.get("schema_repair_executed")))
            summary.setdefault("schemaRepairOk", plain.get("schema_repair_ok"))
            summary.setdefault("seedInvariantRepairExecuted", bool(plain.get("seed_invariant_repair_executed")))
            summary.setdefault("seedInvariantRepairOk", plain.get("seed_invariant_repair_ok"))
            return summary
    except Exception:
        pass

    data = _to_plain_dict(result)
    warnings = data.get("warnings") or []
    errors = data.get("errors") or []

    return {
        "ok": bool(data.get("ok")),
        "status": _safe_str(data.get("status"), "unknown"),
        "enabled": bool(data.get("enabled")),
        "schemaReady": data.get("schema_ready", data.get("schemaReady")),
        "seedReady": data.get("seed_ready", data.get("seedReady")),
        "defaultProjectReady": data.get("default_project_ready", data.get("defaultProjectReady")),
        "defaultUniverseReady": data.get("default_universe_ready", data.get("defaultUniverseReady")),
        "defaultWorldReady": data.get("default_world_ready", data.get("defaultWorldReady")),
        "blockRegistryReady": data.get("block_registry_ready", data.get("blockRegistryReady")),
        "debugBlocksReady": data.get("debug_blocks_ready", data.get("debugBlocksReady")),
        "projectAccessReady": data.get("project_access_ready", data.get("projectAccessReady")),
        "projectOwnerAuthUserId": data.get("project_owner_auth_user_id", data.get("projectOwnerAuthUserId")),
        **{
            key: value
            for key, value in _extract_system_block_readiness(data).items()
            if key != "systemBlocks"
        },
        "schemaBootstrapRequested": bool(data.get("schema_bootstrap_requested")),
        "schemaBootstrapExecuted": bool(data.get("schema_bootstrap_executed")),
        "schemaBootstrapOk": data.get("schema_bootstrap_ok"),
        "schemaRepairRequested": bool(data.get("schema_repair_requested")),
        "schemaRepairExecuted": bool(data.get("schema_repair_executed")),
        "schemaRepairOk": data.get("schema_repair_ok"),
        "seedBootstrapRequested": bool(data.get("seed_bootstrap_requested")),
        "seedBootstrapExecuted": bool(data.get("seed_bootstrap_executed")),
        "seedBootstrapOk": data.get("seed_bootstrap_ok"),
        "seedInvariantRepairExecuted": bool(data.get("seed_invariant_repair_executed")),
        "seedInvariantRepairOk": data.get("seed_invariant_repair_ok"),
        "warningCount": len(warnings),
        "errorCount": len(errors),
        "durationMs": data.get("duration_ms"),
    }


def normalize_bootstrap_result(result: Any) -> dict[str, Any]:
    """Normalize DB bootstrap result to plain dict and attach summary."""
    data = _to_plain_dict(result)
    data.setdefault("warnings", [])
    data.setdefault("errors", [])
    data["summary"] = summarize_bootstrap_result(data)
    return data


def _check_only_result_inner(app: Any) -> dict[str, Any]:
    """Build read-only check-only status. Requires active app context."""
    try:
        from src.bootstrap.db_bootstrap import build_db_bootstrap_status

        status = build_db_bootstrap_status(app)
        schema_audit = _inspect_database_schema_inner()
        invariant_status = _build_invariant_status_if_available(app)

        schema_ready = bool(status.get("schemaReady")) and bool(schema_audit.get("ok"))
        seed_ready = bool(status.get("seedReady"))
        default_project_ready = status.get("defaultProjectReady")
        default_universe_ready = status.get("defaultUniverseReady")
        default_world_ready = status.get("defaultWorldReady")
        system_status = _extract_system_block_status(status)
        if not system_status:
            system_status = _extract_system_block_status(invariant_status)
        if not system_status:
            system_status = _build_system_block_status_inner()
        system_readiness = _extract_system_block_readiness(
            {
                "status": status,
                "seedInvariant": invariant_status,
                "systemBlocks": system_status,
            }
        )
        project_access_status = _build_project_access_status_inner()
        project_access_ready = _safe_bool(project_access_status.get("ready"), False)

        if default_project_ready is None:
            default_project_ready = _nested_bool(invariant_status, ("ready", "project"))
        if default_universe_ready is None:
            default_universe_ready = _nested_bool(invariant_status, ("ready", "universe"))
        if default_world_ready is None:
            default_world_ready = _nested_bool(invariant_status, ("ready", "world"))

        core_seed_ready = bool(
            default_project_ready is not False
            and default_universe_ready is not False
            and default_world_ready is not False
            and project_access_ready
            and system_readiness["systemBlocksReady"]
            and (
                not _debug_blocks_required()
                or status.get("debugBlocksReady") is not False
            )
        )
        if core_seed_ready:
            seed_ready = True

        ok = bool(
            schema_ready
            and seed_ready
            and core_seed_ready
        )

        errors = [] if ok else [
            {
                "code": "check_only_not_ready",
                "message": "DB bootstrap read-only status is not ready.",
                "details": {
                    "bootstrapStatus": status,
                    "schemaAudit": schema_audit,
                    "seedInvariant": invariant_status,
                },
            }
        ]

        result = {
            "ok": ok,
            "status": "ready" if ok else "not_ready",
            "enabled": False,
            "backend": "db_bootstrap_status",
            "schemaReady": schema_ready,
            "seedReady": seed_ready,
            "defaultProjectReady": default_project_ready,
            "defaultUniverseReady": default_universe_ready,
            "defaultWorldReady": default_world_ready,
            "blockRegistryReady": status.get("blockRegistryReady"),
            "debugBlocksReady": status.get("debugBlocksReady"),
            "projectAccessReady": project_access_ready,
            "projectOwnerAuthUserId": project_access_status.get("ownerAuthUserId"),
            "projectAccess": project_access_status,
            "systemBlocksReady": system_readiness["systemBlocksReady"],
            "systemRailingReady": system_readiness["systemRailingReady"],
            "airInvariantReady": system_readiness["airInvariantReady"],
            "systemBlockCount": system_readiness["systemBlockCount"],
            "systemBlocksCreated": system_readiness["systemBlocksCreated"],
            "systemBlocksUpdated": system_readiness["systemBlocksUpdated"],
            "systemBlocksMissing": system_readiness["systemBlocksMissing"],
            "systemBlocksDrifted": system_readiness["systemBlocksDrifted"],
            "systemBlocks": system_status,
            "schema_bootstrap_requested": False,
            "seed_bootstrap_requested": False,
            "schema_bootstrap_executed": False,
            "seed_bootstrap_executed": False,
            "seed_invariant_repair_executed": False,
            "schema_bootstrap_ok": schema_ready,
            "seed_bootstrap_ok": seed_ready,
            "seed_invariant_repair_ok": None,
            "schema_repair_requested": False,
            "schema_repair_executed": False,
            "schema_repair_ok": None,
            "warnings": [],
            "errors": errors,
            "pre_status": status,
            "schemaAudit": schema_audit,
            "seedInvariant": invariant_status,
        }
        result["summary"] = summarize_bootstrap_result(result)

        return result

    except Exception as exc:
        schema_audit = _inspect_database_schema_inner()
        system_status = _build_system_block_status_inner()
        system_readiness = _extract_system_block_readiness(system_status)
        project_access_status = _build_project_access_status_inner()
        project_access_ready = _safe_bool(project_access_status.get("ready"), False)
        ok = bool(
            schema_audit.get("ok")
            and project_access_ready
            and system_readiness["systemBlocksReady"]
        )

        errors = [] if ok else [
            {
                "code": "check_only_schema_not_ready",
                "message": "Schema audit is not ready.",
                "details": schema_audit,
            }
        ]

        warnings = [
            {
                "code": "db_bootstrap_status_unavailable",
                "message": _safe_exception_message(exc),
            }
        ]

        result = {
            "ok": ok,
            "status": "ready" if ok else "not_ready",
            "enabled": False,
            "backend": "schema_audit",
            "schemaReady": bool(schema_audit.get("ok")),
            "seedReady": None,
            "defaultProjectReady": None,
            "defaultUniverseReady": None,
            "defaultWorldReady": None,
            "blockRegistryReady": None,
            "debugBlocksReady": None,
            "projectAccessReady": project_access_ready,
            "projectOwnerAuthUserId": project_access_status.get("ownerAuthUserId"),
            "projectAccess": project_access_status,
            "systemBlocksReady": system_readiness["systemBlocksReady"],
            "systemRailingReady": system_readiness["systemRailingReady"],
            "airInvariantReady": system_readiness["airInvariantReady"],
            "systemBlockCount": system_readiness["systemBlockCount"],
            "systemBlocksCreated": system_readiness["systemBlocksCreated"],
            "systemBlocksUpdated": system_readiness["systemBlocksUpdated"],
            "systemBlocksMissing": system_readiness["systemBlocksMissing"],
            "systemBlocksDrifted": system_readiness["systemBlocksDrifted"],
            "systemBlocks": system_status,
            "schema_bootstrap_requested": False,
            "seed_bootstrap_requested": False,
            "schema_bootstrap_executed": False,
            "seed_bootstrap_executed": False,
            "seed_invariant_repair_executed": False,
            "schema_bootstrap_ok": bool(schema_audit.get("ok")),
            "seed_bootstrap_ok": None,
            "seed_invariant_repair_ok": None,
            "schema_repair_requested": False,
            "schema_repair_executed": False,
            "schema_repair_ok": None,
            "warnings": warnings,
            "errors": errors,
            "schemaAudit": schema_audit,
        }
        result["summary"] = summarize_bootstrap_result(result)

        return result


def _check_only_result(app: Any) -> dict[str, Any]:
    """Build read-only check-only status."""
    if _has_app_context():
        return _check_only_result_inner(app)

    with app.app_context():
        return _check_only_result_inner(app)


def _run_preferred_or_fallback_bootstrap(
    app: Any,
    args: argparse.Namespace,
    *,
    effective_create_all: bool,
    effective_seed: bool,
) -> dict[str, Any]:
    """
    Run DB bootstrap inside a Flask app context.

    Critical behavior:
    - Preferred src.bootstrap.db_bootstrap runs under app.app_context().
    - If preferred bootstrap returns ok=false without raising and tables are
      still missing, direct fallback db.create_all()/seed is attempted.
    - If preferred bootstrap returns ok=false because seed/default world is not
      ready, a direct seed invariant repair fallback is attempted.
    - Schema audit and repair also run under app context.
    """
    with app.app_context():
        if args.check_only:
            return _check_only_result_inner(app)

        try:
            from src.bootstrap.db_bootstrap import run_db_bootstrap

            raw_result = run_db_bootstrap(
                app,
                enabled=True,
                run_schema=effective_create_all,
                run_seed=effective_seed,
                fail_on_error=False,
                include_pre_status=True,
                include_post_status=True,
            )
            bootstrap_result = normalize_bootstrap_result(raw_result)
            bootstrap_result["backend"] = bootstrap_result.get("backend") or "src.bootstrap.db_bootstrap"

            schema_audit_before_repair = _inspect_database_schema_inner()
            bootstrap_result["schemaAuditBeforeRepair"] = schema_audit_before_repair

            preferred_ok = bool(bootstrap_result.get("ok"))
            missing_tables = list(schema_audit_before_repair.get("missingTables") or [])
            seed_not_ready = _bootstrap_indicates_seed_not_ready(bootstrap_result)

            if not preferred_ok and effective_create_all and missing_tables:
                fallback_result = _fallback_bootstrap(
                    app,
                    run_schema=effective_create_all,
                    run_seed=effective_seed,
                    repair_missing_columns=bool(args.repair_missing_columns),
                    dry_run_repair=bool(args.dry_run_repair),
                    repair_seed_invariants=bool(args.repair_seed_invariants),
                )
                fallback_result["backend"] = "fallback_after_preferred_failed_missing_tables"
                fallback_result["preferredBootstrap"] = bootstrap_result
                fallback_result.setdefault("warnings", []).append(
                    {
                        "code": "preferred_bootstrap_returned_not_ok",
                        "message": (
                            "Preferred src.bootstrap.db_bootstrap returned ok=false "
                            "and required tables were missing; direct fallback bootstrap was executed."
                        ),
                        "details": {
                            "preferredStatus": bootstrap_result.get("status"),
                            "missingTables": missing_tables,
                        },
                    }
                )
                fallback_result["summary"] = summarize_bootstrap_result(fallback_result)
                return fallback_result

            if not preferred_ok and effective_seed and seed_not_ready and args.repair_seed_invariants:
                seed_repair_result = _run_seed_invariant_repair(app)
                invariant_after = _build_invariant_status_if_available(app)
                schema_audit_after_seed_repair = _inspect_database_schema_inner()

                bootstrap_result["seedInvariantRepairFallback"] = seed_repair_result
                bootstrap_result["seedInvariantAfterFallbackRepair"] = invariant_after
                bootstrap_result["schemaAuditAfterSeedFallbackRepair"] = schema_audit_after_seed_repair
                bootstrap_result["seed_invariant_repair_executed"] = True
                bootstrap_result["seed_invariant_repair_ok"] = bool(seed_repair_result.get("ok"))

                _append_unique_errors(
                    bootstrap_result.setdefault("warnings", []),
                    seed_repair_result.get("warnings") or [],
                )
                _append_unique_errors(
                    bootstrap_result.setdefault("errors", []),
                    seed_repair_result.get("errors") or [],
                )

                default_project_ready = _nested_bool(invariant_after, ("ready", "project"))
                default_universe_ready = _nested_bool(invariant_after, ("ready", "universe"))
                default_world_ready = _nested_bool(invariant_after, ("ready", "world"))
                system_status_after = _extract_system_block_status(seed_repair_result)
                if not system_status_after:
                    system_status_after = _extract_system_block_status(invariant_after)
                if not system_status_after:
                    system_status_after = _build_system_block_status_inner()
                system_readiness_after = _extract_system_block_readiness(
                    {
                        "seedRepair": seed_repair_result,
                        "seedInvariant": invariant_after,
                        "systemBlocks": system_status_after,
                    }
                )
                project_access_repair_after = _repair_project_access_if_needed_inner(
                    commit=True,
                )
                project_access_after = _to_plain_dict(
                    project_access_repair_after.get("status")
                )
                project_access_ready_after = _safe_bool(
                    project_access_after.get("ready"),
                    False,
                )
                bootstrap_result["projectAccessRepairFallback"] = project_access_repair_after
                seed_ready = bool(
                    seed_repair_result.get("ok")
                    and default_world_ready is not False
                    and system_readiness_after["systemBlocksReady"]
                    and (not _project_access_required() or project_access_ready_after)
                    and (
                        not _debug_blocks_required()
                        or _nested_bool(invariant_after, ("ready", "debugBlocks")) is not False
                    )
                )
                schema_ready = bool(schema_audit_after_seed_repair.get("ok"))

                bootstrap_result["schemaReady"] = schema_ready
                bootstrap_result["seedReady"] = seed_ready
                bootstrap_result["defaultProjectReady"] = default_project_ready
                bootstrap_result["defaultUniverseReady"] = default_universe_ready
                bootstrap_result["defaultWorldReady"] = default_world_ready
                bootstrap_result["projectAccessReady"] = project_access_ready_after
                bootstrap_result["projectOwnerAuthUserId"] = project_access_after.get("ownerAuthUserId")
                bootstrap_result["projectAccess"] = project_access_after
                bootstrap_result["systemBlocksReady"] = system_readiness_after["systemBlocksReady"]
                bootstrap_result["systemRailingReady"] = system_readiness_after["systemRailingReady"]
                bootstrap_result["airInvariantReady"] = system_readiness_after["airInvariantReady"]
                bootstrap_result["systemBlockCount"] = system_readiness_after["systemBlockCount"]
                bootstrap_result["systemBlocksCreated"] = system_readiness_after["systemBlocksCreated"]
                bootstrap_result["systemBlocksUpdated"] = system_readiness_after["systemBlocksUpdated"]
                bootstrap_result["systemBlocksMissing"] = system_readiness_after["systemBlocksMissing"]
                bootstrap_result["systemBlocksDrifted"] = system_readiness_after["systemBlocksDrifted"]
                bootstrap_result["systemBlocks"] = system_status_after

                if (
                    schema_ready
                    and seed_ready
                    and default_world_ready
                    and system_readiness_after["systemBlocksReady"]
                    and (not _project_access_required() or project_access_ready_after)
                ):
                    bootstrap_result["ok"] = True
                    bootstrap_result["status"] = "completed"
                    bootstrap_result["errors"] = [
                        item
                        for item in bootstrap_result.get("errors", [])
                        if not (
                            isinstance(item, Mapping)
                            and (
                                "seed" in _safe_str(item.get("code"), "").lower()
                                or "world" in _safe_str(item.get("code"), "").lower()
                                or "invariant" in _safe_str(item.get("code"), "").lower()
                                or "system" in _safe_str(item.get("code"), "").lower()
                                or "railing" in _safe_str(item.get("code"), "").lower()
                                or "air" in _safe_str(item.get("code"), "").lower()
                                or "access" in _safe_str(item.get("code"), "").lower()
                                or "owner" in _safe_str(item.get("code"), "").lower()
                                or "role" in _safe_str(item.get("code"), "").lower()
                                or "assignment" in _safe_str(item.get("code"), "").lower()
                            )
                        )
                    ]
                    bootstrap_result.setdefault("warnings", []).append(
                        {
                            "code": "seed_invariant_repaired_after_preferred_not_ok",
                            "message": (
                                "Preferred bootstrap returned not-ok, but the seed/default-world "
                                "invariant was repaired successfully by the script fallback."
                            ),
                        }
                    )
                else:
                    bootstrap_result["ok"] = False
                    bootstrap_result.setdefault("errors", []).append(
                        {
                            "code": "seed_invariant_repair_fallback_failed",
                            "message": (
                                "Seed invariant repair fallback did not produce a ready "
                                "default world, project access and Air/Railing system-block state."
                            ),
                            "details": {
                                "seedRepair": seed_repair_result,
                                "invariantAfter": invariant_after,
                                "schemaAuditAfter": schema_audit_after_seed_repair,
                            },
                        }
                    )

            repair_result = {
                "ok": True,
                "executed": False,
                "status": "not_requested",
            }

            if args.repair_missing_columns:
                if schema_audit_before_repair.get("missingColumns"):
                    repair_result = _repair_missing_columns_inner(
                        dry_run=bool(args.dry_run_repair),
                    )

            schema_audit_after_repair = _inspect_database_schema_inner()
            bootstrap_result["schemaRepair"] = repair_result
            bootstrap_result["schemaAuditAfterRepair"] = schema_audit_after_repair
            bootstrap_result["schema_repair_requested"] = bool(args.repair_missing_columns)
            bootstrap_result["schema_repair_executed"] = bool(repair_result.get("executed"))
            bootstrap_result["schema_repair_ok"] = bool(repair_result.get("ok"))

            if repair_result.get("warnings"):
                _append_unique_errors(
                    bootstrap_result.setdefault("warnings", []),
                    repair_result.get("warnings") or [],
                )

            if repair_result.get("errors"):
                _append_unique_errors(
                    bootstrap_result.setdefault("errors", []),
                    repair_result.get("errors") or [],
                )

            if not schema_audit_after_repair.get("ok"):
                bootstrap_result.setdefault("errors", []).append(
                    {
                        "code": "schema_audit_not_ready",
                        "message": "Database schema is not ready after bootstrap.",
                        "details": {
                            "missingTables": schema_audit_after_repair.get("missingTables"),
                            "missingColumns": schema_audit_after_repair.get("missingColumns"),
                        },
                    }
                )
                bootstrap_result["ok"] = False

            invariant_status = _build_invariant_status_if_available(app)
            bootstrap_result["seedInvariantFinal"] = invariant_status
            bootstrap_result.setdefault("schemaReady", bool(schema_audit_after_repair.get("ok")))
            bootstrap_result.setdefault("defaultProjectReady", _nested_bool(invariant_status, ("ready", "project")))
            bootstrap_result.setdefault("defaultUniverseReady", _nested_bool(invariant_status, ("ready", "universe")))
            bootstrap_result.setdefault("defaultWorldReady", _nested_bool(invariant_status, ("ready", "world")))
            final_system_status = _extract_system_block_status(bootstrap_result)
            if not final_system_status:
                final_system_status = _extract_system_block_status(invariant_status)
            if not final_system_status:
                final_system_status = _build_system_block_status_inner()
            final_system_readiness = _extract_system_block_readiness(
                {
                    "bootstrap": bootstrap_result,
                    "seedInvariant": invariant_status,
                    "systemBlocks": final_system_status,
                }
            )
            bootstrap_result["systemBlocksReady"] = final_system_readiness["systemBlocksReady"]
            bootstrap_result["systemRailingReady"] = final_system_readiness["systemRailingReady"]
            bootstrap_result["airInvariantReady"] = final_system_readiness["airInvariantReady"]
            bootstrap_result["systemBlockCount"] = final_system_readiness["systemBlockCount"]
            bootstrap_result["systemBlocksCreated"] = final_system_readiness["systemBlocksCreated"]
            bootstrap_result["systemBlocksUpdated"] = final_system_readiness["systemBlocksUpdated"]
            bootstrap_result["systemBlocksMissing"] = final_system_readiness["systemBlocksMissing"]
            bootstrap_result["systemBlocksDrifted"] = final_system_readiness["systemBlocksDrifted"]
            bootstrap_result["systemBlocks"] = final_system_status
            if effective_seed:
                final_project_access_repair = _repair_project_access_if_needed_inner(
                    commit=True,
                )
            else:
                final_project_access_repair = {
                    "ok": True,
                    "changed": False,
                    "status": _build_project_access_status_inner(),
                }
            final_project_access = _to_plain_dict(
                final_project_access_repair.get("status")
            )
            final_project_access_ready = _safe_bool(
                final_project_access.get("ready"),
                False,
            )
            bootstrap_result["projectAccessRepair"] = final_project_access_repair
            bootstrap_result["projectAccessReady"] = final_project_access_ready
            bootstrap_result["projectOwnerAuthUserId"] = final_project_access.get("ownerAuthUserId")
            bootstrap_result["projectAccess"] = final_project_access

            if effective_seed and bootstrap_result.get("defaultWorldReady") is False:
                bootstrap_result.setdefault("errors", []).append(
                    {
                        "code": "default_world_not_ready_after_bootstrap",
                        "message": "Default concrete world is not ready after bootstrap.",
                        "details": invariant_status,
                    }
                )
                bootstrap_result["ok"] = False

            if effective_seed and _project_access_required() and not final_project_access_ready:
                bootstrap_result.setdefault("errors", []).append(
                    {
                        "code": "project_access_not_ready_after_bootstrap",
                        "message": "Default project owner/access projection is not ready after bootstrap.",
                        "details": final_project_access,
                    }
                )
                bootstrap_result["ok"] = False

            if effective_seed and not final_system_readiness["systemBlocksReady"]:
                bootstrap_result.setdefault("errors", []).append(
                    {
                        "code": "system_blocks_not_ready_after_bootstrap",
                        "message": (
                            "Air invariant or built-in Railing mirror is not ready "
                            "after bootstrap."
                        ),
                        "details": final_system_status,
                    }
                )
                bootstrap_result["ok"] = False

            hard_seed_ready = bool(
                not effective_seed
                or (
                    bootstrap_result.get("defaultWorldReady") is not False
                    and final_system_readiness["systemBlocksReady"]
                    and (not _project_access_required() or final_project_access_ready)
                    and (
                        not _debug_blocks_required()
                        or _nested_bool(invariant_status, ("ready", "debugBlocks")) is not False
                    )
                )
            )
            if hard_seed_ready:
                bootstrap_result["seedReady"] = True

            bootstrap_result["summary"] = summarize_bootstrap_result(bootstrap_result)
            return bootstrap_result

        except Exception as exc:
            bootstrap_result = _fallback_bootstrap(
                app,
                run_schema=effective_create_all,
                run_seed=effective_seed,
                repair_missing_columns=bool(args.repair_missing_columns),
                dry_run_repair=bool(args.dry_run_repair),
                repair_seed_invariants=bool(args.repair_seed_invariants),
            )
            bootstrap_result.setdefault("warnings", []).append(
                {
                    "code": "preferred_bootstrap_module_unavailable",
                    "message": (
                        "src.bootstrap.db_bootstrap could not be used; "
                        "fallback bootstrap path was executed."
                    ),
                    "details": {
                        "error": _safe_exception_message(exc),
                    },
                }
            )
            bootstrap_result["summary"] = summarize_bootstrap_result(bootstrap_result)
            return bootstrap_result


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------

def build_arg_parser() -> argparse.ArgumentParser:
    """Build argument parser."""
    parser = argparse.ArgumentParser(
        description="Bootstrap the VECTOPLAN Chunk Service database explicitly.",
    )

    parser.add_argument(
        "--app-factory",
        default=os.getenv("VECTOPLAN_CHUNK_BOOTSTRAP_APP_FACTORY", "app:create_app"),
        help="Flask app factory target. Default: app:create_app",
    )
    parser.add_argument(
        "--config",
        default=os.getenv("VECTOPLAN_CHUNK_CONFIG", None),
        help="Optional config name passed to create_app(config).",
    )
    parser.add_argument(
        "--mode",
        default=os.getenv("VECTOPLAN_CHUNK_MODE", "db-bootstrap"),
        help="Bootstrap mode value placed in VECTOPLAN_CHUNK_MODE. Default: db-bootstrap",
    )

    create_group = parser.add_mutually_exclusive_group()
    create_group.add_argument(
        "--create-all",
        dest="create_all",
        action="store_true",
        default=DEFAULT_CREATE_ALL,
        help="Run schema bootstrap using db.create_all(). Default: enabled.",
    )
    create_group.add_argument(
        "--no-create-all",
        dest="create_all",
        action="store_false",
        help="Do not run db.create_all().",
    )

    seed_group = parser.add_mutually_exclusive_group()
    seed_group.add_argument(
        "--seed",
        dest="seed",
        action="store_true",
        default=DEFAULT_SEED,
        help=(
            "Run default seed bootstrap, including built-in Air/Railing "
            "system-block reconciliation. Default: enabled."
        ),
    )
    seed_group.add_argument(
        "--no-seed",
        dest="seed",
        action="store_false",
        help="Do not run default seed bootstrap.",
    )

    parser.add_argument(
        "--check-only",
        action="store_true",
        help="Only build read-only DB bootstrap status. No create_all and no seed.",
    )

    repair_default = _env_bool("VECTOPLAN_CHUNK_BOOTSTRAP_REPAIR_MISSING_COLUMNS", True)
    repair_group = parser.add_mutually_exclusive_group()
    repair_group.add_argument(
        "--repair-missing-columns",
        dest="repair_missing_columns",
        action="store_true",
        default=repair_default,
        help=(
            "Best-effort local/dev repair for missing columns after create_all. "
            "Default follows VECTOPLAN_CHUNK_BOOTSTRAP_REPAIR_MISSING_COLUMNS, true if unset."
        ),
    )
    repair_group.add_argument(
        "--no-repair-missing-columns",
        dest="repair_missing_columns",
        action="store_false",
        help="Do not repair missing columns.",
    )

    seed_repair_default = _env_bool(
        "VECTOPLAN_CHUNK_DB_BOOTSTRAP_REPAIR_SEED_INVARIANTS",
        True,
    )
    seed_repair_group = parser.add_mutually_exclusive_group()
    seed_repair_group.add_argument(
        "--repair-seed-invariants",
        dest="repair_seed_invariants",
        action="store_true",
        default=seed_repair_default,
        help=(
            "Repair partial seed invariants such as missing world_spawn or "
            "a missing/drifted system_railing mirror. "
            "Default follows VECTOPLAN_CHUNK_DB_BOOTSTRAP_REPAIR_SEED_INVARIANTS, true if unset."
        ),
    )
    seed_repair_group.add_argument(
        "--no-repair-seed-invariants",
        dest="repair_seed_invariants",
        action="store_false",
        help="Do not repair partial seed/default-world invariants.",
    )

    parser.add_argument(
        "--dry-run-repair",
        action="store_true",
        help="Show missing-column repair DDL without executing it.",
    )

    parser.add_argument(
        "--fail-on-error",
        dest="fail_on_error",
        action="store_true",
        default=True,
        help="Return failure exit code on bootstrap errors. Default: true.",
    )
    parser.add_argument(
        "--no-fail-on-error",
        dest="fail_on_error",
        action="store_false",
        help="Do not raise/fail process on bootstrap errors.",
    )

    parser.add_argument(
        "--json",
        action="store_true",
        default=DEFAULT_JSON,
        help="Print full JSON result.",
    )
    parser.add_argument(
        "--compact-json",
        action="store_true",
        help="Print compact JSON result.",
    )
    parser.add_argument(
        "--debug-traceback",
        action="store_true",
        help="Include traceback in JSON result on failures.",
    )
    parser.add_argument(
        "--allow-runtime-startup-hooks",
        action="store_true",
        help=(
            "Do not force VECTOPLAN_CHUNK_RUN_STARTUP_HOOKS=false before app import. "
            "Use only for diagnostics."
        ),
    )

    return parser


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------

def main(argv: Sequence[str] | None = None) -> int:
    """Script entrypoint."""
    parser = build_arg_parser()

    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        try:
            return int(exc.code)
        except Exception:
            return EXIT_INVALID_ARGS

    started_at = _utc_now_iso()
    service_root = resolve_service_root()

    try:
        configure_python_path(service_root)

        effective_create_all = bool(args.create_all)
        effective_seed = bool(args.seed)

        if args.check_only:
            effective_create_all = False
            effective_seed = False

        set_default_env(
            create_all=effective_create_all,
            seed=effective_seed,
            check_only=bool(args.check_only),
            mode=args.mode,
            force_runtime_hooks_off=not bool(args.allow_runtime_startup_hooks),
            repair_missing_columns=bool(args.repair_missing_columns),
            repair_seed_invariants=bool(args.repair_seed_invariants),
        )

        app = create_flask_app(
            app_factory=args.app_factory,
            config_name=args.config,
        )

    except Exception as exc:
        completed_at = _utc_now_iso()
        traceback_text = traceback.format_exc() if args.debug_traceback else None

        result = make_script_result(
            ok=False,
            status="app_failed",
            started_at=started_at,
            completed_at=completed_at,
            args=args,
            error=_safe_exception_message(exc),
            traceback_text=traceback_text,
            service_root=service_root,
        )

        if args.json or args.compact_json:
            _print_json(result, pretty=not args.compact_json)
        else:
            _print_human_result(result)
            if traceback_text:
                print(traceback_text)

        return EXIT_APP_FAILED

    try:
        bootstrap_result = _run_preferred_or_fallback_bootstrap(
            app,
            args,
            effective_create_all=effective_create_all,
            effective_seed=effective_seed,
        )

        if "summary" not in bootstrap_result:
            bootstrap_result["summary"] = summarize_bootstrap_result(bootstrap_result)

        ok = bool(bootstrap_result.get("ok"))
        completed_at = _utc_now_iso()

        script_result = make_script_result(
            ok=ok,
            status="completed" if ok else "failed",
            started_at=started_at,
            completed_at=completed_at,
            args=args,
            bootstrap_result=bootstrap_result,
            service_root=service_root,
        )

        if args.json or args.compact_json:
            _print_json(script_result, pretty=not args.compact_json)
        else:
            _print_human_result(script_result)

        if ok:
            return EXIT_OK

        return EXIT_BOOTSTRAP_FAILED if args.fail_on_error else EXIT_OK

    except Exception as exc:
        completed_at = _utc_now_iso()
        traceback_text = traceback.format_exc() if args.debug_traceback else None

        script_result = make_script_result(
            ok=False,
            status="bootstrap_failed",
            started_at=started_at,
            completed_at=completed_at,
            args=args,
            error=_safe_exception_message(exc),
            traceback_text=traceback_text,
            service_root=service_root,
        )

        if args.json or args.compact_json:
            _print_json(script_result, pretty=not args.compact_json)
        else:
            _print_human_result(script_result)
            if traceback_text:
                print(traceback_text)

        return EXIT_BOOTSTRAP_FAILED if args.fail_on_error else EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())