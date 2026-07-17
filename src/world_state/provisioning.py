# services/vectoplan-chunk/src/world_state/provisioning.py
"""
Atomic project provisioning for ``vectoplan-chunk``.

This module creates or returns the complete chunk-side project graph for an
external ``vectoplan-app`` project:

    Project
      -> project-scoped default roles and owner assignment
      -> Universe
           -> concrete editable WorldInstance

Supported world templates:

    flat   (default)
    earth  (requires an explicit Earth global reference)

Service boundaries and invariants:

* ``vectoplan-app`` owns App projects and user identities.
* ``vectoplan-chunk`` owns Chunk projects, universes, worlds and access rows.
* External App project IDs and user IDs are opaque strings without cross-service
  database foreign keys.
* ``flat`` and ``earth`` are provider/template identities, never concrete
  ``world_id`` values.
* ``world_spawn`` is the default concrete editable world ID.
* Existing world templates are immutable through this provisioning path.
* An existing Earth reference is never silently replaced or re-anchored.
* Universe and world lookups are always parent-scoped to preserve project data
  isolation.
* All created or repaired rows participate in one caller-controlled SQLAlchemy
  transaction.

This module deliberately does not:

* create or alter database tables,
* run migrations or global seeds,
* generate or load chunks,
* read/write snapshots, events, commands or object rows,
* evaluate authorization,
* call ``vectoplan-app`` or an authentication service.

The public functions return structured results instead of leaking SQLAlchemy
exceptions into route handlers. With ``commit=False`` the caller owns commit and
rollback; with ``commit=True`` this module commits on success and rolls back on
failure.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
from functools import lru_cache
from types import MappingProxyType
from typing import Any, Callable, Final, Mapping, MutableMapping, Optional, Sequence

try:
    from flask import current_app, has_app_context
except Exception:  # pragma: no cover - isolated tooling can run without Flask.
    current_app = None  # type: ignore[assignment]

    def has_app_context() -> bool:  # type: ignore[no-redef]
        return False

try:
    from sqlalchemy.exc import IntegrityError, SQLAlchemyError
except Exception:  # pragma: no cover - SQLAlchemy is required in service runtime.
    IntegrityError = Exception  # type: ignore[misc, assignment]
    SQLAlchemyError = Exception  # type: ignore[misc, assignment]

try:
    from extensions import db
except Exception:  # pragma: no cover - keep diagnostics importable.
    db = None  # type: ignore[assignment]

try:
    from models import BlockRegistry, Project, Universe, WorldInstance
except Exception:  # pragma: no cover - keep diagnostics importable.
    Project = None  # type: ignore[assignment]
    Universe = None  # type: ignore[assignment]
    WorldInstance = None  # type: ignore[assignment]
    BlockRegistry = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Constants and stable contracts
# ---------------------------------------------------------------------------

PROVISIONING_SCHEMA_VERSION: Final[str] = "vectoplan.chunk.project-provisioning.v2"
PROVISIONING_SERVICE_VERSION: Final[str] = "2.0.3"
PROVISIONING_ROW_LOCK_POLICY_VERSION: Final[str] = "project-provisioning-row-lock.v2"
PROVISIONING_EARTH_REFERENCE_POLICY_VERSION: Final[str] = "project-provisioning-earth-reference.v2"
PROVISIONING_WORLD_STATE_POLICY_VERSION: Final[str] = "project-provisioning-world-state.v2"

WORLD_TEMPLATE_FLAT: Final[str] = "flat"
WORLD_TEMPLATE_EARTH: Final[str] = "earth"
SUPPORTED_WORLD_TEMPLATES: Final[tuple[str, ...]] = (
    WORLD_TEMPLATE_FLAT,
    WORLD_TEMPLATE_EARTH,
)

DEFAULT_OWNER_USER_ID: Final[str] = "1"
DEFAULT_PROJECT_PREFIX: Final[str] = "chk_prj_"
DEFAULT_UNIVERSE_PREFIX: Final[str] = "chk_uni_"
DEFAULT_WORLD_ID: Final[str] = "world_spawn"
DEFAULT_WORLD_ROLE: Final[str] = "default_spawn"
DEFAULT_WORLD_SCOPE: Final[str] = "project"
DEFAULT_BLOCK_REGISTRY_ID: Final[str] = "debug-blocks"
DEFAULT_BLOCK_REGISTRY_VERSION: Final[str] = "1"
DEFAULT_SOURCE_SERVICE: Final[str] = "vectoplan-app"

EARTH_REFERENCE_SCHEMA_VERSION: Final[str] = "earth-global-reference.schema.v1"
EARTH_REFERENCE_DEFAULT_CRS: Final[str] = ""

MAX_PUBLIC_ID_LENGTH: Final[int] = 128
MAX_USER_ID_LENGTH: Final[int] = 191
MAX_NAME_LENGTH: Final[int] = 255
MAX_DESCRIPTION_LENGTH: Final[int] = 4096
MAX_EXTERNAL_URL_LENGTH: Final[int] = 1024
MAX_METADATA_DEPTH: Final[int] = 32

_PUBLIC_ID_RE: Final[re.Pattern[str]] = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$"
)
_CONTROL_CHARACTER_RE: Final[re.Pattern[str]] = re.compile(r"[\x00-\x1f\x7f]")
_MISSING: Final[object] = object()

PROJECT_ID_FIELDS: Final[tuple[str, ...]] = (
    "project_id",
    "public_id",
    "key",
)
UNIVERSE_ID_FIELDS: Final[tuple[str, ...]] = (
    "universe_id",
    "public_id",
    "key",
)
WORLD_ID_FIELDS: Final[tuple[str, ...]] = (
    "world_id",
    "public_id",
    "key",
)

WORLD_TEMPLATE_PAYLOAD_FIELDS: Final[tuple[str, ...]] = (
    "worldTemplate",
    "world_template",
    "worldTemplateId",
    "world_template_id",
    "templateId",
    "template_id",
)
WORLD_PROVIDER_PAYLOAD_FIELDS: Final[tuple[str, ...]] = (
    "providerId",
    "provider_id",
    "providerWorldId",
    "provider_world_id",
)
EARTH_REFERENCE_PAYLOAD_FIELDS: Final[tuple[str, ...]] = (
    "earthReference",
    "earth_reference",
    "globalReference",
    "global_reference",
    "globalReferencePoint",
    "global_reference_point",
)
OWNER_USER_ID_PAYLOAD_FIELDS: Final[tuple[str, ...]] = (
    "ownerUserId",
    "owner_user_id",
    "ownerId",
    "owner_id",
)
ACTOR_USER_ID_PAYLOAD_FIELDS: Final[tuple[str, ...]] = (
    "actorUserId",
    "actor_user_id",
    "createdByUserId",
    "created_by_user_id",
)


@lru_cache(maxsize=1)
def _default_world_template_contracts() -> Mapping[str, Mapping[str, Any]]:
    """Return immutable pure defaults; no app configuration is cached here."""

    contracts: dict[str, Mapping[str, Any]] = {
        WORLD_TEMPLATE_FLAT: MappingProxyType(
            {
                "template_id": "flat",
                "provider_id": "flat",
                "provider_world_id": "flat",
                "generator_type": "flat-world",
                "generator_version": "1",
                "projection_type": "flat-local-v1",
                "topology_type": "flat-unbounded-v1",
                "coordinate_system": "vectoplan-world-y-up-v1",
                "chunk_size": 16,
                "cell_size": 1.0,
                "surface_y": 0,
                "min_y": -8,
                "max_y": 64,
                "seed": "dev-seed",
                "spawn_x": 0,
                "spawn_y": 2,
                "spawn_z": 0,
                "spawn_yaw": 0.0,
                "spawn_pitch": 0.0,
            }
        ),
        WORLD_TEMPLATE_EARTH: MappingProxyType(
            {
                "template_id": "earth",
                "provider_id": "earth",
                "provider_world_id": "earth",
                "generator_type": "earth-flat-periodic",
                "generator_version": "1",
                "projection_type": "vectoplan-periodic-equirectangular",
                "topology_type": "periodic-x-v1",
                "coordinate_system": "vectoplan-earth-grid-v1",
                "chunk_size": 16,
                "cell_size": 1.0,
                "surface_y": 0,
                "min_y": -1024,
                "max_y": 8192,
                "seed": "earth-v1",
                "spawn_x": 0,
                "spawn_y": 0,
                "spawn_z": 0,
                "spawn_yaw": 0.0,
                "spawn_pitch": 0.0,
            }
        ),
    }
    return MappingProxyType(contracts)


# ---------------------------------------------------------------------------
# Result and error contracts
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProvisioningIssue:
    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "details": _json_safe(self.details),
        }


@dataclass(frozen=True, slots=True)
class ProvisioningIds:
    chunk_project_id: str
    chunk_universe_id: str
    chunk_world_id: str
    project_explicit: bool = False
    universe_explicit: bool = False
    world_explicit: bool = False


@dataclass(frozen=True, slots=True)
class WorldTemplateSelection:
    template_id: str
    contract: Mapping[str, Any]
    earth_reference: Optional[dict[str, Any]] = None
    earth_reference_fingerprint: Optional[str] = None

    @property
    def is_flat(self) -> bool:
        return self.template_id == WORLD_TEMPLATE_FLAT

    @property
    def is_earth(self) -> bool:
        return self.template_id == WORLD_TEMPLATE_EARTH

    def to_dict(self) -> dict[str, Any]:
        return {
            "worldTemplate": self.template_id,
            "providerId": self.contract.get("provider_id"),
            "providerWorldId": self.contract.get("provider_world_id"),
            "generatorType": self.contract.get("generator_type"),
            "projectionType": self.contract.get("projection_type"),
            "topologyType": self.contract.get("topology_type"),
            "coordinateSystem": self.contract.get("coordinate_system"),
            "earthReferenceRequired": self.is_earth,
            "earthReferenceFingerprint": self.earth_reference_fingerprint,
        }


@dataclass(slots=True)
class ChunkProjectProvisioningResult:
    ok: bool
    code: str
    message: str
    created: bool = False
    updated: bool = False
    status_code: int = 200

    external_app_project_id: Optional[str] = None
    owner_user_id: Optional[str] = None
    chunk_project_id: Optional[str] = None
    chunk_universe_id: Optional[str] = None
    chunk_world_id: Optional[str] = None
    world_template: Optional[str] = None
    earth_reference_fingerprint: Optional[str] = None
    block_registry_id: Optional[str] = None
    block_registry_version: Optional[str] = None

    project: dict[str, Any] = field(default_factory=dict)
    universe: dict[str, Any] = field(default_factory=dict)
    world: dict[str, Any] = field(default_factory=dict)
    access: dict[str, Any] = field(default_factory=dict)
    block_registry: dict[str, Any] = field(default_factory=dict)
    route_hints: dict[str, str] = field(default_factory=dict)
    lifecycle: dict[str, Any] = field(default_factory=dict)

    warnings: list[ProvisioningIssue] = field(default_factory=list)
    errors: list[ProvisioningIssue] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "code": self.code,
            "message": self.message,
            "created": self.created,
            "updated": self.updated,
            "schemaVersion": PROVISIONING_SCHEMA_VERSION,
            "serviceVersion": PROVISIONING_SERVICE_VERSION,
            "ids": {
                "externalAppProjectId": self.external_app_project_id,
                "ownerUserId": self.owner_user_id,
                "chunkProjectId": self.chunk_project_id,
                "chunkUniverseId": self.chunk_universe_id,
                "chunkWorldId": self.chunk_world_id,
                "blockRegistryId": self.block_registry_id,
                "blockRegistryVersion": self.block_registry_version,
            },
            "worldTemplate": self.world_template,
            "earthReferenceFingerprint": self.earth_reference_fingerprint,
            "project": _json_safe(self.project),
            "universe": _json_safe(self.universe),
            "world": _json_safe(self.world),
            "access": _json_safe(self.access),
            "blockRegistry": _json_safe(self.block_registry),
            "routeHints": dict(self.route_hints),
            "lifecycle": _json_safe(self.lifecycle),
            "warnings": [issue.to_dict() for issue in self.warnings],
            "errors": [issue.to_dict() for issue in self.errors],
        }


class ProvisioningError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: Optional[Mapping[str, Any]] = None,
        status_code: int = 400,
    ) -> None:
        super().__init__(message)
        self.code = str(code)
        self.message = str(message)
        self.details = dict(details or {})
        self.status_code = int(status_code)


# ---------------------------------------------------------------------------
# Primitive safe helpers
# ---------------------------------------------------------------------------


def utcnow() -> datetime:
    try:
        return datetime.now(timezone.utc)
    except Exception:  # pragma: no cover
        return datetime.fromtimestamp(0, tz=timezone.utc)


def _safe_str(value: Any, default: str = "", *, max_length: int | None = None) -> str:
    if value is None:
        text = default
    else:
        try:
            text = str(value).strip()
        except Exception:
            text = default
    if not text:
        text = default
    if max_length is not None and max_length > 0 and len(text) > max_length:
        text = text[:max_length]
    return text


def _safe_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in {0, 1}:
        return bool(value)
    text_value = _safe_str(value).lower()
    if text_value in {"1", "true", "yes", "y", "on", "enabled"}:
        return True
    if text_value in {"0", "false", "no", "n", "off", "disabled"}:
        return False
    return default


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _safe_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _safe_exception_message(exc: BaseException | Any) -> str:
    try:
        message = str(exc)
    except Exception:
        message = exc.__class__.__name__
    return message or exc.__class__.__name__


def _json_safe(value: Any, *, depth: int = 0) -> Any:
    if depth > MAX_METADATA_DEPTH:
        raise ValueError("JSON value exceeds maximum nesting depth")
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        current = value
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        return current.astimezone(timezone.utc).isoformat()
    if isinstance(value, Mapping):
        return {
            str(key): _json_safe(item, depth=depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(item, depth=depth + 1) for item in value]
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return _json_safe(to_dict(), depth=depth + 1)
    try:
        json.dumps(value)
        return value
    except Exception:
        return _safe_str(value, f"<{type(value).__name__}>")


def _canonical_json(value: Any) -> str:
    return json.dumps(
        _json_safe(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )



def _short_hash(value: Any, *, length: int = 12) -> str:
    digest = hashlib.sha256(_safe_str(value, "unknown").encode("utf-8")).hexdigest()
    return digest[: max(6, min(length, 64))]


def _normalize_required_text(
    value: Any,
    *,
    field_name: str,
    max_length: int,
) -> str:
    if value is None:
        raise ProvisioningError(
            "invalid_provisioning_payload",
            f"{field_name} is required.",
            details={"field": field_name},
            status_code=400,
        )
    try:
        text = str(value).strip()
    except Exception as exc:
        raise ProvisioningError(
            "invalid_provisioning_payload",
            f"{field_name} must be text-compatible.",
            details={"field": field_name},
            status_code=400,
        ) from exc
    if not text:
        raise ProvisioningError(
            "invalid_provisioning_payload",
            f"{field_name} must not be empty.",
            details={"field": field_name},
            status_code=400,
        )
    if _CONTROL_CHARACTER_RE.search(text):
        raise ProvisioningError(
            "invalid_provisioning_payload",
            f"{field_name} contains control characters.",
            details={"field": field_name},
            status_code=400,
        )
    if len(text) > max_length:
        raise ProvisioningError(
            "invalid_provisioning_payload",
            f"{field_name} exceeds maximum length {max_length}.",
            details={"field": field_name, "maxLength": max_length},
            status_code=400,
        )
    return text


def _normalize_optional_text(
    value: Any,
    *,
    field_name: str,
    max_length: int,
) -> Optional[str]:
    if value is None:
        return None
    try:
        text = str(value).strip()
    except Exception as exc:
        raise ProvisioningError(
            "invalid_provisioning_payload",
            f"{field_name} must be text-compatible.",
            details={"field": field_name},
            status_code=400,
        ) from exc
    if not text:
        return None
    return _normalize_required_text(
        text,
        field_name=field_name,
        max_length=max_length,
    )


def _normalize_public_id(value: Any, *, field_name: str) -> str:
    text = _normalize_required_text(
        value,
        field_name=field_name,
        max_length=MAX_PUBLIC_ID_LENGTH,
    )
    if not _PUBLIC_ID_RE.fullmatch(text):
        raise ProvisioningError(
            "invalid_provisioning_identifier",
            (
                f"{field_name} may only contain letters, numbers, underscores, "
                "dashes, dots and colons and must begin with a letter or number."
            ),
            details={"field": field_name, "value": text},
            status_code=400,
        )
    return text


def _normalize_optional_public_id(value: Any, *, field_name: str) -> Optional[str]:
    if value is None or _safe_str(value) == "":
        return None
    return _normalize_public_id(value, field_name=field_name)


def _normalize_user_id(value: Any, *, field_name: str, required: bool = True) -> Optional[str]:
    if value is None or _safe_str(value) == "":
        if required:
            raise ProvisioningError(
                "missing_owner_user_id",
                f"{field_name} is required.",
                details={"field": field_name},
                status_code=400,
            )
        return None
    return _normalize_required_text(
        value,
        field_name=field_name,
        max_length=MAX_USER_ID_LENGTH,
    )


@lru_cache(maxsize=32)
def _normalize_world_template_cached(raw_value: str) -> str:
    normalized = raw_value.strip().lower().replace("_", "-")
    aliases = {
        "flat": WORLD_TEMPLATE_FLAT,
        "flat-world": WORLD_TEMPLATE_FLAT,
        "local": WORLD_TEMPLATE_FLAT,
        "earth": WORLD_TEMPLATE_EARTH,
        "earth-v1": WORLD_TEMPLATE_EARTH,
        "global-earth": WORLD_TEMPLATE_EARTH,
    }
    resolved = aliases.get(normalized)
    if resolved is None:
        raise ValueError(
            "worldTemplate must be one of: " + ", ".join(SUPPORTED_WORLD_TEMPLATES)
        )
    return resolved


def _normalize_world_template(value: Any) -> str:
    raw = _safe_str(value, WORLD_TEMPLATE_FLAT)
    try:
        return _normalize_world_template_cached(raw)
    except ValueError as exc:
        raise ProvisioningError(
            "unsupported_world_template",
            str(exc),
            details={"worldTemplate": raw},
            status_code=400,
        ) from exc


def _payload_dict(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    if payload is None:
        return {}
    if not isinstance(payload, Mapping):
        raise ProvisioningError(
            "invalid_provisioning_payload",
            "Provisioning payload must be a JSON object.",
            status_code=400,
        )
    try:
        return dict(payload)
    except Exception as exc:
        raise ProvisioningError(
            "invalid_provisioning_payload",
            "Provisioning payload could not be converted to a dictionary.",
            status_code=400,
        ) from exc


def _payload_value(
    payload: Mapping[str, Any],
    names: Sequence[str],
    *,
    default: Any = None,
    allow_empty: bool = False,
) -> Any:
    for name in names:
        if name not in payload:
            continue
        value = payload.get(name)
        if allow_empty or value not in (None, ""):
            return value
    return default


def _payload_has_any(payload: Mapping[str, Any], names: Sequence[str]) -> bool:
    return any(name in payload for name in names)


def _config_value(name: str, default: Any = None) -> Any:
    try:
        if has_app_context() and current_app is not None:
            return current_app.config.get(name, default)
    except Exception:
        pass
    return default


def _config_str(name: str, default: str) -> str:
    return _safe_str(_config_value(name, default), default)


def _config_bool(name: str, default: bool = False) -> bool:
    return _safe_bool(_config_value(name, default), default)


def _config_int(name: str, default: int) -> int:
    return _safe_int(_config_value(name, default), default)


def _config_float(name: str, default: float) -> float:
    return _safe_float(_config_value(name, default), default)


@lru_cache(maxsize=1)
def _load_georeferencing_api() -> Mapping[str, Any]:
    """Load the Earth-reference domain API only when Earth is provisioned.

    The provisioning module remains cheap and import-safe for Flat projects.
    Heavy CRS/PROJ modules are imported only after an explicit Earth request.
    """

    attempts: list[str] = []
    layouts = (
        (
            "src.georeferencing.contracts",
            "src.georeferencing.crs",
            "src.georeferencing.earth_grid",
        ),
        (
            "georeferencing.contracts",
            "georeferencing.crs",
            "georeferencing.earth_grid",
        ),
    )

    for contracts_path, crs_path, earth_grid_path in layouts:
        try:
            contracts_module = __import__(
                contracts_path,
                fromlist=(
                    "GlobalCoordinate",
                    "GlobalReferencePoint",
                ),
            )
            crs_module = __import__(crs_path, fromlist=("resolve_crs",))
            earth_grid_module = __import__(
                earth_grid_path,
                fromlist=("get_default_earth_grid_definition",),
            )

            global_coordinate = getattr(contracts_module, "GlobalCoordinate", None)
            global_reference = getattr(contracts_module, "GlobalReferencePoint", None)
            resolve_crs = getattr(crs_module, "resolve_crs", None)
            get_default_grid = getattr(
                earth_grid_module,
                "get_default_earth_grid_definition",
                None,
            )

            missing = [
                name
                for name, value in (
                    ("GlobalCoordinate", global_coordinate),
                    ("GlobalReferencePoint", global_reference),
                    ("resolve_crs", resolve_crs),
                    ("get_default_earth_grid_definition", get_default_grid),
                )
                if value is None or (name.startswith("get_") or name == "resolve_crs") and not callable(value)
            ]
            if missing:
                attempts.append(
                    f"{contracts_path}: missing exports {', '.join(missing)}"
                )
                continue

            return MappingProxyType(
                {
                    "GlobalCoordinate": global_coordinate,
                    "GlobalReferencePoint": global_reference,
                    "resolveCrs": resolve_crs,
                    "getDefaultEarthGridDefinition": get_default_grid,
                    "contractsModule": contracts_module,
                    "crsModule": crs_module,
                    "earthGridModule": earth_grid_module,
                    "contractsModuleName": contracts_path,
                    "crsModuleName": crs_path,
                    "earthGridModuleName": earth_grid_path,
                }
            )
        except Exception as exc:
            attempts.append(
                f"{contracts_path}: {exc.__class__.__name__}: {_safe_exception_message(exc)}"
            )

    raise ProvisioningError(
        "georeferencing_service_unavailable",
        "Could not load the georeferencing domain API required for Earth provisioning.",
        details={
            "policyVersion": PROVISIONING_EARTH_REFERENCE_POLICY_VERSION,
            "attempts": attempts,
        },
        status_code=503,
    )


def _exception_http_status(exc: BaseException, *, default: int) -> int:
    for name in (
        "http_status_code",
        "status_code",
        "default_http_status",
    ):
        value = getattr(exc, name, None)
        try:
            normalized = int(value)
        except Exception:
            continue
        if 400 <= normalized <= 599:
            return normalized
    return int(default)


def _exception_contract_details(exc: BaseException) -> dict[str, Any]:
    """Extract bounded, JSON-safe domain details without exposing tracebacks."""

    result: dict[str, Any] = {}
    raw_details = getattr(exc, "details", None)
    if isinstance(raw_details, Mapping):
        try:
            normalized_details = _json_safe(dict(raw_details))
            if isinstance(normalized_details, dict):
                result["details"] = normalized_details
        except Exception:
            pass

    code = getattr(exc, "code", None) or getattr(exc, "default_code", None)
    if code is not None:
        code_value = getattr(code, "value", code)
        result["code"] = _safe_str(code_value)

    result["exceptionType"] = exc.__class__.__name__
    result["message"] = _safe_exception_message(exc)
    result["httpStatus"] = _exception_http_status(exc, default=500)

    retryable = getattr(exc, "retryable", None)
    if retryable is None:
        retryable = getattr(exc, "default_retryable", None)
    if retryable is not None:
        result["retryable"] = bool(retryable)

    return result


# ---------------------------------------------------------------------------
# World-template and Earth-reference normalization
# ---------------------------------------------------------------------------


def _decimal_token(value: Any, *, field_name: str) -> str:
    if isinstance(value, bool) or value is None:
        raise ProvisioningError(
            "invalid_earth_reference",
            f"{field_name} must be numeric.",
            details={"field": field_name},
            status_code=400,
        )
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ProvisioningError(
            "invalid_earth_reference",
            f"{field_name} must be numeric.",
            details={"field": field_name, "value": _safe_str(value)},
            status_code=400,
        ) from exc
    if not number.is_finite():
        raise ProvisioningError(
            "invalid_earth_reference",
            f"{field_name} must be finite.",
            details={"field": field_name},
            status_code=400,
        )
    normalized = format(number.normalize(), "f")
    if "." in normalized:
        normalized = normalized.rstrip("0").rstrip(".")
    return normalized or "0"


def _reference_crs_value(reference: Mapping[str, Any]) -> Any:
    for key in (
        "crs",
        "crsId",
        "crs_id",
        "sourceCrs",
        "source_crs",
        "coordinateReferenceSystem",
        "coordinate_reference_system",
        "crsDefinition",
        "crs_definition",
    ):
        if key in reference and reference.get(key) not in (None, ""):
            return reference.get(key)
    return None


def _normalize_crs_for_comparison(value: Any) -> Any:
    if isinstance(value, Mapping):
        mapping = dict(value)
        for key in (
            "authorityCode",
            "authority_code",
            "id",
            "code",
            "name",
            "value",
            "definition",
        ):
            candidate = mapping.get(key)
            if candidate not in (None, ""):
                return _safe_str(candidate).upper()
        return _json_safe(mapping)
    return _normalize_required_text(
        value,
        field_name="earthReference.crs",
        max_length=512,
    ).upper()


def _coordinate_mapping(reference: Mapping[str, Any]) -> Mapping[str, Any]:
    for key in (
        "coordinates",
        "coordinate",
        "position",
        "globalCoordinate",
        "global_coordinate",
        "point",
    ):
        value = reference.get(key)
        if isinstance(value, Mapping):
            return value
    return reference


def _coordinate_sequence(reference: Mapping[str, Any]) -> Optional[Sequence[Any]]:
    for key in (
        "coordinates",
        "coordinate",
        "position",
        "globalCoordinate",
        "global_coordinate",
        "point",
    ):
        value = reference.get(key)
        if isinstance(value, Sequence) and not isinstance(
            value,
            (str, bytes, bytearray, Mapping),
        ):
            return value
    return None


def _first_mapping_value(mapping: Mapping[str, Any], names: Sequence[str]) -> Any:
    for name in names:
        if name in mapping and mapping.get(name) not in (None, ""):
            return mapping.get(name)
    return None


def _earth_reference_comparison_contract(reference: Mapping[str, Any]) -> dict[str, Any]:
    crs_raw = _reference_crs_value(reference)
    if crs_raw is None:
        raise ProvisioningError(
            "earth_reference_crs_required",
            "Earth provisioning requires an explicit CRS; it is never inferred from coordinate values.",
            details={"acceptedFields": ["crs", "sourceCrs", "crsDefinition"]},
            status_code=400,
        )

    sequence = _coordinate_sequence(reference)
    coordinate_order = _safe_str(
        _payload_value(
            reference,
            ("coordinateOrder", "coordinate_order", "axisOrder", "axis_order"),
            default="",
        ),
        "",
    )

    if sequence is not None:
        if len(sequence) < 2 or len(sequence) > 3:
            raise ProvisioningError(
                "invalid_earth_reference",
                "Earth reference coordinate arrays must contain two or three values.",
                details={"coordinateCount": len(sequence)},
                status_code=400,
            )
        coordinates = [
            _decimal_token(value, field_name=f"earthReference.coordinates[{index}]")
            for index, value in enumerate(sequence)
        ]
        if not coordinate_order:
            coordinate_order = (
                "longitude-latitude-height"
                if len(coordinates) == 3
                else "longitude-latitude"
            )
    else:
        mapping = _coordinate_mapping(reference)
        longitude = _first_mapping_value(mapping, ("longitude", "lon", "lng"))
        latitude = _first_mapping_value(mapping, ("latitude", "lat"))
        height = _first_mapping_value(
            mapping,
            ("height", "altitude", "elevation", "z"),
        )

        if longitude is not None or latitude is not None:
            if longitude is None or latitude is None:
                raise ProvisioningError(
                    "invalid_earth_reference",
                    "Earth reference requires both longitude and latitude.",
                    status_code=400,
                )
            coordinates = [
                _decimal_token(longitude, field_name="earthReference.longitude"),
                _decimal_token(latitude, field_name="earthReference.latitude"),
            ]
            if height is not None:
                coordinates.append(
                    _decimal_token(height, field_name="earthReference.height")
                )
            if not coordinate_order:
                coordinate_order = (
                    "longitude-latitude-height"
                    if len(coordinates) == 3
                    else "longitude-latitude"
                )
        else:
            x_value = _first_mapping_value(mapping, ("x", "easting", "east"))
            y_value = _first_mapping_value(mapping, ("y", "northing", "north"))
            z_value = _first_mapping_value(mapping, ("z", "height", "elevation"))
            if x_value is None or y_value is None:
                raise ProvisioningError(
                    "earth_reference_coordinates_required",
                    (
                        "Earth provisioning requires explicit coordinates. Supply longitude/latitude, "
                        "x/y, easting/northing or a two/three-value coordinates array."
                    ),
                    status_code=400,
                )
            coordinates = [
                _decimal_token(x_value, field_name="earthReference.x"),
                _decimal_token(y_value, field_name="earthReference.y"),
            ]
            if z_value is not None:
                coordinates.append(
                    _decimal_token(z_value, field_name="earthReference.z")
                )
            if not coordinate_order:
                coordinate_order = "x-y-z" if len(coordinates) == 3 else "x-y"

    return {
        "crs": _normalize_crs_for_comparison(crs_raw),
        "coordinates": coordinates,
        "coordinateOrder": coordinate_order,
        "alwaysXY": _safe_bool(
            _payload_value(reference, ("alwaysXY", "always_xy"), default=True),
            True,
        ),
    }


def _normalize_coordinate_order(
    coordinate_order: Any,
    *,
    coordinate_count: int,
) -> tuple[str, bool]:
    token = _safe_str(coordinate_order).strip().lower()
    token = re.sub(r"[\s_,/]+", "-", token)
    token = re.sub(r"-+", "-", token).strip("-")

    if coordinate_count not in {2, 3}:
        raise ProvisioningError(
            "invalid_earth_reference",
            "Earth reference coordinates must contain two or three values.",
            details={"coordinateCount": coordinate_count},
            status_code=400,
        )

    forward_aliases = {
        "x-y",
        "easting-northing",
        "longitude-latitude",
        "lon-lat",
        "lng-lat",
    }
    reverse_aliases = {
        "y-x",
        "northing-easting",
        "latitude-longitude",
        "lat-lon",
        "lat-lng",
    }

    if coordinate_count == 3:
        forward_aliases.update(
            {
                "x-y-z",
                "easting-northing-height",
                "longitude-latitude-height",
                "lon-lat-height",
                "lng-lat-height",
            }
        )
        reverse_aliases.update(
            {
                "y-x-z",
                "northing-easting-height",
                "latitude-longitude-height",
                "lat-lon-height",
                "lat-lng-height",
            }
        )

    if token in forward_aliases:
        return token, False
    if token in reverse_aliases:
        return token, True

    raise ProvisioningError(
        "invalid_earth_reference_coordinate_order",
        "earthReference.coordinateOrder is not supported.",
        details={
            "coordinateOrder": token or None,
            "coordinateCount": coordinate_count,
            "supportedExamples": [
                "longitude-latitude-height",
                "latitude-longitude-height",
                "x-y-z",
                "y-x-z",
            ],
        },
        status_code=400,
    )


def _reference_version(reference: Mapping[str, Any]) -> int:
    raw = _payload_value(
        reference,
        ("referenceVersion", "reference_version"),
        default=1,
    )
    try:
        version = int(raw)
    except Exception as exc:
        raise ProvisioningError(
            "invalid_earth_reference_version",
            "earthReference.referenceVersion must be a positive integer.",
            details={"referenceVersion": _safe_str(raw)},
            status_code=400,
        ) from exc
    if version <= 0:
        raise ProvisioningError(
            "invalid_earth_reference_version",
            "earthReference.referenceVersion must be greater than zero.",
            details={"referenceVersion": version},
            status_code=400,
        )
    return version


def _is_canonical_global_reference_mapping(value: Mapping[str, Any]) -> bool:
    return all(isinstance(value.get(name), Mapping) for name in ("coordinate", "crs", "grid"))


def _normalize_earth_reference(value: Any) -> tuple[dict[str, Any], str]:
    api = _load_georeferencing_api()
    global_coordinate_type = api["GlobalCoordinate"]
    global_reference_type = api["GlobalReferencePoint"]

    try:
        if isinstance(value, global_reference_type):
            reference = value
        else:
            if not isinstance(value, Mapping):
                raise ProvisioningError(
                    "earth_reference_required",
                    "worldTemplate=earth requires earthReference as a JSON object.",
                    status_code=400,
                )

            try:
                normalized = _json_safe(dict(value))
            except Exception as exc:
                raise ProvisioningError(
                    "invalid_earth_reference",
                    "earthReference is not JSON serializable.",
                    status_code=400,
                ) from exc
            if not isinstance(normalized, dict):
                raise ProvisioningError(
                    "invalid_earth_reference",
                    "earthReference must normalize to a JSON object.",
                    status_code=400,
                )

            if _is_canonical_global_reference_mapping(normalized):
                reference = global_reference_type.from_mapping(normalized)
            else:
                comparison = _earth_reference_comparison_contract(normalized)
                if not bool(comparison.get("alwaysXY", True)):
                    raise ProvisioningError(
                        "invalid_earth_reference_axis_policy",
                        "Earth provisioning requires alwaysXY=true.",
                        details={
                            "alwaysXY": False,
                            "requiredAxisConvention": "x-east-y-up-z-north",
                        },
                        status_code=400,
                    )

                coordinate_values = list(comparison["coordinates"])
                _, reverse_xy = _normalize_coordinate_order(
                    comparison.get("coordinateOrder"),
                    coordinate_count=len(coordinate_values),
                )
                if reverse_xy:
                    coordinate_values[0], coordinate_values[1] = (
                        coordinate_values[1],
                        coordinate_values[0],
                    )

                coordinate = global_coordinate_type.from_values(
                    coordinate_values[0],
                    coordinate_values[1],
                    coordinate_values[2] if len(coordinate_values) == 3 else None,
                )
                crs_input = _reference_crs_value(normalized)
                resolved_crs = api["resolveCrs"](crs_input)
                grid_definition = api["getDefaultEarthGridDefinition"]()

                source = _payload_value(
                    normalized,
                    ("source", "sourceService", "source_service"),
                    default=None,
                    allow_empty=True,
                )
                source_reference_id = _payload_value(
                    normalized,
                    (
                        "sourceReferenceId",
                        "source_reference_id",
                        "referenceId",
                        "reference_id",
                    ),
                    default=None,
                    allow_empty=True,
                )

                reference = global_reference_type(
                    coordinate=coordinate,
                    crs=resolved_crs,
                    grid=grid_definition.grid,
                    reference_version=_reference_version(normalized),
                    source=source,
                    source_reference_id=source_reference_id,
                )

        default_grid = api["getDefaultEarthGridDefinition"]().grid
        if reference.grid != default_grid:
            raise ProvisioningError(
                "earth_reference_grid_conflict",
                "Earth reference uses a grid contract that differs from the configured Earth-v1 grid.",
                details={
                    "expectedGrid": _json_safe(default_grid.to_dict()),
                    "actualGrid": _json_safe(reference.grid.to_dict()),
                },
                status_code=409,
            )

        persistence_payload = reference.to_persistence_dict()
        if not isinstance(persistence_payload, Mapping):
            raise ProvisioningError(
                "invalid_earth_reference",
                "GlobalReferencePoint.to_persistence_dict() did not return an object.",
                status_code=500,
            )

        canonical_payload = _json_safe(dict(persistence_payload))
        if not isinstance(canonical_payload, dict):
            raise ProvisioningError(
                "invalid_earth_reference",
                "Canonical Earth reference is not JSON serializable.",
                status_code=500,
            )

        fingerprint = _safe_str(getattr(reference, "fingerprint", None))
        if not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
            raise ProvisioningError(
                "invalid_earth_reference_fingerprint",
                "Canonical Earth reference did not produce a valid SHA-256 fingerprint.",
                details={"fingerprintLength": len(fingerprint)},
                status_code=500,
            )

        return canonical_payload, fingerprint

    except ProvisioningError:
        raise
    except Exception as exc:
        domain = _exception_contract_details(exc)
        raise ProvisioningError(
            "invalid_earth_reference",
            "Earth reference could not be normalized into the canonical GlobalReferencePoint contract.",
            details={
                "policyVersion": PROVISIONING_EARTH_REFERENCE_POLICY_VERSION,
                "domainError": domain,
            },
            status_code=_exception_http_status(exc, default=400),
        ) from exc


def _world_template_contract(template_id: str) -> dict[str, Any]:
    """
    Resolve one immutable provider/template contract from isolated config namespaces.

    Flat uses ``VECTOPLAN_CHUNK_DEFAULT_*`` because it is the service default.
    Earth uses ``VECTOPLAN_CHUNK_EARTH_*`` exclusively for geometry and vertical
    bounds. Generic Flat defaults must never leak into an Earth WorldInstance.

    Earth ``spawn_x/y/z`` remain placeholders in this contract. Their persisted
    values are derived from the canonical GlobalReferencePoint by
    ``WorldInstance.create_earth_spawn`` and are synchronized from the precise
    local-metric spawn fields rather than from configuration.
    """

    defaults = dict(_default_world_template_contracts()[template_id])

    if template_id == WORLD_TEMPLATE_FLAT:
        defaults.update(
            {
                "generator_type": _config_str(
                    "VECTOPLAN_CHUNK_DEFAULT_GENERATOR_TYPE",
                    str(defaults["generator_type"]),
                ),
                "generator_version": _config_str(
                    "VECTOPLAN_CHUNK_DEFAULT_GENERATOR_VERSION",
                    str(defaults["generator_version"]),
                ),
                "projection_type": _config_str(
                    "VECTOPLAN_CHUNK_DEFAULT_PROJECTION_TYPE",
                    str(defaults["projection_type"]),
                ),
                "topology_type": _config_str(
                    "VECTOPLAN_CHUNK_DEFAULT_TOPOLOGY_TYPE",
                    str(defaults["topology_type"]),
                ),
                "coordinate_system": _config_str(
                    "VECTOPLAN_CHUNK_DEFAULT_COORDINATE_SYSTEM",
                    str(defaults["coordinate_system"]),
                ),
                "seed": _config_str(
                    "VECTOPLAN_CHUNK_DEFAULT_SEED",
                    str(defaults["seed"]),
                ),
                "chunk_size": _config_int(
                    "VECTOPLAN_CHUNK_DEFAULT_CHUNK_SIZE",
                    int(defaults["chunk_size"]),
                ),
                "cell_size": _config_float(
                    "VECTOPLAN_CHUNK_DEFAULT_CELL_SIZE",
                    float(defaults["cell_size"]),
                ),
                "surface_y": _config_int(
                    "VECTOPLAN_CHUNK_DEFAULT_SURFACE_Y",
                    int(defaults["surface_y"]),
                ),
                "min_y": _config_int(
                    "VECTOPLAN_CHUNK_DEFAULT_MIN_Y",
                    int(defaults["min_y"]),
                ),
                "max_y": _config_int(
                    "VECTOPLAN_CHUNK_DEFAULT_MAX_Y",
                    int(defaults["max_y"]),
                ),
                "spawn_x": _config_int(
                    "VECTOPLAN_CHUNK_DEFAULT_SPAWN_X",
                    int(defaults["spawn_x"]),
                ),
                "spawn_y": _config_int(
                    "VECTOPLAN_CHUNK_DEFAULT_SPAWN_Y",
                    int(defaults["spawn_y"]),
                ),
                "spawn_z": _config_int(
                    "VECTOPLAN_CHUNK_DEFAULT_SPAWN_Z",
                    int(defaults["spawn_z"]),
                ),
                "spawn_yaw": _config_float(
                    "VECTOPLAN_CHUNK_DEFAULT_SPAWN_YAW",
                    float(defaults["spawn_yaw"]),
                ),
                "spawn_pitch": _config_float(
                    "VECTOPLAN_CHUNK_DEFAULT_SPAWN_PITCH",
                    float(defaults["spawn_pitch"]),
                ),
            }
        )
    elif template_id == WORLD_TEMPLATE_EARTH:
        defaults.update(
            {
                "generator_type": _config_str(
                    "VECTOPLAN_CHUNK_EARTH_GENERATOR_TYPE",
                    str(defaults["generator_type"]),
                ),
                "generator_version": _config_str(
                    "VECTOPLAN_CHUNK_EARTH_GENERATOR_VERSION",
                    str(defaults["generator_version"]),
                ),
                "projection_type": _config_str(
                    "VECTOPLAN_CHUNK_EARTH_PROJECTION_TYPE",
                    str(defaults["projection_type"]),
                ),
                "topology_type": _config_str(
                    "VECTOPLAN_CHUNK_EARTH_TOPOLOGY_TYPE",
                    str(defaults["topology_type"]),
                ),
                "coordinate_system": _config_str(
                    "VECTOPLAN_CHUNK_EARTH_COORDINATE_SYSTEM",
                    str(defaults["coordinate_system"]),
                ),
                "seed": _config_str(
                    "VECTOPLAN_CHUNK_EARTH_SEED",
                    str(defaults["seed"]),
                ),
                "chunk_size": _config_int(
                    "VECTOPLAN_CHUNK_EARTH_CHUNK_SIZE",
                    int(defaults["chunk_size"]),
                ),
                "cell_size": _config_float(
                    "VECTOPLAN_CHUNK_EARTH_CELL_SIZE",
                    float(defaults["cell_size"]),
                ),
                "surface_y": _config_int(
                    "VECTOPLAN_CHUNK_EARTH_SURFACE_Y",
                    int(defaults["surface_y"]),
                ),
                "min_y": _config_int(
                    "VECTOPLAN_CHUNK_EARTH_MIN_Y",
                    int(defaults["min_y"]),
                ),
                "max_y": _config_int(
                    "VECTOPLAN_CHUNK_EARTH_MAX_Y",
                    int(defaults["max_y"]),
                ),
                "spawn_yaw": _config_float(
                    "VECTOPLAN_CHUNK_EARTH_SPAWN_YAW",
                    float(defaults["spawn_yaw"]),
                ),
                "spawn_pitch": _config_float(
                    "VECTOPLAN_CHUNK_EARTH_SPAWN_PITCH",
                    float(defaults["spawn_pitch"]),
                ),
            }
        )
    else:  # Defensive guard for future callers bypassing template normalization.
        raise ProvisioningError(
            "unsupported_world_template",
            "Unsupported world template contract requested.",
            details={"worldTemplate": template_id},
            status_code=400,
        )

    if defaults["chunk_size"] <= 0:
        raise ProvisioningError(
            "invalid_world_template_configuration",
            "Configured chunk_size must be greater than zero.",
            details={"worldTemplate": template_id, "field": "chunk_size"},
            status_code=500,
        )
    if defaults["cell_size"] <= 0:
        raise ProvisioningError(
            "invalid_world_template_configuration",
            "Configured cell_size must be greater than zero.",
            details={"worldTemplate": template_id, "field": "cell_size"},
            status_code=500,
        )
    if defaults["min_y"] > defaults["max_y"]:
        raise ProvisioningError(
            "invalid_world_template_configuration",
            "Configured min_y must not exceed max_y.",
            details={
                "worldTemplate": template_id,
                "minY": defaults["min_y"],
                "maxY": defaults["max_y"],
            },
            status_code=500,
        )
    return defaults

def _resolve_world_template_selection(payload: Mapping[str, Any]) -> WorldTemplateSelection:
    raw_template = _payload_value(
        payload,
        WORLD_TEMPLATE_PAYLOAD_FIELDS,
        default=WORLD_TEMPLATE_FLAT,
    )
    template_id = _normalize_world_template(raw_template)

    supplied_provider = _payload_value(
        payload,
        WORLD_PROVIDER_PAYLOAD_FIELDS,
        default=None,
    )
    if supplied_provider not in (None, ""):
        normalized_provider = _normalize_world_template(supplied_provider)
        if normalized_provider != template_id:
            raise ProvisioningError(
                "world_template_provider_conflict",
                "Client-supplied provider identity conflicts with worldTemplate.",
                details={
                    "worldTemplate": template_id,
                    "providerValue": _safe_str(supplied_provider),
                },
                status_code=400,
            )

    earth_reference_value = _payload_value(
        payload,
        EARTH_REFERENCE_PAYLOAD_FIELDS,
        default=None,
        allow_empty=True,
    )
    contract = _world_template_contract(template_id)

    if template_id == WORLD_TEMPLATE_FLAT:
        if earth_reference_value not in (None, "", {}):
            raise ProvisioningError(
                "earth_reference_not_allowed_for_flat",
                "earthReference is only valid when worldTemplate=earth.",
                status_code=400,
            )
        return WorldTemplateSelection(
            template_id=template_id,
            contract=MappingProxyType(contract),
        )

    earth_reference, fingerprint = _normalize_earth_reference(
        earth_reference_value
    )
    return WorldTemplateSelection(
        template_id=template_id,
        contract=MappingProxyType(contract),
        earth_reference=earth_reference,
        earth_reference_fingerprint=fingerprint,
    )


# ---------------------------------------------------------------------------
# Model and SQLAlchemy helpers
# ---------------------------------------------------------------------------


def _model_columns(model_or_instance: Any) -> set[str]:
    model = model_or_instance if isinstance(model_or_instance, type) else type(model_or_instance)
    try:
        table = getattr(model, "__table__", None)
        columns = getattr(table, "columns", None)
        if columns is None:
            return set()
        return {str(column.name) for column in columns}
    except Exception:
        return set()


def _model_relationships(model_or_instance: Any) -> set[str]:
    model = model_or_instance if isinstance(model_or_instance, type) else type(model_or_instance)
    try:
        mapper = getattr(model, "__mapper__", None)
        relationships = getattr(mapper, "relationships", None)
        if relationships is None:
            return set()
        return {str(relationship.key) for relationship in relationships}
    except Exception:
        return set()


def _supports_attr(model_or_instance: Any, name: str) -> bool:
    if model_or_instance is None:
        return False
    if name in _model_columns(model_or_instance):
        return True
    if name in _model_relationships(model_or_instance):
        return True
    try:
        return hasattr(model_or_instance, name)
    except Exception:
        return False


def _get_attr(instance: Any, name: str, default: Any = None) -> Any:
    try:
        return getattr(instance, name, default)
    except Exception:
        return default


def _set_if_changed(instance: Any, name: str, value: Any) -> bool:
    if not _supports_attr(instance, name):
        return False
    current = _get_attr(instance, name, _MISSING)
    if current is not _MISSING and current == value:
        return False
    try:
        setattr(instance, name, value)
        return True
    except Exception as exc:
        raise ProvisioningError(
            "model_field_assignment_failed",
            f"Could not assign {type(instance).__name__}.{name}.",
            details={"model": type(instance).__name__, "field": name},
            status_code=500,
        ) from exc


def _set_first_supported_if_changed(
    instance: Any,
    names: Sequence[str],
    value: Any,
) -> bool:
    for name in names:
        if _supports_attr(instance, name):
            return _set_if_changed(instance, name, value)
    return False


def _set_json_if_changed(
    instance: Any,
    name: str,
    value: Mapping[str, Any],
) -> bool:
    if not _supports_attr(instance, name):
        return False
    normalized = _json_safe(dict(value))
    current = _get_attr(instance, name, None)
    if isinstance(current, str):
        try:
            current = json.loads(current)
        except Exception:
            current = {}
    current_normalized = _json_safe(current or {})
    if current_normalized == normalized:
        return False
    try:
        if isinstance(_get_attr(instance, name, None), str):
            setattr(instance, name, _canonical_json(normalized))
        else:
            setattr(instance, name, normalized)
        return True
    except Exception as exc:
        raise ProvisioningError(
            "model_field_assignment_failed",
            f"Could not assign {type(instance).__name__}.{name}.",
            details={"model": type(instance).__name__, "field": name},
            status_code=500,
        ) from exc


def _merge_json_if_changed(
    instance: Any,
    name: str,
    patch: Mapping[str, Any],
) -> bool:
    if not _supports_attr(instance, name):
        return False
    current = _get_attr(instance, name, None)
    if isinstance(current, str):
        try:
            current = json.loads(current)
        except Exception:
            current = {}
    merged = dict(current) if isinstance(current, Mapping) else {}
    changed = False
    for key, value in patch.items():
        safe_value = _json_safe(value)
        if merged.get(str(key), _MISSING) != safe_value:
            merged[str(key)] = safe_value
            changed = True
    if not changed:
        return False
    return _set_json_if_changed(instance, name, merged)


def _touch_existing(instance: Any, *, actor_user_id: Optional[str]) -> None:
    touch = _get_attr(instance, "touch", None)
    if callable(touch):
        try:
            touch(updated_by_user_id=actor_user_id)
            return
        except TypeError:
            try:
                touch()
                if actor_user_id is not None:
                    _set_if_changed(instance, "updated_by_user_id", actor_user_id)
                return
            except Exception:
                pass
        except Exception:
            pass

    if _supports_attr(instance, "revision"):
        revision = _safe_int(_get_attr(instance, "revision", 1), 1)
        _set_if_changed(instance, "revision", max(1, revision + 1))
    if actor_user_id is not None:
        _set_if_changed(instance, "updated_by_user_id", actor_user_id)
    _set_if_changed(instance, "updated_at", utcnow())


def _is_deleted(instance: Any) -> bool:
    if instance is None:
        return False
    try:
        is_deleted_value = getattr(instance, "is_deleted")
        if isinstance(is_deleted_value, bool):
            return is_deleted_value
    except Exception:
        pass
    return bool(
        _get_attr(instance, "deleted_at", None) is not None
        or _safe_str(_get_attr(instance, "status", "")).lower() == "deleted"
    )


def _restore_lifecycle(instance: Any, *, actor_user_id: Optional[str]) -> bool:
    changed = False
    changed = _set_if_changed(instance, "status", "active") or changed
    if _supports_attr(instance, "deleted_at"):
        changed = _set_if_changed(instance, "deleted_at", None) or changed
    if _supports_attr(instance, "archived_at"):
        changed = _set_if_changed(instance, "archived_at", None) or changed
    if changed:
        _touch_existing(instance, actor_user_id=actor_user_id)
    return changed


def _database_id(instance: Any, *, field_name: str) -> int:
    value = _get_attr(instance, "id", None)
    try:
        normalized = int(value)
    except Exception as exc:
        raise ProvisioningError(
            "model_database_id_missing",
            f"{field_name} has no persistent database ID after flush.",
            details={"model": type(instance).__name__},
            status_code=500,
        ) from exc
    if normalized <= 0:
        raise ProvisioningError(
            "model_database_id_missing",
            f"{field_name} has no persistent database ID after flush.",
            details={"model": type(instance).__name__},
            status_code=500,
        )
    return normalized


def _extract_public_id(
    instance: Any,
    names: Sequence[str],
    fallback: Optional[str] = None,
) -> Optional[str]:
    for name in names:
        value = _get_attr(instance, name, None)
        if value not in (None, ""):
            return _safe_str(value)
    return fallback


def _assign_parent_reference(
    child: Any,
    *,
    parent: Any,
    db_id_fields: Sequence[str],
    relation_fields: Sequence[str],
) -> bool:
    parent_db_id = _database_id(parent, field_name="parent")
    for field_name in db_id_fields:
        if _supports_attr(child, field_name):
            return _set_if_changed(child, field_name, parent_db_id)
    for field_name in relation_fields:
        if _supports_attr(child, field_name):
            return _set_if_changed(child, field_name, parent)
    raise ProvisioningError(
        "model_parent_reference_missing",
        f"{type(child).__name__} cannot reference {type(parent).__name__}.",
        details={
            "child": type(child).__name__,
            "parent": type(parent).__name__,
            "acceptedFields": list(db_id_fields) + list(relation_fields),
        },
        status_code=500,
    )


def _ensure_session(session: Any | None) -> Any:
    if session is not None:
        return session
    if db is None:
        raise ProvisioningError(
            "database_unavailable",
            "SQLAlchemy db extension is not available.",
            status_code=500,
        )
    try:
        return db.session
    except Exception as exc:
        raise ProvisioningError(
            "database_session_unavailable",
            "Could not access SQLAlchemy session.",
            status_code=500,
        ) from exc


def _ensure_models_available() -> None:
    missing: list[str] = []
    for name, model in (
        ("Project", Project),
        ("Universe", Universe),
        ("WorldInstance", WorldInstance),
    ):
        if model is None:
            missing.append(name)
    if missing:
        raise ProvisioningError(
            "models_unavailable",
            "Required provisioning models are unavailable.",
            details={"missing": missing},
            status_code=500,
        )


def _query_one_or_none(query: Any, *, entity_name: str) -> Any | None:
    try:
        return query.one_or_none()
    except Exception as exc:
        raise ProvisioningError(
            "provisioning_lookup_failed",
            f"Could not query {entity_name}.",
            details={"entity": entity_name, "error": _safe_exception_message(exc)},
            status_code=500,
        ) from exc


def _disable_eagerloads_for_lock(query: Any) -> Any:
    """
    Return a query whose implicit ORM eager joins are disabled for row locking.

    PostgreSQL rejects ``FOR UPDATE`` when SQLAlchemy adds a ``LEFT OUTER JOIN``
    for a relationship configured with ``lazy="joined"`` and the lock is applied
    to the complete statement. Provisioning lookups only need the base entity,
    so suppressing eager loaders is both safer and cheaper. Relationships remain
    available through normal lazy loading after the row has been resolved.
    """

    enable_eagerloads = getattr(query, "enable_eagerloads", None)
    if not callable(enable_eagerloads):
        return query

    try:
        return enable_eagerloads(False)
    except Exception:
        # Compatibility fallback for lightweight query doubles and older ORM
        # wrappers. The targeted ``OF <model>`` lock below still prevents
        # PostgreSQL from trying to lock nullable joined tables.
        return query


def _with_for_update(
    query: Any,
    *,
    model: Any | None = None,
    enabled: bool = True,
) -> Any:
    """
    Apply a targeted pessimistic row lock to the base provisioning entity.

    ``with_for_update(of=model)`` compiles to ``FOR UPDATE OF <base_table>`` on
    PostgreSQL. Combined with disabled eager loading this avoids the PostgreSQL
    error ``FOR UPDATE cannot be applied to the nullable side of an outer join``.

    The fallbacks preserve compatibility with query doubles and SQLAlchemy-like
    wrappers that do not accept the ``of`` keyword.
    """

    if not enabled:
        return query

    prepared_query = _disable_eagerloads_for_lock(query)
    with_for_update = getattr(prepared_query, "with_for_update", None)
    if not callable(with_for_update):
        return prepared_query

    lock_targets: list[Any] = []
    if model is not None:
        lock_targets.append(model)
        table = getattr(model, "__table__", None)
        if table is not None and table is not model:
            lock_targets.append(table)

    for target in lock_targets:
        try:
            return with_for_update(of=target)
        except TypeError:
            # The wrapper does not support SQLAlchemy's ``of`` keyword.
            break
        except Exception:
            # A model target may not be accepted by a custom wrapper; try the
            # mapped table and finally the legacy no-argument form.
            continue

    try:
        return with_for_update()
    except Exception:
        # Maintain the previous best-effort locking behavior. The lookup still
        # runs with eager joins disabled where the query API supports it.
        return prepared_query


def _query_by_filters(
    session: Any,
    model: Any,
    filters: Mapping[str, Any],
    *,
    entity_name: str,
    lock: bool = False,
) -> Any | None:
    if model is None:
        return None
    supported = {
        key: value
        for key, value in filters.items()
        if value is not None and key in _model_columns(model)
    }
    if not supported:
        return None
    try:
        query = session.query(model)
        for key, value in supported.items():
            query = query.filter(getattr(model, key) == value)
        query = _with_for_update(
            query,
            model=model,
            enabled=lock,
        )
    except Exception as exc:
        raise ProvisioningError(
            "provisioning_lookup_failed",
            f"Could not build {entity_name} lookup.",
            details={"entity": entity_name, "filters": _json_safe(supported)},
            status_code=500,
        ) from exc
    return _query_one_or_none(query, entity_name=entity_name)


def _flush(session: Any, *, objects: Optional[Sequence[Any]] = None) -> None:
    try:
        if objects:
            try:
                session.flush(list(objects))
                return
            except TypeError:
                pass
        session.flush()
    except Exception:
        raise


def _instantiate_model(model: Any, *, model_name: str) -> Any:
    try:
        return model()
    except Exception as exc:
        raise ProvisioningError(
            "model_instance_create_failed",
            f"Could not instantiate {model_name}.",
            details={"model": model_name, "error": _safe_exception_message(exc)},
            status_code=500,
        ) from exc


def _filter_factory_kwargs(factory: Callable[..., Any], kwargs: Mapping[str, Any]) -> dict[str, Any]:
    try:
        signature = inspect.signature(factory)
    except Exception:
        return dict(kwargs)
    has_var_kwargs = any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    )
    if has_var_kwargs:
        return dict(kwargs)
    accepted = {
        name
        for name, parameter in signature.parameters.items()
        if parameter.kind
        in {
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        }
    }
    return {key: value for key, value in kwargs.items() if key in accepted}


def _call_factory_candidates(
    factory: Callable[..., Any],
    candidates: Sequence[tuple[Sequence[Any], Mapping[str, Any]]],
    *,
    factory_name: str,
) -> Any | None:
    try:
        signature = inspect.signature(factory)
    except Exception:
        signature = None

    for args, raw_kwargs in candidates:
        kwargs = _filter_factory_kwargs(factory, raw_kwargs)
        if signature is not None:
            try:
                signature.bind(*args, **kwargs)
            except TypeError:
                continue
        try:
            return factory(*args, **kwargs)
        except TypeError:
            if signature is None:
                continue
            raise
        except Exception as exc:
            domain_error = _exception_contract_details(exc)
            raise ProvisioningError(
                "model_factory_failed",
                f"{factory_name} failed.",
                details={
                    "factory": factory_name,
                    "error": _safe_exception_message(exc),
                    "exceptionType": exc.__class__.__name__,
                    "domainError": domain_error,
                },
                status_code=_exception_http_status(
                    exc,
                    default=400 if isinstance(exc, ValueError) else 500,
                ),
            ) from exc
    return None


def _serialize_model(instance: Any, fields: Sequence[str]) -> dict[str, Any]:
    if instance is None:
        return {}
    result: dict[str, Any] = {}
    for field_name in fields:
        if field_name not in _model_columns(instance):
            continue
        value = _get_attr(instance, field_name, None)
        result[field_name] = _json_safe(value)
    return result

# ---------------------------------------------------------------------------
# Pure ID, route and metadata builders
# ---------------------------------------------------------------------------


def _prefixed_id(prefix: str, external_id: str, *, fallback: str) -> str:
    normalized_prefix = _safe_str(prefix, fallback)
    safe_external = re.sub(r"[^A-Za-z0-9_.:-]+", "-", external_id).strip("-._:")
    safe_external = safe_external or fallback
    digest = _short_hash(external_id, length=12)
    max_external_length = max(
        1,
        MAX_PUBLIC_ID_LENGTH - len(normalized_prefix) - len(digest) - 1,
    )
    candidate = f"{normalized_prefix}{safe_external[:max_external_length]}_{digest}"
    return _normalize_public_id(candidate, field_name="generated_id")


def _resolve_provisioning_ids(
    external_app_project_id: str,
    payload: Mapping[str, Any],
) -> ProvisioningIds:
    project_value = _payload_value(
        payload,
        ("chunkProjectId", "chunk_project_id", "projectId", "project_id"),
        default=None,
    )
    universe_value = _payload_value(
        payload,
        ("chunkUniverseId", "chunk_universe_id", "universeId", "universe_id"),
        default=None,
    )
    world_value = _payload_value(
        payload,
        ("chunkWorldId", "chunk_world_id", "worldId", "world_id"),
        default=None,
    )

    project_explicit = project_value not in (None, "")
    universe_explicit = universe_value not in (None, "")
    world_explicit = world_value not in (None, "")

    project_id = (
        _normalize_public_id(project_value, field_name="chunkProjectId")
        if project_explicit
        else _prefixed_id(
            _config_str(
                "VECTOPLAN_CHUNK_PROJECT_PROVISIONING_PROJECT_ID_PREFIX",
                DEFAULT_PROJECT_PREFIX,
            ),
            external_app_project_id,
            fallback="chk_prj_",
        )
    )
    universe_id = (
        _normalize_public_id(universe_value, field_name="chunkUniverseId")
        if universe_explicit
        else _prefixed_id(
            _config_str(
                "VECTOPLAN_CHUNK_PROJECT_PROVISIONING_UNIVERSE_ID_PREFIX",
                DEFAULT_UNIVERSE_PREFIX,
            ),
            external_app_project_id,
            fallback="chk_uni_",
        )
    )
    world_id = (
        _normalize_public_id(world_value, field_name="chunkWorldId")
        if world_explicit
        else _normalize_public_id(
            _config_str(
                "VECTOPLAN_CHUNK_PROJECT_PROVISIONING_DEFAULT_WORLD_ID",
                DEFAULT_WORLD_ID,
            ),
            field_name="configuredDefaultWorldId",
        )
    )

    if world_id in SUPPORTED_WORLD_TEMPLATES:
        raise ProvisioningError(
            "provider_id_used_as_world_id",
            "flat and earth are template/provider IDs and cannot be used as concrete world IDs.",
            details={"worldId": world_id},
            status_code=400,
        )

    return ProvisioningIds(
        chunk_project_id=project_id,
        chunk_universe_id=universe_id,
        chunk_world_id=world_id,
        project_explicit=project_explicit,
        universe_explicit=universe_explicit,
        world_explicit=world_explicit,
    )


def _derive_existing_child_ids(
    project: Any,
    ids: ProvisioningIds,
) -> ProvisioningIds:
    universe_id = ids.chunk_universe_id
    world_id = ids.chunk_world_id

    existing_universe_id = _safe_str(
        _get_attr(project, "default_universe_id", None),
        "",
    )
    existing_world_id = _safe_str(
        _get_attr(project, "spawn_world_id", None)
        or _get_attr(project, "default_world_id", None),
        "",
    )

    if ids.universe_explicit and existing_universe_id and universe_id != existing_universe_id:
        raise ProvisioningError(
            "chunk_universe_id_conflict",
            "Explicit chunkUniverseId conflicts with the linked project's existing default universe.",
            details={
                "requested": universe_id,
                "existing": existing_universe_id,
            },
            status_code=409,
        )
    if not ids.universe_explicit and existing_universe_id:
        universe_id = existing_universe_id

    if ids.world_explicit and existing_world_id and world_id != existing_world_id:
        raise ProvisioningError(
            "chunk_world_id_conflict",
            "Explicit chunkWorldId conflicts with the linked project's existing default world.",
            details={
                "requested": world_id,
                "existing": existing_world_id,
            },
            status_code=409,
        )
    if not ids.world_explicit and existing_world_id:
        world_id = existing_world_id

    return ProvisioningIds(
        chunk_project_id=ids.chunk_project_id,
        chunk_universe_id=universe_id,
        chunk_world_id=world_id,
        project_explicit=ids.project_explicit,
        universe_explicit=ids.universe_explicit,
        world_explicit=ids.world_explicit,
    )


def _build_route_hints(chunk_project_id: str, chunk_world_id: str) -> dict[str, str]:
    project_base = f"/projects/{chunk_project_id}"
    world_base = f"{project_base}/worlds/{chunk_world_id}"
    return {
        "project": project_base,
        "access": f"{project_base}/access",
        "roles": f"{project_base}/roles",
        "groups": f"{project_base}/groups",
        "bootstrap": f"{project_base}/bootstrap",
        "worlds": f"{project_base}/worlds",
        "world": world_base,
        "blocks": f"{world_base}/blocks",
        "chunk": f"{world_base}/chunks",
        "chunks": f"{world_base}/chunks",
        "chunksBatch": f"{world_base}/chunks/batch",
        "commands": f"{world_base}/commands",
    }


def _project_name(payload: Mapping[str, Any], external_app_project_id: str) -> str:
    return _normalize_required_text(
        _payload_value(
            payload,
            ("name", "projectName", "project_name", "title", "displayName"),
            default=f"Chunk Project for {external_app_project_id}",
        ),
        field_name="name",
        max_length=MAX_NAME_LENGTH,
    )


def _project_description(payload: Mapping[str, Any]) -> Optional[str]:
    return _normalize_optional_text(
        _payload_value(
            payload,
            ("description", "projectDescription", "project_description", "summary"),
            default=None,
            allow_empty=True,
        ),
        field_name="description",
        max_length=MAX_DESCRIPTION_LENGTH,
    )


def _external_url(payload: Mapping[str, Any]) -> Optional[str]:
    return _normalize_optional_text(
        _payload_value(
            payload,
            ("externalUrl", "external_url", "appProjectUrl", "app_project_url"),
            default=None,
            allow_empty=True,
        ),
        field_name="externalUrl",
        max_length=MAX_EXTERNAL_URL_LENGTH,
    )


def _payload_metadata(payload: Mapping[str, Any]) -> dict[str, Any]:
    value = _payload_value(
        payload,
        ("metadata", "metadataJson", "metadata_json", "projectMetadata", "project_metadata"),
        default={},
        allow_empty=True,
    )
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ProvisioningError(
            "invalid_provisioning_payload",
            "metadata must be a JSON object.",
            details={"field": "metadata"},
            status_code=400,
        )
    normalized = _json_safe(dict(value))
    return normalized if isinstance(normalized, dict) else {}


def _build_project_metadata(
    *,
    external_app_project_id: str,
    owner_user_id: str,
    ids: ProvisioningIds,
    selection: WorldTemplateSelection,
    payload: Mapping[str, Any],
    created: bool,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "schemaVersion": PROVISIONING_SCHEMA_VERSION,
        "sourceService": _config_str(
            "VECTOPLAN_CHUNK_PROJECT_PROVISIONING_SOURCE_SERVICE",
            DEFAULT_SOURCE_SERVICE,
        ),
        "externalAppProjectId": external_app_project_id,
        "ownerUserId": owner_user_id,
        "chunkProjectId": ids.chunk_project_id,
        "chunkUniverseId": ids.chunk_universe_id,
        "chunkWorldId": ids.chunk_world_id,
        "worldTemplate": selection.template_id,
        "providerId": selection.contract.get("provider_id"),
        "providerWorldId": selection.contract.get("provider_world_id"),
        "earthReferenceFingerprint": selection.earth_reference_fingerprint,
        "routeHints": _build_route_hints(ids.chunk_project_id, ids.chunk_world_id),
        "appMetadata": _payload_metadata(payload),
    }
    if created:
        metadata["provisionedAt"] = utcnow().isoformat()
    return metadata


def _build_universe_metadata(
    *,
    ids: ProvisioningIds,
    selection: WorldTemplateSelection,
    created: bool,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "schemaVersion": PROVISIONING_SCHEMA_VERSION,
        "chunkProjectId": ids.chunk_project_id,
        "chunkUniverseId": ids.chunk_universe_id,
        "chunkWorldId": ids.chunk_world_id,
        "worldTemplate": selection.template_id,
    }
    if created:
        metadata["provisionedAt"] = utcnow().isoformat()
    return metadata


def _build_world_metadata(
    *,
    ids: ProvisioningIds,
    selection: WorldTemplateSelection,
    created: bool,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "schemaVersion": PROVISIONING_SCHEMA_VERSION,
        "chunkProjectId": ids.chunk_project_id,
        "chunkUniverseId": ids.chunk_universe_id,
        "chunkWorldId": ids.chunk_world_id,
        "worldTemplate": selection.template_id,
        "templateId": selection.contract.get("template_id"),
        "providerId": selection.contract.get("provider_id"),
        "providerWorldId": selection.contract.get("provider_world_id"),
        "earthReferenceFingerprint": selection.earth_reference_fingerprint,
        "blockRegistryId": _config_str(
            "VECTOPLAN_CHUNK_DEFAULT_BLOCK_REGISTRY_ID",
            DEFAULT_BLOCK_REGISTRY_ID,
        ),
        "blockRegistryVersion": _config_str(
            "VECTOPLAN_CHUNK_DEFAULT_BLOCK_REGISTRY_VERSION",
            DEFAULT_BLOCK_REGISTRY_VERSION,
        ),
    }
    if created:
        metadata["provisionedAt"] = utcnow().isoformat()
    return metadata


# ---------------------------------------------------------------------------
# Project Access adapter
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _load_project_access_api() -> Mapping[str, Any]:
    import_errors: list[str] = []
    required_exports = (
        "ensure_project_access_initialized",
        "ProjectAccessServiceError",
    )
    for import_path in (
        "src.project_access",
        "project_access",
        "src.project_access.service",
    ):
        try:
            module = __import__(import_path, fromlist=required_exports)
        except Exception as exc:
            import_errors.append(
                f"{import_path}: {exc.__class__.__name__}: {_safe_exception_message(exc)}"
            )
            continue
        ensure_function = getattr(module, "ensure_project_access_initialized", None)
        error_type = getattr(module, "ProjectAccessServiceError", RuntimeError)
        if not callable(ensure_function):
            import_errors.append(
                f"{import_path}: ensure_project_access_initialized is unavailable"
            )
            continue
        return MappingProxyType(
            {
                "ensure": ensure_function,
                "errorType": error_type,
                "module": module,
                "moduleName": _safe_str(getattr(module, "__name__", None)),
                "modulePath": _safe_str(getattr(module, "__file__", None)),
            }
        )
    raise ProvisioningError(
        "project_access_service_unavailable",
        "Could not import the project access service.",
        details={"errors": import_errors},
        status_code=500,
    )


def _initialize_project_access(
    *,
    project: Any,
    owner_user_id: str,
    actor_user_id: str,
    session: Any,
    allow_owner_replacement: bool,
    require_access: bool,
) -> tuple[dict[str, Any], bool, list[ProvisioningIssue]]:
    warnings: list[ProvisioningIssue] = []
    try:
        api = _load_project_access_api()
    except ProvisioningError as exc:
        if require_access:
            raise
        warnings.append(
            ProvisioningIssue(
                code=exc.code,
                message=exc.message,
                details=exc.details,
            )
        )
        return {
            "ok": False,
            "accessInitialized": False,
            "skipped": True,
            "reason": exc.message,
        }, False, warnings

    ensure_function = api["ensure"]
    error_type = api["errorType"]
    try:
        result = ensure_function(
            project=project,
            owner_user_id=owner_user_id,
            actor_user_id=actor_user_id,
            session=session,
            synchronize_default_roles=True,
            restore_deleted_roles=True,
            replace_existing_owner=allow_owner_replacement,
            allow_missing_owner=False,
            lock_project=True,
            flush=True,
        )
    except error_type as exc:  # type: ignore[misc]
        code = _safe_str(getattr(exc, "code", None), "project_access_initialization_failed")
        details = getattr(exc, "details", None)
        status_code = 409 if "conflict" in code or "owner" in code else 400
        raise ProvisioningError(
            code,
            _safe_exception_message(exc),
            details=details if isinstance(details, Mapping) else {},
            status_code=status_code,
        ) from exc
    except Exception as exc:
        raise ProvisioningError(
            "project_access_initialization_failed",
            "Project access initialization failed.",
            details={
                "exceptionType": exc.__class__.__name__,
                "error": _safe_exception_message(exc),
            },
            status_code=500,
        ) from exc

    to_dict = getattr(result, "to_dict", None)
    if callable(to_dict):
        try:
            payload = to_dict(include_internal=False, include_metadata=True)
        except TypeError:
            payload = to_dict()
    else:
        payload = _json_safe(result)
    payload_dict = dict(payload) if isinstance(payload, Mapping) else {}
    project_db_id = _database_id(project, field_name="Project")
    payload_dict["projectDbId"] = project_db_id
    payload_dict["projectId"] = (
        _extract_public_id(project, PROJECT_ID_FIELDS)
        or _safe_str(payload_dict.get("projectId"))
        or None
    )
    changed = _safe_bool(getattr(result, "changed", payload_dict.get("changed")), False)
    access_initialized = _safe_bool(
        getattr(result, "access_initialized", payload_dict.get("accessInitialized")),
        False,
    )
    if require_access and not access_initialized:
        raise ProvisioningError(
            "project_access_not_initialized",
            "Project access service returned an incomplete role/owner state.",
            details={"access": payload_dict},
            status_code=500,
        )
    return payload_dict, changed, warnings


# ---------------------------------------------------------------------------
# Scoped lookups and identity conflict checks
# ---------------------------------------------------------------------------


def _find_project(
    session: Any,
    *,
    external_app_project_id: str,
    chunk_project_id: str,
    lock: bool = True,
) -> Any | None:
    by_external = None
    by_project_id = None
    if "external_app_project_id" in _model_columns(Project):
        by_external = _query_by_filters(
            session,
            Project,
            {"external_app_project_id": external_app_project_id},
            entity_name="Project",
            lock=lock,
        )
    if "project_id" in _model_columns(Project):
        by_project_id = _query_by_filters(
            session,
            Project,
            {"project_id": chunk_project_id},
            entity_name="Project",
            lock=lock,
        )

    if by_external is not None and by_project_id is not None:
        if _get_attr(by_external, "id", None) != _get_attr(by_project_id, "id", None):
            raise ProvisioningError(
                "project_identity_collision",
                "External App project ID and Chunk project ID resolve to different projects.",
                details={
                    "externalAppProjectId": external_app_project_id,
                    "chunkProjectId": chunk_project_id,
                },
                status_code=409,
            )
    project = by_external or by_project_id
    if project is None:
        return None

    existing_external = _safe_str(
        _get_attr(project, "external_app_project_id", None),
        "",
    )
    if existing_external and existing_external != external_app_project_id:
        raise ProvisioningError(
            "external_app_project_id_conflict",
            "Chunk project is already linked to another App project.",
            details={
                "requested": external_app_project_id,
                "existing": existing_external,
                "chunkProjectId": _extract_public_id(project, PROJECT_ID_FIELDS),
            },
            status_code=409,
        )
    existing_project_id = _extract_public_id(project, PROJECT_ID_FIELDS)
    if existing_project_id and existing_project_id != chunk_project_id:
        raise ProvisioningError(
            "chunk_project_id_conflict",
            "Explicit or deterministic Chunk project ID conflicts with the existing App link.",
            details={
                "requested": chunk_project_id,
                "existing": existing_project_id,
            },
            status_code=409,
        )
    return project


def _find_universe(
    session: Any,
    *,
    project_db_id: int,
    universe_id: str,
    lock: bool = True,
) -> Any | None:
    columns = _model_columns(Universe)
    filters: dict[str, Any] = {}
    if "project_db_id" in columns:
        filters["project_db_id"] = project_db_id
    if "universe_id" in columns:
        filters["universe_id"] = universe_id
    if len(filters) < 2:
        raise ProvisioningError(
            "universe_model_scope_contract_missing",
            "Universe model must expose project_db_id and universe_id for isolated provisioning.",
            details={"columns": sorted(columns)},
            status_code=500,
        )
    return _query_by_filters(
        session,
        Universe,
        filters,
        entity_name="Universe",
        lock=lock,
    )


def _find_world(
    session: Any,
    *,
    project_db_id: int,
    universe_db_id: int,
    world_id: str,
    lock: bool = True,
) -> Any | None:
    columns = _model_columns(WorldInstance)
    filters: dict[str, Any] = {}
    if "universe_db_id" in columns:
        filters["universe_db_id"] = universe_db_id
    if "world_id" in columns:
        filters["world_id"] = world_id
    if "project_db_id" in columns:
        filters["project_db_id"] = project_db_id
    if "universe_db_id" not in filters or "world_id" not in filters:
        raise ProvisioningError(
            "world_model_scope_contract_missing",
            "WorldInstance model must expose universe_db_id and world_id for isolated provisioning.",
            details={"columns": sorted(columns)},
            status_code=500,
        )
    return _query_by_filters(
        session,
        WorldInstance,
        filters,
        entity_name="WorldInstance",
        lock=lock,
    )


def _find_default_block_registry(session: Any) -> Any | None:
    if BlockRegistry is None:
        return None
    columns = _model_columns(BlockRegistry)
    filters: dict[str, Any] = {}
    if "registry_id" in columns:
        filters["registry_id"] = _config_str(
            "VECTOPLAN_CHUNK_DEFAULT_BLOCK_REGISTRY_ID",
            DEFAULT_BLOCK_REGISTRY_ID,
        )
    if "registry_version" in columns:
        filters["registry_version"] = _config_str(
            "VECTOPLAN_CHUNK_DEFAULT_BLOCK_REGISTRY_VERSION",
            DEFAULT_BLOCK_REGISTRY_VERSION,
        )
    if not filters:
        return None
    try:
        query = session.query(BlockRegistry)
        for key, value in filters.items():
            query = query.filter(getattr(BlockRegistry, key) == value)
        return query.first()
    except Exception:
        return None

# ---------------------------------------------------------------------------
# Entity creation and synchronization
# ---------------------------------------------------------------------------


def _create_project_instance(
    *,
    external_app_project_id: str,
    owner_user_id: str,
    actor_user_id: str,
    ids: ProvisioningIds,
    selection: WorldTemplateSelection,
    payload: Mapping[str, Any],
) -> Any:
    name = _project_name(payload, external_app_project_id)
    description = _project_description(payload)
    source_service = _config_str(
        "VECTOPLAN_CHUNK_PROJECT_PROVISIONING_SOURCE_SERVICE",
        DEFAULT_SOURCE_SERVICE,
    )
    metadata = _build_project_metadata(
        external_app_project_id=external_app_project_id,
        owner_user_id=owner_user_id,
        ids=ids,
        selection=selection,
        payload=payload,
        created=True,
    )

    factory = getattr(Project, "create_for_app_project", None)
    if callable(factory):
        instance = _call_factory_candidates(
            factory,
            (
                (
                    (),
                    {
                        "app_project_public_id": external_app_project_id,
                        "chunk_project_id": ids.chunk_project_id,
                        "name": name,
                        "description": description,
                        "default_universe_id": ids.chunk_universe_id,
                        "default_world_id": ids.chunk_world_id,
                        "spawn_world_id": ids.chunk_world_id,
                        "source_service": source_service,
                        "external_url": _external_url(payload),
                        "owner_user_id": owner_user_id,
                        "created_by_user_id": actor_user_id,
                        "metadata_json": metadata,
                    },
                ),
            ),
            factory_name="Project.create_for_app_project",
        )
        if instance is not None:
            return instance

    factory = getattr(Project, "create", None)
    if callable(factory):
        instance = _call_factory_candidates(
            factory,
            (
                (
                    (),
                    {
                        "project_id": ids.chunk_project_id,
                        "slug": ids.chunk_project_id,
                        "name": name,
                        "description": description,
                        "default_universe_id": ids.chunk_universe_id,
                        "default_world_id": ids.chunk_world_id,
                        "spawn_world_id": ids.chunk_world_id,
                        "external_app_project_id": external_app_project_id,
                        "source_service": source_service,
                        "external_url": _external_url(payload),
                        "owner_user_id": owner_user_id,
                        "created_by_user_id": actor_user_id,
                        "updated_by_user_id": actor_user_id,
                        "metadata_json": metadata,
                    },
                ),
            ),
            factory_name="Project.create",
        )
        if instance is not None:
            return instance

    instance = _instantiate_model(Project, model_name="Project")
    now = utcnow()
    values = {
        "project_id": ids.chunk_project_id,
        "slug": ids.chunk_project_id,
        "name": name,
        "description": description,
        "status": "active",
        "schema_version": "project.schema.v2",
        "revision": 1,
        "default_universe_id": ids.chunk_universe_id,
        "default_world_id": ids.chunk_world_id,
        "spawn_world_id": ids.chunk_world_id,
        "external_app_project_id": external_app_project_id,
        "source_service": source_service,
        "external_url": _external_url(payload),
        "owner_type": "user",
        "owner_id": owner_user_id,
        "created_by_user_id": actor_user_id,
        "updated_by_user_id": actor_user_id,
        "metadata_json": metadata,
        "created_at": now,
        "updated_at": now,
        "archived_at": None,
        "deleted_at": None,
    }
    for field_name, value in values.items():
        if _supports_attr(instance, field_name):
            _set_if_changed(instance, field_name, value)
    return instance


def _apply_project_state(
    project: Any,
    *,
    external_app_project_id: str,
    owner_user_id: str,
    actor_user_id: str,
    ids: ProvisioningIds,
    selection: WorldTemplateSelection,
    payload: Mapping[str, Any],
    created: bool,
    allow_owner_replacement: bool,
    restore_deleted_project: bool,
) -> bool:
    changed = False

    actual_project_id = _extract_public_id(project, PROJECT_ID_FIELDS)
    if actual_project_id and actual_project_id != ids.chunk_project_id:
        raise ProvisioningError(
            "chunk_project_id_conflict",
            "Existing project's public ID differs from the requested Chunk project ID.",
            details={"requested": ids.chunk_project_id, "existing": actual_project_id},
            status_code=409,
        )

    if _is_deleted(project):
        if not restore_deleted_project:
            raise ProvisioningError(
                "chunk_project_deleted",
                "The linked Chunk project is soft-deleted and must be restored explicitly.",
                details={"chunkProjectId": actual_project_id or ids.chunk_project_id},
                status_code=410,
            )
        changed = _restore_lifecycle(project, actor_user_id=actor_user_id) or changed

    existing_external = _safe_str(
        _get_attr(project, "external_app_project_id", None),
        "",
    )
    if existing_external and existing_external != external_app_project_id:
        raise ProvisioningError(
            "external_app_project_id_conflict",
            "Chunk project is already linked to another App project.",
            details={"requested": external_app_project_id, "existing": existing_external},
            status_code=409,
        )

    existing_owner_type = _safe_str(_get_attr(project, "owner_type", None), "").lower()
    existing_owner_id = _safe_str(_get_attr(project, "owner_id", None), "")
    if existing_owner_type and existing_owner_type != "user":
        if not allow_owner_replacement:
            raise ProvisioningError(
                "project_owner_type_conflict",
                "Existing project owner is not a user owner.",
                details={
                    "ownerType": existing_owner_type,
                    "ownerId": existing_owner_id or None,
                    "requestedOwnerUserId": owner_user_id,
                },
                status_code=409,
            )
    if existing_owner_id and existing_owner_id != owner_user_id:
        if not allow_owner_replacement:
            raise ProvisioningError(
                "project_owner_conflict",
                "Existing project owner differs from the requested owner user ID.",
                details={
                    "existingOwnerUserId": existing_owner_id,
                    "requestedOwnerUserId": owner_user_id,
                },
                status_code=409,
            )

    changed = _set_first_supported_if_changed(
        project,
        PROJECT_ID_FIELDS,
        ids.chunk_project_id,
    ) or changed
    changed = _set_if_changed(
        project,
        "external_app_project_id",
        external_app_project_id,
    ) or changed
    changed = _set_if_changed(project, "owner_type", "user") or changed
    changed = _set_if_changed(project, "owner_id", owner_user_id) or changed
    changed = _set_if_changed(
        project,
        "source_service",
        _config_str(
            "VECTOPLAN_CHUNK_PROJECT_PROVISIONING_SOURCE_SERVICE",
            DEFAULT_SOURCE_SERVICE,
        ),
    ) or changed
    changed = _set_if_changed(
        project,
        "default_universe_id",
        ids.chunk_universe_id,
    ) or changed
    changed = _set_if_changed(
        project,
        "default_world_id",
        ids.chunk_world_id,
    ) or changed
    changed = _set_if_changed(
        project,
        "spawn_world_id",
        ids.chunk_world_id,
    ) or changed

    if created or _config_bool(
        "VECTOPLAN_CHUNK_PROJECT_PROVISIONING_ALLOW_NAME_UPDATE",
        True,
    ):
        if created or _payload_has_any(
            payload,
            ("name", "projectName", "project_name", "title", "displayName"),
        ):
            changed = _set_if_changed(
                project,
                "name",
                _project_name(payload, external_app_project_id),
            ) or changed
        if created or _payload_has_any(
            payload,
            ("description", "projectDescription", "project_description", "summary"),
        ):
            changed = _set_if_changed(
                project,
                "description",
                _project_description(payload),
            ) or changed

    if _payload_has_any(
        payload,
        ("externalUrl", "external_url", "appProjectUrl", "app_project_url"),
    ):
        changed = _set_if_changed(
            project,
            "external_url",
            _external_url(payload),
        ) or changed

    if _safe_str(_get_attr(project, "status", "active")).lower() != "active":
        changed = _set_if_changed(project, "status", "active") or changed
        changed = _set_if_changed(project, "archived_at", None) or changed

    if _config_bool(
        "VECTOPLAN_CHUNK_PROJECT_PROVISIONING_ALLOW_METADATA_UPDATE",
        True,
    ) or created:
        changed = _merge_json_if_changed(
            project,
            "metadata_json",
            _build_project_metadata(
                external_app_project_id=external_app_project_id,
                owner_user_id=owner_user_id,
                ids=ids,
                selection=selection,
                payload=payload,
                created=created,
            ),
        ) or changed

    if not created and changed:
        _touch_existing(project, actor_user_id=actor_user_id)
    return changed


def _create_universe_instance(
    *,
    project: Any,
    actor_user_id: str,
    ids: ProvisioningIds,
    selection: WorldTemplateSelection,
    payload: Mapping[str, Any],
) -> Any:
    name = _normalize_required_text(
        _payload_value(
            payload,
            ("universeName", "universe_name"),
            default=_config_str(
                "VECTOPLAN_CHUNK_PROJECT_PROVISIONING_DEFAULT_UNIVERSE_NAME",
                "Project Universe",
            ),
        ),
        field_name="universeName",
        max_length=MAX_NAME_LENGTH,
    )
    metadata = _build_universe_metadata(
        ids=ids,
        selection=selection,
        created=True,
    )

    factory = getattr(Universe, "create_for_project", None)
    if callable(factory):
        instance = _call_factory_candidates(
            factory,
            (
                (
                    (project,),
                    {
                        "universe_id": ids.chunk_universe_id,
                        "slug": ids.chunk_universe_id,
                        "name": name,
                        "default_world_id": ids.chunk_world_id,
                        "spawn_world_id": ids.chunk_world_id,
                        "created_by_user_id": actor_user_id,
                        "metadata_json": metadata,
                    },
                ),
                (
                    (),
                    {
                        "project": project,
                        "universe_id": ids.chunk_universe_id,
                        "slug": ids.chunk_universe_id,
                        "name": name,
                        "default_world_id": ids.chunk_world_id,
                        "spawn_world_id": ids.chunk_world_id,
                        "created_by_user_id": actor_user_id,
                        "metadata_json": metadata,
                    },
                ),
            ),
            factory_name="Universe.create_for_project",
        )
        if instance is not None:
            return instance

    factory = getattr(Universe, "create", None)
    if callable(factory):
        instance = _call_factory_candidates(
            factory,
            (
                (
                    (),
                    {
                        "project_db_id": _database_id(project, field_name="Project"),
                        "universe_id": ids.chunk_universe_id,
                        "slug": ids.chunk_universe_id,
                        "name": name,
                        "default_world_id": ids.chunk_world_id,
                        "spawn_world_id": ids.chunk_world_id,
                        "created_by_user_id": actor_user_id,
                        "metadata_json": metadata,
                    },
                ),
            ),
            factory_name="Universe.create",
        )
        if instance is not None:
            return instance

    instance = _instantiate_model(Universe, model_name="Universe")
    now = utcnow()
    values = {
        "project_db_id": _database_id(project, field_name="Project"),
        "universe_id": ids.chunk_universe_id,
        "slug": ids.chunk_universe_id,
        "name": name,
        "description": "Project-scoped universe created by App project provisioning.",
        "status": "active",
        "schema_version": "universe.schema.v2",
        "revision": 1,
        "universe_role": "default",
        "universe_scope": "project",
        "default_world_id": ids.chunk_world_id,
        "spawn_world_id": ids.chunk_world_id,
        "created_by_user_id": actor_user_id,
        "updated_by_user_id": actor_user_id,
        "metadata_json": metadata,
        "created_at": now,
        "updated_at": now,
        "archived_at": None,
        "deleted_at": None,
    }
    for field_name, value in values.items():
        if _supports_attr(instance, field_name):
            _set_if_changed(instance, field_name, value)
    return instance


def _apply_universe_state(
    universe: Any,
    *,
    project: Any,
    actor_user_id: str,
    ids: ProvisioningIds,
    selection: WorldTemplateSelection,
    payload: Mapping[str, Any],
    created: bool,
) -> bool:
    changed = False
    changed = _assign_parent_reference(
        universe,
        parent=project,
        db_id_fields=("project_db_id",),
        relation_fields=("project",),
    ) or changed
    changed = _set_first_supported_if_changed(
        universe,
        UNIVERSE_ID_FIELDS,
        ids.chunk_universe_id,
    ) or changed
    changed = _set_if_changed(
        universe,
        "default_world_id",
        ids.chunk_world_id,
    ) or changed
    changed = _set_if_changed(
        universe,
        "spawn_world_id",
        ids.chunk_world_id,
    ) or changed

    if created or _payload_has_any(payload, ("universeName", "universe_name")):
        changed = _set_if_changed(
            universe,
            "name",
            _normalize_required_text(
                _payload_value(
                    payload,
                    ("universeName", "universe_name"),
                    default=_config_str(
                        "VECTOPLAN_CHUNK_PROJECT_PROVISIONING_DEFAULT_UNIVERSE_NAME",
                        "Project Universe",
                    ),
                ),
                field_name="universeName",
                max_length=MAX_NAME_LENGTH,
            ),
        ) or changed
    if _is_deleted(universe) or _safe_str(_get_attr(universe, "status", "active")).lower() != "active":
        changed = _set_if_changed(universe, "status", "active") or changed
        changed = _set_if_changed(universe, "deleted_at", None) or changed
        changed = _set_if_changed(universe, "archived_at", None) or changed

    changed = _merge_json_if_changed(
        universe,
        "metadata_json",
        _build_universe_metadata(
            ids=ids,
            selection=selection,
            created=created,
        ),
    ) or changed
    if not created and changed:
        _touch_existing(universe, actor_user_id=actor_user_id)
    return changed


def _existing_world_template(world: Any) -> Optional[str]:
    for field_name in ("template_id", "provider_id", "provider_world_id"):
        value = _safe_str(_get_attr(world, field_name, None), "")
        if not value:
            continue
        try:
            return _normalize_world_template_cached(value)
        except ValueError:
            continue
    return None


def _existing_earth_reference(world: Any) -> Optional[dict[str, Any]]:
    value = _get_attr(world, "global_reference_json", None)
    if value in (None, ""):
        return None
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except Exception:
            return None
    if isinstance(value, Mapping):
        return _json_safe(dict(value))
    return None


def _ensure_world_template_compatible(
    world: Any,
    *,
    selection: WorldTemplateSelection,
) -> None:
    existing_template = _existing_world_template(world)
    if existing_template is not None and existing_template != selection.template_id:
        raise ProvisioningError(
            "world_template_conflict",
            "Existing concrete world uses another immutable world template.",
            details={
                "existingWorldTemplate": existing_template,
                "requestedWorldTemplate": selection.template_id,
                "worldId": _extract_public_id(world, WORLD_ID_FIELDS),
            },
            status_code=409,
        )

    existing_reference = _existing_earth_reference(world)
    if selection.is_flat:
        if existing_reference is not None:
            raise ProvisioningError(
                "flat_world_has_earth_reference",
                "Existing Flat world contains an Earth global reference and is inconsistent.",
                details={"worldId": _extract_public_id(world, WORLD_ID_FIELDS)},
                status_code=409,
            )
        return

    if existing_reference is None:
        return

    try:
        _, existing_fingerprint = _normalize_earth_reference(existing_reference)
    except ProvisioningError as exc:
        raise ProvisioningError(
            "existing_earth_reference_invalid",
            "Existing Earth world contains an invalid global reference.",
            details={
                "worldId": _extract_public_id(world, WORLD_ID_FIELDS),
                "error": exc.message,
                "referenceErrorCode": exc.code,
                "referenceErrorDetails": exc.details,
            },
            status_code=409,
        ) from exc

    if existing_fingerprint != selection.earth_reference_fingerprint:
        raise ProvisioningError(
            "earth_reference_conflict",
            (
                "Existing Earth global reference differs from the requested reference. "
                "Provisioning never performs silent re-anchoring."
            ),
            details={
                "worldId": _extract_public_id(world, WORLD_ID_FIELDS),
                "existingFingerprint": existing_fingerprint,
                "requestedFingerprint": selection.earth_reference_fingerprint,
            },
            status_code=409,
        )



def _floor_precise_spawn_value(value: Any, *, field_name: str) -> int:
    """Convert one persisted precise local-metric spawn value with floor semantics."""

    if value in (None, ""):
        raise ProvisioningError(
            "earth_spawn_state_incomplete",
            "Earth precise spawn value is missing.",
            details={"field": field_name},
            status_code=500,
        )
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ProvisioningError(
            "earth_spawn_state_invalid",
            "Earth precise spawn value is not numeric.",
            details={"field": field_name, "value": _safe_str(value)},
            status_code=500,
        ) from exc
    if not number.is_finite():
        raise ProvisioningError(
            "earth_spawn_state_invalid",
            "Earth precise spawn value must be finite.",
            details={"field": field_name},
            status_code=500,
        )
    return int(number.to_integral_value(rounding=ROUND_FLOOR))


def _synchronize_earth_spawn_from_precise(world: Any) -> bool:
    """
    Keep integer and precise Earth spawn coordinates internally consistent.

    ``WorldInstance.create_earth_spawn`` derives ``spawn_*_precise`` from the
    canonical reference and stores integer coordinates using mathematical floor.
    Provisioning must never replace those values with Flat/default configuration.
    Existing rows created by the previous policy are repaired idempotently when
    their precise fields are available.
    """

    pairs = (
        ("spawn_x", "spawn_x_precise"),
        ("spawn_y", "spawn_y_precise"),
        ("spawn_z", "spawn_z_precise"),
    )
    available = [
        (integer_field, precise_field)
        for integer_field, precise_field in pairs
        if _supports_attr(world, precise_field)
        and _get_attr(world, precise_field, None) not in (None, "")
    ]

    if not available:
        # Compatibility with model variants that do not persist precise spawn
        # columns: preserve the values produced by the Earth factory.
        return False

    changed = False
    for integer_field, precise_field in available:
        derived = _floor_precise_spawn_value(
            _get_attr(world, precise_field, None),
            field_name=f"WorldInstance.{precise_field}",
        )
        changed = _set_if_changed(world, integer_field, derived) or changed
    return changed

def _create_world_instance(
    *,
    project: Any,
    universe: Any,
    actor_user_id: str,
    ids: ProvisioningIds,
    selection: WorldTemplateSelection,
    payload: Mapping[str, Any],
) -> Any:
    world_name = _normalize_required_text(
        _payload_value(
            payload,
            ("worldName", "world_name"),
            default=(
                "Earth Spawn World"
                if selection.is_earth
                else _config_str(
                    "VECTOPLAN_CHUNK_PROJECT_PROVISIONING_DEFAULT_WORLD_NAME",
                    "Spawn World",
                )
            ),
        ),
        field_name="worldName",
        max_length=MAX_NAME_LENGTH,
    )
    metadata = _build_world_metadata(
        ids=ids,
        selection=selection,
        created=True,
    )
    common_kwargs = {
        "project_db_id": _database_id(project, field_name="Project"),
        "universe_db_id": _database_id(universe, field_name="Universe"),
        "world_id": ids.chunk_world_id,
        "slug": "spawn" if ids.chunk_world_id == DEFAULT_WORLD_ID else ids.chunk_world_id,
        "name": world_name,
        "created_by_user_id": actor_user_id,
        "metadata_json": metadata,
        "source_service": "vectoplan-chunk-project-provisioning",
        "external_ref": ids.chunk_world_id,
    }

    factory_name = (
        "create_earth_spawn" if selection.is_earth else "create_flat_spawn"
    )
    factory = getattr(WorldInstance, factory_name, None)
    if callable(factory):
        factory_kwargs = dict(common_kwargs)
        if selection.is_earth:
            factory_kwargs["global_reference"] = selection.earth_reference
            factory_kwargs["earth_reference"] = selection.earth_reference
        instance = _call_factory_candidates(
            factory,
            (
                ((), factory_kwargs),
                (
                    (project, universe),
                    {
                        key: value
                        for key, value in factory_kwargs.items()
                        if key not in {"project_db_id", "universe_db_id"}
                    },
                ),
                (
                    (),
                    {
                        **factory_kwargs,
                        "project": project,
                        "universe": universe,
                    },
                ),
            ),
            factory_name=f"WorldInstance.{factory_name}",
        )
        if instance is not None:
            return instance

    factory = getattr(WorldInstance, "create_for_universe", None)
    if callable(factory):
        factory_kwargs = {
            **common_kwargs,
            "template_id": selection.template_id,
            "provider_id": selection.contract.get("provider_id"),
            "provider_world_id": selection.contract.get("provider_world_id"),
            "global_reference": selection.earth_reference,
        }
        instance = _call_factory_candidates(
            factory,
            (
                ((universe,), factory_kwargs),
                ((), {**factory_kwargs, "universe": universe, "project": project}),
            ),
            factory_name="WorldInstance.create_for_universe",
        )
        if instance is not None:
            return instance

    factory = getattr(WorldInstance, "create", None)
    if callable(factory):
        instance = _call_factory_candidates(
            factory,
            (
                (
                    (),
                    {
                        **common_kwargs,
                        "template_id": selection.template_id,
                        "provider_id": selection.contract.get("provider_id"),
                        "provider_world_id": selection.contract.get("provider_world_id"),
                        "global_reference": selection.earth_reference,
                    },
                ),
            ),
            factory_name="WorldInstance.create",
        )
        if instance is not None:
            return instance

    if selection.is_earth:
        columns = _model_columns(WorldInstance)
        supports_reference = (
            "global_reference_json" in columns
            or callable(getattr(WorldInstance, "set_global_reference", None))
        )
        if not supports_reference:
            raise ProvisioningError(
                "earth_world_model_not_supported",
                (
                    "WorldInstance does not expose create_earth_spawn(), "
                    "global_reference_json or set_global_reference()."
                ),
                details={"columns": sorted(columns)},
                status_code=501,
            )

    return _instantiate_model(WorldInstance, model_name="WorldInstance")


def _apply_world_state(
    world: Any,
    *,
    project: Any,
    universe: Any,
    actor_user_id: str,
    ids: ProvisioningIds,
    selection: WorldTemplateSelection,
    payload: Mapping[str, Any],
    created: bool,
) -> bool:
    _ensure_world_template_compatible(world, selection=selection)
    changed = False

    changed = _assign_parent_reference(
        world,
        parent=universe,
        db_id_fields=("universe_db_id",),
        relation_fields=("universe",),
    ) or changed
    if _supports_attr(world, "project_db_id") or _supports_attr(world, "project"):
        changed = _assign_parent_reference(
            world,
            parent=project,
            db_id_fields=("project_db_id",),
            relation_fields=("project",),
        ) or changed

    changed = _set_first_supported_if_changed(
        world,
        WORLD_ID_FIELDS,
        ids.chunk_world_id,
    ) or changed
    changed = _set_if_changed(world, "template_id", selection.contract["template_id"]) or changed
    changed = _set_if_changed(world, "provider_id", selection.contract["provider_id"]) or changed
    changed = _set_if_changed(
        world,
        "provider_world_id",
        selection.contract["provider_world_id"],
    ) or changed
    changed = _set_if_changed(world, "world_type", "runtime-world") or changed
    changed = _set_if_changed(world, "world_role", DEFAULT_WORLD_ROLE) or changed
    changed = _set_if_changed(world, "world_scope", DEFAULT_WORLD_SCOPE) or changed

    for field_name in (
        "generator_type",
        "generator_version",
        "projection_type",
        "topology_type",
        "coordinate_system",
        "chunk_size",
        "cell_size",
        "surface_y",
        "min_y",
        "max_y",
        "seed",
        "spawn_yaw",
        "spawn_pitch",
    ):
        changed = _set_if_changed(
            world,
            field_name,
            selection.contract[field_name],
        ) or changed

    if selection.is_earth:
        changed = _synchronize_earth_spawn_from_precise(world) or changed
    else:
        for field_name in ("spawn_x", "spawn_y", "spawn_z"):
            changed = _set_if_changed(
                world,
                field_name,
                selection.contract[field_name],
            ) or changed

    changed = _set_if_changed(
        world,
        "block_registry_id",
        _config_str(
            "VECTOPLAN_CHUNK_DEFAULT_BLOCK_REGISTRY_ID",
            DEFAULT_BLOCK_REGISTRY_ID,
        ),
    ) or changed
    changed = _set_if_changed(
        world,
        "block_registry_version",
        _config_str(
            "VECTOPLAN_CHUNK_DEFAULT_BLOCK_REGISTRY_VERSION",
            DEFAULT_BLOCK_REGISTRY_VERSION,
        ),
    ) or changed
    changed = _set_if_changed(
        world,
        "source_service",
        "vectoplan-chunk-project-provisioning",
    ) or changed
    changed = _set_if_changed(world, "external_ref", ids.chunk_world_id) or changed

    if created or _payload_has_any(payload, ("worldName", "world_name")):
        changed = _set_if_changed(
            world,
            "name",
            _normalize_required_text(
                _payload_value(
                    payload,
                    ("worldName", "world_name"),
                    default="Earth Spawn World" if selection.is_earth else "Spawn World",
                ),
                field_name="worldName",
                max_length=MAX_NAME_LENGTH,
            ),
        ) or changed
    if created:
        changed = _set_if_changed(
            world,
            "slug",
            "spawn" if ids.chunk_world_id == DEFAULT_WORLD_ID else ids.chunk_world_id,
        ) or changed

    if _is_deleted(world) or _safe_str(_get_attr(world, "status", "active")).lower() != "active":
        changed = _set_if_changed(world, "status", "active") or changed
        changed = _set_if_changed(world, "deleted_at", None) or changed
        changed = _set_if_changed(world, "archived_at", None) or changed

    if selection.is_earth:
        existing_reference = _existing_earth_reference(world)
        if existing_reference is None:
            set_reference = _get_attr(world, "set_global_reference", None)
            if callable(set_reference):
                try:
                    set_reference(
                        selection.earth_reference,
                        updated_by_user_id=actor_user_id,
                    )
                    changed = True
                except TypeError:
                    try:
                        set_reference(selection.earth_reference)
                        changed = True
                    except Exception as exc:
                        raise ProvisioningError(
                            "earth_reference_persistence_failed",
                            "WorldInstance.set_global_reference() failed.",
                            details={"error": _safe_exception_message(exc)},
                            status_code=409,
                        ) from exc
                except Exception as exc:
                    raise ProvisioningError(
                        "earth_reference_persistence_failed",
                        "WorldInstance.set_global_reference() failed.",
                        details={"error": _safe_exception_message(exc)},
                        status_code=409,
                    ) from exc
            else:
                changed = _set_json_if_changed(
                    world,
                    "global_reference_json",
                    selection.earth_reference or {},
                ) or changed
                if _supports_attr(world, "global_reference_fingerprint"):
                    changed = _set_if_changed(
                        world,
                        "global_reference_fingerprint",
                        selection.earth_reference_fingerprint,
                    ) or changed
                if _supports_attr(world, "coordinate_frame_revision"):
                    revision = _safe_int(
                        _get_attr(world, "coordinate_frame_revision", 0),
                        0,
                    )
                    if revision < 1:
                        changed = _set_if_changed(
                            world,
                            "coordinate_frame_revision",
                            1,
                        ) or changed
                if _supports_attr(world, "global_reference_updated_at"):
                    changed = _set_if_changed(
                        world,
                        "global_reference_updated_at",
                        utcnow(),
                    ) or changed
                if _supports_attr(world, "global_reference_updated_by_user_id"):
                    changed = _set_if_changed(
                        world,
                        "global_reference_updated_by_user_id",
                        actor_user_id,
                    ) or changed
    else:
        if _supports_attr(world, "global_reference_json"):
            changed = _set_if_changed(world, "global_reference_json", None) or changed
        if _supports_attr(world, "global_reference_fingerprint"):
            changed = _set_if_changed(world, "global_reference_fingerprint", None) or changed
        if _supports_attr(world, "coordinate_frame_revision"):
            changed = _set_if_changed(world, "coordinate_frame_revision", 0) or changed

    changed = _merge_json_if_changed(
        world,
        "metadata_json",
        _build_world_metadata(
            ids=ids,
            selection=selection,
            created=created,
        ),
    ) or changed

    if not created and changed:
        _touch_existing(world, actor_user_id=actor_user_id)
    return changed

# ---------------------------------------------------------------------------
# Result builders
# ---------------------------------------------------------------------------


def _project_payload(project: Any) -> dict[str, Any]:
    return _serialize_model(
        project,
        (
            "id",
            "project_id",
            "slug",
            "name",
            "description",
            "status",
            "schema_version",
            "revision",
            "default_universe_id",
            "default_world_id",
            "spawn_world_id",
            "external_app_project_id",
            "source_service",
            "external_url",
            "owner_type",
            "owner_id",
            "created_by_user_id",
            "updated_by_user_id",
            "metadata_json",
            "created_at",
            "updated_at",
            "archived_at",
            "deleted_at",
        ),
    )


def _universe_payload(universe: Any) -> dict[str, Any]:
    return _serialize_model(
        universe,
        (
            "id",
            "project_db_id",
            "universe_id",
            "slug",
            "name",
            "description",
            "status",
            "schema_version",
            "revision",
            "universe_role",
            "universe_scope",
            "default_world_id",
            "spawn_world_id",
            "created_by_user_id",
            "updated_by_user_id",
            "metadata_json",
            "created_at",
            "updated_at",
            "archived_at",
            "deleted_at",
        ),
    )


def _world_payload(world: Any) -> dict[str, Any]:
    return _serialize_model(
        world,
        (
            "id",
            "project_db_id",
            "universe_db_id",
            "world_id",
            "slug",
            "name",
            "description",
            "status",
            "schema_version",
            "revision",
            "world_type",
            "world_role",
            "world_scope",
            "template_id",
            "provider_id",
            "provider_world_id",
            "generator_type",
            "generator_version",
            "projection_type",
            "topology_type",
            "coordinate_system",
            "chunk_size",
            "cell_size",
            "surface_y",
            "min_y",
            "max_y",
            "seed",
            "block_registry_id",
            "block_registry_version",
            "spawn_x",
            "spawn_y",
            "spawn_z",
            "spawn_yaw",
            "spawn_pitch",
            "spawn_coordinate_space",
            "spawn_x_precise",
            "spawn_y_precise",
            "spawn_z_precise",
            "coordinate_frame_revision",
            "global_reference_json",
            "global_reference_fingerprint",
            "global_reference_locked_at",
            "global_reference_lock_reasons_json",
            "global_reference_updated_at",
            "global_reference_updated_by_user_id",
            "source_service",
            "external_ref",
            "created_by_user_id",
            "updated_by_user_id",
            "metadata_json",
            "created_at",
            "updated_at",
            "archived_at",
            "deleted_at",
        ),
    )


def _block_registry_payload(registry: Any) -> dict[str, Any]:
    return _serialize_model(
        registry,
        (
            "id",
            "registry_id",
            "registry_version",
            "label",
            "name",
            "status",
            "is_default",
            "source",
        ),
    )


def _success_result(
    *,
    external_app_project_id: str,
    owner_user_id: str,
    ids: ProvisioningIds,
    selection: WorldTemplateSelection,
    project: Any,
    universe: Any,
    world: Any,
    access: Mapping[str, Any],
    block_registry: Any | None,
    created_components: Sequence[str],
    updated_components: Sequence[str],
    warnings: Sequence[ProvisioningIssue],
) -> ChunkProjectProvisioningResult:
    created = bool(created_components)
    updated = bool(updated_components)
    if created:
        code = "chunk_project_provisioned"
        message = "Chunk project graph was provisioned."
        status_code = 201
    elif updated:
        code = "chunk_project_updated"
        message = "Existing Chunk project graph was repaired or synchronized."
        status_code = 200
    else:
        code = "chunk_project_exists"
        message = "Chunk project graph already exists and is unchanged."
        status_code = 200

    return ChunkProjectProvisioningResult(
        ok=True,
        code=code,
        message=message,
        created=created,
        updated=updated,
        status_code=status_code,
        external_app_project_id=external_app_project_id,
        owner_user_id=owner_user_id,
        chunk_project_id=ids.chunk_project_id,
        chunk_universe_id=ids.chunk_universe_id,
        chunk_world_id=ids.chunk_world_id,
        world_template=selection.template_id,
        earth_reference_fingerprint=selection.earth_reference_fingerprint,
        block_registry_id=_config_str(
            "VECTOPLAN_CHUNK_DEFAULT_BLOCK_REGISTRY_ID",
            DEFAULT_BLOCK_REGISTRY_ID,
        ),
        block_registry_version=_config_str(
            "VECTOPLAN_CHUNK_DEFAULT_BLOCK_REGISTRY_VERSION",
            DEFAULT_BLOCK_REGISTRY_VERSION,
        ),
        project=_project_payload(project),
        universe=_universe_payload(universe),
        world=_world_payload(world),
        access=dict(access),
        block_registry=_block_registry_payload(block_registry),
        route_hints=_build_route_hints(ids.chunk_project_id, ids.chunk_world_id),
        lifecycle={
            "createdComponents": list(created_components),
            "updatedComponents": list(updated_components),
            "idempotent": not created and not updated,
            "transactionCommitted": False,
            "generatedAt": utcnow().isoformat(),
        },
        warnings=list(warnings),
        errors=[],
    )


def _error_result(
    *,
    code: str,
    message: str,
    status_code: int,
    details: Optional[Mapping[str, Any]] = None,
    external_app_project_id: Optional[str] = None,
    owner_user_id: Optional[str] = None,
    world_template: Optional[str] = None,
) -> ChunkProjectProvisioningResult:
    return ChunkProjectProvisioningResult(
        ok=False,
        code=code,
        message=message,
        status_code=status_code,
        external_app_project_id=external_app_project_id,
        owner_user_id=owner_user_id,
        world_template=world_template,
        errors=[
            ProvisioningIssue(
                code=code,
                message=message,
                details=dict(details or {}),
            )
        ],
    )


# ---------------------------------------------------------------------------
# Core transaction body
# ---------------------------------------------------------------------------


def _provision_once(
    *,
    app_project_public_id: str,
    payload: Mapping[str, Any],
    session: Any,
    commit: bool,
    owner_user_id: Optional[str],
    actor_user_id: Optional[str],
    allow_owner_replacement: bool,
    restore_deleted_project: bool,
    require_access: bool,
) -> ChunkProjectProvisioningResult:
    _ensure_models_available()

    external_app_project_id = _normalize_public_id(
        app_project_public_id,
        field_name="appProjectPublicId",
    )
    selection = _resolve_world_template_selection(payload)
    ids = _resolve_provisioning_ids(external_app_project_id, payload)

    owner_source = owner_user_id
    if owner_source in (None, ""):
        owner_source = _payload_value(
            payload,
            OWNER_USER_ID_PAYLOAD_FIELDS,
            default=_config_str(
                "VECTOPLAN_CHUNK_DEFAULT_PROJECT_OWNER_USER_ID",
                DEFAULT_OWNER_USER_ID,
            ),
        )
    normalized_owner = _normalize_user_id(
        owner_source,
        field_name="ownerUserId",
        required=True,
    )
    assert normalized_owner is not None

    actor_source = actor_user_id
    if actor_source in (None, ""):
        actor_source = _payload_value(
            payload,
            ACTOR_USER_ID_PAYLOAD_FIELDS,
            default=normalized_owner,
        )
    normalized_actor = _normalize_user_id(
        actor_source,
        field_name="actorUserId",
        required=False,
    ) or normalized_owner

    warnings: list[ProvisioningIssue] = []
    created_components: list[str] = []
    updated_components: list[str] = []

    project = _find_project(
        session,
        external_app_project_id=external_app_project_id,
        chunk_project_id=ids.chunk_project_id,
        lock=True,
    )
    created_project = project is None

    if project is None:
        project = _create_project_instance(
            external_app_project_id=external_app_project_id,
            owner_user_id=normalized_owner,
            actor_user_id=normalized_actor,
            ids=ids,
            selection=selection,
            payload=payload,
        )
        session.add(project)
        _flush(session, objects=[project])
        created_components.append("project")
    else:
        ids = _derive_existing_child_ids(project, ids)

    project_changed = _apply_project_state(
        project,
        external_app_project_id=external_app_project_id,
        owner_user_id=normalized_owner,
        actor_user_id=normalized_actor,
        ids=ids,
        selection=selection,
        payload=payload,
        created=created_project,
        allow_owner_replacement=allow_owner_replacement,
        restore_deleted_project=restore_deleted_project,
    )
    session.add(project)
    _flush(session, objects=[project])
    if project_changed and not created_project:
        updated_components.append("project")

    actual_project_id = _extract_public_id(project, PROJECT_ID_FIELDS, ids.chunk_project_id)
    if actual_project_id is None:
        raise ProvisioningError(
            "chunk_project_id_missing",
            "Project has no public Chunk project ID after flush.",
            status_code=500,
        )
    if actual_project_id != ids.chunk_project_id:
        ids = ProvisioningIds(
            chunk_project_id=actual_project_id,
            chunk_universe_id=ids.chunk_universe_id,
            chunk_world_id=ids.chunk_world_id,
            project_explicit=ids.project_explicit,
            universe_explicit=ids.universe_explicit,
            world_explicit=ids.world_explicit,
        )

    access_payload, access_changed, access_warnings = _initialize_project_access(
        project=project,
        owner_user_id=normalized_owner,
        actor_user_id=normalized_actor,
        session=session,
        allow_owner_replacement=allow_owner_replacement,
        require_access=require_access,
    )
    warnings.extend(access_warnings)
    if access_changed:
        access_role_stats = access_payload.get("roleStats") or {}
        access_assignment_stats = access_payload.get("assignmentStats") or {}
        created_count = _safe_int(access_role_stats.get("created"), 0) + _safe_int(
            access_assignment_stats.get("created"),
            0,
        )
        if created_count > 0:
            created_components.append("projectAccess")
        else:
            updated_components.append("projectAccess")

    project_db_id = _database_id(project, field_name="Project")
    universe = _find_universe(
        session,
        project_db_id=project_db_id,
        universe_id=ids.chunk_universe_id,
        lock=True,
    )
    created_universe = universe is None
    if universe is None:
        universe = _create_universe_instance(
            project=project,
            actor_user_id=normalized_actor,
            ids=ids,
            selection=selection,
            payload=payload,
        )
        session.add(universe)
        _flush(session, objects=[universe])
        created_components.append("universe")

    universe_changed = _apply_universe_state(
        universe,
        project=project,
        actor_user_id=normalized_actor,
        ids=ids,
        selection=selection,
        payload=payload,
        created=created_universe,
    )
    session.add(universe)
    _flush(session, objects=[universe])
    if universe_changed and not created_universe:
        updated_components.append("universe")

    actual_universe_id = _extract_public_id(
        universe,
        UNIVERSE_ID_FIELDS,
        ids.chunk_universe_id,
    )
    if actual_universe_id is None:
        raise ProvisioningError(
            "chunk_universe_id_missing",
            "Universe has no public ID after flush.",
            status_code=500,
        )
    if actual_universe_id != ids.chunk_universe_id:
        ids = ProvisioningIds(
            chunk_project_id=ids.chunk_project_id,
            chunk_universe_id=actual_universe_id,
            chunk_world_id=ids.chunk_world_id,
            project_explicit=ids.project_explicit,
            universe_explicit=ids.universe_explicit,
            world_explicit=ids.world_explicit,
        )

    universe_db_id = _database_id(universe, field_name="Universe")
    world = _find_world(
        session,
        project_db_id=project_db_id,
        universe_db_id=universe_db_id,
        world_id=ids.chunk_world_id,
        lock=True,
    )
    created_world = world is None
    if world is None:
        world = _create_world_instance(
            project=project,
            universe=universe,
            actor_user_id=normalized_actor,
            ids=ids,
            selection=selection,
            payload=payload,
        )
        session.add(world)
        _flush(session, objects=[world])
        created_components.append("world")

    world_changed = _apply_world_state(
        world,
        project=project,
        universe=universe,
        actor_user_id=normalized_actor,
        ids=ids,
        selection=selection,
        payload=payload,
        created=created_world,
    )
    session.add(world)
    _flush(session, objects=[world])
    if world_changed and not created_world:
        updated_components.append("world")

    actual_world_id = _extract_public_id(world, WORLD_ID_FIELDS, ids.chunk_world_id)
    if actual_world_id is None:
        raise ProvisioningError(
            "chunk_world_id_missing",
            "WorldInstance has no public world ID after flush.",
            status_code=500,
        )
    if actual_world_id != ids.chunk_world_id:
        ids = ProvisioningIds(
            chunk_project_id=ids.chunk_project_id,
            chunk_universe_id=ids.chunk_universe_id,
            chunk_world_id=actual_world_id,
            project_explicit=ids.project_explicit,
            universe_explicit=ids.universe_explicit,
            world_explicit=ids.world_explicit,
        )

    # Final reference synchronization after all actual public IDs are known.
    final_project_changed = False
    final_project_changed = _set_if_changed(
        project,
        "default_universe_id",
        ids.chunk_universe_id,
    ) or final_project_changed
    final_project_changed = _set_if_changed(
        project,
        "default_world_id",
        ids.chunk_world_id,
    ) or final_project_changed
    final_project_changed = _set_if_changed(
        project,
        "spawn_world_id",
        ids.chunk_world_id,
    ) or final_project_changed
    final_universe_changed = False
    final_universe_changed = _set_if_changed(
        universe,
        "default_world_id",
        ids.chunk_world_id,
    ) or final_universe_changed
    final_universe_changed = _set_if_changed(
        universe,
        "spawn_world_id",
        ids.chunk_world_id,
    ) or final_universe_changed

    if final_project_changed and not created_project:
        _touch_existing(project, actor_user_id=normalized_actor)
        if "project" not in updated_components:
            updated_components.append("project")
    if final_universe_changed and not created_universe:
        _touch_existing(universe, actor_user_id=normalized_actor)
        if "universe" not in updated_components:
            updated_components.append("universe")

    session.add(project)
    session.add(universe)
    _flush(session, objects=[project, universe, world])

    block_registry = _find_default_block_registry(session)
    if block_registry is None:
        warnings.append(
            ProvisioningIssue(
                code="default_block_registry_not_found",
                message=(
                    "Default BlockRegistry is not available. Run the explicit DB bootstrap "
                    "before loading or editing world chunks."
                ),
                details={
                    "blockRegistryId": _config_str(
                        "VECTOPLAN_CHUNK_DEFAULT_BLOCK_REGISTRY_ID",
                        DEFAULT_BLOCK_REGISTRY_ID,
                    ),
                    "blockRegistryVersion": _config_str(
                        "VECTOPLAN_CHUNK_DEFAULT_BLOCK_REGISTRY_VERSION",
                        DEFAULT_BLOCK_REGISTRY_VERSION,
                    ),
                },
            )
        )

    result = _success_result(
        external_app_project_id=external_app_project_id,
        owner_user_id=normalized_owner,
        ids=ids,
        selection=selection,
        project=project,
        universe=universe,
        world=world,
        access=access_payload,
        block_registry=block_registry,
        created_components=tuple(dict.fromkeys(created_components)),
        updated_components=tuple(dict.fromkeys(updated_components)),
        warnings=warnings,
    )

    if commit:
        session.commit()
        result.lifecycle["transactionCommitted"] = True
    return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def preview_chunk_project_ids(
    app_project_public_id: str,
    payload: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Preview IDs, template and routes without touching the database."""

    data = _payload_dict(payload)
    external_app_project_id = _normalize_public_id(
        app_project_public_id,
        field_name="appProjectPublicId",
    )
    ids = _resolve_provisioning_ids(external_app_project_id, data)
    selection = _resolve_world_template_selection(data)
    owner = _normalize_user_id(
        _payload_value(
            data,
            OWNER_USER_ID_PAYLOAD_FIELDS,
            default=_config_str(
                "VECTOPLAN_CHUNK_DEFAULT_PROJECT_OWNER_USER_ID",
                DEFAULT_OWNER_USER_ID,
            ),
        ),
        field_name="ownerUserId",
        required=True,
    )
    return {
        "schemaVersion": PROVISIONING_SCHEMA_VERSION,
        "externalAppProjectId": external_app_project_id,
        "ownerUserId": owner,
        "chunkProjectId": ids.chunk_project_id,
        "chunkUniverseId": ids.chunk_universe_id,
        "chunkWorldId": ids.chunk_world_id,
        **selection.to_dict(),
        "routeHints": _build_route_hints(ids.chunk_project_id, ids.chunk_world_id),
    }


def ensure_chunk_project_for_app_project(
    app_project_public_id: str,
    payload: Mapping[str, Any] | None = None,
    *,
    session: Any | None = None,
    commit: bool = True,
    owner_user_id: Any = None,
    actor_user_id: Any = None,
    allow_owner_replacement: bool = False,
    restore_deleted_project: bool = False,
    require_access: bool | None = None,
    retry_on_integrity_error: bool = True,
) -> ChunkProjectProvisioningResult:
    """
    Idempotently provision one App project into the Chunk service.

    ``owner_user_id`` and ``actor_user_id`` should be supplied by a trusted route
    or service layer. Payload aliases remain supported for the current transition
    and local command testing. When no owner is supplied, the temporary external
    placeholder ID ``"1"`` is used.

    With ``commit=False`` no commit or rollback is performed here. The caller owns
    the surrounding transaction and must roll it back after a failed result.
    """

    data: dict[str, Any] = {}
    normalized_app_id: Optional[str] = None
    normalized_owner: Optional[str] = None
    selected_template: Optional[str] = None

    try:
        if not _config_bool("VECTOPLAN_CHUNK_PROJECT_PROVISIONING_ENABLED", True):
            raise ProvisioningError(
                "project_provisioning_disabled",
                "Chunk project provisioning is disabled by configuration.",
                status_code=503,
            )

        data = _payload_dict(payload)
        app_id_source = app_project_public_id or _payload_value(
            data,
            (
                "appProjectPublicId",
                "app_project_public_id",
                "externalAppProjectId",
                "external_app_project_id",
                "appProjectId",
                "app_project_id",
            ),
            default=None,
        )
        normalized_app_id = _normalize_public_id(
            app_id_source,
            field_name="appProjectPublicId",
        )
        selection = _resolve_world_template_selection(data)
        selected_template = selection.template_id

        owner_source = owner_user_id
        if owner_source in (None, ""):
            owner_source = _payload_value(
                data,
                OWNER_USER_ID_PAYLOAD_FIELDS,
                default=_config_str(
                    "VECTOPLAN_CHUNK_DEFAULT_PROJECT_OWNER_USER_ID",
                    DEFAULT_OWNER_USER_ID,
                ),
            )
        normalized_owner = _normalize_user_id(
            owner_source,
            field_name="ownerUserId",
            required=True,
        )

        active_session = _ensure_session(session)
        resolved_require_access = (
            bool(require_access)
            if require_access is not None
            else _config_bool(
                "VECTOPLAN_CHUNK_PROJECT_PROVISIONING_REQUIRE_ACCESS",
                True,
            )
        )
        return _provision_once(
            app_project_public_id=normalized_app_id,
            payload=data,
            session=active_session,
            commit=commit,
            owner_user_id=normalized_owner,
            actor_user_id=_normalize_user_id(
                actor_user_id,
                field_name="actorUserId",
                required=False,
            ) if actor_user_id not in (None, "") else None,
            allow_owner_replacement=bool(allow_owner_replacement),
            restore_deleted_project=bool(restore_deleted_project),
            require_access=resolved_require_access,
        )

    except IntegrityError as exc:
        active_session = _ensure_session(session)
        if commit:
            try:
                active_session.rollback()
            except Exception:
                pass
            if retry_on_integrity_error and normalized_app_id is not None:
                try:
                    return ensure_chunk_project_for_app_project(
                        normalized_app_id,
                        data,
                        session=active_session,
                        commit=True,
                        owner_user_id=normalized_owner,
                        actor_user_id=actor_user_id,
                        allow_owner_replacement=allow_owner_replacement,
                        restore_deleted_project=restore_deleted_project,
                        require_access=require_access,
                        retry_on_integrity_error=False,
                    )
                except Exception:
                    pass
        return _error_result(
            code="project_provisioning_integrity_error",
            message="Chunk project provisioning failed because of a database integrity conflict.",
            status_code=409,
            details={
                "exceptionType": exc.__class__.__name__,
                "error": _safe_exception_message(exc),
                "transactionOwner": "provisioning" if commit else "caller",
            },
            external_app_project_id=normalized_app_id,
            owner_user_id=normalized_owner,
            world_template=selected_template,
        )

    except ProvisioningError as exc:
        if commit:
            try:
                _ensure_session(session).rollback()
            except Exception:
                pass
        return _error_result(
            code=exc.code,
            message=exc.message,
            status_code=exc.status_code,
            details={
                **exc.details,
                "transactionOwner": "provisioning" if commit else "caller",
            },
            external_app_project_id=normalized_app_id,
            owner_user_id=normalized_owner,
            world_template=selected_template,
        )

    except SQLAlchemyError as exc:
        if commit:
            try:
                _ensure_session(session).rollback()
            except Exception:
                pass
        return _error_result(
            code="project_provisioning_database_error",
            message="Chunk project provisioning failed because of a database error.",
            status_code=500,
            details={
                "exceptionType": exc.__class__.__name__,
                "error": _safe_exception_message(exc),
                "transactionOwner": "provisioning" if commit else "caller",
            },
            external_app_project_id=normalized_app_id,
            owner_user_id=normalized_owner,
            world_template=selected_template,
        )

    except Exception as exc:
        if commit:
            try:
                _ensure_session(session).rollback()
            except Exception:
                pass
        return _error_result(
            code="project_provisioning_unexpected_error",
            message="Chunk project provisioning failed because of an unexpected error.",
            status_code=500,
            details={
                "exceptionType": exc.__class__.__name__,
                "error": _safe_exception_message(exc),
                "transactionOwner": "provisioning" if commit else "caller",
            },
            external_app_project_id=normalized_app_id,
            owner_user_id=normalized_owner,
            world_template=selected_template,
        )


def ensure_chunk_project_from_payload(
    payload: Mapping[str, Any] | None,
    *,
    session: Any | None = None,
    commit: bool = True,
    owner_user_id: Any = None,
    actor_user_id: Any = None,
    allow_owner_replacement: bool = False,
    restore_deleted_project: bool = False,
    require_access: bool | None = None,
) -> ChunkProjectProvisioningResult:
    """Provision from a request body used by ``POST /projects/ensure``."""

    try:
        data = _payload_dict(payload)
        app_project_public_id = _payload_value(
            data,
            (
                "appProjectPublicId",
                "app_project_public_id",
                "externalAppProjectId",
                "external_app_project_id",
                "appProjectId",
                "app_project_id",
                "externalProjectId",
                "external_project_id",
            ),
            default=None,
        )
        return ensure_chunk_project_for_app_project(
            app_project_public_id,
            data,
            session=session,
            commit=commit,
            owner_user_id=owner_user_id,
            actor_user_id=actor_user_id,
            allow_owner_replacement=allow_owner_replacement,
            restore_deleted_project=restore_deleted_project,
            require_access=require_access,
        )
    except ProvisioningError as exc:
        return _error_result(
            code=exc.code,
            message=exc.message,
            status_code=exc.status_code,
            details=exc.details,
        )


def provisioning_result_to_response_tuple(
    result: ChunkProjectProvisioningResult,
) -> tuple[dict[str, Any], int]:
    return result.to_dict(), result.status_code


def clear_provisioning_caches() -> dict[str, Any]:
    """Clear only pure normalization/import caches, never ORM state."""

    before = {
        "worldTemplate": _normalize_world_template_cached.cache_info()._asdict(),
        "templateContracts": _default_world_template_contracts.cache_info()._asdict(),
        "projectAccessApi": _load_project_access_api.cache_info()._asdict(),
        "georeferencingApi": _load_georeferencing_api.cache_info()._asdict(),
    }
    _normalize_world_template_cached.cache_clear()
    _default_world_template_contracts.cache_clear()
    _load_project_access_api.cache_clear()
    _load_georeferencing_api.cache_clear()
    return {
        "cleared": True,
        "before": before,
        "after": {
            "worldTemplate": _normalize_world_template_cached.cache_info()._asdict(),
            "templateContracts": _default_world_template_contracts.cache_info()._asdict(),
            "projectAccessApi": _load_project_access_api.cache_info()._asdict(),
            "georeferencingApi": _load_georeferencing_api.cache_info()._asdict(),
        },
    }


def get_project_provisioning_contract() -> dict[str, Any]:
    """Return a DB-free public contract for diagnostics and status routes."""

    return {
        "schemaVersion": PROVISIONING_SCHEMA_VERSION,
        "serviceVersion": PROVISIONING_SERVICE_VERSION,
        "supportedWorldTemplates": list(SUPPORTED_WORLD_TEMPLATES),
        "defaultWorldTemplate": WORLD_TEMPLATE_FLAT,
        "defaultConcreteWorldId": DEFAULT_WORLD_ID,
        "defaultOwnerUserId": _config_str(
            "VECTOPLAN_CHUNK_DEFAULT_PROJECT_OWNER_USER_ID",
            DEFAULT_OWNER_USER_ID,
        ),
        "accessRequiredByDefault": _config_bool(
            "VECTOPLAN_CHUNK_PROJECT_PROVISIONING_REQUIRE_ACCESS",
            True,
        ),
        "authzEnforced": False,
        "externalUserForeignKeys": False,
        "externalAppProjectForeignKeys": False,
        "worldTemplateMutable": False,
        "earthReferenceRequired": True,
        "earthReferenceReanchoring": False,
        "worldStateSynchronization": {
            "policyVersion": PROVISIONING_WORLD_STATE_POLICY_VERSION,
            "templateConfigNamespacesIsolated": True,
            "earthGenericDefaultLeakagePrevented": True,
            "earthSpawnDerivedFromReference": True,
            "earthIntegerSpawnDerivedFromPrecise": True,
            "accessProjectDbIdIncluded": True,
        },
        "earthReferenceNormalization": {
            "policyVersion": PROVISIONING_EARTH_REFERENCE_POLICY_VERSION,
            "compactInputAccepted": True,
            "canonicalInputAccepted": True,
            "explicitCrsRequired": True,
            "alwaysXYRequired": True,
            "defaultEarthGridInjected": True,
            "canonicalPersistenceContract": "GlobalReferencePoint.to_persistence_dict",
            "fingerprintSource": "GlobalReferencePoint.fingerprint",
        },
        "transactionModes": {
            "commitTrue": "provisioning owns commit and rollback",
            "commitFalse": "caller owns commit and rollback",
        },
        "rowLocking": {
            "policyVersion": PROVISIONING_ROW_LOCK_POLICY_VERSION,
            "targetedBaseTableLocks": True,
            "eagerLoadsDisabledForLockLookups": True,
            "postgresOuterJoinSafe": True,
        },
        "boundaries": {
            "createsTables": False,
            "runsMigrations": False,
            "seedsGlobalBlocks": False,
            "loadsChunks": False,
            "writesSnapshots": False,
            "writesEvents": False,
            "callsAppService": False,
        },
    }


__all__ = [
    "PROVISIONING_SCHEMA_VERSION",
    "PROVISIONING_SERVICE_VERSION",
    "PROVISIONING_ROW_LOCK_POLICY_VERSION",
    "PROVISIONING_EARTH_REFERENCE_POLICY_VERSION",
    "PROVISIONING_WORLD_STATE_POLICY_VERSION",
    "WORLD_TEMPLATE_FLAT",
    "WORLD_TEMPLATE_EARTH",
    "SUPPORTED_WORLD_TEMPLATES",
    "DEFAULT_OWNER_USER_ID",
    "DEFAULT_WORLD_ID",
    "ProvisioningError",
    "ProvisioningIssue",
    "ProvisioningIds",
    "WorldTemplateSelection",
    "ChunkProjectProvisioningResult",
    "preview_chunk_project_ids",
    "ensure_chunk_project_for_app_project",
    "ensure_chunk_project_from_payload",
    "provisioning_result_to_response_tuple",
    "clear_provisioning_caches",
    "get_project_provisioning_contract",
]
