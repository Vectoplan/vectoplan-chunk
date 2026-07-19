# services/vectoplan-chunk/src/services/project_provisioning_service.py
"""
Idempotent Chunk-side provisioning for VECTOPLAN App projects.

One App project maps to one Chunk project, one Universe, and one concrete
WorldInstance. App projects request Earth by default. Flat is a controlled
fallback only for configured Earth-reference business/precondition errors.

This module:
- accepts only authenticated trusted service principals for mutations;
- accepts only canonical ``auth_user_id`` values for the owner;
- rejects local AppUser ids, emails, and request-supplied actor identities;
- never changes an existing world's template silently;
- performs no outbound HTTP request;
- never creates schema, runs migrations, or seeds bootstrap data.
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import math
import re
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping, Optional, Protocol, Sequence


SERVICE_VERSION = "1.0.0"
PROVISIONING_SCHEMA_VERSION = "chunk-project-provisioning-v1"
PROVISIONING_REQUEST_SCHEMA_VERSION = "chunk-project-provisioning-request-v1"
PROVISIONING_RESULT_SCHEMA_VERSION = "chunk-project-provisioning-result-v1"
WORLD_SPEC_SCHEMA_VERSION = "chunk-world-spec-v1"

TEMPLATE_EARTH = "earth"
TEMPLATE_FLAT = "flat"

STATUS_DISABLED = "disabled"
STATUS_PENDING = "pending"
STATUS_PROVISIONING = "provisioning"
STATUS_READY = "ready"
STATUS_FALLBACK_READY = "fallback_ready"
STATUS_FAILED = "failed"
STATUS_REPAIR_REQUIRED = "repair_required"

ACCESS_STATUS_PENDING = "pending"
ACCESS_STATUS_READY = "ready"
ACCESS_STATUS_FAILED = "failed"
ACCESS_STATUS_REPAIR_REQUIRED = "repair_required"
ACCESS_STATUS_DISABLED = "disabled"

CODE_OK = "project_provisioned"
CODE_IDEMPOTENT = "project_already_provisioned"
CODE_DRY_RUN = "project_provision_preview"
CODE_FALLBACK = "project_provisioned_with_flat_fallback"
CODE_DISABLED = "project_provisioning_disabled"
CODE_MUTATIONS_DISABLED = "runtime_business_mutations_disabled"
CODE_APP_PROJECT_ID_REQUIRED = "app_project_public_id_required"
CODE_APP_PROJECT_ID_INVALID = "app_project_public_id_invalid"
CODE_APP_PROJECT_ID_CONFLICT = "app_project_public_id_conflict"
CODE_OWNER_REQUIRED = "owner_auth_user_id_required"
CODE_OWNER_INVALID = "owner_auth_user_id_invalid"
CODE_OWNER_CONFLICT = "owner_transfer_required"
CODE_SERVICE_UNAUTHENTICATED = "service_authentication_required"
CODE_SERVICE_FORBIDDEN = "service_not_allowed"
CODE_TEMPLATE_UNSUPPORTED = "world_template_unsupported"
CODE_TEMPLATE_CHANGE_FORBIDDEN = "world_template_change_forbidden"
CODE_EARTH_REFERENCE_REQUIRED = "earth_reference_required"
CODE_EARTH_REFERENCE_INCOMPLETE = "earth_reference_incomplete"
CODE_EARTH_REFERENCE_INVALID = "earth_reference_invalid"
CODE_EARTH_CRS_UNSUPPORTED = "unsupported_coordinate_reference"
CODE_METADATA_TOO_LARGE = "provisioning_metadata_too_large"
CODE_REQUEST_TOO_LARGE = "provisioning_request_too_large"
CODE_EXISTING_PROJECT_INCOMPLETE = "existing_chunk_project_incomplete"
CODE_EXISTING_PROJECT_CONFLICT = "existing_chunk_project_conflict"
CODE_PROVIDER_ERROR = "world_provider_error"
CODE_PROVIDER_INITIALIZATION_FAILED = "provider_initialization_failed"
CODE_DATABASE_ERROR = "database_error"
CODE_VERIFICATION_FAILED = "project_provisioning_verification_failed"
CODE_ACCESS_INITIALIZATION_FAILED = "project_access_initialization_failed"
CODE_INTERNAL_ERROR = "project_provisioning_internal_error"

DEFAULT_FALLBACK_CODES = (
    "coordinates_unavailable",
    "earth_reference_missing",
    "earth_reference_required",
    "earth_reference_incomplete",
    "earth_reference_invalid",
    "earth_reference_not_available",
    "invalid_earth_reference",
    "project_coordinates_unavailable",
    "unsupported_coordinate_reference",
)

FORBIDDEN_FALLBACK_CODES = frozenset(
    {
        "auth_failed", "authentication_failed", "database_error", "dns_error",
        "forbidden", "http_5xx", "internal_error", "internal_server_error",
        "network_error", "provider_initialization_failed", "service_unavailable",
        "timeout", "transport_error", "unauthorized",
    }
)

_TRUE_VALUES = frozenset({"1", "true", "yes", "on", "enabled", "active", "ready", "ok"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off", "disabled", "inactive", "failed", ""})
_SAFE_ID_RE = re.compile(r"[^A-Za-z0-9_.:-]+")
_SAFE_AUTH_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{2,254}$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]+")
_SECRET_KEY_RE = re.compile(
    r"(?:authorization|api[_-]?key|token|secret|password|cookie|session|private[_-]?key)",
    re.IGNORECASE,
)
_LOCAL_ID_KEY_RE = re.compile(
    r"(?:^|_)(?:user_id|app_user_id|local_user_id|account_id|owner_user_id)(?:$|_)",
    re.IGNORECASE,
)

_DEFAULT_CONFIG_CACHE_TTL = 60.0
_DEFAULT_RESULT_CACHE_TTL = 30.0
_DEFAULT_RESULT_CACHE_SIZE = 512
_DEFAULT_LOCK_CACHE_SIZE = 2048


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_text(value: Any, default: str = "", max_len: int = 4096) -> str:
    if value is None:
        return default
    try:
        text = _CONTROL_RE.sub("", str(value)).strip()
    except Exception:
        return default
    if not text:
        return default
    return text[:max_len] if max_len else text


def _safe_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return bool(value)
    text = _safe_text(value, "", 40).lower()
    if text in _TRUE_VALUES:
        return True
    if text in _FALSE_VALUES:
        return False
    return bool(default)


def _safe_int(value: Any, default: int = 0, *, minimum: int | None = None, maximum: int | None = None) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        result = int(default)
    if minimum is not None:
        result = max(minimum, result)
    if maximum is not None:
        result = min(maximum, result)
    return result


def _safe_float(value: Any, default: float = 0.0, *, minimum: float | None = None, maximum: float | None = None) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        result = float(default)
    if not math.isfinite(result):
        result = float(default)
    if minimum is not None:
        result = max(minimum, result)
    if maximum is not None:
        result = min(maximum, result)
    return result


def _as_mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _as_sequence(value: Any) -> list[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return list(value)
    return []


def _stable_json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    except Exception:
        return "{}"


def _json_size(value: Any) -> int:
    return len(_stable_json(value).encode("utf-8"))


def _sha256(value: Any) -> str:
    return hashlib.sha256(_safe_text(value, "", 1_000_000).encode("utf-8")).hexdigest()


def _short_fingerprint(value: Any, prefix: str = "fp") -> str:
    digest = _sha256(value)
    return f"{prefix}_{digest[:16]}" if digest else ""


def _constant_equal(left: Any, right: Any) -> bool:
    try:
        return hmac.compare_digest(_safe_text(left), _safe_text(right))
    except Exception:
        return False


def _normalize_identifier(value: Any, default: str = "", max_len: int = 255) -> str:
    text = _safe_text(value, default, max_len * 2)
    if not text:
        return default
    text = _SAFE_ID_RE.sub("-", text).strip("-._:")
    return (text or default)[:max_len]


def _normalize_code(value: Any, default: str = "") -> str:
    return _normalize_identifier(value, default, 160).lower().replace("-", "_").replace(".", "_")


def _normalize_template_id(value: Any, default: str = TEMPLATE_EARTH) -> str:
    text = _normalize_identifier(value, default, 80).lower()
    aliases = {
        "globe": TEMPLATE_EARTH, "geodetic": TEMPLATE_EARTH, "geo": TEMPLATE_EARTH,
        "earth-world": TEMPLATE_EARTH, "flat-world": TEMPLATE_FLAT,
        "plane": TEMPLATE_FLAT, "local": TEMPLATE_FLAT,
    }
    return aliases.get(text, text or default)


def _mask_email(value: Any) -> str:
    text = _safe_text(value, "", 320)
    if not _EMAIL_RE.match(text):
        return text
    local, domain = text.split("@", 1)
    suffix = domain.split(".")[-1] if "." in domain else ""
    return f"{local[:1] if local else '*'}***@***{'.' + suffix if suffix else ''}"


def sanitize_metadata(value: Any, *, max_depth: int = 5, max_items: int = 128, max_string: int = 2048, _depth: int = 0) -> Any:
    if _depth >= max_depth:
        return "[truncated]"
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for index, (raw_key, raw_value) in enumerate(value.items()):
            if index >= max_items:
                result["_truncated"] = True
                break
            key = _normalize_identifier(raw_key, "", 120).lower()
            if not key:
                continue
            if _SECRET_KEY_RE.search(key):
                result[key] = "[redacted]"
                continue
            if key in {"auth_user_id", "authuserid", "actor_auth_user_id", "target_auth_user_id", "email", "owner_email", "user_email"} or _LOCAL_ID_KEY_RE.search(key):
                result[key] = "[redacted-identity]"
                continue
            if key in {"geometry", "geometries", "chunks", "blocks", "world_state", "worldstate", "snapshot", "binary", "blob"}:
                result[key] = "[omitted-bulk-data]"
                continue
            result[key] = sanitize_metadata(raw_value, max_depth=max_depth, max_items=max_items, max_string=max_string, _depth=_depth + 1)
        return result
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [sanitize_metadata(item, max_depth=max_depth, max_items=max_items, max_string=max_string, _depth=_depth + 1) for item in list(value)[:max_items]]
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    text = _safe_text(value, "", max_string)
    if _EMAIL_RE.match(text):
        return _mask_email(text)
    if "://" in text:
        return "[redacted-url]"
    return text


def _get_config_value(config: Any, name: str, default: Any = None) -> Any:
    if config is None:
        return default
    if isinstance(config, Mapping):
        return config.get(name, default)
    try:
        return getattr(config, name, default)
    except Exception:
        return default


def _csv_tuple(value: Any, default: Iterable[str] = ()) -> tuple[str, ...]:
    values = list(default) if value is None else (re.split(r"[,;\s]+", value) if isinstance(value, str) else _as_sequence(value))
    result: list[str] = []
    for item in values:
        normalized = _normalize_code(item)
        if normalized and normalized not in result:
            result.append(normalized)
    return tuple(result)


def _normalize_app_project_id(value: Any) -> str:
    text = _safe_text(value, "", 255)
    if not text:
        raise ProjectProvisioningError("The app project public id is required.", code=CODE_APP_PROJECT_ID_REQUIRED, status_code=400)
    if "/" in text or "\\" in text or "://" in text or _EMAIL_RE.match(text):
        raise ProjectProvisioningError("The app project public id is invalid.", code=CODE_APP_PROJECT_ID_INVALID, status_code=400)
    normalized = _normalize_identifier(text, "", 160)
    if len(normalized) < 3:
        raise ProjectProvisioningError("The app project public id is invalid.", code=CODE_APP_PROJECT_ID_INVALID, status_code=400)
    return normalized


def _normalize_auth_user_id(value: Any) -> str:
    text = _safe_text(value, "", 255)
    if not text:
        raise ProjectProvisioningError("The canonical owner auth user id is required.", code=CODE_OWNER_REQUIRED, status_code=400)
    if text.isdigit() or _EMAIL_RE.match(text) or "/" in text or "\\" in text or "://" in text:
        raise ProjectProvisioningError("owner_auth_user_id must be an opaque canonical auth_user_id.", code=CODE_OWNER_INVALID, status_code=400)
    if not _SAFE_AUTH_ID_RE.fullmatch(text):
        raise ProjectProvisioningError("owner_auth_user_id is invalid.", code=CODE_OWNER_INVALID, status_code=400)
    return text


def _detect_forbidden_identity_keys(payload: Mapping[str, Any]) -> list[str]:
    allowed = {"owner_auth_user_id", "ownerAuthUserId", "auth_user_id", "authUserId"}
    forbidden: list[str] = []
    for key in payload:
        text = _safe_text(key, "", 120)
        normalized = text.replace("-", "_")
        if text in allowed:
            continue
        if _LOCAL_ID_KEY_RE.search(normalized) or normalized.lower() in {"email", "owner_email", "actor_user_id", "actor_id", "client_user_id", "request_user_id"}:
            forbidden.append(text)
    return sorted(set(forbidden))


def _deterministic_resource_id(prefix: str, app_project_public_id: str, kind: str) -> str:
    safe_prefix = _normalize_identifier(prefix, "", 48)
    if safe_prefix and not safe_prefix.endswith(("-", "_", ":")):
        safe_prefix += "_"
    digest = hashlib.sha256(f"{kind}:{app_project_public_id}".encode()).hexdigest()[:24]
    hint = _normalize_identifier(app_project_public_id, "project", 28).lower()
    return _normalize_identifier(f"{safe_prefix}{hint}_{digest}", digest, 120)


def _build_request_fingerprint(app_id: str, project_name: str, owner_id: str, template_id: str, earth_reference: Optional["EarthReference"], metadata: Mapping[str, Any]) -> str:
    return hashlib.sha256(_stable_json({
        "app_project_public_id": app_id,
        "project_name": _safe_text(project_name, "", 255),
        "owner_fingerprint": _short_fingerprint(owner_id, "usr"),
        "requested_template_id": template_id,
        "earth_reference_fingerprint": earth_reference.fingerprint if earth_reference else None,
        "metadata": sanitize_metadata(metadata),
        "schema_version": PROVISIONING_REQUEST_SCHEMA_VERSION,
    }).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class ProvisioningSettings:
    enabled: bool = True
    idempotent: bool = True
    runtime_business_mutations_enabled: bool = True
    source_service: str = "vectoplan-app"
    allowed_service_ids: tuple[str, ...] = ("vectoplan-app", "vectoplan-chunk-init")
    allow_existing_by_external_id: bool = True
    allow_name_update: bool = True
    allow_metadata_update: bool = True
    create_universe: bool = True
    create_world: bool = True
    create_block_registry_ref: bool = True
    project_id_prefix: str = "chk_prj_"
    universe_id_prefix: str = "chk_uni_"
    world_id_prefix: str = "chk_wld_"
    default_template_id: str = TEMPLATE_EARTH
    fallback_template_id: str = TEMPLATE_FLAT
    supported_template_ids: tuple[str, ...] = (TEMPLATE_EARTH, TEMPLATE_FLAT)
    allow_fallback: bool = True
    allow_template_change: bool = False
    fallback_error_codes: tuple[str, ...] = DEFAULT_FALLBACK_CODES
    earth_crs_id: str = "EPSG:4979"
    default_earth_height: float = 0.0
    require_earth_reference: bool = True
    default_world_name: str = "Spawn World"
    default_universe_name: str = "Project Universe"
    block_registry_id: str = "debug-blocks"
    block_registry_version: str = "1"
    default_chunk_size: int = 16
    default_cell_size: float = 1.0
    default_surface_y: int = 0
    default_min_y: int = -8
    default_max_y: int = 64
    default_spawn_x: int = 0
    default_spawn_y: int = 2
    default_spawn_z: int = 0
    max_metadata_bytes: int = 65536
    max_request_bytes: int = 262144
    result_cache_ttl: float = _DEFAULT_RESULT_CACHE_TTL
    result_cache_size: int = _DEFAULT_RESULT_CACHE_SIZE


_SETTINGS_CACHE: dict[int, tuple[float, ProvisioningSettings]] = {}
_SETTINGS_CACHE_LOCK = threading.RLock()


def _load_provisioning_settings(config: Any = None) -> ProvisioningSettings:
    key = id(config) if config is not None else 0
    now = time.monotonic()
    with _SETTINGS_CACHE_LOCK:
        cached = _SETTINGS_CACHE.get(key)
        if cached and cached[0] > now:
            return cached[1]

    supported_raw = _get_config_value(
        config,
        "VECTOPLAN_CHUNK_PROJECT_PROVISIONING_SUPPORTED_TEMPLATE_IDS",
        (TEMPLATE_EARTH, TEMPLATE_FLAT),
    )
    supported = tuple(
        item for item in (_normalize_template_id(value, "") for value in _as_sequence(supported_raw))
        if item
    ) or (TEMPLATE_EARTH, TEMPLATE_FLAT)

    fallback_codes = tuple(
        code
        for code in _csv_tuple(
            _get_config_value(
                config,
                "VECTOPLAN_CHUNK_PROJECT_PROVISIONING_FALLBACK_ERROR_CODES",
                DEFAULT_FALLBACK_CODES,
            ),
            DEFAULT_FALLBACK_CODES,
        )
        if code not in FORBIDDEN_FALLBACK_CODES
    )

    allowed_raw = _get_config_value(
        config,
        "VECTOPLAN_CHUNK_ACCESS_MUTATION_SERVICE_IDS",
        (
            _get_config_value(
                config,
                "VECTOPLAN_CHUNK_PROJECT_PROVISIONING_SOURCE_SERVICE",
                "vectoplan-app",
            ),
            "vectoplan-chunk-init",
        ),
    )
    allowed_services = tuple(
        value for value in (
            _normalize_identifier(item, "", 120).lower()
            for item in _as_sequence(allowed_raw)
        )
        if value
    ) or ("vectoplan-app", "vectoplan-chunk-init")

    settings = ProvisioningSettings(
        enabled=_safe_bool(_get_config_value(config, "VECTOPLAN_CHUNK_PROJECT_PROVISIONING_ENABLED", True), True),
        idempotent=_safe_bool(_get_config_value(config, "VECTOPLAN_CHUNK_PROJECT_PROVISIONING_IDEMPOTENT", True), True),
        runtime_business_mutations_enabled=_safe_bool(
            _get_config_value(
                config,
                "VECTOPLAN_CHUNK_RUNTIME_BUSINESS_MUTATIONS_ENABLED",
                _get_config_value(config, "VECTOPLAN_CHUNK_ALLOW_RUNTIME_DB_MUTATIONS", True),
            ),
            True,
        ),
        source_service=_normalize_identifier(
            _get_config_value(config, "VECTOPLAN_CHUNK_PROJECT_PROVISIONING_SOURCE_SERVICE", "vectoplan-app"),
            "vectoplan-app",
            120,
        ).lower(),
        allowed_service_ids=allowed_services,
        allow_existing_by_external_id=_safe_bool(
            _get_config_value(config, "VECTOPLAN_CHUNK_PROJECT_PROVISIONING_ALLOW_EXISTING_BY_EXTERNAL_ID", True),
            True,
        ),
        allow_name_update=_safe_bool(_get_config_value(config, "VECTOPLAN_CHUNK_PROJECT_PROVISIONING_ALLOW_NAME_UPDATE", True), True),
        allow_metadata_update=_safe_bool(_get_config_value(config, "VECTOPLAN_CHUNK_PROJECT_PROVISIONING_ALLOW_METADATA_UPDATE", True), True),
        create_universe=_safe_bool(_get_config_value(config, "VECTOPLAN_CHUNK_PROJECT_PROVISIONING_CREATE_UNIVERSE", True), True),
        create_world=_safe_bool(_get_config_value(config, "VECTOPLAN_CHUNK_PROJECT_PROVISIONING_CREATE_WORLD", True), True),
        create_block_registry_ref=_safe_bool(
            _get_config_value(config, "VECTOPLAN_CHUNK_PROJECT_PROVISIONING_CREATE_BLOCK_REGISTRY_REF", True),
            True,
        ),
        project_id_prefix=_safe_text(_get_config_value(config, "VECTOPLAN_CHUNK_PROJECT_PROVISIONING_PROJECT_ID_PREFIX", "chk_prj_"), "chk_prj_", 48),
        universe_id_prefix=_safe_text(_get_config_value(config, "VECTOPLAN_CHUNK_PROJECT_PROVISIONING_UNIVERSE_ID_PREFIX", "chk_uni_"), "chk_uni_", 48),
        world_id_prefix=_safe_text(_get_config_value(config, "VECTOPLAN_CHUNK_PROJECT_PROVISIONING_WORLD_ID_PREFIX", "chk_wld_"), "chk_wld_", 48),
        default_template_id=_normalize_template_id(
            _get_config_value(config, "VECTOPLAN_CHUNK_PROJECT_PROVISIONING_DEFAULT_TEMPLATE_ID", TEMPLATE_EARTH),
            TEMPLATE_EARTH,
        ),
        fallback_template_id=_normalize_template_id(
            _get_config_value(config, "VECTOPLAN_CHUNK_PROJECT_PROVISIONING_FALLBACK_TEMPLATE_ID", TEMPLATE_FLAT),
            TEMPLATE_FLAT,
        ),
        supported_template_ids=supported,
        allow_fallback=_safe_bool(_get_config_value(config, "VECTOPLAN_CHUNK_PROJECT_PROVISIONING_ALLOW_FALLBACK", True), True),
        allow_template_change=_safe_bool(_get_config_value(config, "VECTOPLAN_CHUNK_PROJECT_PROVISIONING_ALLOW_TEMPLATE_CHANGE", False), False),
        fallback_error_codes=fallback_codes,
        earth_crs_id=_safe_text(_get_config_value(config, "VECTOPLAN_CHUNK_EARTH_CRS_ID", "EPSG:4979"), "EPSG:4979", 64).upper(),
        default_earth_height=_safe_float(
            _get_config_value(config, "VECTOPLAN_CHUNK_DEFAULT_EARTH_HEIGHT", 0.0),
            0.0,
            minimum=-12000.0,
            maximum=100000.0,
        ),
        require_earth_reference=_safe_bool(
            _get_config_value(config, "VECTOPLAN_CHUNK_PROJECT_PROVISIONING_REQUIRE_EARTH_REFERENCE", True),
            True,
        ),
        default_world_name=_safe_text(_get_config_value(config, "VECTOPLAN_CHUNK_PROJECT_PROVISIONING_DEFAULT_WORLD_NAME", "Spawn World"), "Spawn World", 255),
        default_universe_name=_safe_text(_get_config_value(config, "VECTOPLAN_CHUNK_PROJECT_PROVISIONING_DEFAULT_UNIVERSE_NAME", "Project Universe"), "Project Universe", 255),
        block_registry_id=_normalize_identifier(_get_config_value(config, "VECTOPLAN_CHUNK_DEFAULT_BLOCK_REGISTRY_ID", "debug-blocks"), "debug-blocks", 120),
        block_registry_version=_safe_text(_get_config_value(config, "VECTOPLAN_CHUNK_DEFAULT_BLOCK_REGISTRY_VERSION", "1"), "1", 80),
        default_chunk_size=_safe_int(_get_config_value(config, "VECTOPLAN_CHUNK_DEFAULT_CHUNK_SIZE", 16), 16, minimum=1, maximum=256),
        default_cell_size=_safe_float(_get_config_value(config, "VECTOPLAN_CHUNK_DEFAULT_CELL_SIZE", 1.0), 1.0, minimum=0.0001),
        default_surface_y=_safe_int(_get_config_value(config, "VECTOPLAN_CHUNK_DEFAULT_SURFACE_Y", 0), 0),
        default_min_y=_safe_int(_get_config_value(config, "VECTOPLAN_CHUNK_DEFAULT_MIN_Y", -8), -8),
        default_max_y=_safe_int(_get_config_value(config, "VECTOPLAN_CHUNK_DEFAULT_MAX_Y", 64), 64),
        default_spawn_x=_safe_int(_get_config_value(config, "VECTOPLAN_CHUNK_DEFAULT_SPAWN_X", 0), 0),
        default_spawn_y=_safe_int(_get_config_value(config, "VECTOPLAN_CHUNK_DEFAULT_SPAWN_Y", 2), 2),
        default_spawn_z=_safe_int(_get_config_value(config, "VECTOPLAN_CHUNK_DEFAULT_SPAWN_Z", 0), 0),
        max_metadata_bytes=_safe_int(
            _get_config_value(config, "VECTOPLAN_CHUNK_PROJECT_PROVISIONING_MAX_METADATA_BYTES", 65536),
            65536,
            minimum=1024,
            maximum=1024 * 1024,
        ),
        max_request_bytes=_safe_int(
            _get_config_value(config, "VECTOPLAN_CHUNK_PROJECT_PROVISIONING_MAX_REQUEST_BYTES", 262144),
            262144,
            minimum=4096,
            maximum=4 * 1024 * 1024,
        ),
        result_cache_ttl=_safe_float(
            _get_config_value(config, "VECTOPLAN_CHUNK_PROJECT_PROVISIONING_CACHE_TTL_SECONDS", _DEFAULT_RESULT_CACHE_TTL),
            _DEFAULT_RESULT_CACHE_TTL,
            minimum=0.0,
            maximum=3600.0,
        ),
        result_cache_size=_safe_int(
            _get_config_value(config, "VECTOPLAN_CHUNK_PROJECT_PROVISIONING_CACHE_MAX_ENTRIES", _DEFAULT_RESULT_CACHE_SIZE),
            _DEFAULT_RESULT_CACHE_SIZE,
            minimum=0,
            maximum=10000,
        ),
    )
    with _SETTINGS_CACHE_LOCK:
        _SETTINGS_CACHE[key] = (now + _DEFAULT_CONFIG_CACHE_TTL, settings)
    return settings


def clear_project_provisioning_settings_cache() -> None:
    with _SETTINGS_CACHE_LOCK:
        _SETTINGS_CACHE.clear()


class ProjectProvisioningError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: str = CODE_INTERNAL_ERROR,
        status_code: int = 500,
        retryable: bool = False,
        repair_required: bool = False,
        details: Optional[Mapping[str, Any]] = None,
        request_id: str = "",
        correlation_id: str = "",
        cause: Optional[BaseException] = None,
    ) -> None:
        super().__init__(_safe_text(message, "Project provisioning failed.", 4000))
        self.code = _normalize_code(code, CODE_INTERNAL_ERROR)
        self.status_code = _safe_int(status_code, 500, minimum=400, maximum=599)
        self.retryable = bool(retryable)
        self.repair_required = bool(repair_required)
        self.details = sanitize_metadata(details or {})
        self.request_id = _safe_text(request_id, "", 160)
        self.correlation_id = _safe_text(correlation_id or request_id, "", 160)
        self.cause = cause

    def to_dict(self, *, include_private: bool = False) -> dict[str, Any]:
        payload = {
            "ok": False,
            "code": self.code,
            "status_code": self.status_code,
            "statusCode": self.status_code,
            "error": str(self),
            "retryable": self.retryable,
            "repair_required": self.repair_required,
            "repairRequired": self.repair_required,
            "request_id": self.request_id or None,
            "requestId": self.request_id or None,
            "correlation_id": self.correlation_id or None,
            "correlationId": self.correlation_id or None,
        }
        if self.details:
            payload["details"] = sanitize_metadata(self.details)
        if include_private and self.cause is not None:
            payload["cause_type"] = type(self.cause).__name__
        return payload


@dataclass(frozen=True, slots=True)
class EarthReference:
    latitude: float
    longitude: float
    height: float = 0.0
    crs_id: str = "EPSG:4979"
    source: str = "vectoplan-app"
    reference_id: str = ""
    confidence: Optional[float] = None
    fingerprint: str = ""

    def __post_init__(self) -> None:
        latitude = _safe_float(self.latitude, 0.0, minimum=-90.0, maximum=90.0)
        longitude = _safe_float(self.longitude, 0.0, minimum=-180.0, maximum=180.0)
        height = _safe_float(self.height, 0.0, minimum=-12000.0, maximum=100000.0)
        crs_id = _safe_text(self.crs_id, "EPSG:4979", 64).upper()
        source = _normalize_identifier(self.source, "vectoplan-app", 120)
        reference_id = _normalize_identifier(self.reference_id, "", 160)
        confidence = None if self.confidence is None else _safe_float(self.confidence, 0.0, minimum=0.0, maximum=1.0)
        fingerprint = self.fingerprint or hashlib.sha256(_stable_json({
            "latitude": round(latitude, 9),
            "longitude": round(longitude, 9),
            "height": round(height, 4),
            "crs_id": crs_id,
            "reference_id": reference_id or None,
        }).encode()).hexdigest()
        object.__setattr__(self, "latitude", latitude)
        object.__setattr__(self, "longitude", longitude)
        object.__setattr__(self, "height", height)
        object.__setattr__(self, "crs_id", crs_id)
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "reference_id", reference_id)
        object.__setattr__(self, "confidence", confidence)
        object.__setattr__(self, "fingerprint", fingerprint)

    def to_dict(self, *, include_private: bool = False) -> dict[str, Any]:
        payload = {
            "present": True,
            "crs_id": self.crs_id,
            "crsId": self.crs_id,
            "reference_fingerprint": self.fingerprint,
            "referenceFingerprint": self.fingerprint,
            "reference_id": self.reference_id or None,
            "referenceId": self.reference_id or None,
            "source": self.source,
        }
        if self.confidence is not None:
            payload["confidence"] = self.confidence
        if include_private:
            payload.update({"latitude": self.latitude, "longitude": self.longitude, "height": self.height})
        return payload


def build_earth_reference(
    value: Any,
    *,
    default_crs_id: str = "EPSG:4979",
    default_height: float = 0.0,
    required: bool = False,
) -> Optional[EarthReference]:
    data = _as_mapping(value)
    if not data:
        if required:
            raise ProjectProvisioningError(
                "Earth provisioning requires a valid earth reference.",
                code=CODE_EARTH_REFERENCE_REQUIRED,
                status_code=422,
                details={"required_fields": ["latitude", "longitude"]},
            )
        return None

    latitude_value = data.get("latitude", data.get("lat"))
    longitude_value = data.get("longitude", data.get("lon", data.get("lng")))
    if latitude_value in (None, "") or longitude_value in (None, ""):
        raise ProjectProvisioningError(
            "Earth reference is incomplete.",
            code=CODE_EARTH_REFERENCE_INCOMPLETE,
            status_code=422,
            details={"required_fields": ["latitude", "longitude"]},
        )
    try:
        latitude = float(latitude_value)
        longitude = float(longitude_value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ProjectProvisioningError(
            "Earth reference coordinates are invalid.",
            code=CODE_EARTH_REFERENCE_INVALID,
            status_code=422,
            cause=exc,
        ) from exc
    if not math.isfinite(latitude) or not math.isfinite(longitude):
        raise ProjectProvisioningError("Earth reference coordinates are invalid.", code=CODE_EARTH_REFERENCE_INVALID, status_code=422)
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        raise ProjectProvisioningError("Earth reference coordinates are outside valid ranges.", code=CODE_EARTH_REFERENCE_INVALID, status_code=422)

    crs_id = _safe_text(data.get("crs_id", data.get("crsId", data.get("coordinate_srid", default_crs_id))), default_crs_id, 64).upper()
    if crs_id not in {"EPSG:4979", "EPSG:4326", _safe_text(default_crs_id, "EPSG:4979", 64).upper()}:
        raise ProjectProvisioningError(
            "The earth coordinate reference is unsupported.",
            code=CODE_EARTH_CRS_UNSUPPORTED,
            status_code=422,
            details={"crs_id": crs_id},
        )
    return EarthReference(
        latitude=latitude,
        longitude=longitude,
        height=_safe_float(data.get("height", data.get("altitude", data.get("elevation", default_height))), default_height, minimum=-12000, maximum=100000),
        crs_id=crs_id,
        source=_safe_text(data.get("source"), "vectoplan-app", 120),
        reference_id=_safe_text(data.get("reference_id", data.get("referenceId")), "", 160),
        confidence=None if data.get("confidence") is None else _safe_float(data.get("confidence"), 0.0, minimum=0.0, maximum=1.0),
    )


@dataclass(frozen=True, slots=True)
class ProvisioningRequest:
    app_project_public_id: str
    project_name: str
    owner_auth_user_id: str
    requested_template_id: str
    earth_reference: Optional[EarthReference]
    metadata: Mapping[str, Any]
    source_service: str
    request_id: str
    correlation_id: str
    idempotency_key_hash: str
    request_fingerprint: str
    earth_reference_error_code: str = ""
    initialize_access: bool = True
    schema_version: str = PROVISIONING_REQUEST_SCHEMA_VERSION

    def to_dict(self, *, include_private: bool = False) -> dict[str, Any]:
        payload = {
            "schema_version": self.schema_version,
            "app_project_public_id": self.app_project_public_id,
            "appProjectPublicId": self.app_project_public_id,
            "project_name": self.project_name,
            "projectName": self.project_name,
            "requested_template_id": self.requested_template_id,
            "requestedTemplateId": self.requested_template_id,
            "earth_reference": self.earth_reference.to_dict(include_private=include_private) if self.earth_reference else {"present": False},
            "source_service": self.source_service,
            "request_id": self.request_id,
            "correlation_id": self.correlation_id,
            "idempotency_key_hash": self.idempotency_key_hash or None,
            "request_fingerprint": self.request_fingerprint,
            "earth_reference_error_code": self.earth_reference_error_code or None,
            "initialize_access": self.initialize_access,
            "owner_fingerprint": _short_fingerprint(self.owner_auth_user_id, "usr"),
        }
        if self.metadata:
            payload["metadata"] = sanitize_metadata(self.metadata)
        if include_private:
            payload["owner_auth_user_id"] = self.owner_auth_user_id
        return payload


@dataclass(frozen=True, slots=True)
class ResourceIdentifiers:
    chunk_project_id: str
    universe_id: str
    world_id: str

    def to_dict(self) -> dict[str, str]:
        return {
            "chunk_project_id": self.chunk_project_id,
            "chunkProjectId": self.chunk_project_id,
            "universe_id": self.universe_id,
            "universeId": self.universe_id,
            "world_id": self.world_id,
            "worldId": self.world_id,
        }


@dataclass(frozen=True, slots=True)
class WorldPreparation:
    requested_template_id: str
    effective_template_id: str
    provider_id: str
    generator_type: str
    projection_type: str
    topology_type: str
    coordinate_system: str
    world_type: str
    world_role: str
    world_scope: str
    chunk_size: int
    cell_size: float
    surface_y: int
    min_y: int
    max_y: int
    spawn: Mapping[str, Any]
    provider_config: Mapping[str, Any]
    metadata: Mapping[str, Any]
    fallback_used: bool = False
    fallback_code: str = ""
    schema_version: str = WORLD_SPEC_SCHEMA_VERSION

    def to_dict(self, *, include_private: bool = False) -> dict[str, Any]:
        payload = {
            "schema_version": self.schema_version,
            "requested_template_id": self.requested_template_id,
            "requestedTemplateId": self.requested_template_id,
            "effective_template_id": self.effective_template_id,
            "effectiveTemplateId": self.effective_template_id,
            "provider_id": self.provider_id,
            "generator_type": self.generator_type,
            "projection_type": self.projection_type,
            "topology_type": self.topology_type,
            "coordinate_system": self.coordinate_system,
            "world_type": self.world_type,
            "world_role": self.world_role,
            "world_scope": self.world_scope,
            "chunk_size": self.chunk_size,
            "cell_size": self.cell_size,
            "surface_y": self.surface_y,
            "min_y": self.min_y,
            "max_y": self.max_y,
            "spawn": sanitize_metadata(self.spawn),
            "fallback_used": self.fallback_used,
            "fallback_code": self.fallback_code or None,
            "metadata": sanitize_metadata(self.metadata),
        }
        if include_private:
            payload["provider_config"] = sanitize_metadata(self.provider_config)
        return payload


@dataclass(frozen=True, slots=True)
class ProvisioningRecord:
    app_project_public_id: str
    chunk_project_id: str
    universe_id: str
    world_id: str
    project_name: str
    owner_auth_user_id: str
    requested_template_id: str
    effective_template_id: str
    fallback_used: bool
    fallback_code: str
    status: str
    access_status: str
    request_fingerprint: str
    source_service: str
    metadata: Mapping[str, Any] = field(default_factory=dict)
    world_metadata: Mapping[str, Any] = field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""

    @property
    def complete(self) -> bool:
        return bool(self.chunk_project_id and self.universe_id and self.world_id)

    def to_dict(self, *, include_private: bool = False) -> dict[str, Any]:
        payload = {
            "app_project_public_id": self.app_project_public_id,
            "appProjectPublicId": self.app_project_public_id,
            "chunk_project_id": self.chunk_project_id,
            "chunkProjectId": self.chunk_project_id,
            "universe_id": self.universe_id,
            "universeId": self.universe_id,
            "world_id": self.world_id,
            "worldId": self.world_id,
            "project_name": self.project_name,
            "requested_template_id": self.requested_template_id,
            "effective_template_id": self.effective_template_id,
            "fallback_used": self.fallback_used,
            "fallback_code": self.fallback_code or None,
            "status": self.status,
            "access_status": self.access_status,
            "request_fingerprint": self.request_fingerprint,
            "source_service": self.source_service,
            "owner_fingerprint": _short_fingerprint(self.owner_auth_user_id, "usr"),
            "complete": self.complete,
            "created_at": self.created_at or None,
            "updated_at": self.updated_at or None,
        }
        if self.metadata:
            payload["metadata"] = sanitize_metadata(self.metadata)
        if self.world_metadata:
            payload["world_metadata"] = sanitize_metadata(self.world_metadata)
        if include_private:
            payload["owner_auth_user_id"] = self.owner_auth_user_id
        return payload


@dataclass(frozen=True, slots=True)
class ProvisioningPlan:
    request: ProvisioningRequest
    identifiers: ResourceIdentifiers
    world: WorldPreparation
    existing: bool
    create_project: bool
    create_universe: bool
    create_world: bool
    create_block_registry_ref: bool
    update_project: bool
    update_metadata: bool
    initialize_access: bool
    idempotent: bool = False

    def to_dict(self, *, include_private: bool = False) -> dict[str, Any]:
        return {
            "request": self.request.to_dict(include_private=include_private),
            "identifiers": self.identifiers.to_dict(),
            "world": self.world.to_dict(include_private=include_private),
            "existing": self.existing,
            "create_project": self.create_project,
            "create_universe": self.create_universe,
            "create_world": self.create_world,
            "create_block_registry_ref": self.create_block_registry_ref,
            "update_project": self.update_project,
            "update_metadata": self.update_metadata,
            "initialize_access": self.initialize_access,
            "idempotent": self.idempotent,
        }


@dataclass(frozen=True, slots=True)
class ProvisioningResult:
    ok: bool
    status: str
    code: str
    status_code: int
    app_project_public_id: str
    chunk_project_id: str = ""
    universe_id: str = ""
    world_id: str = ""
    requested_template_id: str = ""
    effective_template_id: str = ""
    fallback_used: bool = False
    fallback_code: str = ""
    idempotent: bool = False
    created: bool = False
    verified: bool = False
    access_status: str = ACCESS_STATUS_PENDING
    access_sync_required: bool = True
    retryable: bool = False
    repair_required: bool = False
    error: str = ""
    request_id: str = ""
    correlation_id: str = ""
    request_fingerprint: str = ""
    owner_fingerprint: str = ""
    elapsed_ms: float = 0.0
    details: Mapping[str, Any] = field(default_factory=dict)
    record: Optional[ProvisioningRecord] = None
    schema_version: str = PROVISIONING_RESULT_SCHEMA_VERSION

    def to_dict(self, *, include_private: bool = False) -> dict[str, Any]:
        payload = {
            "schema_version": self.schema_version,
            "ok": self.ok,
            "status": self.status,
            "code": self.code,
            "status_code": self.status_code,
            "app_project_public_id": self.app_project_public_id or None,
            "chunk_project_id": self.chunk_project_id or None,
            "universe_id": self.universe_id or None,
            "world_id": self.world_id or None,
            "requested_template_id": self.requested_template_id or None,
            "effective_template_id": self.effective_template_id or None,
            "fallback_used": self.fallback_used,
            "fallback_code": self.fallback_code or None,
            "idempotent": self.idempotent,
            "created": self.created,
            "verified": self.verified,
            "access_status": self.access_status,
            "access_sync_required": self.access_sync_required,
            "retryable": self.retryable,
            "repair_required": self.repair_required,
            "error": self.error or None,
            "request_id": self.request_id or None,
            "correlation_id": self.correlation_id or None,
            "request_fingerprint": self.request_fingerprint or None,
            "owner_fingerprint": self.owner_fingerprint or None,
            "elapsed_ms": round(max(0.0, self.elapsed_ms), 3),
        }
        if self.details:
            payload["details"] = sanitize_metadata(self.details)
        if self.record is not None:
            payload["project"] = self.record.to_dict(include_private=include_private)
        return payload


class WorldProvider(Protocol):
    def prepare_world(
        self,
        template_id: str,
        request: ProvisioningRequest,
        identifiers: ResourceIdentifiers,
        *,
        settings: ProvisioningSettings,
    ) -> WorldPreparation:
        ...


class DefaultWorldProvider:
    def prepare_world(
        self,
        template_id: str,
        request: ProvisioningRequest,
        identifiers: ResourceIdentifiers,
        *,
        settings: ProvisioningSettings,
    ) -> WorldPreparation:
        normalized = _normalize_template_id(template_id, settings.default_template_id)
        if normalized == TEMPLATE_EARTH:
            return self._prepare_earth(request, identifiers, settings=settings)
        if normalized == TEMPLATE_FLAT:
            return self._prepare_flat(request, identifiers, settings=settings)
        raise ProjectProvisioningError(
            f"Unsupported world template: {normalized}.",
            code=CODE_TEMPLATE_UNSUPPORTED,
            status_code=422,
            details={"template_id": normalized},
        )

    def _prepare_earth(
        self,
        request: ProvisioningRequest,
        identifiers: ResourceIdentifiers,
        *,
        settings: ProvisioningSettings,
    ) -> WorldPreparation:
        if request.earth_reference_error_code:
            raise ProjectProvisioningError(
                "The supplied earth reference is invalid for Earth provisioning.",
                code=request.earth_reference_error_code,
                status_code=422,
            )
        reference = request.earth_reference
        if reference is None:
            if settings.require_earth_reference:
                raise ProjectProvisioningError(
                    "Earth provisioning requires a valid earth reference.",
                    code=CODE_EARTH_REFERENCE_REQUIRED,
                    status_code=422,
                )
            reference = EarthReference(
                latitude=0.0,
                longitude=0.0,
                height=settings.default_earth_height,
                crs_id=settings.earth_crs_id,
                source="configured-default",
            )
        if reference.crs_id not in {settings.earth_crs_id, "EPSG:4326", "EPSG:4979"}:
            raise ProjectProvisioningError(
                "The earth coordinate reference is unsupported.",
                code=CODE_EARTH_CRS_UNSUPPORTED,
                status_code=422,
                details={"crs_id": reference.crs_id},
            )
        return WorldPreparation(
            requested_template_id=TEMPLATE_EARTH,
            effective_template_id=TEMPLATE_EARTH,
            provider_id="earth-reference",
            generator_type="earth-reference-v1",
            projection_type="earth-local-tangent-v1",
            topology_type="earth-bounded-project-v1",
            coordinate_system=settings.earth_crs_id,
            world_type="runtime-world",
            world_role="default_spawn",
            world_scope="project",
            chunk_size=settings.default_chunk_size,
            cell_size=settings.default_cell_size,
            surface_y=settings.default_surface_y,
            min_y=settings.default_min_y,
            max_y=settings.default_max_y,
            spawn={
                "position": {"x": settings.default_spawn_x, "y": settings.default_spawn_y, "z": settings.default_spawn_z},
                "rotation": {"yaw": 0.0, "pitch": 0.0},
            },
            provider_config={
                "earth_reference": reference.to_dict(include_private=True),
                "origin_mode": "app-georeference",
                "chunk_project_id": identifiers.chunk_project_id,
            },
            metadata={
                "earth_reference": reference.to_dict(include_private=False),
                "origin_mode": "app-georeference",
            },
        )

    def _prepare_flat(
        self,
        request: ProvisioningRequest,
        identifiers: ResourceIdentifiers,
        *,
        settings: ProvisioningSettings,
    ) -> WorldPreparation:
        return WorldPreparation(
            requested_template_id=request.requested_template_id,
            effective_template_id=TEMPLATE_FLAT,
            provider_id=TEMPLATE_FLAT,
            generator_type="flat-world",
            projection_type="flat-local-v1",
            topology_type="flat-unbounded-v1",
            coordinate_system="vectoplan-world-y-up-v1",
            world_type="runtime-world",
            world_role="default_spawn",
            world_scope="project",
            chunk_size=settings.default_chunk_size,
            cell_size=settings.default_cell_size,
            surface_y=settings.default_surface_y,
            min_y=settings.default_min_y,
            max_y=settings.default_max_y,
            spawn={
                "position": {"x": settings.default_spawn_x, "y": settings.default_spawn_y, "z": settings.default_spawn_z},
                "rotation": {"yaw": 0.0, "pitch": 0.0},
            },
            provider_config={"layers": [{"y": settings.default_surface_y - 1, "block": "stone"}, {"y": settings.default_surface_y, "block": "grass"}]},
            metadata={"origin_mode": "flat-local"},
        )


class ProjectProvisioningRepository(Protocol):
    def get_by_app_project_id(self, app_project_public_id: str) -> Optional[ProvisioningRecord]: ...
    def get_by_chunk_project_id(self, chunk_project_id: str) -> Optional[ProvisioningRecord]: ...
    def create_project(self, values: Mapping[str, Any]) -> Any: ...
    def update_project(self, chunk_project_id: str, values: Mapping[str, Any]) -> Any: ...
    def create_universe(self, values: Mapping[str, Any]) -> Any: ...
    def create_world(self, values: Mapping[str, Any]) -> Any: ...
    def create_block_registry_ref(self, values: Mapping[str, Any]) -> Any: ...
    def set_provisioning_state(self, chunk_project_id: str, values: Mapping[str, Any]) -> None: ...
    def transaction(self, *, commit: bool = True) -> Any: ...
    def flush(self) -> None: ...
    def commit(self) -> None: ...
    def rollback(self) -> None: ...


class InMemoryProjectProvisioningRepository:
    """Thread-safe repository for tests and dependency-free deployments."""

    def __init__(self) -> None:
        self.projects: dict[str, dict[str, Any]] = {}
        self.app_index: dict[str, str] = {}
        self.universes: dict[str, dict[str, Any]] = {}
        self.worlds: dict[str, dict[str, Any]] = {}
        self.block_registry_refs: dict[str, dict[str, Any]] = {}
        self.states: dict[str, dict[str, Any]] = {}
        self.commits = 0
        self.flushes = 0
        self.rollbacks = 0
        self._lock = threading.RLock()
        self._depth = 0
        self._snapshot: Optional[dict[str, Any]] = None

    def _take_snapshot(self) -> dict[str, Any]:
        return {
            "projects": copy.deepcopy(self.projects),
            "app_index": copy.deepcopy(self.app_index),
            "universes": copy.deepcopy(self.universes),
            "worlds": copy.deepcopy(self.worlds),
            "block_registry_refs": copy.deepcopy(self.block_registry_refs),
            "states": copy.deepcopy(self.states),
        }

    def _restore_snapshot(self, snapshot: Mapping[str, Any]) -> None:
        self.projects = copy.deepcopy(snapshot.get("projects", {}))
        self.app_index = copy.deepcopy(snapshot.get("app_index", {}))
        self.universes = copy.deepcopy(snapshot.get("universes", {}))
        self.worlds = copy.deepcopy(snapshot.get("worlds", {}))
        self.block_registry_refs = copy.deepcopy(snapshot.get("block_registry_refs", {}))
        self.states = copy.deepcopy(snapshot.get("states", {}))

    def _record_from_project(self, project: Optional[Mapping[str, Any]]) -> Optional[ProvisioningRecord]:
        if not project:
            return None
        project_id = _safe_text(project.get("chunk_project_id") or project.get("id"), "", 255)
        state = self.states.get(project_id, {})
        return ProvisioningRecord(
            app_project_public_id=_safe_text(project.get("app_project_public_id"), "", 160),
            chunk_project_id=project_id,
            universe_id=_safe_text(project.get("universe_id"), "", 255),
            world_id=_safe_text(project.get("world_id"), "", 255),
            project_name=_safe_text(project.get("project_name") or project.get("name"), "", 255),
            owner_auth_user_id=_safe_text(project.get("owner_auth_user_id"), "", 255),
            requested_template_id=_normalize_template_id(project.get("requested_template_id"), TEMPLATE_EARTH),
            effective_template_id=_normalize_template_id(project.get("effective_template_id"), ""),
            fallback_used=_safe_bool(project.get("fallback_used"), False),
            fallback_code=_normalize_code(project.get("fallback_code")),
            status=_safe_text(state.get("status") or project.get("status"), STATUS_PENDING, 40),
            access_status=_safe_text(state.get("access_status") or project.get("access_status"), ACCESS_STATUS_PENDING, 40),
            request_fingerprint=_safe_text(project.get("request_fingerprint"), "", 128),
            source_service=_safe_text(project.get("source_service"), "", 120),
            metadata=_as_mapping(project.get("metadata")),
            world_metadata=_as_mapping(project.get("world_metadata")),
            created_at=_safe_text(project.get("created_at"), "", 64),
            updated_at=_safe_text(project.get("updated_at"), "", 64),
        )

    def get_by_app_project_id(self, app_project_public_id: str) -> Optional[ProvisioningRecord]:
        with self._lock:
            project_id = self.app_index.get(app_project_public_id)
            return self._record_from_project(self.projects.get(project_id)) if project_id else None

    def get_by_chunk_project_id(self, chunk_project_id: str) -> Optional[ProvisioningRecord]:
        with self._lock:
            return self._record_from_project(self.projects.get(chunk_project_id))

    def create_project(self, values: Mapping[str, Any]) -> dict[str, Any]:
        with self._lock:
            project_id = _safe_text(values.get("chunk_project_id") or values.get("id"), "", 255)
            app_id = _safe_text(values.get("app_project_public_id"), "", 160)
            if not project_id or not app_id:
                raise ValueError("project identifiers are required")
            if project_id in self.projects or app_id in self.app_index:
                raise ValueError("project already exists")
            item = copy.deepcopy(dict(values))
            item["id"] = project_id
            item["chunk_project_id"] = project_id
            item.setdefault("created_at", _utcnow_iso())
            item["updated_at"] = _utcnow_iso()
            self.projects[project_id] = item
            self.app_index[app_id] = project_id
            return copy.deepcopy(item)

    def update_project(self, chunk_project_id: str, values: Mapping[str, Any]) -> dict[str, Any]:
        with self._lock:
            if chunk_project_id not in self.projects:
                raise KeyError(chunk_project_id)
            self.projects[chunk_project_id].update(copy.deepcopy(dict(values)))
            self.projects[chunk_project_id]["updated_at"] = _utcnow_iso()
            return copy.deepcopy(self.projects[chunk_project_id])

    def create_universe(self, values: Mapping[str, Any]) -> dict[str, Any]:
        with self._lock:
            universe_id = _safe_text(values.get("universe_id") or values.get("id"), "", 255)
            if not universe_id or universe_id in self.universes:
                raise ValueError("invalid or existing universe")
            item = copy.deepcopy(dict(values))
            item["id"] = universe_id
            item["universe_id"] = universe_id
            item.setdefault("created_at", _utcnow_iso())
            item["updated_at"] = _utcnow_iso()
            self.universes[universe_id] = item
            return copy.deepcopy(item)

    def create_world(self, values: Mapping[str, Any]) -> dict[str, Any]:
        with self._lock:
            world_id = _safe_text(values.get("world_id") or values.get("id"), "", 255)
            if not world_id or world_id in self.worlds:
                raise ValueError("invalid or existing world")
            item = copy.deepcopy(dict(values))
            item["id"] = world_id
            item["world_id"] = world_id
            item.setdefault("created_at", _utcnow_iso())
            item["updated_at"] = _utcnow_iso()
            self.worlds[world_id] = item
            return copy.deepcopy(item)

    def create_block_registry_ref(self, values: Mapping[str, Any]) -> dict[str, Any]:
        with self._lock:
            key = _safe_text(
                values.get("ref_id") or f"{values.get('chunk_project_id')}:{values.get('registry_id')}",
                "",
                255,
            )
            if not key:
                raise ValueError("block registry reference id is required")
            item = copy.deepcopy(dict(values))
            item["ref_id"] = key
            self.block_registry_refs[key] = item
            return copy.deepcopy(item)

    def set_provisioning_state(self, chunk_project_id: str, values: Mapping[str, Any]) -> None:
        with self._lock:
            state = self.states.setdefault(chunk_project_id, {})
            state.update(copy.deepcopy(dict(values)))
            state["updated_at"] = _utcnow_iso()
            if chunk_project_id in self.projects:
                self.projects[chunk_project_id].update(
                    {
                        key: value
                        for key, value in state.items()
                        if key in {
                            "status", "access_status", "request_fingerprint",
                            "requested_template_id", "effective_template_id",
                            "fallback_used", "fallback_code",
                        }
                    }
                )
                self.projects[chunk_project_id]["updated_at"] = state["updated_at"]

    @contextmanager
    def transaction(self, *, commit: bool = True):
        with self._lock:
            outermost = self._depth == 0
            if outermost:
                self._snapshot = self._take_snapshot()
            self._depth += 1
            try:
                yield self
                self.flush()
                if commit and outermost:
                    self.commit()
            except Exception:
                if outermost and self._snapshot is not None:
                    self._restore_snapshot(self._snapshot)
                self.rollback()
                raise
            finally:
                self._depth = max(0, self._depth - 1)
                if outermost:
                    self._snapshot = None

    def flush(self) -> None:
        self.flushes += 1

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


class SQLAlchemyProjectProvisioningRepository:
    """Adaptive repository with injected session and models."""

    DEFAULT_FIELD_MAP = {
        "project": {
            "id": ("chunk_project_id", "public_id", "project_id", "id"),
            "app_project_public_id": ("app_project_public_id", "external_app_project_id", "source_project_id"),
            "name": ("name", "project_name", "display_name"),
            "owner_auth_user_id": ("owner_auth_user_id", "auth_owner_user_id"),
            "universe_id": ("universe_id", "default_universe_id"),
            "world_id": ("world_id", "default_world_id"),
            "requested_template_id": ("requested_template_id", "world_template_requested"),
            "effective_template_id": ("effective_template_id", "world_template_effective", "template_id"),
            "fallback_used": ("fallback_used", "world_fallback_used"),
            "fallback_code": ("fallback_code", "world_fallback_code"),
            "status": ("provisioning_status", "status"),
            "access_status": ("access_status", "access_sync_status"),
            "request_fingerprint": ("request_fingerprint", "provisioning_fingerprint"),
            "source_service": ("source_service", "created_by_service"),
            "metadata": ("metadata_json", "metadata"),
            "world_metadata": ("world_metadata", "world_metadata_json"),
        },
        "universe": {
            "id": ("universe_id", "public_id", "id"),
            "project_id": ("chunk_project_id", "project_id"),
            "name": ("name", "display_name"),
            "status": ("status",),
            "metadata": ("metadata_json", "metadata"),
        },
        "world": {
            "id": ("world_id", "public_id", "id"),
            "project_id": ("chunk_project_id", "project_id"),
            "universe_id": ("universe_id",),
            "name": ("name", "display_name"),
            "template_id": ("template_id", "world_template_id"),
            "provider_id": ("provider_id",),
            "generator_type": ("generator_type",),
            "projection_type": ("projection_type",),
            "topology_type": ("topology_type",),
            "coordinate_system": ("coordinate_system", "crs_id"),
            "world_type": ("world_type", "type"),
            "world_role": ("world_role", "role"),
            "world_scope": ("world_scope", "scope"),
            "chunk_size": ("chunk_size",),
            "cell_size": ("cell_size",),
            "surface_y": ("surface_y",),
            "min_y": ("min_y",),
            "max_y": ("max_y",),
            "spawn": ("spawn_json", "spawn"),
            "provider_config": ("provider_config", "generator_config"),
            "metadata": ("metadata_json", "metadata"),
            "status": ("status",),
        },
        "block_registry_ref": {
            "project_id": ("chunk_project_id", "project_id"),
            "registry_id": ("registry_id", "block_registry_id"),
            "registry_version": ("registry_version", "block_registry_version"),
            "status": ("status",),
            "metadata": ("metadata_json", "metadata"),
        },
    }

    def __init__(
        self,
        session: Any,
        *,
        project_model: Any,
        universe_model: Any,
        world_model: Any,
        block_registry_ref_model: Any = None,
        field_map: Optional[Mapping[str, Mapping[str, Any]]] = None,
    ) -> None:
        if session is None or project_model is None or universe_model is None or world_model is None:
            raise ValueError("session, project_model, universe_model and world_model are required")
        self.session = session
        self.project_model = project_model
        self.universe_model = universe_model
        self.world_model = world_model
        self.block_registry_ref_model = block_registry_ref_model
        self.field_map = copy.deepcopy(self.DEFAULT_FIELD_MAP)
        for section, mapping in _as_mapping(field_map).items():
            if section in self.field_map and isinstance(mapping, Mapping):
                self.field_map[section].update(dict(mapping))

    @staticmethod
    def _candidates(value: Any) -> tuple[str, ...]:
        return (value,) if isinstance(value, str) else tuple(
            item for item in (_safe_text(v, "", 120) for v in _as_sequence(value)) if item
        )

    def _resolve_attr(self, model_or_instance: Any, section: str, key: str) -> Optional[str]:
        for candidate in self._candidates(self.field_map.get(section, {}).get(key, (key,))):
            try:
                if hasattr(model_or_instance, candidate):
                    return candidate
            except Exception:
                pass
        return None

    def _set_values(self, instance: Any, section: str, values: Mapping[str, Any]) -> Any:
        for key, value in values.items():
            attr = self._resolve_attr(instance, section, key)
            if attr:
                setattr(instance, attr, copy.deepcopy(value))
        return instance

    def _get_value(self, instance: Any, section: str, key: str, default: Any = None) -> Any:
        attr = self._resolve_attr(instance, section, key)
        return getattr(instance, attr, default) if attr else default

    def _query_one(self, model: Any, attr: str, value: Any) -> Any:
        query = getattr(model, "query", None)
        if query is None and callable(getattr(self.session, "query", None)):
            query = self.session.query(model)
        if query is None:
            return None
        column = getattr(model, attr, None)
        if column is not None and hasattr(query, "filter"):
            return query.filter(column == value).one_or_none()
        if hasattr(query, "filter_by"):
            return query.filter_by(**{attr: value}).one_or_none()
        return None

    def _record(self, instance: Any) -> Optional[ProvisioningRecord]:
        if instance is None:
            return None
        return ProvisioningRecord(
            app_project_public_id=_safe_text(self._get_value(instance, "project", "app_project_public_id"), "", 160),
            chunk_project_id=_safe_text(self._get_value(instance, "project", "id"), "", 255),
            universe_id=_safe_text(self._get_value(instance, "project", "universe_id"), "", 255),
            world_id=_safe_text(self._get_value(instance, "project", "world_id"), "", 255),
            project_name=_safe_text(self._get_value(instance, "project", "name"), "", 255),
            owner_auth_user_id=_safe_text(self._get_value(instance, "project", "owner_auth_user_id"), "", 255),
            requested_template_id=_normalize_template_id(self._get_value(instance, "project", "requested_template_id"), TEMPLATE_EARTH),
            effective_template_id=_normalize_template_id(self._get_value(instance, "project", "effective_template_id"), ""),
            fallback_used=_safe_bool(self._get_value(instance, "project", "fallback_used"), False),
            fallback_code=_normalize_code(self._get_value(instance, "project", "fallback_code")),
            status=_safe_text(self._get_value(instance, "project", "status"), STATUS_PENDING, 40),
            access_status=_safe_text(self._get_value(instance, "project", "access_status"), ACCESS_STATUS_PENDING, 40),
            request_fingerprint=_safe_text(self._get_value(instance, "project", "request_fingerprint"), "", 128),
            source_service=_safe_text(self._get_value(instance, "project", "source_service"), "", 120),
            metadata=_as_mapping(self._get_value(instance, "project", "metadata")),
            world_metadata=_as_mapping(self._get_value(instance, "project", "world_metadata")),
            created_at=_safe_text(getattr(instance, "created_at", ""), "", 64),
            updated_at=_safe_text(getattr(instance, "updated_at", ""), "", 64),
        )

    def get_by_app_project_id(self, app_project_public_id: str) -> Optional[ProvisioningRecord]:
        attr = self._resolve_attr(self.project_model, "project", "app_project_public_id")
        return self._record(self._query_one(self.project_model, attr, app_project_public_id)) if attr else None

    def get_by_chunk_project_id(self, chunk_project_id: str) -> Optional[ProvisioningRecord]:
        attr = self._resolve_attr(self.project_model, "project", "id")
        return self._record(self._query_one(self.project_model, attr, chunk_project_id)) if attr else None

    def create_project(self, values: Mapping[str, Any]) -> Any:
        instance = self._set_values(self.project_model(), "project", values)
        self.session.add(instance)
        return instance

    def update_project(self, chunk_project_id: str, values: Mapping[str, Any]) -> Any:
        attr = self._resolve_attr(self.project_model, "project", "id")
        instance = self._query_one(self.project_model, attr, chunk_project_id) if attr else None
        if instance is None:
            raise KeyError(chunk_project_id)
        return self._set_values(instance, "project", values)

    def create_universe(self, values: Mapping[str, Any]) -> Any:
        instance = self._set_values(self.universe_model(), "universe", values)
        self.session.add(instance)
        return instance

    def create_world(self, values: Mapping[str, Any]) -> Any:
        instance = self._set_values(self.world_model(), "world", values)
        self.session.add(instance)
        return instance

    def create_block_registry_ref(self, values: Mapping[str, Any]) -> Any:
        if self.block_registry_ref_model is None:
            return None
        instance = self._set_values(self.block_registry_ref_model(), "block_registry_ref", values)
        self.session.add(instance)
        return instance

    def set_provisioning_state(self, chunk_project_id: str, values: Mapping[str, Any]) -> None:
        self.update_project(chunk_project_id, values)

    @contextmanager
    def transaction(self, *, commit: bool = True):
        savepoint = None
        try:
            if not commit and callable(getattr(self.session, "begin_nested", None)):
                savepoint = self.session.begin_nested()
            yield self
            self.flush()
            if commit:
                self.commit()
            elif savepoint is not None and callable(getattr(savepoint, "commit", None)):
                savepoint.commit()
        except Exception:
            if savepoint is not None and callable(getattr(savepoint, "rollback", None)):
                try:
                    savepoint.rollback()
                except Exception:
                    pass
            elif commit:
                self.rollback()
            raise

    def flush(self) -> None:
        if callable(getattr(self.session, "flush", None)):
            self.session.flush()

    def commit(self) -> None:
        if callable(getattr(self.session, "commit", None)):
            self.session.commit()

    def rollback(self) -> None:
        if callable(getattr(self.session, "rollback", None)):
            self.session.rollback()


def resolve_project_provisioning_repository(
    repository: Any = None,
    *,
    session: Any = None,
    project_model: Any = None,
    universe_model: Any = None,
    world_model: Any = None,
    block_registry_ref_model: Any = None,
    field_map: Optional[Mapping[str, Mapping[str, Any]]] = None,
) -> Any:
    if repository is not None:
        return repository
    if session is not None and project_model is not None and universe_model is not None and world_model is not None:
        return SQLAlchemyProjectProvisioningRepository(
            session,
            project_model=project_model,
            universe_model=universe_model,
            world_model=world_model,
            block_registry_ref_model=block_registry_ref_model,
            field_map=field_map,
        )
    raise ProjectProvisioningError(
        "A provisioning repository or SQLAlchemy model/session set is required.",
        code=CODE_DATABASE_ERROR,
        status_code=500,
        repair_required=True,
    )


def _principal_service_id(principal: Any) -> str:
    if principal is None:
        return ""
    if isinstance(principal, Mapping):
        value = principal.get("service_id") or principal.get("serviceId")
    else:
        value = getattr(principal, "service_id", getattr(principal, "serviceId", ""))
    return _normalize_identifier(value, "", 120).lower()


def _principal_authenticated(principal: Any) -> bool:
    if principal is None:
        return False
    if isinstance(principal, Mapping):
        value = principal.get("authenticated", principal.get("is_authenticated"))
    else:
        value = getattr(principal, "authenticated", getattr(principal, "is_authenticated", False))
    return _safe_bool(value, False)


def _principal_value(principal: Any, snake: str, camel: str) -> str:
    if principal is None:
        return ""
    if isinstance(principal, Mapping):
        value = principal.get(snake) or principal.get(camel)
    else:
        value = getattr(principal, snake, getattr(principal, camel, ""))
    return _safe_text(value, "", 160)


def require_project_provisioning_principal(
    principal: Any,
    *,
    settings: ProvisioningSettings,
) -> Any:
    if not _principal_authenticated(principal):
        raise ProjectProvisioningError(
            "An authenticated service principal is required.",
            code=CODE_SERVICE_UNAUTHENTICATED,
            status_code=401,
        )
    service_id = _principal_service_id(principal)
    if service_id not in set(settings.allowed_service_ids):
        raise ProjectProvisioningError(
            "The calling service is not allowed to provision Chunk projects.",
            code=CODE_SERVICE_FORBIDDEN,
            status_code=403,
            details={"service_id": service_id or None},
        )
    return principal


def build_provisioning_request(
    app_project_public_id: Any,
    payload: Any,
    *,
    principal: Any,
    request_id: str = "",
    correlation_id: str = "",
    idempotency_key: str = "",
    config: Any = None,
) -> ProvisioningRequest:
    settings = _load_provisioning_settings(config)
    data = _as_mapping(payload)
    if _json_size(data) > settings.max_request_bytes:
        raise ProjectProvisioningError(
            "The provisioning request is too large.",
            code=CODE_REQUEST_TOO_LARGE,
            status_code=413,
        )

    forbidden_keys = _detect_forbidden_identity_keys(data)
    if forbidden_keys:
        raise ProjectProvisioningError(
            "The provisioning request contains forbidden local identity fields.",
            code=CODE_OWNER_INVALID,
            status_code=400,
            details={"forbidden_fields": forbidden_keys},
        )

    path_app_id = _normalize_app_project_id(app_project_public_id)
    body_app_id_raw = (
        data.get("app_project_public_id")
        or data.get("appProjectPublicId")
        or data.get("external_app_project_id")
        or data.get("externalAppProjectId")
    )
    if body_app_id_raw:
        body_app_id = _normalize_app_project_id(body_app_id_raw)
        if not _constant_equal(path_app_id, body_app_id):
            raise ProjectProvisioningError(
                "The path and payload app project ids differ.",
                code=CODE_APP_PROJECT_ID_CONFLICT,
                status_code=409,
            )

    owner_auth_user_id = _normalize_auth_user_id(
        data.get("owner_auth_user_id")
        or data.get("ownerAuthUserId")
        or data.get("auth_user_id")
        or data.get("authUserId")
    )

    requested_template_id = _normalize_template_id(
        data.get("requested_template_id")
        or data.get("requestedTemplateId")
        or data.get("world_template")
        or data.get("worldTemplate")
        or data.get("template_id")
        or data.get("templateId")
        or settings.default_template_id,
        settings.default_template_id,
    )
    if requested_template_id not in set(settings.supported_template_ids):
        raise ProjectProvisioningError(
            f"Unsupported world template: {requested_template_id}.",
            code=CODE_TEMPLATE_UNSUPPORTED,
            status_code=422,
            details={"supported_template_ids": list(settings.supported_template_ids)},
        )

    earth_reference_error_code = ""
    try:
        earth_reference = build_earth_reference(
            data.get("earth_reference")
            or data.get("earthReference")
            or data.get("georeference")
            or data.get("geoReference")
            or data.get("coordinates"),
            default_crs_id=settings.earth_crs_id,
            default_height=settings.default_earth_height,
            required=False,
        )
    except ProjectProvisioningError as earth_error:
        if (
            requested_template_id == TEMPLATE_EARTH
            and settings.allow_fallback
            and earth_error.code in set(settings.fallback_error_codes)
            and earth_error.code not in FORBIDDEN_FALLBACK_CODES
            and earth_error.status_code < 500
        ):
            earth_reference = None
            earth_reference_error_code = earth_error.code
        else:
            raise

    metadata = sanitize_metadata(
        data.get("metadata")
        or data.get("metadata_json")
        or data.get("meta")
        or {}
    )
    if _json_size(metadata) > settings.max_metadata_bytes:
        raise ProjectProvisioningError(
            "Provisioning metadata is too large.",
            code=CODE_METADATA_TOO_LARGE,
            status_code=413,
        )

    resolved_request_id = (
        _safe_text(request_id, "", 160)
        or _principal_value(principal, "request_id", "requestId")
        or f"req_{hashlib.sha256(f'{path_app_id}:{time.time_ns()}'.encode()).hexdigest()[:24]}"
    )
    resolved_correlation_id = (
        _safe_text(correlation_id, "", 160)
        or _principal_value(principal, "correlation_id", "correlationId")
        or resolved_request_id
    )
    idempotency_hash = (
        _sha256(idempotency_key)
        if idempotency_key
        else _principal_value(principal, "idempotency_key_hash", "idempotencyKeyHash")
    )
    project_name = _safe_text(
        data.get("project_name")
        or data.get("projectName")
        or data.get("name")
        or data.get("title")
        or f"Project {path_app_id}",
        f"Project {path_app_id}",
        255,
    )
    source_service = _principal_service_id(principal) or settings.source_service
    initialize_access = _safe_bool(
        data.get("initialize_access", data.get("initializeAccess", True)),
        True,
    )
    fingerprint = _build_request_fingerprint(
        path_app_id,
        project_name,
        owner_auth_user_id,
        requested_template_id,
        earth_reference,
        {
            **_as_mapping(metadata),
            "_earth_reference_error_code": earth_reference_error_code or None,
        },
    )
    return ProvisioningRequest(
        app_project_public_id=path_app_id,
        project_name=project_name,
        owner_auth_user_id=owner_auth_user_id,
        requested_template_id=requested_template_id,
        earth_reference=earth_reference,
        metadata=metadata,
        source_service=source_service,
        request_id=resolved_request_id,
        correlation_id=resolved_correlation_id,
        idempotency_key_hash=idempotency_hash,
        request_fingerprint=fingerprint,
        earth_reference_error_code=earth_reference_error_code,
        initialize_access=initialize_access,
    )


def build_resource_identifiers(
    app_project_public_id: Any,
    *,
    config: Any = None,
) -> ResourceIdentifiers:
    settings = _load_provisioning_settings(config)
    app_id = _normalize_app_project_id(app_project_public_id)
    return ResourceIdentifiers(
        chunk_project_id=_deterministic_resource_id(settings.project_id_prefix, app_id, "project"),
        universe_id=_deterministic_resource_id(settings.universe_id_prefix, app_id, "universe"),
        world_id=_deterministic_resource_id(settings.world_id_prefix, app_id, "world"),
    )


def is_fallback_eligible(
    error: BaseException,
    *,
    requested_template_id: str,
    settings: ProvisioningSettings,
) -> bool:
    if requested_template_id == settings.fallback_template_id or not settings.allow_fallback:
        return False
    code = _normalize_code(getattr(error, "code", ""))
    status_code = _safe_int(getattr(error, "status_code", 500), 500)
    if not code or code in FORBIDDEN_FALLBACK_CODES:
        return False
    if status_code in {401, 403, 408, 429} or status_code >= 500:
        return False
    return code in set(settings.fallback_error_codes)


def prepare_world_with_fallback(
    request: ProvisioningRequest,
    identifiers: ResourceIdentifiers,
    *,
    provider: Optional[WorldProvider] = None,
    config: Any = None,
) -> WorldPreparation:
    settings = _load_provisioning_settings(config)
    resolved_provider = provider or DefaultWorldProvider()
    try:
        return resolved_provider.prepare_world(
            request.requested_template_id,
            request,
            identifiers,
            settings=settings,
        )
    except ProjectProvisioningError as error:
        if not is_fallback_eligible(
            error,
            requested_template_id=request.requested_template_id,
            settings=settings,
        ):
            raise
        try:
            fallback = resolved_provider.prepare_world(
                settings.fallback_template_id,
                request,
                identifiers,
                settings=settings,
            )
        except ProjectProvisioningError:
            raise
        except Exception as fallback_exc:
            raise ProjectProvisioningError(
                "The fallback world provider failed.",
                code=CODE_PROVIDER_ERROR,
                status_code=500,
                retryable=True,
                cause=fallback_exc,
            ) from fallback_exc
        return replace(
            fallback,
            requested_template_id=request.requested_template_id,
            effective_template_id=settings.fallback_template_id,
            fallback_used=True,
            fallback_code=error.code,
            metadata={
                **_as_mapping(fallback.metadata),
                "fallback": {
                    "used": True,
                    "from_template": request.requested_template_id,
                    "to_template": settings.fallback_template_id,
                    "reason_code": error.code,
                },
            },
        )
    except Exception as exc:
        raise ProjectProvisioningError(
            "The world provider failed unexpectedly.",
            code=CODE_PROVIDER_INITIALIZATION_FAILED,
            status_code=500,
            retryable=True,
            cause=exc,
        ) from exc


def _existing_template_compatible(
    existing: ProvisioningRecord,
    request: ProvisioningRequest,
    *,
    settings: ProvisioningSettings,
) -> bool:
    existing_requested = _normalize_template_id(
        existing.requested_template_id,
        existing.effective_template_id or request.requested_template_id,
    )
    existing_effective = _normalize_template_id(
        existing.effective_template_id,
        existing_requested,
    )
    if existing_requested == request.requested_template_id:
        return True
    return bool(
        request.requested_template_id == TEMPLATE_EARTH
        and existing_requested == TEMPLATE_EARTH
        and existing_effective == settings.fallback_template_id
        and existing.fallback_used
    )


def validate_existing_record(
    existing: ProvisioningRecord,
    request: ProvisioningRequest,
    identifiers: ResourceIdentifiers,
    *,
    settings: ProvisioningSettings,
) -> None:
    if not settings.allow_existing_by_external_id:
        raise ProjectProvisioningError(
            "A Chunk project already exists for this App project.",
            code=CODE_EXISTING_PROJECT_CONFLICT,
            status_code=409,
            repair_required=True,
        )
    if existing.chunk_project_id and not _constant_equal(existing.chunk_project_id, identifiers.chunk_project_id):
        raise ProjectProvisioningError(
            "The existing Chunk project id differs from the deterministic id.",
            code=CODE_EXISTING_PROJECT_CONFLICT,
            status_code=409,
            repair_required=True,
            details={
                "existing_chunk_project_id": existing.chunk_project_id,
                "expected_chunk_project_id": identifiers.chunk_project_id,
            },
        )
    if existing.owner_auth_user_id and not _constant_equal(existing.owner_auth_user_id, request.owner_auth_user_id):
        raise ProjectProvisioningError(
            "The existing owner differs. Use the dedicated owner-transfer operation.",
            code=CODE_OWNER_CONFLICT,
            status_code=409,
            repair_required=True,
            details={
                "existing_owner_fingerprint": _short_fingerprint(existing.owner_auth_user_id, "usr"),
                "requested_owner_fingerprint": _short_fingerprint(request.owner_auth_user_id, "usr"),
            },
        )
    if not _existing_template_compatible(existing, request, settings=settings):
        raise ProjectProvisioningError(
            "The existing world template cannot be changed by provisioning retry. "
            "Use a dedicated world-migration operation when template changes are enabled.",
            code=CODE_TEMPLATE_CHANGE_FORBIDDEN,
            status_code=409,
            repair_required=True,
            details={
                "existing_requested_template_id": existing.requested_template_id,
                "existing_effective_template_id": existing.effective_template_id,
                "requested_template_id": request.requested_template_id,
                "template_change_configured": settings.allow_template_change,
                "dedicated_operation_required": True,
            },
        )
    if existing.chunk_project_id and not existing.universe_id and not settings.create_universe:
        raise ProjectProvisioningError(
            "The existing Chunk project is missing its Universe.",
            code=CODE_EXISTING_PROJECT_INCOMPLETE,
            status_code=409,
            repair_required=True,
        )
    if existing.chunk_project_id and not existing.world_id and not settings.create_world:
        raise ProjectProvisioningError(
            "The existing Chunk project is missing its World.",
            code=CODE_EXISTING_PROJECT_INCOMPLETE,
            status_code=409,
            repair_required=True,
        )


_RESULT_CACHE: "OrderedDict[tuple[str, str], tuple[float, ProvisioningResult]]" = OrderedDict()
_RESULT_CACHE_LOCK = threading.RLock()
_PROJECT_LOCKS: "OrderedDict[str, threading.RLock]" = OrderedDict()
_PROJECT_LOCKS_LOCK = threading.RLock()


def _project_lock(app_project_public_id: str) -> threading.RLock:
    with _PROJECT_LOCKS_LOCK:
        lock = _PROJECT_LOCKS.get(app_project_public_id)
        if lock is None:
            lock = threading.RLock()
            _PROJECT_LOCKS[app_project_public_id] = lock
        else:
            _PROJECT_LOCKS.move_to_end(app_project_public_id)
        while len(_PROJECT_LOCKS) > _DEFAULT_LOCK_CACHE_SIZE:
            _PROJECT_LOCKS.popitem(last=False)
        return lock


def _cache_get(app_id: str, fingerprint: str, *, settings: ProvisioningSettings) -> Optional[ProvisioningResult]:
    if settings.result_cache_size <= 0 or settings.result_cache_ttl <= 0:
        return None
    key = (app_id, fingerprint)
    with _RESULT_CACHE_LOCK:
        item = _RESULT_CACHE.get(key)
        if not item:
            return None
        expires_at, result = item
        if expires_at <= time.monotonic():
            _RESULT_CACHE.pop(key, None)
            return None
        _RESULT_CACHE.move_to_end(key)
        return result


def _cache_put(result: ProvisioningResult, *, settings: ProvisioningSettings) -> None:
    if not result.ok or settings.result_cache_size <= 0 or settings.result_cache_ttl <= 0:
        return
    if not result.app_project_public_id or not result.request_fingerprint:
        return
    key = (result.app_project_public_id, result.request_fingerprint)
    with _RESULT_CACHE_LOCK:
        _RESULT_CACHE[key] = (time.monotonic() + settings.result_cache_ttl, result)
        _RESULT_CACHE.move_to_end(key)
        while len(_RESULT_CACHE) > settings.result_cache_size:
            _RESULT_CACHE.popitem(last=False)


def clear_project_provisioning_cache(app_project_public_id: str = "") -> None:
    target = _safe_text(app_project_public_id, "", 160)
    with _RESULT_CACHE_LOCK:
        if not target:
            _RESULT_CACHE.clear()
            return
        for key in list(_RESULT_CACHE):
            if key[0] == target:
                _RESULT_CACHE.pop(key, None)


def _project_values(
    request: ProvisioningRequest,
    identifiers: ResourceIdentifiers,
    world: WorldPreparation,
    *,
    status: str,
    access_status: str,
) -> dict[str, Any]:
    now = _utcnow_iso()
    return {
        "id": identifiers.chunk_project_id,
        "chunk_project_id": identifiers.chunk_project_id,
        "app_project_public_id": request.app_project_public_id,
        "name": request.project_name,
        "project_name": request.project_name,
        "owner_auth_user_id": request.owner_auth_user_id,
        "universe_id": identifiers.universe_id,
        "world_id": identifiers.world_id,
        "requested_template_id": request.requested_template_id,
        "effective_template_id": world.effective_template_id,
        "fallback_used": world.fallback_used,
        "fallback_code": world.fallback_code or None,
        "status": status,
        "access_status": access_status,
        "request_fingerprint": request.request_fingerprint,
        "source_service": request.source_service,
        "metadata": sanitize_metadata(request.metadata),
        "world_metadata": sanitize_metadata(world.metadata),
        "created_at": now,
        "updated_at": now,
    }


def _universe_values(
    request: ProvisioningRequest,
    identifiers: ResourceIdentifiers,
    *,
    settings: ProvisioningSettings,
) -> dict[str, Any]:
    return {
        "id": identifiers.universe_id,
        "universe_id": identifiers.universe_id,
        "chunk_project_id": identifiers.chunk_project_id,
        "project_id": identifiers.chunk_project_id,
        "name": settings.default_universe_name,
        "status": STATUS_READY,
        "metadata": {
            "app_project_public_id": request.app_project_public_id,
            "source_service": request.source_service,
        },
    }


def _world_values(
    request: ProvisioningRequest,
    identifiers: ResourceIdentifiers,
    world: WorldPreparation,
    *,
    settings: ProvisioningSettings,
) -> dict[str, Any]:
    return {
        "id": identifiers.world_id,
        "world_id": identifiers.world_id,
        "chunk_project_id": identifiers.chunk_project_id,
        "project_id": identifiers.chunk_project_id,
        "universe_id": identifiers.universe_id,
        "name": settings.default_world_name,
        "template_id": world.effective_template_id,
        "requested_template_id": request.requested_template_id,
        "effective_template_id": world.effective_template_id,
        "provider_id": world.provider_id,
        "generator_type": world.generator_type,
        "projection_type": world.projection_type,
        "topology_type": world.topology_type,
        "coordinate_system": world.coordinate_system,
        "world_type": world.world_type,
        "world_role": world.world_role,
        "world_scope": world.world_scope,
        "chunk_size": world.chunk_size,
        "cell_size": world.cell_size,
        "surface_y": world.surface_y,
        "min_y": world.min_y,
        "max_y": world.max_y,
        "spawn": sanitize_metadata(world.spawn),
        "provider_config": sanitize_metadata(world.provider_config),
        "metadata": sanitize_metadata(world.metadata),
        "status": STATUS_READY,
    }


def _block_registry_values(
    identifiers: ResourceIdentifiers,
    *,
    settings: ProvisioningSettings,
) -> dict[str, Any]:
    return {
        "ref_id": f"{identifiers.chunk_project_id}:{settings.block_registry_id}",
        "chunk_project_id": identifiers.chunk_project_id,
        "project_id": identifiers.chunk_project_id,
        "registry_id": settings.block_registry_id,
        "registry_version": settings.block_registry_version,
        "status": STATUS_READY,
        "metadata": {"source": "project-provisioning"},
    }


def _record_result(
    record: ProvisioningRecord,
    request: ProvisioningRequest,
    *,
    code: str,
    status_code: int = 200,
    idempotent: bool = False,
    created: bool = False,
    elapsed_ms: float = 0.0,
    details: Optional[Mapping[str, Any]] = None,
) -> ProvisioningResult:
    return ProvisioningResult(
        ok=True,
        status=record.status,
        code=code,
        status_code=status_code,
        app_project_public_id=record.app_project_public_id,
        chunk_project_id=record.chunk_project_id,
        universe_id=record.universe_id,
        world_id=record.world_id,
        requested_template_id=record.requested_template_id,
        effective_template_id=record.effective_template_id,
        fallback_used=record.fallback_used,
        fallback_code=record.fallback_code,
        idempotent=idempotent,
        created=created,
        verified=True,
        access_status=record.access_status,
        access_sync_required=record.access_status != ACCESS_STATUS_READY,
        request_id=request.request_id,
        correlation_id=request.correlation_id,
        request_fingerprint=request.request_fingerprint,
        owner_fingerprint=_short_fingerprint(record.owner_auth_user_id, "usr"),
        elapsed_ms=elapsed_ms,
        details=sanitize_metadata(details or {}),
        record=record,
    )


def _error_result(
    error: ProjectProvisioningError,
    *,
    app_project_public_id: str,
    request: Optional[ProvisioningRequest],
    elapsed_ms: float,
) -> ProvisioningResult:
    return ProvisioningResult(
        ok=False,
        status=STATUS_REPAIR_REQUIRED if error.repair_required else STATUS_FAILED,
        code=error.code,
        status_code=error.status_code,
        app_project_public_id=app_project_public_id,
        requested_template_id=request.requested_template_id if request else "",
        retryable=error.retryable,
        repair_required=error.repair_required,
        error=str(error),
        request_id=error.request_id or (request.request_id if request else ""),
        correlation_id=error.correlation_id or (request.correlation_id if request else ""),
        request_fingerprint=request.request_fingerprint if request else "",
        owner_fingerprint=_short_fingerprint(request.owner_auth_user_id, "usr") if request else "",
        elapsed_ms=elapsed_ms,
        details=error.details,
    )


def plan_project_provisioning(
    app_project_public_id: Any,
    payload: Any,
    *,
    repository: Any,
    principal: Any,
    provider: Optional[WorldProvider] = None,
    request_id: str = "",
    correlation_id: str = "",
    idempotency_key: str = "",
    config: Any = None,
) -> ProvisioningPlan:
    settings = _load_provisioning_settings(config)
    require_project_provisioning_principal(principal, settings=settings)
    request = build_provisioning_request(
        app_project_public_id,
        payload,
        principal=principal,
        request_id=request_id,
        correlation_id=correlation_id,
        idempotency_key=idempotency_key,
        config=config,
    )
    identifiers = build_resource_identifiers(request.app_project_public_id, config=config)
    existing = repository.get_by_app_project_id(request.app_project_public_id)
    if existing is not None:
        validate_existing_record(existing, request, identifiers, settings=settings)
        if existing.complete and existing.status in {STATUS_READY, STATUS_FALLBACK_READY}:
            world = WorldPreparation(
                requested_template_id=existing.requested_template_id,
                effective_template_id=existing.effective_template_id,
                provider_id=existing.effective_template_id,
                generator_type="existing",
                projection_type="existing",
                topology_type="existing",
                coordinate_system="existing",
                world_type="runtime-world",
                world_role="default_spawn",
                world_scope="project",
                chunk_size=settings.default_chunk_size,
                cell_size=settings.default_cell_size,
                surface_y=settings.default_surface_y,
                min_y=settings.default_min_y,
                max_y=settings.default_max_y,
                spawn={},
                provider_config={},
                metadata=existing.world_metadata,
                fallback_used=existing.fallback_used,
                fallback_code=existing.fallback_code,
            )
            return ProvisioningPlan(
                request=request,
                identifiers=ResourceIdentifiers(existing.chunk_project_id, existing.universe_id, existing.world_id),
                world=world,
                existing=True,
                create_project=False,
                create_universe=False,
                create_world=False,
                create_block_registry_ref=False,
                update_project=settings.allow_name_update and existing.project_name != request.project_name,
                update_metadata=settings.allow_metadata_update and sanitize_metadata(existing.metadata) != sanitize_metadata(request.metadata),
                initialize_access=request.initialize_access and existing.access_status != ACCESS_STATUS_READY,
                idempotent=True,
            )

    world = prepare_world_with_fallback(request, identifiers, provider=provider, config=config)
    return ProvisioningPlan(
        request=request,
        identifiers=identifiers,
        world=world,
        existing=existing is not None,
        create_project=existing is None,
        create_universe=settings.create_universe and (existing is None or not existing.universe_id),
        create_world=settings.create_world and (existing is None or not existing.world_id),
        create_block_registry_ref=settings.create_block_registry_ref and existing is None,
        update_project=existing is not None,
        update_metadata=existing is not None and settings.allow_metadata_update,
        initialize_access=request.initialize_access,
        idempotent=False,
    )


def _call_access_initializer(
    initializer: Optional[Callable[..., Any]],
    request: ProvisioningRequest,
    record: ProvisioningRecord,
    *,
    principal: Any,
    repository: Any,
    config: Any,
    kwargs: Optional[Mapping[str, Any]] = None,
) -> tuple[str, Mapping[str, Any]]:
    if not request.initialize_access:
        return ACCESS_STATUS_DISABLED, {"initialized": False, "reason": "request_disabled"}
    if initializer is None:
        candidate = getattr(repository, "initialize_owner_access", None)
        initializer = candidate if callable(candidate) else None
    if initializer is None:
        return ACCESS_STATUS_PENDING, {"initialized": False, "reason": "initializer_unavailable"}

    payload = {
        "owner_auth_user_id": request.owner_auth_user_id,
        "assignments": [
            {
                "auth_user_id": request.owner_auth_user_id,
                "role": "owner",
                "assignment_type": "user",
                "direct": True,
            }
        ],
        "projection_version": "app-project-access-v1",
        "source_service": request.source_service,
    }
    call_kwargs = dict(kwargs or {})
    call_kwargs.setdefault("principal", principal)
    call_kwargs.setdefault("request_id", request.request_id)
    call_kwargs.setdefault("correlation_id", request.correlation_id)
    call_kwargs.setdefault("config", config)

    try:
        try:
            result = initializer(record.chunk_project_id, payload, **call_kwargs)
        except TypeError:
            result = initializer(record.chunk_project_id, request.owner_auth_user_id)
    except Exception as exc:
        return ACCESS_STATUS_REPAIR_REQUIRED, {
            "initialized": False,
            "code": _normalize_code(getattr(exc, "code", CODE_ACCESS_INITIALIZATION_FAILED)),
            "retryable": _safe_bool(getattr(exc, "retryable", False), False),
            "repair_required": _safe_bool(getattr(exc, "repair_required", True), True),
        }

    if isinstance(result, Mapping):
        data = dict(result)
    elif hasattr(result, "to_dict") and callable(result.to_dict):
        try:
            data = _as_mapping(result.to_dict())
        except Exception:
            data = {}
    else:
        data = {
            "ok": _safe_bool(getattr(result, "ok", result is not False), result is not False),
            "status": _safe_text(getattr(result, "status", ""), "", 40),
            "code": _normalize_code(getattr(result, "code", "")),
        }

    ok = _safe_bool(data.get("ok"), True)
    status = _safe_text(data.get("status"), "", 40).lower()
    if ok and status in {"ready", "ok", "synchronized", "synced"}:
        return ACCESS_STATUS_READY, sanitize_metadata(data)
    if ok:
        return ACCESS_STATUS_PENDING, sanitize_metadata(data)
    if _safe_bool(data.get("repair_required", data.get("repairRequired", False)), False):
        return ACCESS_STATUS_REPAIR_REQUIRED, sanitize_metadata(data)
    return ACCESS_STATUS_FAILED, sanitize_metadata(data)


def provision_project_for_app(
    app_project_public_id: Any,
    payload: Any,
    *,
    repository: Any = None,
    session: Any = None,
    project_model: Any = None,
    universe_model: Any = None,
    world_model: Any = None,
    block_registry_ref_model: Any = None,
    field_map: Optional[Mapping[str, Mapping[str, Any]]] = None,
    principal: Any = None,
    provider: Optional[WorldProvider] = None,
    access_initializer: Optional[Callable[..., Any]] = None,
    access_initializer_kwargs: Optional[Mapping[str, Any]] = None,
    request_id: str = "",
    correlation_id: str = "",
    idempotency_key: str = "",
    commit: bool = True,
    dry_run: bool = False,
    force: bool = False,
    raise_on_error: bool = False,
    config: Any = None,
) -> ProvisioningResult:
    started = time.perf_counter()
    request: Optional[ProvisioningRequest] = None
    repo: Any = None
    app_id = _safe_text(app_project_public_id, "", 160)

    try:
        settings = _load_provisioning_settings(config)
        app_id = _normalize_app_project_id(app_project_public_id)

        if not settings.enabled:
            return ProvisioningResult(
                ok=True,
                status=STATUS_DISABLED,
                code=CODE_DISABLED,
                status_code=200,
                app_project_public_id=app_id,
                access_status=ACCESS_STATUS_DISABLED,
                access_sync_required=False,
                request_id=_safe_text(request_id, "", 160),
                correlation_id=_safe_text(correlation_id or request_id, "", 160),
                elapsed_ms=(time.perf_counter() - started) * 1000.0,
            )
        if not settings.runtime_business_mutations_enabled and not dry_run:
            raise ProjectProvisioningError(
                "Runtime business mutations are disabled.",
                code=CODE_MUTATIONS_DISABLED,
                status_code=503,
                retryable=True,
            )

        resolved_principal = require_project_provisioning_principal(principal, settings=settings)
        request = build_provisioning_request(
            app_id,
            payload,
            principal=resolved_principal,
            request_id=request_id,
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
            config=config,
        )
        repo = resolve_project_provisioning_repository(
            repository,
            session=session,
            project_model=project_model,
            universe_model=universe_model,
            world_model=world_model,
            block_registry_ref_model=block_registry_ref_model,
            field_map=field_map,
        )

        with _project_lock(app_id):
            if force:
                clear_project_provisioning_cache(app_id)

            if settings.idempotent and not force and not dry_run:
                cached = _cache_get(app_id, request.request_fingerprint, settings=settings)
                if cached is not None:
                    current = repo.get_by_app_project_id(app_id)
                    if (
                        current is not None
                        and current.complete
                        and current.status in {STATUS_READY, STATUS_FALLBACK_READY}
                        and _constant_equal(current.request_fingerprint, request.request_fingerprint)
                        and (
                            current.access_status == ACCESS_STATUS_READY
                            or access_initializer is None
                        )
                    ):
                        return replace(
                            cached,
                            code=CODE_IDEMPOTENT,
                            status_code=200,
                            idempotent=True,
                            created=False,
                            request_id=request.request_id,
                            correlation_id=request.correlation_id,
                            elapsed_ms=(time.perf_counter() - started) * 1000.0,
                            record=current,
                        )

            plan = plan_project_provisioning(
                app_id,
                payload,
                repository=repo,
                principal=resolved_principal,
                provider=provider,
                request_id=request.request_id,
                correlation_id=request.correlation_id,
                idempotency_key=idempotency_key,
                config=config,
            )

            if dry_run:
                return ProvisioningResult(
                    ok=True,
                    status=STATUS_PENDING,
                    code=CODE_DRY_RUN,
                    status_code=200,
                    app_project_public_id=app_id,
                    chunk_project_id=plan.identifiers.chunk_project_id,
                    universe_id=plan.identifiers.universe_id,
                    world_id=plan.identifiers.world_id,
                    requested_template_id=request.requested_template_id,
                    effective_template_id=plan.world.effective_template_id,
                    fallback_used=plan.world.fallback_used,
                    fallback_code=plan.world.fallback_code,
                    idempotent=plan.idempotent,
                    created=False,
                    verified=False,
                    access_status=ACCESS_STATUS_PENDING,
                    access_sync_required=plan.initialize_access,
                    request_id=request.request_id,
                    correlation_id=request.correlation_id,
                    request_fingerprint=request.request_fingerprint,
                    owner_fingerprint=_short_fingerprint(request.owner_auth_user_id, "usr"),
                    elapsed_ms=(time.perf_counter() - started) * 1000.0,
                    details={"plan": plan.to_dict(include_private=False)},
                )

            existing = repo.get_by_app_project_id(app_id)
            if plan.idempotent and existing is not None:
                updates: dict[str, Any] = {}
                if plan.update_project:
                    updates.update({"name": request.project_name, "project_name": request.project_name})
                if plan.update_metadata:
                    updates["metadata"] = sanitize_metadata(request.metadata)
                if updates:
                    with repo.transaction(commit=commit):
                        repo.update_project(existing.chunk_project_id, updates)
                    existing = repo.get_by_app_project_id(app_id) or existing

                access_status, access_details = _call_access_initializer(
                    access_initializer,
                    request,
                    existing,
                    principal=resolved_principal,
                    repository=repo,
                    config=config,
                    kwargs=access_initializer_kwargs,
                )
                if access_status != existing.access_status:
                    try:
                        with repo.transaction(commit=commit):
                            repo.set_provisioning_state(
                                existing.chunk_project_id,
                                {
                                    "status": existing.status,
                                    "access_status": access_status,
                                    "request_id": request.request_id,
                                    "correlation_id": request.correlation_id,
                                },
                            )
                    except Exception:
                        pass
                    existing = replace(existing, access_status=access_status)

                result = _record_result(
                    existing,
                    request,
                    code=CODE_IDEMPOTENT,
                    idempotent=True,
                    created=False,
                    elapsed_ms=(time.perf_counter() - started) * 1000.0,
                    details={"access": access_details},
                )
                _cache_put(result, settings=settings)
                return result

            values = _project_values(
                request,
                plan.identifiers,
                plan.world,
                status=STATUS_PROVISIONING,
                access_status=ACCESS_STATUS_PENDING,
            )

            try:
                with repo.transaction(commit=commit):
                    if plan.create_project:
                        repo.create_project(values)
                    else:
                        repo.update_project(plan.identifiers.chunk_project_id, values)

                    if plan.create_universe:
                        repo.create_universe(
                            _universe_values(request, plan.identifiers, settings=settings)
                        )
                    if plan.create_world:
                        repo.create_world(
                            _world_values(request, plan.identifiers, plan.world, settings=settings)
                        )
                    if plan.create_block_registry_ref:
                        repo.create_block_registry_ref(
                            _block_registry_values(plan.identifiers, settings=settings)
                        )

                    ready_status = STATUS_FALLBACK_READY if plan.world.fallback_used else STATUS_READY
                    repo.set_provisioning_state(
                        plan.identifiers.chunk_project_id,
                        {
                            "status": ready_status,
                            "access_status": ACCESS_STATUS_PENDING,
                            "request_fingerprint": request.request_fingerprint,
                            "requested_template_id": request.requested_template_id,
                            "effective_template_id": plan.world.effective_template_id,
                            "fallback_used": plan.world.fallback_used,
                            "fallback_code": plan.world.fallback_code or None,
                            "request_id": request.request_id,
                            "correlation_id": request.correlation_id,
                        },
                    )
            except ProjectProvisioningError:
                raise
            except Exception as exc:
                raise ProjectProvisioningError(
                    "The Chunk project could not be persisted.",
                    code=CODE_DATABASE_ERROR,
                    status_code=500,
                    retryable=True,
                    cause=exc,
                    request_id=request.request_id,
                    correlation_id=request.correlation_id,
                ) from exc

            record = repo.get_by_app_project_id(app_id)
            if record is None or not record.complete:
                raise ProjectProvisioningError(
                    "The persisted Chunk project could not be verified.",
                    code=CODE_VERIFICATION_FAILED,
                    status_code=500,
                    retryable=True,
                    repair_required=True,
                    request_id=request.request_id,
                    correlation_id=request.correlation_id,
                )
            if not _constant_equal(record.owner_auth_user_id, request.owner_auth_user_id):
                raise ProjectProvisioningError(
                    "The persisted owner identity does not match the request.",
                    code=CODE_VERIFICATION_FAILED,
                    status_code=500,
                    repair_required=True,
                    request_id=request.request_id,
                    correlation_id=request.correlation_id,
                )
            if not _existing_template_compatible(record, request, settings=settings):
                raise ProjectProvisioningError(
                    "The persisted world template does not match the request.",
                    code=CODE_VERIFICATION_FAILED,
                    status_code=500,
                    repair_required=True,
                    request_id=request.request_id,
                    correlation_id=request.correlation_id,
                )

            access_status, access_details = _call_access_initializer(
                access_initializer,
                request,
                record,
                principal=resolved_principal,
                repository=repo,
                config=config,
                kwargs=access_initializer_kwargs,
            )
            if access_status != record.access_status:
                try:
                    with repo.transaction(commit=commit):
                        repo.set_provisioning_state(
                            record.chunk_project_id,
                            {
                                "status": record.status,
                                "access_status": access_status,
                                "request_id": request.request_id,
                                "correlation_id": request.correlation_id,
                            },
                        )
                except Exception:
                    access_status = ACCESS_STATUS_REPAIR_REQUIRED
                    access_details = {
                        **_as_mapping(access_details),
                        "state_update_failed": True,
                    }
                record = replace(record, access_status=access_status)

            result = _record_result(
                record,
                request,
                code=CODE_FALLBACK if record.fallback_used else CODE_OK,
                status_code=201,
                idempotent=False,
                created=True,
                elapsed_ms=(time.perf_counter() - started) * 1000.0,
                details={"access": access_details},
            )
            _cache_put(result, settings=settings)
            return result

    except ProjectProvisioningError as error:
        if request is not None:
            error.request_id = error.request_id or request.request_id
            error.correlation_id = error.correlation_id or request.correlation_id

        if repo is not None and app_id:
            try:
                existing = repo.get_by_app_project_id(app_id)
                if existing is not None:
                    with repo.transaction(commit=commit):
                        repo.set_provisioning_state(
                            existing.chunk_project_id,
                            {
                                "status": STATUS_REPAIR_REQUIRED if error.repair_required else STATUS_FAILED,
                                "access_status": existing.access_status,
                                "error_code": error.code,
                                "request_id": error.request_id,
                                "correlation_id": error.correlation_id,
                            },
                        )
            except Exception:
                pass

        if raise_on_error:
            raise
        return _error_result(
            error,
            app_project_public_id=app_id,
            request=request,
            elapsed_ms=(time.perf_counter() - started) * 1000.0,
        )

    except Exception as exc:
        error = ProjectProvisioningError(
            "Project provisioning failed unexpectedly.",
            code=CODE_INTERNAL_ERROR,
            status_code=500,
            retryable=True,
            cause=exc,
            request_id=request.request_id if request else _safe_text(request_id, "", 160),
            correlation_id=request.correlation_id if request else _safe_text(correlation_id or request_id, "", 160),
        )
        if raise_on_error:
            raise error from exc
        return _error_result(
            error,
            app_project_public_id=app_id,
            request=request,
            elapsed_ms=(time.perf_counter() - started) * 1000.0,
        )


def ensure_project_for_app(*args: Any, **kwargs: Any) -> ProvisioningResult:
    return provision_project_for_app(*args, **kwargs)


def preview_project_for_app(*args: Any, **kwargs: Any) -> ProvisioningResult:
    kwargs["dry_run"] = True
    kwargs.setdefault("commit", False)
    return provision_project_for_app(*args, **kwargs)


def get_project_provisioning_status(
    app_project_public_id: Any,
    *,
    repository: Any = None,
    session: Any = None,
    project_model: Any = None,
    universe_model: Any = None,
    world_model: Any = None,
    block_registry_ref_model: Any = None,
    field_map: Optional[Mapping[str, Mapping[str, Any]]] = None,
    include_private: bool = False,
) -> dict[str, Any]:
    try:
        app_id = _normalize_app_project_id(app_project_public_id)
        repo = resolve_project_provisioning_repository(
            repository,
            session=session,
            project_model=project_model,
            universe_model=universe_model,
            world_model=world_model,
            block_registry_ref_model=block_registry_ref_model,
            field_map=field_map,
        )
        record = repo.get_by_app_project_id(app_id)
        if record is None:
            return {
                "ok": True,
                "found": False,
                "app_project_public_id": app_id,
                "status": "not_found",
            }
        return {
            "ok": True,
            "found": True,
            "status": record.status,
            "project": record.to_dict(include_private=include_private),
        }
    except ProjectProvisioningError as error:
        return error.to_dict(include_private=include_private)
    except Exception as exc:
        return ProjectProvisioningError(
            "Provisioning status could not be read.",
            code=CODE_DATABASE_ERROR,
            status_code=500,
            retryable=True,
            cause=exc,
        ).to_dict(include_private=include_private)


def serialize_provisioning_result(
    result: Any,
    *,
    include_private: bool = False,
) -> dict[str, Any]:
    if result is None:
        return {}
    if isinstance(result, ProvisioningResult):
        return result.to_dict(include_private=include_private)
    if isinstance(result, ProjectProvisioningError):
        return result.to_dict(include_private=include_private)
    if hasattr(result, "to_dict") and callable(result.to_dict):
        try:
            return result.to_dict(include_private=include_private)
        except TypeError:
            return result.to_dict()
        except Exception:
            return {}
    return sanitize_metadata(result) if isinstance(result, Mapping) else {}


def get_project_provisioning_service_status(*, config: Any = None) -> dict[str, Any]:
    settings = _load_provisioning_settings(config)
    with _RESULT_CACHE_LOCK:
        cache_entries = len(_RESULT_CACHE)
    with _PROJECT_LOCKS_LOCK:
        lock_entries = len(_PROJECT_LOCKS)
    return {
        "ok": True,
        "service": "project_provisioning_service",
        "version": SERVICE_VERSION,
        "schema_version": PROVISIONING_SCHEMA_VERSION,
        "enabled": settings.enabled,
        "idempotent": settings.idempotent,
        "runtime_business_mutations_enabled": settings.runtime_business_mutations_enabled,
        "source_service": settings.source_service,
        "allowed_service_ids": list(settings.allowed_service_ids),
        "default_template_id": settings.default_template_id,
        "fallback_template_id": settings.fallback_template_id,
        "supported_template_ids": list(settings.supported_template_ids),
        "allow_fallback": settings.allow_fallback,
        "allow_template_change": settings.allow_template_change,
        "fallback_error_codes": list(settings.fallback_error_codes),
        "earth_crs_id": settings.earth_crs_id,
        "require_earth_reference": settings.require_earth_reference,
        "canonical_owner_field": "auth_user_id",
        "local_user_ids_accepted": False,
        "outbound_http": False,
        "schema_mutations": False,
        "cache": {
            "entries": cache_entries,
            "max_entries": settings.result_cache_size,
            "ttl_seconds": settings.result_cache_ttl,
        },
        "locks": {
            "entries": lock_entries,
            "max_entries": _DEFAULT_LOCK_CACHE_SIZE,
        },
    }


__all__ = [
    "SERVICE_VERSION",
    "PROVISIONING_SCHEMA_VERSION",
    "PROVISIONING_REQUEST_SCHEMA_VERSION",
    "PROVISIONING_RESULT_SCHEMA_VERSION",
    "WORLD_SPEC_SCHEMA_VERSION",
    "TEMPLATE_EARTH",
    "TEMPLATE_FLAT",
    "STATUS_DISABLED",
    "STATUS_PENDING",
    "STATUS_PROVISIONING",
    "STATUS_READY",
    "STATUS_FALLBACK_READY",
    "STATUS_FAILED",
    "STATUS_REPAIR_REQUIRED",
    "ACCESS_STATUS_PENDING",
    "ACCESS_STATUS_READY",
    "ACCESS_STATUS_FAILED",
    "ACCESS_STATUS_REPAIR_REQUIRED",
    "ACCESS_STATUS_DISABLED",
    "CODE_OK",
    "CODE_IDEMPOTENT",
    "CODE_DRY_RUN",
    "CODE_FALLBACK",
    "CODE_DISABLED",
    "CODE_MUTATIONS_DISABLED",
    "CODE_APP_PROJECT_ID_REQUIRED",
    "CODE_APP_PROJECT_ID_INVALID",
    "CODE_APP_PROJECT_ID_CONFLICT",
    "CODE_OWNER_REQUIRED",
    "CODE_OWNER_INVALID",
    "CODE_OWNER_CONFLICT",
    "CODE_SERVICE_UNAUTHENTICATED",
    "CODE_SERVICE_FORBIDDEN",
    "CODE_TEMPLATE_UNSUPPORTED",
    "CODE_TEMPLATE_CHANGE_FORBIDDEN",
    "CODE_EARTH_REFERENCE_REQUIRED",
    "CODE_EARTH_REFERENCE_INCOMPLETE",
    "CODE_EARTH_REFERENCE_INVALID",
    "CODE_EARTH_CRS_UNSUPPORTED",
    "CODE_METADATA_TOO_LARGE",
    "CODE_REQUEST_TOO_LARGE",
    "CODE_EXISTING_PROJECT_INCOMPLETE",
    "CODE_EXISTING_PROJECT_CONFLICT",
    "CODE_PROVIDER_ERROR",
    "CODE_PROVIDER_INITIALIZATION_FAILED",
    "CODE_DATABASE_ERROR",
    "CODE_VERIFICATION_FAILED",
    "CODE_ACCESS_INITIALIZATION_FAILED",
    "CODE_INTERNAL_ERROR",
    "DEFAULT_FALLBACK_CODES",
    "FORBIDDEN_FALLBACK_CODES",
    "ProvisioningSettings",
    "ProjectProvisioningError",
    "EarthReference",
    "ProvisioningRequest",
    "ResourceIdentifiers",
    "WorldPreparation",
    "ProvisioningRecord",
    "ProvisioningPlan",
    "ProvisioningResult",
    "WorldProvider",
    "DefaultWorldProvider",
    "ProjectProvisioningRepository",
    "InMemoryProjectProvisioningRepository",
    "SQLAlchemyProjectProvisioningRepository",
    "sanitize_metadata",
    "build_earth_reference",
    "build_provisioning_request",
    "build_resource_identifiers",
    "is_fallback_eligible",
    "prepare_world_with_fallback",
    "validate_existing_record",
    "resolve_project_provisioning_repository",
    "require_project_provisioning_principal",
    "plan_project_provisioning",
    "provision_project_for_app",
    "ensure_project_for_app",
    "preview_project_for_app",
    "get_project_provisioning_status",
    "serialize_provisioning_result",
    "clear_project_provisioning_cache",
    "clear_project_provisioning_settings_cache",
    "get_project_provisioning_service_status",
]
