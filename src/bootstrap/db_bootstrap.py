# services/vectoplan-chunk/src/bootstrap/db_bootstrap.py
"""
Explicit database bootstrap orchestrator for the `vectoplan-chunk` service.

This module coordinates the controlled DB bootstrap path.

Responsibilities:
- build effective bootstrap settings,
- run schema bootstrap when explicitly enabled,
- run default seed bootstrap when explicitly enabled,
- ensure schema bootstrap runs before seed bootstrap,
- repair partial default seed invariants in explicit bootstrap mode,
- ensure the concrete editable default world `world_spawn` exists,
- repair the canonical `auth_dev_owner` project owner on local/dev bootstrap,
- require canonical and legacy project-access projections for the dev project,
- require the default runtime BlockRegistry,
- require the reserved Air persistence invariant,
- require the canonical `system_railing` BlockType mirror,
- reject inactive, deleted or drifted built-in system-block mirrors,
- prevent seed bootstrap after failed schema bootstrap,
- collect read-only pre/post status,
- cleanup SQLAlchemy sessions after each phase,
- return a serializable aggregate result for scripts/logs/status output.

Important boundaries:
- no Flask app creation here,
- no Gunicorn startup integration here,
- no request handling here,
- no chunk generation here,
- no command execution here,
- no Snapshot/Event/Command/ObjectRef traversal here,
- no Alembic migration execution here.

Design rule:

    Normal runtime startup must not call this module automatically.
    This module is intended for an explicit init command/container.

Seed invariant rule:

    Schema-ready is not enough.

    The local/default chunk world is considered ready only when these objects
    exist and are linked:

        Project.project_id      = dev-project
        Universe.universe_id    = dev-universe
        WorldInstance.world_id  = world_spawn
        Project.owner_auth_user_id = auth_dev_owner (or persisted canonical owner)
        ProjectAccessAssignment      = exactly one direct owner
        ProjectRole keys             = owner/admin/editor/viewer
        BlockRegistry                = debug-blocks@1
        Air cell state             = cellValue 0, without BlockType row
        BlockType.block_type_id    = system_railing

    `system_railing` must be active and match its immutable code definition.
    `system_air` must not exist as a persistent BlockType row.

    `world_spawn` is the concrete editable world.
    `flat` is only the template/provider id.

Typical call site:

    app = create_app()
    result = run_db_bootstrap(app)

Later, when Alembic is introduced, schema_bootstrap can be replaced or extended
without changing normal runtime startup behavior.
"""

from __future__ import annotations

import hashlib
import importlib
import json

from contextlib import nullcontext
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
from types import MappingProxyType, SimpleNamespace
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
    from .db_locks import build_lock_diagnostics, safe_session_cleanup
except Exception:  # pragma: no cover - fallback for direct import tests
    build_lock_diagnostics = None  # type: ignore[assignment]
    safe_session_cleanup = None  # type: ignore[assignment]

_DEFAULT_SEED_IMPORT_ERROR: str | None = None

try:
    from .default_seed import (
        build_default_project_access_status,
        build_default_seed_status,
        build_default_seed_summary,
        build_default_system_blocks_status,
        default_seed_result_to_dict,
        run_default_seed,
        seed_default_project_access,
    )
except Exception as exc:  # pragma: no cover - fallback for partial import tests
    _DEFAULT_SEED_IMPORT_ERROR = (
        f"{exc.__class__.__name__}: {str(exc) or exc.__class__.__name__}"
    )
    build_default_project_access_status = None  # type: ignore[assignment]
    build_default_seed_status = None  # type: ignore[assignment]
    build_default_seed_summary = None  # type: ignore[assignment]
    build_default_system_blocks_status = None  # type: ignore[assignment]
    default_seed_result_to_dict = None  # type: ignore[assignment]
    run_default_seed = None  # type: ignore[assignment]
    seed_default_project_access = None  # type: ignore[assignment]

try:
    from .schema_bootstrap import (
        build_schema_bootstrap_summary,
        build_schema_status,
        run_schema_bootstrap,
        schema_bootstrap_result_to_dict,
    )
except Exception:  # pragma: no cover - fallback for partial import tests
    build_schema_bootstrap_summary = None  # type: ignore[assignment]
    build_schema_status = None  # type: ignore[assignment]
    run_schema_bootstrap = None  # type: ignore[assignment]
    schema_bootstrap_result_to_dict = None  # type: ignore[assignment]

try:
    from .settings import (
        BootstrapSettings,
        build_bootstrap_settings,
        get_bool_setting,
        get_str_setting,
    )
except Exception:  # pragma: no cover - fallback for direct import tests
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

    def get_str_setting(
        app: Any,
        key: str,
        default: str = "",
        aliases: Sequence[str] | None = None,
        prefer_env: bool = True,
    ) -> str:
        keys = (key, *(aliases or ()))
        for candidate in keys:
            try:
                value = getattr(app, "config", {}).get(candidate)
            except Exception:
                value = None
            text_value = str(value).strip() if value is not None else ""
            if text_value:
                return text_value
        return default


# -----------------------------------------------------------------------------
# Constants
# -----------------------------------------------------------------------------

DB_BOOTSTRAP_RESULT_VERSION: Final[str] = "db-bootstrap-result.v5"

STATUS_COMPLETED: Final[str] = "completed"
STATUS_SKIPPED: Final[str] = "skipped"
STATUS_FAILED: Final[str] = "failed"
STATUS_PARTIAL: Final[str] = "partial"
STATUS_READY: Final[str] = "ready"

STEP_STATUS_OK: Final[str] = "ok"
STEP_STATUS_SKIPPED: Final[str] = "skipped"
STEP_STATUS_FAILED: Final[str] = "failed"
STEP_STATUS_WARNING: Final[str] = "warning"

STEP_SCHEMA_STATUS_BEFORE: Final[str] = "schema_status_before"
STEP_SCHEMA_BOOTSTRAP: Final[str] = "schema_bootstrap"
STEP_SEED_STATUS_BEFORE: Final[str] = "seed_status_before"
STEP_DEFAULT_SEED: Final[str] = "default_seed"
STEP_DEFAULT_SEED_INVARIANT_REPAIR: Final[str] = "default_seed_invariant_repair"
STEP_SCHEMA_STATUS_AFTER: Final[str] = "schema_status_after"
STEP_SEED_STATUS_AFTER: Final[str] = "seed_status_after"

DEFAULT_PROJECT_ID: Final[str] = "dev-project"
DEFAULT_UNIVERSE_ID: Final[str] = "dev-universe"
DEFAULT_WORLD_ID: Final[str] = "world_spawn"
DEFAULT_TEMPLATE_ID: Final[str] = "flat"
DEFAULT_PROVIDER_ID: Final[str] = "flat"
DEFAULT_PROVIDER_WORLD_ID: Final[str] = "flat"
DEFAULT_BLOCK_REGISTRY_ID: Final[str] = "debug-blocks"
DEFAULT_BLOCK_REGISTRY_VERSION: Final[str] = "1"
DEFAULT_BLOCK_REGISTRY_SOURCE: Final[str] = "internal"
BLOCK_REGISTRY_ALLOWED_SOURCES: Final[tuple[str, ...]] = (
    "internal",
    "library",
    "imported",
    "test",
)
DEFAULT_SYSTEM_AIR_BLOCK_ID: Final[str] = "system_air"
DEFAULT_SYSTEM_RAILING_BLOCK_TYPE_ID: Final[str] = "system_railing"
DEFAULT_SYSTEM_BLOCK_BOOTSTRAP_USER_ID: Final[str] = "vectoplan-system-block-bootstrap"

DEFAULT_PROJECT_OWNER_AUTH_USER_ID: Final[str] = "auth_dev_owner"
DEFAULT_PROJECT_ACCESS_SOURCE_SERVICE: Final[str] = "vectoplan-app"
DEFAULT_PROJECT_ACCESS_SERVICE_ID: Final[str] = "vectoplan-chunk-init"
DEFAULT_PROJECT_ACCESS_PROJECTION_VERSION: Final[str] = "app-project-access-v1"
DEFAULT_PROJECT_ACCESS_ROLE: Final[str] = "owner"
DEFAULT_PROJECT_ACCESS_ASSIGNMENT_TYPE: Final[str] = "direct"
DEFAULT_PROJECT_ROLE_KEYS: Final[tuple[str, ...]] = (
    "owner",
    "admin",
    "editor",
    "viewer",
)

_DEFAULT_SEED_EXPORT_NAMES: Final[tuple[str, ...]] = (
    "build_default_project_access_status",
    "build_default_seed_status",
    "build_default_seed_summary",
    "build_default_system_blocks_status",
    "default_seed_result_to_dict",
    "run_default_seed",
    "seed_default_project_access",
)


@lru_cache(maxsize=1)
def _load_default_seed_api() -> Mapping[str, Any]:
    """
    Load available default-seed exports lazily.

    The eager relative import above is retained for compatibility, but one
    missing export or an import-order issue must not disable every seed helper.
    Import failures are not cached by functools.lru_cache, so a later retry can
    succeed after package initialization has completed.
    """
    candidates: list[tuple[str, str | None]] = []
    package_name = _safe_str(globals().get("__package__"), "")
    if package_name:
        candidates.append((".default_seed", package_name))
    candidates.extend(
        (
            ("src.bootstrap.default_seed", None),
            ("bootstrap.default_seed", None),
        )
    )

    import_errors: list[str] = []
    seen: set[tuple[str, str | None]] = set()
    for module_name, package in candidates:
        key = (module_name, package)
        if key in seen:
            continue
        seen.add(key)
        try:
            module = importlib.import_module(module_name, package=package)
        except Exception as exc:
            import_errors.append(
                f"{module_name}: {exc.__class__.__name__}: "
                f"{_safe_exception_message(exc)}"
            )
            continue

        exports: dict[str, Any] = {
            "module": module,
            "moduleName": _safe_str(getattr(module, "__name__", None), module_name),
            "modulePath": _safe_str(getattr(module, "__file__", None), ""),
        }
        for export_name in _DEFAULT_SEED_EXPORT_NAMES:
            value = getattr(module, export_name, None)
            if callable(value):
                exports[export_name] = value

        if len(exports) > 3:
            exports["missingExports"] = [
                name for name in _DEFAULT_SEED_EXPORT_NAMES if name not in exports
            ]
            exports["eagerImportError"] = _DEFAULT_SEED_IMPORT_ERROR
            return MappingProxyType(exports)

        import_errors.append(
            f"{module_name}: module imported but no supported seed exports exist"
        )

    details = " | ".join(import_errors)
    if _DEFAULT_SEED_IMPORT_ERROR:
        details = (
            f"eager import: {_DEFAULT_SEED_IMPORT_ERROR}"
            + (f" | {details}" if details else "")
        )
    raise RuntimeError(
        "Could not import any usable default-seed API."
        + (f" {details}" if details else "")
    )


def _get_default_seed_callable(name: str) -> Any | None:
    """Resolve one seed callable without making all exports interdependent."""
    current = globals().get(name)
    if callable(current):
        return current
    try:
        api = _load_default_seed_api()
    except Exception:
        return None
    value = api.get(name)
    return value if callable(value) else None


def _default_seed_api_diagnostics() -> dict[str, Any]:
    """Return bounded import diagnostics without exposing tracebacks."""
    try:
        api = _load_default_seed_api()
        return {
            "available": True,
            "moduleName": api.get("moduleName"),
            "modulePath": api.get("modulePath"),
            "missingExports": list(api.get("missingExports") or []),
            "eagerImportError": api.get("eagerImportError"),
        }
    except Exception as exc:
        return {
            "available": False,
            "eagerImportError": _DEFAULT_SEED_IMPORT_ERROR,
            "error": _safe_exception_message(exc),
        }


def clear_db_bootstrap_default_seed_caches() -> None:
    """Clear immutable default-seed import caches only."""
    _load_default_seed_api.cache_clear()


# -----------------------------------------------------------------------------
# Data structures
# -----------------------------------------------------------------------------

@dataclass(slots=True)
class DbBootstrapMessage:
    """Serializable DB bootstrap warning/error."""

    code: str
    message: str
    timestamp: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class DbBootstrapStep:
    """Serializable DB bootstrap step."""

    name: str
    ok: bool
    status: str
    skipped: bool = False
    message: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    duration_ms: int = 0
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class DbBootstrapResult:
    """Serializable aggregate DB bootstrap result."""

    ok: bool
    status: str
    result_version: str = DB_BOOTSTRAP_RESULT_VERSION

    started_at: str | None = None
    completed_at: str | None = None
    duration_ms: int = 0

    enabled: bool = False
    schema_bootstrap_requested: bool = False
    seed_bootstrap_requested: bool = False

    schema_bootstrap_executed: bool = False
    seed_bootstrap_executed: bool = False
    seed_invariant_repair_executed: bool = False

    schema_bootstrap_ok: bool | None = None
    seed_bootstrap_ok: bool | None = None
    seed_invariant_repair_ok: bool | None = None

    schema_ready: bool | None = None
    seed_ready: bool | None = None

    default_project_ready: bool | None = None
    default_universe_ready: bool | None = None
    default_world_ready: bool | None = None

    project_owner_ready: bool | None = None
    project_access_ready: bool | None = None
    canonical_project_access_ready: bool | None = None
    legacy_project_access_ready: bool | None = None
    project_owner_auth_user_id: str | None = None
    project_access_assignment_count: int = 0
    project_access_role_count: int = 0

    block_registry_ready: bool | None = None
    debug_blocks_ready: bool | None = None
    debug_blocks_required: bool = False

    system_blocks_ready: bool | None = None
    system_railing_ready: bool | None = None
    air_invariant_ready: bool | None = None

    system_block_count: int = 0
    system_blocks_created: int = 0
    system_blocks_updated: int = 0
    system_blocks_missing: int = 0
    system_blocks_drifted: int = 0

    fail_on_error: bool = True

    steps: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)

    schema: dict[str, Any] = field(default_factory=dict)
    seed: dict[str, Any] = field(default_factory=dict)
    seed_invariant: dict[str, Any] = field(default_factory=dict)
    project_access: dict[str, Any] = field(default_factory=dict)
    system_blocks: dict[str, Any] = field(default_factory=dict)
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


def _safe_int(value: Any, default: int = 0) -> int:
    """Normalize value as int."""
    if isinstance(value, bool):
        return int(value)

    if isinstance(value, int):
        return value

    try:
        return int(value)
    except Exception:
        return default


def _safe_float(value: Any, default: float = 0.0) -> float:
    """Normalize value as float."""
    if isinstance(value, (int, float)):
        return float(value)

    try:
        return float(value)
    except Exception:
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

    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        try:
            result = to_dict()
            if isinstance(result, dict):
                return result
        except Exception:
            return {}

    return {}



def _looks_like_unsafe_auth_user_id(value: Any) -> bool:
    """Return whether a value is a local/placeholder identity, not an auth id."""
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

    allowed = set(
        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.:-"
    )
    return len(text) > 255 or any(character not in allowed for character in text)


def _normalize_auth_user_id(
    value: Any,
    default: str = DEFAULT_PROJECT_OWNER_AUTH_USER_ID,
) -> str:
    """Normalize a canonical cross-service auth id and replace legacy ids."""
    candidate = _safe_str(value, "")
    if not _looks_like_unsafe_auth_user_id(candidate):
        return candidate

    fallback = _safe_str(default, "")
    if fallback and not _looks_like_unsafe_auth_user_id(fallback):
        return fallback
    return ""


def _configured_project_owner_auth_user_id(
    app: Any,
    settings: Any = None,
) -> str:
    """Resolve the canonical default-project owner from new and legacy settings."""
    world_defaults = getattr(settings, "world_defaults", None) if settings else None
    for attribute_name in (
        "project_owner_auth_user_id",
        "default_project_owner_auth_user_id",
        "owner_auth_user_id",
        "project_owner_user_id",
        "default_project_owner_user_id",
        "owner_user_id",
    ):
        try:
            value = getattr(world_defaults, attribute_name, None)
        except Exception:
            value = None
        normalized = _normalize_auth_user_id(value, "")
        if normalized:
            return normalized

    raw_value = get_str_setting(
        app,
        "VECTOPLAN_CHUNK_DEFAULT_PROJECT_OWNER_AUTH_USER_ID",
        DEFAULT_PROJECT_OWNER_AUTH_USER_ID,
        aliases=(
            "VECTOPLAN_CHUNK_DEFAULT_OWNER_AUTH_USER_ID",
            "VECTOPLAN_CHUNK_PROJECT_OWNER_AUTH_USER_ID",
            "VECTOPLAN_CHUNK_DEV_PROJECT_OWNER_AUTH_USER_ID",
            "VECTOPLAN_CHUNK_DEFAULT_PROJECT_OWNER_USER_ID",
            "VECTOPLAN_CHUNK_DEFAULT_OWNER_USER_ID",
            "VECTOPLAN_CHUNK_PROJECT_OWNER_USER_ID",
        ),
        prefer_env=True,
    )
    return _normalize_auth_user_id(
        raw_value,
        DEFAULT_PROJECT_OWNER_AUTH_USER_ID,
    )


def _project_owner_from_model(project: Any, fallback: str) -> str:
    """Return a canonical stored owner, ignoring numeric/placeholder values."""
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
    """Repair compatible owner/actor fields to one canonical auth identity."""
    owner = _normalize_auth_user_id(
        owner_auth_user_id,
        DEFAULT_PROJECT_OWNER_AUTH_USER_ID,
    )
    if not owner:
        raise RuntimeError("A canonical project owner auth_user_id is required.")

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


def _project_owner_fields_ready(project: Any, owner_auth_user_id: str) -> bool:
    """Return whether mapped project owner fields carry one canonical identity."""
    owner = _normalize_auth_user_id(owner_auth_user_id, "")
    if project is None or not owner:
        return False

    actual = _project_owner_from_model(project, "")
    if actual != owner:
        return False

    owner_type = _safe_str(getattr(project, "owner_type", "user"), "user").lower()
    if owner_type != "user":
        return False

    for name in ("owner_auth_user_id", "owner_id"):
        if not hasattr(project, name) and not _model_has_column(project.__class__, name):
            continue
        value = _safe_str(getattr(project, name, None), "")
        if value and _normalize_auth_user_id(value, "") != owner:
            return False
    return True


def _project_access_required(app: Any, seed_settings: Any = None) -> bool:
    """Return whether access is required for mutation or read-only readiness.

    When seed settings are supplied, project seeding implies access seeding.
    Read-only status checks deliberately ignore disabled mutation flags and follow
    the runtime access-control requirement instead.
    """
    if seed_settings is not None:
        try:
            explicit = getattr(seed_settings, "seed_project_access", None)
        except Exception:
            explicit = None
        try:
            seed_project = getattr(seed_settings, "seed_dev_project", None)
        except Exception:
            seed_project = None
        if explicit is not None or seed_project is not None:
            return bool(
                _safe_bool(explicit, False)
                or _safe_bool(seed_project, False)
            )

    return get_bool_setting(
        app,
        "VECTOPLAN_CHUNK_PROJECT_ACCESS_REQUIRED",
        get_bool_setting(
            app,
            "VECTOPLAN_CHUNK_ACCESS_CONTROL_ENABLED",
            True,
            aliases=("CHUNK_ACCESS_CONTROL_ENABLED",),
            prefer_env=True,
        ),
        aliases=("CHUNK_PROJECT_ACCESS_REQUIRED",),
        prefer_env=True,
    )


def _debug_blocks_required(app: Any, seed_settings: Any = None) -> bool:
    """Return whether debug block rows are an explicit seed requirement."""
    try:
        explicit = getattr(seed_settings, "seed_debug_blocks", None)
    except Exception:
        explicit = None
    if explicit is not None:
        return _safe_bool(explicit, False)

    return get_bool_setting(
        app,
        "VECTOPLAN_CHUNK_DB_BOOTSTRAP_SEED_DEBUG_BLOCKS",
        get_bool_setting(
            app,
            "VECTOPLAN_CHUNK_SEED_DEBUG_BLOCKS",
            False,
            aliases=("CHUNK_SEED_DEBUG_BLOCKS",),
            prefer_env=True,
        ),
        aliases=("CHUNK_DB_BOOTSTRAP_SEED_DEBUG_BLOCKS",),
        prefer_env=True,
    )


def _result_to_dict_with_variants(value: Any) -> dict[str, Any]:
    """Serialize service results whose to_dict signatures differ by generation."""
    data = _safe_dict(value)
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


def _project_access_projection_fingerprint(
    project_id: str,
    owner_auth_user_id: str,
) -> str:
    """Build the deterministic one-owner bootstrap projection fingerprint."""
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
    """Create serializable bootstrap message."""
    return asdict(
        DbBootstrapMessage(
            code=_safe_str(code, "db_bootstrap_message"),
            message=_safe_str(message, ""),
            timestamp=_utc_now_iso(),
            details=details or {},
        )
    )


def _make_step(
    name: str,
    ok: bool,
    status: str,
    *,
    skipped: bool = False,
    message: str | None = None,
    started_at: str | None = None,
    completed_at: str | None = None,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create serializable bootstrap step."""
    started_at = started_at or _utc_now_iso()
    completed_at = completed_at or _utc_now_iso()

    return asdict(
        DbBootstrapStep(
            name=_safe_str(name, "step"),
            ok=bool(ok),
            status=_safe_str(status, STEP_STATUS_FAILED),
            skipped=bool(skipped),
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


def _app_context(app: Any) -> Any:
    """Return app context if needed, otherwise nullcontext."""
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
    """Return effective Flask-SQLAlchemy extension."""
    return db_extension if db_extension is not None else default_db


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


def _config_value(app: Any, key: str, default: Any = None) -> Any:
    """Read app.config value robustly."""
    try:
        if app is not None and hasattr(app, "config"):
            return app.config.get(key, default)
    except Exception:
        pass

    return default


def _config_str(app: Any, key: str, default: str) -> str:
    """Read config string."""
    value = _config_value(app, key, default)
    return _safe_str(value, default)


def _config_bool(app: Any, key: str, default: bool = False) -> bool:
    """Read config bool."""
    value = _config_value(app, key, default)
    return _safe_bool(value, default)


def _default_project_id(app: Any) -> str:
    return _config_str(app, "VECTOPLAN_CHUNK_DEFAULT_PROJECT_ID", DEFAULT_PROJECT_ID)


def _default_universe_id(app: Any) -> str:
    return _config_str(app, "VECTOPLAN_CHUNK_DEFAULT_UNIVERSE_ID", DEFAULT_UNIVERSE_ID)


def _default_world_id(app: Any) -> str:
    explicit = _config_value(app, "VECTOPLAN_CHUNK_DEFAULT_INSTANCE_WORLD_ID", None)
    if explicit:
        return _safe_str(explicit, DEFAULT_WORLD_ID)

    default_world = _config_value(app, "VECTOPLAN_CHUNK_DEFAULT_WORLD_ID", DEFAULT_WORLD_ID)
    candidate = _safe_str(default_world, DEFAULT_WORLD_ID)

    if candidate in {
        _default_template_id(app),
        _default_provider_id(app),
        _default_provider_world_id(app),
    }:
        return DEFAULT_WORLD_ID

    return candidate or DEFAULT_WORLD_ID


def _default_template_id(app: Any) -> str:
    return _config_str(app, "VECTOPLAN_CHUNK_DEFAULT_TEMPLATE_ID", DEFAULT_TEMPLATE_ID)


def _default_provider_id(app: Any) -> str:
    return _config_str(app, "VECTOPLAN_CHUNK_DEFAULT_PROVIDER_ID", DEFAULT_PROVIDER_ID)


def _default_provider_world_id(app: Any) -> str:
    return _config_str(
        app,
        "VECTOPLAN_CHUNK_DEFAULT_PROVIDER_WORLD_ID",
        DEFAULT_PROVIDER_WORLD_ID,
    )


def _default_block_registry_id(app: Any) -> str:
    return _config_str(
        app,
        "VECTOPLAN_CHUNK_DEFAULT_BLOCK_REGISTRY_ID",
        DEFAULT_BLOCK_REGISTRY_ID,
    )


def _default_block_registry_version(app: Any) -> str:
    return _config_str(
        app,
        "VECTOPLAN_CHUNK_DEFAULT_BLOCK_REGISTRY_VERSION",
        DEFAULT_BLOCK_REGISTRY_VERSION,
    )


# -----------------------------------------------------------------------------
# Model helpers
# -----------------------------------------------------------------------------

def _import_model_class_map() -> dict[str, Any]:
    """Import the complete model class map robustly."""
    try:
        from models import get_model_class_map

        result = get_model_class_map()
        if isinstance(result, Mapping):
            return dict(result)
    except Exception:
        pass

    model_map: dict[str, Any] = {}
    for class_name in (
        "Project",
        "ProjectAccessAssignment",
        "ProjectRole",
        "ProjectGroup",
        "ProjectGroupMember",
        "ProjectRoleAssignment",
        "Universe",
        "WorldInstance",
        "BlockRegistry",
        "BlockType",
    ):
        try:
            module = __import__("models", fromlist=[class_name])
            model_map[class_name] = getattr(module, class_name)
        except Exception:
            model_map[class_name] = None
    return model_map


def _model_class(name: str) -> Any | None:
    """Return model class by canonical name."""
    model_map = _import_model_class_map()
    return model_map.get(name)


def _model_table(model_class: Any) -> Any | None:
    """Return model table."""
    try:
        return getattr(model_class, "__table__", None)
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
    """Set attribute if it exists or is a mapped column."""
    if instance is None:
        return False

    model_class = instance.__class__

    if not hasattr(instance, name) and not _model_has_column(model_class, name):
        return False

    try:
        setattr(instance, name, value)
        return True
    except Exception:
        return False


def _set_attr_if_empty(instance: Any, name: str, value: Any) -> bool:
    """Set attribute only when currently empty."""
    if instance is None:
        return False

    try:
        current = getattr(instance, name, None)
    except Exception:
        current = None

    if current not in (None, "", {}, []):
        return False

    return _set_attr_if_supported(instance, name, value)


def _set_attr_force(instance: Any, name: str, value: Any) -> bool:
    """Set attribute forcefully when supported."""
    return _set_attr_if_supported(instance, name, value)


def _merge_metadata_json(instance: Any, payload: Mapping[str, Any]) -> None:
    """Merge payload into metadata_json if supported."""
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


def _make_instance(model_class: Any, values: Mapping[str, Any]) -> Any:
    """Instantiate model with supported column values."""
    supported_values = {
        key: value
        for key, value in values.items()
        if _model_has_column(model_class, key)
    }

    try:
        return model_class(**supported_values)
    except Exception:
        instance = model_class()
        for key, value in supported_values.items():
            _set_attr_if_supported(instance, key, value)
        return instance


def _query_first_by_fields(session: Any, model_class: Any, **fields: Any) -> Any | None:
    """Query first row matching supported fields."""
    if session is None or model_class is None:
        return None

    filters = {
        key: value
        for key, value in fields.items()
        if value is not None and _model_has_column(model_class, key)
    }

    if not filters:
        return None

    try:
        query = session.query(model_class)
        for key, value in filters.items():
            query = query.filter(getattr(model_class, key) == value)
        return query.first()
    except Exception:
        try:
            return session.query(model_class).filter_by(**filters).first()
        except Exception:
            return None


def _row_db_id(row: Any) -> Any | None:
    """Return row db id."""
    try:
        return getattr(row, "id", None)
    except Exception:
        return None


def _row_public_id(row: Any, *names: str) -> str | None:
    """Return first id-like attribute."""
    for name in names:
        try:
            value = getattr(row, name, None)
        except Exception:
            value = None

        text = _safe_str(value, "")
        if text:
            return text

    return None



# -----------------------------------------------------------------------------
# Default project owner/access projection
# -----------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _load_project_access_projection_api() -> Mapping[str, Any]:
    """Load the canonical access projection service without touching the DB."""
    errors: list[str] = []
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
            return MappingProxyType(
                {
                    "module": module,
                    "moduleName": module_name,
                    "initialize": initialize,
                }
            )
        except Exception as exc:
            errors.append(
                f"{module_name}: {exc.__class__.__name__}: "
                f"{_safe_exception_message(exc)}"
            )
    raise RuntimeError(
        "Could not import the canonical project-access service. "
        + " | ".join(errors)
    )


def clear_db_bootstrap_project_access_caches() -> None:
    """Clear immutable access integration caches; never change database state."""
    try:
        api = _load_project_access_projection_api()
        module = api.get("module")
        clear_function = getattr(module, "clear_project_access_cache", None)
        if callable(clear_function):
            clear_function()
    except Exception:
        pass
    _load_project_access_projection_api.cache_clear()


def _initialize_canonical_project_access(
    app: Any,
    project: Any,
    owner_auth_user_id: str,
    db_obj: Any,
) -> dict[str, Any]:
    """Initialize ProjectAccessAssignment without committing the outer transaction."""
    models = _import_model_class_map()
    assignment_model = models.get("ProjectAccessAssignment")
    project_model = models.get("Project") or project.__class__
    project_id = _safe_str(getattr(project, "project_id", None), _default_project_id(app))
    owner = _normalize_auth_user_id(owner_auth_user_id)
    fingerprint = _project_access_projection_fingerprint(project_id, owner)
    request_id = f"bootstrap-{project_id}"
    correlation_id = request_id
    service_errors: list[str] = []

    if assignment_model is None:
        raise RuntimeError("ProjectAccessAssignment model is unavailable.")

    try:
        api = _load_project_access_projection_api()
        initialize = api["initialize"]
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
            "VECTOPLAN_CHUNK_ACCESS_SOURCE_SERVICE": (
                DEFAULT_PROJECT_ACCESS_SOURCE_SERVICE
            ),
            "VECTOPLAN_CHUNK_ACCESS_PROJECTION_VERSION": (
                DEFAULT_PROJECT_ACCESS_PROJECTION_VERSION
            ),
            "VECTOPLAN_CHUNK_ACCESS_PRUNE_STALE_DIRECT_ASSIGNMENTS": True,
            "VECTOPLAN_CHUNK_ACCESS_PRESERVE_GROUP_ASSIGNMENTS": True,
            "VECTOPLAN_CHUNK_ACCESS_VERIFY_AFTER_SYNC": True,
            "VECTOPLAN_CHUNK_RUNTIME_BUSINESS_MUTATIONS_ENABLED": True,
            "VECTOPLAN_CHUNK_SERVICE_AUTH_REQUIRED": True,
        }
        raw_result = initialize(
            project_id,
            payload,
            session=db_obj.session,
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
        data = _result_to_dict_with_variants(raw_result)
        if data and not _safe_bool(data.get("ok"), False):
            raise RuntimeError(
                _safe_str(
                    data.get("error"),
                    data.get("code") or "canonical access synchronization failed",
                )
            )
        db_obj.session.flush()
        return {
            "ok": True,
            "backend": api.get("moduleName"),
            "result": data,
            "projectionFingerprint": fingerprint,
            "ownerAuthUserId": owner,
        }
    except Exception as exc:
        service_errors.append(
            f"{exc.__class__.__name__}: {_safe_exception_message(exc)}"
        )

    # Direct ORM fallback. Group assignments and non-owner direct members remain.
    try:
        rows = (
            db_obj.session.query(assignment_model)
            .filter(getattr(assignment_model, "chunk_project_id") == project_id)
            .all()
        )
    except Exception:
        try:
            rows = db_obj.session.query(assignment_model).all()
        except Exception:
            rows = []
        rows = [
            row
            for row in rows
            if _safe_str(getattr(row, "chunk_project_id", ""), "") == project_id
        ]

    owner_row = None
    demoted_count = 0
    duplicate_owner_rows: list[Any] = []
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
            if owner_row is None:
                owner_row = row
            else:
                duplicate_owner_rows.append(row)
            continue

        if row_role == DEFAULT_PROJECT_ACCESS_ROLE and row_active:
            _set_attr_if_supported(row, "role", "admin")
            _set_attr_if_supported(row, "updated_at", _utc_now())
            db_obj.session.add(row)
            demoted_count += 1

    for duplicate in duplicate_owner_rows:
        _set_attr_if_supported(duplicate, "role", "admin")
        _set_attr_if_supported(duplicate, "updated_at", _utc_now())
        db_obj.session.add(duplicate)
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
                    "seededBy": "db_bootstrap.default_world_invariant_repair",
                    "bootstrapService": DEFAULT_PROJECT_ACCESS_SERVICE_ID,
                },
            )
        else:
            owner_row = _make_instance(
                assignment_model,
                {
                    "chunk_project_id": project_id,
                    "auth_user_id": owner,
                    "group_id": None,
                    "role": DEFAULT_PROJECT_ACCESS_ROLE,
                    "assignment_type": DEFAULT_PROJECT_ACCESS_ASSIGNMENT_TYPE,
                    "active": True,
                    "managed": True,
                    "source_service": DEFAULT_PROJECT_ACCESS_SOURCE_SERVICE,
                    "projection_version": DEFAULT_PROJECT_ACCESS_PROJECTION_VERSION,
                    "projection_fingerprint": fingerprint,
                    "request_id": request_id,
                    "correlation_id": correlation_id,
                    "metadata_json": {
                        "seededBy": "db_bootstrap.default_world_invariant_repair",
                    },
                },
            )
        db_obj.session.add(owner_row)
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
        db_obj.session.add(owner_row)

    db_obj.session.flush()
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
    db_obj.session.add(project)
    db_obj.session.flush()

    return {
        "ok": True,
        "backend": "direct_project_access_assignment",
        "created": created,
        "demotedExtraOwners": demoted_count,
        "projectionFingerprint": fingerprint,
        "ownerAuthUserId": owner,
        "serviceErrors": service_errors,
    }



def _legacy_role_permissions(role_key: str) -> dict[str, bool]:
    """Return deterministic compatibility permissions for one standard role."""
    role_key = _safe_str(role_key, "").lower()
    permission_keys = (
        "view",
        "edit",
        "manage",
        "delete",
        "transfer",
        "embed",
        "view_settings",
        "manage_settings",
        "view_team",
        "manage_team",
        "view_admin",
    )
    if role_key == "owner":
        return {key: True for key in permission_keys}
    if role_key == "admin":
        enabled = {
            "view",
            "edit",
            "manage",
            "delete",
            "embed",
            "view_settings",
            "manage_settings",
            "view_team",
            "manage_team",
            "view_admin",
        }
        return {key: key in enabled for key in permission_keys}
    if role_key == "editor":
        enabled = {"view", "edit", "embed", "view_settings", "view_team"}
        return {key: key in enabled for key in permission_keys}
    return {key: key == "view" for key in permission_keys}


def _legacy_assignment_is_active(row: Any, now: datetime | None = None) -> bool:
    """Evaluate a legacy role assignment without loading relationships."""
    now = now or _utc_now()
    status_value = _safe_str(getattr(row, "status", "active"), "active").lower()
    if status_value not in {"active", "ready", "enabled"}:
        return False
    if getattr(row, "deleted_at", None) is not None:
        return False
    if getattr(row, "revoked_at", None) is not None:
        return False
    starts_at = getattr(row, "starts_at", None)
    expires_at = getattr(row, "expires_at", None)
    try:
        if isinstance(starts_at, datetime):
            if starts_at.tzinfo is None:
                starts_at = starts_at.replace(tzinfo=timezone.utc)
            if now < starts_at.astimezone(timezone.utc):
                return False
        if isinstance(expires_at, datetime):
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            if now >= expires_at.astimezone(timezone.utc):
                return False
    except Exception:
        return False
    return True


def _initialize_legacy_project_access_direct(
    project: Any,
    owner_auth_user_id: str,
    db_obj: Any,
) -> dict[str, Any]:
    """
    Ensure standard legacy roles and one owner assignment using mapped columns.

    This path is used only when compatibility adapters are missing or return an
    incomplete result. It participates in the caller's transaction and never
    commits independently.
    """
    models = _import_model_class_map()
    ProjectRole = models.get("ProjectRole")
    ProjectRoleAssignment = models.get("ProjectRoleAssignment")
    if ProjectRole is None or ProjectRoleAssignment is None:
        raise RuntimeError(
            "Legacy ProjectRole or ProjectRoleAssignment model is unavailable."
        )

    project_db_id = _row_db_id(project)
    project_id = _safe_str(
        getattr(project, "project_id", None),
        DEFAULT_PROJECT_ID,
    )
    owner = _normalize_auth_user_id(owner_auth_user_id)
    if project_db_id is None:
        raise RuntimeError(
            "Cannot initialize legacy project access without project database id."
        )

    now = _utc_now()
    roles: dict[str, Any] = {}
    roles_created = 0
    roles_updated = 0

    try:
        existing_roles = (
            db_obj.session.query(ProjectRole)
            .filter(getattr(ProjectRole, "project_db_id") == project_db_id)
            .limit(64)
            .all()
        )
    except Exception:
        existing_roles = []

    for role_key in DEFAULT_PROJECT_ROLE_KEYS:
        candidates = [
            row
            for row in existing_roles
            if _safe_str(getattr(row, "role_key", None), "").lower()
            == role_key
        ]
        role = next(
            (
                row
                for row in candidates
                if _safe_str(
                    getattr(row, "status", "active"),
                    "active",
                ).lower()
                in {"active", "ready", "enabled"}
                and getattr(row, "deleted_at", None) is None
            ),
            candidates[0] if candidates else None,
        )

        role_id = f"{project_id}:{role_key}"
        created = role is None
        if role is None:
            role = _make_instance(
                ProjectRole,
                {
                    "role_id": role_id,
                    "project_db_id": project_db_id,
                    "role_key": role_key,
                    "name": role_key.title(),
                    "description": (
                        f"Bootstrap compatibility role '{role_key}' for "
                        f"project '{project_id}'."
                    ),
                    "permissions_json": _legacy_role_permissions(role_key),
                    "is_system": True,
                    "status": "active",
                    "schema_version": "project-role.schema.v1",
                    "revision": 1,
                    "created_by_user_id": owner,
                    "updated_by_user_id": owner,
                    "metadata_json": {
                        "seededBy": (
                            "db_bootstrap."
                            "default_world_invariant_repair"
                        ),
                        "sourceService": (
                            DEFAULT_PROJECT_ACCESS_SOURCE_SERVICE
                        ),
                    },
                    "created_at": now,
                    "updated_at": now,
                    "deleted_at": None,
                },
            )
            db_obj.session.add(role)
            roles_created += 1
        else:
            changed = False
            for field_name, value in (
                ("role_id", _safe_str(getattr(role, "role_id", None), role_id)),
                ("project_db_id", project_db_id),
                ("role_key", role_key),
                ("name", role_key.title()),
                ("permissions_json", _legacy_role_permissions(role_key)),
                ("is_system", True),
                ("status", "active"),
                ("updated_by_user_id", owner),
                ("updated_at", now),
                ("deleted_at", None),
            ):
                changed = _set_attr_if_supported(role, field_name, value) or changed
            if changed:
                roles_updated += 1
            db_obj.session.add(role)

        roles[role_key] = role

        # Fresh installs have no duplicates. On repaired databases, deactivate
        # duplicate active rows so readiness remains deterministic.
        for duplicate in candidates:
            if duplicate is role:
                continue
            duplicate_changed = False
            duplicate_changed = _set_attr_if_supported(
                duplicate,
                "status",
                "inactive",
            ) or duplicate_changed
            duplicate_changed = _set_attr_if_supported(
                duplicate,
                "deleted_at",
                now,
            ) or duplicate_changed
            duplicate_changed = _set_attr_if_supported(
                duplicate,
                "updated_by_user_id",
                owner,
            ) or duplicate_changed
            duplicate_changed = _set_attr_if_supported(
                duplicate,
                "updated_at",
                now,
            ) or duplicate_changed
            if duplicate_changed:
                roles_updated += 1
            db_obj.session.add(duplicate)

    db_obj.session.flush()

    owner_role = roles["owner"]
    admin_role = roles["admin"]
    owner_role_db_id = _row_db_id(owner_role)
    admin_role_db_id = _row_db_id(admin_role)
    owner_role_id = _safe_str(getattr(owner_role, "role_id", None), "")
    admin_role_id = _safe_str(getattr(admin_role, "role_id", None), "")

    try:
        assignments = (
            db_obj.session.query(ProjectRoleAssignment)
            .filter(
                getattr(ProjectRoleAssignment, "project_db_id")
                == project_db_id
            )
            .limit(128)
            .all()
        )
    except Exception:
        assignments = []

    owner_role_assignments = [
        row
        for row in assignments
        if (
            (
                owner_role_db_id is not None
                and getattr(row, "role_db_id", None) == owner_role_db_id
            )
            or (
                owner_role_id
                and _safe_str(getattr(row, "role_id", None), "")
                == owner_role_id
            )
        )
    ]
    matching_candidates = [
        row
        for row in owner_role_assignments
        if _safe_str(getattr(row, "subject_type", "user"), "user").lower()
        == "user"
        and _normalize_auth_user_id(
            getattr(row, "user_id", None),
            "",
        )
        == owner
    ]

    primary_owner_assignment = next(
        (
            row
            for row in matching_candidates
            if _legacy_assignment_is_active(row, now)
        ),
        matching_candidates[0] if matching_candidates else None,
    )
    assignments_created = 0
    assignments_demoted = 0

    if primary_owner_assignment is None:
        assignment_fingerprint = hashlib.sha256(
            f"{project_id}:{owner}:owner".encode("utf-8")
        ).hexdigest()[:24]
        primary_owner_assignment = _make_instance(
            ProjectRoleAssignment,
            {
                "assignment_id": (
                    f"bootstrap-owner-{assignment_fingerprint}"
                ),
                "project_db_id": project_db_id,
                "role_db_id": owner_role_db_id,
                "role_id": owner_role_id or None,
                "subject_type": "user",
                "user_id": owner,
                "group_db_id": None,
                "group_id": None,
                "subject_key": f"user:{owner}",
                "permission_overrides_json": {},
                "status": "active",
                "assigned_by_user_id": owner,
                "revoked_by_user_id": None,
                "starts_at": None,
                "expires_at": None,
                "revoked_at": None,
                "revocation_reason": None,
                "schema_version": "project-role-assignment.schema.v1",
                "revision": 1,
                "created_by_user_id": owner,
                "updated_by_user_id": owner,
                "metadata_json": {
                    "seededBy": (
                        "db_bootstrap.default_world_invariant_repair"
                    ),
                    "sourceService": (
                        DEFAULT_PROJECT_ACCESS_SOURCE_SERVICE
                    ),
                },
                "created_at": now,
                "updated_at": now,
                "deleted_at": None,
            },
        )
        db_obj.session.add(primary_owner_assignment)
        assignments_created += 1
    else:
        for field_name, value in (
            ("project_db_id", project_db_id),
            ("role_db_id", owner_role_db_id),
            ("role_id", owner_role_id or None),
            ("subject_type", "user"),
            ("user_id", owner),
            ("status", "active"),
            ("revoked_by_user_id", None),
            ("revoked_at", None),
            ("revocation_reason", None),
            ("starts_at", None),
            ("expires_at", None),
            ("deleted_at", None),
            ("updated_by_user_id", owner),
            ("updated_at", now),
        ):
            _set_attr_if_supported(
                primary_owner_assignment,
                field_name,
                value,
            )
        db_obj.session.add(primary_owner_assignment)

    # Any other effective owner assignment is demoted to admin. Group
    # assignments are preserved; only their role reference changes.
    for assignment in owner_role_assignments:
        if assignment is primary_owner_assignment:
            continue
        if not _legacy_assignment_is_active(assignment, now):
            continue
        _set_attr_if_supported(
            assignment,
            "role_db_id",
            admin_role_db_id,
        )
        _set_attr_if_supported(
            assignment,
            "role_id",
            admin_role_id or None,
        )
        _set_attr_if_supported(
            assignment,
            "updated_by_user_id",
            owner,
        )
        _set_attr_if_supported(assignment, "updated_at", now)
        db_obj.session.add(assignment)
        assignments_demoted += 1

    db_obj.session.flush()
    status = _legacy_project_access_status(
        project,
        owner,
        db_obj,
    )
    if not _safe_bool(status.get("ready"), False):
        raise RuntimeError(
            "Direct legacy project-access initialization remained incomplete: "
            f"rolesReady={_safe_bool(status.get('rolesReady'), False)} "
            f"ownerAssignmentReady="
            f"{_safe_bool(status.get('ownerAssignmentReady'), False)} "
            f"missingRoles={status.get('missingRoleKeys')}"
        )

    return {
        "ok": True,
        "backend": "direct_legacy_project_access_models",
        "rolesCreated": roles_created,
        "rolesUpdated": roles_updated,
        "assignmentsCreated": assignments_created,
        "assignmentsDemoted": assignments_demoted,
        "status": status,
    }

def _initialize_legacy_project_access(
    app: Any,
    project: Any,
    owner_auth_user_id: str,
    db_obj: Any,
) -> dict[str, Any]:
    """Maintain legacy roles/groups during canonical projection adoption."""
    models = _import_model_class_map()
    legacy_names = (
        "ProjectRole",
        "ProjectGroup",
        "ProjectGroupMember",
        "ProjectRoleAssignment",
    )
    models_present = all(models.get(name) is not None for name in legacy_names)
    if not models_present:
        return {
            "ok": True,
            "skipped": True,
            "reason": "legacy access models are not installed",
        }

    errors: list[str] = []
    for module_name in ("src.project_access", "project_access"):
        try:
            module = importlib.import_module(module_name)
            initialize = getattr(module, "ensure_project_access_initialized", None)
            if not callable(initialize):
                raise RuntimeError(
                    "ensure_project_access_initialized export is unavailable"
                )
            raw_result = initialize(
                project=project,
                owner_user_id=owner_auth_user_id,
                actor_user_id=owner_auth_user_id,
                session=db_obj.session,
                synchronize_default_roles=True,
                restore_deleted_roles=True,
                replace_existing_owner=True,
                allow_missing_owner=False,
                lock_project=True,
                flush=True,
            )
            data = _result_to_dict_with_variants(raw_result)
            if not _safe_bool(
                data.get("accessInitialized", data.get("ok", True)),
                True,
            ):
                raise RuntimeError(
                    "Legacy project access initialization returned not ready."
                )
            db_obj.session.flush()
            status = _legacy_project_access_status(
                project,
                owner_auth_user_id,
                db_obj,
            )
            if _safe_bool(status.get("ready"), False):
                return {
                    "ok": True,
                    "backend": module_name,
                    "result": data,
                    "status": status,
                }
            errors.append(
                f"{module_name}: adapter returned an incomplete access state "
                f"(rolesReady={_safe_bool(status.get('rolesReady'), False)}, "
                f"ownerAssignmentReady="
                f"{_safe_bool(status.get('ownerAssignmentReady'), False)})"
            )
        except Exception as exc:
            errors.append(
                f"{module_name}: {exc.__class__.__name__}: "
                f"{_safe_exception_message(exc)}"
            )

    # Compatibility fallback for installations that expose the adapter only
    # through default_seed.py. It still writes inside this outer transaction.
    seed_access = _get_default_seed_callable("seed_default_project_access")
    seed_access_status = _get_default_seed_callable(
        "build_default_project_access_status"
    )
    if callable(seed_access):
        try:
            operations = seed_access(
                app,
                project,
                owner_auth_user_id,
                db_extension=db_obj,
            )
            db_obj.session.flush()
            helper_status = (
                _safe_dict(
                    seed_access_status(
                        project,
                        owner_auth_user_id,
                        db_extension=db_obj,
                    )
                )
                if callable(seed_access_status)
                else {}
            )
            status = _legacy_project_access_status(
                project,
                owner_auth_user_id,
                db_obj,
            )
            if _safe_bool(status.get("ready"), False):
                return {
                    "ok": True,
                    "backend": (
                        "src.bootstrap.default_seed."
                        "seed_default_project_access"
                    ),
                    "operations": operations,
                    "status": status,
                    "helperStatus": helper_status,
                    "importErrors": errors,
                    "defaultSeedApi": _default_seed_api_diagnostics(),
                }
            errors.append(
                "default_seed.seed_default_project_access: incomplete state "
                f"(rolesReady={_safe_bool(status.get('rolesReady'), False)}, "
                f"ownerAssignmentReady="
                f"{_safe_bool(status.get('ownerAssignmentReady'), False)})"
            )
        except Exception as exc:
            errors.append(
                "default_seed.seed_default_project_access: "
                f"{exc.__class__.__name__}: {_safe_exception_message(exc)}"
            )

    try:
        direct = _initialize_legacy_project_access_direct(
            project,
            owner_auth_user_id,
            db_obj,
        )
        direct["adapterErrors"] = errors
        direct["defaultSeedApi"] = _default_seed_api_diagnostics()
        return direct
    except Exception as exc:
        errors.append(
            "direct_legacy_project_access_models: "
            f"{exc.__class__.__name__}: {_safe_exception_message(exc)}"
        )
        raise RuntimeError(
            "Legacy project-access compatibility initialization failed: "
            + " | ".join(errors)
        ) from exc


def _canonical_project_access_status(
    project: Any,
    owner_auth_user_id: str,
    db_obj: Any,
) -> dict[str, Any]:
    """Build read-only status for ProjectAccessAssignment."""
    models = _import_model_class_map()
    assignment_model = models.get("ProjectAccessAssignment")
    project_id = _safe_str(getattr(project, "project_id", None), "")
    owner = _normalize_auth_user_id(owner_auth_user_id, "")
    if assignment_model is None:
        return {
            "ready": False,
            "available": False,
            "assignmentCount": 0,
            "ownerAssignmentCount": 0,
            "ownerTotal": 0,
            "error": "ProjectAccessAssignment model is unavailable.",
        }

    try:
        rows = (
            db_obj.session.query(assignment_model)
            .filter(getattr(assignment_model, "chunk_project_id") == project_id)
            .all()
        )
    except Exception:
        try:
            rows = db_obj.session.query(assignment_model).all()
        except Exception:
            rows = []
        rows = [
            row
            for row in rows
            if _safe_str(getattr(row, "chunk_project_id", ""), "") == project_id
        ]

    assignment_count = 0
    direct_owner_total = 0
    matching_owner_count = 0
    group_assignment_count = 0
    roles: set[str] = set()
    for row in rows:
        if not _safe_bool(getattr(row, "active", True), True):
            continue
        assignment_count += 1
        assignment_type = _safe_str(
            getattr(row, "assignment_type", DEFAULT_PROJECT_ACCESS_ASSIGNMENT_TYPE),
            DEFAULT_PROJECT_ACCESS_ASSIGNMENT_TYPE,
        ).lower()
        role = _safe_str(getattr(row, "role", ""), "").lower()
        if role:
            roles.add(role)
        if assignment_type == "group":
            group_assignment_count += 1
            continue
        if role != DEFAULT_PROJECT_ACCESS_ROLE:
            continue
        direct_owner_total += 1
        if _normalize_auth_user_id(getattr(row, "auth_user_id", None), "") == owner:
            matching_owner_count += 1

    ready = bool(
        owner
        and _project_owner_fields_ready(project, owner)
        and direct_owner_total == 1
        and matching_owner_count == 1
    )
    return {
        "ready": ready,
        "available": True,
        "assignmentCount": assignment_count,
        "groupAssignmentCount": group_assignment_count,
        "ownerAssignmentCount": matching_owner_count,
        "ownerTotal": direct_owner_total,
        "roles": sorted(roles),
        "ownerAuthUserId": owner,
    }


def _legacy_project_access_status(
    project: Any,
    owner_auth_user_id: str,
    db_obj: Any,
) -> dict[str, Any]:
    """
    Build read-only compatibility status directly from the current session.

    The default-seed status helper is optional. During import-order failures the
    bootstrap must still be able to validate rows it just flushed in the same
    outer transaction; otherwise a valid repair is rolled back as "not ready".
    """
    models = _import_model_class_map()
    legacy_names = (
        "ProjectRole",
        "ProjectGroup",
        "ProjectGroupMember",
        "ProjectRoleAssignment",
    )
    models_present = all(models.get(name) is not None for name in legacy_names)
    if not models_present:
        return {
            "ready": True,
            "available": False,
            "skipped": True,
            "roleCount": 0,
            "assignmentCount": 0,
            "requiredRoleKeys": list(DEFAULT_PROJECT_ROLE_KEYS),
            "activeRoleKeys": [],
            "missingRoleKeys": [],
            "duplicateActiveRoleKeys": [],
            "ownerAssignmentCount": 0,
            "matchingOwnerAssignmentCount": 0,
        }

    ProjectRole = models["ProjectRole"]
    ProjectRoleAssignment = models["ProjectRoleAssignment"]
    project_db_id = _row_db_id(project)
    owner = _normalize_auth_user_id(owner_auth_user_id, "")
    if project_db_id is None:
        return {
            "ready": False,
            "available": True,
            "roleCount": 0,
            "assignmentCount": 0,
            "requiredRoleKeys": list(DEFAULT_PROJECT_ROLE_KEYS),
            "activeRoleKeys": [],
            "missingRoleKeys": list(DEFAULT_PROJECT_ROLE_KEYS),
            "duplicateActiveRoleKeys": [],
            "ownerAssignmentCount": 0,
            "matchingOwnerAssignmentCount": 0,
            "error": "Project database id is unavailable.",
        }

    helper_status: dict[str, Any] = {}
    helper_error: str | None = None
    status_factory = _get_default_seed_callable(
        "build_default_project_access_status"
    )
    if callable(status_factory):
        try:
            helper_status = _safe_dict(
                status_factory(
                    project,
                    owner,
                    db_extension=db_obj,
                )
            )
        except Exception as exc:
            helper_error = (
                f"{exc.__class__.__name__}: {_safe_exception_message(exc)}"
            )

    try:
        role_rows = (
            db_obj.session.query(ProjectRole)
            .filter(getattr(ProjectRole, "project_db_id") == project_db_id)
            .limit(max(16, len(DEFAULT_PROJECT_ROLE_KEYS) * 4))
            .all()
        )
    except Exception as exc:
        return {
            "ready": False,
            "available": True,
            "roleCount": 0,
            "assignmentCount": 0,
            "requiredRoleKeys": list(DEFAULT_PROJECT_ROLE_KEYS),
            "activeRoleKeys": [],
            "missingRoleKeys": list(DEFAULT_PROJECT_ROLE_KEYS),
            "duplicateActiveRoleKeys": [],
            "ownerAssignmentCount": 0,
            "matchingOwnerAssignmentCount": 0,
            "helperStatus": helper_status,
            "helperError": helper_error,
            "error": (
                "Could not query legacy project roles: "
                f"{_safe_exception_message(exc)}"
            ),
        }

    active_roles: dict[str, Any] = {}
    duplicate_role_keys: set[str] = set()
    for role_row in role_rows:
        role_key = _safe_str(getattr(role_row, "role_key", None), "").lower()
        if role_key not in DEFAULT_PROJECT_ROLE_KEYS:
            continue
        status_value = _safe_str(
            getattr(role_row, "status", "active"),
            "active",
        ).lower()
        if status_value not in {"active", "ready", "enabled"}:
            continue
        if getattr(role_row, "deleted_at", None) is not None:
            continue
        if role_key in active_roles:
            duplicate_role_keys.add(role_key)
        else:
            active_roles[role_key] = role_row

    active_role_keys = set(active_roles)
    missing_role_keys = sorted(
        set(DEFAULT_PROJECT_ROLE_KEYS) - active_role_keys
    )
    roles_ready = bool(not missing_role_keys and not duplicate_role_keys)
    owner_role = active_roles.get(DEFAULT_PROJECT_ACCESS_ROLE)
    owner_role_db_id = _row_db_id(owner_role)
    owner_role_public_id = _safe_str(
        getattr(owner_role, "role_id", None),
        "",
    )

    assignment_rows: list[Any] = []
    assignment_query_error: str | None = None
    try:
        assignment_query = db_obj.session.query(ProjectRoleAssignment).filter(
            getattr(ProjectRoleAssignment, "project_db_id") == project_db_id
        )
        if (
            owner_role_db_id is not None
            and _model_has_column(ProjectRoleAssignment, "role_db_id")
        ):
            assignment_query = assignment_query.filter(
                getattr(ProjectRoleAssignment, "role_db_id")
                == owner_role_db_id
            )
        elif (
            owner_role_public_id
            and _model_has_column(ProjectRoleAssignment, "role_id")
        ):
            assignment_query = assignment_query.filter(
                getattr(ProjectRoleAssignment, "role_id")
                == owner_role_public_id
            )
        assignment_rows = assignment_query.limit(64).all()
    except Exception as exc:
        assignment_query_error = _safe_exception_message(exc)
        assignment_rows = []

    now = _utc_now()
    effective_owner_assignments: list[Any] = []
    matching_owner_assignments: list[Any] = []
    for assignment in assignment_rows:
        status_value = _safe_str(
            getattr(assignment, "status", "active"),
            "active",
        ).lower()
        if status_value not in {"active", "ready", "enabled"}:
            continue
        if getattr(assignment, "deleted_at", None) is not None:
            continue
        if getattr(assignment, "revoked_at", None) is not None:
            continue

        starts_at = getattr(assignment, "starts_at", None)
        expires_at = getattr(assignment, "expires_at", None)
        try:
            if isinstance(starts_at, datetime):
                if starts_at.tzinfo is None:
                    starts_at = starts_at.replace(tzinfo=timezone.utc)
                if now < starts_at.astimezone(timezone.utc):
                    continue
            if isinstance(expires_at, datetime):
                if expires_at.tzinfo is None:
                    expires_at = expires_at.replace(tzinfo=timezone.utc)
                if now >= expires_at.astimezone(timezone.utc):
                    continue
        except Exception:
            continue

        subject_type = _safe_str(
            getattr(assignment, "subject_type", "user"),
            "user",
        ).lower()
        if subject_type != "user":
            continue
        effective_owner_assignments.append(assignment)
        assignment_owner = _normalize_auth_user_id(
            getattr(assignment, "user_id", None),
            "",
        )
        if assignment_owner == owner:
            matching_owner_assignments.append(assignment)

    owner_assignment_ready = bool(
        owner
        and owner_role is not None
        and assignment_query_error is None
        and len(effective_owner_assignments) == 1
        and len(matching_owner_assignments) == 1
    )
    direct_ready = bool(roles_ready and owner_assignment_ready)

    return {
        "ready": direct_ready,
        "available": True,
        "roleCount": len(active_roles),
        "assignmentCount": len(effective_owner_assignments),
        "requiredRoleKeys": list(DEFAULT_PROJECT_ROLE_KEYS),
        "activeRoleKeys": sorted(active_role_keys),
        "missingRoleKeys": missing_role_keys,
        "duplicateActiveRoleKeys": sorted(duplicate_role_keys),
        "rolesReady": roles_ready,
        "ownerRoleDbId": owner_role_db_id,
        "ownerRoleId": owner_role_public_id or None,
        "ownerAssignmentReady": owner_assignment_ready,
        "ownerAssignmentCount": len(effective_owner_assignments),
        "matchingOwnerAssignmentCount": len(matching_owner_assignments),
        "ownerAuthUserId": owner,
        "assignmentQueryError": assignment_query_error,
        "helperStatus": helper_status,
        "helperReady": (
            _safe_bool(helper_status.get("ready"), False)
            if helper_status
            else None
        ),
        "helperError": helper_error,
        "statusSource": "current_session_direct_query",
    }


def build_default_project_access_projection_status(
    app: Flask,
    *,
    project: Any = None,
    owner_auth_user_id: str | None = None,
    db_extension: Any = None,
) -> dict[str, Any]:
    """Build combined canonical and legacy default-project access readiness."""
    with _app_context(app):
        db_obj = _get_db_extension(db_extension)
        project_id = _default_project_id(app)
        Project = _model_class("Project")

        if db_obj is None:
            return {
                "ready": False,
                "projectExists": project is not None,
                "projectId": project_id,
                "ownerReady": False,
                "canonicalReady": False,
                "legacyReady": False,
                "error": "SQLAlchemy db extension is unavailable.",
            }

        if project is None and Project is not None:
            project = _query_first_by_fields(
                db_obj.session,
                Project,
                project_id=project_id,
            )
        if project is None:
            return {
                "ready": False,
                "projectExists": False,
                "projectId": project_id,
                "ownerReady": False,
                "canonicalReady": False,
                "legacyReady": False,
                "error": "Default project does not exist.",
            }

        configured_owner = _normalize_auth_user_id(
            owner_auth_user_id or _configured_project_owner_auth_user_id(app)
        )
        owner = _project_owner_from_model(project, configured_owner)
        owner_ready = _project_owner_fields_ready(project, owner)
        canonical = _canonical_project_access_status(project, owner, db_obj)
        legacy = _legacy_project_access_status(project, owner, db_obj)
        canonical_ready = _safe_bool(canonical.get("ready"), False)
        legacy_ready = _safe_bool(legacy.get("ready"), False)
        ready = bool(owner_ready and canonical_ready and legacy_ready)

        return {
            "ready": ready,
            "required": _project_access_required(app),
            "projectExists": True,
            "projectId": _safe_str(getattr(project, "project_id", None), project_id),
            "projectDbId": _row_db_id(project),
            "ownerAuthUserId": owner,
            "ownerReady": owner_ready,
            "canonicalReady": canonical_ready,
            "legacyReady": legacy_ready,
            "canonical": canonical,
            "legacy": legacy,
            "counts": {
                "canonicalAssignments": _safe_int(
                    canonical.get("assignmentCount"), 0
                ),
                "canonicalOwnerAssignments": _safe_int(
                    canonical.get("ownerAssignmentCount"), 0
                ),
                "legacyRoles": _safe_int(legacy.get("roleCount"), 0),
                "legacyAssignments": _safe_int(
                    legacy.get("assignmentCount"), 0
                ),
            },
        }


def _ensure_default_project_access(
    app: Any,
    project: Any,
    owner_auth_user_id: str,
    db_obj: Any,
) -> dict[str, Any]:
    """Initialize canonical and legacy access in the surrounding transaction."""
    canonical = _initialize_canonical_project_access(
        app,
        project,
        owner_auth_user_id,
        db_obj,
    )
    legacy = _initialize_legacy_project_access(
        app,
        project,
        owner_auth_user_id,
        db_obj,
    )
    status = build_default_project_access_projection_status(
        app,
        project=project,
        owner_auth_user_id=owner_auth_user_id,
        db_extension=db_obj,
    )
    if not _safe_bool(status.get("ready"), False):
        canonical_status = _safe_dict(status.get("canonical"))
        legacy_status = _safe_dict(status.get("legacy"))
        raise RuntimeError(
            "Project access is not ready after canonical and legacy "
            "initialization. "
            f"ownerReady={_safe_bool(status.get('ownerReady'), False)} "
            f"canonicalReady={_safe_bool(status.get('canonicalReady'), False)} "
            f"legacyReady={_safe_bool(status.get('legacyReady'), False)} "
            f"canonicalOwnerTotal={_safe_int(canonical_status.get('ownerTotal'), 0)} "
            f"canonicalMatchingOwner={_safe_int(canonical_status.get('ownerAssignmentCount'), 0)} "
            f"legacyRolesReady={_safe_bool(legacy_status.get('rolesReady'), False)} "
            f"legacyOwnerAssignmentReady="
            f"{_safe_bool(legacy_status.get('ownerAssignmentReady'), False)} "
            f"legacyError={_safe_str(legacy_status.get('error'), '') or None}"
        )
    return {
        "ok": True,
        "canonical": canonical,
        "legacy": legacy,
        "status": status,
    }


# -----------------------------------------------------------------------------
# Built-in system-block bootstrap adapter
# -----------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _load_system_block_bootstrap_api() -> Mapping[str, Any]:
    """
    Load the system-block bootstrap API lazily and cache immutable exports.

    Database rows and SQLAlchemy instances are deliberately never cached.
    """
    candidates = (
        "src.system_blocks.bootstrap",
        "system_blocks.bootstrap",
    )

    required_exports = (
        "build_system_block_bootstrap_status_for_registry",
        "ensure_system_blocks_for_registry",
        "get_default_system_block_bootstrap_policy",
    )

    import_errors: list[str] = []

    for import_path in candidates:
        try:
            module = __import__(
                import_path,
                fromlist=required_exports,
            )
        except Exception as exc:
            import_errors.append(
                f"{import_path}: {exc.__class__.__name__}: "
                f"{_safe_exception_message(exc)}"
            )
            continue

        exports: dict[str, Any] = {
            "module": module,
            "moduleName": _safe_str(getattr(module, "__name__", None), ""),
            "modulePath": _safe_str(getattr(module, "__file__", None), ""),
        }

        missing: list[str] = []

        for export_name in required_exports:
            try:
                value = getattr(module, export_name)
            except Exception:
                value = None

            if not callable(value):
                missing.append(export_name)
                continue

            exports[export_name] = value

        if missing:
            import_errors.append(
                f"{import_path}: missing callable exports: "
                + ", ".join(missing)
            )
            continue

        return MappingProxyType(exports) if "MappingProxyType" in globals() else exports

    raise RuntimeError(
        "Could not import the system-block bootstrap API. "
        + " | ".join(import_errors)
    )


def clear_db_bootstrap_system_block_caches() -> None:
    """Clear only immutable system-block integration caches."""
    try:
        api = _load_system_block_bootstrap_api()
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

    _load_system_block_bootstrap_api.cache_clear()


def _empty_system_block_status(
    *,
    registry: Any = None,
    error: str | None = None,
    exception_type: str | None = None,
) -> dict[str, Any]:
    """Return a stable not-ready system-block status payload."""
    registry_id = _row_public_id(registry, "registry_id")
    registry_version = _row_public_id(registry, "registry_version")

    return {
        "ready": False,
        "repairable": False,
        "registryDbId": _row_db_id(registry),
        "registryId": registry_id,
        "registryVersion": registry_version,
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
            "missing": 0,
            "drifted": 0,
        },
        "errors": [error] if error else [],
        "errorType": exception_type,
        "error": error,
    }


def build_system_block_invariant_status(
    registry: Any,
) -> dict[str, Any]:
    """
    Build non-mutating Air and persistent-system-block readiness.

    The newer default-seed adapter is preferred so both bootstrap layers expose
    the same payload. A direct system-block package fallback keeps this module
    independently usable in partial import environments.
    """
    if registry is None:
        return _empty_system_block_status(
            error="Default BlockRegistry does not exist.",
            exception_type="RegistryMissing",
        )

    default_system_status = _get_default_seed_callable(
        "build_default_system_blocks_status"
    )
    if callable(default_system_status):
        try:
            payload = default_system_status(registry)
            normalized = _safe_dict(payload)
            if normalized:
                return normalized
        except Exception:
            pass

    try:
        api = _load_system_block_bootstrap_api()
        factory = api["build_system_block_bootstrap_status_for_registry"]
        payload = factory(registry)
        normalized = _safe_dict(payload)

        if normalized:
            return normalized

        return _empty_system_block_status(
            registry=registry,
            error="System-block status factory returned no mapping.",
            exception_type="InvalidStatusPayload",
        )

    except Exception as exc:
        return _empty_system_block_status(
            registry=registry,
            error=_safe_exception_message(exc),
            exception_type=exc.__class__.__name__,
        )


def _system_block_status_counts(
    status: Mapping[str, Any] | None,
) -> dict[str, int]:
    """Extract stable system-block counts."""
    status_dict = _safe_dict(status)
    mirrors = status_dict.get("mirrors") or []

    if not isinstance(mirrors, Sequence) or isinstance(
        mirrors,
        (str, bytes, bytearray),
    ):
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
        mirror = _safe_dict(raw_mirror)
        action = _safe_str(mirror.get("action"), "").lower()

        if _safe_bool(mirror.get("ready"), False):
            inferred["readyMirrors"] += 1
        if _safe_bool(mirror.get("created"), False):
            inferred["created"] += 1
        if _safe_bool(mirror.get("updated"), False):
            inferred["updated"] += 1
        if action in {"missing", "would_create"}:
            inferred["missing"] += 1
        if mirror.get("driftBefore") or action in {
            "drifted",
            "would_update",
            "updated",
        }:
            inferred["drifted"] += 1

    counts = _safe_dict(status_dict.get("counts"))

    return {
        key: max(0, _safe_int(counts.get(key), default))
        for key, default in inferred.items()
    }


def _system_railing_ready(
    status: Mapping[str, Any] | None,
) -> bool:
    """Return whether the canonical Railing mirror is fully ready."""
    mirrors = _safe_dict(status).get("mirrors") or []

    if not isinstance(mirrors, Sequence) or isinstance(
        mirrors,
        (str, bytes, bytearray),
    ):
        return False

    for raw_mirror in mirrors:
        mirror = _safe_dict(raw_mirror)
        system_id = _safe_str(mirror.get("systemBlockId"), "").lower()
        runtime_id = _safe_str(mirror.get("runtimeBlockTypeId"), "").lower()

        if DEFAULT_SYSTEM_RAILING_BLOCK_TYPE_ID in {system_id, runtime_id}:
            return _safe_bool(mirror.get("ready"), False)

    return False


def _air_invariant_ready(
    status: Mapping[str, Any] | None,
) -> bool:
    """Return whether Air remains the reserved non-persistent cell state."""
    air = _safe_dict(_safe_dict(status).get("air"))
    return _safe_bool(air.get("ready"), False)


def _system_blocks_ready(
    status: Mapping[str, Any] | None,
) -> bool:
    """Require aggregate, Air and Railing readiness together."""
    status_dict = _safe_dict(status)
    return bool(
        _safe_bool(status_dict.get("ready"), False)
        and _air_invariant_ready(status_dict)
        and _system_railing_ready(status_dict)
    )


def _reconcile_system_blocks_for_registry(
    registry: Any,
    db_obj: Any,
    *,
    operations: list[dict[str, Any]],
) -> dict[str, Any]:
    """
    Reconcile canonical persistent system blocks without committing.

    The surrounding default-world invariant transaction owns commit/rollback.
    Illegal Air rows are not silently deleted by the default policy; they make
    the bootstrap fail explicitly.
    """
    if registry is None:
        raise RuntimeError(
            "Cannot reconcile system blocks without a BlockRegistry."
        )

    api = _load_system_block_bootstrap_api()
    policy = api["get_default_system_block_bootstrap_policy"]()
    ensure_function = api["ensure_system_blocks_for_registry"]

    bootstrap_result = ensure_function(
        registry,
        policy=policy,
        created_by_user_id=DEFAULT_SYSTEM_BLOCK_BOOTSTRAP_USER_ID,
        updated_by_user_id=DEFAULT_SYSTEM_BLOCK_BOOTSTRAP_USER_ID,
    )

    data = _safe_dict(bootstrap_result)

    if not data:
        to_dict = getattr(bootstrap_result, "to_dict", None)
        if callable(to_dict):
            data = _safe_dict(to_dict())

    if not _system_blocks_ready(data):
        raise RuntimeError(
            "Built-in system-block reconciliation did not produce a ready "
            "Air/Railing state."
        )

    db_obj.session.flush()

    counts = _system_block_status_counts(data)
    changed = _safe_bool(data.get("changed"), False)

    operations.append(
        {
            "kind": "system_blocks",
            "status": "updated" if changed else "existing",
            "ready": True,
            "changed": changed,
            "created": counts["created"],
            "updated": counts["updated"],
            "missing": counts["missing"],
            "drifted": counts["drifted"],
            "airInvariantReady": _air_invariant_ready(data),
            "systemRailingReady": _system_railing_ready(data),
            "registryId": _row_public_id(registry, "registry_id"),
            "registryVersion": _row_public_id(registry, "registry_version"),
            "registryDbId": _row_db_id(registry),
        }
    )

    return data


# -----------------------------------------------------------------------------
# Default seed invariant status/repair
# -----------------------------------------------------------------------------

def build_default_world_invariant_status(
    app: Flask,
    *,
    db_extension: Any = None,
) -> dict[str, Any]:
    """Build read-only readiness for world, system blocks and project access."""
    started_at = _utc_now_iso()

    with _app_context(app):
        db_obj = _get_db_extension(db_extension)
        if db_obj is None:
            completed_at = _utc_now_iso()
            return {
                "ok": False,
                "status": STATUS_FAILED,
                "startedAt": started_at,
                "completedAt": completed_at,
                "durationMs": _duration_ms(started_at, completed_at),
                "error": "SQLAlchemy db extension is unavailable.",
                "database": {"engineAvailable": False},
            }

        Project = _model_class("Project")
        Universe = _model_class("Universe")
        WorldInstance = _model_class("WorldInstance")
        BlockRegistry = _model_class("BlockRegistry")
        BlockType = _model_class("BlockType")

        project_id = _default_project_id(app)
        universe_id = _default_universe_id(app)
        world_id = _default_world_id(app)
        template_id = _default_template_id(app)
        provider_id = _default_provider_id(app)
        provider_world_id = _default_provider_world_id(app)
        registry_id = _default_block_registry_id(app)
        registry_version = _default_block_registry_version(app)
        configured_owner = _configured_project_owner_auth_user_id(app)
        access_required = _project_access_required(app)
        debug_required = _debug_blocks_required(app)

        errors: list[dict[str, Any]] = []
        warnings: list[dict[str, Any]] = []
        project = None
        universe = None
        world = None
        block_registry = None
        block_type_count: int | None = None

        try:
            project = _query_first_by_fields(
                db_obj.session,
                Project,
                project_id=project_id,
            )
            project_db_id = _row_db_id(project)

            universe_fields: dict[str, Any] = {"universe_id": universe_id}
            if project_db_id is not None:
                universe_fields["project_db_id"] = project_db_id
            universe = _query_first_by_fields(
                db_obj.session,
                Universe,
                **universe_fields,
            )
            universe_db_id = _row_db_id(universe)

            world_fields: dict[str, Any] = {"world_id": world_id}
            if project_db_id is not None:
                world_fields["project_db_id"] = project_db_id
            if universe_db_id is not None:
                world_fields["universe_db_id"] = universe_db_id
            world = _query_first_by_fields(
                db_obj.session,
                WorldInstance,
                **world_fields,
            )

            if BlockRegistry is not None:
                block_registry = _query_first_by_fields(
                    db_obj.session,
                    BlockRegistry,
                    registry_id=registry_id,
                    registry_version=registry_version,
                )
                if block_registry is None:
                    block_registry = _query_first_by_fields(
                        db_obj.session,
                        BlockRegistry,
                        registry_id=registry_id,
                    )

            if BlockType is not None:
                try:
                    query = db_obj.session.query(BlockType)
                    registry_db_id = _row_db_id(block_registry)
                    if registry_db_id is not None and _model_has_column(
                        BlockType,
                        "registry_db_id",
                    ):
                        query = query.filter(
                            BlockType.registry_db_id == registry_db_id
                        )
                    else:
                        if _model_has_column(BlockType, "registry_id"):
                            query = query.filter(BlockType.registry_id == registry_id)
                        if _model_has_column(BlockType, "registry_version"):
                            query = query.filter(
                                BlockType.registry_version == registry_version
                            )
                    block_type_count = int(query.count())
                except Exception:
                    block_type_count = None
        except Exception as exc:
            errors.append(
                _make_message(
                    code="default_world_invariant_status_failed",
                    message=_safe_exception_message(exc),
                    details={"exceptionType": exc.__class__.__name__},
                )
            )

        system_blocks = build_system_block_invariant_status(block_registry)
        system_counts = _system_block_status_counts(system_blocks)
        project_ready = project is not None
        universe_ready = universe is not None
        world_ready = world is not None
        registry_ready = block_registry is not None if BlockRegistry is not None else False
        air_ready = _air_invariant_ready(system_blocks)
        railing_ready = _system_railing_ready(system_blocks)
        system_ready = _system_blocks_ready(system_blocks)
        debug_complete = (
            None if block_type_count is None else block_type_count > 0
        )

        owner = (
            _project_owner_from_model(project, configured_owner)
            if project is not None
            else configured_owner
        )
        owner_ready = _project_owner_fields_ready(project, owner)
        project_access = build_default_project_access_projection_status(
            app,
            project=project,
            owner_auth_user_id=owner,
            db_extension=db_obj,
        )
        project_access_ready = _safe_bool(project_access.get("ready"), False)

        ok = bool(
            project_ready
            and universe_ready
            and world_ready
            and registry_ready
            and system_ready
            and air_ready
            and railing_ready
            and owner_ready
            and (not access_required or project_access_ready)
            and (not debug_required or debug_complete is True)
            and not errors
        )
        completed_at = _utc_now_iso()

        return {
            "ok": ok,
            "status": STATUS_READY if ok else STATUS_PARTIAL,
            "startedAt": started_at,
            "completedAt": completed_at,
            "durationMs": _duration_ms(started_at, completed_at),
            "defaults": {
                "projectId": project_id,
                "projectOwnerAuthUserId": configured_owner,
                "ownerUserId": configured_owner,
                "universeId": universe_id,
                "worldId": world_id,
                "templateId": template_id,
                "providerId": provider_id,
                "providerWorldId": provider_world_id,
                "blockRegistryId": registry_id,
                "blockRegistryVersion": registry_version,
                "airSystemBlockId": DEFAULT_SYSTEM_AIR_BLOCK_ID,
                "systemRailingBlockTypeId": DEFAULT_SYSTEM_RAILING_BLOCK_TYPE_ID,
                "projectAccessRequired": access_required,
                "debugBlocksRequired": debug_required,
            },
            "project": {
                "exists": project_ready,
                "projectId": project_id,
                "dbId": _row_db_id(project),
                "actualProjectId": _row_public_id(project, "project_id"),
                "ownerAuthUserId": owner,
                "ownerReady": owner_ready,
                "ownerType": getattr(project, "owner_type", None) if project else None,
            },
            "projectAccess": project_access,
            "universe": {
                "exists": universe_ready,
                "universeId": universe_id,
                "dbId": _row_db_id(universe),
                "actualUniverseId": _row_public_id(universe, "universe_id"),
            },
            "world": {
                "exists": world_ready,
                "worldId": world_id,
                "dbId": _row_db_id(world),
                "actualWorldId": _row_public_id(world, "world_id"),
            },
            "blockRegistry": {
                "checked": BlockRegistry is not None,
                "exists": registry_ready,
                "registryId": registry_id,
                "registryVersion": registry_version,
                "dbId": _row_db_id(block_registry),
            },
            "debugBlocks": {
                "checked": BlockType is not None,
                "required": debug_required,
                "count": block_type_count,
                "complete": debug_complete,
            },
            "systemBlocks": {
                **system_blocks,
                "summary": {
                    "ready": system_ready,
                    "airInvariantReady": air_ready,
                    "systemRailingReady": railing_ready,
                    "mirrorCount": system_counts["mirrors"],
                    "readyMirrorCount": system_counts["readyMirrors"],
                    "missingCount": system_counts["missing"],
                    "driftedCount": system_counts["drifted"],
                },
            },
            "ready": {
                "project": project_ready,
                "projectOwner": owner_ready,
                "projectAccess": project_access_ready,
                "canonicalProjectAccess": _safe_bool(
                    project_access.get("canonicalReady"), False
                ),
                "legacyProjectAccess": _safe_bool(
                    project_access.get("legacyReady"), False
                ),
                "universe": universe_ready,
                "world": world_ready,
                "blockRegistry": registry_ready,
                "debugBlocks": (
                    debug_complete if debug_required else True
                ),
                "systemBlocks": system_ready,
                "airInvariant": air_ready,
                "systemRailing": railing_ready,
            },
            "warnings": warnings,
            "errors": errors,
        }


def _create_or_update_block_registry(
    app: Flask,
    db_obj: Any,
    *,
    operations: list[dict[str, Any]],
) -> Any | None:
    """Ensure the default active BlockRegistry exists."""
    BlockRegistry = _model_class("BlockRegistry")
    if BlockRegistry is None:
        operations.append(
            {
                "kind": "block_registry",
                "status": "skipped",
                "reason": "BlockRegistry model unavailable.",
            }
        )
        return None

    registry_id = _default_block_registry_id(app)
    registry_version = _default_block_registry_version(app)

    registry = _query_first_by_fields(
        db_obj.session,
        BlockRegistry,
        registry_id=registry_id,
        registry_version=registry_version,
    )
    if registry is None:
        registry = _query_first_by_fields(
            db_obj.session,
            BlockRegistry,
            registry_id=registry_id,
        )

    created = False
    changed = False

    if registry is None:
        factory = getattr(BlockRegistry, "create", None)
        if callable(factory):
            try:
                registry = factory(
                    registry_id=registry_id,
                    registry_version=registry_version,
                    label="Debug Blocks",
                    description=(
                        "Default runtime block registry seeded by "
                        "vectoplan-chunk bootstrap."
                    ),
                    status="active",
                    source=DEFAULT_BLOCK_REGISTRY_SOURCE,
                    is_default=True,
                    created_by_user_id=DEFAULT_SYSTEM_BLOCK_BOOTSTRAP_USER_ID,
                    metadata_json={
                        "seededBy": (
                            "db_bootstrap.default_world_invariant_repair"
                        ),
                        "createdAt": _utc_now_iso(),
                    },
                )
            except Exception:
                registry = None

        if registry is None:
            registry = _make_instance(
                BlockRegistry,
                {
                    "registry_id": registry_id,
                    "registry_version": registry_version,
                    "label": "Debug Blocks",
                    "description": (
                        "Default runtime block registry seeded by "
                        "vectoplan-chunk bootstrap."
                    ),
                    "status": "active",
                    "schema_version": "block-registry.schema.v1",
                    "revision": 1,
                    "source": DEFAULT_BLOCK_REGISTRY_SOURCE,
                    "is_default": True,
                    "created_by_user_id": DEFAULT_SYSTEM_BLOCK_BOOTSTRAP_USER_ID,
                    "updated_by_user_id": DEFAULT_SYSTEM_BLOCK_BOOTSTRAP_USER_ID,
                    "metadata_json": {
                        "seededBy": (
                            "db_bootstrap.default_world_invariant_repair"
                        ),
                        "createdAt": _utc_now_iso(),
                    },
                },
            )

        db_obj.session.add(registry)
        db_obj.session.flush()
        created = True

    else:
        current_status = _safe_str(
            getattr(registry, "status", None),
            "",
        ).lower()
        is_deleted = _safe_bool(
            getattr(registry, "is_deleted", False),
            False,
        )

        if current_status != "active" or is_deleted:
            restore = getattr(registry, "restore", None)
            if callable(restore):
                try:
                    restore(updated_by_user_id=DEFAULT_SYSTEM_BLOCK_BOOTSTRAP_USER_ID)
                    changed = True
                except Exception:
                    pass

            changed = _set_attr_force(registry, "status", "active") or changed
            changed = _set_attr_force(registry, "deleted_at", None) or changed
            changed = _set_attr_force(registry, "archived_at", None) or changed

        changed = _set_attr_force(
            registry,
            "registry_id",
            registry_id,
        ) or changed
        changed = _set_attr_force(
            registry,
            "registry_version",
            registry_version,
        ) or changed
        changed = _set_attr_if_empty(
            registry,
            "label",
            "Debug Blocks",
        ) or changed
        changed = _set_attr_if_supported(
            registry,
            "is_default",
            True,
        ) or changed
        changed = _set_attr_force(
            registry,
            "source",
            DEFAULT_BLOCK_REGISTRY_SOURCE,
        ) or changed
        changed = _set_attr_if_supported(
            registry,
            "updated_by_user_id",
            DEFAULT_SYSTEM_BLOCK_BOOTSTRAP_USER_ID,
        ) or changed
        _merge_metadata_json(
            registry,
            {
                "seededBy": (
                    "db_bootstrap.default_world_invariant_repair"
                ),
                "updatedAt": _utc_now_iso(),
            },
        )

        if changed:
            db_obj.session.flush()

    operations.append(
        {
            "kind": "block_registry",
            "status": (
                "created"
                if created
                else "updated"
                if changed
                else "existing"
            ),
            "registryId": registry_id,
            "registryVersion": registry_version,
            "source": _safe_str(
                getattr(registry, "source", None),
                DEFAULT_BLOCK_REGISTRY_SOURCE,
            ),
            "dbId": _row_db_id(registry),
        }
    )

    return registry


def _create_or_update_project(
    app: Flask,
    db_obj: Any,
    *,
    operations: list[dict[str, Any]],
) -> Any:
    """Ensure the default Project and its canonical owner fields exist."""
    Project = _model_class("Project")
    if Project is None:
        raise RuntimeError("Project model is unavailable.")

    project_id = _default_project_id(app)
    universe_id = _default_universe_id(app)
    world_id = _default_world_id(app)
    configured_owner = _configured_project_owner_auth_user_id(app)
    project = _query_first_by_fields(
        db_obj.session,
        Project,
        project_id=project_id,
    )

    created = False
    if project is None:
        factory = getattr(Project, "create_dev_project", None)
        if callable(factory):
            attempts = (
                {
                    "project_id": project_id,
                    "default_universe_id": universe_id,
                    "default_world_id": world_id,
                    "spawn_world_id": world_id,
                    "owner_user_id": configured_owner,
                    "created_by_user_id": configured_owner,
                },
                {
                    "project_id": project_id,
                    "default_universe_id": universe_id,
                    "default_world_id": world_id,
                    "owner_user_id": configured_owner,
                    "created_by_user_id": configured_owner,
                },
                {
                    "project_id": project_id,
                    "default_universe_id": universe_id,
                    "owner_user_id": configured_owner,
                    "created_by_user_id": configured_owner,
                },
            )
            for kwargs in attempts:
                try:
                    project = factory(**kwargs)
                    break
                except TypeError:
                    continue
            if project is None:
                raise RuntimeError(
                    "Project.create_dev_project did not accept a canonical owner contract."
                )
        else:
            project = _make_instance(
                Project,
                {
                    "project_id": project_id,
                    "slug": project_id,
                    "name": "Dev Project",
                    "description": "Default development chunk project.",
                    "status": "active",
                    "schema_version": "project.schema.v3",
                    "revision": 1,
                    "default_universe_id": universe_id,
                    "default_world_id": world_id,
                    "spawn_world_id": world_id,
                    "owner_auth_user_id": configured_owner,
                    "owner_type": "user",
                    "owner_id": configured_owner,
                    "created_by_auth_user_id": configured_owner,
                    "updated_by_auth_user_id": configured_owner,
                    "created_by_user_id": configured_owner,
                    "updated_by_user_id": configured_owner,
                    "metadata_json": {
                        "seededBy": "db_bootstrap.default_world_invariant_repair",
                        "ownerAuthUserId": configured_owner,
                        "createdAt": _utc_now_iso(),
                    },
                },
            )
        db_obj.session.add(project)
        db_obj.session.flush()
        created = True

    owner = _project_owner_from_model(project, configured_owner)
    owner = _apply_project_owner_fields(project, owner)
    _set_attr_if_empty(project, "slug", project_id)
    _set_attr_if_empty(project, "name", "Dev Project")
    _set_attr_if_empty(project, "status", "active")
    _set_attr_force(project, "default_universe_id", universe_id)
    _set_attr_force(project, "default_world_id", world_id)
    _set_attr_force(project, "spawn_world_id", world_id)
    _merge_metadata_json(
        project,
        {
            "seededBy": "db_bootstrap.default_world_invariant_repair",
            "ownerAuthUserId": owner,
            "defaultUniverseId": universe_id,
            "defaultWorldId": world_id,
            "spawnWorldId": world_id,
            "updatedAt": _utc_now_iso(),
        },
    )
    db_obj.session.add(project)
    db_obj.session.flush()

    operations.append(
        {
            "kind": "project",
            "status": "created" if created else "existing",
            "projectId": project_id,
            "ownerAuthUserId": owner,
            "ownerReady": _project_owner_fields_ready(project, owner),
            "dbId": _row_db_id(project),
        }
    )
    return project


def _create_or_update_universe(
    app: Flask,
    db_obj: Any,
    project: Any,
    *,
    operations: list[dict[str, Any]],
) -> Any:
    """Ensure the default Universe exists under the canonical project owner."""
    Universe = _model_class("Universe")
    if Universe is None:
        raise RuntimeError("Universe model is unavailable.")

    universe_id = _default_universe_id(app)
    world_id = _default_world_id(app)
    project_db_id = _row_db_id(project)
    actor = _project_owner_from_model(
        project,
        _configured_project_owner_auth_user_id(app),
    )
    query_fields = {"universe_id": universe_id}
    if project_db_id is not None:
        query_fields["project_db_id"] = project_db_id
    universe = _query_first_by_fields(
        db_obj.session,
        Universe,
        **query_fields,
    )

    created = False
    if universe is None:
        factory = getattr(Universe, "create_for_project", None)
        if callable(factory):
            try:
                universe = factory(
                    project,
                    universe_id=universe_id,
                    slug=universe_id,
                    name="Dev Universe",
                    default_world_id=world_id,
                    spawn_world_id=world_id,
                    created_by_user_id=actor,
                    metadata_json={
                        "seededBy": "db_bootstrap.default_world_invariant_repair",
                        "createdAt": _utc_now_iso(),
                    },
                )
            except TypeError:
                universe = None
        if universe is None:
            universe = _make_instance(
                Universe,
                {
                    "project_db_id": project_db_id,
                    "universe_id": universe_id,
                    "slug": universe_id,
                    "name": "Dev Universe",
                    "description": "Default development chunk universe.",
                    "status": "active",
                    "schema_version": "universe.schema.v2",
                    "revision": 1,
                    "universe_role": "default",
                    "universe_scope": "project",
                    "default_world_id": world_id,
                    "spawn_world_id": world_id,
                    "created_by_user_id": actor,
                    "updated_by_user_id": actor,
                    "metadata_json": {
                        "seededBy": "db_bootstrap.default_world_invariant_repair",
                        "createdAt": _utc_now_iso(),
                    },
                },
            )
        db_obj.session.add(universe)
        db_obj.session.flush()
        created = True

    _set_attr_force(universe, "project_db_id", project_db_id)
    _set_attr_if_empty(universe, "slug", universe_id)
    _set_attr_if_empty(universe, "name", "Dev Universe")
    _set_attr_if_empty(universe, "status", "active")
    _set_attr_force(universe, "default_world_id", world_id)
    _set_attr_force(universe, "spawn_world_id", world_id)
    _set_attr_if_supported(universe, "created_by_user_id", actor)
    _set_attr_if_supported(universe, "updated_by_user_id", actor)
    _merge_metadata_json(
        universe,
        {
            "seededBy": "db_bootstrap.default_world_invariant_repair",
            "defaultWorldId": world_id,
            "spawnWorldId": world_id,
            "updatedAt": _utc_now_iso(),
        },
    )
    db_obj.session.add(universe)
    db_obj.session.flush()
    operations.append(
        {
            "kind": "universe",
            "status": "created" if created else "existing",
            "universeId": universe_id,
            "dbId": _row_db_id(universe),
        }
    )
    return universe


def _create_world_with_factory_or_direct(
    app: Flask,
    db_obj: Any,
    project: Any,
    universe: Any,
) -> Any:
    """Create WorldInstance with canonical owner actor metadata."""
    WorldInstance = _model_class("WorldInstance")
    if WorldInstance is None:
        raise RuntimeError("WorldInstance model is unavailable.")

    world_id = _default_world_id(app)
    registry_id = _default_block_registry_id(app)
    registry_version = _default_block_registry_version(app)
    template_id = _default_template_id(app)
    provider_id = _default_provider_id(app)
    provider_world_id = _default_provider_world_id(app)
    project_db_id = _row_db_id(project)
    universe_db_id = _row_db_id(universe)
    actor = _project_owner_from_model(
        project,
        _configured_project_owner_auth_user_id(app),
    )

    factory = getattr(WorldInstance, "create_flat_spawn", None)
    if callable(factory):
        attempts = [
            {
                "project_db_id": project_db_id,
                "universe_db_id": universe_db_id,
                "world_id": world_id,
                "slug": "spawn",
                "name": "Flat Spawn World",
                "created_by_user_id": actor,
                "metadata_json": {
                    "seededBy": "db_bootstrap.default_world_invariant_repair",
                    "createdAt": _utc_now_iso(),
                },
            },
            {
                "project": project,
                "universe": universe,
                "world_id": world_id,
                "slug": "spawn",
                "name": "Flat Spawn World",
                "created_by_user_id": actor,
                "metadata_json": {
                    "seededBy": "db_bootstrap.default_world_invariant_repair",
                    "createdAt": _utc_now_iso(),
                },
            },
            {
                "world_id": world_id,
                "project_db_id": project_db_id,
                "universe_db_id": universe_db_id,
            },
        ]
        for kwargs in attempts:
            try:
                world = factory(**kwargs)
                db_obj.session.add(world)
                return world
            except TypeError:
                continue
            except Exception:
                break

    return _make_instance(
        WorldInstance,
        {
            "project_db_id": project_db_id,
            "universe_db_id": universe_db_id,
            "world_id": world_id,
            "slug": "spawn",
            "name": "Flat Spawn World",
            "description": "Default editable spawn world.",
            "status": "active",
            "schema_version": "world-instance.schema.v2",
            "revision": 1,
            "world_type": "runtime-world",
            "world_role": "default_spawn",
            "world_scope": "project",
            "template_id": template_id,
            "provider_id": provider_id,
            "provider_world_id": provider_world_id,
            "generator_type": _config_str(
                app, "VECTOPLAN_CHUNK_DEFAULT_GENERATOR_TYPE", "flat-world"
            ),
            "generator_version": _config_str(
                app, "VECTOPLAN_CHUNK_DEFAULT_GENERATOR_VERSION", "1"
            ),
            "projection_type": _config_str(
                app, "VECTOPLAN_CHUNK_DEFAULT_PROJECTION_TYPE", "flat-local-v1"
            ),
            "topology_type": _config_str(
                app, "VECTOPLAN_CHUNK_DEFAULT_TOPOLOGY_TYPE", "flat-unbounded-v1"
            ),
            "coordinate_system": _config_str(
                app,
                "VECTOPLAN_CHUNK_DEFAULT_COORDINATE_SYSTEM",
                "vectoplan-world-y-up-v1",
            ),
            "chunk_size": _safe_int(
                _config_value(app, "VECTOPLAN_CHUNK_DEFAULT_CHUNK_SIZE", 16), 16
            ),
            "cell_size": _safe_float(
                _config_value(app, "VECTOPLAN_CHUNK_DEFAULT_CELL_SIZE", 1.0), 1.0
            ),
            "surface_y": _safe_int(
                _config_value(app, "VECTOPLAN_CHUNK_DEFAULT_SURFACE_Y", 0), 0
            ),
            "min_y": _safe_int(
                _config_value(app, "VECTOPLAN_CHUNK_DEFAULT_MIN_Y", -8), -8
            ),
            "max_y": _safe_int(
                _config_value(app, "VECTOPLAN_CHUNK_DEFAULT_MAX_Y", 64), 64
            ),
            "seed": _config_str(app, "VECTOPLAN_CHUNK_DEFAULT_SEED", "dev-seed"),
            "block_registry_id": registry_id,
            "block_registry_version": registry_version,
            "spawn_x": _safe_int(
                _config_value(app, "VECTOPLAN_CHUNK_DEFAULT_SPAWN_X", 0), 0
            ),
            "spawn_y": _safe_int(
                _config_value(app, "VECTOPLAN_CHUNK_DEFAULT_SPAWN_Y", 2), 2
            ),
            "spawn_z": _safe_int(
                _config_value(app, "VECTOPLAN_CHUNK_DEFAULT_SPAWN_Z", 0), 0
            ),
            "spawn_yaw": _safe_float(
                _config_value(app, "VECTOPLAN_CHUNK_DEFAULT_SPAWN_YAW", 0.0), 0.0
            ),
            "spawn_pitch": _safe_float(
                _config_value(app, "VECTOPLAN_CHUNK_DEFAULT_SPAWN_PITCH", 0.0), 0.0
            ),
            "source_service": "vectoplan-chunk-bootstrap",
            "external_ref": world_id,
            "created_by_user_id": actor,
            "updated_by_user_id": actor,
            "metadata_json": {
                "seededBy": "db_bootstrap.default_world_invariant_repair",
                "createdAt": _utc_now_iso(),
                "templateId": template_id,
                "providerId": provider_id,
                "providerWorldId": provider_world_id,
            },
        },
    )


def _create_or_update_world(
    app: Flask,
    db_obj: Any,
    project: Any,
    universe: Any,
    *,
    operations: list[dict[str, Any]],
) -> Any:
    """Ensure concrete editable default WorldInstance exists."""
    WorldInstance = _model_class("WorldInstance")
    if WorldInstance is None:
        raise RuntimeError("WorldInstance model is unavailable.")

    world_id = _default_world_id(app)
    project_db_id = _row_db_id(project)
    universe_db_id = _row_db_id(universe)
    actor = _project_owner_from_model(
        project,
        _configured_project_owner_auth_user_id(app),
    )
    query_fields = {"world_id": world_id}
    if project_db_id is not None:
        query_fields["project_db_id"] = project_db_id
    if universe_db_id is not None:
        query_fields["universe_db_id"] = universe_db_id
    world = _query_first_by_fields(
        db_obj.session,
        WorldInstance,
        **query_fields,
    )

    created = False
    if world is None:
        world = _create_world_with_factory_or_direct(
            app,
            db_obj,
            project,
            universe,
        )
        db_obj.session.add(world)
        db_obj.session.flush()
        created = True

    _set_attr_force(world, "project_db_id", project_db_id)
    _set_attr_force(world, "universe_db_id", universe_db_id)
    _set_attr_if_empty(world, "slug", "spawn")
    _set_attr_if_empty(world, "name", "Flat Spawn World")
    _set_attr_if_empty(world, "status", "active")

    # Existing world type/template is immutable during a normal bootstrap retry.
    if created:
        _set_attr_force(world, "template_id", _default_template_id(app))
        _set_attr_force(world, "provider_id", _default_provider_id(app))
        _set_attr_force(
            world,
            "provider_world_id",
            _default_provider_world_id(app),
        )
    _set_attr_if_empty(
        world,
        "block_registry_id",
        _default_block_registry_id(app),
    )
    _set_attr_if_empty(
        world,
        "block_registry_version",
        _default_block_registry_version(app),
    )
    _set_attr_if_supported(
        world,
        "spawn_x",
        _safe_int(_config_value(app, "VECTOPLAN_CHUNK_DEFAULT_SPAWN_X", 0), 0),
    )
    _set_attr_if_supported(
        world,
        "spawn_y",
        _safe_int(_config_value(app, "VECTOPLAN_CHUNK_DEFAULT_SPAWN_Y", 2), 2),
    )
    _set_attr_if_supported(
        world,
        "spawn_z",
        _safe_int(_config_value(app, "VECTOPLAN_CHUNK_DEFAULT_SPAWN_Z", 0), 0),
    )
    _set_attr_if_supported(
        world,
        "spawn_yaw",
        _safe_float(
            _config_value(app, "VECTOPLAN_CHUNK_DEFAULT_SPAWN_YAW", 0.0),
            0.0,
        ),
    )
    _set_attr_if_supported(
        world,
        "spawn_pitch",
        _safe_float(
            _config_value(app, "VECTOPLAN_CHUNK_DEFAULT_SPAWN_PITCH", 0.0),
            0.0,
        ),
    )
    _set_attr_if_supported(world, "created_by_user_id", actor)
    _set_attr_if_supported(world, "updated_by_user_id", actor)
    _merge_metadata_json(
        world,
        {
            "seededBy": "db_bootstrap.default_world_invariant_repair",
            "worldId": world_id,
            "templateId": _safe_str(getattr(world, "template_id", None), ""),
            "providerId": _safe_str(getattr(world, "provider_id", None), ""),
            "providerWorldId": _safe_str(
                getattr(world, "provider_world_id", None), ""
            ),
            "updatedAt": _utc_now_iso(),
        },
    )
    db_obj.session.add(world)
    db_obj.session.flush()
    operations.append(
        {
            "kind": "world",
            "status": "created" if created else "existing",
            "worldId": world_id,
            "templateId": getattr(world, "template_id", None),
            "dbId": _row_db_id(world),
        }
    )
    return world


def repair_default_world_invariant(
    app: Flask,
    *,
    db_extension: Any = None,
    commit: bool = True,
) -> dict[str, Any]:
    """Repair the complete default graph including owner and access projection."""
    started_at = _utc_now_iso()

    with _app_context(app):
        db_obj = _get_db_extension(db_extension)
        result: dict[str, Any] = {
            "ok": False,
            "status": STATUS_FAILED,
            "startedAt": started_at,
            "completedAt": None,
            "durationMs": 0,
            "executed": True,
            "operations": [],
            "warnings": [],
            "errors": [],
            "before": {},
            "after": {},
            "projectAccess": {},
            "systemBlocks": {},
        }
        if db_obj is None:
            result["errors"].append(
                _make_message(
                    code="db_extension_unavailable",
                    message="SQLAlchemy db extension is unavailable.",
                )
            )
            completed_at = _utc_now_iso()
            result["completedAt"] = completed_at
            result["durationMs"] = _duration_ms(started_at, completed_at)
            return result

        try:
            result["before"] = build_default_world_invariant_status(
                app,
                db_extension=db_obj,
            )
            registry = _create_or_update_block_registry(
                app,
                db_obj,
                operations=result["operations"],
            )
            if registry is None:
                raise RuntimeError(
                    "Default BlockRegistry could not be created or loaded."
                )
            result["systemBlocks"] = _reconcile_system_blocks_for_registry(
                registry,
                db_obj,
                operations=result["operations"],
            )

            project = _create_or_update_project(
                app,
                db_obj,
                operations=result["operations"],
            )
            owner = _project_owner_from_model(
                project,
                _configured_project_owner_auth_user_id(app),
            )
            owner = _apply_project_owner_fields(project, owner)
            if _project_access_required(app):
                result["projectAccess"] = _ensure_default_project_access(
                    app,
                    project,
                    owner,
                    db_obj,
                )
                result["operations"].append(
                    {
                        "kind": "project_access",
                        "status": "ready",
                        "ownerAuthUserId": owner,
                        "canonicalReady": True,
                        "legacyReady": True,
                    }
                )

            universe = _create_or_update_universe(
                app,
                db_obj,
                project,
                operations=result["operations"],
            )
            _create_or_update_world(
                app,
                db_obj,
                project,
                universe,
                operations=result["operations"],
            )
            db_obj.session.flush()
            if commit:
                db_obj.session.commit()

            result["after"] = build_default_world_invariant_status(
                app,
                db_extension=db_obj,
            )
            result["ok"] = bool((result["after"] or {}).get("ok"))
            result["status"] = STATUS_COMPLETED if result["ok"] else STATUS_PARTIAL
            if not result["ok"]:
                result["errors"].append(
                    _make_message(
                        code="default_world_invariant_repair_incomplete",
                        message=(
                            "Default world, owner, project-access or system-block "
                            "repair did not produce a ready state."
                        ),
                        details={"after": result["after"]},
                    )
                )
        except Exception as exc:
            try:
                db_obj.session.rollback()
            except Exception:
                pass
            result["ok"] = False
            result["status"] = STATUS_FAILED
            result["errors"].append(
                _make_message(
                    code="default_world_invariant_repair_exception",
                    message=_safe_exception_message(exc),
                    details={"exceptionType": exc.__class__.__name__},
                )
            )

        completed_at = _utc_now_iso()
        result["completedAt"] = completed_at
        result["durationMs"] = _duration_ms(started_at, completed_at)
        return result


def _seed_status_needs_invariant_repair(
    seed_data: Mapping[str, Any] | None,
    seed_status: Mapping[str, Any] | None,
    invariant_status: Mapping[str, Any] | None,
) -> bool:
    """Return whether world, system blocks, owner or access need repair."""
    seed_data = seed_data or {}
    seed_status = seed_status or {}
    invariant_status = invariant_status or {}

    if invariant_status and invariant_status.get("ok") is False:
        return True

    invariant_ready = _safe_dict(invariant_status.get("ready"))
    for key in (
        "project",
        "projectOwner",
        "projectAccess",
        "canonicalProjectAccess",
        "legacyProjectAccess",
        "universe",
        "world",
        "blockRegistry",
        "systemBlocks",
        "airInvariant",
        "systemRailing",
    ):
        if key in invariant_ready and not _safe_bool(
            invariant_ready.get(key),
            False,
        ):
            return True

    for payload in (seed_data, seed_status):
        if not payload:
            continue
        if payload.get("ok") is False:
            return True
        if _safe_str(payload.get("status"), "").lower() in {
            STATUS_PARTIAL,
            STATUS_FAILED,
        }:
            return True
        for key in (
            "projectAccessReady",
            "defaultProjectAccessReady",
            "projectOwnerReady",
            "systemBlocksReady",
            "systemRailingReady",
            "airInvariantReady",
        ):
            if key in payload and not _safe_bool(payload.get(key), False):
                return True

        world = payload.get("world")
        if isinstance(world, Mapping) and world.get("exists") is False:
            return True
        default_world = payload.get("defaultWorld")
        if isinstance(default_world, Mapping) and default_world.get("exists") is False:
            return True
        project_access = payload.get("projectAccess")
        if isinstance(project_access, Mapping) and project_access.get("ready") is False:
            return True
        system_blocks = payload.get("systemBlocks")
        if isinstance(system_blocks, Mapping) and not _system_blocks_ready(system_blocks):
            return True
        ready = payload.get("ready")
        if isinstance(ready, Mapping):
            for key in (
                "projectOwner",
                "projectAccess",
                "canonicalProjectAccess",
                "legacyProjectAccess",
                "systemBlocks",
                "airInvariant",
                "systemRailing",
            ):
                if key in ready and not _safe_bool(ready.get(key), False):
                    return True
        invariant = payload.get("defaultWorldInvariant")
        if isinstance(invariant, Mapping) and invariant.get("ok") is False:
            return True
    return False


def _seed_invariant_repair_enabled(app: Any, seed_settings: Any = None) -> bool:
    """Return whether bootstrap may repair seed invariants."""
    try:
        value = getattr(seed_settings, "repair_seed_invariants", None)
        if value is not None:
            return _safe_bool(value, True)
    except Exception:
        pass

    return _config_bool(
        app,
        "VECTOPLAN_CHUNK_DB_BOOTSTRAP_REPAIR_SEED_INVARIANTS",
        True,
    )


def _build_seed_status_with_invariant(
    app: Flask,
    *,
    db_extension: Any = None,
) -> dict[str, Any]:
    """
    Build seed readiness with mandatory owner/access/world/system invariants.

    If default_seed.py is temporarily unavailable, the explicit orchestrator's
    own invariant status is the authoritative fallback. A missing optional
    adapter must not force an otherwise complete direct repair to roll back.
    """
    invariant_status = build_default_world_invariant_status(
        app,
        db_extension=db_extension,
    )

    seed_status_factory = _get_default_seed_callable(
        "build_default_seed_status"
    )
    seed_status: dict[str, Any] = {}
    factory_used = False
    factory_error: str | None = None

    if callable(seed_status_factory):
        try:
            seed_status = _safe_dict(
                seed_status_factory(
                    app,
                    db_extension=db_extension,
                )
            )
            factory_used = bool(seed_status)
            if not factory_used:
                factory_error = (
                    "build_default_seed_status returned an empty payload."
                )
        except Exception as exc:
            factory_error = (
                f"{exc.__class__.__name__}: {_safe_exception_message(exc)}"
            )
    else:
        factory_error = "build_default_seed_status is unavailable."

    if not factory_used:
        invariant_ok = _safe_bool(invariant_status.get("ok"), False)
        seed_status = {
            "ok": invariant_ok,
            "status": STATUS_READY if invariant_ok else STATUS_PARTIAL,
            "source": "db_bootstrap.invariant_status_fallback",
            "degradedStatusSource": True,
            "defaultSeedStatusAvailable": False,
            "defaultSeedStatusError": factory_error,
            "defaultSeedApi": _default_seed_api_diagnostics(),
            "warnings": (
                [
                    _make_message(
                        code="default_seed_status_adapter_unavailable",
                        message=(
                            "Default-seed status adapter is unavailable; "
                            "using the direct bootstrap invariant status."
                        ),
                        details={
                            "error": factory_error,
                            "fallbackSource": (
                                "build_default_world_invariant_status"
                            ),
                        },
                    )
                ]
                if factory_error
                else []
            ),
        }
    else:
        seed_status.setdefault("source", "src.bootstrap.default_seed")
        seed_status.setdefault("degradedStatusSource", False)
        seed_status.setdefault("defaultSeedStatusAvailable", True)
        seed_status.setdefault("defaultSeedApi", _default_seed_api_diagnostics())

    seed_status = _safe_dict(seed_status)
    seed_status["defaultWorldInvariant"] = invariant_status
    seed_status["projectAccess"] = _safe_dict(
        invariant_status.get("projectAccess")
    )
    invariant_system_blocks = _safe_dict(invariant_status.get("systemBlocks"))
    seed_status.setdefault("systemBlocks", invariant_system_blocks)

    if invariant_status.get("ok") is not True:
        seed_status["ok"] = False
        seed_status["status"] = STATUS_PARTIAL
        seed_status.setdefault("errors", [])
        if isinstance(seed_status["errors"], list):
            seed_status["errors"].append(
                _make_message(
                    code=(
                        "default_owner_access_world_or_system_"
                        "invariant_not_ready"
                    ),
                    message=(
                        "Default owner, project access, world or built-in "
                        "system-block invariant is not ready."
                    ),
                    details=invariant_status,
                )
            )
        return seed_status

    # A successfully imported default-seed status remains authoritative for its
    # own optional seed contract. The fallback status is already derived from
    # the complete invariant and can become ready here.
    if factory_used and seed_status.get("ok") is not True:
        return seed_status

    seed_status["ok"] = True
    seed_status["status"] = STATUS_READY
    return seed_status


# -----------------------------------------------------------------------------
# Settings resolution
# -----------------------------------------------------------------------------

def resolve_bootstrap_settings(
    app: Any = None,
    settings: BootstrapSettings | None = None,
) -> Any:
    """Resolve aggregate bootstrap settings with a safe compatible fallback."""
    if settings is not None:
        return settings
    try:
        resolved = build_bootstrap_settings(app)
        if resolved is not None:
            return resolved
    except Exception:
        pass

    schema_enabled = get_bool_setting(
        app,
        "VECTOPLAN_CHUNK_DB_BOOTSTRAP_ENABLED",
        False,
        aliases=("DB_BOOTSTRAP_ENABLED",),
    )
    schema = SimpleNamespace(
        bootstrap_enabled=schema_enabled,
        create_all=get_bool_setting(
            app,
            "VECTOPLAN_CHUNK_DB_BOOTSTRAP_CREATE_ALL",
            schema_enabled,
            aliases=(
                "DB_BOOTSTRAP_CREATE_ALL",
                "VECTOPLAN_CHUNK_AUTO_CREATE_ALL",
            ),
        ),
        fail_on_error=get_bool_setting(
            app,
            "VECTOPLAN_CHUNK_DB_BOOTSTRAP_FAIL_ON_ERROR",
            True,
            aliases=("DB_BOOTSTRAP_FAIL_ON_ERROR",),
        ),
    )
    seed_defaults = get_bool_setting(
        app,
        "VECTOPLAN_CHUNK_DB_BOOTSTRAP_SEED_DEFAULTS",
        schema_enabled,
        aliases=(
            "DB_BOOTSTRAP_SEED_DEFAULTS",
            "VECTOPLAN_CHUNK_AUTO_SEED_DEFAULTS",
        ),
    )
    seed_dev_project = get_bool_setting(
        app,
        "VECTOPLAN_CHUNK_DB_BOOTSTRAP_SEED_DEV_PROJECT",
        seed_defaults,
        aliases=(
            "DB_BOOTSTRAP_SEED_DEV_PROJECT",
            "VECTOPLAN_CHUNK_SEED_DEV_PROJECT",
        ),
    )
    seed = SimpleNamespace(
        seed_defaults=seed_defaults,
        seed_debug_blocks=get_bool_setting(
            app,
            "VECTOPLAN_CHUNK_DB_BOOTSTRAP_SEED_DEBUG_BLOCKS",
            seed_defaults,
            aliases=(
                "DB_BOOTSTRAP_SEED_DEBUG_BLOCKS",
                "VECTOPLAN_CHUNK_SEED_DEBUG_BLOCKS",
            ),
        ),
        seed_dev_project=seed_dev_project,
        seed_project_access=bool(
            seed_dev_project
            or get_bool_setting(
                app,
                "VECTOPLAN_CHUNK_DB_BOOTSTRAP_SEED_PROJECT_ACCESS",
                seed_dev_project,
                aliases=(
                    "VECTOPLAN_CHUNK_SEED_PROJECT_ACCESS",
                    "CHUNK_DB_BOOTSTRAP_SEED_PROJECT_ACCESS",
                ),
            )
        ),
        seed_on_empty_only=get_bool_setting(
            app,
            "VECTOPLAN_CHUNK_SEED_ON_EMPTY_ONLY",
            True,
            aliases=("DB_BOOTSTRAP_SEED_ON_EMPTY_ONLY",),
        ),
        repair_seed_invariants=get_bool_setting(
            app,
            "VECTOPLAN_CHUNK_DB_BOOTSTRAP_REPAIR_SEED_INVARIANTS",
            True,
            aliases=("DB_BOOTSTRAP_REPAIR_SEED_INVARIANTS",),
        ),
        fail_on_error=get_bool_setting(
            app,
            "VECTOPLAN_CHUNK_DB_BOOTSTRAP_FAIL_ON_ERROR",
            True,
            aliases=("DB_BOOTSTRAP_FAIL_ON_ERROR",),
        ),
    )
    owner = _configured_project_owner_auth_user_id(app)
    world_defaults = SimpleNamespace(
        project_owner_auth_user_id=owner,
        default_project_owner_auth_user_id=owner,
        owner_auth_user_id=owner,
        project_owner_user_id=owner,
        default_project_owner_user_id=owner,
        owner_user_id=owner,
    )
    return SimpleNamespace(
        schema=schema,
        seed=seed,
        world_defaults=world_defaults,
        block_defaults=None,
        identity=None,
    )

def get_effective_db_bootstrap_flags(
    app: Any = None,
    *,
    settings: BootstrapSettings | None = None,
    enabled: bool | None = None,
    run_schema: bool | None = None,
    run_seed: bool | None = None,
    fail_on_error: bool | None = None,
) -> dict[str, bool]:
    """Resolve effective schema, seed and project-access bootstrap flags."""
    resolved = resolve_bootstrap_settings(app, settings)
    schema_settings = getattr(resolved, "schema", None)
    seed_settings = getattr(resolved, "seed", None)
    schema_enabled = _safe_bool(
        getattr(schema_settings, "bootstrap_enabled", False), False
    )
    schema_create_all = _safe_bool(
        getattr(schema_settings, "create_all", False), False
    )
    seed_defaults = _safe_bool(
        getattr(seed_settings, "seed_defaults", False), False
    )
    resolved_enabled = bool(enabled if enabled is not None else schema_enabled)
    resolved_run_schema = bool(
        run_schema
        if run_schema is not None
        else (resolved_enabled and schema_create_all)
    )
    resolved_run_seed = bool(
        run_seed
        if run_seed is not None
        else (resolved_enabled and seed_defaults)
    )
    resolved_fail_on_error = bool(
        fail_on_error
        if fail_on_error is not None
        else (
            _safe_bool(getattr(schema_settings, "fail_on_error", True), True)
            and _safe_bool(getattr(seed_settings, "fail_on_error", True), True)
        )
    )
    return {
        "enabled": resolved_enabled,
        "runSchema": resolved_run_schema,
        "runSeed": resolved_run_seed,
        "runProjectAccess": bool(
            resolved_run_seed and _project_access_required(app, seed_settings)
        ),
        "failOnError": resolved_fail_on_error,
    }


# -----------------------------------------------------------------------------
# Status helpers
# -----------------------------------------------------------------------------

def build_db_bootstrap_status(
    app: Flask,
    *,
    db_extension: Any = None,
) -> dict[str, Any]:
    """Build read-only schema, seed, access, world and system readiness."""
    started_at = _utc_now_iso()
    with _app_context(app):
        schema_status: dict[str, Any] = {}
        seed_status: dict[str, Any] = {}
        schema_ok = False
        seed_ok = False

        if build_schema_status is not None:
            try:
                schema_status = build_schema_status(
                    app,
                    db_extension=db_extension,
                )
                schema_ok = bool(schema_status.get("ok"))
            except Exception as exc:
                schema_status = {
                    "ok": False,
                    "status": STATUS_FAILED,
                    "error": _safe_exception_message(exc),
                    "exceptionType": exc.__class__.__name__,
                }
        else:
            schema_status = {
                "ok": False,
                "status": STATUS_FAILED,
                "error": "build_schema_status is unavailable.",
            }

        try:
            seed_status = _build_seed_status_with_invariant(
                app,
                db_extension=db_extension,
            )
            seed_ok = bool(seed_status.get("ok"))
        except Exception as exc:
            seed_status = {
                "ok": False,
                "status": STATUS_FAILED,
                "error": _safe_exception_message(exc),
                "exceptionType": exc.__class__.__name__,
            }

        invariant = _safe_dict(seed_status.get("defaultWorldInvariant"))
        ready = _safe_dict(invariant.get("ready"))
        project_access = _safe_dict(invariant.get("projectAccess"))
        system_blocks = _safe_dict(invariant.get("systemBlocks"))
        counts = _system_block_status_counts(system_blocks)
        access_counts = _safe_dict(project_access.get("counts"))
        system_ready = _system_blocks_ready(system_blocks)
        air_ready = _air_invariant_ready(system_blocks)
        railing_ready = _system_railing_ready(system_blocks)
        project_access_ready = _safe_bool(
            project_access.get("ready"), False
        )
        completed_at = _utc_now_iso()

        return {
            "ok": bool(schema_ok and seed_ok),
            "status": STATUS_COMPLETED if schema_ok and seed_ok else STATUS_PARTIAL,
            "startedAt": started_at,
            "completedAt": completed_at,
            "durationMs": _duration_ms(started_at, completed_at),
            "schemaReady": schema_ok,
            "seedReady": seed_ok,
            "defaultProjectReady": ready.get("project"),
            "projectOwnerReady": ready.get("projectOwner"),
            "projectAccessReady": project_access_ready,
            "canonicalProjectAccessReady": project_access.get("canonicalReady"),
            "legacyProjectAccessReady": project_access.get("legacyReady"),
            "projectOwnerAuthUserId": project_access.get("ownerAuthUserId"),
            "projectAccessAssignmentCount": _safe_int(
                access_counts.get("canonicalAssignments"), 0
            ),
            "projectAccessRoleCount": _safe_int(
                access_counts.get("legacyRoles"), 0
            ),
            "defaultUniverseReady": ready.get("universe"),
            "defaultWorldReady": ready.get("world"),
            "blockRegistryReady": ready.get("blockRegistry"),
            "debugBlocksReady": ready.get("debugBlocks"),
            "debugBlocksRequired": _debug_blocks_required(app),
            "systemBlocksReady": system_ready,
            "systemRailingReady": railing_ready,
            "airInvariantReady": air_ready,
            "systemBlockCount": counts["mirrors"],
            "systemBlocksMissing": counts["missing"],
            "systemBlocksDrifted": counts["drifted"],
            "schema": schema_status,
            "seed": seed_status,
            "projectAccess": project_access,
            "systemBlocks": system_blocks,
        }


# -----------------------------------------------------------------------------
# Bootstrap runner
# -----------------------------------------------------------------------------

def run_db_bootstrap(
    app: Flask,
    *,
    settings: BootstrapSettings | None = None,
    db_extension: Any = None,
    enabled: bool | None = None,
    run_schema: bool | None = None,
    run_seed: bool | None = None,
    fail_on_error: bool | None = None,
    include_pre_status: bool = True,
    include_post_status: bool = True,
) -> DbBootstrapResult:
    """
    Run explicit DB bootstrap.

    Order:
        1. optional read-only pre-status
        2. schema bootstrap
        3. seed bootstrap
        4. optional read-only post-status

    If schema bootstrap is requested and fails, seed bootstrap is skipped.
    """
    started_at = _utc_now_iso()

    result = DbBootstrapResult(
        ok=False,
        status=STATUS_FAILED,
        started_at=started_at,
    )

    if not _is_flask_app(app):
        result.errors.append(
            _make_message(
                code="invalid_flask_app",
                message="run_db_bootstrap(app) expects a Flask app or compatible object.",
            )
        )
        return _finish_result(result, db_extension=db_extension)

    with _app_context(app):
        resolved_settings = resolve_bootstrap_settings(app, settings)
        flags = get_effective_db_bootstrap_flags(
            app,
            settings=resolved_settings,
            enabled=enabled,
            run_schema=run_schema,
            run_seed=run_seed,
            fail_on_error=fail_on_error,
        )

        result.enabled = bool(flags["enabled"])
        result.schema_bootstrap_requested = bool(flags["runSchema"])
        result.seed_bootstrap_requested = bool(flags["runSeed"])
        result.fail_on_error = bool(flags["failOnError"])

        try:
            result.metadata["settingsAvailable"] = resolved_settings is not None
            result.metadata["flags"] = flags
            result.metadata["defaultSeedApi"] = _default_seed_api_diagnostics()
            result.metadata["defaultIds"] = {
                "projectId": _default_project_id(app),
                "universeId": _default_universe_id(app),
                "worldId": _default_world_id(app),
                "templateId": _default_template_id(app),
                "providerId": _default_provider_id(app),
                "providerWorldId": _default_provider_world_id(app),
                "blockRegistryId": _default_block_registry_id(app),
                "blockRegistryVersion": _default_block_registry_version(app),
                "projectOwnerAuthUserId": _configured_project_owner_auth_user_id(
                    app, resolved_settings
                ),
                "projectAccessRequired": _project_access_required(
                    app, getattr(resolved_settings, "seed", None)
                ),
                "airSystemBlockId": DEFAULT_SYSTEM_AIR_BLOCK_ID,
                "systemRailingBlockTypeId": DEFAULT_SYSTEM_RAILING_BLOCK_TYPE_ID,
            }

            schema_settings = getattr(resolved_settings, "schema", None)
            seed_settings = getattr(resolved_settings, "seed", None)
            identity_settings = getattr(resolved_settings, "identity", None)

            result.metadata["schemaSettings"] = {
                "bootstrapEnabled": _safe_bool(getattr(schema_settings, "bootstrap_enabled", False), False),
                "createAll": _safe_bool(getattr(schema_settings, "create_all", False), False),
                "failOnError": _safe_bool(getattr(schema_settings, "fail_on_error", True), True),
            }
            result.metadata["seedSettings"] = {
                "seedDefaults": _safe_bool(getattr(seed_settings, "seed_defaults", False), False),
                "seedDebugBlocks": _safe_bool(getattr(seed_settings, "seed_debug_blocks", False), False),
                "seedDevProject": _safe_bool(getattr(seed_settings, "seed_dev_project", False), False),
                "seedProjectAccess": _project_access_required(app, seed_settings),
                "seedOnEmptyOnly": _safe_bool(getattr(seed_settings, "seed_on_empty_only", True), True),
                "repairSeedInvariants": _seed_invariant_repair_enabled(app, seed_settings),
                "failOnError": _safe_bool(getattr(seed_settings, "fail_on_error", True), True),
            }
            result.metadata["identity"] = {
                "mode": _safe_str(getattr(identity_settings, "mode", ""), ""),
                "isRuntimeMode": _safe_bool(getattr(identity_settings, "is_runtime_mode", False), False),
                "isDbBootstrapMode": _safe_bool(getattr(identity_settings, "is_db_bootstrap_mode", False), False),
            }

            if build_lock_diagnostics is not None:
                result.metadata["lockDiagnostics"] = build_lock_diagnostics(app, db_extension)
        except Exception:
            pass

        if not result.enabled:
            result.steps.append(
                _make_step(
                    name="db_bootstrap",
                    ok=True,
                    status=STEP_STATUS_SKIPPED,
                    skipped=True,
                    message="DB bootstrap disabled by settings.",
                )
            )
            result.ok = True
            result.status = STATUS_SKIPPED
            return _finish_result(result, db_extension=db_extension)

        if not result.schema_bootstrap_requested and not result.seed_bootstrap_requested:
            result.steps.append(
                _make_step(
                    name="db_bootstrap",
                    ok=True,
                    status=STEP_STATUS_SKIPPED,
                    skipped=True,
                    message="DB bootstrap enabled, but no bootstrap phase is requested.",
                )
            )
            result.ok = True
            result.status = STATUS_SKIPPED
            return _finish_result(result, db_extension=db_extension)

        _safe_log_info(
            app,
            "DB bootstrap started. run_schema=%s run_seed=%s",
            result.schema_bootstrap_requested,
            result.seed_bootstrap_requested,
        )

        if include_pre_status:
            _run_pre_status_step(app, result, db_extension=db_extension)

        if result.schema_bootstrap_requested:
            _run_schema_step(
                app,
                result,
                resolved_settings=resolved_settings,
                db_extension=db_extension,
            )

            if result.schema_bootstrap_ok is False:
                if result.seed_bootstrap_requested:
                    result.steps.append(
                        _make_step(
                            name=STEP_DEFAULT_SEED,
                            ok=True,
                            status=STEP_STATUS_SKIPPED,
                            skipped=True,
                            message="Default seed skipped because schema bootstrap failed.",
                        )
                    )

                _cleanup_db_session(rollback=True, db_extension=db_extension)
                return _finish_or_raise(app, result, result.fail_on_error, db_extension=db_extension)
        else:
            result.steps.append(
                _make_step(
                    name=STEP_SCHEMA_BOOTSTRAP,
                    ok=True,
                    status=STEP_STATUS_SKIPPED,
                    skipped=True,
                    message="Schema bootstrap not requested.",
                )
            )
            result.schema_bootstrap_ok = None

        if result.seed_bootstrap_requested:
            _run_seed_step(
                app,
                result,
                resolved_settings=resolved_settings,
                db_extension=db_extension,
            )

            if result.seed_bootstrap_ok is False:
                _cleanup_db_session(rollback=True, db_extension=db_extension)
                return _finish_or_raise(app, result, result.fail_on_error, db_extension=db_extension)
        else:
            result.steps.append(
                _make_step(
                    name=STEP_DEFAULT_SEED,
                    ok=True,
                    status=STEP_STATUS_SKIPPED,
                    skipped=True,
                    message="Default seed not requested.",
                )
            )
            result.seed_bootstrap_ok = None

        if include_post_status:
            _run_post_status_step(app, result, db_extension=db_extension)
            _apply_post_status_readiness(result)

        if result.errors:
            return _finish_or_raise(app, result, result.fail_on_error, db_extension=db_extension)

        result.ok = True
        result.status = STATUS_COMPLETED

        _safe_log_info(
            app,
            "DB bootstrap completed successfully. schema_ok=%s seed_ok=%s "
            "default_world_ready=%s system_blocks_ready=%s",
            result.schema_bootstrap_ok,
            result.seed_bootstrap_ok,
            result.default_world_ready,
            result.system_blocks_ready,
        )

        return _finish_result(result, db_extension=db_extension)


def _run_pre_status_step(
    app: Flask,
    result: DbBootstrapResult,
    *,
    db_extension: Any = None,
) -> None:
    """Run read-only pre-status step."""
    started_at = _utc_now_iso()

    try:
        status = build_db_bootstrap_status(app, db_extension=db_extension)
        result.pre_status = status
        result.steps.append(
            _make_step(
                name="pre_status",
                ok=True,
                status=STEP_STATUS_OK,
                message="Read-only pre-bootstrap status collected.",
                started_at=started_at,
                data={
                    "ok": bool(status.get("ok")),
                    "schemaReady": bool(status.get("schemaReady")),
                    "seedReady": bool(status.get("seedReady")),
                    "defaultProjectReady": status.get("defaultProjectReady"),
                    "projectOwnerReady": status.get("projectOwnerReady"),
                    "projectAccessReady": status.get("projectAccessReady"),
                    "projectOwnerAuthUserId": status.get("projectOwnerAuthUserId"),
                    "defaultUniverseReady": status.get("defaultUniverseReady"),
                    "defaultWorldReady": status.get("defaultWorldReady"),
                    "blockRegistryReady": status.get("blockRegistryReady"),
                    "systemBlocksReady": status.get("systemBlocksReady"),
                    "airInvariantReady": status.get("airInvariantReady"),
                    "systemRailingReady": status.get("systemRailingReady"),
                },
            )
        )
    except Exception as exc:
        message = _safe_exception_message(exc)
        result.warnings.append(
            _make_message(
                code="pre_status_failed",
                message=message,
                details={"exceptionType": exc.__class__.__name__},
            )
        )
        result.steps.append(
            _make_step(
                name="pre_status",
                ok=False,
                status=STEP_STATUS_WARNING,
                message=message,
                started_at=started_at,
                data={"exceptionType": exc.__class__.__name__},
            )
        )


def _run_schema_step(
    app: Flask,
    result: DbBootstrapResult,
    *,
    resolved_settings: Any,
    db_extension: Any = None,
) -> None:
    """Run schema bootstrap step."""
    started_at = _utc_now_iso()

    if run_schema_bootstrap is None:
        message = "run_schema_bootstrap is unavailable."
        result.schema_bootstrap_ok = False
        result.schema_ready = False
        result.errors.append(
            _make_message(
                code="schema_bootstrap_unavailable",
                message=message,
            )
        )
        result.steps.append(
            _make_step(
                name=STEP_SCHEMA_BOOTSTRAP,
                ok=False,
                status=STEP_STATUS_FAILED,
                message=message,
                started_at=started_at,
            )
        )
        return

    schema_settings = getattr(resolved_settings, "schema", None)

    try:
        schema_result = run_schema_bootstrap(
            app,
            settings=schema_settings,
            db_extension=db_extension,
            fail_on_error=False,
        )

        if schema_bootstrap_result_to_dict is not None:
            schema_data = schema_bootstrap_result_to_dict(schema_result)
        else:
            schema_data = _safe_dict(schema_result)

        result.schema = schema_data
        result.schema_bootstrap_executed = True
        result.schema_bootstrap_ok = bool(schema_data.get("ok"))
        result.schema_ready = result.schema_bootstrap_ok

        summary = {}
        if build_schema_bootstrap_summary is not None:
            try:
                summary = build_schema_bootstrap_summary(schema_result)
            except Exception:
                summary = {}

        result.steps.append(
            _make_step(
                name=STEP_SCHEMA_BOOTSTRAP,
                ok=bool(schema_data.get("ok")),
                status=STEP_STATUS_OK if schema_data.get("ok") else STEP_STATUS_FAILED,
                message=(
                    "Schema bootstrap completed."
                    if schema_data.get("ok")
                    else "Schema bootstrap failed."
                ),
                started_at=started_at,
                data={
                    "summary": summary,
                    "result": schema_data,
                },
            )
        )

        if not schema_data.get("ok"):
            result.errors.append(
                _make_message(
                    code="schema_bootstrap_failed",
                    message="Schema bootstrap failed.",
                    details=summary or schema_data,
                )
            )

    except Exception as exc:
        message = _safe_exception_message(exc)
        result.schema_bootstrap_executed = True
        result.schema_bootstrap_ok = False
        result.schema_ready = False
        result.errors.append(
            _make_message(
                code="schema_bootstrap_exception",
                message=message,
                details={
                    "exceptionType": exc.__class__.__name__,
                },
            )
        )
        result.steps.append(
            _make_step(
                name=STEP_SCHEMA_BOOTSTRAP,
                ok=False,
                status=STEP_STATUS_FAILED,
                message=message,
                started_at=started_at,
                data={
                    "exceptionType": exc.__class__.__name__,
                },
            )
        )


def _run_seed_step(
    app: Flask,
    result: DbBootstrapResult,
    *,
    resolved_settings: Any,
    db_extension: Any = None,
) -> None:
    """Run default seed and repair owner/access/world/system invariants."""
    started_at = _utc_now_iso()
    seed_settings = getattr(resolved_settings, "seed", None)
    world_defaults = getattr(resolved_settings, "world_defaults", None)
    block_defaults = getattr(resolved_settings, "block_defaults", None)
    seed_data: dict[str, Any] = {}
    seed_summary: dict[str, Any] = {}
    seed_run_error: dict[str, Any] | None = None

    seed_runner = _get_default_seed_callable("run_default_seed")
    seed_serializer = _get_default_seed_callable(
        "default_seed_result_to_dict"
    )
    seed_summary_builder = _get_default_seed_callable(
        "build_default_seed_summary"
    )

    if not callable(seed_runner):
        seed_run_error = _make_message(
            code="default_seed_unavailable",
            message=(
                "run_default_seed is unavailable. Falling back to direct "
                "owner/access/world/system invariant repair."
            ),
            details={"defaultSeedApi": _default_seed_api_diagnostics()},
        )
        result.warnings.append(seed_run_error)
    else:
        try:
            seed_result = seed_runner(
                app,
                seed_settings=seed_settings,
                world_defaults=world_defaults,
                block_defaults=block_defaults,
                db_extension=db_extension,
                fail_on_error=False,
            )
            seed_data = (
                seed_serializer(seed_result)
                if callable(seed_serializer)
                else _safe_dict(seed_result)
            )
            if callable(seed_summary_builder):
                try:
                    seed_summary = seed_summary_builder(seed_result)
                except Exception:
                    seed_summary = {}
        except Exception as exc:
            seed_run_error = _make_message(
                code="default_seed_exception",
                message=_safe_exception_message(exc),
                details={
                    "exceptionType": exc.__class__.__name__,
                    "defaultSeedApi": _default_seed_api_diagnostics(),
                },
            )
            seed_data = {
                "ok": False,
                "status": STATUS_FAILED,
                "error": seed_run_error,
            }
            result.warnings.append(seed_run_error)

    result.seed_bootstrap_executed = True
    invariant_before = build_default_world_invariant_status(
        app,
        db_extension=db_extension,
    )
    seed_status_before = _build_seed_status_with_invariant(
        app,
        db_extension=db_extension,
    )
    needs_repair = _seed_status_needs_invariant_repair(
        seed_data,
        seed_status_before,
        invariant_before,
    )
    repair_allowed = _seed_invariant_repair_enabled(app, seed_settings)
    repair_result: dict[str, Any] = {
        "ok": True,
        "executed": False,
        "status": "not_requested",
    }

    if needs_repair and repair_allowed:
        repair_started_at = _utc_now_iso()
        repair_result = repair_default_world_invariant(
            app,
            db_extension=db_extension,
            commit=True,
        )
        result.seed_invariant_repair_executed = True
        result.seed_invariant_repair_ok = bool(repair_result.get("ok"))
        result.steps.append(
            _make_step(
                name=STEP_DEFAULT_SEED_INVARIANT_REPAIR,
                ok=bool(repair_result.get("ok")),
                status=(
                    STEP_STATUS_OK
                    if repair_result.get("ok")
                    else STEP_STATUS_FAILED
                ),
                message=(
                    "Default owner/access/world/system invariant repair completed."
                    if repair_result.get("ok")
                    else "Default owner/access/world/system invariant repair failed."
                ),
                started_at=repair_started_at,
                data={"repair": repair_result},
            )
        )
    elif needs_repair and not repair_allowed:
        result.seed_invariant_repair_executed = False
        result.seed_invariant_repair_ok = False
        result.warnings.append(
            _make_message(
                code="seed_invariant_repair_disabled",
                message="Default seed invariant repair is disabled.",
                details={
                    "invariantBefore": invariant_before,
                    "seedStatusBefore": seed_status_before,
                },
            )
        )
    else:
        result.seed_invariant_repair_executed = False
        result.seed_invariant_repair_ok = None

    invariant_after = build_default_world_invariant_status(
        app,
        db_extension=db_extension,
    )
    seed_status_after = _build_seed_status_with_invariant(
        app,
        db_extension=db_extension,
    )
    final_seed_ok = bool(seed_status_after.get("ok"))
    final_invariant_ok = bool(invariant_after.get("ok"))
    project_access = _safe_dict(invariant_after.get("projectAccess"))
    project_access_counts = _safe_dict(project_access.get("counts"))
    system_blocks = _safe_dict(invariant_after.get("systemBlocks"))
    system_counts = _system_block_status_counts(system_blocks)

    result.seed = {
        "ok": bool(final_seed_ok and final_invariant_ok),
        "status": (
            STATUS_COMPLETED
            if final_seed_ok and final_invariant_ok
            else STATUS_PARTIAL
        ),
        "initialSeedResult": seed_data,
        "initialSeedSummary": seed_summary,
        "initialSeedError": seed_run_error,
        "seedStatusBeforeRepair": seed_status_before,
        "seedStatusAfterRepair": seed_status_after,
        "invariantBeforeRepair": invariant_before,
        "invariantAfterRepair": invariant_after,
        "repair": repair_result,
        "projectAccess": project_access,
        "systemBlocks": system_blocks,
    }
    result.seed_invariant = invariant_after
    result.project_access = project_access
    result.system_blocks = system_blocks
    result.seed_bootstrap_ok = bool(final_seed_ok and final_invariant_ok)
    result.seed_ready = result.seed_bootstrap_ok

    ready = _safe_dict(invariant_after.get("ready"))
    result.default_project_ready = _safe_bool(ready.get("project"), False)
    result.project_owner_ready = _safe_bool(ready.get("projectOwner"), False)
    result.project_access_ready = _safe_bool(ready.get("projectAccess"), False)
    result.canonical_project_access_ready = _safe_bool(
        ready.get("canonicalProjectAccess"), False
    )
    result.legacy_project_access_ready = _safe_bool(
        ready.get("legacyProjectAccess"), False
    )
    result.project_owner_auth_user_id = _safe_str(
        project_access.get("ownerAuthUserId"), ""
    ) or None
    result.project_access_assignment_count = _safe_int(
        project_access_counts.get("canonicalAssignments"), 0
    )
    result.project_access_role_count = _safe_int(
        project_access_counts.get("legacyRoles"), 0
    )
    result.default_universe_ready = _safe_bool(ready.get("universe"), False)
    result.default_world_ready = _safe_bool(ready.get("world"), False)
    result.block_registry_ready = _safe_bool(ready.get("blockRegistry"), False)
    result.debug_blocks_required = _debug_blocks_required(app, seed_settings)
    result.debug_blocks_ready = _safe_bool(ready.get("debugBlocks"), True)
    result.system_blocks_ready = _system_blocks_ready(system_blocks)
    result.air_invariant_ready = _air_invariant_ready(system_blocks)
    result.system_railing_ready = _system_railing_ready(system_blocks)
    result.system_block_count = system_counts["mirrors"]
    result.system_blocks_created = system_counts["created"]
    result.system_blocks_updated = system_counts["updated"]
    result.system_blocks_missing = system_counts["missing"]
    result.system_blocks_drifted = system_counts["drifted"]

    result.steps.append(
        _make_step(
            name=STEP_DEFAULT_SEED,
            ok=bool(result.seed_bootstrap_ok),
            status=(
                STEP_STATUS_OK if result.seed_bootstrap_ok else STEP_STATUS_FAILED
            ),
            message=(
                "Default seed bootstrap completed and owner/access/world/system "
                "invariants are ready."
                if result.seed_bootstrap_ok
                else "Default seed bootstrap or an owner/access/world/system invariant is incomplete."
            ),
            started_at=started_at,
            data={
                "summary": {
                    "initialSeedOk": bool(seed_data.get("ok")),
                    "seedStatusBeforeOk": bool(seed_status_before.get("ok")),
                    "seedStatusAfterOk": bool(seed_status_after.get("ok")),
                    "invariantBeforeOk": bool(invariant_before.get("ok")),
                    "invariantAfterOk": bool(invariant_after.get("ok")),
                    "repairExecuted": bool(repair_result.get("executed")),
                    "repairOk": repair_result.get("ok"),
                    "defaultProjectReady": result.default_project_ready,
                    "projectOwnerReady": result.project_owner_ready,
                    "projectAccessReady": result.project_access_ready,
                    "canonicalProjectAccessReady": (
                        result.canonical_project_access_ready
                    ),
                    "legacyProjectAccessReady": result.legacy_project_access_ready,
                    "projectOwnerAuthUserId": result.project_owner_auth_user_id,
                    "defaultUniverseReady": result.default_universe_ready,
                    "defaultWorldReady": result.default_world_ready,
                    "blockRegistryReady": result.block_registry_ready,
                    "debugBlocksRequired": result.debug_blocks_required,
                    "debugBlocksReady": result.debug_blocks_ready,
                    "systemBlocksReady": result.system_blocks_ready,
                    "airInvariantReady": result.air_invariant_ready,
                    "systemRailingReady": result.system_railing_ready,
                    "systemBlockCount": result.system_block_count,
                    "systemBlocksMissing": result.system_blocks_missing,
                    "systemBlocksDrifted": result.system_blocks_drifted,
                },
                "result": result.seed,
            },
        )
    )

    if not result.seed_bootstrap_ok:
        result.errors.append(
            _make_message(
                code="default_seed_failed",
                message=(
                    "Default seed bootstrap failed or the owner/access/world/"
                    "system-block invariant remained partial."
                ),
                details={
                    "seedStatusAfterRepair": seed_status_after,
                    "invariantAfterRepair": invariant_after,
                    "projectAccess": project_access,
                    "systemBlocks": system_blocks,
                    "repair": repair_result,
                },
            )
        )


def _run_post_status_step(
    app: Flask,
    result: DbBootstrapResult,
    *,
    db_extension: Any = None,
) -> None:
    """Run read-only post-status step."""
    started_at = _utc_now_iso()

    try:
        status = build_db_bootstrap_status(app, db_extension=db_extension)
        result.post_status = status
        result.steps.append(
            _make_step(
                name="post_status",
                ok=True,
                status=STEP_STATUS_OK,
                message="Read-only post-bootstrap status collected.",
                started_at=started_at,
                data={
                    "ok": bool(status.get("ok")),
                    "schemaReady": bool(status.get("schemaReady")),
                    "seedReady": bool(status.get("seedReady")),
                    "defaultProjectReady": status.get("defaultProjectReady"),
                    "projectOwnerReady": status.get("projectOwnerReady"),
                    "projectAccessReady": status.get("projectAccessReady"),
                    "projectOwnerAuthUserId": status.get("projectOwnerAuthUserId"),
                    "defaultUniverseReady": status.get("defaultUniverseReady"),
                    "defaultWorldReady": status.get("defaultWorldReady"),
                    "blockRegistryReady": status.get("blockRegistryReady"),
                    "systemBlocksReady": status.get("systemBlocksReady"),
                    "airInvariantReady": status.get("airInvariantReady"),
                    "systemRailingReady": status.get("systemRailingReady"),
                },
            )
        )
    except Exception as exc:
        message = _safe_exception_message(exc)
        result.warnings.append(
            _make_message(
                code="post_status_failed",
                message=message,
                details={"exceptionType": exc.__class__.__name__},
            )
        )
        result.steps.append(
            _make_step(
                name="post_status",
                ok=False,
                status=STEP_STATUS_WARNING,
                message=message,
                started_at=started_at,
                data={"exceptionType": exc.__class__.__name__},
            )
        )


def _apply_post_status_readiness(result: DbBootstrapResult) -> None:
    """Apply post-status readiness and enforce access/system invariants."""
    post_status = result.post_status or {}
    if not isinstance(post_status, Mapping):
        return

    result.schema_ready = _safe_bool(post_status.get("schemaReady"), False)
    result.seed_ready = _safe_bool(post_status.get("seedReady"), False)
    result.default_project_ready = _safe_bool(
        post_status.get("defaultProjectReady"), False
    )
    result.project_owner_ready = _safe_bool(
        post_status.get("projectOwnerReady"), False
    )
    result.project_access_ready = _safe_bool(
        post_status.get("projectAccessReady"), False
    )
    result.canonical_project_access_ready = _safe_bool(
        post_status.get("canonicalProjectAccessReady"), False
    )
    result.legacy_project_access_ready = _safe_bool(
        post_status.get("legacyProjectAccessReady"), False
    )
    result.project_owner_auth_user_id = _safe_str(
        post_status.get("projectOwnerAuthUserId"), ""
    ) or None
    result.project_access_assignment_count = max(
        0, _safe_int(post_status.get("projectAccessAssignmentCount"), 0)
    )
    result.project_access_role_count = max(
        0, _safe_int(post_status.get("projectAccessRoleCount"), 0)
    )
    result.project_access = _safe_dict(post_status.get("projectAccess"))
    result.default_universe_ready = _safe_bool(
        post_status.get("defaultUniverseReady"), False
    )
    result.default_world_ready = _safe_bool(
        post_status.get("defaultWorldReady"), False
    )
    result.block_registry_ready = _safe_bool(
        post_status.get("blockRegistryReady"), False
    )
    result.debug_blocks_required = _safe_bool(
        post_status.get("debugBlocksRequired"), False
    )
    result.debug_blocks_ready = (
        _safe_bool(post_status.get("debugBlocksReady"), False)
        if post_status.get("debugBlocksReady") is not None
        else None
    )
    result.system_blocks_ready = _safe_bool(
        post_status.get("systemBlocksReady"), False
    )
    result.system_railing_ready = _safe_bool(
        post_status.get("systemRailingReady"), False
    )
    result.air_invariant_ready = _safe_bool(
        post_status.get("airInvariantReady"), False
    )
    result.system_block_count = max(
        0, _safe_int(post_status.get("systemBlockCount"), 0)
    )
    result.system_blocks_missing = max(
        0, _safe_int(post_status.get("systemBlocksMissing"), 0)
    )
    result.system_blocks_drifted = max(
        0, _safe_int(post_status.get("systemBlocksDrifted"), 0)
    )
    result.system_blocks = _safe_dict(post_status.get("systemBlocks"))

    if result.schema_bootstrap_requested and not result.schema_ready:
        result.schema_bootstrap_ok = False
        result.errors.append(
            _make_message(
                code="post_bootstrap_schema_not_ready",
                message="Schema is not ready after bootstrap.",
                details={"postStatus": dict(post_status)},
            )
        )

    if result.seed_bootstrap_requested:
        invariant_failures: list[tuple[str, str]] = []
        if not result.project_owner_ready:
            invariant_failures.append(
                (
                    "post_bootstrap_project_owner_not_ready",
                    "Default project canonical owner is not ready after bootstrap.",
                )
            )
        if not result.project_access_ready:
            invariant_failures.append(
                (
                    "post_bootstrap_project_access_not_ready",
                    "Default project access projection is not ready after bootstrap.",
                )
            )
        if not result.canonical_project_access_ready:
            invariant_failures.append(
                (
                    "post_bootstrap_canonical_access_not_ready",
                    "Canonical ProjectAccessAssignment projection is not ready.",
                )
            )
        if not result.legacy_project_access_ready:
            invariant_failures.append(
                (
                    "post_bootstrap_legacy_access_not_ready",
                    "Legacy role compatibility projection is not ready.",
                )
            )
        if not result.block_registry_ready:
            invariant_failures.append(
                (
                    "post_bootstrap_block_registry_not_ready",
                    "Default BlockRegistry is not ready after bootstrap.",
                )
            )
        if result.debug_blocks_required and not result.debug_blocks_ready:
            invariant_failures.append(
                (
                    "post_bootstrap_debug_blocks_not_ready",
                    "Debug blocks were requested but are not ready after bootstrap.",
                )
            )
        if not result.system_blocks_ready:
            invariant_failures.append(
                (
                    "post_bootstrap_system_blocks_not_ready",
                    "Built-in system blocks are not ready after bootstrap.",
                )
            )
        if not result.air_invariant_ready:
            invariant_failures.append(
                (
                    "post_bootstrap_air_invariant_not_ready",
                    "Air persistence invariant is not ready after bootstrap.",
                )
            )
        if not result.system_railing_ready:
            invariant_failures.append(
                (
                    "post_bootstrap_system_railing_not_ready",
                    "system_railing is missing, inactive, deleted or drifted.",
                )
            )

        for code, message in invariant_failures:
            result.errors.append(
                _make_message(
                    code=code,
                    message=message,
                    details={
                        "postStatus": dict(post_status),
                        "projectAccess": result.project_access,
                        "systemBlocks": result.system_blocks,
                    },
                )
            )
        if not result.seed_ready or invariant_failures:
            result.seed_bootstrap_ok = False
            result.errors.append(
                _make_message(
                    code="post_bootstrap_seed_not_ready",
                    message=(
                        "Seed owner/access/world/system invariant is not ready "
                        "after bootstrap."
                    ),
                    details={"postStatus": dict(post_status)},
                )
            )


def _finish_result(
    result: DbBootstrapResult,
    *,
    db_extension: Any = None,
) -> DbBootstrapResult:
    """Finalize result timestamps/status."""
    result.completed_at = _utc_now_iso()
    result.duration_ms = _duration_ms(result.started_at, result.completed_at)

    if result.errors:
        result.ok = False
        result.status = STATUS_FAILED
    elif result.status not in {STATUS_SKIPPED, STATUS_PARTIAL}:
        result.ok = True
        result.status = STATUS_COMPLETED

    _cleanup_db_session(rollback=False, db_extension=db_extension)

    return result


def _finish_or_raise(
    app: Any,
    result: DbBootstrapResult,
    fail_on_error: bool,
    *,
    db_extension: Any = None,
) -> DbBootstrapResult:
    """Finish result and optionally raise."""
    _finish_result(result, db_extension=db_extension)

    if fail_on_error and not result.ok:
        first_error = result.errors[0] if result.errors else {}
        message = _safe_str(
            first_error.get("message") if isinstance(first_error, Mapping) else None,
            "DB bootstrap failed.",
        )
        _safe_log_exception(app, "DB bootstrap failed: %s", message)
        raise RuntimeError(message)

    if not result.ok:
        _safe_log_warning(app, "DB bootstrap failed but fail_on_error=false.")

    return result


# -----------------------------------------------------------------------------
# Convenience APIs
# -----------------------------------------------------------------------------

def run_db_bootstrap_if_enabled(
    app: Flask,
    *,
    settings: BootstrapSettings | None = None,
    db_extension: Any = None,
) -> DbBootstrapResult:
    """Run DB bootstrap if enabled by settings."""
    return run_db_bootstrap(
        app,
        settings=settings,
        db_extension=db_extension,
    )


def db_bootstrap_result_to_dict(
    result: DbBootstrapResult | Mapping[str, Any] | Any,
) -> dict[str, Any]:
    """Serialize DB bootstrap result to dict."""
    if isinstance(result, DbBootstrapResult):
        return result.to_dict()

    if isinstance(result, Mapping):
        try:
            return dict(result)
        except Exception:
            return {}

    return _safe_dict(result)


def build_db_bootstrap_summary(
    result: DbBootstrapResult | Mapping[str, Any] | Any,
) -> dict[str, Any]:
    """Build compact DB-bootstrap summary."""
    data = db_bootstrap_result_to_dict(result)
    steps = data.get("steps") or []
    try:
        failed_steps = [step for step in steps if not bool(step.get("ok"))]
        skipped_steps = [step for step in steps if bool(step.get("skipped"))]
    except Exception:
        failed_steps = []
        skipped_steps = []

    return {
        "ok": bool(data.get("ok")),
        "status": _safe_str(data.get("status"), "unknown"),
        "enabled": bool(data.get("enabled")),
        "schemaReady": data.get("schema_ready"),
        "seedReady": data.get("seed_ready"),
        "defaultProjectReady": data.get("default_project_ready"),
        "projectOwnerReady": data.get("project_owner_ready"),
        "projectAccessReady": data.get("project_access_ready"),
        "canonicalProjectAccessReady": data.get(
            "canonical_project_access_ready"
        ),
        "legacyProjectAccessReady": data.get("legacy_project_access_ready"),
        "projectOwnerAuthUserId": data.get("project_owner_auth_user_id"),
        "projectAccessAssignmentCount": data.get(
            "project_access_assignment_count"
        ),
        "projectAccessRoleCount": data.get("project_access_role_count"),
        "defaultUniverseReady": data.get("default_universe_ready"),
        "defaultWorldReady": data.get("default_world_ready"),
        "blockRegistryReady": data.get("block_registry_ready"),
        "debugBlocksReady": data.get("debug_blocks_ready"),
        "debugBlocksRequired": data.get("debug_blocks_required"),
        "systemBlocksReady": data.get("system_blocks_ready"),
        "systemRailingReady": data.get("system_railing_ready"),
        "airInvariantReady": data.get("air_invariant_ready"),
        "systemBlockCount": data.get("system_block_count"),
        "systemBlocksCreated": data.get("system_blocks_created"),
        "systemBlocksUpdated": data.get("system_blocks_updated"),
        "systemBlocksMissing": data.get("system_blocks_missing"),
        "systemBlocksDrifted": data.get("system_blocks_drifted"),
        "schemaBootstrapRequested": bool(data.get("schema_bootstrap_requested")),
        "seedBootstrapRequested": bool(data.get("seed_bootstrap_requested")),
        "schemaBootstrapExecuted": bool(data.get("schema_bootstrap_executed")),
        "seedBootstrapExecuted": bool(data.get("seed_bootstrap_executed")),
        "seedInvariantRepairExecuted": bool(
            data.get("seed_invariant_repair_executed")
        ),
        "schemaBootstrapOk": data.get("schema_bootstrap_ok"),
        "seedBootstrapOk": data.get("seed_bootstrap_ok"),
        "seedInvariantRepairOk": data.get("seed_invariant_repair_ok"),
        "failOnError": bool(data.get("fail_on_error")),
        "stepCount": len(steps),
        "failedStepCount": len(failed_steps),
        "skippedStepCount": len(skipped_steps),
        "warningCount": len(data.get("warnings") or []),
        "errorCount": len(data.get("errors") or []),
        "durationMs": data.get("duration_ms"),
    }


def build_db_bootstrap_exit_code(
    result: DbBootstrapResult | Mapping[str, Any] | Any,
) -> int:
    """
    Return conventional process exit code for DB bootstrap result.

    0 = ok or skipped
    1 = failed
    """
    data = db_bootstrap_result_to_dict(result)
    return 0 if bool(data.get("ok")) else 1


# -----------------------------------------------------------------------------
# Public exports
# -----------------------------------------------------------------------------

__all__ = [
    "BLOCK_REGISTRY_ALLOWED_SOURCES",
    "DB_BOOTSTRAP_RESULT_VERSION",
    "DEFAULT_BLOCK_REGISTRY_SOURCE",
    "STATUS_COMPLETED",
    "STATUS_FAILED",
    "STATUS_PARTIAL",
    "STATUS_READY",
    "STATUS_SKIPPED",
    "STEP_DEFAULT_SEED",
    "STEP_DEFAULT_SEED_INVARIANT_REPAIR",
    "STEP_SCHEMA_BOOTSTRAP",
    "STEP_SCHEMA_STATUS_AFTER",
    "STEP_SCHEMA_STATUS_BEFORE",
    "STEP_SEED_STATUS_AFTER",
    "STEP_SEED_STATUS_BEFORE",
    "STEP_STATUS_FAILED",
    "STEP_STATUS_OK",
    "STEP_STATUS_SKIPPED",
    "STEP_STATUS_WARNING",
    "DbBootstrapMessage",
    "DbBootstrapResult",
    "DbBootstrapStep",
    "build_db_bootstrap_exit_code",
    "build_db_bootstrap_status",
    "build_db_bootstrap_summary",
    "build_default_project_access_projection_status",
    "build_default_world_invariant_status",
    "build_system_block_invariant_status",
    "clear_db_bootstrap_default_seed_caches",
    "clear_db_bootstrap_project_access_caches",
    "clear_db_bootstrap_system_block_caches",
    "db_bootstrap_result_to_dict",
    "get_effective_db_bootstrap_flags",
    "repair_default_world_invariant",
    "resolve_bootstrap_settings",
    "run_db_bootstrap",
    "run_db_bootstrap_if_enabled",
]
