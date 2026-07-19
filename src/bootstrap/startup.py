# services/vectoplan-chunk/src/bootstrap/startup.py
"""
Read-only runtime startup hooks for the `vectoplan-chunk` service.

This module is the controlled runtime-startup layer for the chunk service.

Responsibilities:
- create and maintain versioned startup state under
  app.extensions["vectoplan_chunk"]["startup"],
- collect compact app, routing and extension metadata,
- verify important service paths/files/routes through runtime_checks.py,
- verify model registry availability without loading product data,
- optionally perform a cheap DB connectivity check,
- merge runtime checks with read-only DB-bootstrap readiness,
- store canonical and legacy project-access readiness without raw user ids,
- verify the centrally registered project-access route surface,
- store compact settings/runtime-check/readiness summaries,
- expose compatibility helpers for existing status routes.

Important boundaries:
- no request handling here
- no chunk generation here
- no command execution here
- no editor UI logic here
- no migrations here
- no db.create_all() here
- no default seeding here
- no ChunkSnapshot loading here
- no ChunkEvent loading here
- no WorldCommandLog loading here
- no WorldObject/WorldObjectChunkRef loading here
- no recursive SQLAlchemy relationship serialization here
- no authorization enforcement here
- no caching of ORM rows, query results or database state here

Design rule:

    Runtime startup must be cheap, bounded and read-only.

Database schema creation and default seed data are handled by the explicit DB
bootstrap path:

    src/bootstrap/db_bootstrap.py
    scripts/bootstrap_db.py

This prevents Gunicorn worker startup from running DB mutations in parallel.
"""

from __future__ import annotations

import hashlib
import os
import re
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Final, Mapping

try:
    from flask import Flask
except Exception:  # pragma: no cover - partial import/test environments
    class Flask:  # type: ignore[no-redef]
        """Minimal typing/runtime placeholder when Flask is unavailable."""

try:
    from extensions import (
        get_extension_summary,
        init_extensions,
        mark_extension_failed,
        mark_extension_initialized,
        mark_extension_warning,
        register_extension,
    )
except Exception:  # pragma: no cover - partial import/test environments
    get_extension_summary = None  # type: ignore[assignment]
    init_extensions = None  # type: ignore[assignment]
    mark_extension_failed = None  # type: ignore[assignment]
    mark_extension_initialized = None  # type: ignore[assignment]
    mark_extension_warning = None  # type: ignore[assignment]
    register_extension = None  # type: ignore[assignment]

try:
    from .settings import (
        build_bootstrap_settings,
        build_settings_summary,
        is_startup_strict,
        should_run_create_all_in_runtime,
        should_run_seed_in_runtime,
        should_run_startup_hooks,
    )
except Exception:  # pragma: no cover - fallback for direct imports
    build_bootstrap_settings = None  # type: ignore[assignment]
    build_settings_summary = None  # type: ignore[assignment]

    def is_startup_strict(app: Any = None) -> bool:
        return _safe_bool(_safe_get_config(app, "VECTOPLAN_CHUNK_STARTUP_STRICT", False), False)

    def should_run_create_all_in_runtime(app: Any = None) -> bool:
        return False

    def should_run_seed_in_runtime(app: Any = None) -> bool:
        return False

    def should_run_startup_hooks(app: Any = None) -> bool:
        return _safe_bool(_safe_get_config(app, "VECTOPLAN_CHUNK_RUN_STARTUP_HOOKS", True), True)

try:
    from .db_bootstrap import build_db_bootstrap_status
except Exception:  # pragma: no cover - partial import/test environments
    build_db_bootstrap_status = None  # type: ignore[assignment]

try:
    from .runtime_checks import (
        FileCheckSpec,
        PathCheckSpec,
        RouteCheckSpec,
        build_runtime_checks_summary,
        get_default_file_check_spec_data,
        get_default_file_check_specs,
        get_default_path_check_spec_data,
        get_default_path_check_specs,
        get_default_route_check_spec_data,
        get_default_route_check_specs,
        log_runtime_checks_result,
        raise_if_runtime_checks_failed,
        run_runtime_checks,
        runtime_checks_result_to_dict,
    )
except Exception:  # pragma: no cover - fallback if runtime_checks is temporarily unavailable
    FileCheckSpec = Any  # type: ignore[misc, assignment]
    PathCheckSpec = Any  # type: ignore[misc, assignment]
    RouteCheckSpec = Any  # type: ignore[misc, assignment]
    build_runtime_checks_summary = None  # type: ignore[assignment]
    get_default_file_check_spec_data = None  # type: ignore[assignment]
    get_default_file_check_specs = None  # type: ignore[assignment]
    get_default_path_check_spec_data = None  # type: ignore[assignment]
    get_default_path_check_specs = None  # type: ignore[assignment]
    get_default_route_check_spec_data = None  # type: ignore[assignment]
    get_default_route_check_specs = None  # type: ignore[assignment]
    log_runtime_checks_result = None  # type: ignore[assignment]
    raise_if_runtime_checks_failed = None  # type: ignore[assignment]
    run_runtime_checks = None  # type: ignore[assignment]
    runtime_checks_result_to_dict = None  # type: ignore[assignment]


# -----------------------------------------------------------------------------
# Constants
# -----------------------------------------------------------------------------

CHUNK_NAMESPACE: Final[str] = "vectoplan_chunk"
LEGACY_EDITOR_NAMESPACE: Final[str] = "vectoplan_editor"
STARTUP_STATE_KEY: Final[str] = "startup"
STARTUP_STATE_VERSION: Final[str] = "startup-state.v3"
STARTUP_CONTRACT_VERSION: Final[str] = "runtime-startup.v3"

DEFAULT_SERVICE_NAME: Final[str] = "vectoplan-chunk"
DEFAULT_DISPLAY_NAME: Final[str] = "VECTOPLAN Chunk Service"
DEFAULT_ACCESS_SOURCE_SERVICE: Final[str] = "vectoplan-app"
DEFAULT_CANONICAL_USER_ID_FIELD: Final[str] = "auth_user_id"

STATUS_IDLE: Final[str] = "idle"
STATUS_RUNNING: Final[str] = "running"
STATUS_COMPLETED: Final[str] = "completed"
STATUS_SKIPPED: Final[str] = "skipped"
STATUS_FAILED: Final[str] = "failed"
STATUS_WARNING: Final[str] = "warning"

_TRUE_VALUES: Final[set[str]] = {"1", "true", "t", "yes", "y", "on", "enabled"}
_FALSE_VALUES: Final[set[str]] = {"0", "false", "f", "no", "n", "off", "disabled"}

PROJECT_ACCESS_BLUEPRINT_NAME: Final[str] = "project_access"

# Retained compatibility surface. New deployments may additionally expose the
# canonical assignment and owner-transfer routes below.
PROJECT_ACCESS_CORE_ROUTE_RULES: Final[tuple[str, ...]] = (
    "/project-access/_status",
    "/projects/<project_id>/access",
    "/projects/<project_id>/access/initialize",
    "/projects/<project_id>/roles",
    "/projects/<project_id>/groups",
    "/projects/<project_id>/assignments",
)

PROJECT_ACCESS_CANONICAL_ROUTE_RULES: Final[tuple[str, ...]] = (
    "/project-access/_status",
    "/projects/<project_id>/access",
    "/projects/<project_id>/access/initialize",
    "/projects/<project_id>/access/assignments",
    "/projects/<project_id>/access/transfer-owner",
)

PROJECT_ACCESS_LEGACY_ROUTE_RULES: Final[tuple[str, ...]] = (
    "/project-access/_status",
    "/projects/<project_id>/access",
    "/projects/<project_id>/access/initialize",
    "/projects/<project_id>/roles",
    "/projects/<project_id>/groups",
    "/projects/<project_id>/assignments",
)

PROJECT_ACCESS_REQUIRED_ROUTE_GROUPS: Final[
    tuple[tuple[str, tuple[str, ...]], ...]
] = (
    ("status", ("/project-access/_status",)),
    ("access", ("/projects/<project_id>/access",)),
    ("initialize", ("/projects/<project_id>/access/initialize",)),
    (
        "assignments",
        (
            "/projects/<project_id>/access/assignments",
            "/projects/<project_id>/assignments",
        ),
    ),
)

PROJECT_ACCESS_OWNER_TRANSFER_ROUTE_RULES: Final[tuple[str, ...]] = (
    "/projects/<project_id>/access/transfer-owner",
    "/projects/<project_id>/transfer-owner",
)


# -----------------------------------------------------------------------------
# Compatibility data structures
# -----------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class SeedOperationResult:
    """
    Compatibility seed operation result.

    Runtime startup no longer performs seeding. This class remains exported so
    old imports do not break while the seed logic lives in default_seed.py.
    """

    name: str
    ok: bool
    created: bool = False
    updated: bool = False
    skipped: bool = False
    message: str | None = None
    data: dict[str, Any] | None = None


# -----------------------------------------------------------------------------
# Primitive helpers
# -----------------------------------------------------------------------------

def _utc_now_iso() -> str:
    """Return UTC timestamp as ISO string."""
    try:
        return datetime.now(timezone.utc).isoformat()
    except Exception:
        return "1970-01-01T00:00:00+00:00"


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


def _safe_log_exception(app: Any, message: str, *args: Any) -> None:
    """Exception-log defensively."""
    try:
        app.logger.exception(message, *args)
    except Exception:
        pass


def _safe_get_config(app: Any, key: str, default: Any = None) -> Any:
    """Read config value defensively."""
    if app is None:
        return default

    try:
        config = getattr(app, "config", None)
    except Exception:
        return default

    if config is None:
        return default

    try:
        if hasattr(config, "get"):
            return config.get(key, default)
    except Exception:
        pass

    try:
        return config[key]
    except Exception:
        return default


def _safe_get_env(key: str, default: Any = None) -> Any:
    """Read environment variable defensively."""
    try:
        value = os.getenv(key)
    except Exception:
        return default

    if value is None:
        return default

    return value


def _safe_config_or_env(app: Any, key: str, default: Any = None) -> Any:
    """Read environment first, then Flask config."""
    value = _safe_get_env(key, None)
    if value is not None:
        return value

    return _safe_get_config(app, key, default)


def _safe_str(value: Any, default: str = "") -> str:
    """Normalize any value to string."""
    if value is None:
        return default

    try:
        normalized = str(value).strip()
    except Exception:
        return default

    return normalized or default


def _safe_bool(value: Any, default: bool = False) -> bool:
    """Normalize bool-like values."""
    if isinstance(value, bool):
        return value

    if isinstance(value, int):
        return bool(value)

    text = _safe_str(value, "")
    if not text:
        return default

    lowered = text.lower()

    if lowered in _TRUE_VALUES:
        return True

    if lowered in _FALSE_VALUES:
        return False

    return default


def _safe_int(
    value: Any,
    default: int = 0,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    """Normalize integer values."""
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


def _sanitize_message_text(value: Any) -> str:
    """Redact common credentials from free-form diagnostic messages."""
    text = _safe_str(value, "")
    if not text:
        return ""

    patterns = (
        (
            r"([a-zA-Z][a-zA-Z0-9+.-]*://[^\s:/@]+:)[^\s/@]+@",
            r"\1***@",
        ),
        (r"(?i)\bBearer\s+[^\s,;]+", "Bearer <redacted>"),
        (
            r"(?i)\b(api[_-]?key|token|password|secret|credential)"
            r"(\s*[:=]\s*)[^\s,;]+",
            r"\1\2<redacted>",
        ),
    )
    for pattern, replacement in patterns:
        try:
            text = re.sub(pattern, replacement, text)
        except Exception:
            continue
    return text[:2000]


def _safe_exception_message(exc: BaseException | Any) -> str:
    """Return a robust, credential-redacted exception message."""
    try:
        message = str(exc)
    except Exception:
        message = exc.__class__.__name__

    return _sanitize_message_text(message) or exc.__class__.__name__


def _safe_deepcopy(value: Any) -> Any:
    """Deep-copy defensively."""
    try:
        return deepcopy(value)
    except Exception:
        return value


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


def _safe_list(value: Any) -> list[Any]:
    """Normalize sequence-like values to a new list without consuming mappings."""
    if value is None:
        return []
    if isinstance(value, list):
        return list(value)
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, set):
        try:
            return sorted(value, key=lambda item: _safe_str(item, ""))
        except Exception:
            return list(value)
    if isinstance(value, (str, bytes, bytearray, Mapping)):
        return []
    try:
        return list(value)
    except Exception:
        return []


def _safe_optional_bool(value: Any) -> bool | None:
    """Return bool for explicit values and None for absent/unknown values."""
    if value is None:
        return None
    return _safe_bool(value, False)


def _first_mapping_value(
    payload: Mapping[str, Any] | None,
    *names: str,
    default: Any = None,
) -> Any:
    """Return the first explicitly present mapping value."""
    mapping = _safe_dict(payload)
    for name in names:
        try:
            if name in mapping:
                return mapping.get(name)
        except Exception:
            continue
    return default


def _identity_fingerprint(value: Any) -> str | None:
    """Return a stable non-reversible identity fingerprint for public diagnostics."""
    text = _safe_str(value, "")
    if not text:
        return None
    try:
        digest = hashlib.sha256(text.encode("utf-8", errors="strict")).hexdigest()
    except Exception:
        return None
    return f"sha256:{digest[:16]}"


def _is_canonical_auth_user_id(value: Any) -> bool:
    """Reject local numeric ids, e-mail addresses and placeholder identities."""
    text = _safe_str(value, "")
    if not text or len(text) > 256:
        return False
    if text.isdecimal() or "@" in text or "://" in text:
        return False
    if any(character.isspace() or ord(character) < 32 for character in text):
        return False
    if text.lower() in {
        "1",
        "0",
        "bootstrap",
        "system",
        "anonymous",
        "guest",
        "none",
        "null",
        "unknown",
    }:
        return False
    return True


def _private_identifiers_enabled(app: Any) -> bool:
    """Allow raw identifiers only behind an explicit diagnostic opt-in."""
    return _safe_bool(
        _safe_config_or_env(
            app,
            "VECTOPLAN_CHUNK_STARTUP_INCLUDE_PRIVATE_IDENTIFIERS",
            False,
        ),
        False,
    )


def _project_access_required(app: Any) -> bool:
    """Return whether the synchronized project-access projection is mandatory."""
    return _safe_bool(
        _safe_config_or_env(
            app,
            "VECTOPLAN_CHUNK_PROJECT_ACCESS_REQUIRED",
            _safe_config_or_env(
                app,
                "VECTOPLAN_CHUNK_ACCESS_CONTROL_ENABLED",
                True,
            ),
        ),
        True,
    )


def _access_authz_enforcement_required(app: Any) -> bool:
    """Require route-level project authorization only after explicit rollout."""
    return _safe_bool(
        _safe_config_or_env(
            app,
            "VECTOPLAN_CHUNK_REQUIRE_PROJECT_ACCESS_AUTHZ_ENFORCEMENT",
            False,
        ),
        False,
    )


def _bootstrap_readiness_required(app: Any) -> bool:
    """Return whether runtime startup must require explicit bootstrap readiness."""
    return _safe_bool(
        _safe_config_or_env(
            app,
            "VECTOPLAN_CHUNK_STARTUP_REQUIRE_BOOTSTRAP_READINESS",
            True,
        ),
        True,
    )


def _debug_blocks_required(app: Any) -> bool:
    """Require debug blocks only when explicitly configured as a runtime invariant."""
    explicit = _safe_config_or_env(
        app,
        "VECTOPLAN_CHUNK_REQUIRE_DEBUG_BLOCKS",
        None,
    )
    if explicit is not None:
        return _safe_bool(explicit, False)

    return _safe_bool(
        _safe_config_or_env(
            app,
            "VECTOPLAN_CHUNK_DB_BOOTSTRAP_SEED_DEBUG_BLOCKS",
            _safe_config_or_env(
                app,
                "VECTOPLAN_CHUNK_SEED_DEBUG_BLOCKS",
                False,
            ),
        ),
        False,
    )


def _owner_transfer_route_required(app: Any) -> bool:
    """Require the dedicated owner-transfer route only after explicit rollout."""
    return _safe_bool(
        _safe_config_or_env(
            app,
            "VECTOPLAN_CHUNK_REQUIRE_PROJECT_OWNER_TRANSFER_ROUTE",
            False,
        ),
        False,
    )


def _normalize_api_prefix(app: Any) -> str:
    """Normalize an optional API prefix used by route-surface checks."""
    prefix = _safe_str(
        _safe_config_or_env(app, "VECTOPLAN_CHUNK_API_PREFIX", ""),
        "",
    )
    if not prefix or prefix == "/":
        return ""
    return "/" + prefix.strip("/")


def _normalize_route_template(rule: Any) -> str:
    """Normalize Flask converter syntax and trailing slashes for comparison."""
    text = "/" + _safe_str(rule, "").lstrip("/")
    try:
        text = re.sub(r"<[^:<>]+:([^<>]+)>", r"<\1>", text)
    except Exception:
        pass
    if len(text) > 1:
        text = text.rstrip("/")
    return text


def _route_variants(app: Any, rule: str) -> tuple[str, ...]:
    """Return unprefixed and configured-prefix variants for a Flask rule."""
    normalized_rule = _normalize_route_template(rule)
    prefix = _normalize_api_prefix(app)
    variants = [normalized_rule]
    if prefix:
        variants.append(prefix + normalized_rule)
    return tuple(dict.fromkeys(variants))


def _route_registered(app: Any, rule_set: set[str], rule: str) -> bool:
    """Return whether any accepted variant of a route is registered."""
    return any(candidate in rule_set for candidate in _route_variants(app, rule))


def _normalize_project_access_status(
    app: Any,
    payload: Mapping[str, Any] | None,
    bootstrap_status: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Normalize legacy and canonical access status without exposing raw identities."""
    merged: dict[str, Any] = {}

    bootstrap = _safe_dict(bootstrap_status)
    bootstrap_access = _safe_dict(bootstrap.get("projectAccess"))
    merged.update(bootstrap_access)

    source = _safe_dict(payload)
    merged.update(source)

    owner_value = _first_mapping_value(
        merged,
        "ownerAuthUserId",
        "projectOwnerAuthUserId",
        "ownerUserId",
        "owner_user_id",
        default=_first_mapping_value(
            bootstrap,
            "projectOwnerAuthUserId",
            "ownerAuthUserId",
            "ownerUserId",
        ),
    )

    owner_ready = _safe_optional_bool(
        _first_mapping_value(
            merged,
            "projectOwnerReady",
            "ownerReady",
            "defaultProjectOwnerReady",
            default=_first_mapping_value(bootstrap, "projectOwnerReady"),
        )
    )
    canonical_ready = _safe_optional_bool(
        _first_mapping_value(
            merged,
            "canonicalProjectAccessReady",
            "canonicalReady",
            "projectionReady",
            default=_first_mapping_value(
                bootstrap,
                "canonicalProjectAccessReady",
            ),
        )
    )
    legacy_ready = _safe_optional_bool(
        _first_mapping_value(
            merged,
            "legacyProjectAccessReady",
            "legacyReady",
            default=_first_mapping_value(
                bootstrap,
                "legacyProjectAccessReady",
            ),
        )
    )
    roles_ready = _safe_optional_bool(
        _first_mapping_value(merged, "rolesReady", "projectRolesReady")
    )
    owner_assignment_ready = _safe_optional_bool(
        _first_mapping_value(
            merged,
            "ownerAssignmentReady",
            "projectOwnerAssignmentReady",
        )
    )
    if (
        owner_assignment_ready is None
        and canonical_ready is not None
        and legacy_ready is not None
    ):
        owner_assignment_ready = bool(canonical_ready and legacy_ready)

    explicit_ready = _safe_optional_bool(
        _first_mapping_value(
            merged,
            "projectAccessReady",
            "accessReady",
            "ready",
            "ok",
            default=_first_mapping_value(bootstrap, "projectAccessReady"),
        )
    )

    if explicit_ready is None:
        required_parts = [
            value
            for value in (
                owner_ready,
                canonical_ready,
                legacy_ready,
                roles_ready,
                owner_assignment_ready,
            )
            if value is not None
        ]
        access_ready = all(required_parts) if required_parts else None
    else:
        access_ready = explicit_ready

    expected_source = _safe_str(
        _safe_config_or_env(
            app,
            "VECTOPLAN_CHUNK_PROJECT_ACCESS_SOURCE_SERVICE",
            DEFAULT_ACCESS_SOURCE_SERVICE,
        ),
        DEFAULT_ACCESS_SOURCE_SERVICE,
    ).lower()
    actual_source = _safe_str(
        _first_mapping_value(
            merged,
            "sourceOfTruth",
            "sourceService",
            "source_service",
            default=expected_source,
        ),
        expected_source,
    ).lower()
    source_ready = actual_source == expected_source

    viewer_read_only = _safe_optional_bool(
        _first_mapping_value(
            merged,
            "viewerReadOnly",
            "viewer_read_only",
        )
    )
    authz_enforced = _safe_optional_bool(
        _first_mapping_value(
            merged,
            "authzEnforced",
            "authorizationEnforced",
            "enforced",
        )
    )

    counts = _safe_dict(merged.get("counts"))
    canonical_assignment_count = _safe_int(
        _first_mapping_value(
            merged,
            "projectAccessAssignmentCount",
            "canonicalAssignmentCount",
            default=_first_mapping_value(
                counts,
                "canonicalAssignments",
                "assignments",
                default=_first_mapping_value(
                    bootstrap,
                    "projectAccessAssignmentCount",
                    default=0,
                ),
            ),
        ),
        0,
        minimum=0,
    )
    legacy_role_count = _safe_int(
        _first_mapping_value(
            merged,
            "projectAccessRoleCount",
            "legacyRoleCount",
            default=_first_mapping_value(
                counts,
                "legacyRoles",
                "roles",
                default=_first_mapping_value(
                    bootstrap,
                    "projectAccessRoleCount",
                    default=0,
                ),
            ),
        ),
        0,
        minimum=0,
    )

    owner_fingerprint = _identity_fingerprint(owner_value)
    expose_private = _private_identifiers_enabled(app)

    checked = _safe_bool(
        _first_mapping_value(
            merged,
            "checked",
            default=bool(source or bootstrap_access or bootstrap),
        ),
        bool(source or bootstrap_access or bootstrap),
    )
    if viewer_read_only is None and checked:
        viewer_read_only = _safe_bool(
            _safe_config_or_env(
                app,
                "VECTOPLAN_CHUNK_VIEWER_READ_ONLY",
                True,
            ),
            True,
        )
    if authz_enforced is None and checked:
        authz_enforced = _safe_optional_bool(
            _safe_config_or_env(
                app,
                "VECTOPLAN_CHUNK_PROJECT_ACCESS_AUTHZ_ENFORCED",
                None,
            )
        )

    required = _safe_bool(
        _first_mapping_value(
            merged,
            "required",
            default=_project_access_required(app),
        ),
        _project_access_required(app),
    )

    return {
        "checked": checked,
        "required": required,
        "ready": access_ready,
        "accessReady": access_ready,
        "projectId": _first_mapping_value(
            merged,
            "projectId",
            "project_id",
            default=_first_mapping_value(bootstrap, "defaultProjectId"),
        ),
        "ownerUserId": owner_value if expose_private else None,
        "ownerAuthUserId": owner_value if expose_private else None,
        "ownerIdFingerprint": owner_fingerprint,
        "ownerIdentityCanonical": (
            _is_canonical_auth_user_id(owner_value)
            if owner_value is not None
            else None
        ),
        "identityExposed": bool(expose_private and owner_value),
        "canonicalUserIdField": DEFAULT_CANONICAL_USER_ID_FIELD,
        "ownerReady": owner_ready,
        "canonicalReady": canonical_ready,
        "legacyReady": legacy_ready,
        "rolesReady": roles_ready if roles_ready is not None else legacy_ready,
        "ownerAssignmentReady": owner_assignment_ready,
        "canonicalAssignmentCount": canonical_assignment_count,
        "legacyRoleCount": legacy_role_count,
        "sourceOfTruth": actual_source,
        "expectedSourceOfTruth": expected_source,
        "sourceOfTruthReady": source_ready,
        "viewerReadOnly": viewer_read_only,
        "authzEnforced": authz_enforced,
        "status": _first_mapping_value(merged, "status"),
        "errors": _safe_list(merged.get("errors")),
    }


def _build_bootstrap_readiness_snapshot(app: Any) -> dict[str, Any]:
    """Collect the bounded read-only DB-bootstrap status used by runtime startup."""
    if not callable(build_db_bootstrap_status):
        return {
            "checked": False,
            "ok": None,
            "status": "unavailable",
            "error": "build_db_bootstrap_status is unavailable.",
        }

    try:
        payload = build_db_bootstrap_status(app)
        data = _safe_dict(payload)
        data["checked"] = True
        return data
    except Exception as exc:
        return {
            "checked": True,
            "ok": False,
            "status": "failed",
            "error": _safe_exception_message(exc),
            "exceptionType": exc.__class__.__name__,
        }


def _bootstrap_readiness_failures(
    app: Any,
    bootstrap_status: Mapping[str, Any] | None,
    access_status: Mapping[str, Any] | None,
) -> list[str]:
    """Return stable failure keys for mandatory runtime bootstrap readiness."""
    status = _safe_dict(bootstrap_status)
    access = _safe_dict(access_status)

    if not _bootstrap_readiness_required(app):
        return []

    if not _safe_bool(status.get("checked"), False):
        return ["bootstrapStatusUnavailable"]

    failures: list[str] = []

    for key, label in (
        ("schemaReady", "schema"),
        ("defaultProjectReady", "defaultProject"),
        ("projectOwnerReady", "projectOwner"),
        ("defaultUniverseReady", "defaultUniverse"),
        ("defaultWorldReady", "defaultWorld"),
        ("blockRegistryReady", "blockRegistry"),
        ("systemBlocksReady", "systemBlocks"),
        ("airInvariantReady", "airInvariant"),
        ("systemRailingReady", "systemRailing"),
    ):
        if not _safe_bool(status.get(key), False):
            failures.append(label)

    if _project_access_required(app):
        if not _safe_bool(access.get("ready"), False):
            failures.append("projectAccess")
        if access.get("ownerIdentityCanonical") is False:
            failures.append("projectOwnerIdentity")
        if access.get("canonicalReady") is False:
            failures.append("canonicalProjectAccess")
        if access.get("legacyReady") is False:
            failures.append("legacyProjectAccess")
        if access.get("sourceOfTruthReady") is False:
            failures.append("projectAccessSourceOfTruth")
        if access.get("viewerReadOnly") is False:
            failures.append("viewerReadOnly")
        if (
            _access_authz_enforcement_required(app)
            and access.get("authzEnforced") is not True
        ):
            failures.append("projectAccessAuthzEnforcement")

    debug_required = bool(
        _debug_blocks_required(app)
        or _safe_bool(status.get("debugBlocksRequired"), False)
    )
    if debug_required and not _safe_bool(status.get("debugBlocksReady"), False):
        failures.append("debugBlocks")

    return failures


def _runtime_effective_ok(
    app: Any,
    runtime_data: Mapping[str, Any] | None,
    bootstrap_status: Mapping[str, Any] | None,
    access_status: Mapping[str, Any] | None,
) -> tuple[bool | None, list[str]]:
    """Combine runtime checks with mandatory bootstrap/access readiness."""
    runtime = _safe_dict(runtime_data)
    raw_runtime_ok = _safe_optional_bool(runtime.get("ok"))
    failures = _bootstrap_readiness_failures(
        app,
        bootstrap_status,
        access_status,
    )

    if raw_runtime_ok is False:
        return False, failures
    if failures:
        return False, failures
    if raw_runtime_ok is None:
        return True if _safe_bool(_safe_dict(bootstrap_status).get("checked"), False) else None, failures
    return True, failures


def _diagnostic_key_is_secret(key: str) -> bool:
    """Return whether a diagnostic mapping key may contain a secret."""
    normalized = _safe_str(key, "").lower().replace("-", "_")
    return any(
        marker in normalized
        for marker in (
            "authorization",
            "password",
            "secret",
            "token",
            "api_key",
            "apikey",
            "credential",
            "cookie",
            "private_key",
            "session",
        )
    )


def _diagnostic_key_is_identity(key: str) -> bool:
    """Return whether a mapping key contains a user/owner identity value."""
    normalized = _safe_str(key, "").lower().replace("-", "_")
    return normalized in {
        "auth_user_id",
        "authuserid",
        "owner_user_id",
        "owneruserid",
        "owner_auth_user_id",
        "ownerauthuserid",
        "project_owner_auth_user_id",
        "projectownerauthuserid",
        "user_id",
        "userid",
        "account_id",
        "accountid",
    }


def _sanitize_diagnostic_value(
    value: Any,
    *,
    key: str = "",
    depth: int = 0,
    max_depth: int = 8,
) -> Any:
    """Build a bounded JSON-safe public diagnostic projection."""
    if depth > max_depth:
        return "<max-depth>"

    if _diagnostic_key_is_secret(key):
        return "<redacted>"

    if _diagnostic_key_is_identity(key):
        if value in (None, ""):
            return None
        return _identity_fingerprint(value)

    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            safe_key = _safe_str(raw_key, "")
            if not safe_key:
                continue
            result[safe_key] = _sanitize_diagnostic_value(
                raw_value,
                key=safe_key,
                depth=depth + 1,
                max_depth=max_depth,
            )
        return result

    if isinstance(value, (list, tuple, set)):
        items = _safe_list(value)
        return [
            _sanitize_diagnostic_value(
                item,
                depth=depth + 1,
                max_depth=max_depth,
            )
            for item in items[:500]
        ]

    if isinstance(value, (str, int, float, bool)) or value is None:
        return value

    try:
        return _safe_str(value, value.__class__.__name__)
    except Exception:
        return value.__class__.__name__


def _is_flask_app(app: object) -> bool:
    """Check whether object can be treated as Flask app."""
    if isinstance(app, Flask):
        return True

    required_attributes = ("extensions", "config", "logger", "url_map")
    try:
        return all(hasattr(app, attr_name) for attr_name in required_attributes)
    except Exception:
        return False


def _message_to_state_item(message: str, code: str = "startup_message", details: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build serializable state warning/error item."""
    return {
        "code": _safe_str(code, "startup_message"),
        "message": _sanitize_message_text(message),
        "timestamp": _utc_now_iso(),
        "details": _safe_dict(
            _sanitize_diagnostic_value(details or {})
        ),
    }


# -----------------------------------------------------------------------------
# Namespace / startup state
# -----------------------------------------------------------------------------

def _ensure_chunk_namespace(app: Flask) -> dict[str, Any]:
    """Ensure app.extensions['vectoplan_chunk'] exists."""
    if not _is_flask_app(app):
        raise TypeError("Startup hooks expect a Flask app or compatible object.")

    try:
        if not isinstance(app.extensions, dict):
            raise TypeError("app.extensions is not a dictionary.")
    except Exception as exc:
        raise RuntimeError("The Flask app has no usable extensions container.") from exc

    try:
        namespace = app.extensions.setdefault(CHUNK_NAMESPACE, {})
    except Exception as exc:
        raise RuntimeError("Could not create vectoplan_chunk namespace.") from exc

    if not isinstance(namespace, dict):
        raise RuntimeError(f"app.extensions['{CHUNK_NAMESPACE}'] is not a dictionary.")

    try:
        app.extensions.setdefault(LEGACY_EDITOR_NAMESPACE, namespace)
    except Exception:
        pass

    namespace.setdefault("namespace", CHUNK_NAMESPACE)
    namespace.setdefault("legacy_namespace", LEGACY_EDITOR_NAMESPACE)
    namespace.setdefault("service_name", _safe_get_config(app, "SERVICE_NAME", DEFAULT_SERVICE_NAME))

    return namespace


def _build_empty_readiness_state() -> dict[str, Any]:
    """Return stable readiness keys used by startup and status consumers."""
    return {
        "startupReady": None,
        "runtimeChecksReady": None,
        "runtimeChecksRawReady": None,
        "bootstrapReadinessChecked": False,
        "bootstrapReadinessReady": None,
        "bootstrapReadinessFailures": [],
        "databaseReady": None,
        "modelsReady": None,
        "schemaChecked": False,
        "schemaReady": None,
        "projectOwnerColumnsReady": None,
        "projectAccessSchemaReady": None,
        "projectAccessChecked": False,
        "defaultProjectOwnerReady": None,
        "defaultProjectOwnerIdentityCanonical": None,
        "defaultProjectOwnerFingerprint": None,
        "canonicalProjectAccessReady": None,
        "legacyProjectAccessReady": None,
        "defaultProjectRolesReady": None,
        "defaultProjectOwnerAssignmentReady": None,
        "defaultProjectAccessReady": None,
        "projectAccessSourceOfTruthReady": None,
        "projectAccessViewerReadOnly": None,
        "projectAccessRouteSurfaceReady": None,
        "projectAccessCanonicalRouteSurfaceReady": None,
        "projectAccessLegacyRouteSurfaceReady": None,
        "projectOwnerTransferRouteReady": None,
        "projectAccessApiEnabled": True,
        "projectAccessRoutesRequired": True,
        "projectAccessAuthzEnforced": None,
    }


def _build_empty_project_access_state() -> dict[str, Any]:
    """Return the public, identity-safe project-access startup placeholder."""
    return {
        "checked": False,
        "ready": None,
        "accessReady": None,
        "required": _project_access_required(None),
        "projectId": None,
        "ownerUserId": None,
        "ownerAuthUserId": None,
        "ownerIdFingerprint": None,
        "ownerIdentityCanonical": None,
        "identityExposed": False,
        "canonicalUserIdField": DEFAULT_CANONICAL_USER_ID_FIELD,
        "ownerReady": None,
        "canonicalReady": None,
        "legacyReady": None,
        "rolesReady": None,
        "ownerAssignmentReady": None,
        "canonicalAssignmentCount": 0,
        "legacyRoleCount": 0,
        "sourceOfTruth": DEFAULT_ACCESS_SOURCE_SERVICE,
        "expectedSourceOfTruth": DEFAULT_ACCESS_SOURCE_SERVICE,
        "sourceOfTruthReady": None,
        "viewerReadOnly": None,
        "authzEnforced": None,
        "status": None,
        "errors": [],
    }


def _build_empty_routing_state() -> dict[str, Any]:
    """Return a compact routing-state placeholder."""
    required_groups = {
        name: list(rules)
        for name, rules in PROJECT_ACCESS_REQUIRED_ROUTE_GROUPS
    }
    return {
        "routingInitialized": False,
        "registeredBlueprintNames": [],
        "routeCount": 0,
        "rules": [],
        "projectAccess": {
            "enabled": True,
            "required": True,
            "blueprintRegistered": False,
            "routeSurfaceReady": False,
            "canonicalRouteSurfaceReady": False,
            "legacyRouteSurfaceReady": False,
            "ownerTransferRouteRequired": False,
            "ownerTransferRouteReady": False,
            "coreRouteRules": list(PROJECT_ACCESS_CORE_ROUTE_RULES),
            "canonicalRouteRules": list(PROJECT_ACCESS_CANONICAL_ROUTE_RULES),
            "legacyRouteRules": list(PROJECT_ACCESS_LEGACY_ROUTE_RULES),
            "requiredRouteGroups": required_groups,
            "missingRouteGroups": list(required_groups),
            "missingRouteRules": list(PROJECT_ACCESS_CORE_ROUTE_RULES),
            "missingCanonicalRouteRules": list(
                PROJECT_ACCESS_CANONICAL_ROUTE_RULES
            ),
            "missingLegacyRouteRules": list(PROJECT_ACCESS_LEGACY_ROUTE_RULES),
            "authzEnforced": None,
        },
    }


def _get_route_rules(app: Flask) -> list[str]:
    """Collect current Flask rules without invoking any route handler."""
    try:
        return sorted({str(rule.rule) for rule in app.url_map.iter_rules()})
    except Exception:
        return []


def _get_blueprint_names(app: Flask) -> list[str]:
    """Collect registered Blueprint names from Flask's authoritative registry."""
    try:
        blueprints = getattr(app, "blueprints", {})
        if isinstance(blueprints, Mapping):
            return sorted(_safe_str(name, "") for name in blueprints if _safe_str(name, ""))
    except Exception:
        pass
    return []


def _read_central_routing_state(app: Flask) -> dict[str, Any]:
    """Read the central route registry metadata without importing route modules."""
    try:
        namespace = _ensure_chunk_namespace(app)
        routing = namespace.get("routing")
        if isinstance(routing, Mapping):
            return _safe_dict(_sanitize_diagnostic_value(routing))
    except Exception:
        pass
    return {}


def _project_access_routes_enabled(app: Flask, routing: Mapping[str, Any]) -> bool:
    value = routing.get("projectAccessApiEnabled")
    if value is not None:
        return _safe_bool(value, True)
    return _safe_bool(
        _safe_config_or_env(app, "VECTOPLAN_CHUNK_ENABLE_PROJECT_ACCESS_ROUTES", True),
        True,
    )


def _project_access_routes_required(
    app: Flask,
    routing: Mapping[str, Any],
    *,
    enabled: bool,
) -> bool:
    if not enabled:
        return False
    value = routing.get("projectAccessRoutesRequired")
    if value is not None:
        return _safe_bool(value, True)
    return _safe_bool(
        _safe_config_or_env(app, "VECTOPLAN_CHUNK_REQUIRE_PROJECT_ACCESS_ROUTES", True),
        True,
    )


def _build_routing_snapshot(app: Flask) -> dict[str, Any]:
    """Build bounded, JSON-safe routing and project-access route diagnostics."""
    central = _read_central_routing_state(app)
    rules = _get_route_rules(app)
    rule_set = {_normalize_route_template(rule) for rule in rules}
    blueprint_names = _get_blueprint_names(app)

    enabled = _project_access_routes_enabled(app, central)
    required = _project_access_routes_required(app, central, enabled=enabled)
    owner_transfer_required = _owner_transfer_route_required(app)
    blueprint_registered = any(
        name == PROJECT_ACCESS_BLUEPRINT_NAME
        or name.endswith("." + PROJECT_ACCESS_BLUEPRINT_NAME)
        for name in blueprint_names
    )

    missing_groups: list[str] = []
    required_groups: dict[str, list[str]] = {}
    for group_name, candidates in PROJECT_ACCESS_REQUIRED_ROUTE_GROUPS:
        required_groups[group_name] = list(candidates)
        if not any(
            _route_registered(app, rule_set, candidate)
            for candidate in candidates
        ):
            missing_groups.append(group_name)

    missing_core = [
        rule
        for rule in PROJECT_ACCESS_CORE_ROUTE_RULES
        if not _route_registered(app, rule_set, rule)
    ]
    missing_canonical = [
        rule
        for rule in PROJECT_ACCESS_CANONICAL_ROUTE_RULES
        if not _route_registered(app, rule_set, rule)
    ]
    missing_legacy = [
        rule
        for rule in PROJECT_ACCESS_LEGACY_ROUTE_RULES
        if not _route_registered(app, rule_set, rule)
    ]

    owner_transfer_ready = any(
        _route_registered(app, rule_set, rule)
        for rule in PROJECT_ACCESS_OWNER_TRANSFER_ROUTE_RULES
    )
    route_surface_ready = bool(
        not enabled
        or (
            blueprint_registered
            and not missing_groups
            and (owner_transfer_ready or not owner_transfer_required)
        )
    )
    canonical_surface_ready = bool(
        blueprint_registered and not missing_canonical
    )
    legacy_surface_ready = bool(blueprint_registered and not missing_legacy)

    central_project_access = _safe_dict(central.get("projectAccess"))
    routing_initialized = _safe_bool(
        central.get("routingInitialized"),
        bool(blueprint_names),
    )
    authz_enforced = _safe_optional_bool(
        _first_mapping_value(
            central_project_access,
            "authzEnforced",
            "authorizationEnforced",
            default=_first_mapping_value(
                central,
                "projectAccessAuthzEnforced",
            ),
        )
    )

    return {
        "routingInitialized": routing_initialized,
        "routesRegistryVersion": central.get("routesRegistryVersion"),
        "registeredBlueprintNames": blueprint_names,
        "routeCount": len(rules),
        "rules": rules,
        "apiPrefix": _normalize_api_prefix(app),
        "registrationErrorCount": _safe_int(
            central.get("blueprintRegistrationErrorCount"),
            len(_safe_list(central.get("errors"))),
            minimum=0,
        ),
        "registrationSuccessCount": _safe_int(
            central.get("blueprintRegistrationSuccessCount"),
            len(_safe_list(central.get("successes"))),
            minimum=0,
        ),
        "registrationSkippedCount": _safe_int(
            central.get("blueprintRegistrationSkippedCount"),
            len(_safe_list(central.get("skipped"))),
            minimum=0,
        ),
        "projectAccess": {
            "enabled": enabled,
            "required": required,
            "blueprintRegistered": blueprint_registered,
            "routeSurfaceReady": route_surface_ready,
            "canonicalRouteSurfaceReady": canonical_surface_ready,
            "legacyRouteSurfaceReady": legacy_surface_ready,
            "ownerTransferRouteRequired": owner_transfer_required,
            "ownerTransferRouteReady": owner_transfer_ready,
            "coreRouteRules": list(PROJECT_ACCESS_CORE_ROUTE_RULES),
            "canonicalRouteRules": list(
                PROJECT_ACCESS_CANONICAL_ROUTE_RULES
            ),
            "legacyRouteRules": list(PROJECT_ACCESS_LEGACY_ROUTE_RULES),
            "requiredRouteGroups": required_groups,
            "missingRouteGroups": missing_groups,
            "missingRouteRules": missing_core,
            "missingCanonicalRouteRules": missing_canonical,
            "missingLegacyRouteRules": missing_legacy,
            "authzEnforced": authz_enforced,
            "registryStatus": central_project_access,
        },
    }


def _derive_readiness_state(
    runtime_data: Mapping[str, Any] | None,
    routing_snapshot: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Normalize runtime, bootstrap, access and route results into one view."""
    runtime = _safe_dict(runtime_data)
    routing = _safe_dict(routing_snapshot)
    database = _safe_dict(runtime.get("database"))
    models = _safe_dict(runtime.get("models"))
    schema = _safe_dict(runtime.get("schema"))
    access = _safe_dict(
        runtime.get("project_access") or runtime.get("projectAccess")
    )
    bootstrap_status = _safe_dict(runtime.get("bootstrap_readiness"))
    route_access = _safe_dict(routing.get("projectAccess"))

    schema_checked = _safe_bool(
        schema.get("checked"),
        bool(schema),
    )
    access_checked = _safe_bool(access.get("checked"), False)
    access_ready = _safe_optional_bool(
        _first_mapping_value(
            access,
            "accessReady",
            "ready",
            "projectAccessReady",
        )
    )
    route_ready = route_access.get("routeSurfaceReady")
    routes_enabled = _safe_bool(route_access.get("enabled"), True)
    routes_required = _safe_bool(route_access.get("required"), routes_enabled)

    raw_runtime_ready = _safe_optional_bool(
        runtime.get("rawOk", runtime.get("ok"))
    )
    runtime_ready = _safe_optional_bool(
        runtime.get("startupEffectiveOk", runtime.get("ok"))
    )

    routing_requirement_ready = bool(
        not routes_enabled
        or not routes_required
        or _safe_bool(route_ready, False)
    )
    access_requirement_ready = bool(
        not _safe_bool(access.get("required"), False)
        or _safe_bool(access_ready, False)
    )
    startup_ready = (
        bool(
            runtime_ready
            and routing_requirement_ready
            and access_requirement_ready
        )
        if runtime_ready is not None
        else None
    )

    connection_checked = _safe_bool(
        database.get("connectionChecked", database.get("checked")),
        False,
    )
    database_ready = (
        _safe_optional_bool(
            database.get("connectionOk", database.get("ok"))
        )
        if connection_checked
        else None
    )

    bootstrap_checked = _safe_bool(
        bootstrap_status.get("checked"),
        False,
    )
    bootstrap_failures = [
        _safe_str(item, "")
        for item in _safe_list(runtime.get("bootstrapReadinessFailures"))
        if _safe_str(item, "")
    ]
    bootstrap_ready = (
        not bootstrap_failures
        if bootstrap_checked
        else None
    )

    schema_ready_value = (
        _safe_optional_bool(schema.get("ok"))
        if schema_checked
        else _safe_optional_bool(bootstrap_status.get("schemaReady"))
    )

    return {
        "startupReady": startup_ready,
        "runtimeChecksReady": runtime_ready,
        "runtimeChecksRawReady": raw_runtime_ready,
        "bootstrapReadinessChecked": bootstrap_checked,
        "bootstrapReadinessReady": bootstrap_ready,
        "bootstrapReadinessFailures": bootstrap_failures,
        "databaseReady": database_ready,
        "modelsReady": _safe_optional_bool(models.get("ok")),
        "schemaChecked": bool(schema_checked or bootstrap_checked),
        "schemaReady": schema_ready_value,
        "projectOwnerColumnsReady": _safe_optional_bool(
            _first_mapping_value(
                schema,
                "projectOwnerColumnsReady",
                default=bootstrap_status.get("projectOwnerColumnsReady"),
            )
        ),
        "projectAccessSchemaReady": _safe_optional_bool(
            _first_mapping_value(
                schema,
                "projectAccessSchemaReady",
                default=bootstrap_status.get("projectAccessSchemaReady"),
            )
        ),
        "projectAccessChecked": access_checked,
        "defaultProjectOwnerReady": _safe_optional_bool(
            access.get("ownerReady")
        ),
        "defaultProjectOwnerIdentityCanonical": _safe_optional_bool(
            access.get("ownerIdentityCanonical")
        ),
        "defaultProjectOwnerFingerprint": access.get(
            "ownerIdFingerprint"
        ),
        "canonicalProjectAccessReady": _safe_optional_bool(
            access.get("canonicalReady")
        ),
        "legacyProjectAccessReady": _safe_optional_bool(
            access.get("legacyReady")
        ),
        "defaultProjectRolesReady": _safe_optional_bool(
            access.get("rolesReady")
        ),
        "defaultProjectOwnerAssignmentReady": _safe_optional_bool(
            access.get("ownerAssignmentReady")
        ),
        "defaultProjectAccessReady": access_ready,
        "projectAccessSourceOfTruthReady": _safe_optional_bool(
            access.get("sourceOfTruthReady")
        ),
        "projectAccessViewerReadOnly": _safe_optional_bool(
            access.get("viewerReadOnly")
        ),
        "projectAccessRouteSurfaceReady": _safe_optional_bool(route_ready),
        "projectAccessCanonicalRouteSurfaceReady": _safe_optional_bool(
            route_access.get("canonicalRouteSurfaceReady")
        ),
        "projectAccessLegacyRouteSurfaceReady": _safe_optional_bool(
            route_access.get("legacyRouteSurfaceReady")
        ),
        "projectOwnerTransferRouteReady": _safe_optional_bool(
            route_access.get("ownerTransferRouteReady")
        ),
        "projectAccessApiEnabled": routes_enabled,
        "projectAccessRoutesRequired": routes_required,
        "projectAccessAuthzEnforced": _safe_optional_bool(
            _first_mapping_value(
                access,
                "authzEnforced",
                default=route_access.get("authzEnforced"),
            )
        ),
    }


def _store_namespace_startup_projection(
    app: Flask,
    state: Mapping[str, Any],
) -> None:
    """Expose compact, identity-safe readiness fields for status consumers."""
    try:
        namespace = _ensure_chunk_namespace(app)
        readiness = _safe_dict(state.get("readiness"))
        routing = _safe_dict(state.get("routing"))
        route_access = _safe_dict(routing.get("projectAccess"))
        project_access = _safe_dict(state.get("projectAccess"))

        namespace["startup_state_version"] = STARTUP_STATE_VERSION
        namespace["startup_contract_version"] = STARTUP_CONTRACT_VERSION
        namespace["startup_status"] = state.get("status")
        namespace["startup_ready"] = readiness.get("startupReady")
        namespace["runtime_checks_ready"] = readiness.get(
            "runtimeChecksReady"
        )
        namespace["runtime_checks_raw_ready"] = readiness.get(
            "runtimeChecksRawReady"
        )
        namespace["bootstrap_readiness_checked"] = readiness.get(
            "bootstrapReadinessChecked"
        )
        namespace["bootstrap_readiness_ready"] = readiness.get(
            "bootstrapReadinessReady"
        )
        namespace["bootstrap_readiness_failures"] = list(
            readiness.get("bootstrapReadinessFailures") or []
        )
        namespace["schema_ready"] = readiness.get("schemaReady")
        namespace["project_owner_columns_ready"] = readiness.get(
            "projectOwnerColumnsReady"
        )
        namespace["project_access_schema_ready"] = readiness.get(
            "projectAccessSchemaReady"
        )
        namespace["default_project_owner_ready"] = readiness.get(
            "defaultProjectOwnerReady"
        )
        namespace["default_project_owner_identity_canonical"] = readiness.get(
            "defaultProjectOwnerIdentityCanonical"
        )
        namespace["default_project_owner_fingerprint"] = readiness.get(
            "defaultProjectOwnerFingerprint"
        )
        namespace["canonical_project_access_ready"] = readiness.get(
            "canonicalProjectAccessReady"
        )
        namespace["legacy_project_access_ready"] = readiness.get(
            "legacyProjectAccessReady"
        )
        namespace["default_project_roles_ready"] = readiness.get(
            "defaultProjectRolesReady"
        )
        namespace["default_project_owner_assignment_ready"] = readiness.get(
            "defaultProjectOwnerAssignmentReady"
        )
        namespace["default_project_access_ready"] = readiness.get(
            "defaultProjectAccessReady"
        )
        namespace["project_access_source_of_truth_ready"] = readiness.get(
            "projectAccessSourceOfTruthReady"
        )
        namespace["project_access_viewer_read_only"] = readiness.get(
            "projectAccessViewerReadOnly"
        )
        namespace["project_access_api_enabled"] = readiness.get(
            "projectAccessApiEnabled"
        )
        namespace["project_access_routes_required"] = readiness.get(
            "projectAccessRoutesRequired"
        )
        namespace["project_access_route_surface_ready"] = readiness.get(
            "projectAccessRouteSurfaceReady"
        )
        namespace["project_access_canonical_route_surface_ready"] = (
            readiness.get("projectAccessCanonicalRouteSurfaceReady")
        )
        namespace["project_access_legacy_route_surface_ready"] = (
            readiness.get("projectAccessLegacyRouteSurfaceReady")
        )
        namespace["project_owner_transfer_route_ready"] = readiness.get(
            "projectOwnerTransferRouteReady"
        )
        namespace["project_access_blueprint_registered"] = route_access.get(
            "blueprintRegistered"
        )
        namespace["project_access_authz_enforced"] = readiness.get(
            "projectAccessAuthzEnforced"
        )
        namespace["project_access_owner_fingerprint"] = project_access.get(
            "ownerIdFingerprint"
        )
        namespace["project_access_identity_exposed"] = bool(
            project_access.get("identityExposed")
        )
        namespace["startup_readiness"] = dict(readiness)
        namespace["startup_routing"] = dict(routing)
        namespace["runtime_checks_summary"] = _safe_dict(
            state.get("runtimeChecksSummary")
        )
    except Exception:
        pass


def _build_initial_startup_state() -> dict[str, Any]:
    """Build a fresh, serializable startup state contract."""
    return {
        "state_version": STARTUP_STATE_VERSION,
        "contract_version": STARTUP_CONTRACT_VERSION,
        "status": STATUS_IDLE,
        "started_at": None,
        "completed_at": None,
        "run_count": 0,
        "strict_mode": False,
        "warnings": [],
        "errors": [],
        "checks": {
            "paths": [],
            "files": [],
            "routes": [],
            "database": {},
            "models": {},
            "schema": {},
            "project_access": {},
            "bootstrap": {},
            "routing": {},
            "runtime": {},
        },
        "metadata": {
            "authzEnforced": None,
            "runtimeReadOnly": True,
            "privateIdentifiersExposed": False,
        },
        "settings": {},
        "runtimeChecks": {},
        "runtimeChecksSummary": {},
        "readiness": _build_empty_readiness_state(),
        "routing": _build_empty_routing_state(),
        "projectAccess": _build_empty_project_access_state(),
        "seed": {
            "attempted": False,
            "completed": False,
            "operations": [],
            "runtimeDisabled": True,
        },
        "database": {
            "checked": False,
            "ok": None,
            "create_all_attempted": False,
            "create_all_ok": None,
            "runtimeDisabled": True,
        },
        "route_summary": {
            "count": 0,
            "required_missing": [],
            "optional_missing": [],
            "rules": [],
        },
    }


def _ensure_startup_state(app: Flask) -> dict[str, Any]:
    """Ensure startup state container."""
    namespace = _ensure_chunk_namespace(app)

    startup_state = namespace.get(STARTUP_STATE_KEY)
    if not isinstance(startup_state, dict):
        startup_state = _build_initial_startup_state()
        namespace[STARTUP_STATE_KEY] = startup_state

    startup_state.setdefault("state_version", STARTUP_STATE_VERSION)
    startup_state.setdefault("contract_version", STARTUP_CONTRACT_VERSION)
    startup_state.setdefault("status", STATUS_IDLE)
    startup_state.setdefault("started_at", None)
    startup_state.setdefault("completed_at", None)
    startup_state.setdefault("run_count", 0)
    startup_state.setdefault("strict_mode", False)
    startup_state.setdefault("warnings", [])
    startup_state.setdefault("errors", [])
    startup_state.setdefault("checks", {})
    startup_state.setdefault("metadata", {})
    startup_state.setdefault("settings", {})
    startup_state.setdefault("runtimeChecks", {})
    startup_state.setdefault("runtimeChecksSummary", {})
    startup_state.setdefault("readiness", _build_empty_readiness_state())
    startup_state.setdefault("routing", _build_empty_routing_state())
    startup_state.setdefault("projectAccess", {})
    startup_state.setdefault("seed", {})
    startup_state.setdefault("database", {})
    startup_state.setdefault("route_summary", {})

    if not isinstance(startup_state["warnings"], list):
        startup_state["warnings"] = []

    if not isinstance(startup_state["errors"], list):
        startup_state["errors"] = []

    if not isinstance(startup_state["checks"], dict):
        startup_state["checks"] = {}

    for key, default in (
        ("paths", []),
        ("files", []),
        ("routes", []),
        ("database", {}),
        ("models", {}),
        ("schema", {}),
        ("project_access", {}),
        ("bootstrap", {}),
        ("routing", {}),
        ("runtime", {}),
    ):
        startup_state["checks"].setdefault(key, default)

    if not isinstance(startup_state["checks"]["paths"], list):
        startup_state["checks"]["paths"] = []
    if not isinstance(startup_state["checks"]["files"], list):
        startup_state["checks"]["files"] = []
    if not isinstance(startup_state["checks"]["routes"], list):
        startup_state["checks"]["routes"] = []
    if not isinstance(startup_state["checks"]["database"], dict):
        startup_state["checks"]["database"] = {}
    if not isinstance(startup_state["checks"]["models"], dict):
        startup_state["checks"]["models"] = {}
    if not isinstance(startup_state["checks"]["schema"], dict):
        startup_state["checks"]["schema"] = {}
    if not isinstance(startup_state["checks"]["project_access"], dict):
        startup_state["checks"]["project_access"] = {}
    if not isinstance(startup_state["checks"]["bootstrap"], dict):
        startup_state["checks"]["bootstrap"] = {}
    if not isinstance(startup_state["checks"]["routing"], dict):
        startup_state["checks"]["routing"] = {}
    if not isinstance(startup_state["checks"]["runtime"], dict):
        startup_state["checks"]["runtime"] = {}

    if not isinstance(startup_state["metadata"], dict):
        startup_state["metadata"] = {}

    if not isinstance(startup_state["settings"], dict):
        startup_state["settings"] = {}

    if not isinstance(startup_state["runtimeChecks"], dict):
        startup_state["runtimeChecks"] = {}

    if not isinstance(startup_state["runtimeChecksSummary"], dict):
        startup_state["runtimeChecksSummary"] = {}

    if not isinstance(startup_state["readiness"], dict):
        startup_state["readiness"] = _build_empty_readiness_state()
    else:
        for key, value in _build_empty_readiness_state().items():
            startup_state["readiness"].setdefault(key, value)

    if not isinstance(startup_state["routing"], dict):
        startup_state["routing"] = _build_empty_routing_state()

    if not isinstance(startup_state["projectAccess"], dict):
        startup_state["projectAccess"] = _build_empty_project_access_state()
    else:
        for key, value in _build_empty_project_access_state().items():
            startup_state["projectAccess"].setdefault(key, value)

    if not isinstance(startup_state["seed"], dict):
        startup_state["seed"] = {}

    startup_state["seed"].setdefault("attempted", False)
    startup_state["seed"].setdefault("completed", False)
    startup_state["seed"].setdefault("operations", [])
    startup_state["seed"].setdefault("runtimeDisabled", True)

    if not isinstance(startup_state["seed"]["operations"], list):
        startup_state["seed"]["operations"] = []

    if not isinstance(startup_state["database"], dict):
        startup_state["database"] = {}

    startup_state["database"].setdefault("checked", False)
    startup_state["database"].setdefault("ok", None)
    startup_state["database"].setdefault("create_all_attempted", False)
    startup_state["database"].setdefault("create_all_ok", None)
    startup_state["database"].setdefault("runtimeDisabled", True)

    if not isinstance(startup_state["route_summary"], dict):
        startup_state["route_summary"] = {}

    startup_state["route_summary"].setdefault("count", 0)
    startup_state["route_summary"].setdefault("required_missing", [])
    startup_state["route_summary"].setdefault("optional_missing", [])
    startup_state["route_summary"].setdefault("rules", [])

    startup_state["state_version"] = STARTUP_STATE_VERSION
    startup_state["contract_version"] = STARTUP_CONTRACT_VERSION
    startup_state["metadata"]["authzEnforced"] = startup_state[
        "projectAccess"
    ].get("authzEnforced")
    startup_state["metadata"]["runtimeReadOnly"] = True
    startup_state["metadata"]["privateIdentifiersExposed"] = bool(
        startup_state["projectAccess"].get("identityExposed")
    )

    return startup_state


def _append_warning(app: Flask, message: str, code: str = "startup_warning", details: dict[str, Any] | None = None) -> None:
    """Append startup warning."""
    state = _ensure_startup_state(app)

    try:
        state["warnings"].append(_message_to_state_item(message, code=code, details=details))
    except Exception:
        pass

    _safe_log_warning(app, message)


def _append_error(app: Flask, message: str, code: str = "startup_error", details: dict[str, Any] | None = None) -> None:
    """Append startup error."""
    state = _ensure_startup_state(app)

    try:
        state["errors"].append(_message_to_state_item(message, code=code, details=details))
    except Exception:
        pass

    _safe_log_warning(app, message)


# -----------------------------------------------------------------------------
# Extension registry helpers
# -----------------------------------------------------------------------------

def _ensure_extension_registry(app: Flask) -> None:
    """
    Ensure extension registry exists.

    This function must remain read-only with respect to application data.
    It may initialize extension bookkeeping, but must not create tables or seed.
    """
    namespace = _ensure_chunk_namespace(app)

    if not namespace.get("extensions_initialized") and init_extensions is not None:
        try:
            init_extensions(app)
        except RuntimeError as exc:
            message = _safe_exception_message(exc)
            if "already registered" not in message.lower() and "already initialized" not in message.lower():
                raise
            _append_warning(
                app,
                f"Extension initialization was already applied: {message}",
                code="extension_initialization_already_applied",
            )
        except Exception as exc:
            message = _safe_exception_message(exc)
            raise RuntimeError(f"Extension registry initialization failed: {message}") from exc

    if register_extension is not None:
        try:
            register_extension(
                app,
                "startup",
                category="internal",
                description="Read-only runtime startup hooks and diagnostics.",
                required=True,
            )
        except Exception as exc:
            _append_warning(
                app,
                f"Could not register startup extension metadata: {_safe_exception_message(exc)}",
                code="startup_extension_registration_failed",
                details={"exceptionType": exc.__class__.__name__},
            )


def _mark_startup_initialized(app: Flask, metadata: dict[str, Any]) -> None:
    """Mark startup extension initialized defensively."""
    if mark_extension_initialized is None:
        return

    try:
        mark_extension_initialized(app, "startup", metadata=metadata)
    except Exception:
        pass


def _mark_startup_failed(app: Flask, message: str, metadata: dict[str, Any]) -> None:
    """Mark startup extension failed defensively."""
    if mark_extension_failed is None:
        return

    try:
        mark_extension_failed(app, "startup", message, metadata=metadata)
    except Exception:
        pass


def _mark_startup_warning(app: Flask, message: str, metadata: dict[str, Any] | None = None) -> None:
    """Mark startup extension warning defensively."""
    if mark_extension_warning is None:
        return

    try:
        mark_extension_warning(app, "startup", message, metadata=metadata or {})
    except Exception:
        pass


def _get_extension_summary(app: Flask) -> dict[str, Any]:
    """Get extension summary defensively."""
    if get_extension_summary is None:
        return {}

    try:
        summary = get_extension_summary(app)
        return _safe_dict(_sanitize_diagnostic_value(summary))
    except Exception:
        return {}


# -----------------------------------------------------------------------------
# Settings/runtime-check helpers
# -----------------------------------------------------------------------------

def _build_settings_summary(app: Flask) -> dict[str, Any]:
    """Build startup settings summary defensively."""
    if build_settings_summary is not None:
        try:
            summary = build_settings_summary(app)
            return _safe_dict(_sanitize_diagnostic_value(summary))
        except Exception as exc:
            return {
                "ok": False,
                "error": _safe_exception_message(exc),
                "source": "build_settings_summary",
            }

    return _safe_dict(
        _sanitize_diagnostic_value(
            {
                "ok": True,
                "runtime": {
                    "runStartupHooks": should_run_startup_hooks(app),
                    "autoCreateAllInRuntime": False,
                    "autoSeedDefaultsInRuntime": False,
                },
            }
        )
    )


def _build_bootstrap_settings(app: Flask) -> Any | None:
    """Build aggregate settings defensively."""
    if build_bootstrap_settings is None:
        return None

    try:
        return build_bootstrap_settings(app)
    except Exception:
        return None


def _should_run_startup_hooks(app: Flask) -> bool:
    """Return whether startup hooks should run."""
    try:
        return should_run_startup_hooks(app)
    except Exception:
        return _safe_bool(
            _safe_config_or_env(app, "VECTOPLAN_CHUNK_RUN_STARTUP_HOOKS", True),
            True,
        )


def _is_strict_startup_enabled(app: Flask) -> bool:
    """Return whether strict startup is enabled."""
    try:
        return is_startup_strict(app)
    except Exception:
        return _safe_bool(
            _safe_config_or_env(app, "VECTOPLAN_CHUNK_STARTUP_STRICT", False),
            False,
        )


def _runtime_db_mutations_requested(app: Flask) -> dict[str, bool]:
    """Return whether runtime DB mutation flags are requested/effective."""
    legacy_create_all_requested = _safe_bool(
        _safe_config_or_env(app, "VECTOPLAN_CHUNK_AUTO_CREATE_ALL", False),
        False,
    )
    legacy_seed_requested = _safe_bool(
        _safe_config_or_env(app, "VECTOPLAN_CHUNK_AUTO_SEED_DEFAULTS", False),
        False,
    )

    try:
        create_all_effective = should_run_create_all_in_runtime(app)
    except Exception:
        create_all_effective = False

    try:
        seed_effective = should_run_seed_in_runtime(app)
    except Exception:
        seed_effective = False

    return {
        "legacyCreateAllRequested": legacy_create_all_requested,
        "legacySeedRequested": legacy_seed_requested,
        "createAllEffective": bool(create_all_effective),
        "seedEffective": bool(seed_effective),
    }


def _run_read_only_runtime_checks(app: Flask, settings: Any | None) -> Any:
    """Run read-only runtime checks."""
    if run_runtime_checks is None:
        raise RuntimeError("runtime_checks.run_runtime_checks is unavailable.")

    return run_runtime_checks(
        app,
        settings=settings,
    )


def _store_runtime_checks_result(app: Flask, result: Any) -> None:
    """Store read-only runtime, bootstrap and normalized access readiness."""
    state = _ensure_startup_state(app)

    if runtime_checks_result_to_dict is not None:
        try:
            runtime_data = runtime_checks_result_to_dict(result)
        except Exception:
            runtime_data = {}
    else:
        runtime_data = {}

    if not runtime_data:
        try:
            if hasattr(result, "to_dict") and callable(result.to_dict):
                runtime_data = result.to_dict()
        except Exception:
            runtime_data = {}

    if not runtime_data and isinstance(result, Mapping):
        runtime_data = _safe_dict(result)

    runtime_data = _safe_dict(runtime_data)
    raw_runtime_ok = _safe_optional_bool(runtime_data.get("ok"))

    bootstrap_status = _build_bootstrap_readiness_snapshot(app)
    raw_access = _safe_dict(
        runtime_data.get("project_access")
        or runtime_data.get("projectAccess")
    )
    normalized_access = _normalize_project_access_status(
        app,
        raw_access,
        bootstrap_status,
    )
    effective_ok, bootstrap_failures = _runtime_effective_ok(
        app,
        runtime_data,
        bootstrap_status,
        normalized_access,
    )

    runtime_data["rawOk"] = raw_runtime_ok
    runtime_data["startupEffectiveOk"] = effective_ok
    runtime_data["bootstrapReadinessFailures"] = list(bootstrap_failures)
    runtime_data["bootstrap_readiness"] = bootstrap_status
    runtime_data["project_access"] = normalized_access

    public_runtime_data = _safe_dict(
        _sanitize_diagnostic_value(runtime_data)
    )
    state["runtimeChecks"] = public_runtime_data

    if build_runtime_checks_summary is not None:
        try:
            summary = build_runtime_checks_summary(result)
        except Exception:
            summary = {}
    else:
        summary = {}

    summary = _safe_dict(summary)
    summary.update(
        {
            "rawOk": raw_runtime_ok,
            "startupEffectiveOk": effective_ok,
            "bootstrapReadinessChecked": _safe_bool(
                bootstrap_status.get("checked"),
                False,
            ),
            "bootstrapReadinessReady": not bootstrap_failures
            if _safe_bool(bootstrap_status.get("checked"), False)
            else None,
            "bootstrapReadinessFailureCount": len(bootstrap_failures),
            "projectAccessReady": normalized_access.get("ready"),
            "canonicalProjectAccessReady": normalized_access.get(
                "canonicalReady"
            ),
            "legacyProjectAccessReady": normalized_access.get("legacyReady"),
            "projectOwnerReady": normalized_access.get("ownerReady"),
        }
    )
    state["runtimeChecksSummary"] = _safe_dict(
        _sanitize_diagnostic_value(summary)
    )

    routing_snapshot = _build_routing_snapshot(app)
    state["routing"] = routing_snapshot
    state["checks"]["routing"] = dict(routing_snapshot)

    if public_runtime_data:
        state["checks"]["paths"] = list(
            public_runtime_data.get("paths") or []
        )
        state["checks"]["files"] = list(
            public_runtime_data.get("files") or []
        )
        state["checks"]["routes"] = list(
            public_runtime_data.get("routes") or []
        )
        state["checks"]["database"] = _safe_dict(
            public_runtime_data.get("database")
        )
        state["checks"]["models"] = _safe_dict(
            public_runtime_data.get("models")
        )
        state["checks"]["schema"] = _safe_dict(
            public_runtime_data.get("schema")
        )
        state["checks"]["project_access"] = dict(normalized_access)
        state["checks"]["bootstrap"] = _safe_dict(
            _sanitize_diagnostic_value(bootstrap_status)
        )
        state["checks"]["runtime"] = dict(
            state["runtimeChecksSummary"]
        )

        route_summary = _safe_dict(
            public_runtime_data.get("route_summary")
        )
        required_missing = list(
            route_summary.get("requiredMissing")
            or route_summary.get("required_missing")
            or []
        )
        optional_missing = list(
            route_summary.get("optionalMissing")
            or route_summary.get("optional_missing")
            or []
        )

        state["route_summary"] = {
            "count": _safe_int(
                route_summary.get("count", 0),
                0,
                minimum=0,
            ),
            "required_missing": required_missing,
            "optional_missing": optional_missing,
            "rules": list(route_summary.get("rules") or []),
        }

        database = _safe_dict(public_runtime_data.get("database"))
        state["database"]["checked"] = bool(
            database.get(
                "connectionChecked",
                database.get("checked", False),
            )
        )
        state["database"]["ok"] = database.get(
            "connectionOk",
            database.get("ok"),
        )

    state["projectAccess"] = dict(normalized_access)
    state["readiness"] = _derive_readiness_state(
        runtime_data,
        routing_snapshot,
    )
    state["metadata"]["authzEnforced"] = normalized_access.get(
        "authzEnforced"
    )
    state["metadata"]["privateIdentifiersExposed"] = bool(
        normalized_access.get("identityExposed")
    )
    state["metadata"]["bootstrapReadinessChecked"] = _safe_bool(
        bootstrap_status.get("checked"),
        False,
    )
    state["metadata"]["bootstrapReadinessFailures"] = list(
        bootstrap_failures
    )

    warnings = _safe_list(runtime_data.get("warnings"))
    for warning in warnings:
        if isinstance(warning, Mapping):
            state["warnings"].append(
                _safe_dict(_sanitize_diagnostic_value(warning))
            )

    errors = _safe_list(runtime_data.get("errors"))
    for error in errors:
        if isinstance(error, Mapping):
            state["errors"].append(
                _safe_dict(_sanitize_diagnostic_value(error))
            )

    _store_namespace_startup_projection(app, state)


def _validate_effective_runtime_readiness(app: Flask) -> None:
    """Fail startup when mandatory read-only readiness is not satisfied."""
    state = _ensure_startup_state(app)
    readiness = _safe_dict(state.get("readiness"))

    if readiness.get("runtimeChecksReady") is not False:
        return

    failures = [
        _safe_str(item, "")
        for item in _safe_list(
            readiness.get("bootstrapReadinessFailures")
        )
        if _safe_str(item, "")
    ]
    message = (
        "Read-only runtime/bootstrap readiness is not satisfied."
        + (
            " Failed invariants: " + ", ".join(failures)
            if failures
            else ""
        )
    )

    existing_codes = {
        _safe_str(item.get("code"), "")
        for item in _safe_list(state.get("errors"))
        if isinstance(item, Mapping)
    }
    if "runtime_bootstrap_readiness_not_ready" not in existing_codes:
        _append_error(
            app,
            message,
            code="runtime_bootstrap_readiness_not_ready",
            details={
                "failures": failures,
                "bootstrapReadinessChecked": readiness.get(
                    "bootstrapReadinessChecked"
                ),
                "projectAccessReady": readiness.get(
                    "defaultProjectAccessReady"
                ),
                "projectOwnerReady": readiness.get(
                    "defaultProjectOwnerReady"
                ),
            },
        )

    state["readiness"]["startupReady"] = False
    _store_namespace_startup_projection(app, state)
    raise RuntimeError(message)


def _validate_project_access_route_surface(app: Flask) -> None:
    """Fail startup when the enabled, required Access route surface is incomplete."""
    state = _ensure_startup_state(app)
    routing = _safe_dict(state.get("routing"))
    access = _safe_dict(routing.get("projectAccess"))

    enabled = _safe_bool(access.get("enabled"), True)
    required = _safe_bool(access.get("required"), enabled)
    ready = _safe_bool(access.get("routeSurfaceReady"), False)

    if not enabled or not required or ready:
        return

    missing_groups = list(access.get("missingRouteGroups") or [])
    missing_rules = list(access.get("missingRouteRules") or [])
    owner_transfer_required = _safe_bool(
        access.get("ownerTransferRouteRequired"),
        False,
    )
    owner_transfer_ready = _safe_bool(
        access.get("ownerTransferRouteReady"),
        False,
    )

    details: list[str] = []
    if missing_groups:
        details.append("groups=" + ",".join(missing_groups))
    elif missing_rules:
        details.append("rules=" + ",".join(missing_rules))
    if owner_transfer_required and not owner_transfer_ready:
        details.append("owner-transfer")

    message = (
        "Required project-access route surface is incomplete."
        + (" Missing " + "; ".join(details) if details else "")
    )
    _append_error(
        app,
        message,
        code="project_access_route_surface_not_ready",
        details={
            "missingRouteGroups": missing_groups,
            "missingRouteRules": missing_rules,
            "missingCanonicalRouteRules": list(
                access.get("missingCanonicalRouteRules") or []
            ),
            "missingLegacyRouteRules": list(
                access.get("missingLegacyRouteRules") or []
            ),
            "blueprintRegistered": access.get("blueprintRegistered"),
            "ownerTransferRouteRequired": owner_transfer_required,
            "ownerTransferRouteReady": owner_transfer_ready,
            "authzEnforced": access.get("authzEnforced"),
        },
    )
    state["readiness"]["startupReady"] = False
    _store_namespace_startup_projection(app, state)
    raise RuntimeError(message)


# -----------------------------------------------------------------------------
# State transitions
# -----------------------------------------------------------------------------


def _start_run(app: Flask) -> dict[str, Any]:
    """Mark startup run as running."""
    state = _ensure_startup_state(app)

    state["status"] = STATUS_RUNNING
    state["started_at"] = _utc_now_iso()
    state["completed_at"] = None
    state["run_count"] = _safe_int(state.get("run_count"), default=0, minimum=0) + 1
    state["strict_mode"] = _is_strict_startup_enabled(app)

    # Reset run-local data while keeping run_count.
    state["warnings"] = []
    state["errors"] = []
    state["runtimeChecks"] = {}
    state["runtimeChecksSummary"] = {}
    state["readiness"] = _build_empty_readiness_state()
    state["routing"] = _build_routing_snapshot(app)
    state["projectAccess"] = _build_empty_project_access_state()
    state["projectAccess"]["required"] = _project_access_required(app)
    state["checks"] = {
        "paths": [],
        "files": [],
        "routes": [],
        "database": {},
        "models": {},
        "schema": {},
        "project_access": {},
        "bootstrap": {},
        "routing": dict(state["routing"]),
        "runtime": {},
    }
    state["route_summary"] = {
        "count": 0,
        "required_missing": [],
        "optional_missing": [],
    }
    state["seed"] = {
        "attempted": False,
        "completed": False,
        "operations": [],
        "runtimeDisabled": True,
    }
    state["database"] = {
        "checked": False,
        "ok": None,
        "create_all_attempted": False,
        "create_all_ok": None,
        "runtimeDisabled": True,
    }
    state["state_version"] = STARTUP_STATE_VERSION
    state["contract_version"] = STARTUP_CONTRACT_VERSION
    state["metadata"] = {
        "authzEnforced": None,
        "runtimeReadOnly": True,
        "privateIdentifiersExposed": False,
        "routingSnapshot": dict(state["routing"]),
    }
    _store_namespace_startup_projection(app, state)

    return state


def _complete_run(app: Flask, status: str = STATUS_COMPLETED) -> dict[str, Any]:
    """Mark startup run as completed."""
    state = _ensure_startup_state(app)

    state["completed_at"] = _utc_now_iso()

    if state.get("errors"):
        state["status"] = STATUS_FAILED
    elif state.get("warnings") and status == STATUS_COMPLETED:
        state["status"] = STATUS_WARNING
    else:
        state["status"] = status

    readiness = _safe_dict(state.get("readiness"))
    if state["status"] == STATUS_FAILED:
        readiness["startupReady"] = False
    elif readiness.get("startupReady") is None:
        readiness["startupReady"] = state["status"] in {
            STATUS_COMPLETED,
            STATUS_WARNING,
        }
    state["readiness"] = readiness
    _store_namespace_startup_projection(app, state)

    return state


def _skip_run(app: Flask, reason: str) -> dict[str, Any]:
    """Mark startup run as skipped."""
    state = _ensure_startup_state(app)

    now = _utc_now_iso()
    state["status"] = STATUS_SKIPPED
    state["started_at"] = now
    state["completed_at"] = now
    state["run_count"] = _safe_int(state.get("run_count"), default=0, minimum=0) + 1
    state["strict_mode"] = _is_strict_startup_enabled(app)
    state["warnings"] = []
    state["errors"] = []
    state["runtimeChecks"] = {}
    state["runtimeChecksSummary"] = {}
    state["projectAccess"] = _build_empty_project_access_state()
    state["projectAccess"]["required"] = _project_access_required(app)

    state["metadata"]["skipReason"] = reason
    state["metadata"]["settingsSummary"] = _build_settings_summary(app)
    state["routing"] = _build_routing_snapshot(app)
    state["checks"] = {
        "paths": [],
        "files": [],
        "routes": [],
        "database": {},
        "models": {},
        "schema": {},
        "project_access": {},
        "bootstrap": {},
        "routing": dict(state["routing"]),
        "runtime": {},
    }
    state["route_summary"] = {
        "count": 0,
        "required_missing": [],
        "optional_missing": [],
        "rules": [],
    }
    state["readiness"] = _derive_readiness_state({}, state["routing"])
    state["readiness"]["startupReady"] = None
    state["metadata"]["authzEnforced"] = None
    state["metadata"]["runtimeReadOnly"] = True
    state["metadata"]["privateIdentifiersExposed"] = False

    state["database"]["checked"] = False
    state["database"]["ok"] = None
    state["database"]["create_all_attempted"] = False
    state["database"]["create_all_ok"] = None
    state["database"]["runtimeDisabled"] = True

    state["seed"]["attempted"] = False
    state["seed"]["completed"] = False
    state["seed"]["runtimeDisabled"] = True

    _safe_log_info(app, "Startup hooks for `vectoplan-chunk` skipped: %s", reason)

    _store_namespace_startup_projection(app, state)

    _mark_startup_initialized(
        app,
        {
            "status": STATUS_SKIPPED,
            "runCount": state["run_count"],
            "skipped": True,
            "reason": reason,
            "completedAt": state["completed_at"],
        },
    )

    return state


def _fail_run(app: Flask, exc: BaseException) -> dict[str, Any]:
    """Mark startup run as failed."""
    state = _ensure_startup_state(app)
    state["status"] = STATUS_FAILED
    state["completed_at"] = _utc_now_iso()
    state["readiness"] = _safe_dict(state.get("readiness"))
    state["readiness"]["startupReady"] = False

    error_message = f"Startup of `vectoplan-chunk` failed: {_safe_exception_message(exc)}"
    _append_error(
        app,
        error_message,
        code="startup_failed",
        details={"exceptionType": exc.__class__.__name__},
    )
    _safe_log_exception(app, error_message)

    _store_namespace_startup_projection(app, state)

    _mark_startup_failed(
        app,
        error_message,
        metadata={
            "status": state["status"],
            "runCount": state["run_count"],
            "strictMode": state["strict_mode"],
            "completedAt": state["completed_at"],
        },
    )

    return state


# -----------------------------------------------------------------------------
# Public startup functions
# -----------------------------------------------------------------------------

def run_startup(app: Flask) -> Flask:
    """
    Run read-only runtime startup for `vectoplan-chunk`.

    Idempotent:
    - repeated calls do not destroy the app
    - run_count is incremented
    - startup state is refreshed

    Safe:
    - does not create tables
    - does not seed defaults
    - does not load chunks/snapshots/events/object refs

    Critical failures:
    - incompatible app object
    - required path/file/route/model/database checks fail
    """
    if not _is_flask_app(app):
        raise TypeError("run_startup(app) expects a Flask app or compatible object.")

    if not _should_run_startup_hooks(app):
        _ensure_chunk_namespace(app)
        _skip_run(app, "VECTOPLAN_CHUNK_RUN_STARTUP_HOOKS=false")
        return app

    state = _start_run(app)

    _safe_log_info(app, "Startup hooks for `vectoplan-chunk` are running.")

    try:
        _ensure_extension_registry(app)

        settings_summary = _build_settings_summary(app)
        state["settings"] = settings_summary
        state["metadata"]["settingsSummary"] = settings_summary

        mutation_flags = _runtime_db_mutations_requested(app)
        state["metadata"]["runtimeDbMutationFlags"] = mutation_flags

        if mutation_flags.get("legacyCreateAllRequested") and not mutation_flags.get("createAllEffective"):
            _append_warning(
                app,
                (
                    "VECTOPLAN_CHUNK_AUTO_CREATE_ALL was requested but ignored during runtime startup. "
                    "Use scripts/bootstrap_db.py or db_bootstrap.py for schema bootstrap."
                ),
                code="runtime_create_all_ignored",
                details=mutation_flags,
            )

        if mutation_flags.get("legacySeedRequested") and not mutation_flags.get("seedEffective"):
            _append_warning(
                app,
                (
                    "VECTOPLAN_CHUNK_AUTO_SEED_DEFAULTS was requested but ignored during runtime startup. "
                    "Use scripts/bootstrap_db.py or db_bootstrap.py for default seeding."
                ),
                code="runtime_seed_ignored",
                details=mutation_flags,
            )

        state["database"]["create_all_attempted"] = False
        state["database"]["create_all_ok"] = None
        state["database"]["runtimeDisabled"] = True
        state["seed"]["attempted"] = False
        state["seed"]["completed"] = False
        state["seed"]["runtimeDisabled"] = True

        settings = _build_bootstrap_settings(app)
        runtime_result = _run_read_only_runtime_checks(app, settings)
        _store_runtime_checks_result(app, runtime_result)
        _validate_project_access_route_surface(app)

        if log_runtime_checks_result is not None:
            try:
                log_runtime_checks_result(app, runtime_result)
            except Exception:
                pass

        # Preserve the native runtime-check policy for native failures, then
        # enforce the merged bootstrap/access readiness added by this module.
        readiness = _safe_dict(state.get("readiness"))
        if (
            readiness.get("runtimeChecksRawReady") is False
            and raise_if_runtime_checks_failed is not None
        ):
            raise_if_runtime_checks_failed(runtime_result)

        _validate_effective_runtime_readiness(app)

        if state.get("errors"):
            first_error = state["errors"][0]
            message = (
                first_error.get("message", "Runtime startup checks failed.")
                if isinstance(first_error, Mapping)
                else _safe_str(first_error, "Runtime startup checks failed.")
            )
            raise RuntimeError(message)

        extension_summary = _get_extension_summary(app)
        state["metadata"]["extensionSummary"] = extension_summary

        completed_state = _complete_run(app, STATUS_COMPLETED)

        _mark_startup_initialized(
            app,
            metadata={
                "status": completed_state["status"],
                "runCount": completed_state["run_count"],
                "strictMode": completed_state["strict_mode"],
                "routeCount": completed_state["route_summary"].get("count", 0),
                "requiredMissingRoutes": completed_state["route_summary"].get("required_missing", []),
                "routingInitialized": completed_state.get("routing", {}).get("routingInitialized"),
                "projectAccessRouteSurfaceReady": completed_state.get("readiness", {}).get("projectAccessRouteSurfaceReady"),
                "schemaReady": completed_state.get("readiness", {}).get("schemaReady"),
                "projectOwnerColumnsReady": completed_state.get("readiness", {}).get("projectOwnerColumnsReady"),
                "projectAccessSchemaReady": completed_state.get("readiness", {}).get("projectAccessSchemaReady"),
                "defaultProjectOwnerReady": completed_state.get("readiness", {}).get("defaultProjectOwnerReady"),
                "defaultProjectRolesReady": completed_state.get("readiness", {}).get("defaultProjectRolesReady"),
                "defaultProjectOwnerAssignmentReady": completed_state.get("readiness", {}).get("defaultProjectOwnerAssignmentReady"),
                "defaultProjectAccessReady": completed_state.get("readiness", {}).get("defaultProjectAccessReady"),
                "canonicalProjectAccessReady": completed_state.get("readiness", {}).get("canonicalProjectAccessReady"),
                "legacyProjectAccessReady": completed_state.get("readiness", {}).get("legacyProjectAccessReady"),
                "projectOwnerIdentityCanonical": completed_state.get("readiness", {}).get("defaultProjectOwnerIdentityCanonical"),
                "projectOwnerFingerprint": completed_state.get("readiness", {}).get("defaultProjectOwnerFingerprint"),
                "projectAccessSourceOfTruthReady": completed_state.get("readiness", {}).get("projectAccessSourceOfTruthReady"),
                "projectAccessViewerReadOnly": completed_state.get("readiness", {}).get("projectAccessViewerReadOnly"),
                "bootstrapReadinessReady": completed_state.get("readiness", {}).get("bootstrapReadinessReady"),
                "bootstrapReadinessFailures": completed_state.get("readiness", {}).get("bootstrapReadinessFailures"),
                "projectAccessAuthzEnforced": completed_state.get("readiness", {}).get("projectAccessAuthzEnforced"),
                "warningCount": len(completed_state.get("warnings", []) or []),
                "errorCount": len(completed_state.get("errors", []) or []),
                "seedAttempted": False,
                "seedCompleted": False,
                "seedRuntimeDisabled": True,
                "createAllAttempted": False,
                "createAllOk": None,
                "createAllRuntimeDisabled": True,
                "completedAt": completed_state["completed_at"],
            },
        )

        if completed_state["status"] == STATUS_WARNING:
            _mark_startup_warning(
                app,
                "Startup hooks for `vectoplan-chunk` completed with warnings.",
                metadata={
                    "warningCount": len(completed_state.get("warnings", []) or []),
                },
            )
            _safe_log_warning(app, "Startup hooks for `vectoplan-chunk` completed with warnings.")
        else:
            _safe_log_info(app, "Startup hooks for `vectoplan-chunk` completed successfully.")

        return app

    except Exception as exc:
        _fail_run(app, exc)
        raise


def bootstrap_app(app: Flask) -> Flask:
    """Compatibility alias."""
    return run_startup(app)


def initialize_app(app: Flask) -> Flask:
    """Compatibility alias."""
    return run_startup(app)


# -----------------------------------------------------------------------------
# Read/debug helpers
# -----------------------------------------------------------------------------

def get_startup_state(app: Flask) -> dict[str, Any]:
    """Return startup state as defensive copy."""
    state = _ensure_startup_state(app)
    return _safe_deepcopy(state)


def get_startup_summary(app: Flask) -> dict[str, Any]:
    """Return compact startup, routing and access-readiness summary."""
    state = _ensure_startup_state(app)

    runtime_checks_summary = _safe_dict(state.get("runtimeChecksSummary"))
    settings_summary = _safe_dict(state.get("settings"))
    readiness = _safe_dict(state.get("readiness"))
    routing = _safe_dict(state.get("routing"))
    route_access = _safe_dict(routing.get("projectAccess"))
    project_access = _safe_dict(state.get("projectAccess"))

    return {
        "stateVersion": state.get("state_version", STARTUP_STATE_VERSION),
        "contractVersion": state.get(
            "contract_version",
            STARTUP_CONTRACT_VERSION,
        ),
        "status": _safe_str(state.get("status"), "unknown"),
        "startedAt": state.get("started_at"),
        "completedAt": state.get("completed_at"),
        "runCount": _safe_int(state.get("run_count"), default=0, minimum=0),
        "strictMode": _safe_bool(state.get("strict_mode"), False),
        "startupReady": readiness.get("startupReady"),
        "warningCount": len(state.get("warnings", []) or []),
        "errorCount": len(state.get("errors", []) or []),
        "routeCount": _safe_int(
            state.get("route_summary", {}).get("count", 0),
            default=0,
            minimum=0,
        ),
        "requiredMissingRoutes": list(
            state.get("route_summary", {}).get("required_missing", []) or []
        ),
        "optionalMissingRoutes": list(
            state.get("route_summary", {}).get("optional_missing", []) or []
        ),
        "routing": {
            "initialized": routing.get("routingInitialized"),
            "registeredBlueprintNames": list(
                routing.get("registeredBlueprintNames") or []
            ),
            "projectAccessApiEnabled": route_access.get("enabled"),
            "projectAccessRoutesRequired": route_access.get("required"),
            "projectAccessBlueprintRegistered": route_access.get(
                "blueprintRegistered"
            ),
            "projectAccessRouteSurfaceReady": route_access.get(
                "routeSurfaceReady"
            ),
            "projectAccessCanonicalRouteSurfaceReady": route_access.get(
                "canonicalRouteSurfaceReady"
            ),
            "projectAccessLegacyRouteSurfaceReady": route_access.get(
                "legacyRouteSurfaceReady"
            ),
            "projectOwnerTransferRouteRequired": route_access.get(
                "ownerTransferRouteRequired"
            ),
            "projectOwnerTransferRouteReady": route_access.get(
                "ownerTransferRouteReady"
            ),
            "projectAccessMissingRouteGroups": list(
                route_access.get("missingRouteGroups") or []
            ),
            "projectAccessMissingRouteRules": list(
                route_access.get("missingRouteRules") or []
            ),
            "projectAccessMissingCanonicalRouteRules": list(
                route_access.get("missingCanonicalRouteRules") or []
            ),
            "projectAccessAuthzEnforced": route_access.get(
                "authzEnforced"
            ),
        },
        "readiness": dict(readiness),
        "projectAccess": dict(project_access),
        "database": {
            "checked": state.get("database", {}).get("checked", False),
            "ok": state.get("database", {}).get("ok"),
            "createAllAttempted": False,
            "createAllOk": None,
            "runtimeDisabled": True,
        },
        "seed": {
            "attempted": False,
            "completed": False,
            "operationCount": 0,
            "runtimeDisabled": True,
        },
        "runtimeChecks": runtime_checks_summary,
        "settings": settings_summary,
        "authzEnforced": project_access.get("authzEnforced"),
        "runtimeReadOnly": True,
        "privateIdentifiersExposed": bool(
            project_access.get("identityExposed")
        ),
    }


def get_startup_readiness(app: Flask) -> dict[str, Any]:
    """Return a defensive copy of the normalized startup readiness projection."""
    state = _ensure_startup_state(app)
    return _safe_deepcopy(_safe_dict(state.get("readiness")))


def get_startup_routing_summary(app: Flask) -> dict[str, Any]:
    """Return bounded routing and project-access route-surface diagnostics."""
    state = _ensure_startup_state(app)
    return _safe_deepcopy(_safe_dict(state.get("routing")))


def get_runtime_checks_summary(app: Flask) -> dict[str, Any]:
    """Return compact runtime checks summary."""
    state = _ensure_startup_state(app)
    return _safe_deepcopy(_safe_dict(state.get("runtimeChecksSummary")))


def get_settings_summary(app: Flask) -> dict[str, Any]:
    """Return compact bootstrap settings summary."""
    state = _ensure_startup_state(app)
    summary = _safe_dict(state.get("settings"))

    if summary:
        return _safe_deepcopy(summary)

    return _build_settings_summary(app)


# -----------------------------------------------------------------------------
# Compatibility helpers for old imports
# -----------------------------------------------------------------------------

def _seed_operation_to_dict(result: SeedOperationResult) -> dict[str, Any]:
    """Serialize compatibility seed operation result."""
    try:
        return asdict(result)
    except Exception:
        return {
            "name": getattr(result, "name", "unknown"),
            "ok": bool(getattr(result, "ok", False)),
            "created": bool(getattr(result, "created", False)),
            "updated": bool(getattr(result, "updated", False)),
            "skipped": bool(getattr(result, "skipped", False)),
            "message": getattr(result, "message", None),
            "data": getattr(result, "data", None) or {},
        }


def _run_create_all_if_enabled(app: Flask) -> None:
    """
    Compatibility no-op.

    Runtime startup no longer creates tables. Use:

        scripts/bootstrap_db.py
        src.bootstrap.db_bootstrap.run_db_bootstrap()
    """
    state = _ensure_startup_state(app)
    state["database"]["create_all_attempted"] = False
    state["database"]["create_all_ok"] = None
    state["database"]["runtimeDisabled"] = True

    _append_warning(
        app,
        "Runtime db.create_all() is disabled. Use explicit DB bootstrap instead.",
        code="runtime_create_all_disabled",
    )


def _run_default_seeding_if_enabled(app: Flask) -> None:
    """
    Compatibility no-op.

    Runtime startup no longer seeds defaults. Use:

        scripts/bootstrap_db.py
        src.bootstrap.db_bootstrap.run_db_bootstrap()
    """
    state = _ensure_startup_state(app)
    state["seed"]["attempted"] = False
    state["seed"]["completed"] = False
    state["seed"]["runtimeDisabled"] = True

    _append_warning(
        app,
        "Runtime default seeding is disabled. Use explicit DB bootstrap instead.",
        code="runtime_seed_disabled",
    )


# -----------------------------------------------------------------------------
# Fallback spec helpers if runtime_checks import failed
# -----------------------------------------------------------------------------

if get_default_path_check_specs is None:
    def get_default_path_check_specs() -> tuple[Any, ...]:  # type: ignore[no-redef]
        return tuple()

if get_default_file_check_specs is None:
    def get_default_file_check_specs() -> tuple[Any, ...]:  # type: ignore[no-redef]
        return tuple()

if get_default_route_check_specs is None:
    def get_default_route_check_specs() -> tuple[Any, ...]:  # type: ignore[no-redef]
        return tuple()

if get_default_path_check_spec_data is None:
    def get_default_path_check_spec_data() -> list[dict[str, Any]]:  # type: ignore[no-redef]
        return []

if get_default_file_check_spec_data is None:
    def get_default_file_check_spec_data() -> list[dict[str, Any]]:  # type: ignore[no-redef]
        return []

if get_default_route_check_spec_data is None:
    def get_default_route_check_spec_data() -> list[dict[str, Any]]:  # type: ignore[no-redef]
        return []


# -----------------------------------------------------------------------------
# Public exports
# -----------------------------------------------------------------------------

__all__ = [
    "STARTUP_STATE_VERSION",
    "STARTUP_CONTRACT_VERSION",
    "PROJECT_ACCESS_BLUEPRINT_NAME",
    "PROJECT_ACCESS_CORE_ROUTE_RULES",
    "PROJECT_ACCESS_CANONICAL_ROUTE_RULES",
    "PROJECT_ACCESS_LEGACY_ROUTE_RULES",
    "PROJECT_ACCESS_REQUIRED_ROUTE_GROUPS",
    "PROJECT_ACCESS_OWNER_TRANSFER_ROUTE_RULES",
    "PathCheckSpec",
    "FileCheckSpec",
    "RouteCheckSpec",
    "SeedOperationResult",
    "get_default_path_check_specs",
    "get_default_file_check_specs",
    "get_default_route_check_specs",
    "get_default_path_check_spec_data",
    "get_default_file_check_spec_data",
    "get_default_route_check_spec_data",
    "run_startup",
    "bootstrap_app",
    "initialize_app",
    "get_startup_state",
    "get_startup_summary",
    "get_startup_readiness",
    "get_startup_routing_summary",
    "get_runtime_checks_summary",
    "get_settings_summary",
]